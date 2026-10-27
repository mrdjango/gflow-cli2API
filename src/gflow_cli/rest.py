# SPDX-License-Identifier: MIT
"""REST task API served beside MCP by ``gflow serve`` (models-gateway, OpenAI clients).

* ``POST /v1/tasks`` queues an image or video generation and answers its task at once;
  ``GET /v1/tasks/{id}`` reports it and ``GET /v1/tasks/{id}/content/{n}`` streams output n.
* ``POST /v1/images/generations`` is the OpenAI Images API: it waits and answers b64_json.
* ``GET /v1/models`` lists the public model names below.

Every generation goes through the MCP generate tools, so validation, the rate limit,
the per-profile browser lock and the queue's spend-safe recovery are the same ones.
Auth is the daemon's bearer middleware, which wraps this router too.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import ipaddress
import re
import socket
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs, urlparse

import httpx
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Route

from gflow_cli.config import get_settings
from gflow_cli.data.store import DataStore
from gflow_cli.mcp import tools
from gflow_cli.mcp.server import no_spend_active
from gflow_cli.worker.queue import QueueRepository

# Public model name -> (kind, gflow CLI alias).
MODELS: dict[str, tuple[str, str]] = {
    "nano-banana-2": ("image", "nano2"),
    "nano-banana-2-lite": ("image", "nano2-lite"),
    "nano-banana-pro": ("image", "nano-pro"),
    "imagen-4": ("image", "image4"),
    "veo-3.1-fast": ("video", "veo_fast"),
    "veo-3.1-quality": ("video", "veo_quality"),
    "veo-3.1-lite": ("video", "veo_lite"),
    "veo-3.1-lite-lower-priority": ("video", "veo_lite_lp"),
    "omni-flash": ("video", "omni_flash"),
}
_PUBLIC_NAME = {alias: name for name, (_, alias) in MODELS.items()}

_IMAGE_ASPECTS = ("1:1", "9:16", "16:9", "4:3", "3:4")
_VIDEO_ASPECTS = ("9:16", "16:9")
_STATUS = {
    "pending": "queued",
    "processing": "in_progress",
    "completed": "completed",
    "failed": "failed",
    "indeterminate": "failed",
}
_CONTENT_PATH = re.compile(r"/v1/tasks/[\w-]+/content(/\d+)?")
_MAX_INPUT_BYTES = 20 * 1024 * 1024
_MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}


class ApiError(Exception):
    def __init__(self, status: int, message: str, code: str = "invalid_request_error") -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.code = code


def _error(status: int, message: str, code: str) -> JSONResponse:
    # OpenAI error envelope, never a 200: a gateway must see a failure as one.
    return JSONResponse(
        {"error": {"message": message, "type": code, "code": code}}, status_code=status
    )


def _tool_error(reply: dict[str, Any]) -> ApiError:
    err: Any = reply.get("error") or {}
    if isinstance(err, str):
        return ApiError(500, err, "server_error")
    status = int(err.get("status") or 500)
    message = str(err.get("detail") or err.get("title") or "generation failed")
    return ApiError(
        status, message, "rate_limit_exceeded" if status == 429 else "invalid_request_error"
    )


def _aspect(body: dict[str, Any], allowed: tuple[str, ...], default: str) -> str:
    value = str(body.get("aspect_ratio") or body.get("size") or "").strip().lower()
    if not value or value == "auto":
        return default
    if value in allowed:
        return value
    w, _, h = value.partition("x")
    if not (w.isdigit() and h.isdigit() and int(w) and int(h)):
        raise ApiError(400, f"aspect_ratio must be one of {list(allowed)} or a WxH size")
    ratio = int(w) / int(h)
    return min(allowed, key=lambda a: abs(int(a.split(":")[0]) / int(a.split(":")[1]) - ratio))


def _public_host(host: str) -> None:
    # The gateway forwards end-user URLs: refuse anything that resolves inside our network.
    # shortcut: resolved again by httpx (DNS-rebinding window); pin the IP if exposed wider.
    try:
        infos = socket.getaddrinfo(host, 443)
    except OSError as exc:
        raise ApiError(400, f"cannot resolve image host {host!r}") from exc
    for info in infos:
        if not ipaddress.ip_address(info[4][0]).is_global:
            raise ApiError(400, "image URLs must point at a public host")


async def _save_input(value: str, folder: Path) -> str:
    """Write one image input (https URL or data URI) to ``folder`` and return its path."""
    value = value.strip()
    if value.startswith("data:"):
        header, _, data = value.partition(",")
        if ";base64" not in header:
            raise ApiError(400, "data URIs must be base64")
        try:
            content = base64.b64decode(data, validate=True)
        except binascii.Error as exc:
            raise ApiError(400, "invalid base64 image data") from exc
        suffix = "." + header[5:].split(";")[0].split("/")[-1]
    else:
        url = urlparse(value)
        if url.scheme != "https" or not url.hostname:
            raise ApiError(400, "images must be https URLs or base64 data URIs")
        _public_host(url.hostname)
        try:
            async with (
                httpx.AsyncClient(timeout=30) as http,
                http.stream("GET", value) as resp,
            ):
                resp.raise_for_status()
                content = b""
                async for chunk in resp.aiter_bytes():
                    content += chunk
                    if len(content) > _MAX_INPUT_BYTES:
                        raise ApiError(400, "image input is larger than 20 MB")
        except httpx.HTTPError as exc:
            raise ApiError(400, f"could not fetch image: {type(exc).__name__}") from exc
        suffix = Path(url.path).suffix
    if len(content) > _MAX_INPUT_BYTES:
        raise ApiError(400, "image input is larger than 20 MB")
    suffix = suffix.lower() if suffix.lower() in _MIME else ".png"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{uuid.uuid4().hex}{suffix}"
    path.write_bytes(content)
    return str(path)


def _images(body: dict[str, Any]) -> list[str]:
    found: list[str] = []
    for key in ("image", "images", "input_reference"):
        value = body.get(key)
        items = cast("list[Any]", value if isinstance(value, list) else [value])
        for item in items:
            if isinstance(item, dict):
                ref = cast("dict[str, Any]", item)
                item = ref.get("url") or ref.get("image_url")
            if isinstance(item, str) and item.strip() and item not in found:
                found.append(item)
    return found


def _int(body: dict[str, Any], key: str, default: int | None) -> int | None:
    value = body.get(key, default)
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ApiError(400, f"{key} must be an integer") from exc


async def _submit(raw: Any, *, wait: bool) -> dict[str, Any]:
    """Run the MCP generate tool for ``body``; answers the tool's reply or raises ApiError."""
    if not isinstance(raw, dict):
        raise ApiError(400, "request body must be a JSON object")
    body = cast("dict[str, Any]", raw)
    model = str(body.get("model") or "").strip()
    if model not in MODELS:
        raise ApiError(404, f"unknown model {model!r}; see GET /v1/models", "model_not_found")
    prompt = str(body.get("prompt") or "").strip()
    if not prompt:
        raise ApiError(400, "prompt is required")
    if no_spend_active():
        raise ApiError(403, "generation is disabled on this server (no-spend mode)", "forbidden")
    kind, alias = MODELS[model]
    folder = get_settings().output_dir / "api-inputs" / uuid.uuid4().hex
    images = [await _save_input(v, folder) for v in _images(body)]
    if kind == "image":
        reply = await tools.gflow_generate_image(
            prompt=prompt,
            model=alias,
            aspect=_aspect(body, _IMAGE_ASPECTS, "1:1"),
            count=_int(body, "n", 1) or 1,
            reference_images=images or None,
            wait=wait,
        )
    else:
        mode = "t2v"
        if images:
            mode = "r2v" if str(body.get("mode") or "").lower() in ("r2v", "reference") else "i2v"
        reply = await tools.gflow_generate_video(
            prompt=prompt,
            mode=mode,
            aspect=_aspect(body, _VIDEO_ASPECTS, "16:9"),
            initial_frame=images[0] if mode == "i2v" else None,
            end_frame=images[1] if mode == "i2v" and len(images) > 1 else None,
            reference_images=images if mode == "r2v" else None,
            model=alias,
            duration=_int(body, "seconds", _int(body, "duration", None)),
            resolution=body.get("resolution"),
            count=_int(body, "n", 1) or 1,
            wait=wait,
        )
    if reply.get("status") in ("error", "failed"):
        raise _tool_error(reply)
    return reply


def _unix(stamp: str | None) -> int | None:
    return int(datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()) if stamp else None


_URL_TTL_S = 24 * 3600


def _signature(path: str, expires: int) -> str:
    token = get_settings().daemon_token
    key = token.get_secret_value() if token else ""
    return hmac.new(key.encode(), f"{path}:{expires}".encode(), hashlib.sha256).hexdigest()


def signed_url(base: str, path: str) -> str:
    """Absolute content URL that works without a bearer header until it expires."""
    expires = int(time.time()) + _URL_TTL_S
    return f"{base.rstrip('/')}{path}?expires={expires}&sig={_signature(path, expires)}"


def has_valid_signature(path: str, query: str) -> bool:
    """True for a content path carrying an unexpired signature (the auth middleware's bypass)."""
    if not _CONTENT_PATH.fullmatch(path):
        return False
    params = parse_qs(query)
    try:
        expires = int(params["expires"][0])
        sig = params["sig"][0]
    except (KeyError, ValueError):
        return False
    return expires > time.time() and hmac.compare_digest(sig, _signature(path, expires))


def _task_view(task_id: str, base: str) -> dict[str, Any]:
    db_path = get_settings().resolved_db_path()
    with DataStore.open(db_path) as store:
        task = QueueRepository(store).get_task(task_id)
    if task is None:
        raise ApiError(404, f"task {task_id!r} not found", "not_found")
    kind = "image" if task.task_type in ("t2i", "i2i") else "video"
    status = _STATUS.get(task.status, "in_progress")
    view: dict[str, Any] = {
        "id": task.task_id,
        "object": "generation.task",
        "kind": kind,
        "model": _PUBLIC_NAME.get(str(task.payload.get("model")), task.payload.get("model")),
        "status": status,
        "created_at": _unix(task.created_at),
        "outputs": [],
    }
    if status == "completed":
        result = tools.task_payload(task.profile_name, task_id, db_path)
        view["completed_at"] = _unix(task.updated_at)
        view["seed"] = result.get("seed")
        view["outputs"] = [
            {"index": i, "type": kind, "url": signed_url(base, f"/v1/tasks/{task_id}/content/{i}")}
            for i in range(len(result.get("files") or []))
        ]
        if not view["outputs"]:
            view["status"] = "failed"
            view["error"] = {"code": "no_output", "message": "generation finished without a file"}
    elif status == "failed":
        err = task.error or {}
        code = "indeterminate" if task.status == "indeterminate" else "generation_failed"
        view["error"] = {"code": code, "message": str(err.get("detail") or err.get("title") or "")}
    return view


async def create_task(request: Request) -> Response:
    reply = await _submit(await request.json(), wait=False)
    return JSONResponse(_task_view(reply["task_id"], str(request.base_url)), status_code=202)


async def get_task(request: Request) -> Response:
    return JSONResponse(_task_view(request.path_params["task_id"], str(request.base_url)))


async def get_content(request: Request) -> Response:
    task_id = request.path_params["task_id"]
    view = _task_view(task_id, str(request.base_url))
    if view["status"] != "completed":
        raise ApiError(409, f"task is {view['status']}", "task_not_completed")
    db_path = get_settings().resolved_db_path()
    with DataStore.open(db_path) as store:
        task = QueueRepository(store).get_task(task_id)
    assert task is not None
    files: list[str] = tools.task_payload(task.profile_name, task_id, db_path).get("files") or []
    index = int(request.path_params.get("index", 0))
    if index >= len(files) or not Path(files[index]).is_file():
        raise ApiError(404, "output not found", "not_found")
    return FileResponse(files[index])


async def images_generations(request: Request) -> Response:
    body = await request.json()
    if isinstance(body, dict) and MODELS.get(str(body.get("model")), ("image",))[0] != "image":  # pyright: ignore[reportUnknownMemberType,reportUnknownArgumentType]
        raise ApiError(400, "use POST /v1/tasks for video models")
    reply = await _submit(body, wait=True)
    data: list[dict[str, str]] = []
    paths: list[str] = reply.get("files") or []
    for path in paths:
        b64 = base64.b64encode(Path(path).read_bytes()).decode()
        data.append({"b64_json": b64})
    if not data:
        raise ApiError(502, "generation finished without a file", "server_error")
    return JSONResponse({"created": int(time.time()), "data": data})


async def list_models(_: Request) -> Response:
    return JSONResponse(
        {
            "object": "list",
            "data": [
                {"id": name, "object": "model", "owned_by": "google-flow", "kind": kind}
                for name, (kind, _) in MODELS.items()
            ],
        }
    )


def _handled(endpoint: Any) -> Any:
    async def run(request: Request) -> Response:
        try:
            return await endpoint(request)
        except ApiError as exc:
            return _error(exc.status, exc.message, exc.code)
        except ValueError:
            return _error(400, "request body must be valid JSON", "invalid_request_error")

    return run


routes = [
    Route("/v1/models", _handled(list_models), methods=["GET"]),
    Route("/v1/tasks", _handled(create_task), methods=["POST"]),
    Route("/v1/tasks/{task_id}", _handled(get_task), methods=["GET"]),
    Route("/v1/tasks/{task_id}/content", _handled(get_content), methods=["GET"]),
    Route("/v1/tasks/{task_id}/content/{index:int}", _handled(get_content), methods=["GET"]),
    Route("/v1/images/generations", _handled(images_generations), methods=["POST"]),
]
