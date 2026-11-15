"""Evaluation-only prompt variants, frozen inline and checked before dispatch."""

from dataclasses import asdict

from chagent.context.builder import DEFAULT_INSTRUCTIONS
from eval.manifests import sha256_text


def variant(content: str, version: str) -> dict:
    return {"version": version, "content": content, "sha256": sha256_text(content)}


def preamble(arm) -> str | None:
    if arm.prompt is None:
        return None
    if arm.prompt["sha256"] != sha256_text(arm.prompt["content"]):
        raise ValueError("prompt content differs from its frozen hash")
    return DEFAULT_INSTRUCTIONS + (
        "\n\n" + arm.prompt["content"] if arm.prompt["content"] else ""
    )


def request_preamble(document: dict) -> str | None:
    # Mirror the surviving named section, including removal/override semantics.
    actual = None
    for message in document["messages"]:
        if "preamble" in (message.get("sections") or {}):
            actual = message["sections"]["preamble"]
    return actual


def verify(request, expected: str) -> None:
    if request_preamble(asdict(request)) != expected:
        raise ValueError(
            "actual request preamble differs from frozen arm; no call was made"
        )


def binding(arm, verified_requests: int) -> dict:
    return {
        "variant": dict(arm.prompt),
        "preamble_sha256": sha256_text(preamble(arm)),
        "verified_requests": verified_requests,
        "request_preamble_sha256": [sha256_text(preamble(arm))] * verified_requests,
    }
