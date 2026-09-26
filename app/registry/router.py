from __future__ import annotations

from fastapi import APIRouter, Query, Response

from app.registry.schemas import (
    PublishRequest,
    QualityLabelRequest,
    ReviewRequest,
    RunRegister,
    WithdrawRequest,
)
from app.registry.service import ResultRegistryService
from app.registry.signing import SignatureError, verify_envelope

router = APIRouter(prefix="/api/registry", tags=["科学结果登记簿"])


def service() -> ResultRegistryService:
    return ResultRegistryService()


@router.post("/runs", status_code=201)
def register_run(payload: RunRegister, response: Response):
    result = service().register_run(payload.model_dump())
    # 重复上传同一运行返回既有版本（幂等），用 200 与新建的 201 区分。
    if result.pop("deduplicated", False):
        response.status_code = 200
    return result


@router.get("/runs")
def query_runs(
    model_code: str | None = None,
    payload_code: str | None = None,
    derated: bool | None = None,
    label: str | None = None,
    status: str | None = Query(default=None, pattern="^(published|withdrawn)$"),
    published_only: bool = False,
    limit: int = Query(default=100, ge=1, le=500),
):
    return service().query_runs(
        model_code=model_code,
        payload_code=payload_code,
        derated=derated,
        label=label,
        status=status,
        published_only=published_only,
        limit=limit,
    )


@router.get("/compare")
def compare(
    by: str = Query(..., pattern="^(radiation_dose|thermal_cycles|payload|model|derated)$"),
    model_code: str | None = None,
    payload_code: str | None = None,
):
    return service().compare(by, model_code=model_code, payload_code=payload_code)


@router.get("/runs/{run_uid}")
def get_run(run_uid: str):
    return service().get_run(run_uid)


@router.post("/runs/{run_uid}/quality-labels")
def add_quality_label(run_uid: str, payload: QualityLabelRequest):
    return service().add_quality_label(run_uid, payload.label, payload.note, payload.actor)


@router.post("/runs/{run_uid}/reviews")
def add_review(run_uid: str, payload: ReviewRequest):
    return service().add_review(run_uid, payload.verdict, payload.comment, payload.reviewer)


@router.post("/runs/{run_uid}/publish")
def publish(run_uid: str, payload: PublishRequest):
    return service().publish(run_uid, payload.actor)


@router.post("/runs/{run_uid}/withdraw")
def withdraw(run_uid: str, payload: WithdrawRequest):
    return service().withdraw(run_uid, payload.reason, payload.actor)


@router.post("/verify-signature")
def verify_signature(envelope: dict):
    try:
        payload = verify_envelope(envelope)
    except SignatureError as exc:
        return {"valid": False, "error": exc.message}
    return {"valid": True, "payload": payload}
