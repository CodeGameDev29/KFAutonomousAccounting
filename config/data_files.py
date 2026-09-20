"""Loaders for the editable data tables under ``config/``.

Everything here is *data*, not code: account display names, the deterministic
category rules, and the vendor alias table. Each has a generic default shipped
in this directory; edit the YAML to fit your own books — no Python changes.

Each file is read once per process and cached. A missing or unreadable file
falls back to a safe empty/default value and logs a warning, so a bad edit
degrades rather than stopping the server from starting.

The files this module loads:
  * ``accounts.yaml``       — ``display:`` names and ``order:`` for account keys
  * ``category_rules.yaml`` — the Phase 1 deterministic regex pre-pass
  * ``vendor_aliases.yaml`` — normalized description regex → stable vendor key

Three more editable YAML files ship in this directory and are read by their own
consumers, NOT through this module:
  * ``vendor_rules.yaml``         — your own clients/vendors, read by
    ``core/categorization.py`` and ``core/reconciliation.py``
  * ``transfer_keywords.yaml``    — transfer-linking keywords, read by
    ``core/transaction_linker.py``
  * ``aa_category_gifi_map.yaml`` — reasonableness fallback rules, read by
    ``core/reasonableness.py`` (its path comes from ``GIFI_MAPPING_PATH``)

What "degrades" means per table:
  * no account display names → the raw account key is shown ("CAD"), and the
    account order falls back to alphabetical;
  * no category rules → nothing is rule-locked and every transaction goes to
    the LLM categorization phases, which is the slower but still correct path;
  * no vendor aliases → descriptions cluster by their slugified leading tokens
    instead of by a shared canonical key.

Individual malformed entries are skipped (with a warning) rather than taking
the whole table down: one unparseable regex costs you that one rule.
"""

from __future__ import annotations

import logging
import re
from functools import lru_cache
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

CONFIG_DIR = Path(__file__).resolve().parent


@lru_cache(maxsize=None)
def load_yaml(name: str) -> dict:
    """Return the parsed mapping in ``CONFIG_DIR/name``, or ``{}`` on any failure."""
    path = CONFIG_DIR / name
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        logger.warning("config data file %s not found — falling back to built-in defaults", name)
        return {}
    except OSError as exc:
        logger.warning("could not read config data file %s: %s", name, exc)
        return {}
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        logger.warning("could not parse config data file %s: %s", name, exc)
        return {}
    if data is None:
        return {}
    if not isinstance(data, dict):
        logger.warning("config data file %s is not a mapping (got %s) — ignoring it", name, type(data).__name__)
        return {}
    return data


@lru_cache(maxsize=1)
def account_display() -> dict[str, str]:
    """Account key → human-readable name, from ``accounts.yaml``.

    Keys are the values stored in ``transactions.account``; an account key with
    no entry here is displayed as-is by every call site.
    """
    raw = load_yaml("accounts.yaml").get("display")
    if not isinstance(raw, dict):
        if raw is not None:
            logger.warning("accounts.yaml: 'display' is not a mapping — ignoring it")
        return {}
    return {str(key): str(value) for key, value in raw.items()}


@lru_cache(maxsize=1)
def account_order() -> list[str]:
    """Preferred display order of account keys, from ``accounts.yaml``.

    Account keys not listed here are appended alphabetically by the callers, so
    an incomplete list never drops an account from a report.
    """
    raw = load_yaml("accounts.yaml").get("order")
    if not isinstance(raw, list):
        if raw is not None:
            logger.warning("accounts.yaml: 'order' is not a list — ignoring it")
        return []
    return [str(key) for key in raw]


@lru_cache(maxsize=1)
def category_rules() -> list[tuple[str, list[str], bool]]:
    """The deterministic category rules, from ``category_rules.yaml``.

    Returns ``(canonical_category_name, [regex, ...], non_reconcilable)`` triples
    in file order — the pre-pass is first-match-wins, so order is meaningful.
    Patterns are returned as strings (the caller runs ``re.search`` with
    ``re.IGNORECASE``); each one is compile-checked here and dropped with a
    warning if it is not a usable regex.
    """
    rules: list[tuple[str, list[str], bool]] = []
    for entry in load_yaml("category_rules.yaml").get("rules") or []:
        if not isinstance(entry, dict):
            logger.warning("category_rules.yaml: skipping non-mapping rule %r", entry)
            continue
        category = entry.get("category")
        patterns = entry.get("patterns")
        if not category or not isinstance(patterns, list):
            logger.warning("category_rules.yaml: skipping rule with no category or patterns: %r", entry)
            continue
        usable: list[str] = []
        for expression in patterns:
            try:
                re.compile(str(expression))
            except re.error as exc:
                logger.warning("category_rules.yaml: skipping unusable pattern %r: %s", expression, exc)
                continue
            usable.append(str(expression))
        if not usable:
            continue
        rules.append((str(category), usable, bool(entry.get("non_reconcilable", False))))
    return rules


@lru_cache(maxsize=1)
def vendor_aliases() -> list[tuple[re.Pattern, str]]:
    """Compiled vendor alias table, from ``vendor_aliases.yaml``.

    Returns ``(compiled case-insensitive pattern, stable key)`` pairs in file
    order — the first match wins. An entry whose pattern will not compile is
    skipped with a warning.
    """
    aliases: list[tuple[re.Pattern, str]] = []
    for entry in load_yaml("vendor_aliases.yaml").get("aliases") or []:
        if not isinstance(entry, dict):
            logger.warning("vendor_aliases.yaml: skipping non-mapping alias %r", entry)
            continue
        expression = entry.get("pattern")
        key = entry.get("key")
        if not expression or not key:
            logger.warning("vendor_aliases.yaml: skipping alias with no pattern or key: %r", entry)
            continue
        try:
            compiled = re.compile(str(expression), re.I)
        except re.error as exc:
            logger.warning("vendor_aliases.yaml: skipping unusable pattern %r: %s", expression, exc)
            continue
        aliases.append((compiled, str(key)))
    return aliases
