"""Company email through Composio's Gmail tools."""

from voice_agent.agent.services.composio import ComposioTools


class ComposioEmail:
    def __init__(self, composio: ComposioTools, sender: str):
        self.composio = composio
        self.sender = sender  # the connected Gmail address; Composio can't always look it up itself

    def send(self, to: str, subject: str, body: str) -> None:
        """Send a plain-text email from the connected Gmail account, or raise ComposioError."""
        self.composio.execute(
            "GMAIL_SEND_EMAIL",
            {"from_email": self.sender, "recipient_email": to, "subject": subject, "body": body, "is_html": False},
        )
