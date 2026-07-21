"""Prometheus metric objects for the API process (`app.main`, port 8000).

TASK-002.1 (tasks/TASK-002.1/20_design.md F4): split out of the former
`app.integrations.metrics` monolith, which was imported by BOTH the API
and the worker process. Because each process gets its own independent
`prometheus_client` default registry, importing the monolith into the
worker registered these API-only metrics there too (exported as zeros) -
duplicating `photos_pending` in Grafana (one real series from the API,
one always-zero series from the worker). Only `app.main`,
`app.api.metrics_middleware`, `app.services.photo_service` and
`app.services.outbox` (API-side collaborators) may import this module.
`app.worker.*` must never import it - see the F4 "импорт-гигиена"
invariant in the design doc.
"""

from prometheus_client import Counter, Gauge, Histogram

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
