"""Every `.format()`-ed prompt template in `core/` must actually format.

A prompt template is a plain string, so a placeholder the call site never
supplies raises `KeyError` on every single call. Those calls sit inside a
`try/except Exception` with a heuristic fallback, which is the dangerous part:
the model is never asked anything, the fallback answers every time, and nothing
in the logs says the template is dead.

The guard is structural rather than a hand-kept list: this module walks the AST
of every file under `core/`, finds each module-level string constant that is
used with `.format(`, and checks the template's placeholders against the
keywords the call site passes. A template with a placeholder nobody supplies is
a dead template, whatever the fallback does about it.
"""

from __future__ import annotations

import ast
import string
from pathlib import Path

import pytest

from config.settings import PROJECT_ROOT

CORE = PROJECT_ROOT / "core"

# Templates that exist today. Discovery finding *fewer* than these means the
# walk itself broke (a rename, a move, an AST shape it no longer recognises) —
# which would silently turn this whole file into a no-op.
KNOWN_TEMPLATES = {
    "VALIDATION_PROMPT",            # core/date_validator.py
    "DATE_VALIDATION_PROMPT",       # core/match_validator.py
    "AMOUNT_VALIDATION_PROMPT",     # core/match_validator.py
    "_AMOUNT_AGENT_PROMPT",         # core/reconciliation.py
    "_DATE_AGENT_PROMPT",           # core/reconciliation.py
    "_FINAL_AGENT_PROMPT",          # core/reconciliation.py
    "BALANCE_CORRECTION_PROMPT",    # core/llm_statement_parser.py
    "VENDOR_NORMALIZATION_FALLBACK",  # core/categorization_agent/prompts.py
    "VENDOR_DECISIONS",
    "REFINEMENT",
    "SELF_CONSISTENCY",
}


# ---------------------------------------------------------------------------
# Template/placeholder helpers
# ---------------------------------------------------------------------------

def placeholder_fields(template: str) -> tuple[set[str], int]:
    """Return (named root fields, number of positional/auto-numbered fields).

    Nested access is reduced to its root, so ``{cfg.model}`` and ``{rows[0]}``
    both count as ``cfg`` / ``rows`` — that is the name `.format()` looks up in
    its keywords. Format specs are parsed too, for ``{x:{width}}``.
    """
    named: set[str] = set()
    positional = 0

    def walk(text: str) -> None:
        nonlocal positional
        for _literal, field_name, format_spec, _conv in string.Formatter().parse(text):
            if format_spec:
                walk(format_spec)
            if field_name is None:
                continue
            root = field_name.split(".")[0].split("[")[0]
            if root == "" or root.isdigit():
                positional += 1
            else:
                named.add(root)

    walk(template)
    return named, positional


def missing_placeholders(template: str, supplied: set[str]) -> set[str]:
    """Named placeholders the call site does not supply — i.e. future KeyErrors."""
    named, _positional = placeholder_fields(template)
    return named - supplied


# ---------------------------------------------------------------------------
# AST discovery
# ---------------------------------------------------------------------------

def _core_files() -> list[Path]:
    return sorted(p for p in CORE.rglob("*.py") if "__pycache__" not in p.parts)


def _string_constants() -> dict[str, dict[Path, str]]:
    """Every module-level `NAME = "..."` under core/, as name -> {path: value}."""
    consts: dict[str, dict[Path, str]] = {}
    for path in _core_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in tree.body:
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, ast.AnnAssign):
                targets = [node.target]
            else:
                continue
            value = node.value
            if not (isinstance(value, ast.Constant) and isinstance(value.value, str)):
                continue
            for target in targets:
                if isinstance(target, ast.Name):
                    consts.setdefault(target.id, {})[path] = value.value
    return consts


class FormatSite:
    """One `TEMPLATE.format(...)` call found in core/."""

    def __init__(self, path: Path, lineno: int, name: str, template: str,
                 keywords: set[str], star_star: bool, positional_args: int):
        self.path = path
        self.lineno = lineno
        self.name = name
        self.template = template
        self.keywords = keywords
        self.star_star = star_star
        self.positional_args = positional_args

    @property
    def id(self) -> str:
        return f"{self.path.relative_to(PROJECT_ROOT).as_posix()}:{self.lineno}:{self.name}"


def _format_sites() -> list[FormatSite]:
    """Find every `NAME.format(...)` in core/ whose NAME is a string constant.

    The template is resolved from the calling module first, then from any other
    module under core/ that defines that name (templates live in
    `categorization_agent/prompts.py` and are formatted by their phase modules).
    """
    consts = _string_constants()
    sites: list[FormatSite] = []

    for path in _core_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (isinstance(func, ast.Attribute) and func.attr == "format"):
                continue
            if not isinstance(func.value, ast.Name):
                continue
            name = func.value.id
            by_path = consts.get(name)
            if not by_path:
                continue
            template = by_path.get(path) or next(iter(by_path.values()))
            sites.append(FormatSite(
                path=path,
                lineno=node.lineno,
                name=name,
                template=template,
                keywords={kw.arg for kw in node.keywords if kw.arg},
                star_star=any(kw.arg is None for kw in node.keywords),
                positional_args=len(node.args),
            ))
    return sites


FORMAT_SITES = _format_sites()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_discovery_finds_every_known_template():
    """If the walk stops finding templates, the tests below stop meaning anything."""
    found = {site.name for site in FORMAT_SITES}
    assert KNOWN_TEMPLATES <= found, f"discovery lost: {sorted(KNOWN_TEMPLATES - found)}"


@pytest.mark.parametrize("site", FORMAT_SITES, ids=lambda s: s.id)
def test_template_formats_with_representative_arguments(site: FormatSite):
    """The template fills in with one representative value per placeholder.

    Catches an `IndexError` from auto-numbered `{}` mixed with named fields, a
    bad conversion or format spec, and an unescaped single brace — everything
    `.format()` can raise once the keywords are right.
    """
    named, positional = placeholder_fields(site.template)
    args = ["REPRESENTATIVE"] * max(positional, site.positional_args)
    kwargs = {field: "REPRESENTATIVE" for field in named}

    try:
        rendered = site.template.format(*args, **kwargs)
    except (KeyError, IndexError, ValueError) as exc:
        pytest.fail(f"{site.id} does not format: {type(exc).__name__}: {exc}")

    for field in named:
        assert "{" + field + "}" not in rendered, f"{site.id}: {{{field}}} survived formatting"


@pytest.mark.parametrize("site", FORMAT_SITES, ids=lambda s: s.id)
def test_call_site_supplies_every_placeholder(site: FormatSite):
    """A named field nobody passes makes the template unusable.

    A `.format()` call that omits a named placeholder raises `KeyError` on every
    single invocation. Where that call sits inside a `try/except Exception` with
    a fallback — the shape the validators in `core/` use — the feature never
    runs and nothing reports it.
    """
    if site.star_star:
        pytest.skip(f"{site.id} formats with **kwargs; keywords not statically known")

    missing = missing_placeholders(site.template, site.keywords)
    assert not missing, (
        f"{site.id} has placeholder(s) the call site never supplies: {sorted(missing)} — "
        "every .format() call there raises KeyError"
    )


def test_the_guard_detects_a_placeholder_no_caller_supplies():
    """A check that cannot fail is not a check, so the guard is run on a known case.

    The dead template embeds a conditional expression in a plain string, so
    `.format()` reads the whole expression as a field name; the repaired one
    passes that choice in as a value.
    """
    dead = (
        'DATE GAP: {gap_days} days '
        '(transaction is {"after" if txn_after else "before"} the document)'
    )
    supplied = {"gap_days", "txn_after"}

    assert missing_placeholders(dead, supplied) == {'"after" if txn_after else "before"'}
    with pytest.raises(KeyError):
        dead.format(gap_days=15, txn_after=True)

    fixed = "DATE GAP: {gap_days} days (transaction is {direction} the document)"
    assert missing_placeholders(fixed, {"gap_days", "direction"}) == set()
    assert "after" in fixed.format(gap_days=15, direction="after")
