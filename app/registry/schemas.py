from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ExperimentConditions(BaseModel):
    """实验条件；已知字段建列索引，未知条件保留在条件 JSON 中，来源链信息不丢失。"""

    model_config = ConfigDict(extra="allow")

    radiation_dose: float | None = Field(default=None, ge=0)
    radiation_unit: str = Field(default="", max_length=40)
    thermal_cycles: int | None = Field(default=None, ge=0)
    thermal_profile: str = Field(default="", max_length=120)
    derated: bool = False
    derating_reason: str = Field(default="", max_length=500)


class RunRegister(BaseModel):
    model_code: str = Field(min_length=1, max_length=80)
    model_version: str = Field(min_length=1, max_length=80)
    payload_code: str = Field(min_length=1, max_length=80)
    payload_version: str = Field(min_length=1, max_length=80)
    algorithm_version: str = Field(default="", max_length=120)
    conditions: ExperimentConditions = Field(default_factory=ExperimentConditions)
    executed_at: str = Field(min_length=1, max_length=64)
    submitted_by: str = Field(min_length=1, max_length=120)
    source_refs: list[str] = Field(min_length=1, max_length=100)
    summary: dict[str, Any]
    metrics: dict[str, Any] = Field(default_factory=dict)


class QualityLabelRequest(BaseModel):
    label: Literal["gold", "silver", "bronze", "quarantined", "untrusted"]
    note: str = Field(default="", max_length=1000)
    actor: str = Field(min_length=1, max_length=120)


class ReviewRequest(BaseModel):
    verdict: Literal["approved", "changes_requested", "rejected"]
    comment: str = Field(min_length=1, max_length=2000)
    reviewer: str = Field(min_length=1, max_length=120)


class PublishRequest(BaseModel):
    actor: str = Field(min_length=1, max_length=120)


class WithdrawRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=1000)
    actor: str = Field(min_length=1, max_length=120)
