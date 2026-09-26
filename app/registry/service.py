from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
from typing import Any

from app.core.clock import Clock, SystemClock, to_storage
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.database import get_connection, transaction
from app.registry.repository import RegistryRepository

DEFAULT_SIGNING_KEY = "registry-local-development-key"
ENTRY_STATUSES = ("registered", "published", "retracted")
CONDITION_FIELDS = ("payload_id", "model_code", "parameter_version", "derated", "radiation_dose", "thermal_cycles")


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def content_digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


class RegistryService:
    """结果登记簿：不可变版本、质量标签、人工复核、发布状态与签名查询。"""

    def __init__(self, connection: sqlite3.Connection | None = None, clock: Clock | None = None, signing_key: str | None = None) -> None:
        self.connection = connection or get_connection()
        self.clock = clock or SystemClock()
        key = signing_key if signing_key is not None else os.getenv("REGISTRY_SIGNING_KEY", DEFAULT_SIGNING_KEY)
        self.signing_key = key.encode()
        self.repository = RegistryRepository(self.connection)

    def signed(self, data: Any) -> dict[str, Any]:
        """为查询响应附加确定性的摘要与 HMAC 签名。"""
        issued_at = to_storage(self.clock.now())
        body = canonical({"data": data, "issued_at": issued_at})
        return {
            "data": data,
            "issued_at": issued_at,
            "digest": hashlib.sha256(body.encode()).hexdigest(),
            "signature": hmac.new(self.signing_key, body.encode(), hashlib.sha256).hexdigest(),
            "algorithm": "HMAC-SHA256",
        }

    def register(self, payload: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        """登记一次运行；同一运行重复上传返回原版本，内容冲突则拒绝。"""
        metadata = payload["metadata"]
        summary = payload["summary"]
        if not summary:
            raise ValidationError("结果摘要不能为空")
        metadata_digest = content_digest(metadata)
        summary_digest = content_digest(summary)
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = RegistryRepository(connection)
            existing = repository.entry_by_run(metadata["payload_id"], metadata["run_id"])
            if existing is not None:
                self._ensure_same_run(existing, metadata_digest, summary_digest)
                return existing, False
            try:
                entry = repository.create_entry(
                    metadata=metadata,
                    summary=summary,
                    metadata_digest=metadata_digest,
                    summary_digest=summary_digest,
                    registered_by=payload["registered_by"],
                    now=now,
                )
            except sqlite3.IntegrityError:
                existing = repository.entry_by_run(metadata["payload_id"], metadata["run_id"])
                if existing is None:
                    raise
                self._ensure_same_run(existing, metadata_digest, summary_digest)
                return existing, False
            repository.add_event(
                entry_id=entry["id"],
                action="registered",
                actor=payload["registered_by"],
                detail={"metadata_digest": metadata_digest, "summary_digest": summary_digest},
                now=now,
            )
            return entry, True

    def get_entry(self, entry_id: int) -> dict[str, Any]:
        entry = self.repository.entry_by_id(entry_id)
        if entry is None:
            raise NotFoundError("登记条目不存在")
        return self.signed(
            {
                "entry": entry,
                "annotations": self.repository.annotations(entry_id),
                "events": self.repository.events(entry_id),
            }
        )

    def list_entries(self, *, filters: dict[str, Any], limit: int = 100) -> dict[str, Any]:
        status = filters.get("status")
        if status is not None and status not in ENTRY_STATUSES:
            raise ValidationError("未知的发布状态", context={"status": status, "allowed": list(ENTRY_STATUSES)})
        items = self.repository.list_entries(filters=filters, limit=max(1, min(limit, 500)))
        return self.signed({"items": items, "count": len(items), "filters": {key: value for key, value in filters.items() if value is not None}})

    def compare(self, *, entry_ids: list[int] | None, filters: dict[str, Any], limit: int = 100) -> dict[str, Any]:
        """按实验条件对比登记条目，并给出条件字段中的差异项。"""
        if entry_ids:
            entries = self.repository.entries_by_ids(list(dict.fromkeys(entry_ids)))
            missing = [entry_id for entry_id in dict.fromkeys(entry_ids) if all(entry["id"] != entry_id for entry in entries)]
            if missing:
                raise NotFoundError("登记条目不存在", context={"missing": missing})
        else:
            active_filters = {key: value for key, value in filters.items() if value is not None}
            if not active_filters:
                raise ValidationError("比较需要指定条目 id 或至少一个实验条件")
            entries = self.repository.list_entries(filters=filters, limit=max(1, min(limit, 500)))
        differing = [field for field in CONDITION_FIELDS if len({canonical(entry[field]) for entry in entries}) > 1]
        return self.signed({"entries": entries, "count": len(entries), "differing_fields": differing})

    def add_annotation(self, entry_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = RegistryRepository(connection)
            if repository.entry_by_id(entry_id) is None:
                raise NotFoundError("登记条目不存在")
            annotation = repository.add_annotation(
                entry_id=entry_id,
                kind=payload["kind"],
                label=payload.get("label") or "",
                comment=payload.get("comment") or "",
                reviewer=payload["reviewer"],
                now=now,
            )
            repository.add_event(
                entry_id=entry_id,
                action="annotated",
                actor=payload["reviewer"],
                detail={"annotation_id": annotation["id"], "kind": annotation["kind"], "label": annotation["label"]},
                now=now,
            )
            return annotation

    def retract_annotation(self, annotation_id: int, actor: str, reason: str) -> dict[str, Any]:
        """撤回错误标注：记录保留在来源链中，但不再视为有效。"""
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = RegistryRepository(connection)
            annotation = repository.annotation_by_id(annotation_id)
            if annotation is None:
                raise NotFoundError("标注不存在")
            cursor = connection.execute(
                "UPDATE registry_annotations SET state='retracted',retracted_by=?,retracted_at=?,retract_reason=? WHERE id=? AND state='active'",
                (actor, now, reason, annotation_id),
            )
            if cursor.rowcount != 1:
                raise ConflictError("标注已被撤回", context={"state": annotation["state"]})
            repository.add_event(
                entry_id=annotation["entry_id"],
                action="annotation_retracted",
                actor=actor,
                detail={"annotation_id": annotation_id, "kind": annotation["kind"], "label": annotation["label"], "reason": reason},
                now=now,
            )
            return repository.annotation_by_id(annotation_id)

    def publish(self, entry_id: int, actor: str, expected_version: int, note: str = "") -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = RegistryRepository(connection)
            entry = self._require_entry(repository, entry_id)
            cursor = connection.execute(
                "UPDATE registry_entries SET status='published',published_by=?,published_at=?,version=version+1,updated_at=? WHERE id=? AND version=? AND status='registered'",
                (actor, now, now, entry_id, expected_version),
            )
            if cursor.rowcount != 1:
                raise self._state_conflict(entry)
            after = repository.entry_by_id(entry_id)
            repository.add_event(
                entry_id=entry_id,
                action="published",
                actor=actor,
                detail={"from_status": entry["status"], "expected_version": expected_version, "note": note},
                now=now,
            )
            return after

    def retract(self, entry_id: int, actor: str, reason: str, expected_version: int) -> dict[str, Any]:
        """撤回发布：状态终态为 retracted，之后任何查询都不会再视为已发布。"""
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = RegistryRepository(connection)
            entry = self._require_entry(repository, entry_id)
            cursor = connection.execute(
                "UPDATE registry_entries SET status='retracted',retracted_by=?,retracted_at=?,retract_reason=?,version=version+1,updated_at=? WHERE id=? AND version=? AND status IN ('registered','published')",
                (actor, now, reason, now, entry_id, expected_version),
            )
            if cursor.rowcount != 1:
                raise self._state_conflict(entry)
            after = repository.entry_by_id(entry_id)
            repository.add_event(
                entry_id=entry_id,
                action="retracted",
                actor=actor,
                detail={"from_status": entry["status"], "expected_version": expected_version, "reason": reason},
                now=now,
            )
            return after

    def summary(self) -> dict[str, Any]:
        by_status = self.repository.count_by_status()
        return self.signed(
            {
                "entries_total": sum(by_status.values()),
                "by_status": by_status,
                "active_quality_labels": self.repository.count_active_labels(),
            }
        )

    @staticmethod
    def _require_entry(repository: RegistryRepository, entry_id: int) -> dict[str, Any]:
        entry = repository.entry_by_id(entry_id)
        if entry is None:
            raise NotFoundError("登记条目不存在")
        return entry

    @staticmethod
    def _state_conflict(entry: dict[str, Any]) -> ConflictError:
        return ConflictError(
            "登记条目状态已变化，请刷新后重试",
            context={"current_status": entry["status"], "current_version": entry["version"]},
        )

    @staticmethod
    def _ensure_same_run(existing: dict[str, Any], metadata_digest: str, summary_digest: str) -> None:
        if existing["metadata_digest"] != metadata_digest or existing["summary_digest"] != summary_digest:
            raise ConflictError(
                "同一运行标识对应了不同的元数据或结果摘要",
                context={"payload_id": existing["payload_id"], "run_id": existing["run_id"], "entry_id": existing["id"]},
            )
