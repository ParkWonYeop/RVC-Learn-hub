import type {
  ExperimentComparisonArtifact,
  ExperimentComparisonJob,
  ModelRegistryArtifact,
  ModelRegistryEntry,
  ModelRegistryMutationResponse,
  ModelRegistryPage,
  ModelRegistryRevokeReason,
  ModelRegistrySnapshot,
} from "@/lib/api-types";

const canonicalUuid =
  /^[a-f0-9]{8}-[a-f0-9]{4}-[1-5][a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$/;
const sha256 = /^[a-f0-9]{64}$/;
const runtimeImageDigest = /^sha256:[a-f0-9]{64}$/;
const reviewedCommit = /^[a-f0-9]{40}$/;
const safeJobName = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/;
const idempotencyKey = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;
const revokeReasons = new Set<ModelRegistryRevokeReason>([
  "quality_rejected",
  "security_issue",
  "operator_request",
]);

export const MODEL_REGISTRY_PAGE_SIZE = 200;
export const MODEL_REGISTRY_COLLECTION_LIMIT = 10_000;

export interface RegistryCandidateSource {
  experimentId: string;
  jobId: string;
  jobName: string;
  attemptId: string;
  attemptNumber: number;
  model: ExperimentComparisonArtifact;
  index: ExperimentComparisonArtifact | null;
}

export type RegistryMutationAction = "register" | "promote" | "revoke";

export class ModelRegistryReadError extends Error {
  constructor(
    readonly status: number,
    readonly code = "request_failed",
  ) {
    super(`Model registry read failed with status ${status}`);
    this.name = "ModelRegistryReadError";
  }
}

export class ModelRegistryMutationError extends Error {
  constructor(
    readonly status: number,
    readonly code = "request_failed",
  ) {
    super(`Model registry mutation failed with status ${status}`);
    this.name = "ModelRegistryMutationError";
  }
}

export function registryCandidateSource(
  experimentId: string,
  job: ExperimentComparisonJob,
): RegistryCandidateSource | null {
  const attempt = job.current_attempt;
  const model = job.availability.final_model;
  if (
    !canonicalUuid.test(experimentId) ||
    !canonicalUuid.test(job.id) ||
    job.config.experiment_id !== experimentId ||
    job.status !== "completed" ||
    attempt?.status !== "completed" ||
    attempt.engine_mode !== "rvc_webui" ||
    !model
  ) {
    return null;
  }
  return {
    experimentId,
    jobId: job.id,
    jobName: job.job_name,
    attemptId: attempt.id,
    attemptNumber: attempt.attempt_number,
    model,
    index: job.availability.final_index,
  };
}

export async function fetchModelRegistrySnapshot(
  experimentId: string,
  signal?: AbortSignal,
): Promise<ModelRegistrySnapshot> {
  if (!canonicalUuid.test(experimentId)) throw new ModelRegistryReadError(422, "invalid_id");
  let offset = 0;
  let expected: Omit<ModelRegistrySnapshot, "items"> | null = null;
  const items: ModelRegistryEntry[] = [];
  while (true) {
    const query = new URLSearchParams({
      offset: String(offset),
      limit: String(MODEL_REGISTRY_PAGE_SIZE),
    });
    const response = await fetch(
      `/bff/experiments/${encodeURIComponent(experimentId)}/model-registry?${query.toString()}`,
      {
        cache: "no-store",
        credentials: "same-origin",
        headers: { Accept: "application/json" },
        signal,
      },
    );
    if (!response.ok) throw await readFailure(response, "read");
    const page = modelRegistryPage(await readJson(response), experimentId, offset);
    if (!page) throw new ModelRegistryReadError(502, "invalid_upstream_response");
    if (page.total > MODEL_REGISTRY_COLLECTION_LIMIT) {
      throw new ModelRegistryReadError(409, "collection_limit_exceeded");
    }
    if (!expected) {
      expected = {
        experiment_id: page.experiment_id,
        registry_row_version: page.registry_row_version,
        active_entry_id: page.active_entry_id,
        can_manage: page.can_manage,
        total: page.total,
      };
    } else if (
      page.registry_row_version !== expected.registry_row_version ||
      page.active_entry_id !== expected.active_entry_id ||
      page.can_manage !== expected.can_manage ||
      page.total !== expected.total
    ) {
      throw new ModelRegistryReadError(409, "registry_changed_during_read");
    }
    items.push(...page.items);
    offset += page.items.length;
    if (offset >= page.total) break;
    if (page.items.length === 0) {
      throw new ModelRegistryReadError(502, "invalid_upstream_response");
    }
  }
  if (!expected || !validCompleteRegistry(items, expected.active_entry_id, expected.total)) {
    throw new ModelRegistryReadError(502, "invalid_upstream_response");
  }
  return { ...expected, items };
}

export async function registerModelCandidate(
  source: RegistryCandidateSource,
  expectedRegistryRowVersion: number,
  key: string,
  expectedActorId: string,
): Promise<{ value: ModelRegistryMutationResponse; replayed: boolean }> {
  return registryMutation(
    `/bff/experiments/${encodeURIComponent(source.experimentId)}/model-registry/candidates`,
    {
      expected_registry_row_version: expectedRegistryRowVersion,
      source_job_id: source.jobId,
      source_attempt_id: source.attemptId,
      model_artifact_id: source.model.id,
    },
    source.experimentId,
    key,
    expectedActorId,
    201,
  );
}

export async function promoteModelRegistryEntry(
  experimentId: string,
  entry: ModelRegistryEntry,
  expectedRegistryRowVersion: number,
  key: string,
  expectedActorId: string,
): Promise<{ value: ModelRegistryMutationResponse; replayed: boolean }> {
  return registryMutation(
    `/bff/experiments/${encodeURIComponent(experimentId)}/model-registry/entries/${encodeURIComponent(entry.id)}/promote`,
    {
      expected_registry_row_version: expectedRegistryRowVersion,
      expected_entry_row_version: entry.row_version,
    },
    experimentId,
    key,
    expectedActorId,
    200,
    entry.id,
  );
}

export async function revokeModelRegistryEntry(
  experimentId: string,
  entry: ModelRegistryEntry,
  expectedRegistryRowVersion: number,
  reasonCode: ModelRegistryRevokeReason,
  key: string,
  expectedActorId: string,
): Promise<{ value: ModelRegistryMutationResponse; replayed: boolean }> {
  if (!revokeReasons.has(reasonCode)) {
    throw new ModelRegistryMutationError(422, "invalid_request");
  }
  return registryMutation(
    `/bff/experiments/${encodeURIComponent(experimentId)}/model-registry/entries/${encodeURIComponent(entry.id)}/revoke`,
    {
      expected_registry_row_version: expectedRegistryRowVersion,
      expected_entry_row_version: entry.row_version,
      reason_code: reasonCode,
    },
    experimentId,
    key,
    expectedActorId,
    200,
    entry.id,
  );
}

export function applyRegistryMutation(
  snapshot: ModelRegistrySnapshot,
  response: ModelRegistryMutationResponse,
): ModelRegistrySnapshot {
  if (
    response.experiment_id !== snapshot.experiment_id ||
    response.registry_row_version < snapshot.registry_row_version
  ) {
    return snapshot;
  }
  const existing = snapshot.items.some((entry) => entry.id === response.entry.id);
  const items = snapshot.items
    .map((entry) => entry.id === response.entry.id ? response.entry : entry)
    .map((entry) => ({ ...entry, is_active: entry.id === response.active_entry_id }));
  if (!existing) items.unshift({ ...response.entry, is_active: response.entry.id === response.active_entry_id });
  return {
    ...snapshot,
    registry_row_version: response.registry_row_version,
    active_entry_id: response.active_entry_id,
    items,
    total: snapshot.total + (existing ? 0 : 1),
  };
}

export function registryErrorMessage(status: number, code = "request_failed"): string {
  if (status === 401) return "세션이 만료되었습니다. 다시 로그인해 주세요.";
  if (status === 403 || status === 404) {
    return "Model Registry를 찾을 수 없거나 관리할 권한이 없습니다.";
  }
  if (status === 409) {
    if (code === "collection_limit_exceeded") return "Registry 항목이 10,000개를 넘어 안전하게 전부 표시할 수 없습니다.";
    return "Registry 원장이 다른 요청으로 변경되었거나 현재 상태와 충돌했습니다. 최신 원장을 다시 불러오세요.";
  }
  if (status === 413) return "Registry 요청이 허용된 크기를 초과했습니다.";
  if (status === 422) return "후보 또는 상태 전이 요청이 유효하지 않습니다.";
  if (status === 429) return "Registry 요청이 제한되었습니다. 잠시 후 다시 시도해 주세요.";
  if (status === 503) return "Model/Index canonical byte를 현재 재검증할 수 없습니다.";
  return "Model Registry 원장을 처리하지 못했습니다.";
}

export function registryMutationIsUncertain(error: unknown): boolean {
  return !(error instanceof ModelRegistryMutationError) || error.status >= 500;
}

export function registryRevokeReasonLabel(reason: ModelRegistryRevokeReason | null): string {
  switch (reason) {
    case "quality_rejected": return "품질 기준 미달";
    case "security_issue": return "보안 문제";
    case "operator_request": return "운영자 요청";
    default: return "해당 없음";
  }
}

export function formatRegistryBytes(value: number): string {
  const units = ["B", "KiB", "MiB", "GiB", "TiB"] as const;
  let amount = value;
  let unit = 0;
  while (amount >= 1_024 && unit < units.length - 1) {
    amount /= 1_024;
    unit += 1;
  }
  return `${new Intl.NumberFormat("ko-KR", { maximumFractionDigits: 2 }).format(amount)} ${units[unit]}`;
}

async function registryMutation(
  path: string,
  body: unknown,
  experimentId: string,
  key: string,
  expectedActorId: string,
  expectedStatus: 200 | 201,
  expectedEntryId?: string,
): Promise<{ value: ModelRegistryMutationResponse; replayed: boolean }> {
  if (
    !canonicalUuid.test(experimentId) ||
    !canonicalUuid.test(expectedActorId) ||
    !idempotencyKey.test(key)
  ) {
    throw new ModelRegistryMutationError(422, "invalid_request");
  }
  const response = await fetch(path, {
    body: JSON.stringify(body),
    cache: "no-store",
    credentials: "same-origin",
    headers: {
      "Content-Type": "application/json",
      "Idempotency-Key": key,
      "X-RVC-Expected-Actor-ID": expectedActorId,
    },
    method: "POST",
  });
  if (!response.ok) throw await readFailure(response, "mutation");
  if (response.status !== expectedStatus) {
    throw new ModelRegistryMutationError(502, "invalid_upstream_response");
  }
  const value = modelRegistryMutationResponse(await readJson(response), experimentId);
  if (!value || expectedEntryId && value.entry.id !== expectedEntryId) {
    throw new ModelRegistryMutationError(502, "invalid_upstream_response");
  }
  return {
    value,
    replayed: response.headers.get("idempotency-replayed") === "true",
  };
}

function modelRegistryPage(
  value: unknown,
  experimentId: string,
  expectedOffset: number,
): ModelRegistryPage | null {
  if (!hasExactKeys(value, [
    "experiment_id",
    "registry_row_version",
    "active_entry_id",
    "can_manage",
    "items",
    "total",
    "offset",
    "limit",
  ])) return null;
  const total = value.total;
  const offset = value.offset;
  const limit = value.limit;
  if (
    value.experiment_id !== experimentId ||
    !registryVersion(value.registry_row_version) ||
    !nullableCanonicalUuid(value.active_entry_id) ||
    typeof value.can_manage !== "boolean" ||
    !Array.isArray(value.items) ||
    !boundedInteger(total, 0, MODEL_REGISTRY_COLLECTION_LIMIT + 1) ||
    offset !== expectedOffset ||
    !boundedInteger(limit, 1, MODEL_REGISTRY_PAGE_SIZE) ||
    value.items.length > (limit as number) ||
    (offset as number) + value.items.length > (total as number)
  ) return null;
  const items = value.items.map(modelRegistryEntry);
  if (items.some((entry) => entry === null)) return null;
  const parsed = items as ModelRegistryEntry[];
  if (
    new Set(parsed.map((entry) => entry.id)).size !== parsed.length ||
    parsed.some((entry) => entry.experiment_id !== experimentId) ||
    parsed.some((entry) => entry.is_active && entry.id !== value.active_entry_id) ||
    parsed.filter((entry) => entry.is_active).length > 1
  ) return null;
  return {
    experiment_id: experimentId,
    registry_row_version: value.registry_row_version as number,
    active_entry_id: value.active_entry_id as string | null,
    can_manage: value.can_manage,
    items: parsed,
    total: total as number,
    offset: offset as number,
    limit: limit as number,
  };
}

function modelRegistryMutationResponse(
  value: unknown,
  experimentId: string,
): ModelRegistryMutationResponse | null {
  if (!hasExactKeys(value, ["experiment_id", "registry_row_version", "active_entry_id", "entry"])) {
    return null;
  }
  const entry = modelRegistryEntry(value.entry);
  if (
    value.experiment_id !== experimentId ||
    !positiveVersion(value.registry_row_version) ||
    !nullableCanonicalUuid(value.active_entry_id) ||
    !entry ||
    entry.experiment_id !== experimentId ||
    entry.is_active !== (entry.id === value.active_entry_id)
  ) return null;
  return {
    experiment_id: experimentId,
    registry_row_version: value.registry_row_version as number,
    active_entry_id: value.active_entry_id as string | null,
    entry,
  };
}

function modelRegistryEntry(value: unknown): ModelRegistryEntry | null {
  if (!hasExactKeys(value, [
    "id",
    "row_version",
    "status",
    "is_active",
    "experiment_id",
    "source_job_id",
    "source_job_name",
    "source_attempt_id",
    "source_attempt_number",
    "engine_mode",
    "model",
    "index",
    "job_config_sha256",
    "rvc_commit_hash",
    "runtime_image_digest",
    "runtime_asset_manifest_sha256",
    "created_at",
    "approved_at",
    "revoked_at",
    "revoke_reason",
  ])) return null;
  const model = modelRegistryArtifact(value.model);
  const index = value.index === null ? null : modelRegistryArtifact(value.index);
  const createdAt = timestamp(value.created_at);
  const approvedAt = value.approved_at === null ? null : timestamp(value.approved_at);
  const revokedAt = value.revoked_at === null ? null : timestamp(value.revoked_at);
  const reason = value.revoke_reason;
  if (
    !canonicalUuid.test(String(value.id)) ||
    !positiveVersion(value.row_version) ||
    !["candidate", "approved", "revoked"].includes(String(value.status)) ||
    typeof value.is_active !== "boolean" ||
    !canonicalUuid.test(String(value.experiment_id)) ||
    !canonicalUuid.test(String(value.source_job_id)) ||
    typeof value.source_job_name !== "string" ||
    !safeJobName.test(value.source_job_name) ||
    !canonicalUuid.test(String(value.source_attempt_id)) ||
    !boundedInteger(value.source_attempt_number, 1, 2_147_483_647) ||
    value.engine_mode !== "rvc_webui" ||
    !model ||
    value.index !== null && !index ||
    typeof value.job_config_sha256 !== "string" || !sha256.test(value.job_config_sha256) ||
    typeof value.rvc_commit_hash !== "string" || !reviewedCommit.test(value.rvc_commit_hash) ||
    typeof value.runtime_image_digest !== "string" || !runtimeImageDigest.test(value.runtime_image_digest) ||
    typeof value.runtime_asset_manifest_sha256 !== "string" || !sha256.test(value.runtime_asset_manifest_sha256) ||
    !createdAt ||
    value.approved_at !== null && !approvedAt ||
    value.revoked_at !== null && !revokedAt ||
    reason !== null && (typeof reason !== "string" || !revokeReasons.has(reason as ModelRegistryRevokeReason))
  ) return null;
  const status = value.status as ModelRegistryEntry["status"];
  if (
    status === "candidate" && (value.is_active || approvedAt !== null || revokedAt !== null || reason !== null) ||
    status === "approved" && (!approvedAt || revokedAt !== null || reason !== null) ||
    status === "revoked" && (value.is_active || !revokedAt || reason === null) ||
    approvedAt && Date.parse(approvedAt) < Date.parse(createdAt) ||
    revokedAt && Date.parse(revokedAt) < Date.parse(approvedAt ?? createdAt)
  ) return null;
  return {
    id: value.id as string,
    row_version: value.row_version as number,
    status,
    is_active: value.is_active,
    experiment_id: value.experiment_id as string,
    source_job_id: value.source_job_id as string,
    source_job_name: value.source_job_name,
    source_attempt_id: value.source_attempt_id as string,
    source_attempt_number: value.source_attempt_number as number,
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
    revoke_reason: reason as ModelRegistryRevokeReason | null,
  };
}

function modelRegistryArtifact(value: unknown): ModelRegistryArtifact | null {
  if (!hasExactKeys(value, ["id", "filename", "size_bytes", "sha256"])) return null;
  if (
    !canonicalUuid.test(String(value.id)) ||
    typeof value.filename !== "string" ||
    value.filename.length < 1 ||
    value.filename.length > 255 ||
    value.filename === "." ||
    value.filename === ".." ||
    /[\\/\u0000-\u001f\u007f]/.test(value.filename) ||
    !boundedInteger(value.size_bytes, 1, Number.MAX_SAFE_INTEGER) ||
    typeof value.sha256 !== "string" ||
    !sha256.test(value.sha256)
  ) return null;
  return {
    id: value.id as string,
    filename: value.filename,
    size_bytes: value.size_bytes as number,
    sha256: value.sha256,
  };
}

function validCompleteRegistry(
  items: ModelRegistryEntry[],
  activeEntryId: string | null,
  total: number,
): boolean {
  if (items.length !== total || new Set(items.map((entry) => entry.id)).size !== items.length) return false;
  if (new Set(items.map((entry) => entry.model.id)).size !== items.length) return false;
  const active = items.filter((entry) => entry.is_active);
  return activeEntryId === null
    ? active.length === 0
    : active.length === 1 && active[0]?.id === activeEntryId && active[0].status === "approved";
}

async function readFailure(
  response: Response,
  kind: "read" | "mutation",
): Promise<ModelRegistryReadError | ModelRegistryMutationError> {
  const value = await readJson(response);
  const code = isRecord(value) && Object.keys(value).length === 1 && typeof value.error === "string"
    ? value.error
    : "request_failed";
  return kind === "read"
    ? new ModelRegistryReadError(response.status, code)
    : new ModelRegistryMutationError(response.status, code);
}

async function readJson(response: Response): Promise<unknown> {
  try {
    return await response.json();
  } catch {
    return null;
  }
}

function timestamp(value: unknown): string | null {
  return typeof value === "string" && value.length <= 64 && Number.isFinite(Date.parse(value))
    ? value
    : null;
}

function nullableCanonicalUuid(value: unknown): boolean {
  return value === null || typeof value === "string" && canonicalUuid.test(value);
}

function positiveVersion(value: unknown): boolean {
  return boundedInteger(value, 1, 2_147_483_647);
}

function registryVersion(value: unknown): boolean {
  return boundedInteger(value, 0, 2_147_483_647);
}

function boundedInteger(value: unknown, minimum: number, maximum: number): boolean {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= minimum && value <= maximum;
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
