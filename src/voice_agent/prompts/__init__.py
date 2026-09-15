"""Phone-agent instructions and caller messages."""

from pathlib import Path

GREETING = (
    "Hi, thanks for calling Summit Air. Our team is busy right now, but tell me what you need "
    "and I'll schedule a call with one of our technicians. También hablo español."
)

AGENT_PROMPT = Path(__file__).with_name("agent.txt").read_text(encoding="utf-8")
SYSTEM_UNAVAILABLE = {
    "en": "I'm sorry, our system is unavailable and I cannot continue this call. Please try again later. Goodbye.",
    "es": "Lo siento, nuestro sistema no está disponible y no puedo continuar esta llamada. Por favor, vuelva a llamar más tarde. Adiós.",
}
