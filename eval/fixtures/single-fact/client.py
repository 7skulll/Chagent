"""A tiny consumer of config.py, so the fixture reads like a project and not a single file."""

from config import BASE_URL, RETRIES, TIMEOUT_SECONDS


def describe() -> str:
    return f"{BASE_URL} retries={RETRIES} timeout={TIMEOUT_SECONDS}s"
