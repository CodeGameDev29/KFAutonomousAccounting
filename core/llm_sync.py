"""Synchronous LLM plumbing shared by the reconciliation-side agents.

The matcher, the transfer linker, the date validator and the categorization
agent all make blocking, JSON-in/JSON-out LLM calls from worker threads. Each
one carrying its own provider chain means a provider change has to be made in
as many places as there are agents, and each of those places can fail its own
way when a hosted key is absent.

This module owns the ladder for all of them: **local → Gemini (if keyed) →
OpenAI (if keyed)**, with a clear error when nothing at all is configured.

It also owns *getting JSON back out*, which is where these calls fail: a tight
``max_tokens`` ceiling against a model that writes a paragraph of
``"reasoning"`` truncates the JSON mid-string, and a caller that only catches
the parse error drops to its deterministic fallback without a word reaching the
UI. What this module does about it:

* asks for a JSON object (``response_format``) with thinking disabled;
* extracts the first balanced JSON object out of fences, thinking blocks and
  trailing prose;
* treats a length-truncated reply as the recoverable error it is and **retries
  once** with double the budget and an instruction to keep the reasoning short;
* logs the raw text at WARNING when it still will not parse, so the next
  failure is diagnosable from the server log instead of guessable; and
* counts every call and every fallback in :data:`llm_call_stats`, which the
  reconciliation run summary reports so a silent downgrade is impossible.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# A shortlist reply is a small JSON object, but a model that writes a paragraph
# of reasoning first needs far more room than the object itself; too low a
# ceiling truncates it mid-reply. The truncation retry doubles this budget.
DEFAULT_MAX_TOKENS = 1024

# How much of a raw reply to put in the log when it will not parse. Enough to
# see where it was cut; short enough not to flood the log file.
RAW_LOG_CHARS = 1500

_RETRY_INSTRUCTION = (
    "\n\nIMPORTANT: your previous reply was not valid JSON ({problem}). "
    "Reply with ONE JSON object and nothing else — no markdown fences, no "
    "commentary before or after it, no thinking. Keep every string value under "
    "200 characters so the object is complete."
)


class LLMJsonError(ValueError):
    """The provider answered, but the answer was not usable JSON."""

    def __init__(self, message: str, raw: str = "", truncated: bool = False) -> None:
        super().__init__(message)
        self.raw = raw
        self.truncated = truncated


@dataclass
class LLMCallStats:
    """Per-run tally of engine LLM calls, so a downgrade is never silent."""

    calls: int = 0
    ok: int = 0
    retried: int = 0
    fallbacks: int = 0
    by_label: dict[str, int] = field(default_factory=dict)
    reasons: dict[str, int] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def reset(self) -> None:
        with self._lock:
            self.calls = 0
            self.ok = 0
            self.retried = 0
            self.fallbacks = 0
            self.by_label = {}
            self.reasons = {}

    def record_call(self) -> None:
        with self._lock:
            self.calls += 1

    def record_ok(self) -> None:
        with self._lock:
            self.ok += 1

    def record_retry(self) -> None:
        with self._lock:
            self.retried += 1

    def record_fallback(self, label: str, reason: str) -> None:
        with self._lock:
            self.fallbacks += 1
            self.by_label[label] = self.by_label.get(label, 0) + 1
            key = reason[:60]
            self.reasons[key] = self.reasons.get(key, 0) + 1

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "calls": self.calls,
                "ok": self.ok,
                "retried": self.retried,
                "fallbacks": self.fallbacks,
                "by_label": dict(self.by_label),
                "reasons": dict(self.reasons),
            }


# Process-wide: a reconciliation run holds a per-user advisory lock, so one run
# at a time owns these counters. Reset at the start of a run, read at the end.
llm_call_stats = LLMCallStats()


def reset_llm_call_stats() -> None:
    """Start a fresh tally (called at the top of a reconciliation run)."""
    llm_call_stats.reset()


def llm_call_stats_snapshot() -> dict:
    """Return the current tally as a plain dict, safe to put in a payload."""
    return llm_call_stats.snapshot()


_THINK_BLOCK = re.compile(r"<think(?:ing)?>.*?</think(?:ing)?>", re.DOTALL | re.IGNORECASE)
_OPEN_THINK = re.compile(r"^\s*<think(?:ing)?>.*", re.DOTALL | re.IGNORECASE)


def strip_code_fences(raw: str) -> str:
    """Remove markdown code fences from an LLM response."""
    raw = (raw or "").strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
        if raw.endswith("```"):
            raw = raw[:-3]
        raw = raw.strip()
    return raw


def extract_json_object(raw: str) -> str:
    """Return the first balanced JSON object (or array) found in *raw*.

    Handles what a model can wrap around its JSON: ``<think>`` blocks,
    ```json fences, a lead-in sentence, trailing prose. Quotes and
    escapes are tracked so a brace inside a string does not end the object.
    Raises :class:`LLMJsonError` when there is no balanced object to find —
    which is what a reply cut off by the token budget looks like.
    """
    text = _THINK_BLOCK.sub("", raw or "")
    if _OPEN_THINK.match(text):
        # An unterminated thinking block: keep whatever follows the tag.
        text = re.sub(r"^\s*<think(?:ing)?>", "", text, flags=re.IGNORECASE)
    text = strip_code_fences(text)
    # A fence can also open mid-reply ("Here is the JSON:\n```json\n{...}").
    fence = text.find("```")
    if fence != -1 and "{" in text[fence:]:
        text = text[fence:].lstrip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.replace("```", " ")
    text = text.strip()
    if not text:
        raise LLMJsonError("empty response", raw=raw)

    start = min(
        (i for i in (text.find("{"), text.find("[")) if i != -1),
        default=-1,
    )
    if start == -1:
        raise LLMJsonError("no JSON object in response", raw=raw)

    opener = text[start]
    closer = "}" if opener == "{" else "]"
    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    raise LLMJsonError(
        "JSON object never closed (response appears cut off)",
        raw=raw, truncated=True,
    )


def parse_json_response(raw: str, finish_reason: str | None = None) -> dict:
    """Parse an LLM reply into a dict, or raise :class:`LLMJsonError`.

    ``finish_reason`` comes straight from the provider: ``"length"`` means the
    token budget ran out mid-answer, which is a diagnosis a bare parse error
    cannot give.
    """
    truncated = (finish_reason or "").lower() == "length"
    try:
        candidate = extract_json_object(raw)
    except LLMJsonError as exc:
        raise LLMJsonError(
            str(exc), raw=raw, truncated=truncated or exc.truncated,
        ) from exc

    try:
        data = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise LLMJsonError(
            f"{exc.msg} at line {exc.lineno} column {exc.colno}",
            raw=raw, truncated=truncated,
        ) from exc

    if isinstance(data, list):
        # Some replies wrap the object in a one-element list. Unwrap it rather
        # than failing the whole call; anything else is a shape error.
        if len(data) == 1 and isinstance(data[0], dict):
            return data[0]
        raise LLMJsonError(
            f"expected a JSON object, got a list of {len(data)}",
            raw=raw, truncated=truncated,
        )
    if not isinstance(data, dict):
        raise LLMJsonError(
            f"expected a JSON object, got {type(data).__name__}",
            raw=raw, truncated=truncated,
        )
    return data


def call_local_json(prompt: str, max_tokens: int = DEFAULT_MAX_TOKENS) -> dict:
    """Call the locally hosted model and return its parsed JSON response.

    Temperature 0 and thinking disabled: these are classification and scoring
    calls, and a <think> block would only add latency and break the parse.
    """
    from openai import OpenAI

    from config.settings import local_llm_config

    cfg = local_llm_config
    client = OpenAI(
        base_url=cfg.base_url,
        api_key=cfg.api_key,
        timeout=cfg.timeout_s,
        max_retries=0,
    )

    def _once(text: str, budget: int):
        response = client.chat.completions.create(
            model=cfg.model,
            messages=[{"role": "user", "content": text}],
            temperature=0,
            max_tokens=budget,
            response_format={"type": "json_object"},
            extra_body=cfg.extra_body,
        )
        choice = response.choices[0]
        return choice.message.content, getattr(choice, "finish_reason", None)

    raw, finish_reason = _once(prompt, max_tokens)
    try:
        return parse_json_response(raw, finish_reason)
    except LLMJsonError as first:
        # One corrective retry. A truncated reply gets a bigger budget as well
        # as the instruction, because the instruction alone cannot make room.
        llm_call_stats.record_retry()
        retry_budget = max(max_tokens * 2, DEFAULT_MAX_TOKENS) if first.truncated else max_tokens
        logger.warning(
            "local LLM reply was not JSON (%s%s) — retrying once with %d tokens",
            first, ", truncated" if first.truncated else "", retry_budget,
        )
        retry_prompt = prompt + _RETRY_INSTRUCTION.format(problem=first)
        raw2, finish2 = _once(retry_prompt, retry_budget)
        try:
            return parse_json_response(raw2, finish2)
        except LLMJsonError as second:
            logger.warning(
                "local LLM reply still not JSON after retry (%s). "
                "finish_reason=%s raw[:%d]=%r",
                second, finish2, RAW_LOG_CHARS, (raw2 or "")[:RAW_LOG_CHARS],
            )
            raise


def call_gemini_json(prompt: str) -> dict:
    """Call Gemini and return its parsed JSON response."""
    import google.genai as genai

    from config.settings import gemini_config

    client = genai.Client(api_key=gemini_config.api_key)
    response = client.models.generate_content(
        model=gemini_config.model,
        contents=prompt,
    )
    return parse_json_response(response.text)


def call_openai_json(prompt: str, max_tokens: int = DEFAULT_MAX_TOKENS) -> dict:
    """Call OpenAI and return its parsed JSON response."""
    from openai import OpenAI

    from config.settings import openai_config

    client = OpenAI(api_key=openai_config.api_key)
    response = client.chat.completions.create(
        model=openai_config.model,
        messages=[{"role": "user", "content": prompt}],
        temperature=0,
        max_tokens=max_tokens,
    )
    choice = response.choices[0]
    return parse_json_response(
        choice.message.content, getattr(choice, "finish_reason", None),
    )


def call_json(prompt: str, max_tokens: int = DEFAULT_MAX_TOKENS, label: str = "agent") -> dict:
    """Run a prompt down the provider ladder and return parsed JSON.

    Order is local → Gemini → OpenAI, and a provider with no key is skipped
    rather than called and refused. Raises RuntimeError when nothing at all is
    configured, so the caller reports "no provider" instead of an auth error
    from whichever client happened to be last. When the local endpoint is the
    only rung and it is unreachable, the raised error is
    `LocalLLMUnreachableError` carrying a sentence a user can act on.

    Every call and every failure is tallied in :data:`llm_call_stats` under
    ``label``, so a run can report how many agent calls ended up on their
    deterministic fallback. Pass a label that names the *caller*
    ("amount_agent", "date_agent"), not the module doing the plumbing.
    """
    from config.settings import gemini_config, local_llm_config, openai_config
    from core.llm_errors import as_local_unreachable, is_local_endpoint_down

    attempted = False
    last_exc: Exception | None = None
    llm_call_stats.record_call()

    if local_llm_config.enabled:
        attempted = True
        try:
            result = call_local_json(prompt, max_tokens=max_tokens)
            llm_call_stats.record_ok()
            return result
        except Exception as exc:
            # A refused socket is the endpoint being down, not the model being
            # wrong. Re-labelled here so a reconcile run logs (and any surface
            # that shows the text reports) the outage instead of a traceback.
            if is_local_endpoint_down(exc):
                last_exc = as_local_unreachable(exc)
                logger.warning(
                    "%s: local LLM endpoint %s unreachable (%s)",
                    label, local_llm_config.base_url, exc,
                )
            else:
                last_exc = exc
                logger.warning("%s: local LLM call failed (%s)", label, exc)

    if gemini_config.enabled:
        attempted = True
        try:
            result = call_gemini_json(prompt)
            llm_call_stats.record_ok()
            return result
        except ImportError as exc:
            last_exc = exc
            logger.info("%s: google.genai not installed", label)
        except Exception as exc:
            last_exc = exc
            logger.warning("%s: Gemini call failed (%s)", label, exc)

    if openai_config.enabled:
        attempted = True
        try:
            result = call_openai_json(prompt, max_tokens=max_tokens)
            llm_call_stats.record_ok()
            return result
        except Exception as exc:
            last_exc = exc
            logger.warning("%s: OpenAI call failed (%s)", label, exc)

    if not attempted:
        llm_call_stats.record_fallback(label, "no provider configured")
        raise RuntimeError(
            "no LLM provider configured — set LLM_BASE_URL, GEMINI_API_KEY "
            "or OPENAI_API_KEY"
        )
    llm_call_stats.record_fallback(label, str(last_exc) if last_exc else "every provider failed")
    raise last_exc if last_exc else RuntimeError(f"{label}: every LLM provider failed")
