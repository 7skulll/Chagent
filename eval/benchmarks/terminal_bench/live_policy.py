"""Preregistered model and spending policy for the single TB2.3 diagnostic."""

from contextlib import aclosing
from dataclasses import asdict

from chagent.models.settings import ModelSettings
from eval.budgets import BudgetClient, BudgetExceeded
from eval.manifests import prompt_material, sha256_json

PRICING = {
    "source": "https://api-docs.deepseek.com/quick_start/pricing/",
    "verified_at": "2026-11-19",
    "listed_version": "DeepSeek-V4.1-Flash-1017",
    "input_usd_per_mtok": 0.30,
    "output_usd_per_mtok": 1.20,
    "cache_hit_usd_per_mtok": 0.006,
}
MODEL_SETTINGS = {
    "provider": "deepseek",
    "model_id": "deepseek-flash",
    "base_url": "https://api.deepseek.com",
    "api": "openai-chat",
    "context_window": 16384,
    "max_tokens": 1024,
    "request_timeout": 35,
    "run_timeout": 180,
    "retry_attempts": 0,
    "thinking": False,
}
BUDGET = {
    "max_turns": 6,
    "max_tool_calls": 5,
    "total_cost_usd": 0.04,
    "wall_seconds": 180,
}


def settings(api_key):
    return ModelSettings(api_key=api_key, **MODEL_SETTINGS)


def protocol():
    ceiling = (
        MODEL_SETTINGS["context_window"] * PRICING["input_usd_per_mtok"]
        + MODEL_SETTINGS["max_tokens"] * PRICING["output_usd_per_mtok"]
    ) / 1_000_000
    if ceiling * BUDGET["max_turns"] > BUDGET["total_cost_usd"]:
        raise BudgetExceeded(
            "Frozen budget cannot cover all maximum request reservations"
        )
    return {
        "model_settings": MODEL_SETTINGS,
        "pricing": PRICING,
        "budget": BUDGET,
        "max_request_usd": ceiling,
    }


class LiveDiagnosticBudget(BudgetClient):
    """Prevent an unaccounted or drifting provider response from dispatching tools."""

    def __init__(self, client, terms, profile, preamble):
        super().__init__(client, terms, terms, expected_preamble=preamble)
        self.profile = profile

    async def stream(self, request):
        _, declared = prompt_material(asdict(request))
        if (
            request.purpose != "task"
            or asdict(request.model) != self.profile["model"]
            or request.max_tokens != self.profile["model"]["max_tokens"]
            or sha256_json(declared) != self.profile["tools_sha256"]
        ):
            self._stop("Actual model request differs from the frozen TB2.3 profile")
        async with aclosing(super().stream(request)) as stream:
            async for event in stream:
                if self.unknown_cost:
                    self._stop(
                        "Provider response has unknown usage; no tools dispatched"
                    )
                if self.violation:
                    self._stop(self.violation)
                yield event
