"""Business logic with the concurrency protocol.

Locking / isolation protocol (PostgreSQL, READ COMMITTED):

  * publish / update-config: take the LAYER row lock (FOR UPDATE) first to
    serialize all publishers of the layer, then re-check bucket-range overlap
    against ACTIVE experiments inside the same transaction. Two concurrent
    publishes therefore cannot both pass validation.

  * assign: take the experiment row lock FOR SHARE (read=True). FOR SHARE is
    compatible with other concurrent assigns but blocks a concurrent END
    (which takes FOR UPDATE on the same row) until this transaction commits,
    so an experiment cannot be ended in the middle of an assignment. The
    partial unique index uq_assignments_active_user guarantees at most one
    ACTIVE assignment per (layer, user); on a unique-violation race the loser
    re-reads and returns the winner's assignment.

  * end: take the experiment row lock FOR UPDATE (waits for in-flight
    assigns), mark ended, then close all its active assignments in the same
    transaction. After commit no ACTIVE assignment can exist for the
    experiment, and no new one can be created (assign re-checks status under
    the lock).

  * exposure: lock the assignment row FOR UPDATE so a concurrent END either
    closes the assignment first (exposure then rejected) or waits until the
    exposure commits. (assignment_id, event_no) unique constraint gives
    idempotency; same event_no with different content is a 409 conflict.
"""
import secrets
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.hashing import choose_group, user_bucket
from app.models import Assignment, Experiment, Exposure, Layer
from app.schemas import ExperimentConfig, ExperimentCreate, ExperimentUpdate

_MISSING = object()


# ---------------------------------------------------------------- layer ----

def create_layer(db: Session, name: str) -> Layer:
    layer = Layer(name=name, salt=secrets.token_hex(16))
    db.add(layer)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "layer name already exists")
    db.refresh(layer)
    return layer


def list_layers(db: Session) -> list[Layer]:
    return list(db.execute(select(Layer).order_by(Layer.created_at)).scalars())


def get_layer(db: Session, layer_id: UUID) -> Layer:
    layer = db.get(Layer, layer_id)
    if layer is None:
        raise HTTPException(404, "layer not found")
    return layer


# ------------------------------------------------------------ experiment ----

def create_experiment(db: Session, payload: ExperimentCreate) -> Experiment:
    get_layer(db, payload.layer_id)
    exp = Experiment(
        layer_id=payload.layer_id,
        name=payload.name,
        status="draft",
        version=0,
        bucket_start=payload.bucket_start,
        bucket_end=payload.bucket_end,
        conditions=payload.conditions,
        groups=[g.model_dump() for g in payload.groups],
    )
    db.add(exp)
    db.commit()
    db.refresh(exp)
    return exp


def get_experiment(db: Session, experiment_id: UUID) -> Experiment:
    exp = db.get(Experiment, experiment_id)
    if exp is None:
        raise HTTPException(404, "experiment not found")
    return exp


def list_experiments(db: Session, layer_id: UUID) -> list[Experiment]:
    get_layer(db, layer_id)
    return list(
        db.execute(
            select(Experiment).where(Experiment.layer_id == layer_id).order_by(Experiment.created_at)
        ).scalars()
    )


def _assert_no_overlap(
    db: Session, layer_id: UUID, exclude_id: UUID, start: int, end: int
) -> None:
    """Must be called while holding the layer row lock."""
    overlap = db.execute(
        select(Experiment).where(
            Experiment.layer_id == layer_id,
            Experiment.status == "active",
            Experiment.id != exclude_id,
            Experiment.bucket_start < end,
            Experiment.bucket_end > start,
        )
    ).scalars().first()
    if overlap is not None:
        raise HTTPException(
            409,
            f"bucket range [{start}, {end}) overlaps active experiment {overlap.id} "
            f"[{overlap.bucket_start}, {overlap.bucket_end})",
        )


def publish_experiment(db: Session, experiment_id: UUID) -> Experiment:
    exp = db.get(Experiment, experiment_id)
    if exp is None:
        raise HTTPException(404, "experiment not found")
    # 1. Serialize publishers/updaters of this layer.
    db.execute(select(Layer).where(Layer.id == exp.layer_id).with_for_update())
    # 2. Serialize with a concurrent END of this experiment; re-read status.
    exp = db.execute(
        select(Experiment)
        .where(Experiment.id == experiment_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one()
    if exp.status == "ended":
        raise HTTPException(409, "ended experiment cannot be published")
    # 3. Atomic occupancy validation under the layer lock.
    _assert_no_overlap(db, exp.layer_id, exp.id, exp.bucket_start, exp.bucket_end)
    exp.status = "active"
    exp.version += 1
    exp.published_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(exp)
    return exp


def update_experiment(db: Session, experiment_id: UUID, patch: ExperimentUpdate) -> Experiment:
    exp = db.get(Experiment, experiment_id)
    if exp is None:
        raise HTTPException(404, "experiment not found")
    if exp.status == "ended":
        raise HTTPException(409, "ended experiment cannot be modified")

    merged = {
        "bucket_start": patch.bucket_start if patch.bucket_start is not None else exp.bucket_start,
        "bucket_end": patch.bucket_end if patch.bucket_end is not None else exp.bucket_end,
        "conditions": patch.conditions if patch.conditions is not None else exp.conditions,
        "groups": [g.model_dump() for g in patch.groups] if patch.groups is not None else exp.groups,
    }
    try:
        cfg = ExperimentConfig(**merged)
    except ValidationError as exc:
        raise HTTPException(422, str(exc))

    # Serialize with other publishers/updaters of the layer.
    db.execute(select(Layer).where(Layer.id == exp.layer_id).with_for_update())
    if exp.status == "active":
        _assert_no_overlap(db, exp.layer_id, exp.id, cfg.bucket_start, cfg.bucket_end)

    exp.bucket_start = cfg.bucket_start
    exp.bucket_end = cfg.bucket_end
    exp.conditions = cfg.conditions
    exp.groups = [g.model_dump() for g in cfg.groups]
    if patch.name is not None:
        exp.name = patch.name
    if exp.status == "active":
        # New rule version: only users not yet assigned see it; existing
        # assignments keep their stored snapshot.
        exp.version += 1
    db.commit()
    db.refresh(exp)
    return exp


def end_experiment(db: Session, experiment_id: UUID) -> Experiment:
    exp = db.execute(
        select(Experiment)
        .where(Experiment.id == experiment_id)
        .with_for_update()  # waits for in-flight assigns (FOR SHARE)
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if exp is None:
        raise HTTPException(404, "experiment not found")
    if exp.status == "draft":
        raise HTTPException(409, "experiment is not published")
    if exp.status == "ended":
        raise HTTPException(409, "experiment already ended")

    exp.status = "ended"
    exp.ended_at = datetime.now(timezone.utc)
    # Close all its active assignments atomically in the same transaction.
    db.execute(
        update(Assignment)
        .where(Assignment.experiment_id == exp.id, Assignment.status == "active")
        .values(status="closed")
    )
    db.commit()
    db.refresh(exp)
    return exp


# ------------------------------------------------------------ assignment ----

def _conditions_match(conditions: dict, attributes: dict) -> bool:
    """All condition key/value pairs must equal the user's attributes."""
    return all(attributes.get(k, _MISSING) == v for k, v in (conditions or {}).items())


def _active_assignment(db: Session, layer_id: UUID, user_key: str) -> Assignment | None:
    return db.execute(
        select(Assignment).where(
            Assignment.layer_id == layer_id,
            Assignment.user_key == user_key,
            Assignment.status == "active",
        )
    ).scalar_one_or_none()


def assign(
    db: Session, layer_id: UUID, user_key: str, attributes: dict[str, Any]
) -> tuple[Assignment | None, int, str | None]:
    """Returns (assignment_or_None, bucket, unassigned_reason_or_None)."""
    layer = get_layer(db, layer_id)
    bucket = user_bucket(layer.salt, user_key)

    # 1. A valid (active) assignment for this user in this layer persists.
    existing = _active_assignment(db, layer.id, user_key)
    if existing is not None:
        return existing, bucket, None

    # 2. Find the active experiment covering this bucket (ranges are
    #    non-overlapping, so at most one). Second pass covers a config that
    #    was updated between our candidate query and the row lock.
    for _ in range(2):
        candidate_ids = db.execute(
            select(Experiment.id).where(
                Experiment.layer_id == layer.id,
                Experiment.status == "active",
                Experiment.bucket_start <= bucket,
                Experiment.bucket_end > bucket,
            )
        ).scalars().all()
        if not candidate_ids:
            return None, bucket, "no_experiment_covers_bucket"

        stale = False
        for cand_id in candidate_ids:
            exp = db.execute(
                select(Experiment)
                .where(Experiment.id == cand_id)
                .with_for_update(read=True)  # FOR SHARE: blocks concurrent END
                .execution_options(populate_existing=True)
            ).scalar_one()
            if exp.status != "active" or not (exp.bucket_start <= bucket < exp.bucket_end):
                stale = True  # ended/updated concurrently; re-query candidates
                continue
            if not _conditions_match(exp.conditions, attributes):
                return None, bucket, "conditions_not_matched"

            group = choose_group(layer.salt, str(exp.id), user_key, exp.groups)
            snapshot = {
                "experiment_id": str(exp.id),
                "experiment_name": exp.name,
                "version": exp.version,
                "bucket_start": exp.bucket_start,
                "bucket_end": exp.bucket_end,
                "conditions": exp.conditions,
                "groups": exp.groups,
            }
            asg = Assignment(
                layer_id=layer.id,
                experiment_id=exp.id,
                experiment_version=exp.version,
                user_key=user_key,
                bucket=bucket,
                group_name=group,
                status="active",
                snapshot=snapshot,
            )
            db.add(asg)
            try:
                db.commit()
            except IntegrityError:
                # Lost a same-user race; the winner's row is now committed.
                db.rollback()
                winner = _active_assignment(db, layer.id, user_key)
                if winner is not None:
                    return winner, bucket, None
                raise
            db.refresh(asg)
            return asg, bucket, None

        if not stale:
            return None, bucket, "no_experiment_covers_bucket"
    return None, bucket, "no_experiment_covers_bucket"


def get_assignment(db: Session, assignment_id: UUID) -> Assignment:
    asg = db.get(Assignment, assignment_id)
    if asg is None:
        raise HTTPException(404, "assignment not found")
    return asg


def list_user_assignments(db: Session, layer_id: UUID, user_key: str) -> list[Assignment]:
    get_layer(db, layer_id)
    return list(
        db.execute(
            select(Assignment)
            .where(Assignment.layer_id == layer_id, Assignment.user_key == user_key)
            .order_by(Assignment.created_at)
        ).scalars()
    )


# -------------------------------------------------------------- exposure ----

def register_exposure(
    db: Session, assignment_id: UUID, event_no: str, content: Any
) -> tuple[Exposure, bool]:
    """Returns (exposure, deduplicated)."""
    asg = db.execute(
        select(Assignment)
        .where(Assignment.id == assignment_id)
        .with_for_update()  # serialize with END closing this assignment
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if asg is None:
        raise HTTPException(404, "assignment not found")
    if asg.status != "active":
        raise HTTPException(409, "assignment is closed (experiment ended); exposure rejected")

    existing = db.execute(
        select(Exposure).where(
            Exposure.assignment_id == assignment_id, Exposure.event_no == event_no
        )
    ).scalar_one_or_none()
    if existing is not None:
        if existing.content == content:
            return existing, True  # idempotent retry
        raise HTTPException(409, "event_no already registered with different content")

    exposure = Exposure(assignment_id=assignment_id, event_no=event_no, content=content)
    db.add(exposure)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        winner = db.execute(
            select(Exposure).where(
                Exposure.assignment_id == assignment_id, Exposure.event_no == event_no
            )
        ).scalar_one()
        if winner.content == content:
            return winner, True
        raise HTTPException(409, "event_no already registered with different content")
    db.refresh(exposure)
    return exposure, False
