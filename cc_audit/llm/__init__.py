"""LLM layer. To change the model, edit "llm" in config/settings.json.
To add a provider, write a class with a chat() method (see base.py) and register it below.
"""
from ..config import settings
from .base import LLMClient, LLMError
from .openai_compatible import OpenAICompatibleClient

PROVIDERS = {
    "openai_compatible": OpenAICompatibleClient,
}


def get_llm(overrides=None):
    cfg = {**settings()["llm"], **(overrides or {})}
    provider = cfg.pop("provider")
    if provider not in PROVIDERS:
        raise LLMError(f"unknown LLM provider '{provider}' - known: {', '.join(PROVIDERS)}")
    return PROVIDERS[provider](**cfg)


__all__ = ["get_llm", "LLMClient", "LLMError", "PROVIDERS"]
