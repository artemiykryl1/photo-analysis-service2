"""Integration test for the v001+v002 migrations against a real, ephemeral
PostgreSQL instance (testcontainers).

This closes the test gap documented in tasks/TASK-000/60_debug.md: the
`create_type=False` fix for the double-`CREATE TYPE` bug (asyncpg
`DuplicateObjectError` on `docker compose up`) could only be caught by
actually *executing* the migration's DDL against a real Postgres engine.
`alembic upgrade head --sql` (offline mode) only prints SQL without
running it, and every other test in this suite mocks the DB entirely -
neither would have caught that regression. This is the only place a real
`alembic upgrade head` / `downgrade` is executed.

TASK-002 (tasks/TASK-002/20_design.md §13.1): extended to also exercise
the v002 revision (`analysis_results`, `batches`, and the seven new
`photos` columns) and the full `v002 -> v001 -> base` downgrade chain.

TASK-003 (tasks/TASK-003/20_design.md §6.3): further extended to exercise
v003 (`analysis_results.eyes_closed_count`/`dominant_color`/`tags`/
`model_version`) and the full `v003 -> v002 -> v001 -> base` downgrade
chain.

Requires a running Docker daemon (spins up a disposable
`postgres:16-alpine` container via testcontainers). Skipped automatically
when Docker is unreachable so the default `pytest` run stays green in
environments without Docker (e.g. this sandbox) - see
tasks/TASK-001/50_tests.md "Как запустить" for how to run it for real.

Run explicitly with Docker available:
    cd photo-service
    uv run pytest -m integration -q
"""

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest

PHOTO_SERVICE_ROOT = Path(__file__).resolve().parent.parent


def _docker_available() -> bool:
    try:
        import docker

        client = docker.from_env()
        client.ping()
        return True
    except Exception:
        return False


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not _docker_available(),
        reason="Docker daemon is not reachable - testcontainers needs it to run this test",
    ),
]


def _run_alembic(args: list[str], database_url: str) -> subprocess.CompletedProcess:
    """Run alembic as a fresh subprocess (not in-process) so
    `app.core.config.get_settings` (which is `@lru_cache`d) always starts
    cold and reads `DATABASE_URL` from this subprocess's own environment -
    see tasks/TASK-001/20_design.md §11.1/§13.
    """
    env = {**os.environ, "DATABASE_URL": database_url}
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=str(PHOTO_SERVICE_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


async def _inspect_v002_schema_present(dsn: str) -> dict:
    """Inspect the full post-`upgrade head` (now v003) schema: extended
    `photos`, `analysis_results` (incl. the v003 extended-analyzer
    columns), `batches`. Function name kept as `_inspect_v002_schema_
    present` for a minimal diff - it now asserts the v003 shape."""
    import asyncpg

    conn = await asyncpg.connect(dsn)
    try:
        photos_exists = await conn.fetchval(
            "select exists (select 1 from information_schema.tables "
            "where table_name = 'photos')"
        )
        photos_columns = await conn.fetch(
            "select column_name from information_schema.columns where table_name = 'photos'"
        )
        photos_column_names = {row["column_name"] for row in photos_columns}

        unique_constraint_count = await conn.fetchval(
            "select count(*) from information_schema.table_constraints "
            "where table_name = 'photos' and constraint_type = 'UNIQUE' "
            "and constraint_name = 'uq_photos_object_key'"
        )

        enum_rows = await conn.fetch(
            "select e.enumlabel from pg_enum e "
            "join pg_type t on e.enumtypid = t.oid "
            "where t.typname = 'photo_status' order by e.enumsortorder"
        )
        enum_labels = [row["enumlabel"] for row in enum_rows]

        analysis_results_exists = await conn.fetchval(
            "select exists (select 1 from information_schema.tables "
            "where table_name = 'analysis_results')"
        )
        analysis_results_columns = await conn.fetch(
            "select column_name from information_schema.columns "
            "where table_name = 'analysis_results'"
        )
        analysis_results_column_names = {
            row["column_name"] for row in analysis_results_columns
        }

        batches_exists = await conn.fetchval(
            "select exists (select 1 from information_schema.tables "
            "where table_name = 'batches')"
        )
        batches_columns = await conn.fetch(
            "select column_name from information_schema.columns where table_name = 'batches'"
        )
        batches_column_names = {row["column_name"] for row in batches_columns}

        index_rows = await conn.fetch(
            "select indexname from pg_indexes where tablename in ('photos', 'batches')"
        )
        index_names = {row["indexname"] for row in index_rows}

        check_rows = await conn.fetch(
            "select constraint_name from information_schema.table_constraints "
            "where table_name in ('photos', 'batches') and constraint_type = 'CHECK' "
            "and constraint_name in ('ck_photos_publish_status', 'ck_batches_status')"
        )
        check_constraint_names = {row["constraint_name"] for row in check_rows}

        return {
            "photos_exists": photos_exists,
            "photos_columns": photos_column_names,
            "unique_constraint_count": unique_constraint_count,
            "enum_labels": enum_labels,
            "analysis_results_exists": analysis_results_exists,
            "analysis_results_columns": analysis_results_column_names,
            "batches_exists": batches_exists,
            "batches_columns": batches_column_names,
            "indexes": index_names,
            "check_constraints": check_constraint_names,
        }
    finally:
        await conn.close()


async def _inspect_photos_columns(dsn: str) -> set:
    import asyncpg

    conn = await asyncpg.connect(dsn)
    try:
        columns = await conn.fetch(
            "select column_name from information_schema.columns where table_name = 'photos'"
        )
        return {row["column_name"] for row in columns}
    finally:
        await conn.close()


async def _inspect_v002_tables_exist(dsn: str) -> tuple:
    import asyncpg

    conn = await asyncpg.connect(dsn)
    try:
        analysis_results_exists = await conn.fetchval(
            "select exists (select 1 from information_schema.tables "
            "where table_name = 'analysis_results')"
        )
        batches_exists = await conn.fetchval(
            "select exists (select 1 from information_schema.tables "
            "where table_name = 'batches')"
        )
        return analysis_results_exists, batches_exists
    finally:
        await conn.close()


async def _inspect_analysis_results_columns(dsn: str) -> set:
    import asyncpg

    conn = await asyncpg.connect(dsn)
    try:
        columns = await conn.fetch(
            "select column_name from information_schema.columns "
            "where table_name = 'analysis_results'"
        )
        return {row["column_name"] for row in columns}
    finally:
        await conn.close()


async def _inspect_schema_absent(dsn: str) -> tuple:
    import asyncpg

    conn = await asyncpg.connect(dsn)
    try:
        table_exists = await conn.fetchval(
            "select exists (select 1 from information_schema.tables "
            "where table_name = 'photos')"
        )
        enum_exists = await conn.fetchval(
            "select exists (select 1 from pg_type where typname = 'photo_status')"
        )
        return table_exists, enum_exists
    finally:
        await conn.close()


class TestMigrationsAgainstRealPostgres:
    def test_upgrade_head_creates_v002_schema_and_downgrade_chain_is_clean(self):
        from testcontainers.postgres import PostgresContainer

        with PostgresContainer("postgres:16-alpine") as postgres:
            # plain DSN (no driver suffix) for asyncpg.connect() verification queries
            raw_dsn = postgres.get_connection_url(driver=None)
            # +asyncpg DSN for alembic/SQLAlchemy (matches app.core.config.Settings.DATABASE_URL format)
            alembic_url = raw_dsn.replace("postgresql://", "postgresql+asyncpg://", 1)

            upgrade = _run_alembic(["upgrade", "head"], alembic_url)
            assert upgrade.returncode == 0, (
                "alembic upgrade head must succeed exactly once against a fresh DB "
                "(a non-zero exit here reproduces the TASK-000 double-CREATE-TYPE bug):\n"
                f"stdout={upgrade.stdout}\nstderr={upgrade.stderr}"
            )

            schema = asyncio.run(_inspect_v002_schema_present(raw_dsn))

            assert schema["photos_exists"] is True
            assert schema["photos_columns"] == {
                # v001
                "photo_id",
                "filename",
                "object_key",
                "user_id",
                "created_at",
                "status",
                # v002 (tasks/TASK-002/20_design.md §2.3)
                "attempts",
                "last_error_code",
                "last_error_message",
                "publish_status",
                "published_at",
                "trace_id",
                "batch_id",
            }
            assert schema["unique_constraint_count"] == 1, (
                "object_key must have exactly one UNIQUE constraint"
            )
            assert schema["enum_labels"] == ["pending", "processing", "done", "failed"]

            assert schema["analysis_results_exists"] is True
            assert schema["analysis_results_columns"] == {
                "photo_id",
                "faces_count",
                "is_blurred",
                "blur_score",
                "perceptual_hash",
                "created_at",
                # v003 (tasks/TASK-003/20_design.md §6.1)
                "eyes_closed_count",
                "dominant_color",
                "tags",
                "model_version",
            }

            assert schema["batches_exists"] is True
            assert schema["batches_columns"] == {
                "batch_id",
                "status",
                "best_photo_id",
                "created_at",
            }

            assert {
                "ix_photos_batch_id",
                "ix_photos_status",
                "ix_photos_publish_status",
                "ix_batches_created_at",
            } <= schema["indexes"]

            assert schema["check_constraints"] == {
                "ck_photos_publish_status",
                "ck_batches_status",
            }

            # downgrade head (v003) -> v002: the four extended-analyzer
            # columns are gone, everything else (v002 shape) unchanged.
            downgrade_to_v002 = _run_alembic(["downgrade", "v002"], alembic_url)
            assert downgrade_to_v002.returncode == 0, (
                f"alembic downgrade v003 -> v002 must succeed cleanly:\n"
                f"stdout={downgrade_to_v002.stdout}\nstderr={downgrade_to_v002.stderr}"
            )
            analysis_results_columns_after_v002 = asyncio.run(
                _inspect_analysis_results_columns(raw_dsn)
            )
            assert analysis_results_columns_after_v002 == {
                "photo_id",
                "faces_count",
                "is_blurred",
                "blur_score",
                "perceptual_hash",
                "created_at",
            }

            # downgrade v002 -> v001: new tables/columns gone, v001 shape restored
            downgrade_to_v001 = _run_alembic(["downgrade", "v001"], alembic_url)
            assert downgrade_to_v001.returncode == 0, (
                f"alembic downgrade v002 -> v001 must succeed cleanly:\n"
                f"stdout={downgrade_to_v001.stdout}\nstderr={downgrade_to_v001.stderr}"
            )

            photos_columns_after_v001 = asyncio.run(_inspect_photos_columns(raw_dsn))
            assert photos_columns_after_v001 == {
                "photo_id",
                "filename",
                "object_key",
                "user_id",
                "created_at",
                "status",
            }
            analysis_results_exists, batches_exist = asyncio.run(
                _inspect_v002_tables_exist(raw_dsn)
            )
            assert analysis_results_exists is False, (
                "downgrade to v001 must drop analysis_results"
            )
            assert batches_exist is False, "downgrade to v001 must drop batches"

            # downgrade v001 -> base: full teardown (same contract as TASK-001)
            downgrade_to_base = _run_alembic(["downgrade", "base"], alembic_url)
            assert downgrade_to_base.returncode == 0, (
                f"alembic downgrade base must succeed cleanly:\n"
                f"stdout={downgrade_to_base.stdout}\nstderr={downgrade_to_base.stderr}"
            )

            table_exists_after, enum_exists_after = asyncio.run(
                _inspect_schema_absent(raw_dsn)
            )
            assert table_exists_after is False, "downgrade must drop the photos table"
            assert enum_exists_after is False, "downgrade must drop the photo_status enum type"
