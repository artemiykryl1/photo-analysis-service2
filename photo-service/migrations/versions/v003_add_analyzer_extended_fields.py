"""add extended analyzer fields to analysis_results

Revision ID: v003
Revises: v002
Create Date: 2026-07-21 00:00:00

TASK-003 A5/A11 (tasks/TASK-003/20_design.md §6): the real analyzer
(package `analyzer.v1`, see `protos/analyzer.proto`) returns four extra
fields the v002 schema had no room for: `eyes_closed_count`,
`dominant_color`, `tags`, `model_version`. All four are nullable and
without a `server_default` - rows written before this migration never had
an analyzer that returned this data, and NULL is the semantically correct
value for "this analyzer version didn't provide this", not something to
backfill (design §6.4). No new PostgreSQL enum type is introduced (lesson
from TASK-000/60_debug.md - the double `CREATE TYPE` bug); `tags` uses
JSONB rather than `postgresql.ARRAY` (design §6.2 - decoded as a plain
Python `list[str]` by asyncpg/SQLAlchemy with no extra dialect import).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "v003"
down_revision: Union[str, None] = "v002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "analysis_results",
        sa.Column("eyes_closed_count", sa.Integer(), nullable=True),
    )
    op.add_column(
        "analysis_results",
        sa.Column("dominant_color", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "analysis_results",
        sa.Column("tags", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "analysis_results",
        sa.Column("model_version", sa.String(length=128), nullable=True),
    )


def downgrade() -> None:
    # Reverse order of upgrade().
    op.drop_column("analysis_results", "model_version")
    op.drop_column("analysis_results", "tags")
    op.drop_column("analysis_results", "dominant_color")
    op.drop_column("analysis_results", "eyes_closed_count")
