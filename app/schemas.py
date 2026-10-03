"""Pydantic request/response schemas."""
from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

TOTAL_BUCKETS = 10000


# ---------- layer ----------

class LayerCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)


class LayerOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    salt: str
    created_at: datetime


# ---------- experiment ----------

class GroupSpec(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    weight: int = Field(ge=0, le=TOTAL_BUCKETS)


class ExperimentConfig(BaseModel):
    """A complete, validated experiment rule set."""

    bucket_start: int = Field(ge=0, lt=TOTAL_BUCKETS)
    bucket_end: int = Field(gt=0, le=TOTAL_BUCKETS)
    conditions: dict[str, Any] = Field(default_factory=dict)
    groups: list[GroupSpec] = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_rules(self) -> "ExperimentConfig":
        if self.bucket_start >= self.bucket_end:
            raise ValueError("bucket_start must be < bucket_end (half-open range)")
        if sum(g.weight for g in self.groups) != TOTAL_BUCKETS:
            raise ValueError("group weights must sum to 10000")
        names = [g.name for g in self.groups]
        if len(set(names)) != len(names):
            raise ValueError("group names must be unique")
        return self


class ExperimentCreate(ExperimentConfig):
    layer_id: UUID
    name: str = Field(min_length=1, max_length=128)


class ExperimentUpdate(BaseModel):
    """Partial update; merged with the stored config and revalidated."""

    name: str | None = Field(default=None, min_length=1, max_length=128)
    bucket_start: int | None = Field(default=None, ge=0, lt=TOTAL_BUCKETS)
    bucket_end: int | None = Field(default=None, gt=0, le=TOTAL_BUCKETS)
    conditions: dict[str, Any] | None = None
    groups: list[GroupSpec] | None = Field(default=None, min_length=1)


# ---------- batch rearrangement (many experiments, one layer, one tx) ----

class RearrangeItem(BaseModel):
    """One experiment move inside the layer's batch rearrangement.

    expected_version is an optimistic-concurrency token: it must equal the
    experiment's current version (drafts are version 0). Drafts listed here
    are published as part of the batch; active experiments are moved to the
    target half-open range. The range must lie within [0, 10000).
    """

    experiment_id: UUID
    expected_version: int = Field(ge=0)
    bucket_start: int = Field(ge=0, lt=TOTAL_BUCKETS)
    bucket_end: int = Field(gt=0, le=TOTAL_BUCKETS)

    @model_validator(mode="after")
    def _validate_range(self) -> "RearrangeItem":
        if self.bucket_start >= self.bucket_end:
            raise ValueError("bucket_start must be < bucket_end (half-open range)")
        return self


class RearrangeIn(BaseModel):
    items: list[RearrangeItem] = Field(min_length=1)

    @model_validator(mode="after")
    def _no_duplicate_experiments(self) -> "RearrangeIn":
        ids = [it.experiment_id for it in self.items]
        if len(set(ids)) != len(ids):
            raise ValueError("experiment_id must not repeat within the batch")
        return self


class RearrangeResult(BaseModel):
    """One experiment's state after the batch."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    status: str
    version: int
    bucket_start: int
    bucket_end: int
    # True when this experiment's version was incremented by the batch:
    # an active experiment whose range actually changed, or a draft published.
    changed: bool


class RearrangeOut(BaseModel):
    layer_id: UUID
    experiments: list[RearrangeResult]


class ExperimentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    layer_id: UUID
    name: str
    status: str
    version: int
    bucket_start: int
    bucket_end: int
    conditions: dict[str, Any]
    groups: list[GroupSpec]
    created_at: datetime
    published_at: datetime | None
    ended_at: datetime | None


# ---------- assignment ----------

class AssignIn(BaseModel):
    user_key: str = Field(min_length=1, max_length=256)
    attributes: dict[str, Any] = Field(default_factory=dict)


class AssignmentOut(BaseModel):
    assigned: bool = True
    assignment_id: UUID
    layer_id: UUID
    experiment_id: UUID
    experiment_version: int
    user_key: str
    bucket: int
    group: str
    status: str
    snapshot: dict[str, Any]
    created_at: datetime


class UnassignedOut(BaseModel):
    assigned: bool = False
    layer_id: UUID
    user_key: str
    bucket: int
    reason: str


# ---------- exposure ----------

class ExposureIn(BaseModel):
    assignment_id: UUID
    event_no: str = Field(min_length=1, max_length=128)
    content: Any  # any JSON payload; required


class ExposureOut(BaseModel):
    id: UUID
    assignment_id: UUID
    event_no: str
    content: Any
    deduplicated: bool
    created_at: datetime
