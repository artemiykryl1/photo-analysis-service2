"""Worker package: Kafka consumer for `photo.analysis.requested`.

Entrypoint: `python -m app.worker.main` (see docker-compose.yml `worker`
service `command`). Runs as a separate process/container from the API,
sharing the same image (tasks/TASK-002/20_design.md §5.1).
"""
