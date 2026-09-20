"""How many categorization calls may be in flight at once.

The categorization agent runs after a match run, against the same LLM endpoint.
Its Phase 4 vendor groups and Phase 5 refinement batches are independent
questions — a group of 20 vendors, a batch of 10 transactions — so issuing them
one at a time leaves the endpoint idle for most of the run.

The knob is ``LocalLLMConfig.engine_max_concurrent``, the same one the matcher
uses, and for the same reason: a categorization call is the same *shape* of work
as a matcher call — a bounded prompt and a bounded JSON reply — not the long
decode of a 4th-pass linker batch, which is why the linker holds itself to a
lower limit. That field carries the concurrency the configured endpoint sustains
before throughput stops improving.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from typing import TypeVar

from config.settings import local_llm_config

T = TypeVar("T")
R = TypeVar("R")

MAX_CONCURRENT_CALLS: int = local_llm_config.engine_max_concurrent


def run_bounded(items: Sequence[T], fn: "Callable[[T], R]") -> list[R]:
    """Map ``fn`` over ``items``, at most :data:`MAX_CONCURRENT_CALLS` at a time.

    Results come back **in the order of** ``items``, whichever call finishes
    first, so everything downstream — the decision list, the override map, the
    progress lines — is assembled in input order rather than completion order.
    """
    if not items:
        return []
    if len(items) == 1 or MAX_CONCURRENT_CALLS <= 1:
        return [fn(item) for item in items]
    workers = max(1, min(MAX_CONCURRENT_CALLS, len(items)))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="categorize") as pool:
        return list(pool.map(fn, items))
