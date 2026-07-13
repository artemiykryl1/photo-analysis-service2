"""SQLAlchemy declarative models.

Only the `photos` table is modeled - it is the minimum needed to give
Alembic a non-empty initial revision and to validate that the vertical
slice (API -> DB -> MinIO) actually works end to end. `analysis_results`
belongs to TASK-003 (result-service scope) and is intentionally NOT added
here yet.

TASK-001: enum values renamed to pending/processing/done/failed,
`s3_path` renamed to `object_key` (UNIQUE, NOT NULL), `filename` added,
`uploaded_at` renamed to `created_at`, `user_id` made nullable (no JWT
in this workshop scope - see tasks/TASK-001/20_design.md §0/§3).
"""

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum as SAEnum, Index, String, func
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


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

    __table_args__ = (Index("ix_photos_created_at", "created_at"),)
