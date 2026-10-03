"""HTTP API surface."""
from contextlib import asynccontextmanager
from uuid import UUID

from fastapi import Depends, FastAPI
from sqlalchemy.orm import Session

from app import service
from app.db import get_db, init_db
from app.models import Assignment
from app.schemas import (
    AssignIn,
    AssignmentOut,
    ExperimentCreate,
    ExperimentOut,
    ExperimentUpdate,
    ExposureIn,
    ExposureOut,
    LayerCreate,
    LayerOut,
    RearrangeIn,
    RearrangeOut,
    RearrangeResult,
    UnassignedOut,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()  # create tables + indexes (waits for Postgres to be ready)
    yield


app = FastAPI(
    title="Anonymous Experiment Bucketing Service",
    version="1.0.0",
    lifespan=lifespan,
)


def _assignment_out(asg: Assignment) -> AssignmentOut:
    return AssignmentOut(
        assignment_id=asg.id,
        layer_id=asg.layer_id,
        experiment_id=asg.experiment_id,
        experiment_version=asg.experiment_version,
        user_key=asg.user_key,
        bucket=asg.bucket,
        group=asg.group_name,
        status=asg.status,
        snapshot=asg.snapshot,
        created_at=asg.created_at,
    )


@app.get("/")
def root():
    return {"service": "experiment-bucketing", "docs": "/docs", "health": "/health"}


@app.get("/health")
def health():
    return {"status": "ok"}


# ------------------------------------------------------------------ layers

@app.post("/layers", response_model=LayerOut, status_code=201)
def create_layer(payload: LayerCreate, db: Session = Depends(get_db)):
    return service.create_layer(db, payload.name)


@app.get("/layers", response_model=list[LayerOut])
def list_layers(db: Session = Depends(get_db)):
    return service.list_layers(db)


@app.get("/layers/{layer_id}", response_model=LayerOut)
def get_layer(layer_id: UUID, db: Session = Depends(get_db)):
    return service.get_layer(db, layer_id)


# ------------------------------------------------------------- experiments

@app.post("/experiments", response_model=ExperimentOut, status_code=201)
def create_experiment(payload: ExperimentCreate, db: Session = Depends(get_db)):
    return service.create_experiment(db, payload)


@app.get("/experiments/{experiment_id}", response_model=ExperimentOut)
def get_experiment(experiment_id: UUID, db: Session = Depends(get_db)):
    return service.get_experiment(db, experiment_id)


@app.get("/layers/{layer_id}/experiments", response_model=list[ExperimentOut])
def list_experiments(layer_id: UUID, db: Session = Depends(get_db)):
    return service.list_experiments(db, layer_id)


@app.post("/experiments/{experiment_id}/publish", response_model=ExperimentOut)
def publish_experiment(experiment_id: UUID, db: Session = Depends(get_db)):
    return service.publish_experiment(db, experiment_id)


@app.put("/experiments/{experiment_id}", response_model=ExperimentOut)
def update_experiment(
    experiment_id: UUID, payload: ExperimentUpdate, db: Session = Depends(get_db)
):
    return service.update_experiment(db, experiment_id, payload)


@app.post("/experiments/{experiment_id}/end", response_model=ExperimentOut)
def end_experiment(experiment_id: UUID, db: Session = Depends(get_db)):
    return service.end_experiment(db, experiment_id)


@app.post("/layers/{layer_id}/rearrangements", response_model=RearrangeOut)
def rearrange_layer(layer_id: UUID, payload: RearrangeIn, db: Session = Depends(get_db)):
    """Atomically re-bucket many experiments of one layer in one batch."""
    results = service.rearrange_layer(db, layer_id, payload.items)
    return RearrangeOut(
        layer_id=layer_id,
        experiments=[
            RearrangeResult(
                id=exp.id,
                status=exp.status,
                version=exp.version,
                bucket_start=exp.bucket_start,
                bucket_end=exp.bucket_end,
                changed=changed,
            )
            for exp, changed in results
        ],
    )


# ------------------------------------------------------------- assignments

@app.post("/layers/{layer_id}/assignments")
def assign(layer_id: UUID, payload: AssignIn, db: Session = Depends(get_db)):
    asg, bucket, reason = service.assign(db, layer_id, payload.user_key, payload.attributes)
    if asg is None:
        return UnassignedOut(
            layer_id=layer_id, user_key=payload.user_key, bucket=bucket, reason=reason
        )
    return _assignment_out(asg)


@app.get("/assignments/{assignment_id}", response_model=AssignmentOut)
def get_assignment(assignment_id: UUID, db: Session = Depends(get_db)):
    return _assignment_out(service.get_assignment(db, assignment_id))


@app.get("/layers/{layer_id}/assignments", response_model=list[AssignmentOut])
def list_user_assignments(layer_id: UUID, user_key: str, db: Session = Depends(get_db)):
    return [_assignment_out(a) for a in service.list_user_assignments(db, layer_id, user_key)]


# --------------------------------------------------------------- exposures

@app.post("/exposures", response_model=ExposureOut)
def register_exposure(payload: ExposureIn, db: Session = Depends(get_db)):
    exposure, deduplicated = service.register_exposure(
        db, payload.assignment_id, payload.event_no, payload.content
    )
    return ExposureOut(
        id=exposure.id,
        assignment_id=exposure.assignment_id,
        event_no=exposure.event_no,
        content=exposure.content,
        deduplicated=deduplicated,
        created_at=exposure.created_at,
    )
