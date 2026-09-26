from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, Response

from app.registry.schemas import AnnotationCreate, EntryRegister, PublishRequest, RetractAnnotationRequest, RetractEntryRequest
from app.registry.service import RegistryService

router = APIRouter(prefix="/api/registry", tags=["科学计算结果登记簿"])


def service() -> RegistryService:
    return RegistryService()


def _condition_filters(
    status: str | None,
    payload_id: str | None,
    model_code: str | None,
    parameter_version: str | None,
    derated: bool | None,
    radiation_dose_min: float | None,
    radiation_dose_max: float | None,
    thermal_cycles_min: int | None,
    thermal_cycles_max: int | None,
) -> dict[str, Any]:
    return {
        "status": status,
        "payload_id": payload_id,
        "model_code": model_code,
        "parameter_version": parameter_version,
        "derated": derated,
        "radiation_dose_min": radiation_dose_min,
        "radiation_dose_max": radiation_dose_max,
        "thermal_cycles_min": thermal_cycles_min,
        "thermal_cycles_max": thermal_cycles_max,
    }


@router.post("/entries", status_code=201)
def register_entry(payload: EntryRegister, response: Response):
    entry, created = service().register(payload.model_dump())
    if not created:
        response.status_code = 200
    return {"entry": entry, "created": created, "deduplicated": not created}


@router.get("/entries")
def list_entries(
    status: str | None = None,
    payload_id: str | None = None,
    model_code: str | None = None,
    parameter_version: str | None = None,
    derated: bool | None = None,
    radiation_dose_min: float | None = None,
    radiation_dose_max: float | None = None,
    thermal_cycles_min: int | None = None,
    thermal_cycles_max: int | None = None,
    limit: int = Query(default=100, ge=1, le=500),
):
    filters = _condition_filters(status, payload_id, model_code, parameter_version, derated, radiation_dose_min, radiation_dose_max, thermal_cycles_min, thermal_cycles_max)
    return service().list_entries(filters=filters, limit=limit)


@router.get("/entries/{entry_id}")
def get_entry(entry_id: int):
    return service().get_entry(entry_id)


@router.get("/compare")
def compare_entries(
    ids: str | None = None,
    status: str | None = None,
    payload_id: str | None = None,
    model_code: str | None = None,
    parameter_version: str | None = None,
    derated: bool | None = None,
    radiation_dose_min: float | None = None,
    radiation_dose_max: float | None = None,
    thermal_cycles_min: int | None = None,
    thermal_cycles_max: int | None = None,
    limit: int = Query(default=100, ge=1, le=500),
):
    entry_ids = [int(part) for part in ids.split(",") if part.strip()] if ids else None
    filters = _condition_filters(status, payload_id, model_code, parameter_version, derated, radiation_dose_min, radiation_dose_max, thermal_cycles_min, thermal_cycles_max)
    return service().compare(entry_ids=entry_ids, filters=filters, limit=limit)


@router.post("/entries/{entry_id}/annotations", status_code=201)
def add_annotation(entry_id: int, payload: AnnotationCreate):
    return service().add_annotation(entry_id, payload.model_dump())


@router.post("/entries/{entry_id}/publish")
def publish_entry(entry_id: int, payload: PublishRequest):
    return service().publish(entry_id, payload.actor, payload.expected_version, payload.note)


@router.post("/entries/{entry_id}/retract")
def retract_entry(entry_id: int, payload: RetractEntryRequest):
    return service().retract(entry_id, payload.actor, payload.reason, payload.expected_version)


@router.post("/annotations/{annotation_id}/retract")
def retract_annotation(annotation_id: int, payload: RetractAnnotationRequest):
    return service().retract_annotation(annotation_id, payload.actor, payload.reason)


@router.get("/summary")
def summary():
    return service().summary()
