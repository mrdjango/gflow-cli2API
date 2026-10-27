# SPDX-License-Identifier: MIT
"""The REST task API drives the same queue as the MCP tools. Only FlowApiClient is faked."""

from __future__ import annotations

import base64
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest
from pydantic import SecretStr
from starlette.applications import Starlette

from gflow_cli import rest
from gflow_cli.data.store import DataStore
from gflow_cli.mcp import tasks_extension
from gflow_cli.mcp.server import _BearerAuthMiddleware  # pyright: ignore[reportPrivateUsage]

from .test_wait_false_runs import _finish, db, wired  # pyright: ignore[reportPrivateUsage]

__all__ = ["db", "wired"]  # fixtures, re-exported for pytest


@pytest.fixture()
async def api(
    db: DataStore, wired: dict[str, Any], tmp_path: Path
) -> AsyncIterator[httpx.AsyncClient]:
    settings = MagicMock(
        resolved_db_path=lambda: db.path,
        output_dir=tmp_path / "out",
        daemon_token=SecretStr("tok"),
    )
    with patch("gflow_cli.rest.get_settings", return_value=settings):
        transport = httpx.ASGITransport(app=Starlette(routes=rest.routes))
        async with httpx.AsyncClient(transport=transport, base_url="http://gflow") as client:
            yield client


async def test_an_image_task_runs_and_serves_its_file(api: httpx.AsyncClient) -> None:
    created = await api.post("/v1/tasks", json={"model": "nano-banana-2", "prompt": "a lighthouse"})
    assert created.status_code == 202, created.text
    task = created.json()
    assert task["status"] in ("queued", "in_progress")
    assert task["model"] == "nano-banana-2"

    await _finish(task["id"])
    done = (await api.get(f"/v1/tasks/{task['id']}")).json()
    assert done["status"] == "completed", done
    (output,) = done["outputs"]
    assert output["type"] == "image"
    assert output["url"].startswith(f"http://gflow/v1/tasks/{task['id']}/content/0?expires=")
    content = await api.get(output["url"])
    assert content.content == b"PNG_FAKE"


async def test_images_generations_waits_and_answers_b64(api: httpx.AsyncClient) -> None:
    reply = await api.post(
        "/v1/images/generations",
        json={"model": "nano-banana-pro", "prompt": "a", "size": "1792x1024"},
    )
    assert reply.status_code == 200, reply.text
    assert base64.b64decode(reply.json()["data"][0]["b64_json"]) == b"PNG_FAKE"


async def test_a_video_model_on_the_images_endpoint_never_generates(
    api: httpx.AsyncClient, wired: dict[str, Any]
) -> None:
    reply = await api.post("/v1/images/generations", json={"model": "veo-3.1-fast", "prompt": "a"})
    assert reply.status_code == 400
    wired["client"].generate_video.assert_not_called()


@pytest.mark.parametrize(
    ("body", "status"),
    [
        ({"model": "nope", "prompt": "a"}, 404),
        ({"model": "nano-banana-2", "prompt": " "}, 400),
        ({"model": "nano-banana-2", "prompt": "a", "image": "http://example.com/a.png"}, 400),
        ({"model": "nano-banana-2", "prompt": "a", "image": "https://localhost/a.png"}, 400),
        ({"model": "nano-banana-2", "prompt": "a", "size": "big"}, 400),
    ],
)
async def test_bad_requests_are_errors_not_200(
    api: httpx.AsyncClient, body: dict[str, Any], status: int
) -> None:
    reply = await api.post("/v1/tasks", json=body)
    assert reply.status_code == status, reply.text
    assert reply.json()["error"]["message"]
    assert not tasks_extension.DETACHED


async def test_a_data_uri_image_becomes_a_reference(api: httpx.AsyncClient, db: DataStore) -> None:
    png = "data:image/png;base64," + base64.b64encode(b"\x89PNG fake").decode()
    created = await api.post(
        "/v1/tasks", json={"model": "nano-banana-2", "prompt": "a", "image": [{"url": png}]}
    )
    assert created.status_code == 202, created.text
    await _finish(created.json()["id"])
    (task_type,) = db.conn.execute("SELECT task_type FROM generation_queue").fetchone()
    assert task_type == "i2i"


async def test_unknown_task_is_404(api: httpx.AsyncClient) -> None:
    assert (await api.get("/v1/tasks/missing")).status_code == 404


async def test_models_lists_both_kinds(api: httpx.AsyncClient) -> None:
    kinds = {m["kind"] for m in (await api.get("/v1/models")).json()["data"]}
    assert kinds == {"image", "video"}


@pytest.mark.parametrize(
    ("value", "aspect"),
    [("1280x720", "16:9"), ("720x1280", "9:16"), ("16:9", "16:9"), ("", "9:16")],
)
def test_sizes_map_to_the_nearest_aspect(value: str, aspect: str) -> None:
    assert rest._aspect({"size": value}, ("9:16", "16:9"), "9:16") == aspect  # pyright: ignore[reportPrivateUsage]


async def test_a_signed_content_url_is_its_own_credential(api: httpx.AsyncClient) -> None:
    created = (await api.post("/v1/tasks", json={"model": "nano-banana-2", "prompt": "a"})).json()
    await _finish(created["id"])
    url = (await api.get(f"/v1/tasks/{created['id']}")).json()["outputs"][0]["url"]

    app = _BearerAuthMiddleware(Starlette(routes=rest.routes), token="tok")
    transport = httpx.ASGITransport(app=app)  # pyright: ignore[reportArgumentType]
    async with httpx.AsyncClient(transport=transport, base_url="http://gflow") as anon:
        assert (await anon.get(url)).content == b"PNG_FAKE"
        assert (await anon.get(url[:-1] + ("0" if url[-1] != "0" else "1"))).status_code == 401
        assert (await anon.get(f"/v1/tasks/{created['id']}")).status_code == 401
        path, query = url.removeprefix("http://gflow").split("?")
        expired = query.replace(query.split("&")[0], "expires=1")
        assert (await anon.get(f"{path}?{expired}")).status_code == 401


async def test_veo_never_pins_a_duration(api: httpx.AsyncClient, wired: dict[str, Any]) -> None:
    bad = await api.post("/v1/tasks", json={"model": "veo-3.1-lite", "prompt": "a", "seconds": 4})
    assert bad.status_code == 400
    created = await api.post(
        "/v1/tasks", json={"model": "veo-3.1-lite", "prompt": "a", "seconds": 8}
    )
    assert created.status_code == 202, created.text
    await _finish(created.json()["id"])
    assert wired["client"].generate_video.call_args.kwargs["req"].duration is None


async def test_output_urls_use_the_public_url(
    api: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GFLOW_CLI_PUBLIC_URL", "https://flow-media.example")
    created = (await api.post("/v1/tasks", json={"model": "nano-banana-2", "prompt": "a"})).json()
    await _finish(created["id"])
    url = (await api.get(f"/v1/tasks/{created['id']}")).json()["outputs"][0]["url"]
    assert url.startswith(f"https://flow-media.example/v1/tasks/{created['id']}/content/0?")
