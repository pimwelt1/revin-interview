"""Tool for urgent issues: email the Summit Air team instead of booking a technician call."""

from datetime import datetime
from uuid import uuid4

import structlog
from langchain_core.tools import tool
from langgraph.prebuilt import ToolRuntime

from voice_agent.agent.services.composio import ComposioError
from voice_agent.agent.services.schedule import spoken_time
from voice_agent.agent.state import AgentContext, CallState

logger = structlog.get_logger()


@tool(parse_docstring=True)
def report_urgent_issue(
    issue: str,
    runtime: ToolRuntime[AgentContext, CallState],
    customer_name: str = "",
    phone: str = "",
    address: str = "",
) -> str:
    """Email the Summit Air team about an urgent issue. Use this instead of booking a technician call.

    Args:
        issue: What is happening and why it is urgent.
        customer_name: The caller's full name, if known.
        phone: The best number to reach the caller, if they gave one.
        address: The service address, if known.
    """
    context, state = runtime.context, runtime.state
    settings = context.settings
    call_id = state["call_id"]
    calling_from = state.get("caller_phone", "")
    if not issue.strip():
        return "Not sent. Missing: issue."

    with context.storage.lock:
        if context.storage.urgent_requests(call_id=call_id):
            return "Already sent on this call. The team has the details; don't send it again."

        request_id = f"UR-{uuid4().hex[:8].upper()}"
        now = datetime.now(settings.timezone)
        missing = [
            label
            for label, value in (("name", customer_name), ("callback number", phone or calling_from), ("address", address))
            if not value.strip()
        ]
        lines = [
            f"Urgent request {request_id} from the Summit Air phone assistant.",
            "",
            f"Issue: {issue}",
            f"Name: {customer_name or 'not given'}",
            f"Callback number: {phone or calling_from or 'not given'}",
            f"Calling from: {calling_from or 'unknown'}",
            f"Address: {address or 'not given'}",
            f"Reported: {now:%A, %B} {now.day}, {spoken_time(now)}",
        ]
        for booked in context.storage.service_calls(call_id=call_id, status="booked"):
            lines.append(f"Already booked on this call: technician call {booked['id']} at {booked['start']}")
        if missing:
            lines.append("Missing: " + ", ".join(missing))

        try:
            context.email.send(settings.company_email, f"URGENT: {issue}"[:120], "\n".join(lines))
        except ComposioError as error:
            logger.error("urgent_email_failed", call_id=call_id, error=str(error))
            return "Not sent: the email to the team failed. Apologize, and offer to schedule the earliest technician call."

        context.storage.add_urgent_request(
            {
                "id": request_id,
                "created_at": now.isoformat(timespec="seconds"),
                "call_id": call_id,
                "customer_name": customer_name,
                "phone": phone,
                "calling_from": calling_from,
                "address": address,
                "issue": issue,
            }
        )

    logger.info("urgent_issue_reported", call_id=call_id, request_id=request_id, missing=missing)
    return (
        f"Sent: the team was alerted by email (urgent request {request_id}). Tell the caller the team will reach out "
        "as soon as possible. Don't promise a time, a visit, or that someone is on the way."
    )
