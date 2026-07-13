import "server-only";

import { NextRequest, NextResponse } from "next/server";
import { bffError } from "./bff-proxy";
import { managerRawRequest } from "./manager-api";
import { isSameOriginMutation, isSameOriginRead } from "./request-security";
import { SESSION_COOKIE_NAME } from "./session-cookie";

const MAX_BODY_BYTES = 4_096;
const MAX_UPSTREAM_BYTES = 1_048_576;
const MAX_ROW_VERSION = 2_147_483_647;
const privateNoStore = "private, no-cache, no-store, must-revalidate";
const canonicalUuidPattern =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const idempotencyKeyPattern = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;
const sha256Pattern = /^[a-f0-9]{64}$/;
const gitCommitPattern = /^[a-f0-9]{40}$/;
const imageDigestPattern = /^sha256:[a-f0-9]{64}$/;
const safeJobNamePattern = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/;
const timestampPattern =
  /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:Z|[+-]\d{2}:\d{2})$/;
const registryStatuses = new Set(["candidate", "approved", "revoked"]);
const revokeReasons = new Set([
  "quality_rejected",
  "security_issue",
  "operator_request",
]);

type RegistryStatus = "candidate" | "approved" | "revoked";
type RevokeReason = "quality_rejected" | "security_issue" | "operator_request";

export type PublicModelRegistryArtifact = {
  id: string;
  filename: string;
  size_bytes: number;
  sha256: string;
};

export type PublicModelRegistryEntry = {
  id: string;
  row_version: number;
  status: RegistryStatus;
  is_active: boolean;
  experiment_id: string;
  source_job_id: string;
  source_job_name: string;
  source_attempt_id: string;
  source_attempt_number: number;
  engine_mode: "rvc_webui";
  model: PublicModelRegistryArtifact;
  index: PublicModelRegistryArtifact | null;
  job_config_sha256: string;
  rvc_commit_hash: string;
  runtime_image_digest: string;
  runtime_asset_manifest_sha256: string;
  created_at: string;
  approved_at: string | null;
  revoked_at: string | null;
  revoke_reason: RevokeReason | null;
};

export type PublicModelRegistryList = {
  experiment_id: string;
  registry_row_version: number;
  active_entry_id: string | null;
  can_manage: boolean;
  items: PublicModelRegistryEntry[];
  total: number;
  offset: number;
  limit: number;
};

export type PublicModelRegistryMutation = {
  experiment_id: string;
  registry_row_version: number;
  active_entry_id: string | null;
  entry: PublicModelRegistryEntry;
};

type CandidateRequest = {
  expected_registry_row_version: number;
  source_job_id: string;
  source_attempt_id: string;
  model_artifact_id: string;
};

type EntryMutationRequest = {
  expected_registry_row_version: number;
  expected_entry_row_version: number;
};

type RevokeRequest = EntryMutationRequest & { reason_code: RevokeReason };

type MutationKind =
  | { kind: "candidate"; payload: CandidateRequest }
  | { kind: "promote"; payload: EntryMutationRequest; entryId: string }
  | { kind: "revoke"; payload: RevokeRequest; entryId: string };

export function isCanonicalRegistryId(value: string): boolean {
  return canonicalUuidPattern.test(value);
}

export async function proxyModelRegistryList(
  request: NextRequest,
  path: `/api/v1/${string}`,
  expectedExperimentId: string,
  expectedOffset: number,
  expectedLimit: number,
): Promise<NextResponse> {
  if (!isSameOriginRead(request)) return bffError("forbidden", 403);
  if (request.headers.has("authorization")) return bffError("invalid_request", 400);
  const upstream = await requestManager(request, path, "GET");
  if (upstream instanceof NextResponse) return upstream;
  if (!upstream.ok) return upstreamError(request, upstream);
  if (upstream.status !== 200) {
    await cancelBody(upstream.body);
    return bffError("invalid_upstream_response", 502);
  }
  const value = await readBoundedResponseJson(upstream, MAX_UPSTREAM_BYTES);
  const registry = publicRegistryList(
    value,
    expectedExperimentId,
    expectedOffset,
    expectedLimit,
  );
  if (!registry) return bffError("invalid_upstream_response", 502);
  return privateJson(registry, 200);
}

export async function createModelRegistryCandidate(
  request: NextRequest,
  expectedExperimentId: string,
): Promise<NextResponse> {
  const guarded = await guardedMutation(request, candidateRequest);
  if (guarded instanceof NextResponse) return guarded;
  return mutate(
    request,
    `/api/v1/experiments/${expectedExperimentId}/model-registry/candidates`,
    201,
    guarded.idempotencyKey,
    guarded.expectedActorId,
    {
      kind: "candidate",
      payload: guarded.payload,
    },
    expectedExperimentId,
  );
}

export async function promoteModelRegistryEntry(
  request: NextRequest,
  expectedExperimentId: string,
  expectedEntryId: string,
): Promise<NextResponse> {
  const guarded = await guardedMutation(request, entryMutationRequest);
  if (guarded instanceof NextResponse) return guarded;
  return mutate(
    request,
    `/api/v1/experiments/${expectedExperimentId}/model-registry/entries/${expectedEntryId}/promote`,
    200,
    guarded.idempotencyKey,
    guarded.expectedActorId,
    {
      kind: "promote",
      payload: guarded.payload,
      entryId: expectedEntryId,
    },
    expectedExperimentId,
  );
}

export async function revokeModelRegistryEntry(
  request: NextRequest,
  expectedExperimentId: string,
  expectedEntryId: string,
): Promise<NextResponse> {
  const guarded = await guardedMutation(request, revokeRequest);
  if (guarded instanceof NextResponse) return guarded;
  return mutate(
    request,
    `/api/v1/experiments/${expectedExperimentId}/model-registry/entries/${expectedEntryId}/revoke`,
    200,
    guarded.idempotencyKey,
    guarded.expectedActorId,
    {
      kind: "revoke",
      payload: guarded.payload,
      entryId: expectedEntryId,
    },
    expectedExperimentId,
  );
}

async function guardedMutation<T>(
  request: NextRequest,
  parse: (value: unknown) => T | null,
): Promise<{ payload: T; idempotencyKey: string; expectedActorId: string } | NextResponse> {
  if (!isSameOriginMutation(request)) return bffError("forbidden", 403);
  if (request.headers.has("authorization")) return bffError("invalid_request", 400);
  if (process.env.DASHBOARD_DEMO_MODE === "true") {
    return bffError("demo_mode_read_only", 409);
  }
  const idempotencyKey = request.headers.get("idempotency-key");
  if (!idempotencyKey || !idempotencyKeyPattern.test(idempotencyKey)) {
    return bffError("invalid_idempotency_key", 400);
  }
  const expectedActorId = request.headers.get("x-rvc-expected-actor-id");
  if (!expectedActorId || !canonicalUuidPattern.test(expectedActorId)) {
    return bffError("invalid_expected_actor", 400);
  }
  const body = await readBoundedRequestJson(request, MAX_BODY_BYTES);
  if (!body.ok) {
    return bffError(
      body.tooLarge ? "payload_too_large" : "invalid_request",
      body.tooLarge ? 413 : 400,
    );
  }
  const payload = parse(body.value);
  return payload
    ? { payload, idempotencyKey, expectedActorId }
    : bffError("invalid_request", 400);
}

async function mutate(
  request: NextRequest,
  path: `/api/v1/${string}`,
  expectedStatus: 200 | 201,
  idempotencyKey: string,
  expectedActorId: string,
  mutation: MutationKind,
  expectedExperimentId: string,
): Promise<NextResponse> {
  const upstream = await requestManager(
    request,
    path,
    "POST",
    mutation.payload,
    idempotencyKey,
    expectedActorId,
  );
  if (upstream instanceof NextResponse) return upstream;
  if (!upstream.ok) return upstreamError(request, upstream);
  if (upstream.status !== expectedStatus) {
    await cancelBody(upstream.body);
    return bffError("invalid_upstream_response", 502);
  }
  const value = await readBoundedResponseJson(upstream, MAX_UPSTREAM_BYTES);
  const result = publicRegistryMutation(value, expectedExperimentId, mutation);
  if (!result) return bffError("invalid_upstream_response", 502);
  const response = privateJson(result, expectedStatus);
  if (upstream.headers.get("idempotency-replayed") === "true") {
    response.headers.set("Idempotency-Replayed", "true");
  }
  return response;
}

async function requestManager(
  request: NextRequest,
  path: `/api/v1/${string}`,
  method: "GET" | "POST",
  body?: unknown,
  idempotencyKey?: string,
  expectedActorId?: string,
): Promise<Response | NextResponse> {
  const token = request.cookies.get(SESSION_COOKIE_NAME)?.value;
  if (!token) return bffError("session_required", 401, request);
  try {
    const { response } = await managerRawRequest(path, {
      body,
      expectedActorId,
      idempotencyKey,
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

function candidateRequest(value: unknown): CandidateRequest | null {
  if (!hasExactKeys(value, [
    "expected_registry_row_version",
    "source_job_id",
    "source_attempt_id",
    "model_artifact_id",
  ])) {
    return null;
  }
  if (
    !registryVersion(value.expected_registry_row_version) ||
    !canonicalUuid(value.source_job_id) ||
    !canonicalUuid(value.source_attempt_id) ||
    !canonicalUuid(value.model_artifact_id)
  ) {
    return null;
  }
  return value as CandidateRequest;
}

function entryMutationRequest(value: unknown): EntryMutationRequest | null {
  if (!hasExactKeys(value, [
    "expected_registry_row_version",
    "expected_entry_row_version",
  ])) {
    return null;
  }
  if (
    !rowVersion(value.expected_registry_row_version) ||
    !rowVersion(value.expected_entry_row_version)
  ) {
    return null;
  }
  return value as EntryMutationRequest;
}

function revokeRequest(value: unknown): RevokeRequest | null {
  if (!hasExactKeys(value, [
    "expected_registry_row_version",
    "expected_entry_row_version",
    "reason_code",
  ])) {
    return null;
  }
  if (
    !rowVersion(value.expected_registry_row_version) ||
    !rowVersion(value.expected_entry_row_version) ||
    !revokeReason(value.reason_code)
  ) {
    return null;
  }
  return value as RevokeRequest;
}

function publicRegistryList(
  value: unknown,
  expectedExperimentId: string,
  expectedOffset: number,
  expectedLimit: number,
): PublicModelRegistryList | null {
  if (!isRecord(value)) return null;
  const experimentId = canonicalUuid(value.experiment_id);
  const registryRowVersion = integer(value.registry_row_version, 0, MAX_ROW_VERSION);
  const activeEntryId = nullableUuid(value.active_entry_id);
  const total = integer(value.total, 0, MAX_ROW_VERSION);
  const offset = integer(value.offset, 0, MAX_ROW_VERSION);
  const limit = integer(value.limit, 1, 200);
  if (
    !experimentId ||
    experimentId !== expectedExperimentId ||
    registryRowVersion === null ||
    activeEntryId === undefined ||
    typeof value.can_manage !== "boolean" ||
    !Array.isArray(value.items) ||
    total === null ||
    offset !== expectedOffset ||
    limit !== expectedLimit ||
    value.items.length > limit ||
    value.items.length > total ||
    (value.items.length > 0 && offset + value.items.length > total)
  ) {
    return null;
  }
  const items = value.items.map((item) => publicRegistryEntry(item, experimentId));
  if (items.some((item) => item === null)) return null;
  const parsed = items as PublicModelRegistryEntry[];
  if (
    new Set(parsed.map((item) => item.id)).size !== parsed.length ||
    new Set(parsed.map((item) => item.model.id)).size !== parsed.length
  ) {
    return null;
  }
  const activeItems = parsed.filter((item) => item.is_active);
  const includedActiveEntry = activeEntryId === null
    ? null
    : parsed.find((item) => item.id === activeEntryId) ?? null;
  if (
    activeItems.length > 1 ||
    activeEntryId === null && activeItems.length !== 0 ||
    activeItems.length === 1 && activeItems[0]?.id !== activeEntryId ||
    includedActiveEntry !== null && !includedActiveEntry.is_active
  ) {
    return null;
  }
  return {
    experiment_id: experimentId,
    registry_row_version: registryRowVersion,
    active_entry_id: activeEntryId,
    can_manage: value.can_manage,
    items: parsed,
    total,
    offset,
    limit,
  };
}

function publicRegistryMutation(
  value: unknown,
  expectedExperimentId: string,
  mutation: MutationKind,
): PublicModelRegistryMutation | null {
  if (!isRecord(value)) return null;
  const experimentId = canonicalUuid(value.experiment_id);
  const registryRowVersion = integer(value.registry_row_version, 1, MAX_ROW_VERSION);
  const activeEntryId = nullableUuid(value.active_entry_id);
  const entry = publicRegistryEntry(value.entry, expectedExperimentId);
  if (
    !experimentId ||
    experimentId !== expectedExperimentId ||
    registryRowVersion === null ||
    registryRowVersion !== mutation.payload.expected_registry_row_version + 1 ||
    activeEntryId === undefined ||
    !entry
  ) {
    return null;
  }
  if (entry.is_active !== (activeEntryId === entry.id)) return null;
  if (mutation.kind === "candidate") {
    if (
      entry.status !== "candidate" ||
      entry.is_active ||
      entry.row_version !== 1 ||
      entry.source_job_id !== mutation.payload.source_job_id ||
      entry.source_attempt_id !== mutation.payload.source_attempt_id ||
      entry.model.id !== mutation.payload.model_artifact_id
    ) {
      return null;
    }
  } else if (mutation.kind === "promote") {
    if (
      entry.id !== mutation.entryId ||
      entry.status !== "approved" ||
      !entry.is_active ||
      activeEntryId !== entry.id ||
      entry.row_version !== mutation.payload.expected_entry_row_version + 1
    ) {
      return null;
    }
  } else if (
    entry.id !== mutation.entryId ||
    entry.status !== "revoked" ||
    entry.is_active ||
    entry.revoke_reason !== mutation.payload.reason_code ||
    entry.row_version !== mutation.payload.expected_entry_row_version + 1
  ) {
    return null;
  }
  return {
    experiment_id: experimentId,
    registry_row_version: registryRowVersion,
    active_entry_id: activeEntryId,
    entry,
  };
}

function publicRegistryEntry(
  value: unknown,
  expectedExperimentId: string,
): PublicModelRegistryEntry | null {
  if (!isRecord(value)) return null;
  const id = canonicalUuid(value.id);
  const rowVersionValue = integer(value.row_version, 1, MAX_ROW_VERSION);
  const status = registryStatus(value.status);
  const experimentId = canonicalUuid(value.experiment_id);
  const sourceJobId = canonicalUuid(value.source_job_id);
  const sourceAttemptId = canonicalUuid(value.source_attempt_id);
  const sourceAttemptNumber = integer(value.source_attempt_number, 1, MAX_ROW_VERSION);
  const model = publicRegistryArtifact(value.model, "model");
  const index = value.index === null ? null : publicRegistryArtifact(value.index, "index");
  const createdAt = timestamp(value.created_at);
  const approvedAt = nullableTimestamp(value.approved_at);
  const revokedAt = nullableTimestamp(value.revoked_at);
  const revokeReasonValue = nullableRevokeReason(value.revoke_reason);
  if (
    !id ||
    rowVersionValue === null ||
    !status ||
    typeof value.is_active !== "boolean" ||
    !experimentId ||
    experimentId !== expectedExperimentId ||
    !sourceJobId ||
    typeof value.source_job_name !== "string" ||
    !safeJobNamePattern.test(value.source_job_name) ||
    !sourceAttemptId ||
    sourceAttemptNumber === null ||
    value.engine_mode !== "rvc_webui" ||
    !model ||
    (value.index !== null && !index) ||
    typeof value.job_config_sha256 !== "string" ||
    !sha256Pattern.test(value.job_config_sha256) ||
    typeof value.rvc_commit_hash !== "string" ||
    !gitCommitPattern.test(value.rvc_commit_hash) ||
    typeof value.runtime_image_digest !== "string" ||
    !imageDigestPattern.test(value.runtime_image_digest) ||
    typeof value.runtime_asset_manifest_sha256 !== "string" ||
    !sha256Pattern.test(value.runtime_asset_manifest_sha256) ||
    !createdAt ||
    approvedAt === undefined ||
    revokedAt === undefined ||
    revokeReasonValue === undefined
  ) {
    return null;
  }
  if (
    status === "candidate" &&
      (value.is_active || approvedAt !== null || revokedAt !== null || revokeReasonValue !== null) ||
    status === "approved" &&
      (approvedAt === null || revokedAt !== null || revokeReasonValue !== null) ||
    status === "revoked" &&
      (value.is_active || revokedAt === null || revokeReasonValue === null) ||
    value.is_active && status !== "approved"
  ) {
    return null;
  }
  const createdTime = Date.parse(createdAt);
  const approvedTime = approvedAt === null ? null : Date.parse(approvedAt);
  const revokedTime = revokedAt === null ? null : Date.parse(revokedAt);
  if (
    approvedTime !== null && approvedTime < createdTime ||
    revokedTime !== null && revokedTime < (approvedTime ?? createdTime)
  ) {
    return null;
  }
  return {
    id,
    row_version: rowVersionValue,
    status,
    is_active: value.is_active,
    experiment_id: experimentId,
    source_job_id: sourceJobId,
    source_job_name: value.source_job_name,
    source_attempt_id: sourceAttemptId,
    source_attempt_number: sourceAttemptNumber,
    engine_mode: "rvc_webui",
    model,
    index,
    job_config_sha256: value.job_config_sha256,
    rvc_commit_hash: value.rvc_commit_hash,
    runtime_image_digest: value.runtime_image_digest,
    runtime_asset_manifest_sha256: value.runtime_asset_manifest_sha256,
    created_at: createdAt,
    approved_at: approvedAt,
    revoked_at: revokedAt,
    revoke_reason: revokeReasonValue,
  };
}

function publicRegistryArtifact(
  value: unknown,
  kind: "model" | "index",
): PublicModelRegistryArtifact | null {
  if (!isRecord(value)) return null;
  const id = canonicalUuid(value.id);
  const sizeBytes = integer(value.size_bytes, 1, Number.MAX_SAFE_INTEGER);
  if (
    !id ||
    !safeArtifactFilename(value.filename, kind) ||
    sizeBytes === null ||
    typeof value.sha256 !== "string" ||
    !sha256Pattern.test(value.sha256)
  ) {
    return null;
  }
  return {
    id,
    filename: value.filename as string,
    size_bytes: sizeBytes,
    sha256: value.sha256,
  };
}

function safeArtifactFilename(value: unknown, kind: "model" | "index"): value is string {
  if (
    typeof value !== "string" ||
    value.length < 1 ||
    value.length > 255 ||
    value === "." ||
    value === ".." ||
    value.includes("/") ||
    value.includes("\\") ||
    /[\u0000-\u001f\u007f]/.test(value)
  ) {
    return false;
  }
  return kind === "model" ? value.endsWith(".pth") : value.endsWith(".index");
}

async function upstreamError(
  request: NextRequest,
  upstream: Response,
): Promise<NextResponse> {
  await cancelBody(upstream.body);
  const status = [400, 401, 403, 404, 409, 413, 422, 429, 503].includes(upstream.status)
    ? upstream.status
    : 502;
  const codes: Record<number, string> = {
    400: "invalid_request",
    401: "session_expired",
    403: "forbidden",
    404: "not_found",
    409: "conflict",
    413: "payload_too_large",
    422: "invalid_request",
    429: "rate_limited",
    503: "manager_unavailable",
    502: "invalid_upstream_response",
  };
  const response = bffError(codes[status] ?? "invalid_upstream_response", status, request);
  const retryAfter = upstream.headers.get("retry-after");
  if (retryAfter && /^(0|[1-9][0-9]{0,5})$/.test(retryAfter)) {
    response.headers.set("Retry-After", retryAfter);
  }
  return response;
}

async function readBoundedRequestJson(
  request: NextRequest,
  maximumBytes: number,
): Promise<{ ok: true; value: unknown } | { ok: false; tooLarge: boolean }> {
  const mediaType = request.headers.get("content-type")?.split(";", 1)[0]?.trim().toLowerCase();
  if (mediaType !== "application/json") return { ok: false, tooLarge: false };
  const contentEncoding = request.headers.get("content-encoding");
  if (contentEncoding !== null && contentEncoding.toLowerCase() !== "identity") {
    return { ok: false, tooLarge: false };
  }
  const declared = request.headers.get("content-length");
  if (declared && (!/^(0|[1-9][0-9]*)$/.test(declared) || Number(declared) > maximumBytes)) {
    return { ok: false, tooLarge: true };
  }
  if (!request.body) return { ok: false, tooLarge: false };
  const bytes = await readBoundedBytes(request.body, maximumBytes);
  if (bytes === null) return { ok: false, tooLarge: true };
  try {
    return {
      ok: true,
      value: JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes)),
    };
  } catch {
    return { ok: false, tooLarge: false };
  }
}

async function readBoundedResponseJson(
  response: Response,
  maximumBytes: number,
): Promise<unknown> {
  if (!response.headers.get("content-type")?.startsWith("application/json")) {
    await cancelBody(response.body);
    return null;
  }
  if (!response.body) return null;
  const bytes = await readBoundedBytes(response.body, maximumBytes);
  if (bytes === null) return null;
  try {
    return JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes));
  } catch {
    return null;
  }
}

async function readBoundedBytes(
  body: ReadableStream<Uint8Array>,
  maximumBytes: number,
): Promise<Uint8Array | null> {
  const reader = body.getReader();
  const chunks: Uint8Array[] = [];
  let length = 0;
  try {
    while (true) {
      const chunk = await reader.read();
      if (chunk.done) break;
      length += chunk.value.byteLength;
      if (length > maximumBytes) {
        await reader.cancel("body limit exceeded");
        return null;
      }
      chunks.push(chunk.value);
    }
  } catch {
    return null;
  }
  const bytes = new Uint8Array(length);
  let offset = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return bytes;
}

function privateJson(body: unknown, status: number): NextResponse {
  const response = NextResponse.json(body, { status });
  response.headers.set("Cache-Control", privateNoStore);
  response.headers.set("Pragma", "no-cache");
  response.headers.set("Vary", "Cookie");
  response.headers.set("X-Content-Type-Options", "nosniff");
  return response;
}

function canonicalUuid(value: unknown): string | null {
  return typeof value === "string" && canonicalUuidPattern.test(value) ? value : null;
}

function nullableUuid(value: unknown): string | null | undefined {
  return value === null ? null : canonicalUuid(value) ?? undefined;
}

function rowVersion(value: unknown): value is number {
  return integer(value, 1, MAX_ROW_VERSION) !== null;
}

function registryVersion(value: unknown): value is number {
  return integer(value, 0, MAX_ROW_VERSION) !== null;
}

function registryStatus(value: unknown): RegistryStatus | null {
  return typeof value === "string" && registryStatuses.has(value)
    ? value as RegistryStatus
    : null;
}

function revokeReason(value: unknown): value is RevokeReason {
  return typeof value === "string" && revokeReasons.has(value);
}

function nullableRevokeReason(value: unknown): RevokeReason | null | undefined {
  return value === null ? null : revokeReason(value) ? value : undefined;
}

function timestamp(value: unknown): string | null {
  if (
    typeof value !== "string" ||
    value.length > 40 ||
    !timestampPattern.test(value) ||
    !Number.isFinite(Date.parse(value))
  ) {
    return null;
  }
  return value;
}

function nullableTimestamp(value: unknown): string | null | undefined {
  return value === null ? null : timestamp(value) ?? undefined;
}

function integer(value: unknown, minimum: number, maximum: number): number | null {
  return typeof value === "number" &&
    Number.isSafeInteger(value) &&
    value >= minimum &&
    value <= maximum
    ? value
    : null;
}

function hasExactKeys<T extends readonly string[]>(
  value: unknown,
  keys: T,
): value is Record<T[number], unknown> {
  if (!isRecord(value)) return false;
  const actual = Object.keys(value);
  return actual.length === keys.length && actual.every((key) => keys.includes(key));
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

async function cancelBody(body: ReadableStream<Uint8Array> | null): Promise<void> {
  if (!body) return;
  try {
    await body.cancel();
  } catch {
    // A fully consumed response body is already closed.
  }
}
