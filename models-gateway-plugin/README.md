# gflow REST task API and its models-gateway plugin

`gflow serve` now serves a REST task API beside MCP (`/mcp`), so models-gateway (or any
OpenAI-style client) can use Google Flow the way it uses CLIProxyAPI or agent-cli-to-api.
Every generation runs through the same code as the MCP generate tools: one queue, one
browser per profile at a time, the same rate limit and the same spend-safe recovery.

## Run it

```bash
GFLOW_CLI_DAEMON_TOKEN=<long random key> gflow serve --host 0.0.0.0 --port 8000
```

The token is required off loopback and every request must send `Authorization: Bearer <token>`.
The server uses the default profile (`gflow auth use <name>`). `GFLOW_MCP_NO_SPEND=1` refuses
every generation with 403.

## Endpoints

| Endpoint | What it does |
|---|---|
| `GET /v1/models` | Public model names and their kind (`image` / `video`) |
| `POST /v1/tasks` | Queue a generation; answers the task (202) at once |
| `GET /v1/tasks/{id}` | `status`: `queued`, `in_progress`, `completed`, `failed`; `outputs[].url` when completed |
| `GET /v1/tasks/{id}/content/{n}` | Output file n |
| `POST /v1/images/generations` | OpenAI Images API: waits, answers `b64_json` (image models only) |

`POST /v1/tasks` body: `model`, `prompt`, and optionally `size` (`WxH`, mapped to the nearest
aspect) or `aspect_ratio`, `n` (1-4), `seconds` (Veo: 8 only; omni-flash: 4/6/8/10), `resolution` (omni-flash),
`image` / `images` (HTTPS URLs or base64 data URIs). For video, one or two images are the first
and last frame; `"mode": "reference"` makes them reference images instead. Image URLs that
resolve to a private address are refused.

Output URLs are absolute and signed (valid 24 hours), so they work without the bearer header;
they use `GFLOW_CLI_PUBLIC_URL` when set (else the request's host). models-gateway refuses to
fetch private addresses, so video content needs a public HTTPS host that forwards only
`/v1/tasks/*/content*` to gflow.
A task that failed after Flow may have spent a credit reports `error.code: "indeterminate"` and
is never retried by gflow.

Models: `nano-banana-2`, `nano-banana-2-lite`, `nano-banana-pro`, `imagen-4` (images; free on
Flow) and `veo-3.1-fast`, `veo-3.1-quality`, `veo-3.1-lite`, `veo-3.1-lite-lower-priority`,
`omni-flash` (video; spends the account's Veo credits).

## models-gateway plugin

`plugin.js` is a Task Plugin API v1 plugin with declared models `gflow/<model>`, serving
`/v1/videos` (`openai_video`) and `/v1/images/generations` + `/edits` (`openai_image`). Usage
facts: `seconds` for video, `image_count` for images.

1. Upload `plugin.js` on the admin panel's task plugin page (or copy it to
   `models-gateway/plugins/tasks/gflow/plugin.js`).
2. Create a **Task Plugin (61)** channel bound to `gflow`, Base URL = the gflow server
   (a trailing `/v1` is fine), key = `GFLOW_CLI_DAEMON_TOKEN`.
3. Map public names to declared names in `model_mapping` (a public name containing
   `reference` sends reference images), and price each model before enabling the channel.

`./check.sh` lints the plugin and replays `gflow.fixture.json` with the gateway's plugin CLI.
