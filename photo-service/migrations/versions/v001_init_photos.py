"""init photos

Revision ID: v001
Revises:
Create Date: 2026-07-11 00:00:00

Written by hand (rather than via autogenerate) to keep the enum creation
deterministic - see 20_design.md §6 risk note on Postgres enum autogenerate.

TASK-001: edited in place (NOT a new v002) since PR #2 was not merged and
there is no production data yet - see tasks/TASK-001/20_design.md §3.3.
Enum values renamed to pending/processing/done/failed, `s3_path` renamed
to `object_key` (UNIQUE, NOT NULL), `filename` column added, `uploaded_at`
renamed to `created_at`, `user_id` made nullable, index renamed to
`ix_photos_created_at`. `create_type=False` is preserved (fix from
TASK-000/60_debug - otherwise DuplicateObjectError reappears).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "v001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

photo_status_enum = postgresql.ENUM(
    "pending", "processing", "done", "failed", name="photo_status", create_type=False
)


def upgrade() -> None:
    bind = op.get_bind()
    photo_status_enum.create(bind, checkfirst=True)

    op.create_table(
        "photos",
        sa.Column("photo_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("filename", sa.String(length=512), nullable=False),
        sa.Column("object_key", sa.String(length=1024), nullable=False),
        sa.Column("user_id", sa.String(length=255), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "status",
            photo_status_enum,
            nullable=False,
            server_default="pending",
        ),
        sa.UniqueConstraint("object_key", name="uq_photos_object_key"),
    )
    op.create_index("ix_photos_created_at", "photos", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_photos_created_at", table_name="photos")
    op.drop_table("photos")
    photo_status_enum.drop(op.get_bind(), checkfirst=True)
