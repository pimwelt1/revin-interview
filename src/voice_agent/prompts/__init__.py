"""Phone-agent instructions and caller messages."""

from pathlib import Path

GREETING = "Hello, thank you for calling Summit Air. How can I help? También hablo español."

CONVERSATION_PROMPT = Path(__file__).with_name("conversation.txt").read_text(encoding="utf-8")
SYSTEM_UNAVAILABLE = {
    "en": "I'm sorry, our system is unavailable and I cannot continue this call. Please try again later. Goodbye.",
    "es": "Lo siento, nuestro sistema no está disponible y no puedo continuar esta llamada. Por favor, vuelva a llamar más tarde. Adiós.",
}
