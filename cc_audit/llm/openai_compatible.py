"""Nebius Token Factory (OpenAI-compatible chat API). Any model in its catalogue: set "model" in settings.

Default: NVIDIA Nemotron 3 Super on Nebius (https://api.tokenfactory.nebius.com/v1, key in NEBIUS_API_KEY).
Only masked payloads are sent, but they do leave the machine.
The API key is read from the environment variable named in settings, never from the settings file.
extra_body is merged into the request as is, e.g. {"chat_template_kwargs": {"enable_thinking": false}}.
"""
import json
import os
import ssl
import time
import urllib.error
import urllib.request

from .base import LLMClient, LLMError, clean_reply

RETRY_STATUS = {429, 500, 502, 503, 504}


def tls_context():
    """python.org Python on macOS ships without root certificates (CERTIFICATE_VERIFY_FAILED until
    'Install Certificates.command' is run); use certifi's bundle when it is installed."""
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


class OpenAICompatibleClient(LLMClient):
    provider = "openai_compatible"

    def __init__(self, model, base_url, api_key_env="LLM_API_KEY", extra_body=None, json_mode_supported=True,
                 max_tokens=1024, retries=3, **kwargs):
        super().__init__(model, **kwargs)
        self.base_url = base_url.rstrip("/")
        self.api_key_env = api_key_env
        self.api_key = os.environ.get(api_key_env, "")
        self.extra_body = extra_body or {}
        self.json_mode_supported = json_mode_supported
        self.max_tokens = max_tokens
        self.retries = retries
        self.tls = tls_context()

    def chat(self, system, user, json_mode=False):
        if not self.api_key:
            raise LLMError(f"no API key: set the environment variable {self.api_key_env} (model {self.model})")
        body = {
            "model": self.model,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            **self.extra_body,
        }
        if json_mode and self.json_mode_supported:
            body["response_format"] = {"type": "json_object"}
        attempt = 0
        while True:
            req = urllib.request.Request(f"{self.base_url}/chat/completions", data=json.dumps(body).encode(),
                                         headers={"Content-Type": "application/json", "Accept": "application/json",
                                                  "User-Agent": "cc-audit-poc/1.0",
                                                  "Authorization": f"Bearer {self.api_key}"})
            try:
                with urllib.request.urlopen(req, timeout=self.timeout, context=self.tls) as resp:
                    return clean_reply(json.loads(resp.read())["choices"][0]["message"]["content"])
            except urllib.error.HTTPError as e:
                detail = e.read().decode(errors="replace")[:300]
                optional = [k for k in ("response_format", *self.extra_body) if k in body]
                if e.code in (400, 422) and optional:
                    # provider rejects an optional parameter (JSON mode, chat_template_kwargs ...): drop them for
                    # this and every later call; the prompt still asks for JSON and clean_reply() extracts it
                    for k in optional:
                        body.pop(k)
                    self.json_mode_supported, self.extra_body = False, {}
                    continue
                if e.code in RETRY_STATUS and attempt < self.retries:     # rate limit / busy: back off and retry
                    attempt += 1
                    time.sleep(float(e.headers.get("Retry-After") or 2 ** attempt))
                    continue
                raise LLMError(f"LLM call failed ({self.base_url}, model {self.model}): HTTP {e.code} {detail}") from e
            except (urllib.error.URLError, TimeoutError, KeyError, IndexError, TypeError) as e:
                raise LLMError(f"LLM call failed ({self.base_url}, model {self.model}): {e}") from e
