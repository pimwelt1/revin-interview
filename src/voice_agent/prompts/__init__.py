"""Phone-agent instructions and caller messages."""

from pathlib import Path

from voice_agent.settings import Settings

# A template, not speakable as it stands: call greeting() to fill in the service area.
GREETING = (
    "Hi, thanks for calling Summit Air, heating and air conditioning for {counties}. Our team is busy right "
    "now, but tell me what you need and I'll schedule a call with one of our technicians. "
    "También hablo español."
)


def greeting(settings: Settings) -> str:
    """The welcome the phone system plays: what we do, where we work, and that Spanish is fine."""
    return GREETING.format(counties=settings.spoken_counties)


DETAILS_PROMPT = Path(__file__).with_name("details.txt").read_text(encoding="utf-8")
AGENT_PROMPT = Path(__file__).with_name("agent.txt").read_text(encoding="utf-8")
SYSTEM_UNAVAILABLE = {
    "en": "I'm sorry, our system is unavailable and I cannot continue this call. Please try again later. Goodbye.",
    "es": "Lo siento, nuestro sistema no está disponible y no puedo continuar esta llamada. Por favor, vuelva a llamar más tarde. Adiós.",
}
