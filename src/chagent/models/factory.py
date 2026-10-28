"""Create the selected provider adapter without opening a network connection."""

import httpx

from chagent.models.anthropic import AnthropicClient
from chagent.models.http import HTTPModelClient
from chagent.models.openai_chat import OpenAIChatClient
from chagent.models.settings import ModelSettings


def create_model_client(settings: ModelSettings, *, transport: httpx.AsyncBaseTransport | None = None) -> HTTPModelClient:
    if settings.api == "anthropic-messages":
        return AnthropicClient(settings, transport=transport)
    return OpenAIChatClient(settings, transport=transport)
