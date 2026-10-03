"""SQLAlchemy models.

Concurrency invariants enforced at the database level:
  * uq_assignments_active_user - partial unique index: at most one ACTIVE
    assignment per (layer_id, user_key). Closed (ended) assignments do not
    block a user from being assigned to another experiment in the layer.
  * uq_exposure_assignment_event - unique (assignment_id, event_no) for
    idempotent exposure registration.
"""
import uuid

from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID

from app.db import Base


class Layer(Base):
    __tablename__ = "layers"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(128), nullable=False, unique=True)
    # Fixed per-layer salt, generated at creation and never changed.
    salt = Column(String(64), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())


class Experiment(Base):
    __tablename__ = "experiments"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    layer_id = Column(UUID(as_uuid=True), ForeignKey("layers.id"), nullable=False, index=True)
    name = Column(String(128), nullable=False)
    status = Column(String(16), nullable=False, default="draft")  # draft | active | ended
    version = Column(Integer, nullable=False, default=0)
    # Half-open bucket range [bucket_start, bucket_end) within [0, 10000).
    bucket_start = Column(Integer, nullable=False)
    bucket_end = Column(Integer, nullable=False)
    # Attribute key/value conditions; ALL must match the user's attributes.
    conditions = Column(JSONB, nullable=False, default=dict)
    # [{"name": "control", "weight": 5000}, ...]; weights sum to 10000.
    groups = Column(JSONB, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    published_at = Column(DateTime(timezone=True))
    ended_at = Column(DateTime(timezone=True))


class Assignment(Base):
    __tablename__ = "assignments"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    layer_id = Column(UUID(as_uuid=True), ForeignKey("layers.id"), nullable=False)
    experiment_id = Column(UUID(as_uuid=True), ForeignKey("experiments.id"), nullable=False)
    experiment_version = Column(Integer, nullable=False)
    user_key = Column(String(256), nullable=False)
    bucket = Column(Integer, nullable=False)
    group_name = Column(String(128), nullable=False)
    status = Column(String(16), nullable=False, default="active")  # active | closed
    # Immutable rule snapshot taken at assignment time.
    snapshot = Column(JSONB, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        Index(
            "uq_assignments_active_user",
            "layer_id",
            "user_key",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
        Index("ix_assignments_experiment", "experiment_id"),
    )


class Exposure(Base):
    __tablename__ = "exposures"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    assignment_id = Column(UUID(as_uuid=True), ForeignKey("assignments.id"), nullable=False)
    event_no = Column(String(128), nullable=False)
    content = Column(JSONB, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("assignment_id", "event_no", name="uq_exposure_assignment_event"),
    )
