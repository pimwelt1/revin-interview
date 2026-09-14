"""Fixed phrases spoken by the phone layer, not the model."""

MESSAGES = {
    "goodbye": ("Thank you for calling Summit Air. Goodbye.", "Gracias por llamar a Summit Air. Adiós."),
    "silence": (
        "Are you still there? Take your time—I'm here to help.",
        "¿Sigue ahí? Tómese su tiempo, estoy aquí para ayudarle.",
    ),
    "silence_goodbye": (
        "I can't hear you, so I'll end the call now. Please call back when you're ready. Goodbye.",
        "No le escucho, así que voy a terminar la llamada. Vuelva a llamar cuando esté listo. Adiós.",
    ),
}


def message(name: str, language: str) -> str:
    return MESSAGES[name][language == "es"]
