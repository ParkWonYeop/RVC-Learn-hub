import "server-only";

import { NextRequest, NextResponse } from "next/server";
import type {
  ApiDataset,
  ApiDatasetUploadInitRequest,
  ApiDatasetUploadInitResponse,
  ApiDatasetUploadStatus,
  ApiList,
} from "@/lib/api-types";
import { bffError } from "./bff-proxy";
import { managerRawRequest } from "./manager-api";
import { isSameOriginMutation, isSameOriginRead, publicOrigin } from "./request-security";
import { SESSION_COOKIE_NAME } from "./session-cookie";

const MAX_INIT_BODY_BYTES = 16_384;
const MAX_DATASET_BYTES = 5 * 1024 ** 3;
const privateNoStore = "private, no-cache, no-store, must-revalidate";
const uploadStatuses = new Set<ApiDatasetUploadStatus>([
  "pending",
  "finalizing",
  "completed",
  "failed",
  "expired",
]);
const datasetStatuses = new Set<ApiDataset["status"]>([
  "legacy_imported",
  "upload_pending",
  "processing",
  "ready",
  "decoder_pending",
  "failed",
  "deleting",
  "delete_failed",
]);
const contentTypes = new Map<string, ReadonlySet<string>>([
  ["zip", new Set(["application/zip", "application/x-zip-compressed"])],
  ["wav", new Set(["audio/wav", "audio/x-wav", "audio/wave"])],
  ["flac", new Set(["audio/flac", "audio/x-flac"])],
  ["mp3", new Set(["audio/mpeg"])],
  ["m4a", new Set(["audio/mp4", "audio/x-m4a"])],
  ["ogg", new Set(["audio/ogg", "application/ogg"])],
  ["aac", new Set(["audio/aac", "audio/x-aac"])],
]);
const allowedUploadHeaders = new Set([
  "content-length",
  "content-type",
  "x-amz-checksum-sha256",
  "x-amz-meta-sha256",
  "x-rvc-upload-token",
]);

export async function proxyDatasetList(
  request: NextRequest,
  path: `/api/v1/${string}`,
): Promise<NextResponse> {
  if (!isSameOriginRead(request)) return bffError("forbidden", 403);
  const upstream = await requestManager(request, path, "GET");
  if (upstream instanceof NextResponse) return upstream;
  if (!upstream.ok) return upstreamError(request, upstream);
  const payload = await readJson(upstream);
  const list = publicDatasetList(payload);
  if (!list) return bffError("invalid_upstream_response", 502);
  return privateJson(list);
}

export async function proxyDatasetDetail(
  request: NextRequest,
  path: `/api/v1/${string}`,
): Promise<NextResponse> {
  if (!isSameOriginRead(request)) return bffError("forbidden", 403);
  const upstream = await requestManager(request, path, "GET");
  if (upstream instanceof NextResponse) return upstream;
  if (!upstream.ok) return upstreamError(request, upstream);
  const dataset = publicDataset(await readJson(upstream));
  if (!dataset) return bffError("invalid_upstream_response", 502);
  return privateJson(dataset);
}

export async function initializeDatasetUpload(request: NextRequest): Promise<NextResponse> {
  if (!isSameOriginMutation(request)) return bffError("forbidden", 403);
  if (process.env.DASHBOARD_DEMO_MODE === "true") {
    return bffError("demo_mode_read_only", 409);
  }
  const payload = await readInitRequest(request);
  if (!payload) return bffError("invalid_request", 400);
  const upstream = await requestManager(
    request,
    "/api/v1/datasets/uploads/init",
    "POST",
    payload,
  );
  if (upstream instanceof NextResponse) return upstream;
  if (!upstream.ok) return upstreamError(request, upstream);
  const response = publicUploadInit(await readJson(upstream), request, payload);
  if (!response) return bffError("invalid_upload_target", 502);
  return privateJson(response, { referrerPolicy: true });
}

export async function finalizeDatasetUpload(
  request: NextRequest,
  path: `/api/v1/${string}`,
): Promise<NextResponse> {
  if (!isSameOriginMutation(request)) return bffError("forbidden", 403);
  if (process.env.DASHBOARD_DEMO_MODE === "true") {
    return bffError("demo_mode_read_only", 409);
  }
  const upstream = await requestManager(request, path, "POST", undefined, null);
  if (upstream instanceof NextResponse) return upstream;
  if (!upstream.ok) return upstreamError(request, upstream);
  const dataset = publicDataset(await readJson(upstream));
  if (!dataset) return bffError("invalid_upstream_response", 502);
  return privateJson(dataset);
}

export async function deleteDataset(
  request: NextRequest,
  path: `/api/v1/${string}`,
): Promise<NextResponse> {
  if (!isSameOriginMutation(request)) return bffError("forbidden", 403);
  if (process.env.DASHBOARD_DEMO_MODE === "true") {
    return bffError("demo_mode_read_only", 409);
  }
  const upstream = await requestManager(request, path, "DELETE");
  if (upstream instanceof NextResponse) return upstream;
  if (!upstream.ok) return upstreamError(request, upstream);
  if (upstream.status !== 204) {
    await cancelBody(upstream.body);
    return bffError("invalid_upstream_response", 502);
  }
  const response = new NextResponse(null, { status: 204 });
  setPrivateHeaders(response.headers);
  return response;
}

async function requestManager(
  request: NextRequest,
  path: `/api/v1/${string}`,
  method: "GET" | "POST" | "DELETE",
  body?: unknown,
  timeoutMs: number | null = 10_000,
): Promise<Response | NextResponse> {
  const token = request.cookies.get(SESSION_COOKIE_NAME)?.value;
  if (!token) return bffError("session_required", 401, request);
  try {
    const { response } = await managerRawRequest(path, {
      body,
      method,
      signal: request.signal,
      timeoutMs,
      token,
    });
    return response;
  } catch (error) {
    if (request.signal.aborted) throw error;
    return bffError("manager_unavailable", 502);
  }
}

async function readInitRequest(
  request: NextRequest,
): Promise<ApiDatasetUploadInitRequest | null> {
  if (!request.headers.get("content-type")?.startsWith("application/json")) return null;
  const declaredLength = request.headers.get("content-length");
  if (declaredLength && Number(declaredLength) > MAX_INIT_BODY_BYTES) return null;
  let value: unknown;
  try {
    value = await request.json();
  } catch {
    return null;
  }
  if (!isRecord(value)) return null;
  const expectedKeys = new Set([
    "name",
    "filename",
    "content_type",
    "size_bytes",
    "sha256",
    "idempotency_key",
  ]);
  if (Object.keys(value).length !== expectedKeys.size) return null;
  if (Object.keys(value).some((key) => !expectedKeys.has(key))) return null;
  const { name, filename, content_type, size_bytes, sha256, idempotency_key } = value;
  if (
    typeof name !== "string" ||
    name.length < 1 ||
    name.length > 128 ||
    name.trim() !== name ||
    hasControlCharacter(name) ||
    typeof filename !== "string" ||
    filename.length < 1 ||
    filename.length > 255 ||
    filename === "." ||
    filename === ".." ||
    filename.includes("/") ||
    filename.includes("\\") ||
    hasControlCharacter(filename) ||
    typeof content_type !== "string" ||
    typeof size_bytes !== "number" ||
    !Number.isSafeInteger(size_bytes) ||
    size_bytes < 1 ||
    size_bytes > MAX_DATASET_BYTES ||
    typeof sha256 !== "string" ||
    !/^[a-f0-9]{64}$/.test(sha256) ||
    typeof idempotency_key !== "string" ||
    !/^[A-Za-z0-9_.:-]{8,128}$/.test(idempotency_key)
  ) {
    return null;
  }
  const extension = filename.includes(".") ? filename.split(".").pop()?.toLowerCase() : null;
  if (!extension || !contentTypes.get(extension)?.has(content_type)) return null;
  return { name, filename, content_type, size_bytes, sha256, idempotency_key };
}

function publicUploadInit(
  value: unknown,
  request: NextRequest,
  expected: ApiDatasetUploadInitRequest,
): ApiDatasetUploadInitResponse | null {
  if (!isRecord(value)) return null;
  const uploadSessionId = safeIdentifier(value.upload_session_id);
  const datasetId = safeIdentifier(value.dataset_id);
  const status = value.status;
  const expiresAt = safeDate(value.expires_at);
  if (
    !uploadSessionId ||
    !datasetId ||
    typeof status !== "string" ||
    !uploadStatuses.has(status as ApiDatasetUploadStatus) ||
    !expiresAt
  ) {
    return null;
  }
  const dataset = value.dataset === null || value.dataset === undefined
    ? null
    : publicDataset(value.dataset);
  if (value.dataset !== null && value.dataset !== undefined && !dataset) return null;
  const failureCode = nullableSafeString(value.failure_code, 128);
  const retryable = typeof value.retryable === "boolean" ? value.retryable : false;
  const retryAfter = nullableBoundedInteger(value.retry_after_seconds, 1, 86_400);
  if (status !== "pending") {
    return {
      upload_session_id: uploadSessionId,
      dataset_id: datasetId,
      status: status as ApiDatasetUploadStatus,
      method: null,
      upload_url: null,
      upload_headers: {},
      expires_at: expiresAt,
      dataset,
      failure_code: failureCode,
      retryable,
      retry_after_seconds: retryAfter,
    };
  }
  if (value.method !== "PUT" || typeof value.upload_url !== "string") return null;
  const uploadUrl = validatedUploadUrl(request, value.upload_url);
  const uploadHeaders = validatedUploadHeaders(value.upload_headers, expected);
  if (!uploadUrl || !uploadHeaders) return null;
  return {
    upload_session_id: uploadSessionId,
    dataset_id: datasetId,
    status: "pending",
    method: "PUT",
    upload_url: uploadUrl,
    upload_headers: uploadHeaders,
    expires_at: expiresAt,
    dataset,
    failure_code: failureCode,
    retryable,
    retry_after_seconds: retryAfter,
  };
}

function validatedUploadUrl(request: NextRequest, value: string): string | null {
  if (value.length > 8_192) return null;
  let target: URL;
  let browser: URL;
  try {
    target = new URL(value);
    browser = new URL(publicOrigin(request));
  } catch {
    return null;
  }
  if (!['http:', 'https:'].includes(target.protocol)) return null;
  if (browser.protocol === "https:" && target.protocol !== "https:") return null;
  if (target.username || target.password || target.hash) return null;
  const allowed = new Set([browser.origin, ...configuredUploadOrigins()]);
  return allowed.has(target.origin) ? target.toString() : null;
}

function configuredUploadOrigins(): string[] {
  const configured = process.env.DATASET_UPLOAD_ALLOWED_ORIGINS ?? "";
  const origins: string[] = [];
  for (const rawValue of configured.split(",")) {
    const value = rawValue.trim();
    if (!value) continue;
    try {
      const parsed = new URL(value);
      if (
        ['http:', 'https:'].includes(parsed.protocol) &&
        !parsed.username &&
        !parsed.password &&
        !parsed.search &&
        !parsed.hash &&
        (parsed.pathname === "/" || parsed.pathname === "")
      ) {
        origins.push(parsed.origin);
      }
    } catch {
      // Invalid entries fail closed and are never used as upload targets.
    }
  }
  return origins;
}

function validatedUploadHeaders(
  value: unknown,
  expected: ApiDatasetUploadInitRequest,
): Record<string, string> | null {
  if (!isRecord(value)) return null;
  const result: Record<string, string> = {};
  for (const [rawName, rawValue] of Object.entries(value)) {
    const name = rawName.toLowerCase();
    if (
      !allowedUploadHeaders.has(name) ||
      typeof rawValue !== "string" ||
      rawValue.length > 1_024 ||
      /[\r\n]/.test(rawValue)
    ) {
      return null;
    }
    result[name] = rawValue;
  }
  if (
    result["content-type"] !== expected.content_type ||
    result["content-length"] !== String(expected.size_bytes)
  ) {
    return null;
  }
  if (
    result["x-amz-meta-sha256"] !== undefined &&
    result["x-amz-meta-sha256"] !== expected.sha256
  ) {
    return null;
  }
  const uploadToken = result["x-rvc-upload-token"];
  if (uploadToken !== undefined && !/^[A-Za-z0-9_-]{16,512}$/.test(uploadToken)) {
    return null;
  }
  const checksum = result["x-amz-checksum-sha256"];
  if (checksum !== undefined && !/^[A-Za-z0-9+/]{43}=$/.test(checksum)) return null;
  return result;
}

function publicDatasetList(value: unknown): ApiList<ApiDataset> | null {
  if (!isRecord(value) || !Array.isArray(value.items)) return null;
  const items = value.items.map(publicDataset);
  const total = boundedInteger(value.total, 0, Number.MAX_SAFE_INTEGER);
  const offset = boundedInteger(value.offset, 0, Number.MAX_SAFE_INTEGER);
  const limit = boundedInteger(value.limit, 1, 200);
  if (items.some((item) => item === null) || total === null || offset === null || limit === null) {
    return null;
  }
  return { items: items as ApiDataset[], total, offset, limit };
}

function publicDataset(value: unknown): ApiDataset | null {
  if (!isRecord(value)) return null;
  const id = safeIdentifier(value.id);
  const name = safeString(value.name, 128);
  const status = value.status;
  const createdAt = safeDate(value.created_at);
  const updatedAt = safeDate(value.updated_at);
  if (
    !id ||
    !name ||
    typeof status !== "string" ||
    !datasetStatuses.has(status as ApiDataset["status"]) ||
    typeof value.is_usable !== "boolean" ||
    typeof value.retryable !== "boolean" ||
    !createdAt ||
    !updatedAt
  ) {
    return null;
  }
  const decoderPendingCount = boundedInteger(value.decoder_pending_count, 0, Number.MAX_SAFE_INTEGER);
  if (decoderPendingCount === null) return null;
  const sourceFileEntryCount = strictNullableInteger(value.source_file_entry_count, 0, 10_000);
  const skippedFileCount = strictNullableInteger(value.skipped_file_count, 0, 10_000);
  const rejectedFileCount = strictNullableInteger(value.rejected_file_count, 0, 10_000);
  const duplicateFileCount = strictNullableInteger(value.duplicate_file_count, 0, 10_000);
  const pcmQuality = publicPcmQuality(value.pcm_quality);
  if (
    sourceFileEntryCount === invalidField ||
    skippedFileCount === invalidField ||
    rejectedFileCount === invalidField ||
    duplicateFileCount === invalidField ||
    pcmQuality === invalidField
  ) {
    return null;
  }
  return {
    id,
    name,
    status: status as ApiDataset["status"],
    original_filename: nullableSafeString(value.original_filename, 255),
    original_size_bytes: nullableNumber(value.original_size_bytes),
    original_sha256: nullableSha256(value.original_sha256),
    original_mime_type: nullableSafeString(value.original_mime_type, 255),
    prepared_flat_size_bytes: nullableNumber(value.prepared_flat_size_bytes),
    prepared_flat_sha256: nullableSha256(value.prepared_flat_sha256),
    manifest_sha256: nullableSha256(value.manifest_sha256),
    quality_report_sha256: nullableSha256(value.quality_report_sha256),
    duration_sec: nullableNumber(value.duration_sec),
    file_count: nullableBoundedInteger(value.file_count, 0, Number.MAX_SAFE_INTEGER),
    sample_rate: nullableBoundedInteger(value.sample_rate, 1, 768_000),
    decoder_pending_count: decoderPendingCount,
    source_file_entry_count: sourceFileEntryCount,
    skipped_file_count: skippedFileCount,
    rejected_file_count: rejectedFileCount,
    duplicate_file_count: duplicateFileCount,
    pcm_quality: pcmQuality,
    is_usable: value.is_usable,
    failure_code: nullableSafeString(value.failure_code, 128),
    retryable: value.retryable,
    created_at: createdAt,
    updated_at: updatedAt,
  };
}

const invalidField = Symbol("invalid-dataset-field");

function strictNullableInteger(
  value: unknown,
  minimum: number,
  maximum: number,
): number | null | typeof invalidField {
  if (value === null || value === undefined) return null;
  return boundedInteger(value, minimum, maximum) ?? invalidField;
}

function publicPcmQuality(
  value: unknown,
): ApiDataset["pcm_quality"] | typeof invalidField {
  if (value === null || value === undefined) return null;
  if (
    !hasExactKeys(value, [
      "algorithm",
      "validated_file_count",
      "sample_count",
      "clipping_ratio",
      "silence_ratio",
      "rms_ratio",
      "silence_threshold_dbfs",
      "loudness",
    ]) ||
    value.algorithm !== "pcm-sample-weighted-v1"
  ) {
    return invalidField;
  }
  const validatedFileCount = boundedInteger(value.validated_file_count, 1, 10_000);
  const sampleCount = boundedInteger(value.sample_count, 1, Number.MAX_SAFE_INTEGER);
  const clippingRatio = boundedFiniteNumber(value.clipping_ratio, 0, 1);
  const silenceRatio = boundedFiniteNumber(value.silence_ratio, 0, 1);
  const rmsRatio = boundedFiniteNumber(value.rms_ratio, 0, 1);
  const silenceThresholdDbfs = boundedFiniteNumber(value.silence_threshold_dbfs, -120, 0, false);
  const loudness = publicPcmLoudness(value.loudness, validatedFileCount);
  if (
    validatedFileCount === null ||
    sampleCount === null ||
    clippingRatio === null ||
    silenceRatio === null ||
    rmsRatio === null ||
    silenceThresholdDbfs === null ||
    loudness === invalidField
  ) {
    return invalidField;
  }
  return {
    algorithm: value.algorithm,
    validated_file_count: validatedFileCount,
    sample_count: sampleCount,
    clipping_ratio: clippingRatio,
    silence_ratio: silenceRatio,
    rms_ratio: rmsRatio,
    silence_threshold_dbfs: silenceThresholdDbfs,
    loudness,
  };
}

function publicPcmLoudness(
  value: unknown,
  validatedFileCount: number | null,
): NonNullable<ApiDataset["pcm_quality"]>["loudness"] | typeof invalidField {
  // Missing LUFS is represented only by an explicit null for historical rows.
  if (value === null) return null;
  if (
    !hasExactKeys(value, [
      "algorithm",
      "scope",
      "block_duration_ms",
      "block_overlap_percent",
      "absolute_gate_lufs",
      "relative_gate_lu",
      "analyzed_file_count",
      "block_count",
      "gated_block_count",
      "integrated_lufs",
      "unavailable_reason",
    ]) ||
    value.algorithm !== "itu-r-bs1770-4-mono-stereo-v1" ||
    value.scope !== "global-gate-over-per-file-complete-blocks-v1" ||
    value.block_duration_ms !== 400 ||
    value.block_overlap_percent !== 75 ||
    value.absolute_gate_lufs !== -70 ||
    value.relative_gate_lu !== -10
  ) {
    return invalidField;
  }
  const analyzedFileCount = boundedInteger(value.analyzed_file_count, 0, 10_000);
  const blockCount = boundedInteger(value.block_count, 0, Number.MAX_SAFE_INTEGER);
  const gatedBlockCount = boundedInteger(
    value.gated_block_count,
    0,
    Number.MAX_SAFE_INTEGER,
  );
  const integratedLufs = value.integrated_lufs === null
    ? null
    : boundedFiniteNumber(value.integrated_lufs, -70, 10);
  const unavailableReasons = new Set([
    "below_absolute_gate",
    "insufficient_duration",
    "unsupported_channel_layout",
    "unsupported_sample_rate",
  ]);
  const unavailableReason = value.unavailable_reason === null
    ? null
    : typeof value.unavailable_reason === "string" && unavailableReasons.has(value.unavailable_reason)
      ? value.unavailable_reason as NonNullable<NonNullable<ApiDataset["pcm_quality"]>["loudness"]>["unavailable_reason"]
      : invalidField;
  if (
    validatedFileCount === null ||
    analyzedFileCount === null ||
    analyzedFileCount > validatedFileCount ||
    blockCount === null ||
    gatedBlockCount === null ||
    gatedBlockCount > blockCount ||
    integratedLufs === null && value.integrated_lufs !== null ||
    unavailableReason === invalidField
  ) {
    return invalidField;
  }
  if (integratedLufs === null) {
    if (unavailableReason === null || gatedBlockCount !== 0) return invalidField;
    if (
      (unavailableReason === "unsupported_channel_layout" ||
        unavailableReason === "unsupported_sample_rate") &&
      (analyzedFileCount !== 0 || blockCount !== 0)
    ) return invalidField;
    if (
      unavailableReason === "insufficient_duration" &&
      (analyzedFileCount === 0 || blockCount !== 0)
    ) return invalidField;
    if (
      unavailableReason === "below_absolute_gate" &&
      (analyzedFileCount === 0 || blockCount === 0)
    ) return invalidField;
  } else if (
    unavailableReason !== null ||
    analyzedFileCount === 0 ||
    blockCount === 0 ||
    gatedBlockCount === 0
  ) {
    return invalidField;
  }
  return {
    algorithm: value.algorithm,
    scope: value.scope,
    block_duration_ms: value.block_duration_ms,
    block_overlap_percent: value.block_overlap_percent,
    absolute_gate_lufs: value.absolute_gate_lufs,
    relative_gate_lu: value.relative_gate_lu,
    analyzed_file_count: analyzedFileCount,
    block_count: blockCount,
    gated_block_count: gatedBlockCount,
    integrated_lufs: integratedLufs,
    unavailable_reason: unavailableReason,
  };
}

async function upstreamError(
  request: NextRequest,
  upstream: Response,
): Promise<NextResponse> {
  await cancelBody(upstream.body);
  const status = [401, 403, 404, 409, 413, 422, 429, 503].includes(upstream.status)
    ? upstream.status
    : 502;
  const codes: Record<number, string> = {
    401: "session_expired",
    403: "forbidden",
    404: "not_found",
    409: "conflict",
    413: "payload_too_large",
    422: "invalid_dataset",
    429: "rate_limited",
    503: "manager_unavailable",
    502: "invalid_upstream_response",
  };
  const response = bffError(codes[status] ?? "proxy_failed", status, request);
  const retryAfter = upstream.headers.get("retry-after");
  if (retryAfter && /^(0|[1-9][0-9]{0,5})$/.test(retryAfter)) {
    response.headers.set("Retry-After", retryAfter);
  }
  return response;
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

function privateJson(
  body: unknown,
  options: { referrerPolicy?: boolean } = {},
): NextResponse {
  const response = NextResponse.json(body);
  setPrivateHeaders(response.headers);
  if (options.referrerPolicy) response.headers.set("Referrer-Policy", "no-referrer");
  return response;
}

function setPrivateHeaders(headers: Headers): void {
  headers.set("Cache-Control", privateNoStore);
  headers.set("Pragma", "no-cache");
  headers.set("Vary", "Cookie");
  headers.set("X-Content-Type-Options", "nosniff");
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function hasExactKeys<T extends readonly string[]>(
  value: unknown,
  keys: T,
): value is Record<T[number], unknown> {
  if (!isRecord(value)) return false;
  const actual = Object.keys(value);
  return actual.length === keys.length && actual.every((key) => keys.includes(key));
}

function hasControlCharacter(value: string): boolean {
  return /[\u0000-\u001f\u007f]/.test(value);
}

function safeString(value: unknown, maxLength: number): string | null {
  return typeof value === "string" && value.length > 0 && value.length <= maxLength &&
    !hasControlCharacter(value)
    ? value
    : null;
}

function nullableSafeString(value: unknown, maxLength: number): string | null {
  return value === null || value === undefined ? null : safeString(value, maxLength);
}

function safeIdentifier(value: unknown): string | null {
  return typeof value === "string" && /^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/.test(value)
    ? value
    : null;
}

function safeDate(value: unknown): string | null {
  return typeof value === "string" && value.length <= 64 && Number.isFinite(Date.parse(value))
    ? value
    : null;
}

function nullableSha256(value: unknown): string | null {
  return value === null || value === undefined
    ? null
    : typeof value === "string" && /^[a-f0-9]{64}$/.test(value)
      ? value
      : null;
}

function boundedInteger(value: unknown, minimum: number, maximum: number): number | null {
  return typeof value === "number" && Number.isSafeInteger(value) &&
    value >= minimum && value <= maximum
    ? value
    : null;
}

function boundedFiniteNumber(
  value: unknown,
  minimum: number,
  maximum: number,
  maximumInclusive = true,
): number | null {
  return typeof value === "number" &&
    Number.isFinite(value) &&
    value >= minimum &&
    (maximumInclusive ? value <= maximum : value < maximum)
    ? value
    : null;
}

function nullableBoundedInteger(
  value: unknown,
  minimum: number,
  maximum: number,
): number | null {
  return value === null || value === undefined
    ? null
    : boundedInteger(value, minimum, maximum);
}

function nullableNumber(value: unknown): number | null {
  return value === null || value === undefined
    ? null
    : typeof value === "number" && Number.isFinite(value) && value >= 0
      ? value
      : null;
}

async function cancelBody(body: ReadableStream<Uint8Array> | null): Promise<void> {
  if (!body) return;
  try {
    await body.cancel();
  } catch {
    // The upstream response may already be consumed or closed.
  }
}
