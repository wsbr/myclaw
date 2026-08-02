"""Minimal registry contract for outbound Channel notification plugins."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class ChannelDeliveryRequest:
    external_conversation_id: str
    idempotency_key: str
    text: str
    config: dict = field(default_factory=dict)
    credential: str | None = None


@dataclass(frozen=True)
class ChannelDeliveryResult:
    status: str
    provider_message_key: str | None = None
    provider_request_key: str | None = None
    error_code: str | None = None
    error_detail: str | None = None
    retryable: bool = False


class ChannelPlugin(Protocol):
    async def deliver(self, request: ChannelDeliveryRequest) -> ChannelDeliveryResult: ...


CHANNEL_PLUGINS: dict[str, ChannelPlugin] = {}


def register_channel_plugin(plugin_key: str, plugin: ChannelPlugin) -> None:
    if not plugin_key or plugin_key in CHANNEL_PLUGINS:
        raise ValueError(f"Channel plugin is already registered or invalid: {plugin_key}")
    CHANNEL_PLUGINS[plugin_key] = plugin


def get_channel_plugin(plugin_key: str) -> ChannelPlugin | None:
    return CHANNEL_PLUGINS.get(plugin_key)
