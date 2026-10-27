// gflow (Google Flow through `gflow serve`) image and video models for
// models-gateway. gflow's task API: POST /v1/tasks answers {id, status};
// GET /v1/tasks/{id} answers {status, outputs:[{index, type, url}], error};
// each output url is an absolute content URL signed by gflow (valid 24 hours),
// so it is fetched without the channel key.
//
// Declared names are `gflow/<gflow model>`; public names come from the
// channel `model_mapping`, as with the other task plugins.

const VIDEO_MODELS = ["veo-3.1-fast", "veo-3.1-quality", "veo-3.1-lite", "veo-3.1-lite-lower-priority", "omni-flash"];
const IMAGE_MODELS = ["nano-banana-2", "nano-banana-2-lite", "nano-banana-pro", "imagen-4"];
// Public names other task plugins also declare (shared models): the gateway picks the
// provider by channel priority. They run as the gflow model of the same name.
const SHARED_MODELS = ["nano-banana-2", "nano-banana-pro"];
// Flow shows a duration control only for Omni Flash; Veo always makes 8-second clips.
const OMNI_SECONDS = [4, 6, 8, 10];
const MAX_IMAGES = 4;

const IMAGE_UNIT_LABEL = { en: "image", zh: "张" };
const VIDEO_SCHEMA = { seconds: { type: "number", unit: "second", description: { en: "Video generation unit price", zh: "视频生成单价" } } };
const IMAGE_SCHEMA = { image_count: { type: "number", unit: "count", unitLabel: IMAGE_UNIT_LABEL, description: { en: "Image generation unit price", zh: "图片生成单价" } } };

function declared(names) {
  return names.map((name) => "gflow/" + name);
}

export const meta = {
  apiVersion: 1,
  key: "gflow",
  name: "Google Flow (gflow)",
  icon: "text:GF",
  description: {
    en: "Veo video and Nano Banana / Imagen images through a gflow serve daemon",
    zh: "通过 gflow serve 守护进程使用 Veo 视频与 Nano Banana / Imagen 图片",
  },
  version: "1.0.2",
  author: { name: "TensorGrid" },
  website: "https://github.com/mrdjango/gflow-cli2API",
  models: declared(VIDEO_MODELS).concat(declared(IMAGE_MODELS), SHARED_MODELS),
  fetchMode: "per_task",
  usageSchema: VIDEO_SCHEMA,
  usageExamples: [{ label: "8s", facts: { seconds: 8 } }],
  usageProfiles: [
    { models: declared(VIDEO_MODELS), schema: VIDEO_SCHEMA, examples: [{ label: "8s", facts: { seconds: 8 } }] },
    { models: declared(IMAGE_MODELS).concat(SHARED_MODELS), schema: IMAGE_SCHEMA, examples: [{ label: "1 image", facts: { image_count: 1 } }] },
  ],
  protocols: [
    { name: "openai_video", models: declared(VIDEO_MODELS) },
    { name: "openai_image", models: declared(IMAGE_MODELS).concat(SHARED_MODELS) },
  ],
};

function trimmed(value) {
  return String(value === undefined || value === null ? "" : value).trim();
}

function has(object, key) {
  return Object.prototype.hasOwnProperty.call(object, key);
}

function listOf(value) {
  if (value === undefined || value === null || value === "") return [];
  return Array.isArray(value) ? value : [value];
}

// The executing gflow model, without the `gflow/` namespace.
function modelOf(ctx) {
  for (const model of [ctx && ctx.upstreamModel, ctx && ctx.model].map(trimmed)) {
    if (meta.models.includes(model)) return model.replace(/^gflow\//, "");
  }
  throw new Error("model is not served by this channel");
}

function isImage(ctx) {
  return IMAGE_MODELS.includes(modelOf(ctx));
}

function images(req) {
  const urls = [];
  for (const value of listOf(req.image).concat(listOf(req.images), listOf(req.input_reference))) {
    const url = trimmed(value && typeof value === "object" ? value.url : value);
    if (!/^(https:\/\/|data:image\/)/i.test(url)) throw new Error("reference images must be HTTPS URLs or base64 data URIs");
    if (!urls.includes(url)) urls.push(url);
  }
  return urls;
}

// Validated gflow request body plus the usage facts it will be billed on.
function taskRequest(ctx) {
  const req = (ctx && ctx.requestBody) || {};
  const prompt = trimmed(req.prompt);
  if (!prompt) throw new Error("prompt is required");
  const model = modelOf(ctx);
  const body = { model: model, prompt: prompt };
  const refs = images(req);
  for (const key of ["size", "aspect_ratio"]) {
    if (has(req, key)) body[key] = req[key];
  }
  if (isImage(ctx)) {
    const count = req.n === undefined || req.n === null || req.n === "" ? 1 : Number(req.n);
    if (!Number.isInteger(count) || count < 1 || count > MAX_IMAGES) throw new Error("n must be an integer between 1 and " + MAX_IMAGES);
    body.n = count;
    if (refs.length) body.images = refs;
    return { body: body, facts: { image_count: count }, action: refs.length ? "image_to_image" : "text_to_image" };
  }
  const raw = req.seconds !== undefined ? req.seconds : req.duration;
  const seconds = raw === undefined || raw === null || raw === "" ? 8 : Number(raw);
  const allowed = model === "omni-flash" ? OMNI_SECONDS : [8];
  if (!allowed.includes(seconds)) throw new Error(model + " supports seconds " + allowed.join(", "));
  body.seconds = seconds;
  if (has(req, "resolution")) body.resolution = req.resolution;
  // A public name saying "reference" asks for reference images; otherwise images are first/last frames.
  const reference = /reference/i.test(trimmed(ctx.model)) || ["r2v", "reference"].includes(trimmed(req.mode).toLowerCase());
  if (refs.length) {
    body.images = refs;
    if (reference) body.mode = "reference";
    else if (refs.length > 2) throw new Error("image-to-video takes a first and an optional last frame");
  } else if (reference) throw new Error("reference-to-video needs at least one image");
  return { body: body, facts: { seconds: seconds }, action: refs.length ? (reference ? "reference_to_video" : "image_to_video") : "text_to_video" };
}

function apiBase(ctx) {
  return trimmed(ctx.baseUrl).replace(/\/+$/, "").replace(/\/v1$/i, "");
}

function authHeaders(ctx) {
  return { "Content-Type": "application/json", Accept: "application/json", Authorization: "Bearer " + ctx.apiKey };
}

export function buildSubmitRequest(ctx) {
  const request = taskRequest(ctx);
  return { url: apiBase(ctx) + "/v1/tasks", method: "POST", headers: authHeaders(ctx), body: request.body, action: request.action };
}

export function parseSubmitResponse(ctx, resp) {
  const body = resp.body || {};
  const taskId = trimmed(body.id);
  if (!taskId) throw new Error(trimmed(body.error && body.error.message) || "the model provider returned no task id");
  return { taskId: taskId, taskData: body };
}

export function extractUsage(ctx) {
  if (ctx.usagePurpose === "billing_ratios") return null;
  return taskRequest(ctx).facts;
}

export function buildQueryRequest(ctx) {
  return { url: apiBase(ctx) + "/v1/tasks/" + encodeURIComponent(ctx.taskId), method: "GET", headers: authHeaders(ctx) };
}

function outputs(body) {
  return listOf(body && body.outputs).filter((item) => item && trimmed(item.url));
}

export function parseTaskResult(ctx, body) {
  const status = trimmed(body && body.status).toLowerCase();
  if (status === "queued") return { status: "QUEUED", progress: "10%" };
  if (status === "in_progress") return { status: "IN_PROGRESS", progress: "50%" };
  if (status === "completed") {
    if (!outputs(body).length) return { status: "FAILURE", progress: "100%", reason: "The model provider finished without an output" };
    return { status: "SUCCESS", progress: "100%" };
  }
  if (status === "failed") {
    const message = trimmed(body.error && body.error.message);
    return { status: "FAILURE", progress: "100%", reason: message || "The model provider could not complete the task" };
  }
  return { status: "UNKNOWN", reason: "unrecognized status: " + status };
}

export function extractUsageOnComplete(task, taskResult, body) {
  const count = outputs(body).filter((item) => item.type === "image").length;
  return count >= 1 && count <= MAX_IMAGES ? { image_count: count } : {};
}

export function listArtifacts(task) {
  if (task.status !== "SUCCESS") return [];
  const found = outputs(task.data);
  if (found.some((item) => item.type === "video")) return [{ key: "video", type: "video", mimeType: "video/mp4" }];
  return found.map((_, index) => ({ key: "image-" + (index + 1), type: "image" }));
}

export function buildContentRequest(ctx) {
  const found = outputs(ctx.data);
  let item = null;
  if (ctx.artifactKey === "video") item = found.find((output) => output.type === "video");
  else {
    const match = /^image-(\d+)$/.exec(trimmed(ctx.artifactKey));
    item = match ? found[Number(match[1]) - 1] : null;
  }
  if (!item) throw new Error("artifact_not_found");
  return { url: trimmed(item.url), method: ctx.clientRequest.method, credentialless: true };
}

// Multipart fields arrive as strings; uploaded files are refused (send URLs or data URIs).
function requestFields(ctx) {
  if (ctx.body && ctx.body.kind === "json") {
    const value = ctx.body.value;
    if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("request body must be an object");
    return value;
  }
  if (ctx.body && ctx.body.kind === "multipart") {
    if ((ctx.body.files || []).length) throw new Error("pass reference images as HTTPS URLs or data URIs, not file uploads");
    const req = {};
    const fields = ctx.body.fields || {};
    for (const name of Object.keys(fields)) req[name.replace(/\[\]$/, "")] = fields[name].length > 1 ? fields[name] : fields[name][0];
    for (const key of ["seconds", "duration", "n"]) {
      if (req[key] !== undefined) req[key] = Number(req[key]);
    }
    return req;
  }
  throw new Error("JSON or multipart body required");
}

const KEYS = ["prompt", "size", "aspect_ratio", "n", "seconds", "duration", "resolution", "mode", "image", "images", "input_reference"];
const VIDEO_STATUS = { NOT_START: "queued", SUBMITTED: "queued", QUEUED: "queued", IN_PROGRESS: "in_progress", SUCCESS: "completed", FAILURE: "failed" };

function decode(ctx) {
  const req = requestFields(ctx);
  const requestBody = { model: ctx.model };
  for (const key of KEYS) {
    if (has(req, key)) requestBody[key] = req[key];
  }
  // Validate before any quota is reserved.
  const request = taskRequest({ model: ctx.model, upstreamModel: ctx.upstreamModel, requestBody: requestBody });
  return { kind: "submit", model: ctx.model, action: request.action, requestBody: requestBody };
}

export const protocols = {
  openai_video: {
    decodeRequest: decode,
    render: function (ctx, task) {
      const output = {
        id: task.task_id,
        object: "video",
        status: VIDEO_STATUS[task.status] || "queued",
        progress: Number(String(task.progress || "0").replace("%", "")) || 0,
        created_at: task.created_at,
      };
      if (task.status === "SUCCESS") output.completed_at = task.updated_at;
      if (task.status === "FAILURE") output.error = { message: task.fail_reason || "task failed", code: "task_failed" };
      return output;
    },
  },
  // OpenAI Images API: the host polls the task inside the request; render gets no
  // artifacts, so it answers gflow's signed URLs (the host fetches them for b64_json).
  openai_image: {
    decodeRequest: function (ctx) {
      const req = requestFields(ctx);
      if (req.stream !== undefined && req.stream !== false && req.stream !== "false") throw new Error("stream is not supported");
      if (req.mask !== undefined) throw new Error("mask is not supported");
      const decoded = decode(ctx);
      if (ctx.operation === "edit" && decoded.action !== "image_to_image") throw new Error("image is required");
      return decoded;
    },
    render: function (ctx, task) {
      return { created: task.created_at, data: outputs(task.data).map((item) => ({ url: trimmed(item.url) })) };
    },
  },
};
