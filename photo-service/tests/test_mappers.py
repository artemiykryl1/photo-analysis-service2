"""Behavioural coverage of `app.services.mappers.analysis_to_response`
(TASK-003 A13/A14, tasks/TASK-003/20_design.md §6.1/§8.3).

This is the single mapper shared by `PhotoService.get_photo`/`list_photos`
(`GET /v1/photos/{id}`, `GET /v1/photos`) and `BatchService.get_batch`
(`GET /v1/batches/{id}`) - it is exercised indirectly by
`test_batch_service.py`, but the four TASK-003 fields
(`eyes_closed_count`, `dominant_color`, `tags`, `model_version`) and the
nullable-old-row case have no dedicated assertions anywhere else, so they
are covered directly here (spec.md criterion: "`GET /v1/photos/{id}` у
done-фото отдаёт eyes_closed_count, dominant_color, tags, model_version";
"старые записи без новых полей не ломают ответ").

A pure function - no session, no I/O - so it is tested directly against
`AnalysisResult` ORM instances built in memory, never touching a database.
"""

from app.db.models import AnalysisResult
from app.services.mappers import analysis_to_response


class TestAnalysisToResponseNone:
    def test_none_input_returns_none(self):
        """Before a photo reaches `status=done` there is no analysis row at
        all - the mapper must pass that through as `None`, not raise or
        synthesize a placeholder object (design §3.3)."""
        assert analysis_to_response(None) is None


class TestAnalysisToResponseNewFields:
    def test_all_four_task_003_fields_are_mapped(self):
        analysis = AnalysisResult(
            faces_count=3,
            is_blurred=False,
            blur_score=930.47,
            perceptual_hash="4b328abfe9f2fe69",
            eyes_closed_count=1,
            dominant_color="#f2fe69",
            tags=["face", "dark", "dominant:#f2fe69"],
            model_version="opencv-dnn-res10-ssd+laplacian+phash/1.1.0",
        )

        response = analysis_to_response(analysis)

        assert response is not None
        assert response.faces_count == 3
        assert response.is_blurred is False
        assert response.blur_score == 930.47
        assert response.perceptual_hash == "4b328abfe9f2fe69"
        assert response.eyes_closed_count == 1
        assert response.dominant_color == "#f2fe69"
        assert response.tags == ["face", "dark", "dominant:#f2fe69"]
        assert response.model_version == "opencv-dnn-res10-ssd+laplacian+phash/1.1.0"

    def test_empty_tags_list_round_trips_as_empty_list_not_none(self):
        analysis = AnalysisResult(
            faces_count=0,
            is_blurred=True,
            blur_score=1.1,
            perceptual_hash="abc123",
            eyes_closed_count=0,
            dominant_color="#000000",
            tags=[],
            model_version="stub/2.0.0",
        )

        response = analysis_to_response(analysis)

        assert response.tags == []


class TestAnalysisToResponseNullableOldRows:
    """TASK-003 A5/D5 (design §6.1/§6.4): the v003 migration adds four
    NULLABLE columns and never backfills existing rows (`ON CONFLICT DO
    NOTHING` stays as-is) - a row written before v003 has all four new
    fields as `NULL`/`None` in the ORM. The mapper (and therefore the HTTP
    response) must not crash or reject that shape."""

    def test_pre_v003_row_with_all_new_fields_none_does_not_raise(self):
        pre_v003_row = AnalysisResult(
            faces_count=2,
            is_blurred=False,
            blur_score=42.0,
            perceptual_hash="oldhash1234567",
            eyes_closed_count=None,
            dominant_color=None,
            tags=None,
            model_version=None,
        )

        response = analysis_to_response(pre_v003_row)

        assert response is not None
        assert response.faces_count == 2
        assert response.eyes_closed_count is None
        assert response.dominant_color is None
        assert response.tags is None
        assert response.model_version is None

    def test_pre_v003_row_serializes_to_json_with_null_new_fields(self):
        """The Pydantic response model must actually be serializable (not
        just constructible) with the new fields absent - this is what a
        real `GET /v1/photos/{id}` response body would look like for an
        old row."""
        pre_v003_row = AnalysisResult(
            faces_count=1,
            is_blurred=True,
            blur_score=0.5,
            perceptual_hash="oldhash",
            eyes_closed_count=None,
            dominant_color=None,
            tags=None,
            model_version=None,
        )

        response = analysis_to_response(pre_v003_row)
        dumped = response.model_dump()

        assert dumped["eyes_closed_count"] is None
        assert dumped["dominant_color"] is None
        assert dumped["tags"] is None
        assert dumped["model_version"] is None
        # old fields are still present and correct
        assert dumped["faces_count"] == 1
        assert dumped["perceptual_hash"] == "oldhash"
