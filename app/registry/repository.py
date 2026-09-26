from __future__ import annotations

import json
import sqlite3
from typing import Any


def _entry_from_row(row: sqlite3.Row) -> dict[str, Any]:
    entry = dict(row)
    entry["derated"] = bool(entry["derated"])
    entry["environment"] = json.loads(entry.pop("environment_json"))
    entry["summary"] = json.loads(entry.pop("summary_json"))
    return entry


class RegistryRepository:
    """封装结果登记簿领域的 SQLite 读写。"""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def entry_by_id(self, entry_id: int) -> dict[str, Any] | None:
        row = self.connection.execute("SELECT * FROM registry_entries WHERE id=?", (entry_id,)).fetchone()
        return _entry_from_row(row) if row is not None else None

    def entry_by_run(self, payload_id: str, run_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM registry_entries WHERE payload_id=? AND run_id=?",
            (payload_id, run_id),
        ).fetchone()
        return _entry_from_row(row) if row is not None else None

    def create_entry(self, *, metadata: dict[str, Any], summary: dict[str, Any], metadata_digest: str, summary_digest: str, registered_by: str, now: str) -> dict[str, Any]:
        cursor = self.connection.execute(
            "INSERT INTO registry_entries(payload_id,run_id,model_code,parameter_version,derated,radiation_dose,thermal_cycles,environment_json,summary_json,metadata_digest,summary_digest,registered_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                metadata["payload_id"],
                metadata["run_id"],
                metadata["model_code"],
                metadata["parameter_version"],
                1 if metadata["derated"] else 0,
                metadata.get("radiation_dose"),
                metadata.get("thermal_cycles"),
                json.dumps(metadata.get("environment") or {}, ensure_ascii=False, sort_keys=True),
                json.dumps(summary, ensure_ascii=False, sort_keys=True),
                metadata_digest,
                summary_digest,
                registered_by,
                now,
                now,
            ),
        )
        return self.entry_by_id(cursor.lastrowid)

    def list_entries(self, *, filters: dict[str, Any], limit: int) -> list[dict[str, Any]]:
        clauses, values = self._condition_clauses(filters)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        values.append(limit)
        rows = self.connection.execute(
            "SELECT * FROM registry_entries" + where + " ORDER BY created_at DESC, id DESC LIMIT ?",
            values,
        ).fetchall()
        return [_entry_from_row(row) for row in rows]

    def entries_by_ids(self, entry_ids: list[int]) -> list[dict[str, Any]]:
        placeholders = ",".join("?" for _ in entry_ids)
        rows = self.connection.execute(
            f"SELECT * FROM registry_entries WHERE id IN ({placeholders})",
            list(entry_ids),
        ).fetchall()
        found = {int(row["id"]): _entry_from_row(row) for row in rows}
        return [found[entry_id] for entry_id in entry_ids if entry_id in found]

    def count_by_status(self) -> dict[str, int]:
        rows = self.connection.execute("SELECT status,COUNT(*) AS amount FROM registry_entries GROUP BY status ORDER BY status").fetchall()
        return {str(row["status"]): int(row["amount"]) for row in rows}

    def count_active_labels(self) -> dict[str, int]:
        rows = self.connection.execute(
            "SELECT label,COUNT(*) AS amount FROM registry_annotations WHERE kind='quality_label' AND state='active' GROUP BY label ORDER BY label"
        ).fetchall()
        return {str(row["label"]): int(row["amount"]) for row in rows}

    def annotation_by_id(self, annotation_id: int) -> dict[str, Any] | None:
        row = self.connection.execute("SELECT * FROM registry_annotations WHERE id=?", (annotation_id,)).fetchone()
        return dict(row) if row is not None else None

    def annotations(self, entry_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM registry_annotations WHERE entry_id=? ORDER BY id",
            (entry_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def add_annotation(self, *, entry_id: int, kind: str, label: str, comment: str, reviewer: str, now: str) -> dict[str, Any]:
        cursor = self.connection.execute(
            "INSERT INTO registry_annotations(entry_id,kind,label,comment,reviewer,created_at) VALUES(?,?,?,?,?,?)",
            (entry_id, kind, label, comment, reviewer, now),
        )
        return self.annotation_by_id(cursor.lastrowid)

    def events(self, entry_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM registry_events WHERE entry_id=? ORDER BY id",
            (entry_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def add_event(self, *, entry_id: int, action: str, actor: str, detail: dict[str, Any], now: str) -> None:
        self.connection.execute(
            "INSERT INTO registry_events(entry_id,action,actor,detail_json,created_at) VALUES(?,?,?,?,?)",
            (entry_id, action, actor, json.dumps(detail, ensure_ascii=False, sort_keys=True), now),
        )

    @staticmethod
    def _condition_clauses(filters: dict[str, Any]) -> tuple[list[str], list[Any]]:
        clauses: list[str] = []
        values: list[Any] = []
        mapping = {
            "status": ("status=?", filters.get("status")),
            "payload_id": ("payload_id=?", filters.get("payload_id")),
            "model_code": ("model_code=?", filters.get("model_code")),
            "parameter_version": ("parameter_version=?", filters.get("parameter_version")),
        }
        for clause, value in mapping.values():
            if value is not None:
                clauses.append(clause)
                values.append(value)
        if filters.get("derated") is not None:
            clauses.append("derated=?")
            values.append(1 if filters["derated"] else 0)
        ranges = (
            ("radiation_dose", "radiation_dose_min", "radiation_dose_max"),
            ("thermal_cycles", "thermal_cycles_min", "thermal_cycles_max"),
        )
        for column, minimum_key, maximum_key in ranges:
            if filters.get(minimum_key) is not None:
                clauses.append(f"{column}>=?")
                values.append(filters[minimum_key])
            if filters.get(maximum_key) is not None:
                clauses.append(f"{column}<=?")
                values.append(filters[maximum_key])
        return clauses, values
