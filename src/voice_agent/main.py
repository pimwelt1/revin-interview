from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from langgraph.checkpoint.memory import InMemorySaver

from voice_agent.agent.agent import build_context, build_model
from voice_agent.agent.graph import build_graph
from voice_agent.logging_config import configure_logging
from voice_agent.settings import Settings
from voice_agent.telephony import TwilioProvider, router


def create_app() -> FastAPI:
    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncGenerator[None, None]:
        settings = Settings()
        settings.require(
            "PUBLIC_BASE_URL",
            "TWILIO_ACCOUNT_SID",
            "TWILIO_AUTH_TOKEN",
            "TWILIO_PHONE_NUMBER",
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
        application.state.settings = settings
        application.state.telephony = TwilioProvider(settings)
        application.state.context = build_context(settings)
        application.state.graph = build_graph(build_model(settings), InMemorySaver())
        logger = structlog.get_logger()

        logger.info("application_started")
        try:
            yield
        finally:
            logger.info("application_stopped")

    application = FastAPI(title="Voice Agent", lifespan=lifespan)
    application.include_router(router)

    @application.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return application


app = create_app()
