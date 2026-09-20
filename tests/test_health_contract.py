"""GET /health publishes the thresholds and limits a client has to agree with.

The web UI mirrors server-side behaviour: it shows the bulk-approve bar, it
rejects an over-sized file before uploading it, it labels the reconciliation
date window. Every one of those numbers is enforced by the server, so the
server is the one that states them — a constant baked into the frontend bundle
is how the two drift apart.

Three properties are tested here:

1. the shape and the types, because a client parses them;
2. the values track ``config/settings.py`` rather than being a second copy of
   the same literals;
3. they survive a degraded instance, since the probes that follow them in the
   handler swallow their own exceptions.
"""

from __future__ import annotations

import dataclasses

import pytest
from fastapi.testclient import TestClient

import config.settings as settings
from server.app import app


@pytest.fixture()
def client():
    return TestClient(app)


def _health(client: TestClient) -> dict:
    resp = client.get("/health")
    assert resp.status_code == 200
    return resp.json()


# ── Shape and types ──────────────────────────────────────────────────────


class TestHealthContractShape:
    def test_reconciliation_block_present_with_every_key(self, client):
        body = _health(client)
        assert "reconciliation" in body
        assert set(body["reconciliation"]) == {
            "auto_approve_threshold",
            "review_threshold",
            "bulk_approve_min_confidence",
            "default_date_window_days",
        }

    def test_limits_block_present(self, client):
        body = _health(client)
        assert "limits" in body
        assert "max_upload_mb" in body["limits"]

    def test_thresholds_are_floats_in_range(self, client):
        recon = _health(client)["reconciliation"]
        for key in (
            "auto_approve_threshold",
            "review_threshold",
            "bulk_approve_min_confidence",
        ):
            value = recon[key]
            assert isinstance(value, float), f"{key} is {type(value).__name__}"
            assert 0.0 <= value <= 1.0

    def test_date_window_is_a_positive_int(self, client):
        window = _health(client)["reconciliation"]["default_date_window_days"]
        assert isinstance(window, int) and not isinstance(window, bool)
        assert window > 0

    def test_max_upload_mb_is_a_positive_int(self, client):
        mb = _health(client)["limits"]["max_upload_mb"]
        assert isinstance(mb, int) and not isinstance(mb, bool)
        assert mb > 0


# ── The values are the settings, not a second copy of them ───────────────


class TestHealthContractTracksSettings:
    def test_values_equal_the_live_settings(self, client):
        body = _health(client)
        cfg = settings.reconciliation_config
        assert body["reconciliation"] == {
            "auto_approve_threshold": float(cfg.auto_approve_threshold),
            "review_threshold": float(cfg.review_threshold),
            "bulk_approve_min_confidence": float(cfg.bulk_approve_min_confidence),
            "default_date_window_days": int(cfg.date_window_days),
        }
        assert body["limits"]["max_upload_mb"] == int(settings.MAX_UPLOAD_MB)

    def test_defaults_are_the_documented_ones(self, client):
        """The values a fresh checkout serves, which .env.example writes out."""
        recon = _health(client)["reconciliation"]
        assert recon["auto_approve_threshold"] == 0.90
        assert recon["review_threshold"] == 0.60
        assert recon["bulk_approve_min_confidence"] == 0.85
        assert recon["default_date_window_days"] == 45
        assert _health(client)["limits"]["max_upload_mb"] == 50

    def test_health_follows_a_changed_reconciliation_setting(self, client, monkeypatch):
        monkeypatch.setattr(
            settings,
            "reconciliation_config",
            dataclasses.replace(
                settings.reconciliation_config,
                auto_approve_threshold=0.97,
                review_threshold=0.42,
                bulk_approve_min_confidence=0.66,
                date_window_days=21,
            ),
        )
        recon = _health(client)["reconciliation"]
        assert recon["auto_approve_threshold"] == 0.97
        assert recon["review_threshold"] == 0.42
        assert recon["bulk_approve_min_confidence"] == 0.66
        assert recon["default_date_window_days"] == 21

    def test_health_follows_a_changed_upload_cap(self, client, monkeypatch):
        monkeypatch.setattr(settings, "MAX_UPLOAD_MB", 5)
        assert _health(client)["limits"]["max_upload_mb"] == 5

    def test_max_upload_mb_matches_the_caps_the_routes_enforce(self, client):
        """Not a literal: the upload routes reject at exactly this many bytes."""
        from server.api.files import MAX_UPLOAD_BYTES
        from server.api.receipts import MAX_FILE_SIZE
        from server.api.transactions import (
            _NOTE_ATTACHMENT_MAX_BYTES,
            MAX_STATEMENT_SIZE,
        )

        mb = _health(client)["limits"]["max_upload_mb"]
        assert MAX_FILE_SIZE == mb * 1024 * 1024
        assert MAX_STATEMENT_SIZE == mb * 1024 * 1024
        assert _NOTE_ATTACHMENT_MAX_BYTES == mb * 1024 * 1024
        assert MAX_UPLOAD_BYTES == mb * 1024 * 1024


# ── A degraded instance still states its contract ────────────────────────


class TestHealthContractSurvivesDegradation:
    def test_keys_present_when_the_database_probe_fails(self, client, monkeypatch):
        import db.connection

        def _boom():
            raise RuntimeError("no database")

        monkeypatch.setattr(db.connection, "get_pool", _boom)

        body = _health(client)
        assert body["status"] == "degraded"
        assert body["reconciliation"]["bulk_approve_min_confidence"] == float(
            settings.reconciliation_config.bulk_approve_min_confidence
        )
        assert body["limits"]["max_upload_mb"] == int(settings.MAX_UPLOAD_MB)

    def test_keys_present_when_storage_and_extraction_probes_fail(
        self, client, monkeypatch
    ):
        import core.file_storage
        import db.connection

        def _boom(*args, **kwargs):
            raise RuntimeError("probe down")

        monkeypatch.setattr(db.connection, "get_pool", _boom)
        monkeypatch.setattr(core.file_storage, "validate_storage_connection", _boom)
        monkeypatch.setattr(settings, "extraction_provider_ladder", _boom)

        body = _health(client)
        assert body["status"] == "degraded"
        # The probe-dependent block is gone, the contract block is not.
        assert "extraction" not in body
        assert set(body["reconciliation"]) == {
            "auto_approve_threshold",
            "review_threshold",
            "bulk_approve_min_confidence",
            "default_date_window_days",
        }
        assert "max_upload_mb" in body["limits"]
