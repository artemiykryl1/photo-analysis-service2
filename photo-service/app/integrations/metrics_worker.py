"""Prometheus metric objects for the worker process (`app.worker.main`,
port `WORKER_METRICS_PORT`).

TASK-002.1 (tasks/TASK-002.1/20_design.md F4): split out of the former
`app.integrations.metrics` monolith - see `app.integrations.metrics_api`
docstring for the full rationale. Only `app.services.analysis_processor`
(and, transitively, `app.worker.main`) may import this module. The API
process must never import it.
"""

from prometheus_client import Counter, Histogram

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
#
# TASK-002.1 review-1 m4: this is a per-*message* counter (one increment per
# successfully-finished delivery of `AnalysisProcessor.process()`), NOT a
# per-*photo* counter - unlike `photo_analysis_completed_total`/
# `photo_analysis_failed_total` above, which describe the fate of the photo
# itself and are incremented once terminal status is committed regardless of
# what happens afterwards. `analysis_processor.py` increments this counter
# only AFTER `_maybe_complete_batch` returns without raising, specifically so
# that a failure there (which leaves the message uncommitted for redelivery)
# does not get double-counted here when the redelivered message re-enters the
# skip/no-op branch. Do not sum this metric across `result` expecting it to
# equal "photos analyzed" - use `photo_analysis_completed_total` +
# `photo_analysis_failed_total` for that.
worker_messages_processed_total = Counter(
    "worker_messages_processed_total",
    "Total Kafka messages processed by the worker, by terminal result",
    ["result"],
)

# TASK-002.1 review-1 M1: poison-pill drops and RETRY-ed (redelivered)
# messages previously incremented zero metrics - only a `logger.warning`/
# `logger.exception` call, which is not something an alert can be built on.
# Both counters below exist specifically to make the accepted trade-offs in
# `app.worker.consumer` (poison-pills are silently skipped; a deterministic
# failure "wedges" a partition, retrying the same message forever) visible
# and alertable:
# - `worker_messages_dropped_total{reason}` - a message was permanently
#   undeliverable and its offset was committed without ever reaching
#   `AnalysisProcessor.process()`. `reason` is a small, fixed set
#   (`unparsable`, `missing_fields`, `invalid_uuid` - no `photo_id`/
#   `object_key`, so no cardinality risk). A sudden burst here usually means
#   a misbehaving producer.
# - `worker_message_retries_total` - `process()` raised an unexpected
#   (non-poison-pill) exception, so the offset was NOT committed and the
#   consumer rewound its position to redeliver the same message. A
#   sustained non-zero rate of this counter (e.g.
#   `increase(worker_message_retries_total[5m]) > 0` staying true across
#   several windows) is the signal that a partition is "stuck" retrying one
#   message instead of making progress - exactly the failure mode this
#   metric is meant to alert on.
worker_messages_dropped_total = Counter(
    "worker_messages_dropped_total",
    "Total Kafka messages permanently dropped (poison-pill) without being processed",
    ["reason"],
)

worker_message_retries_total = Counter(
    "worker_message_retries_total",
    "Total Kafka messages left uncommitted for redelivery after an unexpected "
    "processing error (a sustained non-zero rate indicates a partition stuck "
    "retrying the same message)",
)
