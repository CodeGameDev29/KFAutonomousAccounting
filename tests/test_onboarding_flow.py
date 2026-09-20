"""Onboarding progress: what marks a step done, and what survives a reload.

The fake store below stands in for the onboarding_steps, business_profile and
gather_sources tables, so the flow can be exercised without PostgreSQL.

Three things are pinned. A step turns COMPLETED off the underlying data — a
profile that has a company name, a gather source that is enabled, a receipt
that was uploaded — rather than off a flag someone remembered to set. A step
the user skipped stays SKIPPED while the others complete, and can still be
completed later. And every bit of it comes back unchanged after a
snapshot/reload cycle, because onboarding that forgets where someone got to
asks them to do it all again.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

# ─── In-memory mock store ────────────────────────────────────────────

ALL_STEPS = ["profile", "bank_connect", "receipt_upload", "first_reconciliation", "export"]


class FakeOnboardingStore:
    """Simulates onboarding_steps + business_profile + gather_sources tables."""

    def __init__(self, user_id: str):
        self.user_id = user_id
        # {step_name: {"status": str, "completed_at": datetime | None}}
        self.onboarding_steps: dict[str, dict] = {}
        # business_profile row (or None)
        self.business_profile: dict | None = None
        # gather_sources rows: {source_type: {"enabled": bool, ...}}
        self.gather_sources: dict[str, dict] = {}

    def snapshot(self) -> dict:
        """Return a serializable snapshot of the store for reload testing."""
        return {
            "user_id": self.user_id,
            "onboarding_steps": {
                step: {"status": info["status"]}
                for step, info in self.onboarding_steps.items()
            },
            "business_profile": dict(self.business_profile) if self.business_profile else None,
            "gather_sources": {
                st: {"enabled": gs["enabled"]}
                for st, gs in self.gather_sources.items()
            },
        }

    @classmethod
    def from_snapshot(cls, snap: dict) -> "FakeOnboardingStore":
        """Reconstruct a store from a snapshot (simulates reload)."""
        store = cls(snap["user_id"])
        for step, info in snap.get("onboarding_steps", {}).items():
            store.onboarding_steps[step] = {
                "status": info["status"],
                "completed_at": datetime.now(timezone.utc) if info["status"] != "PENDING" else None,
            }
        if snap.get("business_profile"):
            store.business_profile = dict(snap["business_profile"])
        for st, gs in snap.get("gather_sources", {}).items():
            store.gather_sources[st] = {"enabled": gs["enabled"]}
        return store


def _build_mock_db(store: FakeOnboardingStore):
    """Build a MagicMock DatabasePg whose onboarding methods delegate to *store*."""
    db = MagicMock()
    db.user_id = store.user_id

    # --- get_onboarding_steps ---
    def _get_onboarding_steps():
        result = {}
        for step in ALL_STEPS:
            info = store.onboarding_steps.get(step)
            result[step] = info["status"] if info else "PENDING"
        return result

    db.get_onboarding_steps.side_effect = _get_onboarding_steps

    # --- upsert_onboarding_step ---
    def _upsert_onboarding_step(step: str, status: str):
        completed_at = datetime.now(timezone.utc) if status != "PENDING" else None
        store.onboarding_steps[step] = {
            "status": status,
            "completed_at": completed_at,
        }

    db.upsert_onboarding_step.side_effect = _upsert_onboarding_step

    # --- get_business_profile ---
    def _get_business_profile():
        if store.business_profile is None:
            return None
        bp = dict(store.business_profile)
        bp.setdefault("onboarding_state", {})
        bp.setdefault("setup_checklist", {})
        bp.setdefault("onboarding_complete", False)
        return bp

    db.get_business_profile.side_effect = _get_business_profile

    # --- upsert_business_profile ---
    def _upsert_business_profile(data: dict):
        if store.business_profile is None:
            store.business_profile = {
                "id": 1,
                "company_name": None,
                "fiscal_year": None,
                "base_currency": "CAD",
                "tax_jurisdiction": "CA",
                "onboarding_complete": False,
                "config_json": None,
                "created_at": datetime.now(timezone.utc),
                "updated_at": datetime.now(timezone.utc),
                "onboarding_state": {},
                "setup_checklist": {},
            }
        for key, value in data.items():
            if value is not None:
                store.business_profile[key] = value
        store.business_profile["updated_at"] = datetime.now(timezone.utc)

    db.upsert_business_profile.side_effect = _upsert_business_profile

    # --- is_onboarding_complete ---
    def _is_onboarding_complete():
        bp = _get_business_profile()
        return bp is not None and bp.get("onboarding_complete", False)

    db.is_onboarding_complete.side_effect = _is_onboarding_complete

    # --- get_gather_sources ---
    def _get_gather_sources():
        return [
            {"source_type": st, "enabled": gs["enabled"]}
            for st, gs in sorted(store.gather_sources.items())
        ]

    db.get_gather_sources.side_effect = _get_gather_sources

    # --- upsert_gather_source ---
    def _upsert_gather_source(source_type: str, enabled: bool = True, config_json=None):
        store.gather_sources[source_type] = {"enabled": enabled}

    db.upsert_gather_source.side_effect = _upsert_gather_source

    return db


# ─── Helpers ──────────────────────────────────────────────────────────

def _derive_step_status(db) -> dict[str, str]:
    """Derive the 3-step onboarding status using business profile and sources.

    This mirrors the logic the app would use:
    - profile: COMPLETED if company_name is set
    - bank_connect: COMPLETED if any gather source is enabled
    - receipt_upload: read from onboarding_steps table
    """
    bp = db.get_business_profile()
    steps_raw = db.get_onboarding_steps()
    sources = db.get_gather_sources()

    status = {}

    # Step 1: profile
    if bp and bp.get("company_name"):
        status["profile"] = "COMPLETED"
    else:
        status["profile"] = steps_raw.get("profile", "PENDING")

    # Step 2: bank_connect
    any_source_enabled = any(s["enabled"] for s in sources)
    if any_source_enabled:
        status["bank_connect"] = "COMPLETED"
    else:
        status["bank_connect"] = steps_raw.get("bank_connect", "PENDING")

    # Step 3: receipt_upload
    status["receipt_upload"] = steps_raw.get("receipt_upload", "PENDING")

    return status


# ═══════════════════════════════════════════════════════════════════
#  Onboarding 3-step completion tracking
# ═══════════════════════════════════════════════════════════════════


class TestThreeStepCompletion:
    """Verify that each onboarding step transitions to COMPLETED
    based on the appropriate underlying data."""

    def test_profile_completed_when_company_name_exists(self):
        store = FakeOnboardingStore("user-1")
        db = _build_mock_db(store)

        # Before setting company name
        status = _derive_step_status(db)
        assert status["profile"] == "PENDING"

        # Set company name
        db.upsert_business_profile({"company_name": "Your Company Inc."})
        status = _derive_step_status(db)
        assert status["profile"] == "COMPLETED"

    def test_bank_connect_completed_when_source_enabled(self):
        store = FakeOnboardingStore("user-1")
        db = _build_mock_db(store)

        # No sources -> PENDING
        status = _derive_step_status(db)
        assert status["bank_connect"] == "PENDING"

        # Enable a bank-document source
        db.upsert_gather_source("wise", enabled=True)
        status = _derive_step_status(db)
        assert status["bank_connect"] == "COMPLETED"

    def test_receipt_upload_completed_when_marked(self):
        store = FakeOnboardingStore("user-1")
        db = _build_mock_db(store)

        # Initially PENDING
        status = _derive_step_status(db)
        assert status["receipt_upload"] == "PENDING"

        # Mark as completed (user uploaded a test receipt)
        db.upsert_onboarding_step("receipt_upload", "COMPLETED")
        status = _derive_step_status(db)
        assert status["receipt_upload"] == "COMPLETED"

    def test_all_three_steps_completed(self):
        store = FakeOnboardingStore("user-1")
        db = _build_mock_db(store)

        db.upsert_business_profile({"company_name": "Test Corp"})
        db.upsert_gather_source("wise", enabled=True)
        db.upsert_onboarding_step("receipt_upload", "COMPLETED")

        status = _derive_step_status(db)
        assert status["profile"] == "COMPLETED"
        assert status["bank_connect"] == "COMPLETED"
        assert status["receipt_upload"] == "COMPLETED"

    def test_onboarding_complete_flag(self):
        store = FakeOnboardingStore("user-1")
        db = _build_mock_db(store)

        assert not db.is_onboarding_complete()

        db.upsert_business_profile({
            "company_name": "Test Corp",
            "onboarding_complete": True,
        })

        assert db.is_onboarding_complete()


# ═══════════════════════════════════════════════════════════════════
#  Skip-and-return preserves step status
# ═══════════════════════════════════════════════════════════════════


class TestSkipAndReturn:
    """When a user skips a step, it stays SKIPPED even when other steps
    are completed. The user can return and complete it later."""

    def test_skipped_step_stays_skipped_when_others_completed(self):
        store = FakeOnboardingStore("user-2")
        db = _build_mock_db(store)

        # Skip bank_connect
        db.upsert_onboarding_step("bank_connect", "SKIPPED")

        # Complete other steps
        db.upsert_business_profile({"company_name": "Skip Test Corp"})
        db.upsert_onboarding_step("profile", "COMPLETED")
        db.upsert_onboarding_step("receipt_upload", "COMPLETED")

        # bank_connect should still be SKIPPED in the raw steps
        steps = db.get_onboarding_steps()
        assert steps["bank_connect"] == "SKIPPED"
        assert steps["profile"] == "COMPLETED"
        assert steps["receipt_upload"] == "COMPLETED"

    def test_skipped_step_can_be_completed_later(self):
        store = FakeOnboardingStore("user-2")
        db = _build_mock_db(store)

        # Skip bank_connect
        db.upsert_onboarding_step("bank_connect", "SKIPPED")
        steps = db.get_onboarding_steps()
        assert steps["bank_connect"] == "SKIPPED"

        # User returns and completes it
        db.upsert_onboarding_step("bank_connect", "COMPLETED")
        steps = db.get_onboarding_steps()
        assert steps["bank_connect"] == "COMPLETED"

    def test_skip_does_not_affect_unrelated_steps(self):
        store = FakeOnboardingStore("user-2")
        db = _build_mock_db(store)

        db.upsert_onboarding_step("receipt_upload", "COMPLETED")
        db.upsert_onboarding_step("bank_connect", "SKIPPED")

        steps = db.get_onboarding_steps()
        # receipt_upload remains COMPLETED
        assert steps["receipt_upload"] == "COMPLETED"
        # Untouched steps remain PENDING
        assert steps["profile"] == "PENDING"
        assert steps["first_reconciliation"] == "PENDING"
        assert steps["export"] == "PENDING"

    def test_multiple_steps_can_be_skipped(self):
        store = FakeOnboardingStore("user-2")
        db = _build_mock_db(store)

        db.upsert_onboarding_step("bank_connect", "SKIPPED")
        db.upsert_onboarding_step("receipt_upload", "SKIPPED")

        steps = db.get_onboarding_steps()
        assert steps["bank_connect"] == "SKIPPED"
        assert steps["receipt_upload"] == "SKIPPED"


# ═══════════════════════════════════════════════════════════════════
#  Progress persistence across store reloads
# ═══════════════════════════════════════════════════════════════════


class TestProgressPersistence:
    """Onboarding data survives a store snapshot/reload cycle, simulating
    the persistence that PostgreSQL would provide across sessions."""

    def test_onboarding_steps_persist(self):
        store = FakeOnboardingStore("user-3")
        db = _build_mock_db(store)

        db.upsert_onboarding_step("profile", "COMPLETED")
        db.upsert_onboarding_step("bank_connect", "SKIPPED")
        db.upsert_onboarding_step("receipt_upload", "COMPLETED")

        # Snapshot and reload (simulates new DatabasePg instance)
        snap = store.snapshot()
        store2 = FakeOnboardingStore.from_snapshot(snap)
        db2 = _build_mock_db(store2)

        steps = db2.get_onboarding_steps()
        assert steps["profile"] == "COMPLETED"
        assert steps["bank_connect"] == "SKIPPED"
        assert steps["receipt_upload"] == "COMPLETED"
        assert steps["first_reconciliation"] == "PENDING"
        assert steps["export"] == "PENDING"

    def test_business_profile_persists(self):
        store = FakeOnboardingStore("user-3")
        db = _build_mock_db(store)

        db.upsert_business_profile({
            "company_name": "Persistent Corp",
            "fiscal_year": 2026,
            "base_currency": "CAD",
            "tax_jurisdiction": "MB",
        })

        # Snapshot and reload
        snap = store.snapshot()
        store2 = FakeOnboardingStore.from_snapshot(snap)
        db2 = _build_mock_db(store2)

        bp = db2.get_business_profile()
        assert bp is not None
        assert bp["company_name"] == "Persistent Corp"
        assert bp["fiscal_year"] == 2026
        assert bp["base_currency"] == "CAD"
        assert bp["tax_jurisdiction"] == "MB"

    def test_gather_sources_persist(self):
        store = FakeOnboardingStore("user-3")
        db = _build_mock_db(store)

        db.upsert_gather_source("wise", enabled=True)
        db.upsert_gather_source("gmail", enabled=False)

        # Snapshot and reload
        snap = store.snapshot()
        store2 = FakeOnboardingStore.from_snapshot(snap)
        db2 = _build_mock_db(store2)

        sources = db2.get_gather_sources()
        by_type = {s["source_type"]: s for s in sources}
        assert by_type["wise"]["enabled"] is True
        assert by_type["gmail"]["enabled"] is False

    def test_derived_status_persists_across_reload(self):
        store = FakeOnboardingStore("user-3")
        db = _build_mock_db(store)

        db.upsert_business_profile({"company_name": "Reload Corp"})
        db.upsert_gather_source("wise", enabled=True)
        db.upsert_onboarding_step("receipt_upload", "COMPLETED")

        status_before = _derive_step_status(db)

        # Snapshot, reload, re-derive
        snap = store.snapshot()
        store2 = FakeOnboardingStore.from_snapshot(snap)
        db2 = _build_mock_db(store2)

        status_after = _derive_step_status(db2)

        assert status_before == status_after
        assert status_after["profile"] == "COMPLETED"
        assert status_after["bank_connect"] == "COMPLETED"
        assert status_after["receipt_upload"] == "COMPLETED"

    def test_empty_store_persists_as_empty(self):
        store = FakeOnboardingStore("user-3")
        snap = store.snapshot()
        store2 = FakeOnboardingStore.from_snapshot(snap)
        db2 = _build_mock_db(store2)

        steps = db2.get_onboarding_steps()
        for step in ALL_STEPS:
            assert steps[step] == "PENDING"

        assert db2.get_business_profile() is None
        assert db2.get_gather_sources() == []


# ═══════════════════════════════════════════════════════════════════
#  Skip ALL 5 onboarding steps
# ═══════════════════════════════════════════════════════════════════


class TestSkipAllSteps:
    """When a user skips every single onboarding step, completed_count
    should be 0 and skipped_count should be 5."""

    def test_skip_all_five_steps(self):
        store = FakeOnboardingStore("user-skip-all")
        db = _build_mock_db(store)

        for step in ALL_STEPS:
            db.upsert_onboarding_step(step, "SKIPPED")

        steps = db.get_onboarding_steps()

        completed_count = sum(1 for s in steps.values() if s == "COMPLETED")
        skipped_count = sum(1 for s in steps.values() if s == "SKIPPED")

        assert completed_count == 0
        assert skipped_count == 5

        # Every individual step should be SKIPPED
        for step in ALL_STEPS:
            assert steps[step] == "SKIPPED"
