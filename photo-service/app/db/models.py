"""SQLAlchemy declarative models.

TASK-001: `photos` table only - the minimum needed to give Alembic a
non-empty initial revision and validate the vertical slice (API -> DB ->
MinIO) end to end.

TASK-002 (tasks/TASK-002/20_design.md §2): extends `Photo` with the
async-analysis-pipeline fields (`attempts`, `last_error_*`, the simplified
outbox `publish_status`/`published_at`, `trace_id` for cross-service
tracing, `batch_id`) and adds `AnalysisResult` (1:1 with `Photo`) and
`Batch`. No new PostgreSQL enum types are introduced (lesson from
TASK-000/60_debug.md - `create_type=False` dance); new status columns are
plain `VARCHAR` + `CHECK` constraints instead.
"""

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Enum as SAEnum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class PhotoStatus(str, enum.Enum):
    pending = "pending"
    processing = "processing"
    done = "done"
    failed = "failed"


class Photo(Base):
    __tablename__ = "photos"

    photo_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    object_key: Mapped[str] = mapped_column(String(1024), nullable=False, unique=True)
    user_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    status: Mapped[PhotoStatus] = mapped_column(
        SAEnum(PhotoStatus, name="photo_status"),
        default=PhotoStatus.pending,
        nullable=False,
    )

    # TASK-002: worker retry bookkeeping (tasks/TASK-002/20_design.md §2.3)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    last_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    # TASK-002: simplified outbox (tasks/TASK-002/20_design.md §4)
    publish_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="not_sent", server_default="not_sent"
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # TASK-002: batch membership (nullable - single uploads have no batch)
    batch_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("batches.batch_id"), nullable=True
    )

    analysis: Mapped["AnalysisResult | None"] = relationship(
        "AnalysisResult", uselist=False, back_populates="photo"
    )
    batch: Mapped["Batch | None"] = relationship(
        "Batch", foreign_keys=[batch_id], back_populates="photos"
    )

    __table_args__ = (
        Index("ix_photos_created_at", "created_at"),
        Index("ix_photos_batch_id", "batch_id"),
        Index("ix_photos_status", "status"),
        Index(
            "ix_photos_publish_status",
            "publish_status",
            postgresql_where="publish_status = 'not_sent'",
        ),
        CheckConstraint(
            "publish_status IN ('not_sent', 'sent')", name="ck_photos_publish_status"
        ),
    )


class AnalysisResult(Base):
    """1:1 result of an analyzer-stub/analyzer run for a photo.

    `photo_id` is both the primary key and the FK - guarantees at most one
    result row per photo (idempotent write target for the worker, see
    tasks/TASK-002/20_design.md §2.1).
    """

    __tablename__ = "analysis_results"

    photo_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("photos.photo_id", ondelete="CASCADE"),
        primary_key=True,
    )
    faces_count: Mapped[int] = mapped_column(Integer, nullable=False)
    is_blurred: Mapped[bool] = mapped_column(Boolean, nullable=False)
    blur_score: Mapped[float] = mapped_column(Float, nullable=False)
    perceptual_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    photo: Mapped["Photo"] = relationship("Photo", back_populates="analysis")


class Batch(Base):
    """A batch upload (2-10 photos, TASK-002 block B) sharing one
    `best_photo_id` computed from the deterministic formula
    (tasks/TASK-002/20_design.md §12 step 22). Modeled here in block A
    because `photos.batch_id` FKs into it.
    """

    __tablename__ = "batches"

    batch_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="processing", server_default="processing"
    )
    best_photo_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("photos.photo_id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # Review-1 fix (tasks/TASK-002/40_review-1.md M2): explicit deterministic
    # ordering - without it, `GET /v1/batches/{id}` could return `photos[]`
    # in a different order between calls (no natural order guarantee from
    # the DB), which also weakened the `select_best_photo` tie-break (M1).
    photos: Mapped[list["Photo"]] = relationship(
        "Photo",
        foreign_keys=[Photo.batch_id],
        back_populates="batch",
        order_by="Photo.created_at, Photo.photo_id",
    )

    __table_args__ = (
        Index("ix_batches_created_at", "created_at"),
        CheckConstraint("status IN ('processing', 'completed')", name="ck_batches_status"),
    )
