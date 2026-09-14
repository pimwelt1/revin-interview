import logging
import re

import structlog
from openai import APIError
from pydantic import ValidationError


def error_details(error: Exception) -> dict:
    # OpenAI exception messages can embed full response bodies. Extract only the
    # provider message; never stringify those exceptions or log the complete body.
    if isinstance(error, APIError):
        body = error.body if isinstance(error.body, dict) else {}
        detail = body.get("error", body)
        detail = detail if isinstance(detail, dict) else {}
        return {
            "error_type": type(error).__name__,
            "error_message": detail.get("message") or "OpenAI request failed",
            "error_code": getattr(error, "status_code", None),
            "error_status": getattr(error, "code", None),
        }
    # Validation errors omit caller/model input.
    message = (
        str(error.errors(include_input=False, include_url=False))
        if isinstance(error, ValidationError)
        else str(getattr(error, "message", None) or error or type(error).__name__)
    )
    return {
        "error_type": type(error).__name__,
        "error_message": message,
        "error_code": getattr(error, "code", None),
        "error_status": getattr(error, "status", None),
    }


def configure_logging(level: str, secrets: tuple[str, ...] = ()) -> None:
    def redact(logger, method_name, event):
        def clean(value):
            if isinstance(value, str):
                for secret in secrets:
                    if secret:
                        value = value.replace(secret, "[REDACTED]")
                return re.sub(r"(?i)([?&](?:key|api_key|token)=)[^\s&]+", r"\1[REDACTED]", value)
            if isinstance(value, dict):
                return {key: clean(item) for key, item in value.items()}
            if isinstance(value, (list, tuple)):
                return [clean(item) for item in value]
            return value

        return clean(event)

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            redact,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(getattr(logging, level)),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=False,
    )
