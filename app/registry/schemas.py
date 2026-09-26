from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

QUALITY_LABELS = ("trusted", "suspect", "defective")


class RunMetadata(BaseModel):
    """一次运行的来源元数据：载荷、参数版本与实验条件。"""

    payload_id: str = Field(min_length=1, max_length=80)
    run_id: str = Field(min_length=1, max_length=120)
    model_code: str = Field(min_length=1, max_length=80)
    parameter_version: str = Field(min_length=1, max_length=80)
    derated: bool
    radiation_dose: float | None = Field(default=None, ge=0)
    thermal_cycles: int | None = Field(default=None, ge=0)
    environment: dict[str, Any] = Field(default_factory=dict)


class EntryRegister(BaseModel):
    metadata: RunMetadata
    summary: dict[str, Any]
    registered_by: str = Field(min_length=1, max_length=120)


class AnnotationCreate(BaseModel):
    kind: Literal["quality_label", "review"]
    label: Literal["trusted", "suspect", "defective"] | None = None
    comment: str = Field(default="", max_length=2000)
    reviewer: str = Field(min_length=1, max_length=120)

    @model_validator(mode="after")
    def validate_content(self) -> "AnnotationCreate":
        if self.kind == "quality_label" and self.label is None:
            raise ValueError("质量标签标注必须提供 label")
        if self.kind == "review" and not self.comment.strip():
            raise ValueError("人工复核意见必须提供 comment")
        return self


class PublishRequest(BaseModel):
    actor: str = Field(min_length=1, max_length=120)
    expected_version: int = Field(ge=1)
    note: str = Field(default="", max_length=1000)


class RetractEntryRequest(BaseModel):
    actor: str = Field(min_length=1, max_length=120)
    reason: str = Field(min_length=2, max_length=1000)
    expected_version: int = Field(ge=1)


class RetractAnnotationRequest(BaseModel):
    actor: str = Field(min_length=1, max_length=120)
    reason: str = Field(min_length=2, max_length=1000)
