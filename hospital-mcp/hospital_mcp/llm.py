"""LLM access for the in-app assistant: a pool of models across providers, rotated automatically.

LLM_MODELS is a comma-separated list of provider:model entries, e.g.
    groq:meta-llama/llama-4-scout-17b-16e-instruct, groq:llama-3.3-70b-versatile, openrouter:openrouter/free

* Each user turn starts on the next model in the list (round robin), and stays on it for that turn.
* If a model is rate-limited (429), missing (404) or failing (5xx / timeout), it is put on cooldown and the
  same request is retried on the next model, so the user just gets an answer.
* Why this helps: Groq's free limits are per model, so rotating across Groq models multiplies the budget.
  OpenRouter's free limits are per account (20/min, 50/day without credits), so it is a useful extra backup,
  not a multiplier.

Both providers speak the OpenAI chat-completions format, so one code path serves both.
"""
import itertools
import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

import requests

log = logging.getLogger("hospital-llm")

PROVIDERS = {
    "groq": "https://api.groq.com/openai/v1/chat/completions",
    "openrouter": "https://openrouter.ai/api/v1/chat/completions",
}

DEFAULT_MODELS = {
    "groq": ["meta-llama/llama-4-scout-17b-16e-instruct", "llama-3.3-70b-versatile", "openai/gpt-oss-20b",
             "moonshotai/kimi-k2-instruct", "qwen/qwen3-32b"],
    "openrouter": ["openrouter/free"],
}


class RateLimitedError(RuntimeError):
    """Every model in the pool is cooling down."""


@dataclass
class Entry:
    provider: str
    model: str
    cooldown_until: float = 0.0

    @property
    def label(self) -> str:
        return f"{self.provider}:{self.model}"


def send(provider: str, api_key: str, model: str, body: dict, timeout: int = 60) -> Tuple[int, dict, dict]:
    """One HTTP call. Returns (status, headers, json-or-{'text':...}). Patched in tests."""
    headers = {"Authorization": f"Bearer {api_key}"}
    if provider == "openrouter":
        headers.update({"HTTP-Referer": "https://github.com/hospital-mcp", "X-Title": "Hospital MCP"})
    r = requests.post(PROVIDERS[provider], headers=headers, json={**body, "model": model}, timeout=timeout)
    try:
        data = r.json()
    except ValueError:
        data = {"text": r.text[:300]}
    return r.status_code, dict(r.headers), data


class LLMPool:
    def __init__(self, entries: List[Entry], keys: Dict[str, Optional[str]], clock: Callable[[], float] = time.time):
        self.entries = [e for e in entries if keys.get(e.provider)]
        self.keys, self.clock = keys, clock
        self._rr = itertools.count()
        self._lock = threading.Lock()

    @classmethod
    def from_settings(cls, s) -> "LLMPool":
        keys = {"groq": s.groq_api_key, "openrouter": s.openrouter_api_key}
        raw = [x.strip() for x in (s.llm_models or "").split(",") if x.strip()]
        if not raw:  # sensible defaults for whichever keys are present
            raw = [f"{p}:{m}" for p in ("groq", "openrouter") if keys.get(p) for m in DEFAULT_MODELS[p]]
        entries = []
        for item in raw:
            provider, _, model = item.partition(":")
            if provider not in PROVIDERS or not model:
                raise RuntimeError(f"LLM_MODELS entry '{item}' must look like groq:<model> or openrouter:<model>.")
            entries.append(Entry(provider, model))
        return cls(entries, keys)

    @property
    def configured(self) -> bool:
        return bool(self.entries)

    def _order(self) -> List[Entry]:
        with self._lock:
            start = next(self._rr) % len(self.entries)
        return self.entries[start:] + self.entries[:start]

    def turn(self) -> Callable[[List[dict], List[dict]], dict]:
        """An llm(messages, tools) callable for one user turn: stays on one model unless it fails."""
        order = self._order()
        current: List[Optional[Entry]] = [None]

        def llm(messages: List[dict], tools: List[dict]) -> dict:
            body: dict = {"messages": messages, "temperature": 0.2, "max_tokens": 1024}
            if tools:
                body.update(tools=tools, tool_choice="auto")
            candidates = ([current[0]] if current[0] else []) + [e for e in order if e is not current[0]]
            last_error = "no model available"
            for e in candidates:
                if e.cooldown_until > self.clock():
                    continue
                try:
                    status, headers, data = send(e.provider, self.keys[e.provider], e.model, body)
                except requests.RequestException as ex:
                    e.cooldown_until = self.clock() + 30
                    last_error = f"{e.label}: {type(ex).__name__}"
                    continue
                if status == 200 and data.get("choices"):
                    current[0] = e
                    msg = data["choices"][0]["message"]
                    msg.setdefault("content", "")
                    return msg
                last_error = f"{e.label}: HTTP {status} {str(data)[:160]}"
                log.warning("LLM %s failed (%s); trying the next model", e.label, status)
                if status == 429:
                    try:
                        wait = float(headers.get("retry-after") or headers.get("Retry-After") or 60)
                    except ValueError:
                        wait = 60
                    e.cooldown_until = self.clock() + min(max(wait, 5), 3600)
                elif status in (404, 400) and "model" in str(data).lower():
                    e.cooldown_until = self.clock() + 3600       # unknown or retired model: skip for an hour
                elif status in (401, 403):
                    e.cooldown_until = self.clock() + 3600       # bad key for this provider
                elif status == 400 and messages and tools:
                    e.cooldown_until = self.clock() + 300        # e.g. this model rejects tool calling
                else:
                    e.cooldown_until = self.clock() + 30
            raise RateLimitedError("All AI models are busy or at their free limits right now. "
                                   f"Please wait a minute and try again. (last: {last_error})")
        return llm

    def status(self) -> List[dict]:
        now = self.clock()
        return [{"model": e.label, "ready": e.cooldown_until <= now,
                 "cooldown_seconds": max(0, int(e.cooldown_until - now))} for e in self.entries]