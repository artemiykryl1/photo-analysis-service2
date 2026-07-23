"""Prometheus metric objects shared by the API and worker processes.

tasks/TASK-002/20_design.md §8. Both `app.main` (API, port 8000) and
`app.worker.main` (worker, port `WORKER_METRICS_PORT`) import this module,
but they run as separate OS processes, so each gets its own independent
`prometheus_client` default registry - there is no cross-process state to
worry about.

Block A (this module) only *declares* the metrics and increments the ones
used by the analysis pipeline (upload, outbox, worker). The `/metrics`
HTTP exposition endpoints and the RED HTTP middleware are TASK-002 block C
(tasks/TASK-002/20_design.md §12 steps 26-27) - out of scope here.
"""

from prometheus_client import Counter, Gauge, Histogram

# --- API metrics (§8.1) ------------------------------------------------

photos_uploaded_total = Counter(
    "photos_uploaded_total",
    "Total number of photos accepted via POST /v1/photos[/batch]",
)

http_requests_total = Counter(
    "http_requests_total",
    "Total HTTP requests",
    ["method", "endpoint", "status"],
)

http_request_duration_seconds = Histogram(
    "http_request_duration_seconds",
    "HTTP request duration in seconds",
    ["method", "endpoint"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
)

storage_upload_errors_total = Counter(
    "storage_upload_errors_total",
    "Total MinIO upload failures during photo create",
)

kafka_publish_errors_total = Counter(
    "kafka_publish_errors_total",
    "Total failures publishing to Kafka from the outbox publisher",
)

photos_pending = Gauge(
    "photos_pending",
    "Number of photos currently in pending status",
)

# --- Worker metrics (§8.2) ----------------------------------------------

photo_analysis_started_total = Counter(
    "photo_analysis_started_total",
    "Total photos for which the worker won the atomic pending->processing claim",
)

photo_analysis_completed_total = Counter(
    "photo_analysis_completed_total",
    "Total photos that reached status=done",
)

# Review-2 fix (tasks/TASK-002/41_review-2.md #3): `reason` is intentionally
# NOT a label on `worker_messages_processed_total` below (kept coarse:
# done/failed/skipped) to avoid duplicating a second, differently-shaped
# breakdown of the same failure - the "why did it fail" dimension lives
# exclusively here, on `photo_analysis_failed_total{reason}`, with exactly
# two values written by `AnalysisProcessor` (analysis_processor.py):
# `no_retry` (permanent/classification error, no retry attempted) and
# `retries_exhausted` (transient error, retried until WORKER_MAX_ATTEMPTS).
# A dashboard that needs failure *reasons* should query this metric, not
# `worker_messages_processed_total{result="failed"}`.
photo_analysis_failed_total = Counter(
    "photo_analysis_failed_total",
    "Total photos that reached status=failed",
    ["reason"],
)

photo_analysis_duration_seconds = Histogram(
    "photo_analysis_duration_seconds",
    "Duration from claim to terminal status (done/failed) in seconds",
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30),
)

analyzer_grpc_errors_total = Counter(
    "analyzer_grpc_errors_total",
    "Total analyzer gRPC call failures",
    ["code"],
)

# `result` is macro-level only (done/failed/skipped) - it does NOT carry
# `photo_id`/`object_key` (cardinality) nor a failure `reason` (see the
# comment above `photo_analysis_failed_total`, which is the metric to use
# for that breakdown).
worker_messages_processed_total = Counter(
    "worker_messages_processed_total",
    "Total Kafka messages processed by the worker, by terminal result",
    ["result"],
)
