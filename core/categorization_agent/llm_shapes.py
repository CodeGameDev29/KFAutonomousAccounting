"""Reading a batch of decisions out of whatever shape the model replied in.

Every batched phase of this agent asks one question about N things and expects
N answers back. The transport underneath (``core.llm_sync.call_json``) hands
back a parsed **JSON object** — it has to, because the endpoint is called with
``response_format={"type": "json_object"}``, which forbids a top-level array. A
phase that tested ``isinstance(response, list)`` and discarded anything else
would therefore discard *every* reply. Asked for an array of vendor decisions
under that constraint, a model answers with something like:

    >>> {"vendor_key": "wise", "category": "Internal transfer", ...}

One object: the constrained decoder closes it and stops, and the rest of the
group is never written. The prompts therefore ask for the array *inside* an
object, which the decoder can produce; this module is the other half — it reads
the array back out of the shapes a model emits for that request, so a wording
drift in a later model costs an extra key lookup instead of a whole phase.

Each branch below stands for one of those shapes; none of them is speculative.
"""

from __future__ import annotations

import json
import logging
import re

logger = logging.getLogger(__name__)

# Keys a model plausibly wraps the array in, best first. Only consulted when the
# reply has MORE than one list-valued key and the right one is ambiguous.
_PREFERRED_LIST_KEYS: tuple[str, ...] = (
    "decisions", "refinements", "vendor_keys", "vendors", "results",
    "items", "assignments", "categories", "transactions", "data", "output",
)

_NON_ALNUM = re.compile(r"[^a-z0-9]+")

# How much of a reply to quote in the log when it could not be read. Enough to
# see the shape, short enough that a 20-vendor batch does not flood the file.
RAW_EXCERPT_CHARS = 300


def normalize_key(value: object) -> str:
    """Fold a vendor key to a form that survives the model's spelling choices.

    ``"Bluepeak Software"``, ``"bluepeak_software"`` and
    ``"Bluepeak-Software."`` all become ``"bluepeak_software"``, so a reply
    that title-cased the key it was sent still lands on the right cluster.
    """
    if not isinstance(value, str):
        return ""
    return _NON_ALNUM.sub("_", value.strip().lower()).strip("_")


def excerpt(response: object) -> str:
    """Return a short, log-safe rendering of a reply for a WARNING line."""
    try:
        text = json.dumps(response, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = repr(response)
    text = " ".join(text.split())
    if len(text) > RAW_EXCERPT_CHARS:
        return text[:RAW_EXCERPT_CHARS] + "…"
    return text


def unwrap_item_list(response: object, identity_key: str) -> list[dict]:
    """Return the list of decision dicts carried by ``response``.

    Accepts, in the order the branches are tried:

    * a JSON array (what the prompt asks for in spirit, and what a provider
      without a response-format constraint returns);
    * an object wrapping the array under a single list-valued key —
      ``{"decisions": [...]}``, ``{"vendors": [...]}``, anything;
    * a single decision object, which is what a JSON-object-constrained
      decoder emits when asked for an array;
    * an object mapping each identity to its decision —
      ``{"vendor_a": {"category": ...}, "vendor_b": {...}}``.

    ``identity_key`` is the field that names the thing being decided
    (``"vendor_key"`` in Phase 4, ``"id"`` in Phase 5); it is what tells a
    single decision apart from an unrelated wrapper. Returns ``[]`` when the
    reply carries nothing usable — the caller logs that with an excerpt.
    """
    if isinstance(response, list):
        return [item for item in response if isinstance(item, dict)]
    if not isinstance(response, dict):
        return []

    lists = {k: v for k, v in response.items() if isinstance(v, list)}
    if len(lists) == 1:
        (only,) = lists.values()
        return [item for item in only if isinstance(item, dict)]
    if lists:
        for key in _PREFERRED_LIST_KEYS:
            if key in lists:
                return [item for item in lists[key] if isinstance(item, dict)]
        # Ambiguous: take the one list whose entries look like decisions.
        candidates = [
            value for value in lists.values()
            if any(isinstance(i, dict) and identity_key in i for i in value)
        ]
        if len(candidates) == 1:
            return [item for item in candidates[0] if isinstance(item, dict)]
        return []

    # No list anywhere. A single decision object?
    if identity_key in response:
        return [response]

    # A mapping of identity -> decision, e.g. {"wise": {"category": ...}}.
    if response and all(isinstance(v, dict) for v in response.values()):
        return [
            {identity_key: key, **value}
            for key, value in response.items()
        ]
    return []
