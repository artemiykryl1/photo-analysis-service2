"""init photos

Revision ID: v001
Revises:
Create Date: 2026-07-11 00:00:00

Written by hand (rather than via autogenerate) to keep the enum creation
deterministic - see 20_design.md §6 risk note on Postgres enum autogenerate.
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
    "queued", "analyzing", "done", "error", name="photo_status", create_type=False
)


def upgrade() -> None:
    bind = op.get_bind()
    photo_status_enum.create(bind, checkfirst=True)

    op.create_table(
        "photos",
        sa.Column("photo_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", sa.String(length=255), nullable=False),
        sa.Column("s3_path", sa.String(length=1024), nullable=False),
        sa.Column(
            "uploaded_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "status",
            photo_status_enum,
            nullable=False,
            server_default="queued",
        ),
    )
    op.create_index(
        "ix_photos_user_uploaded", "photos", ["user_id", "uploaded_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_photos_user_uploaded", table_name="photos")
    op.drop_table("photos")
    photo_status_enum.drop(op.get_bind(), checkfirst=True)
