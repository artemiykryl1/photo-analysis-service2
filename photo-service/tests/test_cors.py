"""CORS smoke tests (TASK-003 B1, tasks/TASK-003/20_design.md §8.5).

`Settings.CORS_ALLOWED_ORIGINS` defaults to
`http://localhost:8080,http://localhost:5173`, so a preflight from one of
those origins must succeed with the expected headers, while a preflight
from an unlisted origin must NOT receive `access-control-allow-origin`
(Starlette's `CORSMiddleware` still returns 200 for the preflight itself,
it just omits the allow headers - the browser is the one that then blocks
the real request).

Full test coverage of this module is `test-writer`'s job per the pipeline;
this file only closes the specific "Готово" criterion from design B-1.
"""


async def test_preflight_with_allowed_origin_gets_cors_headers(client):
    response = await client.options(
        "/v1/photos",
        headers={
            "Origin": "http://localhost:8080",
            "Access-Control-Request-Method": "POST",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:8080"


async def test_preflight_with_disallowed_origin_has_no_cors_headers(client):
    response = await client.options(
        "/v1/photos",
        headers={
            "Origin": "http://evil.example",
            "Access-Control-Request-Method": "POST",
        },
    )

    assert "access-control-allow-origin" not in response.headers


async def test_simple_request_from_allowed_origin_echoes_allow_origin(client):
    response = await client.get("/healthz", headers={"Origin": "http://localhost:8080"})

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:8080"


async def test_cors_does_not_allow_credentials(client):
    """design §8.5: `allow_credentials=False` - no auth exists yet, so this
    must stay explicit rather than silently becoming `True`."""
    response = await client.options(
        "/v1/photos",
        headers={
            "Origin": "http://localhost:8080",
            "Access-Control-Request-Method": "POST",
        },
    )

    assert "access-control-allow-credentials" not in response.headers
