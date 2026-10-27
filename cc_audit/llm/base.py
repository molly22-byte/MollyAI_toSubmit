"""The one interface every LLM backend implements. Nothing else in the project talks to a model directly."""
import re
from abc import ABC, abstractmethod

THINK = re.compile(r"<think>.*?</think>", re.S)
FENCE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.S)


class LLMError(RuntimeError):
    pass


def clean_reply(text):
    """Reasoning models (Nemotron 3, DeepSeek ...) may put a <think> block, a ```json fence or a sentence
    around the answer; strip them so explainer.call_llm() gets the bare JSON it asked for."""
    text = THINK.sub("", text or "").strip()
    if text.startswith("<think>"):                      # reasoning cut off before it closed: no answer
        return ""
    m = FENCE.match(text)
    text = m.group(1) if m else text
    start, end = text.find("{"), text.rfind("}")
    if not text.startswith("{") and 0 <= start < end:   # "Here is the JSON: {...}"
        return text[start:end + 1]
    return text


class LLMClient(ABC):
    provider = "base"

    def __init__(self, model, temperature=0, timeout_seconds=300, **_):
        self.model = model
        self.temperature = temperature
        self.timeout = timeout_seconds

    @abstractmethod
    def chat(self, system, user, json_mode=False):
        """Send one system + user message, return the reply text. json_mode asks for a JSON object."""

    def describe(self):
        return {"provider": self.provider, "model": self.model}
