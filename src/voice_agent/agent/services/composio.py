"""Run Composio tools (Google Calendar, Gmail) for the connected COMPOSIO_USER_ID."""

from typing import Any

from composio import Composio


class ComposioError(Exception):
    """Composio could not run the tool, or the tool reported a failure."""


class ComposioTools:
    def __init__(self, api_key: str, user_id: str):
        self.client = Composio(api_key=api_key)
        self.user_id = user_id

    def execute(self, slug: str, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            # Composio refuses "latest" tool versions unless told otherwise; pin a version once the demo is stable.
            result = self.client.tools.execute(
                slug, arguments, user_id=self.user_id, dangerously_skip_version_check=True
            )
        except Exception as error:
            raise ComposioError(f"{slug} request failed: {type(error).__name__}") from error
        if not result.get("successful"):
            raise ComposioError(f"{slug} failed: {result.get('error')}")
        return result.get("data") or {}


def find_key(data: Any, key: str) -> Any:
    """Return the first value for `key`, searching nested dicts breadth-first.

    Composio wraps Google's responses in varying envelopes (e.g. `response_data`).
    """
    queue = [data]
    while queue:
        item = queue.pop(0)
        if isinstance(item, dict):
            if key in item:
                return item[key]
            queue.extend(item.values())
        elif isinstance(item, list):
            queue.extend(item)
    return None
