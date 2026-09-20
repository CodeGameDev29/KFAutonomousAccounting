"""Lightweight test framework for the E2E API suites.

Tracks a verdict per test, generates structured results, and writes
progress.json for the orchestrator to poll.

Three verdicts, matching the journeys: PASS, FAIL, and NOT-EXERCISED. There is
no SKIP. A missing setup step this harness could have performed is a FAIL —
that is what a silent skip used to hide. NOT-EXERCISED is reserved for what the
install genuinely cannot offer (registration closed, an integration that is not
configured, a persona who does not drive this surface) and always carries the reason, so a
reader can tell an untested surface from a working one.

A test may also declare `depends_on`. When a precondition test failed, the
dependent test is NOT-EXERCISED **carrying the precondition's own error as the
cause** — never FAIL. Otherwise one rejected upload reads as twenty broken
features.
"""

from __future__ import annotations

import json
import logging
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

SKILL_DIR = Path(__file__).resolve().parent.parent
PROGRESS_FILE = SKILL_DIR / "progress.json"
REPORTS_DIR = SKILL_DIR / "reports"


@dataclass
class TestResult:
    name: str
    profile: str
    suite: str
    status: str  # "passed" | "failed" | "not-exercised"
    duration_ms: int = 0
    error: str = ""
    details: str = ""


@dataclass
class ProfileResult:
    name: str
    description: str
    results: list[TestResult] = field(default_factory=list)

    @property
    def passed(self) -> int:
        return sum(1 for r in self.results if r.status == "passed")

    @property
    def failed(self) -> int:
        return sum(1 for r in self.results if r.status == "failed")

    @property
    def not_exercised(self) -> int:
        return sum(1 for r in self.results if r.status == "not-exercised")

    @property
    def total(self) -> int:
        return len(self.results)


class TestRunner:
    """Runs test functions, tracks results, writes progress."""

    def __init__(self):
        self.profile_results: list[ProfileResult] = []
        self.current_profile: ProfileResult | None = None
        self.current_suite: str = ""
        self.start_time: float = 0
        self._all_results: list[TestResult] = []
        # Per-profile index by test name, for precondition lookups.
        self._by_name: dict[str, TestResult] = {}

    def start(self):
        self.start_time = time.time()
        self._write_progress("running")

    def set_profile(self, name: str, description: str):
        self.current_profile = ProfileResult(name=name, description=description)
        self.profile_results.append(self.current_profile)
        # Preconditions are per-persona: persona 2's uploads are its own.
        self._by_name = {}
        self._write_progress("running")

    def set_suite(self, suite: str):
        self.current_suite = suite
        self._write_progress("running")

    def _precondition_verdict(self, depends_on: tuple[str, ...]) -> tuple[str, str] | None:
        """Decide whether a test can run at all, given its preconditions.

        Returns None to run it, or (status, reason). A failed or not-exercised
        precondition makes this test NOT-EXERCISED and carries the cause
        forward; a precondition that never ran is a wiring bug in the suite
        registry and is surfaced as a FAIL rather than silently ignored.
        """
        for dep in depends_on:
            prior = self._by_name.get(dep)
            if prior is None:
                return ("failed",
                        f"harness wiring: declared precondition `{dep}` has not run in this "
                        f"profile — fix the name or the order in runner.py's SUITE_REGISTRY")
            if prior.status == "failed":
                return ("not-exercised",
                        f"precondition `{dep}` FAILED, so this test never had its setup — "
                        f"cause: {prior.error[:300]}")
            if prior.status == "not-exercised":
                return ("not-exercised",
                        f"precondition `{dep}` was not exercised — cause: {prior.error[:300]}")
        return None

    def run_test(self, name: str, fn: Callable, *args,
                 depends_on: tuple[str, ...] = (), **kwargs) -> TestResult:
        """Execute a single test function and record the result."""
        profile_name = self.current_profile.name if self.current_profile else "unknown"
        gate = self._precondition_verdict(depends_on)
        if gate is not None:
            status, reason = gate
            result = TestResult(
                name=name, profile=profile_name, suite=self.current_suite,
                status=status, duration_ms=0, error=reason,
                details=("Not exercised: " if status == "not-exercised" else "") + reason,
            )
            if status == "not-exercised":
                logger.warning("  NOT-EXERCISED: %s — %s", name, reason)
            else:
                logger.error("  FAIL: %s — %s", name, reason)
            return self._record(result)

        t0 = time.time()
        try:
            fn(*args, **kwargs)
            duration = int((time.time() - t0) * 1000)
            result = TestResult(
                name=name, profile=profile_name, suite=self.current_suite,
                status="passed", duration_ms=duration,
            )
            logger.info("  PASS: %s (%dms)", name, duration)
        except NotExercised as e:
            # Not credit and not a hidden failure: the reason is carried through
            # to the report so the gap is visible.
            duration = int((time.time() - t0) * 1000)
            result = TestResult(
                name=name, profile=profile_name, suite=self.current_suite,
                status="not-exercised", duration_ms=duration,
                error=str(e),
                details=f"Not exercised: {e}",
            )
            logger.warning("  NOT-EXERCISED: %s — %s", name, e)
        except SkipTest as e:
            # Project policy: no SKIP state allowed. Record as FAIL.
            duration = int((time.time() - t0) * 1000)
            result = TestResult(
                name=name, profile=profile_name, suite=self.current_suite,
                status="failed", duration_ms=duration,
                error=f"[SKIP->FAIL] {e}",
                details=f"Test attempted to skip: {e}. Per project policy, missing setup = FAIL.",
            )
            logger.warning("  FAIL (was SKIP): %s — %s", name, e)
        except Exception as e:
            duration = int((time.time() - t0) * 1000)
            tb = traceback.format_exc()
            result = TestResult(
                name=name, profile=profile_name, suite=self.current_suite,
                status="failed", duration_ms=duration,
                error=str(e) or e.__class__.__name__, details=tb[-500:],
            )
            logger.error("  FAIL: %s — %s", name, result.error)

        return self._record(result)

    def _record(self, result: TestResult) -> TestResult:
        self._all_results.append(result)
        self._by_name[result.name] = result
        if self.current_profile:
            self.current_profile.results.append(result)
        self._write_progress("running")
        return result

    def finish(self):
        elapsed = time.time() - self.start_time
        status = "complete" if not any(r.status == "failed" for r in self._all_results) else "complete_with_failures"
        self._write_progress(status)
        self._write_report(elapsed)

    def _write_progress(self, status: str):
        elapsed = time.time() - self.start_time if self.start_time else 0
        data = {
            "status": status,
            "elapsed_seconds": round(elapsed, 1),
            "current_profile": self.current_profile.name if self.current_profile else None,
            "current_phase": self.current_suite,
            "passed": sum(1 for r in self._all_results if r.status == "passed"),
            "failed": sum(1 for r in self._all_results if r.status == "failed"),
            "not_exercised": sum(1 for r in self._all_results if r.status == "not-exercised"),
            "total": len(self._all_results),
            "profiles_completed": sum(1 for p in self.profile_results if p is not self.current_profile),
            "profiles_total": len(self.profile_results),
        }
        try:
            PROGRESS_FILE.write_text(json.dumps(data, indent=2))
        except Exception:
            pass

    def _write_report(self, elapsed: float):
        REPORTS_DIR.mkdir(exist_ok=True)
        ts = time.strftime("%Y%m%d-%H%M%S")
        report_path = REPORTS_DIR / f"e2e-api-{ts}.md"

        lines = [
            "# E2E API Suite Report",
            f"\n**Date**: {time.strftime('%Y-%m-%d %H:%M:%S')}",
            f"**Duration**: {elapsed:.1f}s",
            f"**Total Tests**: {len(self._all_results)}",
            f"**Passed**: {sum(1 for r in self._all_results if r.status == 'passed')}",
            f"**Failed**: {sum(1 for r in self._all_results if r.status == 'failed')}",
            f"**Not exercised**: {sum(1 for r in self._all_results if r.status == 'not-exercised')}",
            "",
            "## Per-Profile Summary",
            "",
            "| Profile | Description | Tests | Passed | Failed | Not exercised |",
            "|---------|-------------|-------|--------|--------|---------------|",
        ]
        for p in self.profile_results:
            lines.append(
                f"| {p.name} | {p.description} | {p.total} | {p.passed} | {p.failed} "
                f"| {p.not_exercised} |"
            )

        # Failures detail
        failures = [r for r in self._all_results if r.status == "failed"]
        if failures:
            lines.extend(["", "## Failures", ""])
            for i, f in enumerate(failures, 1):
                lines.append(f"### {i}. [{f.profile}] {f.suite} / {f.name} ({f.duration_ms}ms)")
                lines.append(f"**Error**: {f.error}")
                if f.details:
                    lines.append(f"```\n{f.details}\n```")
                lines.append("")

        # Per-suite breakdown
        suites: dict[str, list[TestResult]] = {}
        for r in self._all_results:
            suites.setdefault(r.suite, []).append(r)
        not_exercised = [r for r in self._all_results if r.status == "not-exercised"]
        if not_exercised:
            lines.extend(["", "## Not exercised (with reason)", ""])
            for r in not_exercised:
                lines.append(f"- [{r.profile}] {r.suite} / {r.name} — {r.error}")

        lines.extend(["", "## Coverage by Suite", ""])
        lines.append("| Suite | Total | Passed | Failed | Not exercised |")
        lines.append("|-------|-------|--------|--------|---------------|")
        for suite, results in suites.items():
            p = sum(1 for r in results if r.status == "passed")
            f = sum(1 for r in results if r.status == "failed")
            n = sum(1 for r in results if r.status == "not-exercised")
            lines.append(f"| {suite} | {len(results)} | {p} | {f} | {n} |")

        # Durations, because the client timeouts in api_client.py are meant to be
        # set from what a run actually measured, not from a guess.
        slowest = sorted(self._all_results, key=lambda r: r.duration_ms, reverse=True)[:8]
        if slowest and slowest[0].duration_ms:
            lines.extend(["", "## Slowest tests (what the client timeouts have to fit)", ""])
            for r in slowest:
                lines.append(
                    f"- {r.duration_ms / 1000:.1f}s — [{r.profile}] {r.suite} / {r.name} ({r.status})"
                )

        report_path.write_text("\n".join(lines), encoding="utf-8")
        logger.info("Report written to %s", report_path)
        return report_path


class SkipTest(Exception):
    """A setup step this harness could have done was not done: records FAIL.

    Kept because a test that quietly skips reads as green. Raise it when the
    prerequisite was ours to arrange; raise NotExercised when the install
    genuinely cannot provide it, and declare `depends_on` when another test in
    this run was supposed to provide it.
    """
    pass


class NotExercised(Exception):
    """This surface cannot be driven on this install; records NOT-EXERCISED.

    Legitimate reasons: registration is closed so no second account exists, an
    optional integration is not configured (Gmail, Wise, PayPal, a mail
    transport), the persona under test does not drive this surface. The message is the reason
    and lands in the report. It never turns a broken feature green — if the
    thing could have been exercised, this is the wrong exception.
    """
    pass


def not_applicable_to_persona(profile, flag: str, what: str) -> None:
    """Raise NOT-EXERCISED for a surface this persona deliberately does not use.

    Names the flag and which *other* personas cover it, so a reader can tell
    "nobody tested this" from "another persona tested this".
    """
    from profiles import personas_with  # local import: profiles imports lib

    covered = [n for n in personas_with(flag) if n != profile.name]
    who = (f"persona(s) {', '.join(covered)} cover it" if covered else
           "no persona in this run covers it — a coverage gap, not a product state")
    raise NotExercised(
        f"persona '{profile.name}' does not {what} (profile flag {flag}=False); {who}"
    )


def assert_eq(actual, expected, msg: str = ""):
    if actual != expected:
        raise AssertionError(f"{msg}: expected {expected!r}, got {actual!r}")


def assert_in(value, container, msg: str = ""):
    if value not in container:
        raise AssertionError(f"{msg}: {value!r} not in {container!r}")


def _body_of(resp) -> str:
    try:
        return resp.text[:300]
    except Exception:
        return "<unreadable body>"


def assert_status(resp, expected: int, msg: str = ""):
    if resp.status_code != expected:
        raise AssertionError(
            f"{msg}: expected HTTP {expected}, got {resp.status_code}. Body: {_body_of(resp)}"
        )


def assert_status_in(resp, expected, msg: str = ""):
    """Assert the status is one of `expected`, and say what the body was.

    Keep `expected` to codes that mean *the feature worked*. Put a 400 in one of
    these lists and a receipt-upload test passes on a rejected upload, taking
    every downstream reconciliation verdict with it.
    """
    if resp.status_code not in expected:
        codes = "/".join(str(c) for c in expected)
        raise AssertionError(
            f"{msg}: expected HTTP {codes}, got {resp.status_code}. Body: {_body_of(resp)}"
        )


def assert_key(data: dict, key: str, msg: str = ""):
    if not isinstance(data, dict):
        raise AssertionError(
            f"{msg}: expected a JSON object, got {type(data).__name__}: {str(data)[:200]}"
        )
    if key not in data:
        raise AssertionError(f"{msg}: key '{key}' not in response: {list(data.keys())}")
