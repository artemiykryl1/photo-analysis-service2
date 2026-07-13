"""HTTP router for the photos domain.

TASK-000 scope: no routes yet. `POST /api/v1/photos` (upload) is TASK-001,
`GET /api/v1/photos` (list) is TASK-004. Kept as an empty router (rather
than a placeholder route returning 501) to avoid introducing a fake
contract before it is actually designed.

`healthz`/`readyz` are infrastructure endpoints, not part of the photos
domain, and live in `main.py`.

Rule for reviewers: this module must never import SQLAlchemy models,
`select`, or `session.execute`, and must never import `app.integrations.storage`
for direct MinIO calls - it may only call into `services/`.
"""

from fastapi import APIRouter

router = APIRouter(prefix="/api/v1/photos", tags=["photos"])
