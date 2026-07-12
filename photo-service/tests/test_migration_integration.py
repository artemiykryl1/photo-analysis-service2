"""Integration test for the v001 migration against a real, ephemeral
PostgreSQL instance (testcontainers).

This closes the test gap documented in tasks/TASK-000/60_debug.md: the
`create_type=False` fix for the double-`CREATE TYPE` bug (asyncpg
`DuplicateObjectError` on `docker compose up`) could only be caught by
actually *executing* the migration's DDL against a real Postgres engine.
`alembic upgrade head --sql` (offline mode) only prints SQL without
running it, and every other test in this suite mocks the DB entirely -
neither would have caught that regression. This is the only place a real
`alembic upgrade head` / `downgrade base` is executed.

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


async def _inspect_schema_present(dsn: str) -> tuple:
    import asyncpg

    conn = await asyncpg.connect(dsn)
    try:
        table_exists = await conn.fetchval(
            "select exists (select 1 from information_schema.tables "
            "where table_name = 'photos')"
        )
        columns = await conn.fetch(
            "select column_name from information_schema.columns where table_name = 'photos'"
        )
        column_names = {row["column_name"] for row in columns}

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

        return table_exists, column_names, unique_constraint_count, enum_labels
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


class TestV001MigrationAgainstRealPostgres:
    def test_upgrade_head_creates_schema_exactly_once_and_downgrade_is_clean(self):
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

            table_exists, column_names, unique_count, enum_labels = asyncio.run(
                _inspect_schema_present(raw_dsn)
            )

            assert table_exists is True
            assert column_names == {
                "photo_id",
                "filename",
                "object_key",
                "user_id",
                "created_at",
                "status",
            }
            assert unique_count == 1, "object_key must have exactly one UNIQUE constraint"
            assert enum_labels == ["pending", "processing", "done", "failed"]

            downgrade = _run_alembic(["downgrade", "base"], alembic_url)
            assert downgrade.returncode == 0, (
                f"alembic downgrade base must succeed cleanly:\n"
                f"stdout={downgrade.stdout}\nstderr={downgrade.stderr}"
            )

            table_exists_after, enum_exists_after = asyncio.run(
                _inspect_schema_absent(raw_dsn)
            )
            assert table_exists_after is False, "downgrade must drop the photos table"
            assert enum_exists_after is False, "downgrade must drop the photo_status enum type"
