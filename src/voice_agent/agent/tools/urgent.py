"""Tool for urgent issues: email the Summit Air team instead of booking a technician call.

Extreme emergencies (gas, fire, carbon monoxide) don't come here: the caller is told to leave and call 911,
and the call ends. This is for urgent but safe situations, and it waits until the team can act on it.
"""

from datetime import datetime
from uuid import uuid4

import structlog
from langchain_core.tools import tool
from langgraph.prebuilt import ToolRuntime

from voice_agent.agent.details import DETAILS, details_needed, full_address
from voice_agent.agent.services.composio import ComposioError
from voice_agent.agent.services.schedule import spoken_time
from voice_agent.agent.state import AgentContext, CallState
from voice_agent.agent.tools.scheduling import serving_technicians

logger = structlog.get_logger()


@tool
def report_urgent_issue(runtime: ToolRuntime[AgentContext, CallState]) -> str:
    """Email the Summit Air team about an urgent issue, using the details collected on this call.

    Use this instead of booking a technician call. One email is sent per call; it cannot be updated afterwards.
    """
    context, state = runtime.context, runtime.state
    settings, call_id = context.settings, state["call_id"]
    facts = state.get("facts", {})
    if missing := details_needed(state):
        logger.info("missing details", call_id=call_id, missing=missing, facts=facts)
        return f"Not sent: {missing}"
    details = {name: facts[name] for name in DETAILS}
    # The team can't act on an address no technician covers, however urgent it is.
    if not serving_technicians(context, full_address(details)):
        return "Not sent: no technician covers that address. Tell the caller it is outside our service area."

    with context.storage.lock:
        sent = context.storage.urgent_requests(call_id=call_id)
        if sent:
            return (
                f"Already sent on this call: urgent request {sent[0]['id']}. The team has those details and will "
                "reach out; this email can't be changed."
            )

        request_id = f"UR-{uuid4().hex[:8].upper()}"
        now = datetime.now(settings.timezone)
        lines = [
            f"Urgent request {request_id}, from the Summit Air phone assistant.",
            "",
            f"Issue: {details['issue']}",
            f"Name: {details['customer_name']}",
            f"Call back on: {details['phone']}",
            f"Calling from: {state.get('caller_phone') or 'unknown'}",
            f"Address: {full_address(details)}",
            f"Reported: {now:%A, %B} {now.day}, {spoken_time(now)}",
        ]
        for booked in context.storage.service_calls(call_id=call_id, status="booked"):
            lines.append(f"Already booked on this call: technician call {booked['id']} at {booked['start']}")

        try:
            context.email.send(settings.company_email, f"URGENT: {details['issue']}"[:120], "\n".join(lines))
        except ComposioError as error:
            logger.error("urgent_email_failed", call_id=call_id, error=str(error))
            return (
                "Not sent: the email to the team failed. Apologize, and offer to schedule the earliest technician call."
            )

        context.storage.add_urgent_request(
            {
                "id": request_id,
                "created_at": now.isoformat(timespec="seconds"),
                "call_id": call_id,
                "calling_from": state.get("caller_phone", ""),
                **details,
            }
        )

    logger.info("urgent_issue_reported", call_id=call_id, request_id=request_id)
    return (
        f"Sent: the team was emailed (urgent request {request_id}). Tell the caller the team will reach out as soon "
        "as possible. Don't promise a time, a visit, or that someone is on the way."
    )
