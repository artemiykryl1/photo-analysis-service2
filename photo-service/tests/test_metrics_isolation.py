"""Regression coverage for the F4 metrics-isolation invariant
(tasks/TASK-002.1/20_design.md F4).

Before F4, `app.integrations.metrics` was a single module imported by
BOTH the API and the worker process, so every worker import registered
the API-only metrics (`photos_pending`, `http_requests_total`, ...) in
the worker's own Prometheus registry too - exported as always-zero
series, duplicating `photos_pending` in Grafana. The fix splits the
module into `metrics_api`/`metrics_worker`; this file proves the split
actually holds at the level that matters - each entrypoint's real,
transitive import graph.

Each check runs `python -c "..."` in its OWN subprocess. This is
deliberate, not incidental: within a single pytest process, dozens of
OTHER test files already `import app.main` (and several import
`app.services.analysis_processor`, which pulls in `metrics_worker`), so
by the time this test file's own test functions run, BOTH registries
would already be polluted by every other test module's imports - making
an in-process check of "does importing X register Y" meaningless. A
fresh subprocess has an import graph containing ONLY what the one
`import` statement in the snippet actually pulls in.
"""

import subprocess
import sys
from pathlib import Path

PHOTO_SERVICE_ROOT = Path(__file__).resolve().parents[1]

API_ONLY_METRICS = [
    "photos_pending",
    "http_requests_total",
    "http_request_duration_seconds",
    "photos_uploaded_total",
    "storage_upload_errors_total",
    "kafka_publish_errors_total",
]

WORKER_ONLY_METRICS = [
    "photo_analysis_started_total",
    "photo_analysis_completed_total",
    "photo_analysis_failed_total",
    "photo_analysis_duration_seconds",
    "analyzer_grpc_errors_total",
    "worker_messages_processed_total",
    "worker_messages_dropped_total",
    "worker_message_retries_total",
]

_SNIPPET = (
    "import {module}\n"
    "from prometheus_client import REGISTRY, generate_latest\n"
    "print(generate_latest(REGISTRY).decode())\n"
)


def _registry_text_after_importing(module: str) -> str:
    result = subprocess.run(
        [sys.executable, "-c", _SNIPPET.format(module=module)],
        cwd=str(PHOTO_SERVICE_ROOT),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, (
        f"subprocess importing {module!r} failed (exit {result.returncode}):\n"
        f"stdout={result.stdout}\nstderr={result.stderr}"
    )
    return result.stdout


class TestWorkerRegistryExcludesApiMetrics:
    def test_importing_worker_main_registers_only_worker_metrics(self):
        text = _registry_text_after_importing("app.worker.main")

        for name in API_ONLY_METRICS:
            assert name not in text, (
                f"app.worker.main leaked api-only metric {name!r} into its "
                "own Prometheus registry (F4 regression)"
            )
        for name in WORKER_ONLY_METRICS:
            assert name in text, f"app.worker.main is missing its own metric {name!r}"


class TestApiRegistryExcludesWorkerMetrics:
    def test_importing_main_registers_only_api_metrics(self):
        text = _registry_text_after_importing("app.main")

        for name in WORKER_ONLY_METRICS:
            assert name not in text, (
                f"app.main leaked worker-only metric {name!r} into its own "
                "Prometheus registry (F4 regression)"
            )
        for name in API_ONLY_METRICS:
            assert name in text, f"app.main is missing its own metric {name!r}"
