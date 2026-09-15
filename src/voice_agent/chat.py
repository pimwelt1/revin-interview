"""Talk to the agent by typing, through the same Session a phone call uses. For manual testing.

    PYTHONPATH=src .venv/bin/python -m voice_agent.chat

It uses the real technician calendars through Composio, so bookings made here are real.
"""

import argparse
import asyncio
from uuid import uuid4

from langgraph.checkpoint.memory import InMemorySaver

from voice_agent.agent.agent import build_context, build_model
from voice_agent.agent.graph import build_graph
from voice_agent.logging_config import configure_logging
from voice_agent.prompts import GREETING, SYSTEM_UNAVAILABLE
from voice_agent.prompts.phrases import message as phrase
from voice_agent.session import Session
from voice_agent.settings import Settings


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--phone", default="+15555550123", help="caller phone number, as Twilio would send it")
    parser.add_argument("--lang", default="en-US", help="language hint sent with every turn, like Twilio's lang, e.g. es-MX")
    parser.add_argument("--verbose", action="store_true", help="print JSON logs")
    args = parser.parse_args()

    settings = Settings()
    settings.require("OPENAI_API_KEY", "OPENAI_MODEL", "COMPOSIO_API_KEY", "COMPANY_EMAIL", "EMAIL_FROM")
    configure_logging("INFO" if args.verbose else "WARNING", secrets=(settings.openai_api_key.get_secret_value(),))
    context = build_context(settings)
    graph = build_graph(build_model(settings), InMemorySaver())
    session = Session(f"chat-{uuid4().hex[:8]}", graph, context, caller_phone=args.phone)

    print(f"Agent: {GREETING}")
    while not (session.finished or session.failed):
        try:
            text = (await asyncio.to_thread(input, "\nYou: ")).strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not text:
            continue
        turn = session.start_turn(text, language=args.lang)
        async for token in session.reply(turn):
            first_token = turn.response == token
            print(f"Agent: {token}" if first_token else token, end="", flush=True)
        if turn.response:
            print()

    if session.failed:
        print(f"Agent: {SYSTEM_UNAVAILABLE[session.language]}  [system error, rerun with --verbose]")
    elif session.finished:
        print(f"Agent: {phrase('goodbye', session.language)}")


if __name__ == "__main__":
    asyncio.run(main())
