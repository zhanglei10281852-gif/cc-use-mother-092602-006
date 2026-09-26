from __future__ import annotations

import hashlib
import hmac
import json
import os
from datetime import UTC, datetime

import pytest

from app.core.clock import FrozenClock
from app.core.errors import ConflictError
from app.database import close_connection, get_connection, init_db
from app.registry.service import RegistryService, canonical

SIGNING_KEY = "registry-test-signing-key"


def run_metadata(run_id: str, *, payload_id: str = "payload-01", dose: float = 12.5, cycles: int = 40, derated: bool = True) -> dict:
    return {
        "payload_id": payload_id,
        "run_id": run_id,
        "model_code": "thermal-model",
        "parameter_version": "v3",
        "derated": derated,
        "radiation_dose": dose,
        "thermal_cycles": cycles,
        "environment": {"orbit": "LEO"},
    }


def register_payload(run_id: str, **kwargs) -> dict:
    return {"metadata": run_metadata(run_id, **kwargs), "summary": {"score": 0.98, "anomalies": 0}, "registered_by": "uploader-1"}


def register(client, run_id: str, **kwargs):
    response = client.post("/api/registry/entries", json=register_payload(run_id, **kwargs))
    assert response.status_code == 201, response.text
    return response.json()["entry"]


def verify_signature(body: dict) -> None:
    canonical_body = canonical({"data": body["data"], "issued_at": body["issued_at"]})
    assert body["digest"] == hashlib.sha256(canonical_body.encode()).hexdigest()
    expected = hmac.new(SIGNING_KEY.encode(), canonical_body.encode(), hashlib.sha256).hexdigest()
    assert body["signature"] == expected
    assert body["algorithm"] == "HMAC-SHA256"


@pytest.fixture(autouse=True)
def signing_key(monkeypatch):
    monkeypatch.setenv("REGISTRY_SIGNING_KEY", SIGNING_KEY)


def test_register_deduplicates_same_run_and_rejects_conflicts(client):
    entry = register(client, "run-0001")
    assert entry["status"] == "registered"
    assert entry["version"] == 1
    assert entry["metadata_digest"] and entry["summary_digest"]

    duplicate = client.post("/api/registry/entries", json=register_payload("run-0001"))
    assert duplicate.status_code == 200
    assert duplicate.json()["deduplicated"] is True
    assert duplicate.json()["entry"]["id"] == entry["id"]
    assert duplicate.json()["entry"]["version"] == 1

    conflict = register_payload("run-0001")
    conflict["summary"] = {"score": 0.5}
    rejected = client.post("/api/registry/entries", json=conflict)
    assert rejected.status_code == 409

    conflict_meta = register_payload("run-0001")
    conflict_meta["metadata"]["radiation_dose"] = 99.0
    rejected_meta = client.post("/api/registry/entries", json=conflict_meta)
    assert rejected_meta.status_code == 409

    empty_summary = register_payload("run-0002")
    empty_summary["summary"] = {}
    assert client.post("/api/registry/entries", json=empty_summary).status_code == 422


def test_publish_signed_query_and_retract_hides_from_published(client):
    entry = register(client, "run-0010")
    published = client.post(
        f"/api/registry/entries/{entry['id']}/publish",
        json={"actor": "reviewer-1", "expected_version": 1, "note": "复核通过"},
    )
    assert published.status_code == 200, published.text
    assert published.json()["status"] == "published"
    assert published.json()["version"] == 2
    assert published.json()["published_by"] == "reviewer-1"

    listing = client.get("/api/registry/entries", params={"status": "published"})
    assert listing.status_code == 200
    verify_signature(listing.json())
    assert [item["id"] for item in listing.json()["data"]["items"]] == [entry["id"]]

    detail = client.get(f"/api/registry/entries/{entry['id']}")
    verify_signature(detail.json())
    assert [event["action"] for event in detail.json()["data"]["events"]] == ["registered", "published"]

    retracted = client.post(
        f"/api/registry/entries/{entry['id']}/retract",
        json={"actor": "reviewer-2", "reason": "参数版本标注错误", "expected_version": 2},
    )
    assert retracted.status_code == 200, retracted.text
    assert retracted.json()["status"] == "retracted"
    assert retracted.json()["retract_reason"] == "参数版本标注错误"

    after = client.get("/api/registry/entries", params={"status": "published"})
    assert after.json()["data"]["items"] == []
    everything = client.get("/api/registry/entries")
    assert everything.json()["data"]["items"][0]["status"] == "retracted"

    detail_after = client.get(f"/api/registry/entries/{entry['id']}").json()
    assert [event["action"] for event in detail_after["data"]["events"]] == ["registered", "published", "retracted"]

    republish = client.post(
        f"/api/registry/entries/{entry['id']}/publish",
        json={"actor": "reviewer-1", "expected_version": 3},
    )
    assert republish.status_code == 409

    duplicate = client.post("/api/registry/entries", json=register_payload("run-0010"))
    assert duplicate.status_code == 200
    assert duplicate.json()["entry"]["status"] == "retracted"


def test_annotations_quality_labels_and_retraction(client):
    entry = register(client, "run-0020")
    label = client.post(
        f"/api/registry/entries/{entry['id']}/annotations",
        json={"kind": "quality_label", "label": "trusted", "reviewer": "reviewer-1"},
    )
    assert label.status_code == 201, label.text
    review = client.post(
        f"/api/registry/entries/{entry['id']}/annotations",
        json={"kind": "review", "comment": "热循环段数据完整，可以发布", "reviewer": "reviewer-2"},
    )
    assert review.status_code == 201

    missing_label = client.post(
        f"/api/registry/entries/{entry['id']}/annotations",
        json={"kind": "quality_label", "reviewer": "reviewer-1"},
    )
    assert missing_label.status_code == 422

    summary = client.get("/api/registry/summary")
    verify_signature(summary.json())
    assert summary.json()["data"]["active_quality_labels"] == {"trusted": 1}

    annotation_id = label.json()["id"]
    retracted = client.post(
        f"/api/registry/annotations/{annotation_id}/retract",
        json={"actor": "reviewer-1", "reason": "误标，辐射剂量单位混淆"},
    )
    assert retracted.status_code == 200, retracted.text
    assert retracted.json()["state"] == "retracted"
    assert retracted.json()["retract_reason"] == "误标，辐射剂量单位混淆"

    again = client.post(
        f"/api/registry/annotations/{annotation_id}/retract",
        json={"actor": "reviewer-1", "reason": "重复撤回"},
    )
    assert again.status_code == 409

    detail = client.get(f"/api/registry/entries/{entry['id']}").json()["data"]
    states = {item["id"]: item["state"] for item in detail["annotations"]}
    assert states[annotation_id] == "retracted"
    assert states[review.json()["id"]] == "active"
    actions = [event["action"] for event in detail["events"]]
    assert actions == ["registered", "annotated", "annotated", "annotation_retracted"]

    summary_after = client.get("/api/registry/summary").json()["data"]
    assert summary_after["active_quality_labels"] == {}


def test_compare_by_experimental_conditions(client):
    low = register(client, "run-0030", dose=5.0, cycles=10, derated=False)
    high = register(client, "run-0031", dose=30.0, cycles=80)
    other_model = client.post(
        "/api/registry/entries",
        json={"metadata": {**run_metadata("run-0032"), "model_code": "other-model"}, "summary": {"score": 0.1}, "registered_by": "uploader-1"},
    )
    assert other_model.status_code == 201

    by_ids = client.get("/api/registry/compare", params={"ids": f"{low['id']},{high['id']}"})
    assert by_ids.status_code == 200, by_ids.text
    verify_signature(by_ids.json())
    compared = by_ids.json()["data"]
    assert compared["count"] == 2
    assert set(compared["differing_fields"]) == {"radiation_dose", "thermal_cycles", "derated"}

    by_condition = client.get(
        "/api/registry/compare",
        params={"model_code": "thermal-model", "radiation_dose_min": 10, "thermal_cycles_max": 100},
    )
    condition_entries = by_condition.json()["data"]["entries"]
    assert [item["id"] for item in condition_entries] == [high["id"]]

    filtered = client.get(
        "/api/registry/entries",
        params={"derated": "true", "radiation_dose_min": 20},
    )
    assert [item["id"] for item in filtered.json()["data"]["items"]] == [high["id"]]

    missing = client.get("/api/registry/compare", params={"ids": "99999"})
    assert missing.status_code == 404
    empty = client.get("/api/registry/compare")
    assert empty.status_code == 422


def test_concurrent_reviewers_get_deterministic_conflict(client):
    entry = register(client, "run-0040")
    stale = client.post(
        f"/api/registry/entries/{entry['id']}/publish",
        json={"actor": "reviewer-a", "expected_version": 99},
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["context"] == {"current_status": "registered", "current_version": 1}

    init_db()
    clock = FrozenClock(datetime(2026, 9, 26, 6, 0, tzinfo=UTC))
    service = RegistryService(get_connection(), clock, signing_key=SIGNING_KEY)
    first = service.publish(entry["id"], "reviewer-a", 1)
    assert first["status"] == "published" and first["version"] == 2
    with pytest.raises(ConflictError) as excinfo:
        service.publish(entry["id"], "reviewer-b", 1)
    assert excinfo.value.context == {"current_status": "published", "current_version": 2}
    with pytest.raises(ConflictError):
        service.retract(entry["id"], "reviewer-b", "并发撤回", 1)
    loser_view = service.get_entry(entry["id"])["data"]
    assert loser_view["entry"]["status"] == "published"
    assert [event["action"] for event in loser_view["events"]] == ["registered", "published"]


def test_signed_response_is_deterministic_with_frozen_clock(client):
    init_db()
    clock = FrozenClock(datetime(2026, 9, 26, 8, 0, tzinfo=UTC))
    service = RegistryService(get_connection(), clock, signing_key=SIGNING_KEY)
    entry, created = service.register(register_payload("run-0050"))
    assert created
    first = service.get_entry(entry["id"])
    second = service.get_entry(entry["id"])
    assert first == second
    verify_signature(first)
    clock.advance(seconds=60)
    later = service.get_entry(entry["id"])
    assert later["issued_at"] != first["issued_at"]
    assert later["digest"] != first["digest"]
    verify_signature(later)


def test_restart_preserves_state_and_dedup(client):
    entry = register(client, "run-0060")
    client.post(
        f"/api/registry/entries/{entry['id']}/annotations",
        json={"kind": "quality_label", "label": "suspect", "reviewer": "reviewer-1"},
    )
    client.post(
        f"/api/registry/entries/{entry['id']}/publish",
        json={"actor": "reviewer-1", "expected_version": 1},
    )

    close_connection()
    init_db()
    service = RegistryService(get_connection(), signing_key=SIGNING_KEY)
    restored = service.get_entry(entry["id"])["data"]
    assert restored["entry"]["status"] == "published"
    assert restored["entry"]["published_by"] == "reviewer-1"
    assert [item["label"] for item in restored["annotations"]] == ["suspect"]
    assert [event["action"] for event in restored["events"]] == ["registered", "annotated", "published"]

    again, created = service.register(register_payload("run-0060"))
    assert not created
    assert again["id"] == entry["id"]
    assert again["status"] == "published"
