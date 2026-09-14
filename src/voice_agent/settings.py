from typing import Literal, Self
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        hide_input_in_errors=True,
    )

    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    public_base_url: str = ""
    twilio_account_sid: str = ""
    twilio_auth_token: SecretStr = SecretStr("")
    twilio_phone_number: str = ""
    openai_api_key: SecretStr = SecretStr("")
    openai_model: str = "gpt-4.1-mini"
    model_timeout_seconds: float = Field(default=20, gt=0, le=120)
    silence_timeout_seconds: float = Field(default=10, gt=0, le=120)
    relay_voice: str = "UgBBYS2sOqTuMpoF3BR0"

    @model_validator(mode="after")
    def validate_configuration(self) -> Self:
        if self.public_base_url:
            url = urlsplit(self.public_base_url)
            if url.scheme != "https" or not url.hostname or url.username or url.password or url.query or url.fragment:
                raise ValueError("PUBLIC_BASE_URL must be an HTTPS URL without credentials, query, or fragment")

        required = {
            "PUBLIC_BASE_URL": self.public_base_url,
            "TWILIO_ACCOUNT_SID": self.twilio_account_sid,
            "TWILIO_AUTH_TOKEN": self.twilio_auth_token.get_secret_value(),
            "TWILIO_PHONE_NUMBER": self.twilio_phone_number,
            "OPENAI_API_KEY": self.openai_api_key.get_secret_value(),
            "OPENAI_MODEL": self.openai_model,
        }
        missing = [name for name, value in required.items() if not value.strip()]
        if missing:
            raise ValueError("Missing configuration: " + ", ".join(missing))

        return self
