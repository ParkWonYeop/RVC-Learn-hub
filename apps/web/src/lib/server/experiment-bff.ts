import "server-only";

import { NextRequest, NextResponse } from "next/server";
import type {
  ApiCreatedJob,
  ApiExperiment,
  ApiExperimentCreateRequest,
  ApiExperimentUpdateRequest,
  ApiExperimentJobName,
  ApiExperimentJobNameList,
  ApiJobConfig,
  ApiList,
  ExperimentComparisonArtifact,
  ExperimentComparisonAttempt,
  ExperimentComparisonAvailability,
  ExperimentComparisonJob,
  ExperimentComparisonMetricPoint,
  ExperimentComparisonMetricSeries,
  ExperimentComparisonResponse,
  ExperimentComparisonSample,
} from "@/lib/api-types";
import { jobStatuses } from "@/lib/types";
import { bffError } from "./bff-proxy";
import { managerRawRequest } from "./manager-api";
import { isSameOriginMutation, isSameOriginRead } from "./request-security";
import { SESSION_COOKIE_NAME } from "./session-cookie";

const MAX_EXPERIMENT_BODY_BYTES = 12_288;
const MAX_JOB_BODY_BYTES = 65_536;
const privateNoStore = "private, no-cache, no-store, must-revalidate";
const safeIdentifierPattern = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/;
const officialRepository = "RVC-Project/Retrieval-based-Voice-Conversion-WebUI";
const versions = new Set(["v1", "v2"]);
const rates = new Set(["40k", "48k"]);
const f0Methods = new Set(["pm", "harvest", "dio", "rmvpe", "rmvpe_gpu"]);
const inferenceF0Methods = new Set(["pm", "harvest", "crepe", "rmvpe"]);
const jobStatusSet = new Set<string>(jobStatuses);
const canonicalUuidPattern =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const sha256Pattern = /^[a-f0-9]{64}$/;
const metricKeyPattern = /^[A-Za-z0-9_.-]{1,128}$/;
const trainingComparisonMetricKeys = new Set([
  "current_epoch",
  "epoch_completed",
  "epoch_progress_percent",
  "grad_norm_d",
  "grad_norm_g",
  "learning_rate",
  "loss_d_total",
  "loss_fm",
  "loss_g_adversarial",
  "loss_g_total",
  "loss_kl",
  "loss_mel",
  "step",
  "total_epoch",
]);
const systemComparisonMetricKeyPattern =
  /^system\.gpu\.(?:[0-9]|[1-5][0-9]|6[0-3])\.(?:temperature_c|utilization_percent|vram_total_mb|vram_used_mb)$/;
const systemComparisonMetricKeys = new Set([
  "system.disk_free_bytes",
  "system.gpu.count",
  "system.gpu.telemetry_available",
]);

export async function proxyExperimentList(
  request: NextRequest,
  path: `/api/v1/${string}`,
): Promise<NextResponse> {
  if (!isSameOriginRead(request)) return bffError("forbidden", 403);
  const upstream = await requestManager(request, path, "GET");
  if (upstream instanceof NextResponse) return upstream;
  if (!upstream.ok) return upstreamError(request, upstream, "invalid_experiment");
  const list = publicExperimentList(await readJson(upstream));
  if (!list) return bffError("invalid_upstream_response", 502);
  return privateJson(list);
}

export async function proxyExperimentDetail(
  request: NextRequest,
  path: `/api/v1/${string}`,
): Promise<NextResponse> {
  if (!isSameOriginRead(request)) return bffError("forbidden", 403);
  const upstream = await requestManager(request, path, "GET");
  if (upstream instanceof NextResponse) return upstream;
  if (!upstream.ok) return upstreamError(request, upstream, "invalid_experiment");
  const experiment = publicExperiment(await readJson(upstream));
  if (!experiment) return bffError("invalid_upstream_response", 502);
  return privateJson(experiment);
}

export async function createExperiment(request: NextRequest): Promise<NextResponse> {
  if (!isSameOriginMutation(request)) return bffError("forbidden", 403);
  if (process.env.DASHBOARD_DEMO_MODE === "true") {
    return bffError("demo_mode_read_only", 409);
  }
  const body = await readBoundedJson(request, MAX_EXPERIMENT_BODY_BYTES);
  if (!body.ok) return bffError(body.tooLarge ? "payload_too_large" : "invalid_request", body.tooLarge ? 413 : 400);
  const payload = experimentCreateRequest(body.value);
  if (!payload) return bffError("invalid_request", 400);
  const upstream = await requestManager(request, "/api/v1/experiments", "POST", payload);
  if (upstream instanceof NextResponse) return upstream;
  if (!upstream.ok) {
    return upstreamError(request, upstream, "invalid_experiment", "experiment");
  }
  const experiment = publicExperiment(await readJson(upstream));
  if (!experiment) return bffError("invalid_upstream_response", 502);
  return privateJson(experiment, upstream.status);
}

export async function updateExperiment(
  request: NextRequest,
  path: `/api/v1/${string}`,
): Promise<NextResponse> {
  if (!isSameOriginMutation(request)) return bffError("forbidden", 403);
  if (process.env.DASHBOARD_DEMO_MODE === "true") {
    return bffError("demo_mode_read_only", 409);
  }
  const body = await readBoundedJson(request, MAX_EXPERIMENT_BODY_BYTES);
  if (!body.ok) {
    return bffError(
      body.tooLarge ? "payload_too_large" : "invalid_request",
      body.tooLarge ? 413 : 400,
    );
  }
  const payload = experimentUpdateRequest(body.value);
  if (!payload) return bffError("invalid_request", 400);
  const upstream = await requestManager(request, path, "PATCH", payload);
  if (upstream instanceof NextResponse) return upstream;
  if (!upstream.ok) return experimentMutationError(request, upstream, "update");
  const experiment = publicExperiment(await readJson(upstream));
  if (!experiment) return bffError("invalid_upstream_response", 502);
  return privateJson(experiment, upstream.status);
}

export async function deleteExperiment(
  request: NextRequest,
  path: `/api/v1/${string}`,
  expectedRowVersion: number,
): Promise<NextResponse> {
  if (!isSameOriginMutation(request)) return bffError("forbidden", 403);
  if (process.env.DASHBOARD_DEMO_MODE === "true") {
    return bffError("demo_mode_read_only", 409);
  }
  const upstream = await requestManager(
    request,
    `${path}?expected_row_version=${expectedRowVersion}`,
    "DELETE",
  );
  if (upstream instanceof NextResponse) return upstream;
  if (!upstream.ok) return experimentMutationError(request, upstream, "delete");
  if (upstream.status !== 204) {
    await cancelBody(upstream.body);
    return bffError("invalid_upstream_response", 502);
  }
  await cancelBody(upstream.body);
  return privateEmpty();
}

export async function proxyExperimentJobNames(
  request: NextRequest,
  path: `/api/v1/${string}`,
): Promise<NextResponse> {
  if (!isSameOriginRead(request)) return bffError("forbidden", 403);
  const upstream = await requestManager(request, path, "GET");
  if (upstream instanceof NextResponse) return upstream;
  if (!upstream.ok) return upstreamError(request, upstream, "invalid_job");
  const list = publicJobNameList(await readJson(upstream));
  if (!list) return bffError("invalid_upstream_response", 502);
  return privateJson(list);
}

export async function proxyExperimentComparison(
  request: NextRequest,
  path: `/api/v1/${string}`,
  expectedExperimentId: string,
  expectedJobIds: readonly string[],
): Promise<NextResponse> {
  if (!isSameOriginRead(request)) return bffError("forbidden", 403);
  const upstream = await requestManager(request, path, "GET");
  if (upstream instanceof NextResponse) return upstream;
  if (!upstream.ok) return upstreamError(request, upstream, "invalid_job");
  const comparison = publicExperimentComparison(
    await readJson(upstream),
    expectedExperimentId,
    expectedJobIds,
  );
  if (!comparison) return bffError("invalid_upstream_response", 502);
  return privateJson(comparison);
}

export async function createJob(request: NextRequest): Promise<NextResponse> {
  if (!isSameOriginMutation(request)) return bffError("forbidden", 403);
  if (process.env.DASHBOARD_DEMO_MODE === "true") {
    return bffError("demo_mode_read_only", 409);
  }
  const body = await readBoundedJson(request, MAX_JOB_BODY_BYTES);
  if (!body.ok) return bffError(body.tooLarge ? "payload_too_large" : "invalid_request", body.tooLarge ? 413 : 400);
  const payload = jobCreateRequest(body.value);
  if (!payload) return bffError("invalid_request", 400);
  const upstream = await requestManager(request, "/api/v1/jobs", "POST", payload);
  if (upstream instanceof NextResponse) return upstream;
  if (!upstream.ok) return upstreamError(request, upstream, "invalid_job", "job");
  const created = publicCreatedJob(await readJson(upstream));
  if (!created) return bffError("invalid_upstream_response", 502);
  return privateJson(created, upstream.status);
}

async function requestManager(
  request: NextRequest,
  path: `/api/v1/${string}`,
  method: "GET" | "POST" | "PATCH" | "DELETE",
  body?: unknown,
): Promise<Response | NextResponse> {
  const token = request.cookies.get(SESSION_COOKIE_NAME)?.value;
  if (!token) return bffError("session_required", 401, request);
  try {
    const { response } = await managerRawRequest(path, {
      body,
      method,
      signal: request.signal,
      token,
    });
    return response;
  } catch (error) {
    if (request.signal.aborted) throw error;
    return bffError("manager_unavailable", 502);
  }
}

function experimentCreateRequest(value: unknown): ApiExperimentCreateRequest | null {
  if (!hasExactKeys(value, ["name", "dataset_id", "description"])) return null;
  const { name, dataset_id: datasetId, description } = value;
  if (
    typeof name !== "string" ||
    name.length < 1 ||
    name.length > 128 ||
    name.trim() !== name ||
    name === "." ||
    name === ".." ||
    name.includes("/") ||
    name.includes("\\") ||
    hasControlCharacter(name) ||
    typeof datasetId !== "string" ||
    !safeIdentifierPattern.test(datasetId) ||
    (description !== null &&
      (typeof description !== "string" ||
        description.length > 8_192 ||
        hasControlCharacterExceptWhitespace(description)))
  ) {
    return null;
  }
  return { name, dataset_id: datasetId, description };
}

function experimentUpdateRequest(value: unknown): ApiExperimentUpdateRequest | null {
  if (!hasExactKeys(value, ["expected_row_version", "description"])) return null;
  const { expected_row_version: expectedRowVersion, description } = value;
  if (
    !integerInRange(expectedRowVersion, 1, 2_147_483_647) ||
    (description !== null &&
      (typeof description !== "string" ||
        description.length > 8_192 ||
        hasControlCharacterExceptWhitespace(description)))
  ) {
    return null;
  }
  return {
    expected_row_version: expectedRowVersion as number,
    description: description as string | null,
  };
}

function jobCreateRequest(value: unknown): ApiJobConfig | null {
  if (
    !hasExactKeys(value, [
      "schema_version",
      "job_name",
      "experiment_id",
      "dataset_id",
      "rvc_backend",
      "model",
      "pretrained",
      "training_feature",
      "training",
      "f0_extraction",
      "index",
      "auto_inference_samples",
      "artifacts",
      "resource",
    ]) ||
    value.schema_version !== "1.0" ||
    !safeIdentifier(value.job_name) ||
    !safeIdentifier(value.experiment_id) ||
    !safeIdentifier(value.dataset_id)
  ) {
    return null;
  }
  const backend = exactRecord(value.rvc_backend, [
    "backend_type",
    "repository",
    "rvc_version",
    "rvc_commit_hash",
  ]);
  const model = exactRecord(value.model, ["version", "sample_rate", "use_f0", "speaker_id"]);
  const pretrained = exactRecord(value.pretrained, [
    "mode",
    "g_path",
    "d_path",
    "allow_custom_override",
  ]);
  const feature = exactRecord(value.training_feature, [
    "feature_dir_policy",
    "v1_feature_dir",
    "v2_feature_dir",
  ]);
  const training = exactRecord(value.training, [
    "epochs",
    "batch_size_per_gpu",
    "save_every_epoch",
    "save_only_latest",
    "save_every_weights",
    "cache_dataset_in_gpu",
    "gpu_ids",
  ]);
  const f0 = exactRecord(value.f0_extraction, ["training_f0_method", "rmvpe_gpu_ids"]);
  const index = exactRecord(value.index, [
    "build_index",
    "collect_total_fea",
    "collect_added_index",
  ]);
  const samples = exactRecord(value.auto_inference_samples, [
    "enabled",
    "test_set_id",
    "inference_f0_method",
    "transpose",
    "index_rate",
    "filter_radius",
    "resample_sr",
    "rms_mix_rate",
    "protect",
  ]);
  const artifacts = exactRecord(value.artifacts, [
    "collect_checkpoints",
    "collect_small_model",
    "extract_small_model_if_missing",
    "collect_index",
    "collect_tensorboard",
    "collect_logs",
    "collect_samples",
  ]);
  const resource = exactRecord(value.resource, [
    "min_vram_gb",
    "preferred_worker_tags",
    "priority",
  ]);
  if (!backend || !model || !pretrained || !feature || !training || !f0 || !index || !samples || !artifacts || !resource) {
    return null;
  }
  if (
    backend.backend_type !== "rvc_webui" ||
    backend.repository !== officialRepository ||
    typeof backend.rvc_version !== "string" ||
    !versions.has(backend.rvc_version) ||
    backend.rvc_commit_hash !== null ||
    typeof model.version !== "string" ||
    !versions.has(model.version) ||
    backend.rvc_version !== model.version ||
    typeof model.sample_rate !== "string" ||
    !rates.has(model.sample_rate) ||
    typeof model.use_f0 !== "boolean" ||
    !integerInRange(model.speaker_id, 0, 1_000_000)
  ) {
    return null;
  }
  if (
    pretrained.mode !== "auto" ||
    pretrained.g_path !== null ||
    pretrained.d_path !== null ||
    pretrained.allow_custom_override !== false ||
    feature.feature_dir_policy !== "auto" ||
    feature.v1_feature_dir !== "3_feature256" ||
    feature.v2_feature_dir !== "3_feature768"
  ) {
    return null;
  }
  const gpuIds = integerArray(training.gpu_ids, 1, 16, 0, 31);
  if (
    !integerInRange(training.epochs, 1, 100_000) ||
    !integerInRange(training.batch_size_per_gpu, 1, 1_024) ||
    !integerInRange(training.save_every_epoch, 1, 100_000) ||
    typeof training.save_only_latest !== "boolean" ||
    typeof training.save_every_weights !== "boolean" ||
    typeof training.cache_dataset_in_gpu !== "boolean" ||
    !gpuIds
  ) {
    return null;
  }
  const method = f0.training_f0_method;
  const rmvpeGpuIds = f0.rmvpe_gpu_ids === null
    ? null
    : integerArray(f0.rmvpe_gpu_ids, 1, 16, 0, 31);
  if (
    (method !== null && (typeof method !== "string" || !f0Methods.has(method))) ||
    (model.use_f0 && method === null) ||
    (!model.use_f0 && (method !== null || rmvpeGpuIds !== null)) ||
    (method === "rmvpe_gpu" && !rmvpeGpuIds) ||
    (method !== "rmvpe_gpu" && rmvpeGpuIds !== null)
  ) {
    return null;
  }
  if (
    typeof index.build_index !== "boolean" ||
    typeof index.collect_total_fea !== "boolean" ||
    typeof index.collect_added_index !== "boolean" ||
    index.collect_total_fea !== index.build_index ||
    index.collect_added_index !== index.build_index
  ) {
    return null;
  }
  if (
    samples.enabled !== false ||
    samples.test_set_id !== null ||
    typeof samples.inference_f0_method !== "string" ||
    !inferenceF0Methods.has(samples.inference_f0_method) ||
    !integerInRange(samples.transpose, -48, 48) ||
    !numberInRange(samples.index_rate, 0, 1) ||
    !integerInRange(samples.filter_radius, 0, 7) ||
    !integerInRange(samples.resample_sr, 0, 768_000) ||
    !numberInRange(samples.rms_mix_rate, 0, 1) ||
    !numberInRange(samples.protect, 0, 0.5)
  ) {
    return null;
  }
  if (
    !booleanFields(artifacts, [
      "collect_checkpoints",
      "collect_small_model",
      "extract_small_model_if_missing",
      "collect_index",
      "collect_tensorboard",
      "collect_logs",
      "collect_samples",
    ]) ||
    artifacts.collect_samples !== false ||
    artifacts.collect_index !== index.build_index
  ) {
    return null;
  }
  const tags = stringArray(resource.preferred_worker_tags, 64, 128);
  if (
    !numberInRange(resource.min_vram_gb, 0, 1_024) ||
    !integerInRange(resource.priority, 0, 10) ||
    !tags
  ) {
    return null;
  }
  return value as unknown as ApiJobConfig;
}

function publicExperimentList(value: unknown): ApiList<ApiExperiment> | null {
  if (!isRecord(value) || !Array.isArray(value.items)) return null;
  const items = value.items.map(publicExperiment);
  const total = integer(value.total, 0, Number.MAX_SAFE_INTEGER);
  const offset = integer(value.offset, 0, Number.MAX_SAFE_INTEGER);
  const limit = integer(value.limit, 1, 200);
  if (items.some((item) => item === null) || total === null || offset === null || limit === null) {
    return null;
  }
  return { items: items as ApiExperiment[], total, offset, limit };
}

function publicExperiment(value: unknown): ApiExperiment | null {
  if (!isRecord(value)) return null;
  const id = safeIdentifier(value.id);
  const rowVersion = integer(value.row_version, 1, 2_147_483_647);
  const name = safeDisplayString(value.name, 128);
  const datasetId = safeIdentifier(value.dataset_id);
  const description = nullableDisplayString(value.description, 8_192);
  const createdAt = safeDate(value.created_at);
  const updatedAt = safeDate(value.updated_at);
  if (
    !id ||
    rowVersion === null ||
    !name ||
    !datasetId ||
    description === undefined ||
    !createdAt ||
    !updatedAt
  ) {
    return null;
  }
  return {
    id,
    row_version: rowVersion,
    name,
    dataset_id: datasetId,
    description,
    created_at: createdAt,
    updated_at: updatedAt,
  };
}

async function experimentMutationError(
  request: NextRequest,
  upstream: Response,
  operation: "update" | "delete",
): Promise<NextResponse> {
  if (upstream.status !== 409) {
    return upstreamError(request, upstream, "invalid_experiment");
  }
  const value = await readJson(upstream);
  const detail = isRecord(value) && typeof value.detail === "string" ? value.detail : null;
  const error = detail === "experiment changed; refresh and retry"
    ? "stale_experiment"
    : operation === "delete" && detail === "experiment with jobs cannot be deleted"
      ? "experiment_has_jobs"
      : operation === "delete" && detail === "experiment with MLflow projection cannot be deleted"
        ? "experiment_has_mlflow_projection"
        : operation === "delete" && detail === "experiment became referenced and cannot be deleted"
          ? "experiment_became_referenced"
          : "conflict";
  return bffError(error, 409, request);
}

function publicJobNameList(value: unknown): ApiExperimentJobNameList | null {
  if (!isRecord(value) || !Array.isArray(value.items)) return null;
  const items = value.items.map(publicJobName);
  const total = integer(value.total, 0, Number.MAX_SAFE_INTEGER);
  const offset = integer(value.offset, 0, Number.MAX_SAFE_INTEGER);
  const limit = integer(value.limit, 1, 200);
  if (items.some((item) => item === null) || total === null || offset === null || limit === null) {
    return null;
  }
  return { items: items as ApiExperimentJobName[], total, offset, limit };
}

function publicJobName(value: unknown): ApiExperimentJobName | null {
  if (!isRecord(value)) return null;
  const id = safeIdentifier(value.id);
  const jobName = safeIdentifier(value.job_name);
  const createdAt = safeDate(value.created_at);
  if (
    !id ||
    !jobName ||
    typeof value.status !== "string" ||
    !jobStatusSet.has(value.status) ||
    !createdAt
  ) {
    return null;
  }
  return { id, job_name: jobName, status: value.status as ApiExperimentJobName["status"], created_at: createdAt };
}

function publicExperimentComparison(
  value: unknown,
  expectedExperimentId: string,
  expectedJobIds: readonly string[],
): ExperimentComparisonResponse | null {
  if (
    !isRecord(value) ||
    value.metric_point_limit_per_key !== 200 ||
    !Array.isArray(value.jobs) ||
    value.jobs.length !== expectedJobIds.length ||
    value.jobs.length < 2 ||
    value.jobs.length > 16
  ) {
    return null;
  }
  const experiment = publicExperiment(value.experiment);
  if (
    !experiment ||
    !canonicalUuidPattern.test(experiment.id) ||
    !canonicalUuidPattern.test(experiment.dataset_id) ||
    experiment.id !== expectedExperimentId
  ) {
    return null;
  }
  const jobs = value.jobs.map((item, index) =>
    publicComparisonJob(item, expectedJobIds[index] ?? "", experiment),
  );
  if (jobs.some((job) => job === null)) return null;
  return {
    experiment,
    jobs: jobs as ExperimentComparisonJob[],
    metric_point_limit_per_key: 200,
  };
}

function publicComparisonJob(
  value: unknown,
  expectedJobId: string,
  experiment: ApiExperiment,
): ExperimentComparisonJob | null {
  if (!isRecord(value) || !canonicalUuidPattern.test(expectedJobId)) return null;
  const id = canonicalUuid(value.id);
  const jobName = safeIdentifier(value.job_name);
  const status = typeof value.status === "string" && jobStatusSet.has(value.status)
    ? value.status as ExperimentComparisonJob["status"]
    : null;
  const config = publicComparisonJobConfig(value.config);
  const currentEpoch = nullableInteger(value.current_epoch, 0, 100_000);
  const totalEpoch = integer(value.total_epoch, 1, 100_000);
  const currentAttempt = value.current_attempt === null
    ? null
    : publicComparisonAttempt(value.current_attempt);
  const metrics = publicComparisonMetrics(value.metrics);
  const availability = publicComparisonAvailability(value.availability);
  if (
    id !== expectedJobId ||
    !jobName ||
    !status ||
    !config ||
    currentEpoch === undefined ||
    totalEpoch === null ||
    currentEpoch !== null && currentEpoch > totalEpoch ||
    value.current_attempt !== null && currentAttempt === null ||
    metrics === null ||
    availability === null ||
    config.job_name !== jobName ||
    config.experiment_id !== experiment.id ||
    config.dataset_id !== experiment.dataset_id ||
    config.training.epochs !== totalEpoch ||
    currentAttempt !== null && currentAttempt.status !== status ||
    currentAttempt === null && status !== "queued" && status !== "cancelled" ||
    availability.final_model !== null && config.artifacts.collect_small_model !== true ||
    availability.final_index !== null &&
      (config.index.build_index !== true || config.artifacts.collect_index !== true) ||
    availability.samples.length > 0 &&
      (config.auto_inference_samples.enabled !== true || config.artifacts.collect_samples !== true) ||
    currentAttempt === null &&
      (metrics.length > 0 ||
        availability.final_model !== null ||
        availability.final_index !== null ||
        availability.samples.length > 0)
  ) {
    return null;
  }
  return {
    id,
    job_name: jobName,
    status,
    config,
    current_epoch: currentEpoch,
    total_epoch: totalEpoch,
    current_attempt: currentAttempt,
    metrics,
    availability,
  };
}

function publicComparisonAttempt(value: unknown): ExperimentComparisonAttempt | null {
  if (!isRecord(value)) return null;
  const id = canonicalUuid(value.id);
  const attemptNumber = integer(value.attempt_number, 1, 2_147_483_647);
  const engineMode = value.engine_mode === "fake" || value.engine_mode === "rvc_webui"
    ? value.engine_mode
    : null;
  const status = typeof value.status === "string" && jobStatusSet.has(value.status)
    ? value.status as ExperimentComparisonAttempt["status"]
    : null;
  const startedAt = safeDate(value.started_at);
  const finishedAt = value.finished_at === null ? null : safeDate(value.finished_at);
  const isTerminal = status === "completed" || status === "failed" || status === "cancelled";
  if (
    !id ||
    attemptNumber === null ||
    !engineMode ||
    !status ||
    !startedAt ||
    value.finished_at !== null && finishedAt === null ||
    isTerminal !== (finishedAt !== null) ||
    finishedAt !== null && Date.parse(finishedAt) < Date.parse(startedAt)
  ) {
    return null;
  }
  return {
    id,
    attempt_number: attemptNumber,
    engine_mode: engineMode,
    status,
    started_at: startedAt,
    finished_at: finishedAt,
  };
}

function publicComparisonMetrics(value: unknown): ExperimentComparisonMetricSeries[] | null {
  if (!Array.isArray(value) || value.length > 273) return null;
  const result = value.map(publicComparisonMetricSeries);
  if (result.some((series) => series === null)) return null;
  const parsed = result as ExperimentComparisonMetricSeries[];
  return new Set(parsed.map((series) => series.key)).size === parsed.length ? parsed : null;
}

function publicComparisonMetricSeries(value: unknown): ExperimentComparisonMetricSeries | null {
  if (!isRecord(value) || !comparisonMetricKey(value.key) || !Array.isArray(value.points)) {
    return null;
  }
  const totalPoints = integer(value.total_points, 1, Number.MAX_SAFE_INTEGER);
  const points = value.points.map(publicComparisonMetricPoint);
  if (
    totalPoints === null ||
    typeof value.truncated !== "boolean" ||
    points.length < 1 ||
    points.length > 200 ||
    points.some((point) => point === null)
  ) {
    return null;
  }
  const parsed = points as ExperimentComparisonMetricPoint[];
  if (
    totalPoints < parsed.length ||
    value.truncated !== (totalPoints > parsed.length) ||
    parsed.some((point, index) => index > 0 && point.sequence <= parsed[index - 1]!.sequence)
  ) {
    return null;
  }
  return {
    key: value.key,
    total_points: totalPoints,
    truncated: value.truncated,
    points: parsed,
  };
}

function publicComparisonMetricPoint(value: unknown): ExperimentComparisonMetricPoint | null {
  if (!isRecord(value)) return null;
  const sequence = integer(value.sequence, 0, Number.MAX_SAFE_INTEGER);
  const epoch = nullableInteger(value.epoch, 0, Number.MAX_SAFE_INTEGER);
  const step = nullableInteger(value.step, 0, Number.MAX_SAFE_INTEGER);
  const occurredAt = safeDate(value.occurred_at);
  if (
    sequence === null ||
    epoch === undefined ||
    step === undefined ||
    !numberInRange(value.value, -Number.MAX_VALUE, Number.MAX_VALUE) ||
    !occurredAt
  ) {
    return null;
  }
  return {
    sequence,
    epoch,
    step,
    value: value.value as number,
    occurred_at: occurredAt,
  };
}

function publicComparisonAvailability(value: unknown): ExperimentComparisonAvailability | null {
  if (!isRecord(value) || !Array.isArray(value.samples) || value.samples.length > 128) {
    return null;
  }
  const finalModel = value.final_model === null ? null : publicComparisonArtifact(value.final_model);
  const finalIndex = value.final_index === null ? null : publicComparisonArtifact(value.final_index);
  const samples = value.samples.map(publicComparisonSample);
  if (
    value.final_model !== null && finalModel === null ||
    value.final_index !== null && finalIndex === null ||
    samples.some((sample) => sample === null)
  ) {
    return null;
  }
  const parsed = samples as ExperimentComparisonSample[];
  if (
    new Set(parsed.map((sample) => sample.id)).size !== parsed.length ||
    new Set(parsed.map((sample) => sample.test_set_item_id)).size !== parsed.length
  ) {
    return null;
  }
  return { final_model: finalModel, final_index: finalIndex, samples: parsed };
}

function publicComparisonArtifact(value: unknown): ExperimentComparisonArtifact | null {
  if (!isRecord(value)) return null;
  const id = canonicalUuid(value.id);
  const filename = safeArtifactFilename(value.filename);
  const sizeBytes = integer(value.size_bytes, 1, Number.MAX_SAFE_INTEGER);
  if (
    !id ||
    !filename ||
    sizeBytes === null ||
    typeof value.sha256 !== "string" ||
    !sha256Pattern.test(value.sha256)
  ) {
    return null;
  }
  return { id, filename, size_bytes: sizeBytes, sha256: value.sha256 };
}

function publicComparisonSample(value: unknown): ExperimentComparisonSample | null {
  if (!isRecord(value)) return null;
  const id = canonicalUuid(value.id);
  const testSetItemId = canonicalUuid(value.test_set_item_id);
  const outputSizeBytes = integer(value.output_size_bytes, 1, Number.MAX_SAFE_INTEGER);
  const outputSampleRateHz = integer(value.output_sample_rate_hz, 1, Number.MAX_SAFE_INTEGER);
  const outputChannels = integer(value.output_channels, 1, Number.MAX_SAFE_INTEGER);
  const createdAt = safeDate(value.created_at);
  if (
    !id ||
    !testSetItemId ||
    outputSizeBytes === null ||
    typeof value.output_sha256 !== "string" ||
    !sha256Pattern.test(value.output_sha256) ||
    outputSampleRateHz === null ||
    outputChannels === null ||
    !numberInRange(value.output_duration_seconds, Number.MIN_VALUE, Number.MAX_VALUE) ||
    !createdAt
  ) {
    return null;
  }
  return {
    id,
    test_set_item_id: testSetItemId,
    output_size_bytes: outputSizeBytes,
    output_sha256: value.output_sha256,
    output_sample_rate_hz: outputSampleRateHz,
    output_channels: outputChannels,
    output_duration_seconds: value.output_duration_seconds as number,
    created_at: createdAt,
  };
}

function publicComparisonJobConfig(value: unknown): ApiJobConfig | null {
  if (
    !hasExactKeys(value, [
      "schema_version",
      "job_name",
      "experiment_id",
      "dataset_id",
      "rvc_backend",
      "model",
      "pretrained",
      "training_feature",
      "training",
      "f0_extraction",
      "index",
      "auto_inference_samples",
      "artifacts",
      "resource",
    ]) ||
    value.schema_version !== "1.0" ||
    !safeIdentifier(value.job_name) ||
    !canonicalUuid(value.experiment_id) ||
    !canonicalUuid(value.dataset_id)
  ) {
    return null;
  }
  const backend = exactRecord(value.rvc_backend, [
    "backend_type",
    "repository",
    "rvc_version",
    "rvc_commit_hash",
  ]);
  const model = exactRecord(value.model, ["version", "sample_rate", "use_f0", "speaker_id"]);
  const pretrained = exactRecord(value.pretrained, [
    "mode",
    "g_path",
    "d_path",
    "allow_custom_override",
  ]);
  const feature = exactRecord(value.training_feature, [
    "feature_dir_policy",
    "v1_feature_dir",
    "v2_feature_dir",
  ]);
  const training = exactRecord(value.training, [
    "epochs",
    "batch_size_per_gpu",
    "save_every_epoch",
    "save_only_latest",
    "save_every_weights",
    "cache_dataset_in_gpu",
    "gpu_ids",
  ]);
  const f0 = exactRecord(value.f0_extraction, ["training_f0_method", "rmvpe_gpu_ids"]);
  const index = exactRecord(value.index, [
    "build_index",
    "collect_total_fea",
    "collect_added_index",
  ]);
  const samples = exactRecord(value.auto_inference_samples, [
    "enabled",
    "test_set_id",
    "inference_f0_method",
    "transpose",
    "index_rate",
    "filter_radius",
    "resample_sr",
    "rms_mix_rate",
    "protect",
  ]);
  const artifacts = exactRecord(value.artifacts, [
    "collect_checkpoints",
    "collect_small_model",
    "extract_small_model_if_missing",
    "collect_index",
    "collect_tensorboard",
    "collect_logs",
    "collect_samples",
  ]);
  const resource = exactRecord(value.resource, [
    "min_vram_gb",
    "preferred_worker_tags",
    "priority",
  ]);
  if (
    !backend ||
    !model ||
    !pretrained ||
    !feature ||
    !training ||
    !f0 ||
    !index ||
    !samples ||
    !artifacts ||
    !resource
  ) {
    return null;
  }
  const backendVersion = backend.rvc_version;
  const commitHash = backend.rvc_commit_hash;
  if (
    backend.backend_type !== "rvc_webui" ||
    typeof backend.repository !== "string" ||
    !safePublicString(backend.repository, 512) ||
    backendVersion !== null && (typeof backendVersion !== "string" || !versions.has(backendVersion)) ||
    commitHash !== null &&
      (typeof commitHash !== "string" || !/^[a-f0-9]{7,64}$/.test(commitHash)) ||
    typeof model.version !== "string" ||
    !versions.has(model.version) ||
    backendVersion !== null && backendVersion !== model.version ||
    typeof model.sample_rate !== "string" ||
    !rates.has(model.sample_rate) ||
    typeof model.use_f0 !== "boolean" ||
    !integerInRange(model.speaker_id, 0, 1_000_000)
  ) {
    return null;
  }
  const gPath = nullableSafeRelativePath(pretrained.g_path);
  const dPath = nullableSafeRelativePath(pretrained.d_path);
  if (
    pretrained.mode !== "auto" && pretrained.mode !== "custom" ||
    gPath === undefined ||
    dPath === undefined ||
    pretrained.mode === "custom" && (gPath === null || dPath === null) ||
    typeof pretrained.allow_custom_override !== "boolean" ||
    feature.feature_dir_policy !== "auto" ||
    feature.v1_feature_dir !== "3_feature256" ||
    feature.v2_feature_dir !== "3_feature768"
  ) {
    return null;
  }
  const gpuIds = integerArray(training.gpu_ids, 1, 64, 0, 63);
  if (
    !integerInRange(training.epochs, 1, 100_000) ||
    !integerInRange(training.batch_size_per_gpu, 1, 1_024) ||
    !integerInRange(training.save_every_epoch, 1, 2_147_483_647) ||
    typeof training.save_only_latest !== "boolean" ||
    typeof training.save_every_weights !== "boolean" ||
    typeof training.cache_dataset_in_gpu !== "boolean" ||
    !gpuIds
  ) {
    return null;
  }
  const method = f0.training_f0_method;
  const rmvpeGpuIds = f0.rmvpe_gpu_ids === null
    ? null
    : integerArray(f0.rmvpe_gpu_ids, 1, 64, 0, 63);
  if (
    method !== null && (typeof method !== "string" || !f0Methods.has(method)) ||
    model.use_f0 === true && method === null ||
    model.use_f0 === false && (method !== null || rmvpeGpuIds !== null) ||
    method === "rmvpe_gpu" && !rmvpeGpuIds ||
    method !== "rmvpe_gpu" && rmvpeGpuIds !== null ||
    !booleanFields(index, ["build_index", "collect_total_fea", "collect_added_index"])
  ) {
    return null;
  }
  const testSetId = samples.test_set_id === null ? null : safeIdentifier(samples.test_set_id);
  if (
    typeof samples.enabled !== "boolean" ||
    testSetId === null !== (samples.test_set_id === null) ||
    samples.enabled && testSetId === null ||
    !samples.enabled && samples.test_set_id !== null ||
    typeof samples.inference_f0_method !== "string" ||
    !inferenceF0Methods.has(samples.inference_f0_method) ||
    !integerInRange(samples.transpose, -48, 48) ||
    !numberInRange(samples.index_rate, 0, 1) ||
    !integerInRange(samples.filter_radius, 0, 7) ||
    !validInferenceResampleRate(samples.resample_sr) ||
    !numberInRange(samples.rms_mix_rate, 0, 1) ||
    !numberInRange(samples.protect, 0, 0.5) ||
    !booleanFields(artifacts, [
      "collect_checkpoints",
      "collect_small_model",
      "extract_small_model_if_missing",
      "collect_index",
      "collect_tensorboard",
      "collect_logs",
      "collect_samples",
    ]) ||
    samples.enabled && artifacts.collect_samples !== true ||
    samples.enabled && artifacts.collect_small_model !== true ||
    samples.enabled && index.build_index === false && samples.index_rate !== 0 ||
    samples.enabled && samples.index_rate as number > 0 && artifacts.collect_index !== true ||
    samples.enabled && samples.index_rate as number > 0 && index.collect_added_index !== true
  ) {
    return null;
  }
  const tags = stringArray(resource.preferred_worker_tags, 64, 128);
  if (
    !numberInRange(resource.min_vram_gb, 0, 1_024) ||
    !integerInRange(resource.priority, 0, 10) ||
    !tags ||
    tags.some((tag) => tag.trim() !== tag)
  ) {
    return null;
  }
  return value as unknown as ApiJobConfig;
}

function comparisonMetricKey(value: unknown): value is string {
  if (typeof value !== "string" || !metricKeyPattern.test(value)) return false;
  return trainingComparisonMetricKeys.has(value) ||
    systemComparisonMetricKeys.has(value) ||
    systemComparisonMetricKeyPattern.test(value);
}

function canonicalUuid(value: unknown): string | null {
  return typeof value === "string" && canonicalUuidPattern.test(value) ? value : null;
}

function safeArtifactFilename(value: unknown): string | null {
  return typeof value === "string" &&
    value.length >= 1 &&
    value.length <= 255 &&
    value !== "." &&
    value !== ".." &&
    !value.includes("/") &&
    !value.includes("\\") &&
    !hasControlCharacter(value)
    ? value
    : null;
}

function safePublicString(value: string, maximumLength: number): boolean {
  return value.length >= 1 && value.length <= maximumLength && !hasControlCharacter(value);
}

function nullableSafeRelativePath(value: unknown): string | null | undefined {
  if (value === null) return null;
  if (typeof value !== "string" || value.length < 1 || value.length > 1_024 || hasControlCharacter(value)) {
    return undefined;
  }
  const normalized = value.replaceAll("\\", "/");
  if (normalized.startsWith("/") || normalized.split("/").includes("..")) return undefined;
  return value;
}

function validInferenceResampleRate(value: unknown): boolean {
  return integerInRange(value, 0, 0) || integerInRange(value, 16_000, 192_000);
}

function nullableInteger(
  value: unknown,
  minimum: number,
  maximum: number,
): number | null | undefined {
  return value === null ? null : integer(value, minimum, maximum) ?? undefined;
}

function publicCreatedJob(value: unknown): ApiCreatedJob | null {
  const job = publicJobName(value);
  if (!job || !isRecord(value)) return null;
  const experimentId = safeIdentifier(value.experiment_id);
  const datasetId = safeIdentifier(value.dataset_id);
  if (!experimentId || !datasetId) return null;
  return { ...job, experiment_id: experimentId, dataset_id: datasetId };
}

async function upstreamError(
  request: NextRequest,
  upstream: Response,
  validationCode: "invalid_experiment" | "invalid_job",
  committedResourceType?: "experiment" | "job",
): Promise<NextResponse> {
  const projectionDeferred = upstream.status === 503 && committedResourceType
    ? committedProjection(await readJson(upstream), committedResourceType)
    : null;
  if (!projectionDeferred) await cancelBody(upstream.body);
  if (projectionDeferred) {
    const response = privateJson(
      {
        error: "projection_deferred",
        ledger_committed: true,
        resource_type: committedResourceType,
        resource_id: projectionDeferred,
      },
      503,
    );
    copyRetryAfter(upstream, response);
    return response;
  }
  const status = [401, 403, 404, 409, 413, 422, 429, 503].includes(upstream.status)
    ? upstream.status
    : 502;
  const codes: Record<number, string> = {
    401: "session_expired",
    403: "forbidden",
    404: "not_found",
    409: "conflict",
    413: "payload_too_large",
    422: validationCode,
    429: "rate_limited",
    503: "manager_unavailable",
    502: "invalid_upstream_response",
  };
  const response = bffError(codes[status] ?? "proxy_failed", status, request);
  copyRetryAfter(upstream, response);
  return response;
}

function committedProjection(
  value: unknown,
  expectedResourceType: "experiment" | "job",
): string | null {
  if (!isRecord(value) || !isRecord(value.detail)) return null;
  const detail = value.detail;
  return detail.code === "mlflow_projection_deferred" &&
    detail.ledger_committed === true &&
    detail.resource_type === expectedResourceType
    ? safeIdentifier(detail.resource_id)
    : null;
}

function copyRetryAfter(upstream: Response, response: NextResponse): void {
  const retryAfter = upstream.headers.get("retry-after");
  if (retryAfter && /^(0|[1-9][0-9]{0,5})$/.test(retryAfter)) {
    response.headers.set("Retry-After", retryAfter);
  }
}

async function readBoundedJson(
  request: NextRequest,
  maximumBytes: number,
): Promise<{ ok: true; value: unknown } | { ok: false; tooLarge: boolean }> {
  const mediaType = request.headers.get("content-type")?.split(";", 1)[0]?.trim().toLowerCase();
  if (mediaType !== "application/json") {
    return { ok: false, tooLarge: false };
  }
  const declared = request.headers.get("content-length");
  if (declared && (!/^(0|[1-9][0-9]*)$/.test(declared) || Number(declared) > maximumBytes)) {
    return { ok: false, tooLarge: true };
  }
  if (!request.body) return { ok: false, tooLarge: false };
  const reader = request.body.getReader();
  const chunks: Uint8Array[] = [];
  let length = 0;
  try {
    while (true) {
      const chunk = await reader.read();
      if (chunk.done) break;
      length += chunk.value.byteLength;
      if (length > maximumBytes) {
        await reader.cancel("body limit exceeded");
        return { ok: false, tooLarge: true };
      }
      chunks.push(chunk.value);
    }
  } catch {
    return { ok: false, tooLarge: false };
  }
  const bytes = new Uint8Array(length);
  let offset = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, offset);
    offset += chunk.byteLength;
  }
  try {
    return { ok: true, value: JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes)) };
  } catch {
    return { ok: false, tooLarge: false };
  }
}

async function readJson(response: Response): Promise<unknown> {
  if (!response.headers.get("content-type")?.startsWith("application/json")) {
    await cancelBody(response.body);
    return null;
  }
  try {
    return await response.json();
  } catch {
    return null;
  }
}

function privateJson(body: unknown, status = 200): NextResponse {
  const response = NextResponse.json(body, { status });
  setPrivateHeaders(response.headers);
  return response;
}

function privateEmpty(): NextResponse {
  const response = new NextResponse(null, { status: 204 });
  setPrivateHeaders(response.headers);
  return response;
}

function setPrivateHeaders(headers: Headers): void {
  headers.set("Cache-Control", privateNoStore);
  headers.set("Pragma", "no-cache");
  headers.set("Vary", "Cookie");
  headers.set("X-Content-Type-Options", "nosniff");
}

function hasExactKeys<T extends readonly string[]>(
  value: unknown,
  keys: T,
): value is Record<T[number], unknown> {
  if (!isRecord(value)) return false;
  const actual = Object.keys(value);
  return actual.length === keys.length && actual.every((key) => keys.includes(key));
}

function exactRecord<T extends readonly string[]>(
  value: unknown,
  keys: T,
): Record<T[number], unknown> | null {
  return hasExactKeys(value, keys) ? value : null;
}

function booleanFields(value: Record<string, unknown>, fields: readonly string[]): boolean {
  return fields.every((field) => typeof value[field] === "boolean");
}

function integerArray(
  value: unknown,
  minimumLength: number,
  maximumLength: number,
  minimum: number,
  maximum: number,
): number[] | null {
  if (!Array.isArray(value) || value.length < minimumLength || value.length > maximumLength) return null;
  if (!value.every((item) => integerInRange(item, minimum, maximum))) return null;
  const parsed = value as number[];
  return new Set(parsed).size === parsed.length ? parsed : null;
}

function stringArray(value: unknown, maximumLength: number, maximumItemLength: number): string[] | null {
  if (!Array.isArray(value) || value.length > maximumLength) return null;
  if (
    !value.every(
      (item) =>
        typeof item === "string" &&
        item.length >= 1 &&
        item.length <= maximumItemLength &&
        !hasControlCharacter(item),
    )
  ) {
    return null;
  }
  const parsed = value as string[];
  return new Set(parsed).size === parsed.length ? parsed : null;
}

function safeIdentifier(value: unknown): string | null {
  return typeof value === "string" && safeIdentifierPattern.test(value) ? value : null;
}

function safeDisplayString(value: unknown, maximumLength: number): string | null {
  return typeof value === "string" && value.length >= 1 && value.length <= maximumLength && !hasControlCharacter(value)
    ? value
    : null;
}

function nullableDisplayString(value: unknown, maximumLength: number): string | null | undefined {
  return value === null
    ? null
    : typeof value === "string" && value.length <= maximumLength && !hasControlCharacterExceptWhitespace(value)
      ? value
      : undefined;
}

function safeDate(value: unknown): string | null {
  return typeof value === "string" && value.length <= 64 && Number.isFinite(Date.parse(value))
    ? value
    : null;
}

function integer(value: unknown, minimum: number, maximum: number): number | null {
  return integerInRange(value, minimum, maximum) ? value as number : null;
}

function integerInRange(value: unknown, minimum: number, maximum: number): boolean {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= minimum && value <= maximum;
}

function numberInRange(value: unknown, minimum: number, maximum: number): boolean {
  return typeof value === "number" && Number.isFinite(value) && value >= minimum && value <= maximum;
}

function hasControlCharacter(value: string): boolean {
  return /[\u0000-\u001f\u007f]/.test(value);
}

function hasControlCharacterExceptWhitespace(value: string): boolean {
  return /[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/.test(value);
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

async function cancelBody(body: ReadableStream<Uint8Array> | null): Promise<void> {
  if (!body) return;
  try {
    await body.cancel();
  } catch {
    // Closing a response body that already completed is harmless.
  }
}
