from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI

from voice_agent.agent.agent import build_context
from voice_agent.agent.graph import build_call_graph
from voice_agent.logging_config import configure_logging
from voice_agent.settings import Settings
from voice_agent.telephony import TwilioProvider, router


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    settings = Settings()
    settings.require(
        "PUBLIC_BASE_URL",
        "TWILIO_AUTH_TOKEN",
        "OPENAI_API_KEY",
        "OPENAI_MODEL",
        "COMPOSIO_API_KEY",
        "COMPANY_EMAIL",
        "EMAIL_FROM",
    )
    configure_logging(
        settings.log_level,
        secrets=(
            settings.openai_api_key.get_secret_value(),
            settings.twilio_auth_token.get_secret_value(),
            settings.composio_api_key.get_secret_value(),
        ),
    )
    app.state.settings = settings
    app.state.telephony = TwilioProvider(settings)
    app.state.context = build_context(settings)
    app.state.graph = build_call_graph(settings)
    logger = structlog.get_logger()

    logger.info("application_started")
    try:
        yield
    finally:
        logger.info("application_stopped")


app = FastAPI(title="Voice Agent", lifespan=lifespan)
app.include_router(router)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
