"""Shared ORM -> Pydantic response mappers with no other dependencies.

TASK-002.1 (tasks/TASK-002.1/20_design.md F3-развязка): `analysis_to_response`
used to live in `app.services.photo_service` (as `_analysis_response`) and
was imported from there by `app.services.batch_service`. That import chain
meant `batch_service` transitively pulled in `photo_service`, which imports
`app.integrations.metrics_api` - and `analysis_processor` (worker-side)
needs `select_best_photo` from `batch_service` to finish a batch (F3). Left
as-is, that would have re-introduced the F4 bug (worker importing api-only
metrics). Moving the mapper to this dependency-free module lets
`batch_service` avoid importing `photo_service` at all, so the worker's
import graph (`analysis_processor -> batch_service -> {mappers,
batch_repository, errors, schemas, models}`) never touches `metrics_api`.
"""

from app.db.models import AnalysisResult
from app.schemas.photos import AnalysisResultResponse


def analysis_to_response(analysis: AnalysisResult | None) -> AnalysisResultResponse | None:
    """Map the ORM `AnalysisResult` (if any) to its Pydantic response
    shape. `None` until the photo reaches `status=done` (design §3.3)."""
    if analysis is None:
        return None
    return AnalysisResultResponse(
        faces_count=analysis.faces_count,
        is_blurred=analysis.is_blurred,
        blur_score=analysis.blur_score,
        perceptual_hash=analysis.perceptual_hash,
    )
