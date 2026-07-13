"""SQLAlchemy declarative models.

Only the `photos` table is modeled in TASK-000 - it is the minimum needed
to give Alembic a non-empty initial revision and to validate that the
vertical slice (API -> DB) actually works end to end. `analysis_results`
belongs to TASK-003 (result-service scope) and is intentionally NOT added
here yet.
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
    queued = "queued"
    analyzing = "analyzing"
    done = "done"
    error = "error"


class Photo(Base):
    __tablename__ = "photos"

    photo_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[str] = mapped_column(String(255), nullable=False)
    s3_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    status: Mapped[PhotoStatus] = mapped_column(
        SAEnum(PhotoStatus, name="photo_status"),
        default=PhotoStatus.queued,
        nullable=False,
    )

    __table_args__ = (Index("ix_photos_user_uploaded", "user_id", "uploaded_at"),)
