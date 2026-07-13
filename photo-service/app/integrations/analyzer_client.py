"""Analyzer integration stub.

The actual photo analysis is performed by an external Analyzer Service,
consumed asynchronously via Kafka (`photos-to-analyze` topic, see
constitution.md §2.4). Kafka is entirely out of scope for TASK-000 (no
producer/consumer configured here, no client instantiated).

This module deliberately contains NO implementation and is NOT imported
by any other module yet. It only documents the future interface so the
integrations/ layer boundary is visible in the directory structure.

TODO(TASK-002): implement a Kafka producer wrapper here, e.g.:

    async def request_analysis(photo_id: str, s3_path: str, user_id: str,
                                uploaded_at: str, trace_id: str) -> None:
        \"\"\"Publish a message to `photos-to-analyze` (key=photo_id).\"\"\"
        ...
"""
