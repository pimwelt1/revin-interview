from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI

from voice_agent.logging_config import configure_logging
from voice_agent.openai_model import OpenAIModel
from voice_agent.settings import Settings
from voice_agent.telephony import TwilioProvider, router


def create_app() -> FastAPI:
    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncGenerator[None, None]:
        settings = Settings()
        configure_logging(
            settings.log_level,
            secrets=(
                settings.openai_api_key.get_secret_value(),
                settings.twilio_auth_token.get_secret_value(),
            ),
        )
        application.state.settings = settings
        application.state.telephony = TwilioProvider(settings)
        application.state.model = OpenAIModel(settings)
        logger = structlog.get_logger()

        logger.info("application_started")
        try:
            yield
        finally:
            await application.state.model.close()
            logger.info("application_stopped")

    application = FastAPI(title="Voice Agent", lifespan=lifespan)
    application.include_router(router)

    @application.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return application


app = create_app()
