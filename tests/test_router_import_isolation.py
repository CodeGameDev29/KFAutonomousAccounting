"""Every router module must be importable on its own and still have its routes.

What this locks down
--------------------
A router module that does ``from server.app import limiter`` *while its own
routes are being defined* — inside a decorator factory, say, which runs at
decoration time — re-enters a half-initialised ``server.app`` whenever it is
imported before the app itself. The ``except Exception`` around such an import
then quietly substitutes a no-op decorator, and depending on which module wins
the race the damage ranges from "the per-route rate limit is silently off" to a
router that finishes importing with **zero routes on it**, so every path under
it answers 405 with nothing in the log to explain it.

``server/rate_limit.py`` owns the limiter and imports nothing that imports the
app, so the ordering cannot bite. This test is the guard: each router module is
imported **first**, in its own fresh interpreter (``subprocess`` with ``-c``),
and has to come back with a non-zero route count.

A plain in-process ``import`` cannot test this — by the time pytest has
collected anything, ``server.app`` is already in ``sys.modules`` and the broken
ordering is unreachable. One process per module is the point, and they are run
concurrently so the whole file costs a few seconds rather than half a minute.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

# The two modules that decorate routes with the shared limiter, named
# explicitly so a failure says which one lost its routes.
RATE_LIMITED_MODULES = (
    "server.local_auth",
    "server.api.auth_validation",
)

_ROUTER_RE = re.compile(r"^(\w*router\w*)\s*=\s*APIRouter", re.M)

# The probe runs in a fresh interpreter: import the module FIRST (nothing else
# has been imported yet), then report every APIRouter it exposes and how many
# routes are on it. Printed as one JSON line so log noise on stderr is harmless.
_PROBE = """
import json, sys
import {module} as m
from fastapi import APIRouter
out = {{
    name: len(obj.routes)
    for name, obj in vars(m).items()
    if isinstance(obj, APIRouter)
}}
print("__PROBE__" + json.dumps({{
    "routers": out,
    "app_imported": "server.app" in sys.modules,
}}))
"""


def _router_modules() -> list[str]:
    """Every module under ``server/api`` that builds an APIRouter, plus the
    identity service. ``server/api/__init__.py`` is excluded: it exists to
    assemble the others, so importing it is the ordinary path, not isolation."""
    modules = ["server.local_auth", "server.google_auth"]
    for path in sorted((REPO_ROOT / "server" / "api").glob("*.py")):
        if path.name == "__init__.py":
            continue
        if _ROUTER_RE.search(path.read_text(encoding="utf-8")):
            modules.append(f"server.api.{path.stem}")
    return modules


def _probe(module: str) -> dict:
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE.format(module=module)],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert proc.returncode == 0, (
        f"{module} could not be imported on its own:\n"
        f"{proc.stdout}\n{proc.stderr}"
    )
    line = next(
        (ln for ln in proc.stdout.splitlines() if ln.startswith("__PROBE__")),
        None,
    )
    assert line is not None, f"{module}: no probe output\n{proc.stdout}\n{proc.stderr}"
    return json.loads(line[len("__PROBE__"):])


@pytest.fixture(scope="module")
def probes() -> dict[str, dict]:
    modules = _router_modules()
    with ThreadPoolExecutor(max_workers=8) as pool:
        return dict(zip(modules, pool.map(_probe, modules)))


@pytest.mark.parametrize("module", RATE_LIMITED_MODULES)
def test_a_rate_limited_module_keeps_its_routes_when_imported_first(module, probes):
    """The modules whose decorators reach for the limiter at import time."""
    routers = probes[module]["routers"]
    assert routers, f"{module} exposes no APIRouter at all"
    for name, count in routers.items():
        assert count > 0, (
            f"{module}.{name} imported standalone with {count} routes - every path "
            "under it would answer 405. Something in this module imports "
            "server.app while its routes are being defined."
        )


def test_no_router_module_imports_the_app_to_get_its_routes(probes):
    """Sweep: every router module in the tree, not just the named ones."""
    empty: list[str] = []
    for module, probe in probes.items():
        if not probe["routers"]:
            empty.append(f"{module}: no APIRouter found")
        for name, count in probe["routers"].items():
            if count == 0:
                empty.append(f"{module}.{name}: 0 routes")
    assert not empty, "Routers that lost their routes when imported first: " + "; ".join(empty)


def test_importing_a_router_does_not_drag_in_the_whole_app(probes):
    """The property that makes the ordering stop mattering.

    A route module reaching for ``server.app`` is what creates the cycle. It also
    means importing one router boots the entire application - every other router,
    the scheduler imports, the static-file mount - which is what makes such a
    failure so hard to see. ``server/api/events.py`` is exempt: the SSE hub is
    genuinely part of the app surface it re-exports from.
    """
    offenders = [
        module
        for module, probe in probes.items()
        if probe["app_imported"] and module != "server.api.events"
    ]
    assert not offenders, (
        "These modules import server.app just by being imported: "
        + ", ".join(sorted(offenders))
    )
