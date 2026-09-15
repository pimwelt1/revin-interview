from pathlib import Path
from typing import Literal, Self
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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

    # Telephony
    public_base_url: str = ""
    twilio_account_sid: str = ""
    twilio_auth_token: SecretStr = SecretStr("")
    twilio_phone_number: str = ""
    silence_timeout_seconds: float = Field(default=10, gt=0, le=120)
    relay_voice: str = "UgBBYS2sOqTuMpoF3BR0"

    # Agent model
    openai_api_key: SecretStr = SecretStr("")
    openai_model: str = "gpt-4.1-mini"
    model_timeout_seconds: float = Field(default=20, gt=0, le=120)

    # Calendar (Composio user that owns the Google Calendar connection)
    composio_api_key: SecretStr = SecretStr("")
    composio_user_id: str = "summit-air"

    # Urgent issues are emailed to COMPANY_EMAIL from EMAIL_FROM, the Gmail account connected in Composio
    company_email: str = ""
    email_from: str = ""

    # Business schedule
    business_timezone: str = "America/New_York"
    business_weekdays: str = "0,1,2,3,4"  # Monday = 0


    ###TO MODIFY
    business_start_hour: int = Field(default=8, ge=0, le=23)
    business_end_hour: int = Field(default=17, ge=1, le=24)
    service_call_minutes: int = Field(default=15, ge=5, le=120)
    booking_horizon_days: int = Field(default=14, ge=1, le=60)

    data_dir: Path = Path("data")

    @property
    def timezone(self) -> ZoneInfo:
        return ZoneInfo(self.business_timezone)

    @property
    def weekdays(self) -> set[int]:
        return {int(day) for day in self.business_weekdays.split(",")}

    @model_validator(mode="after")
    def validate_configuration(self) -> Self:
        if self.public_base_url:
            url = urlsplit(self.public_base_url)
            if url.scheme != "https" or not url.hostname or url.username or url.password or url.query or url.fragment:
                raise ValueError("PUBLIC_BASE_URL must be an HTTPS URL without credentials, query, or fragment")
        try:
            ZoneInfo(self.business_timezone)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("BUSINESS_TIMEZONE must be an IANA time zone, e.g. America/New_York") from None
        try:
            if not self.weekdays or not self.weekdays <= set(range(7)):
                raise ValueError
        except ValueError:
            raise ValueError("BUSINESS_WEEKDAYS must be comma-separated days from Monday 0 to Sunday 6") from None
        if self.business_start_hour * 60 + self.service_call_minutes > self.business_end_hour * 60:
            raise ValueError("BUSINESS_END_HOUR leaves no room for one service call after BUSINESS_START_HOUR")
        return self

    def require(self, *names: str) -> None:
        """Fail startup with the names of required settings that are empty."""
        missing = []
        for name in names:
            value = getattr(self, name.lower())
            if isinstance(value, SecretStr):
                value = value.get_secret_value()
            if not str(value).strip():
                missing.append(name)
        if missing:
            raise ValueError("Missing configuration: " + ", ".join(missing))
