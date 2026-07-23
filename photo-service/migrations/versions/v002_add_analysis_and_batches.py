"""add analysis_results, batches and photos analysis-pipeline columns

Revision ID: v002
Revises: v001
Create Date: 2026-07-17 00:00:00

TASK-002 (tasks/TASK-002/20_design.md §2): adds the async analysis
pipeline schema on top of v001's `photos` table. No new PostgreSQL enum
types are introduced (lesson from TASK-000/60_debug.md - the double
`CREATE TYPE` bug); the new `photos.publish_status` and `batches.status`
columns are plain `VARCHAR` + `CHECK`, not `postgresql.ENUM`.

Ordering (must not change - see design §2, "Порядок в upgrade()"):
  1. create `analysis_results` (FK -> photos, already exists from v001)
  2. create `batches` (+ index) - its `best_photo_id` FK references the
     already-existing `photos` table
  3. ALTER `photos` ADD COLUMNS (including `batch_id` FK -> the just
     created `batches` table)
  4. CHECK constraints
  5. indexes

`downgrade()` reverses this: drop indexes and the new `photos` columns
first (this also drops the `batch_id` FK and the publish_status CHECK),
then `batches`, then `analysis_results`.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "v002"
down_revision: Union[str, None] = "v001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. analysis_results (1:1 with photos; photo_id is both PK and FK)
    op.create_table(
        "analysis_results",
        sa.Column("photo_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("faces_count", sa.Integer(), nullable=False),
        sa.Column("is_blurred", sa.Boolean(), nullable=False),
        sa.Column("blur_score", sa.Float(), nullable=False),
        sa.Column("perceptual_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["photo_id"],
            ["photos.photo_id"],
            name="fk_analysis_results_photo_id_photos",
            ondelete="CASCADE",
        ),
    )

    # 2. batches (best_photo_id references photos, which already exists)
    op.create_table(
        "batches",
        sa.Column("batch_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "status", sa.String(length=20), nullable=False, server_default="processing"
        ),
        sa.Column("best_photo_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["best_photo_id"],
            ["photos.photo_id"],
            name="fk_batches_best_photo_id_photos",
        ),
        sa.CheckConstraint(
            "status IN ('processing', 'completed')", name="ck_batches_status"
        ),
    )
    op.create_index("ix_batches_created_at", "batches", ["created_at"])

    # 3. ALTER photos ADD COLUMNS (batch_id FK -> batches, just created above)
    op.add_column(
        "photos",
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "photos", sa.Column("last_error_code", sa.String(length=64), nullable=True)
    )
    op.add_column("photos", sa.Column("last_error_message", sa.Text(), nullable=True))
    op.add_column(
        "photos",
        sa.Column(
            "publish_status",
            sa.String(length=20),
            nullable=False,
            server_default="not_sent",
        ),
    )
    op.add_column(
        "photos",
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("photos", sa.Column("trace_id", sa.String(length=64), nullable=True))
    op.add_column(
        "photos",
        sa.Column("batch_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_photos_batch_id_batches",
        "photos",
        "batches",
        ["batch_id"],
        ["batch_id"],
    )

    # 4. CHECK constraints
    op.create_check_constraint(
        "ck_photos_publish_status",
        "photos",
        "publish_status IN ('not_sent', 'sent')",
    )

    # 5. indexes
    op.create_index("ix_photos_batch_id", "photos", ["batch_id"])
    op.create_index("ix_photos_status", "photos", ["status"])
    op.create_index(
        "ix_photos_publish_status",
        "photos",
        ["publish_status"],
        postgresql_where=sa.text("publish_status = 'not_sent'"),
    )


def downgrade() -> None:
    # 5/4. drop photos indexes and CHECK
    op.drop_index("ix_photos_publish_status", table_name="photos")
    op.drop_index("ix_photos_status", table_name="photos")
    op.drop_index("ix_photos_batch_id", table_name="photos")
    op.drop_constraint("ck_photos_publish_status", "photos", type_="check")

    # 3. drop photos columns (batch_id drop also removes its FK)
    op.drop_constraint("fk_photos_batch_id_batches", "photos", type_="foreignkey")
    op.drop_column("photos", "batch_id")
    op.drop_column("photos", "trace_id")
    op.drop_column("photos", "published_at")
    op.drop_column("photos", "publish_status")
    op.drop_column("photos", "last_error_message")
    op.drop_column("photos", "last_error_code")
    op.drop_column("photos", "attempts")

    # 2. batches
    op.drop_index("ix_batches_created_at", table_name="batches")
    op.drop_table("batches")

    # 1. analysis_results
    op.drop_table("analysis_results")
