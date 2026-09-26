from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import pytest

from app.database import close_connection
from app.registry.service import ResultRegistryService
from app.registry.signing import verify_envelope
from app.core.clock import FrozenClock
from datetime import UTC, datetime


def run_payload(**overrides):
    payload = {
        "model_code": "rad-hard-net",
        "model_version": "3.2.1",
        "payload_code": "thermal-calib",
        "payload_version": "2026.09",
        "algorithm_version": "infer-7",
        "conditions": {
            "radiation_dose": 50.0,
            "radiation_unit": "krad",
            "thermal_cycles": 120,
            "thermal_profile": "-40C~+85C",
            "derated": False,
        },
        "executed_at": "2026-09-25T08:30:00+00:00",
        "submitted_by": "payload-pipeline",
        "source_refs": ["payload://thermal-calib/2026.09", "run://upstream-9981"],
        "summary": {"drift_ppm": 12.4, "pass": True},
        "metrics": {"seconds": 38.2},
    }
    payload.update(overrides)
    return payload


@pytest.fixture(autouse=True)
def _isolate_signing_env():
    os.environ.pop("RESULT_REGISTRY_SIGNING_KEY", None)
    yield
    os.environ.pop("RESULT_REGISTRY_SIGNING_KEY", None)


@pytest.fixture()
def service(tmp_path: Path):
    close_connection()
    os.environ["TOWNSHIP_DATABASE_PATH"] = str(tmp_path / "registry.db")
    instance = ResultRegistryService(clock=FrozenClock(datetime(2026, 9, 26, 10, 0, tzinfo=UTC)))
    yield instance
    close_connection()


def register(client, **overrides):
    return client.post("/api/registry/runs", json=run_payload(**overrides))


# --------------------------------------------------------------------- 登记/来源链
def test_register_persists_summary_and_provenance_chain(client):
    response = register(client)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["run_uid"].startswith("run-")
    assert body["version"] == 1
    assert body["summary"] == {"drift_ppm": 12.4, "pass": True}
    assert body["metrics"] == {"seconds": 38.2}
    assert body["fingerprint"]
    assert body["summary_digest"]
    assert body["provenance_digest"]
    # 来源链不能断：完整记录必须保留上游引用。
    assert body["source_refs"] == ["payload://thermal-calib/2026.09", "run://upstream-9981"]
    assert body["publish_status"] is None
    assert body["publication"] is None


def test_source_chain_is_required(client):
    response = register(client, source_refs=[])
    assert response.status_code == 422
    missing = run_payload()
    missing.pop("source_refs")
    assert client.post("/api/registry/runs", json=missing).status_code == 422


def test_empty_summary_rejected(client):
    assert register(client, summary={}).status_code == 422


# --------------------------------------------------------------------- 不可变/重复上传
def test_duplicate_upload_returns_original_version(client):
    first = register(client)
    second = register(client)
    assert first.status_code == 201
    assert second.status_code == 200  # 幂等命中，区别于新建
    a, b = first.json(), second.json()
    assert a["run_uid"] == b["run_uid"]
    assert a["fingerprint"] == b["fingerprint"]
    assert a["version"] == b["version"] == 1


def test_changed_conditions_or_summary_creates_new_version_identity(client):
    first = register(client).json()
    different_condition = register(client, conditions={**run_payload()["conditions"], "radiation_dose": 80.0}).json()
    different_summary = register(client, summary={"drift_ppm": 99.0, "pass": False}).json()
    assert different_condition["run_uid"] != first["run_uid"]
    assert different_summary["run_uid"] != first["run_uid"]
    listing = client.get("/api/registry/runs").json()["payload"]["items"]
    assert len(listing) == 3
    # 已登记记录的版本号与摘要保持不可变。
    assert all(item["version"] == 1 for item in listing)


# --------------------------------------------------------------------- 标签/复核/发布
def test_quality_labels_reviews_and_publish_lifecycle(client):
    uid = register(client).json()["run_uid"]
    label = client.post(f"/api/registry/runs/{uid}/quality-labels", json={"label": "gold", "note": "重复性好", "actor": "qa-lead"})
    assert label.status_code == 200
    assert label.json()["current_label"] == "gold"
    review = client.post(f"/api/registry/runs/{uid}/reviews", json={"verdict": "approved", "comment": "可发布", "reviewer": "scientist-a"})
    assert review.status_code == 200
    assert review.json()["reviews"][0]["revision"] == 1
    published = client.post(f"/api/registry/runs/{uid}/publish", json={"actor": "publisher"})
    assert published.status_code == 200
    assert published.json()["publish_status"] == "published"
    # 重复发布被拒绝。
    assert client.post(f"/api/registry/runs/{uid}/publish", json={"actor": "publisher"}).status_code == 409


def test_withdraw_blocks_rediscovery_as_published_and_is_terminal(client):
    uid = register(client).json()["run_uid"]
    client.post(f"/api/registry/runs/{uid}/publish", json={"actor": "publisher"})
    withdrawn = client.post(f"/api/registry/runs/{uid}/withdraw", json={"reason": "标注错误", "actor": "publisher"})
    assert withdrawn.status_code == 200
    assert withdrawn.json()["publish_status"] == "withdrawn"

    # 撤回后不能被新的查询误认为已发布。
    published_items = client.get("/api/registry/runs?published_only=true").json()["payload"]["items"]
    assert published_items == []
    by_status = client.get("/api/registry/runs?status=published").json()["payload"]["items"]
    assert by_status == []
    withdrawn_items = client.get("/api/registry/runs?status=withdrawn").json()["payload"]["items"]
    assert len(withdrawn_items) == 1 and withdrawn_items[0]["run_uid"] == uid

    # 撤回是终态：不能重新发布、不能重复撤回。
    assert client.post(f"/api/registry/runs/{uid}/publish", json={"actor": "publisher"}).status_code == 409
    assert client.post(f"/api/registry/runs/{uid}/withdraw", json={"reason": "again", "actor": "publisher"}).status_code == 409


def test_withdraw_requires_publication(client):
    uid = register(client).json()["run_uid"]
    assert client.post(f"/api/registry/runs/{uid}/withdraw", json={"reason": "x", "actor": "p"}).status_code == 409


# --------------------------------------------------------------------- 条件比较
def test_compare_groups_by_experiment_conditions(client):
    register(client, conditions={**run_payload()["conditions"], "radiation_dose": 30.0})
    register(client, conditions={**run_payload()["conditions"], "radiation_dose": 30.0, "thermal_cycles": 200})
    register(client, conditions={**run_payload()["conditions"], "radiation_dose": 90.0, "derated": True, "derating_reason": "高剂量降额"})

    envelope = client.get("/api/registry/compare?by=radiation_dose").json()
    groups = envelope["payload"]["groups"]
    by_dose = {g["condition"]: g["count"] for g in groups}
    assert by_dose == {"30": 2, "90": 1}
    doses = sorted(float(g["condition"]) for g in groups)
    assert doses == [30.0, 90.0]

    derated = client.get("/api/registry/compare?by=derated").json()["payload"]["groups"]
    by_name = {g["condition"]: g["count"] for g in derated}
    assert by_name == {"nominal": 2, "derated": 1}


def test_compare_invalid_dimension(client):
    assert client.get("/api/registry/compare?by=unknown").status_code == 422


def test_filter_by_payload_and_label(client):
    uid = register(client).json()["run_uid"]
    client.post(f"/api/registry/runs/{uid}/quality-labels", json={"label": "silver", "note": "", "actor": "qa"})
    items = client.get("/api/registry/runs?payload_code=thermal-calib&label=silver").json()["payload"]["items"]
    assert len(items) == 1 and items[0]["run_uid"] == uid
    assert client.get("/api/registry/runs?label=gold").json()["payload"]["items"] == []


# --------------------------------------------------------------------- 签名响应
def test_query_response_is_signed_and_verifies(client):
    register(client)
    envelope = client.get("/api/registry/runs").json()
    assert envelope["alg"] == "HS256"
    assert envelope["key_id"] == "hmac-sha256:dev-insecure"
    assert envelope["signature"]
    verified = client.post("/api/registry/verify-signature", json=envelope)
    assert verified.status_code == 200
    assert verified.json()["valid"] is True

    # 篡改任何查询结果都会导致验签失败。
    tampered = json.loads(json.dumps(envelope))
    tampered["payload"]["items"][0]["summary"]["drift_ppm"] = 0.0
    bad = client.post("/api/registry/verify-signature", json=tampered).json()
    assert bad["valid"] is False


def test_signed_response_with_configured_key(tmp_path):
    close_connection()
    os.environ["TOWNSHIP_DATABASE_PATH"] = str(tmp_path / "k.db")
    os.environ["RESULT_REGISTRY_SIGNING_KEY"] = "team-secret"
    svc = ResultRegistryService()
    svc.register_run(run_payload())
    envelope = svc.query_runs()
    assert envelope["key_id"] == "hmac-sha256:env"
    assert verify_envelope(envelope, b"team-secret")["count"] == 1
    with pytest.raises(Exception):
        verify_envelope(envelope, b"wrong-key")
    close_connection()
    os.environ.pop("RESULT_REGISTRY_SIGNING_KEY", None)


# --------------------------------------------------------------------- 重启持久化
def test_state_survives_connection_restart(service, tmp_path):
    created = service.register_run(run_payload())
    uid = created["run_uid"]
    service.publish(uid, "publisher")
    close_connection()  # 模拟进程重启：丢弃线程内连接

    revived = ResultRegistryService(clock=FrozenClock(datetime(2026, 9, 26, 11, 0, tzinfo=UTC)))
    fetched = revived.get_run(uid)
    assert fetched["fingerprint"] == created["fingerprint"]
    assert fetched["publish_status"] == "published"
    assert fetched["source_refs"] == created["source_refs"]
    assert revived.query_runs(published_only=True)["payload"]["count"] == 1


# --------------------------------------------------------------------- 并发确定性
def test_concurrent_identical_registration_creates_single_run(service):
    results: list[dict] = []
    errors: list[Exception] = []
    barrier = threading.Barrier(8)

    def worker():
        local = ResultRegistryService()
        barrier.wait()
        try:
            results.append(local.register_run(run_payload()))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert len({item["run_uid"] for item in results}) == 1
    assert len({item["fingerprint"] for item in results}) == 1
    assert service.connection.execute("SELECT COUNT(*) FROM registry_runs").fetchone()[0] == 1


def test_concurrent_publish_has_single_winner(service):
    uid = service.register_run(run_payload())["run_uid"]
    outcomes: list[str] = []
    barrier = threading.Barrier(6)

    def worker():
        local = ResultRegistryService()
        barrier.wait()
        try:
            local.publish(uid, f"publisher-{threading.get_ident()}")
            outcomes.append("published")
        except Exception:  # noqa: BLE001
            outcomes.append("conflict")

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert outcomes.count("published") == 1
    assert outcomes.count("conflict") == 5


def test_concurrent_reviews_get_dense_ordered_revisions(service):
    uid = service.register_run(run_payload())["run_uid"]
    barrier = threading.Barrier(5)
    errors: list[Exception] = []

    def worker(index: int):
        local = ResultRegistryService()
        barrier.wait()
        try:
            local.add_review(uid, "approved", f"意见 {index}", f"reviewer-{index}")
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    revisions = [
        row[0]
        for row in service.connection.execute(
            "SELECT revision FROM registry_reviews WHERE run_id=(SELECT id FROM registry_runs WHERE run_uid=?) ORDER BY revision",
            (uid,),
        ).fetchall()
    ]
    assert revisions == [1, 2, 3, 4, 5]
