from __future__ import annotations

import json
import os
import sqlite3
import uuid
from typing import Any

from app.core.clock import Clock, SystemClock, to_storage
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.database import get_connection, transaction
from app.registry.signing import build_envelope, canonical_json, content_digest, load_signing_key

SCHEMA = """
CREATE TABLE IF NOT EXISTS registry_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_uid TEXT NOT NULL UNIQUE,
    model_code TEXT NOT NULL,
    model_version TEXT NOT NULL,
    payload_code TEXT NOT NULL,
    payload_version TEXT NOT NULL,
    algorithm_version TEXT NOT NULL DEFAULT '',
    radiation_dose REAL,
    radiation_unit TEXT NOT NULL DEFAULT '',
    thermal_cycles INTEGER,
    thermal_profile TEXT NOT NULL DEFAULT '',
    derated INTEGER NOT NULL DEFAULT 0 CHECK(derated IN (0,1)),
    derating_reason TEXT NOT NULL DEFAULT '',
    conditions_json TEXT NOT NULL DEFAULT '{}',
    executed_at TEXT NOT NULL,
    submitted_by TEXT NOT NULL,
    source_refs_json TEXT NOT NULL,
    summary_json TEXT NOT NULL,
    metrics_json TEXT NOT NULL DEFAULT '{}',
    summary_digest TEXT NOT NULL,
    provenance_digest TEXT NOT NULL,
    fingerprint TEXT NOT NULL UNIQUE,
    run_version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_registry_runs_model ON registry_runs(model_code, model_version);
CREATE INDEX IF NOT EXISTS idx_registry_runs_payload ON registry_runs(payload_code, payload_version);
CREATE INDEX IF NOT EXISTS idx_registry_runs_derated ON registry_runs(derated);
CREATE INDEX IF NOT EXISTS idx_registry_runs_dose ON registry_runs(radiation_dose);

CREATE TABLE IF NOT EXISTS registry_quality_labels (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES registry_runs(id) ON DELETE RESTRICT,
    label TEXT NOT NULL CHECK(label IN ('gold','silver','bronze','quarantined','untrusted')),
    note TEXT NOT NULL DEFAULT '',
    set_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_registry_quality_run ON registry_quality_labels(run_id, id);

CREATE TABLE IF NOT EXISTS registry_reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES registry_runs(id) ON DELETE RESTRICT,
    revision INTEGER NOT NULL,
    verdict TEXT NOT NULL CHECK(verdict IN ('approved','changes_requested','rejected')),
    comment TEXT NOT NULL,
    reviewer TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(run_id, revision)
);
CREATE INDEX IF NOT EXISTS idx_registry_reviews_run ON registry_reviews(run_id, revision);

CREATE TABLE IF NOT EXISTS registry_publications (
    run_id INTEGER PRIMARY KEY REFERENCES registry_runs(id) ON DELETE RESTRICT,
    status TEXT NOT NULL CHECK(status IN ('published','withdrawn')),
    sequence INTEGER NOT NULL DEFAULT 1,
    published_at TEXT NOT NULL,
    withdrawn_at TEXT,
    withdraw_reason TEXT NOT NULL DEFAULT '',
    last_actor TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS registry_publication_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES registry_runs(id) ON DELETE RESTRICT,
    sequence INTEGER NOT NULL,
    action TEXT NOT NULL CHECK(action IN ('publish','withdraw')),
    actor TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_registry_pub_events_run ON registry_publication_events(run_id, id);
"""

RUN_SELECT = (
    "SELECT r.*, "
    "(SELECT label FROM registry_quality_labels q WHERE q.run_id=r.id ORDER BY q.id DESC LIMIT 1) AS current_label, "
    "(SELECT status FROM registry_publications p WHERE p.run_id=r.id) AS publish_status, "
    "(SELECT verdict FROM registry_reviews v WHERE v.run_id=r.id ORDER BY v.revision DESC, v.id DESC LIMIT 1) AS latest_verdict, "
    "(SELECT COUNT(*) FROM registry_reviews v WHERE v.run_id=r.id) AS review_count "
    "FROM registry_runs r"
)

COMPARE_DIMENSIONS = ("radiation_dose", "thermal_cycles", "payload", "model", "derated")


def ensure_schema() -> None:
    get_connection().executescript(SCHEMA)


class ResultRegistryService:
    """接收运行元数据与结果摘要，维护版本、质量标签、复核意见与发布状态。"""

    def __init__(
        self,
        connection: sqlite3.Connection | None = None,
        clock: Clock | None = None,
        signing_key: str | None = None,
    ) -> None:
        self.connection = connection or get_connection()
        self.clock = clock or SystemClock()
        explicit_key = signing_key if signing_key is not None else os.getenv("RESULT_REGISTRY_SIGNING_KEY", "")
        self.signing_key = load_signing_key(signing_key)
        self.key_id = "hmac-sha256:env" if explicit_key else "hmac-sha256:dev-insecure"
        ensure_schema()

    # ------------------------------------------------------------------ 登记
    def register_run(self, payload: dict[str, Any]) -> dict[str, Any]:
        conditions = self._normalize_conditions(payload.get("conditions") or {})
        source_refs = self._validate_source_refs(payload.get("source_refs"))
        summary = payload.get("summary")
        if not isinstance(summary, dict) or not summary:
            raise ValidationError("结果摘要必须是非空对象，登记簿不保存完整结果内容")
        metrics = payload.get("metrics") or {}
        if not isinstance(metrics, dict):
            raise ValidationError("指标摘要必须是对象")

        provenance = {
            "model": {"code": payload["model_code"], "version": payload["model_version"], "algorithm_version": payload.get("algorithm_version", "")},
            "payload": {"code": payload["payload_code"], "version": payload["payload_version"]},
            "conditions": conditions,
            "source_refs": source_refs,
            "summary": summary,
            "metrics": metrics,
            "executed_at": payload["executed_at"],
            "submitted_by": payload["submitted_by"],
        }
        fingerprint = content_digest(provenance)
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            existing = connection.execute("SELECT id FROM registry_runs WHERE fingerprint=?", (fingerprint,)).fetchone()
            if existing is not None:
                presented = self._present_full(connection.execute(RUN_SELECT + " WHERE r.id=?", (existing["id"],)).fetchone(), connection)
                presented["deduplicated"] = True
                return presented
            cursor = connection.execute(
                "INSERT INTO registry_runs(run_uid,model_code,model_version,payload_code,payload_version,algorithm_version,"
                "radiation_dose,radiation_unit,thermal_cycles,thermal_profile,derated,derating_reason,conditions_json,"
                "executed_at,submitted_by,source_refs_json,summary_json,metrics_json,summary_digest,provenance_digest,"
                "fingerprint,run_version,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?)",
                (
                    "run-" + uuid.uuid4().hex,
                    payload["model_code"], payload["model_version"], payload["payload_code"], payload["payload_version"],
                    payload.get("algorithm_version", ""),
                    conditions.get("radiation_dose"), conditions.get("radiation_unit", ""),
                    conditions.get("thermal_cycles"), conditions.get("thermal_profile", ""),
                    1 if conditions.get("derated") else 0, conditions.get("derating_reason", ""),
                    canonical_json(conditions),
                    payload["executed_at"], payload["submitted_by"],
                    canonical_json(source_refs), canonical_json(summary), canonical_json(metrics),
                    content_digest({"summary": summary, "metrics": metrics}),
                    content_digest({k: v for k, v in provenance.items() if k != "summary" and k != "metrics"}),
                    fingerprint, now,
                ),
            )
            row = connection.execute(RUN_SELECT + " WHERE r.id=?", (cursor.lastrowid,)).fetchone()
            presented = self._present_full(row, connection)
            presented["deduplicated"] = False
            return presented

    # ------------------------------------------------------------------ 查询
    def get_run(self, run_uid: str) -> dict[str, Any]:
        with transaction() as connection:
            row = connection.execute(RUN_SELECT + " WHERE r.run_uid=?", (run_uid,)).fetchone()
            if row is None:
                raise NotFoundError("登记簿中不存在该运行")
            return self._present_full(row, connection)

    def query_runs(
        self,
        *,
        model_code: str | None = None,
        payload_code: str | None = None,
        derated: bool | None = None,
        label: str | None = None,
        status: str | None = None,
        published_only: bool = False,
        limit: int = 100,
    ) -> dict[str, Any]:
        clauses: list[str] = []
        values: list[Any] = []
        if model_code:
            clauses.append("sr.model_code=?")
            values.append(model_code)
        if payload_code:
            clauses.append("sr.payload_code=?")
            values.append(payload_code)
        if derated is not None:
            clauses.append("sr.derated=?")
            values.append(1 if derated else 0)
        if label:
            clauses.append("sr.current_label=?")
            values.append(label)
        if published_only:
            # 只返回当前仍处于发布状态的运行；已撤回行的状态是 withdrawn，天然被排除。
            clauses.append("sr.publish_status='published'")
        elif status:
            clauses.append("sr.publish_status=?")
            values.append(status)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        values.append(max(1, min(limit, 500)))
        rows = self.connection.execute(
            "SELECT * FROM (" + RUN_SELECT + ") AS sr" + where + " ORDER BY sr.id LIMIT ?", values
        ).fetchall()
        items = [self._present_summary(row) for row in rows]
        return self.seal({"kind": "runs", "count": len(items), "items": items})

    def compare(self, by: str, *, model_code: str | None = None, payload_code: str | None = None) -> dict[str, Any]:
        if by not in COMPARE_DIMENSIONS:
            raise ValidationError("不支持的比较维度", context={"allowed": list(COMPARE_DIMENSIONS)})
        clauses: list[str] = []
        values: list[Any] = []
        if model_code:
            clauses.append("r.model_code=?")
            values.append(model_code)
        if payload_code:
            clauses.append("r.payload_code=?")
            values.append(payload_code)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = self.connection.execute(RUN_SELECT + where + " ORDER BY r.id", values).fetchall()

        groups: dict[str, list[sqlite3.Row]] = {}
        for row in rows:
            key = self._group_key(row, by)
            if key is None:
                continue
            groups.setdefault(str(key), []).append(row)
        payload_groups = [
            {"condition": json.loads(name) if name.startswith("{") else name, "count": len(group_rows),
             "runs": [self._present_summary(item) for item in group_rows]}
            for name, group_rows in sorted(groups.items())
        ]
        return self.seal({"kind": "comparison", "compared_by": by, "group_count": len(payload_groups), "groups": payload_groups})

    # ------------------------------------------------------------------ 标签/复核
    def add_quality_label(self, run_uid: str, label: str, note: str, actor: str) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            run = self._require_run(connection, run_uid)
            connection.execute(
                "INSERT INTO registry_quality_labels(run_id,label,note,set_by,created_at) VALUES(?,?,?,?,?)",
                (run["id"], label, note, actor, now),
            )
            return self._present_full(
                connection.execute(RUN_SELECT + " WHERE r.id=?", (run["id"],)).fetchone(), connection
            )

    def add_review(self, run_uid: str, verdict: str, comment: str, reviewer: str) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            run = self._require_run(connection, run_uid)
            revision = int(connection.execute(
                "SELECT COALESCE(MAX(revision),0)+1 FROM registry_reviews WHERE run_id=?", (run["id"],)
            ).fetchone()[0])
            connection.execute(
                "INSERT INTO registry_reviews(run_id,revision,verdict,comment,reviewer,created_at) VALUES(?,?,?,?,?,?)",
                (run["id"], revision, verdict, comment, reviewer, now),
            )
            return self._present_full(
                connection.execute(RUN_SELECT + " WHERE r.id=?", (run["id"],)).fetchone(), connection
            )

    # ------------------------------------------------------------------ 发布/撤回
    def publish(self, run_uid: str, actor: str) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            run = self._require_run(connection, run_uid)
            publication = connection.execute("SELECT * FROM registry_publications WHERE run_id=?", (run["id"],)).fetchone()
            if publication is not None and publication["status"] == "published":
                raise ConflictError("该运行结果已经处于发布状态")
            if publication is not None and publication["status"] == "withdrawn":
                # 撤回是终态：强制登记修正后的新运行，避免旧结果被再次当作已发布。
                raise ConflictError("已撤回的结果不能重新发布，请登记修正后的新运行")
            connection.execute(
                "INSERT INTO registry_publications(run_id,status,sequence,published_at,last_actor,created_at,updated_at) "
                "VALUES(?,'published',1,?,?,?,?)",
                (run["id"], now, actor, now, now),
            )
            connection.execute(
                "INSERT INTO registry_publication_events(run_id,sequence,action,actor,reason,created_at) VALUES(?,1,'publish',?,'',?)",
                (run["id"], actor, now),
            )
            return self._present_full(connection.execute(RUN_SELECT + " WHERE r.id=?", (run["id"],)).fetchone(), connection)

    def withdraw(self, run_uid: str, reason: str, actor: str) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            run = self._require_run(connection, run_uid)
            publication = connection.execute("SELECT * FROM registry_publications WHERE run_id=?", (run["id"],)).fetchone()
            if publication is None:
                raise ConflictError("该运行结果尚未发布，无需撤回")
            if publication["status"] == "withdrawn":
                raise ConflictError("该运行结果已经撤回")
            sequence = int(publication["sequence"]) + 1
            connection.execute(
                "UPDATE registry_publications SET status='withdrawn',sequence=?,withdrawn_at=?,withdraw_reason=?,"
                "last_actor=?,updated_at=? WHERE run_id=?",
                (sequence, now, reason, actor, now, run["id"]),
            )
            connection.execute(
                "INSERT INTO registry_publication_events(run_id,sequence,action,actor,reason,created_at) VALUES(?,?,'withdraw',?,?,?)",
                (run["id"], sequence, actor, reason, now),
            )
            return self._present_full(connection.execute(RUN_SELECT + " WHERE r.id=?", (run["id"],)).fetchone(), connection)

    # ------------------------------------------------------------------ 签名
    def seal(self, payload: dict[str, Any]) -> dict[str, Any]:
        return build_envelope(
            payload,
            key=self.signing_key,
            key_id=self.key_id,
            generated_at=to_storage(self.clock.now()),
        )

    # ------------------------------------------------------------------ 内部
    @staticmethod
    def _require_run(connection: sqlite3.Connection, run_uid: str) -> sqlite3.Row:
        run = connection.execute("SELECT * FROM registry_runs WHERE run_uid=?", (run_uid,)).fetchone()
        if run is None:
            raise NotFoundError("登记簿中不存在该运行")
        return run

    @staticmethod
    def _group_key(row: sqlite3.Row, by: str) -> str | None:
        if by == "radiation_dose":
            value = row["radiation_dose"]
            return None if value is None else f"{float(value):.6g}"
        if by == "thermal_cycles":
            value = row["thermal_cycles"]
            return None if value is None else str(int(value))
        if by == "payload":
            return canonical_json({"payload_code": row["payload_code"], "payload_version": row["payload_version"]})
        if by == "model":
            return canonical_json({"model_code": row["model_code"], "model_version": row["model_version"]})
        if by == "derated":
            return "derated" if row["derated"] else "nominal"
        return None

    @staticmethod
    def _present_conditions(row: sqlite3.Row) -> dict[str, Any]:
        conditions = dict(json.loads(row["conditions_json"]))
        # 独立列是规范化后的权威值，覆盖回条件对象以保证查询结果与存储一致。
        conditions.update(
            {
                "radiation_dose": row["radiation_dose"],
                "radiation_unit": row["radiation_unit"],
                "thermal_cycles": row["thermal_cycles"],
                "thermal_profile": row["thermal_profile"],
                "derated": bool(row["derated"]),
                "derating_reason": row["derating_reason"],
            }
        )
        return conditions

    def _present_summary(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "run_uid": row["run_uid"],
            "model": {"code": row["model_code"], "version": row["model_version"], "algorithm_version": row["algorithm_version"]},
            "payload": {"code": row["payload_code"], "version": row["payload_version"]},
            "conditions": self._present_conditions(row),
            "summary": json.loads(row["summary_json"]),
            "metrics": json.loads(row["metrics_json"]),
            "summary_digest": row["summary_digest"],
            "fingerprint": row["fingerprint"],
            "version": row["run_version"],
            "current_label": row["current_label"],
            "publish_status": row["publish_status"],
            "latest_review_verdict": row["latest_verdict"],
            "review_count": row["review_count"],
            "executed_at": row["executed_at"],
            "submitted_by": row["submitted_by"],
            "created_at": row["created_at"],
        }

    def _present_full(self, row: sqlite3.Row, connection: sqlite3.Connection) -> dict[str, Any]:
        result = self._present_summary(row)
        result["source_refs"] = json.loads(row["source_refs_json"])
        result["provenance_digest"] = row["provenance_digest"]
        result["quality_labels"] = [
            {"label": item["label"], "note": item["note"], "set_by": item["set_by"], "created_at": item["created_at"]}
            for item in connection.execute("SELECT * FROM registry_quality_labels WHERE run_id=? ORDER BY id", (row["id"],)).fetchall()
        ]
        result["reviews"] = [
            {"revision": item["revision"], "verdict": item["verdict"], "comment": item["comment"], "reviewer": item["reviewer"], "created_at": item["created_at"]}
            for item in connection.execute("SELECT * FROM registry_reviews WHERE run_id=? ORDER BY revision", (row["id"],)).fetchall()
        ]
        publication = connection.execute("SELECT * FROM registry_publications WHERE run_id=?", (row["id"],)).fetchone()
        result["publication"] = None if publication is None else {
            "status": publication["status"],
            "sequence": publication["sequence"],
            "published_at": publication["published_at"],
            "withdrawn_at": publication["withdrawn_at"],
            "withdraw_reason": publication["withdraw_reason"],
            "last_actor": publication["last_actor"],
            "updated_at": publication["updated_at"],
        }
        result["publication_events"] = [
            {"sequence": item["sequence"], "action": item["action"], "actor": item["actor"], "reason": item["reason"], "created_at": item["created_at"]}
            for item in connection.execute("SELECT * FROM registry_publication_events WHERE run_id=? ORDER BY id", (row["id"],)).fetchall()
        ]
        return result

    @staticmethod
    def _validate_source_refs(value: Any) -> list[str]:
        if not isinstance(value, list) or not value:
            raise ValidationError("来源链至少包含一个来源引用（载荷、载荷标识或上游运行）")
        refs = [item for item in value if isinstance(item, str) and item.strip()]
        if len(refs) != len(value):
            raise ValidationError("来源引用必须是非空字符串")
        return list(dict.fromkeys(refs))

    @staticmethod
    def _normalize_conditions(raw: Any) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise ValidationError("实验条件必须是对象")
        conditions = dict(raw)
        dose = conditions.get("radiation_dose")
        if dose is not None and (isinstance(dose, bool) or not isinstance(dose, (int, float))):
            raise ValidationError("辐射剂量必须是数值")
        cycles = conditions.get("thermal_cycles")
        if cycles is not None and (isinstance(cycles, bool) or not isinstance(cycles, int) or cycles < 0):
            raise ValidationError("热循环次数必须是非负整数")
        if not isinstance(conditions.get("derated", False), bool):
            raise ValidationError("降额标记必须是布尔值")
        return conditions
