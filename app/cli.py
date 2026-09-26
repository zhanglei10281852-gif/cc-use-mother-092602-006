from __future__ import annotations

import argparse
import json

from fastapi.testclient import TestClient

from app.database import database_path, get_connection, init_db
from app.main import app


def command_init() -> int:
    init_db()
    print(json.dumps({"database": str(database_path()), "status": "initialized"}, ensure_ascii=False))
    return 0


def command_check() -> int:
    init_db()
    connection = get_connection()
    result = {
        "database": str(database_path()),
        "integrity": connection.execute("PRAGMA integrity_check").fetchone()[0],
        "foreign_keys": connection.execute("PRAGMA foreign_keys").fetchone()[0],
        "journal_mode": connection.execute("PRAGMA journal_mode").fetchone()[0],
        "tables": connection.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0],
    }
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["integrity"] == "ok" and result["foreign_keys"] == 1 else 1


def command_smoke() -> int:
    with TestClient(app) as client:
        root = client.get("/")
        health = client.get("/api/system/health")
    result = {"root": root.json(), "health": health.json(), "status_codes": [root.status_code, health.status_code]}
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["status_codes"] == [200, 200] else 1


def command_compute_demo() -> int:
    template = {
        "code": "monte-carlo-demo",
        "name": "蒙特卡洛演示",
        "algorithm": "monte-carlo",
        "parameter_schema": {
            "samples": {"type": "integer", "required": True, "minimum": 10, "maximum": 1000000},
            "seed": {"type": "integer", "required": True},
        },
        "default_parameters": {},
        "max_runtime_seconds": 60,
        "max_attempts": 3,
    }
    with TestClient(app) as client:
        created = client.post("/api/compute/templates?actor=cli-demo", json=template)
        if created.status_code not in {201, 409}:
            print(created.text)
            return 1
        task = client.post(
            "/api/compute/tasks",
            json={
                "template_code": "monte-carlo-demo",
                "project_code": "demo",
                "requested_by": "cli-user",
                "parameters": {"samples": 1000, "seed": 42},
                "priority": 80,
                "idempotency_key": "compute-demo-000001",
            },
        )
        claimed = client.post(
            "/api/compute/tasks/claim",
            json={"worker_id": "cli-worker", "capabilities": ["monte-carlo"], "lease_seconds": 60},
        )
    result = {"task": task.status_code, "claimed": claimed.status_code, "task_id": task.json().get("id")}
    print(json.dumps(result, ensure_ascii=False))
    return 0 if task.status_code == 202 and claimed.status_code == 200 and claimed.json().get("task") else 1


def command_registry_demo() -> int:
    metadata = {
        "payload_id": "payload-01",
        "run_id": "run-20260926-001",
        "model_code": "thermal-model",
        "parameter_version": "v3",
        "derated": True,
        "radiation_dose": 12.5,
        "thermal_cycles": 40,
        "environment": {"orbit": "LEO"},
    }
    with TestClient(app) as client:
        created = client.post(
            "/api/registry/entries",
            json={"metadata": metadata, "summary": {"score": 0.98, "anomalies": 0}, "registered_by": "cli-user"},
        )
        if created.status_code not in {200, 201}:
            print(created.text)
            return 1
        entry = created.json()["entry"]
        if entry["status"] == "registered":
            published = client.post(
                f"/api/registry/entries/{entry['id']}/publish",
                json={"actor": "cli-reviewer", "expected_version": entry["version"], "note": "登记簿演示发布"},
            )
            if published.status_code != 200:
                print(published.text)
                return 1
        detail = client.get(f"/api/registry/entries/{entry['id']}")
    body = detail.json()
    result = {
        "register": created.status_code,
        "entry_id": entry["id"],
        "status": body["data"]["entry"]["status"],
        "signature": bool(body.get("signature")),
    }
    print(json.dumps(result, ensure_ascii=False))
    return 0 if created.status_code in {200, 201} and result["status"] == "published" and result["signature"] else 1


def main() -> int:
    parser = argparse.ArgumentParser(prog="compute-operations", description="科学计算任务运营服务维护入口")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("init-db", help="初始化 SQLite 数据库")
    subparsers.add_parser("check-db", help="检查数据库完整性")
    subparsers.add_parser("smoke", help="执行本地 API 冒烟检查")
    subparsers.add_parser("compute-demo", help="执行计算任务提交与领取演示")
    subparsers.add_parser("registry-demo", help="执行结果登记、发布与签名查询演示")
    args = parser.parse_args()
    commands = {
        "init-db": command_init,
        "check-db": command_check,
        "smoke": command_smoke,
        "compute-demo": command_compute_demo,
        "registry-demo": command_registry_demo,
    }
    return commands[args.command]()


if __name__ == "__main__":
    raise SystemExit(main())
