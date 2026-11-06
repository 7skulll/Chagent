"""Resolve provider presets from named credentials without guessing key contents."""

from collections.abc import Mapping
from dataclasses import dataclass, field
import math
import os
from urllib.parse import urlsplit

from chagent.models.base import Model


class ModelConfigurationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ProviderPreset:
    key_env: str
    base_url: str
    model: str
    api: str = "openai-chat"


PRESETS = {
    "openai": ProviderPreset("OPENAI_API_KEY", "https://api.openai.com/v1", "gpt-5.4-mini"),
    "deepseek": ProviderPreset("DEEPSEEK_API_KEY", "https://api.deepseek.com", "deepseek-flash"),
    "anthropic": ProviderPreset("ANTHROPIC_API_KEY", "https://api.anthropic.com/v1", "claude-sonnet-4-6", "anthropic-messages"),
    "qwen": ProviderPreset("DASHSCOPE_API_KEY", "https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen-plus"),
}


@dataclass(frozen=True, slots=True)
class ModelSettings:
    provider: str
    model_id: str
    base_url: str
    api_key: str = field(repr=False)
    api: str = "openai-chat"
    # Application budgets, deliberately below preset model context limits.
    context_window: int = 65_536
    max_tokens: int = 8192
    request_timeout: float = 60
    #: Wall-clock ceiling for one run. Tool time is paused out of it (the
    #: scheduler calls ``RunBudget.pause`` around execution), so this covers
    #: model turns and bookkeeping, not a Maven build. At 180 seconds a single
    #: verified build consumed most of the budget before the fix loop started.
    run_timeout: float = 600
    # A transient 429/5xx used to consume the whole turn, and sometimes the whole
    # run. Model requests have no side effects, so retrying is safe; ``0`` keeps
    # the pre-retry behaviour exactly.
    retry_attempts: int = 2
    retry_delay: float = 0.5
    #: Ask the provider for its reasoning channel instead of suppressing it. The
    #: decoders already read ``reasoning_content``/signed thinking into
    #: ThinkingContent, so this only changes what the request asks for. Off is
    #: the pre-existing wire behaviour: a provider that ignores the field costs
    #: nothing, one that rejects it would fail every turn, so it is opt-in.
    thinking: bool = False
    #: Which variable stated it, for the same reason as ``key_source``: one place
    #: resolves precedence so diagnostics cannot re-derive it differently.
    #: ``CHAGENT_THINKING`` wins when both are set.
    thinking_source: str = ""
    # Name only; never include the credential in diagnostics.
    key_source: str = ""

    def __post_init__(self):
        if self.provider not in {*PRESETS, "custom"} or self.api not in {"openai-chat", "anthropic-messages"}:
            raise ModelConfigurationError("Unknown provider or API protocol")
        if not self.model_id.strip() or not self.api_key.strip():
            raise ModelConfigurationError("Model name and API key must not be empty")
        try:
            url = urlsplit(self.base_url)
            valid_url = (
                url.hostname and not url.username and not url.password and not url.query and not url.fragment
                and (url.scheme == "https" or (url.scheme == "http" and url.hostname in {"localhost", "127.0.0.1", "::1"}))
            )
            _ = url.port
        except ValueError:
            valid_url = False
        if not valid_url:
            raise ModelConfigurationError("Base URL must use HTTPS (or local HTTP), without credentials or query parameters")
        if self.max_tokens < 1 or self.context_window <= self.max_tokens + 256:
            raise ModelConfigurationError("Context budget must exceed output budget and safety margin")
        if any(not math.isfinite(value) or value <= 0 for value in (self.request_timeout, self.run_timeout)):
            raise ModelConfigurationError("Model timeouts must be finite and positive")
        if isinstance(self.retry_attempts, bool) or not isinstance(self.retry_attempts, int) \
                or not 0 <= self.retry_attempts <= 5:
            raise ModelConfigurationError("Retry attempts must be an integer between 0 and 5")
        if not math.isfinite(self.retry_delay) or self.retry_delay < 0:
            raise ModelConfigurationError("Retry delay must be finite and nonnegative")

    def describe(self) -> str:
        """One line naming what will actually be used, without the key itself.

        Ambient variables beat ``--env-file`` entries, so a stale value can make
        a correct file look wrong. Printing the resolved pair is the cheapest
        way to stop that from becoming a mystery.
        """
        source = self.key_source or "environment"
        retries = f"retry {self.retry_attempts}" if self.retry_attempts else "no retry"
        # Silent either way is the failure mode worth naming: thinking off looks
        # like a model that simply did not reason.
        reasoning = f"thinking on ({self.thinking_source or 'default'})" if self.thinking else "thinking off"
        return (f"{self.provider}/{self.model_id} ({self.api}) -> {self.base_url} "
                f"| key {source} | {retries} | {reasoning}")

    def model(self) -> Model:
        return Model(self.model_id, self.provider, self.api, self.context_window, self.max_tokens, endpoint=self.base_url)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "ModelSettings":
        values = os.environ if env is None else env
        provider = values.get("CHAGENT_PROVIDER", "").strip().lower()
        generic = values.get("CHAGENT_API_KEY", "").strip()
        auto_selected = not provider
        if not provider:
            available = [name for name, preset in PRESETS.items() if values.get(preset.key_env, "").strip()]
            if not available and generic:
                raise ModelConfigurationError("CHAGENT_API_KEY requires CHAGENT_PROVIDER; alternatively configure one named provider API key")
            if len(available) != 1:
                raise ModelConfigurationError(
                    "Configure one of OPENAI_API_KEY, DEEPSEEK_API_KEY, ANTHROPIC_API_KEY, DASHSCOPE_API_KEY; "
                    "when multiple keys are present, select CHAGENT_PROVIDER"
                )
            provider = available[0]
        if provider not in {*PRESETS, "custom"}:
            raise ModelConfigurationError("CHAGENT_PROVIDER must be openai, deepseek, anthropic, qwen or custom")
        preset = PRESETS.get(provider)
        named_key = values.get(preset.key_env, "").strip() if preset else ""
        key = named_key if preset else generic
        key_source = preset.key_env if preset else "CHAGENT_API_KEY"
        base_url = values.get("CHAGENT_BASE_URL", "").strip()
        if not base_url and provider == "openai":
            base_url = values.get("OPENAI_BASE_URL", "").strip()
        base_url = (base_url or (preset.base_url if preset else "")).rstrip("/")
        model = values.get("CHAGENT_MODEL", "").strip() or (preset.model if preset else "")
        if provider == "custom" and (not base_url or not model):
            raise ModelConfigurationError("Custom providers require CHAGENT_BASE_URL and CHAGENT_MODEL")
        if not key:
            raise ModelConfigurationError(f"No API key configured: set {key_source} for {provider}")
        # Accept the same on/off spelling as the other switches; anything else is
        # a typo that would otherwise silently mean "off" and look like the
        # provider ignoring us.
        thinking_source = ""
        thinking = False
        for name in ("CHAGENT_THINKING", "CHAGENT_REASONING"):
            if values.get(name, "").strip():
                thinking_source = name
                break
        if thinking_source:
            stated = values[thinking_source].strip().lower()
            if stated not in {"on", "off"}:
                raise ModelConfigurationError(f"{thinking_source} must be on or off")
            thinking = stated == "on"
        try:
            return cls(
                provider=provider, model_id=model, base_url=base_url, api_key=key,
                api=preset.api if preset else "openai-chat",
                key_source=key_source,
                context_window=int(values.get("CHAGENT_CONTEXT_WINDOW", "65536")),
                max_tokens=int(values.get("CHAGENT_MAX_TOKENS", "8192")),
                request_timeout=float(values.get("CHAGENT_MODEL_TIMEOUT", "60")),
                run_timeout=float(values.get("CHAGENT_RUN_TIMEOUT", "600")),
                retry_attempts=int(values.get("CHAGENT_RETRY_ATTEMPTS", "2")),
                thinking=thinking,
                thinking_source=thinking_source,
            )
        except ModelConfigurationError:
            raise
        except ValueError:
            raise ModelConfigurationError("Model budget and timeout settings must be numeric") from None
