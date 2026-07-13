import type {
  ModelRegistryArtifact,
  ModelRegistryEntry,
  ModelRegistryMutationResponse,
  ModelRegistryRevokeReason,
  ModelRegistrySnapshot,
} from "@/lib/api-types";
import {
  promoteModelRegistryEntry,
  registerModelCandidate,
  revokeModelRegistryEntry,
  type RegistryCandidateSource,
} from "@/lib/client/model-registry";

const canonicalUuid =
  /^[a-f0-9]{8}-[a-f0-9]{4}-[1-5][a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$/;

interface RegistryMutationIntentBase {
  actorId: string;
  baselineFingerprint: string;
  expectedRegistryRowVersion: number;
  experimentId: string;
  idempotencyKey: string;
}

export type RegistryMutationIntent =
  | RegistryMutationIntentBase & {
    action: "register";
    source: RegistryCandidateSource;
  }
  | RegistryMutationIntentBase & {
    action: "promote";
    entry: ModelRegistryEntry;
  }
  | RegistryMutationIntentBase & {
    action: "revoke";
    entry: ModelRegistryEntry;
    reason: ModelRegistryRevokeReason;
  };

export type RegistryReconciliationState = "applied" | "changed" | "unchanged";

export interface RegistryReconciliationResult {
  snapshot: ModelRegistrySnapshot;
  state: RegistryReconciliationState;
}

export class RegistryActorIdentityError extends Error {
  constructor(
    readonly status: number,
    readonly code = "request_failed",
  ) {
    super(`Registry actor identity check failed with status ${status}`);
    this.name = "RegistryActorIdentityError";
  }
}

export class RegistryActorChangedError extends Error {
  constructor() {
    super("Registry mutation actor changed");
    this.name = "RegistryActorChangedError";
  }
}

export function createRegistryRegisterIntent(
  snapshot: ModelRegistrySnapshot,
  source: RegistryCandidateSource,
  idempotencyKey: string,
  actorId: string,
): RegistryMutationIntent {
  return {
    action: "register",
    actorId,
    baselineFingerprint: registrySnapshotFingerprint(snapshot),
    expectedRegistryRowVersion: snapshot.registry_row_version,
    experimentId: snapshot.experiment_id,
    idempotencyKey,
    source: cloneCandidateSource(source),
  };
}

export function createRegistryPromoteIntent(
  snapshot: ModelRegistrySnapshot,
  entry: ModelRegistryEntry,
  idempotencyKey: string,
  actorId: string,
): RegistryMutationIntent {
  return {
    action: "promote",
    actorId,
    baselineFingerprint: registrySnapshotFingerprint(snapshot),
    entry: cloneEntry(entry),
    expectedRegistryRowVersion: snapshot.registry_row_version,
    experimentId: snapshot.experiment_id,
    idempotencyKey,
  };
}

export function createRegistryRevokeIntent(
  snapshot: ModelRegistrySnapshot,
  entry: ModelRegistryEntry,
  reason: ModelRegistryRevokeReason,
  idempotencyKey: string,
  actorId: string,
): RegistryMutationIntent {
  return {
    action: "revoke",
    actorId,
    baselineFingerprint: registrySnapshotFingerprint(snapshot),
    entry: cloneEntry(entry),
    expectedRegistryRowVersion: snapshot.registry_row_version,
    experimentId: snapshot.experiment_id,
    idempotencyKey,
    reason,
  };
}

export async function executeRegistryMutationIntent(
  intent: RegistryMutationIntent,
): Promise<{ replayed: boolean; value: ModelRegistryMutationResponse }> {
  switch (intent.action) {
    case "register":
      return registerModelCandidate(
        intent.source,
        intent.expectedRegistryRowVersion,
        intent.idempotencyKey,
        intent.actorId,
      );
    case "promote":
      return promoteModelRegistryEntry(
        intent.experimentId,
        intent.entry,
        intent.expectedRegistryRowVersion,
        intent.idempotencyKey,
        intent.actorId,
      );
    case "revoke":
      return revokeModelRegistryEntry(
        intent.experimentId,
        intent.entry,
        intent.expectedRegistryRowVersion,
        intent.reason,
        intent.idempotencyKey,
        intent.actorId,
      );
  }
}

export async function verifyRegistryIntentActor(
  intent: RegistryMutationIntent,
  signal?: AbortSignal,
): Promise<void> {
  let response: Response;
  try {
    response = await fetch("/bff/session/identity", {
      cache: "no-store",
      credentials: "same-origin",
      headers: { Accept: "application/json" },
      signal,
    });
  } catch {
    throw new RegistryActorIdentityError(502, "request_failed");
  }
  const value = await readIdentityJson(response);
  if (!response.ok) {
    const code = hasExactKeys(value, ["error"]) && typeof value.error === "string"
      ? value.error
      : "request_failed";
    throw new RegistryActorIdentityError(response.status, code);
  }
  if (
    !hasExactKeys(value, ["actor_id"]) ||
    typeof value.actor_id !== "string" ||
    !canonicalUuid.test(value.actor_id)
  ) {
    throw new RegistryActorIdentityError(502, "invalid_upstream_response");
  }
  if (value.actor_id !== intent.actorId) throw new RegistryActorChangedError();
}

export function reconcileRegistryMutation(
  intent: RegistryMutationIntent,
  snapshot: ModelRegistrySnapshot,
): RegistryReconciliationResult {
  if (registryIntentIsApplied(intent, snapshot)) return { snapshot, state: "applied" };
  if (
    snapshot.experiment_id === intent.experimentId &&
    registrySnapshotFingerprint(snapshot) === intent.baselineFingerprint
  ) {
    return { snapshot, state: "unchanged" };
  }
  return { snapshot, state: "changed" };
}

export function registryIntentDescription(intent: RegistryMutationIntent): string {
  switch (intent.action) {
    case "register": return `${intent.source.jobName} 후보 등록`;
    case "promote": return `${intent.entry.source_job_name} Champion 승인`;
    case "revoke": return `${intent.entry.source_job_name} 폐기`;
  }
}

function registryIntentIsApplied(
  intent: RegistryMutationIntent,
  snapshot: ModelRegistrySnapshot,
): boolean {
  if (snapshot.experiment_id !== intent.experimentId) return false;
  switch (intent.action) {
    case "register":
      return snapshot.items.some((entry) =>
        entry.source_job_id === intent.source.jobId &&
        entry.source_attempt_id === intent.source.attemptId &&
        entry.model.id === intent.source.model.id
      );
    case "promote":
      return snapshot.active_entry_id === intent.entry.id && snapshot.items.some((entry) =>
        entry.id === intent.entry.id && entry.status === "approved" && entry.is_active
      );
    case "revoke":
      return snapshot.items.some((entry) =>
        entry.id === intent.entry.id &&
        entry.status === "revoked" &&
        entry.revoke_reason === intent.reason &&
        !entry.is_active
      );
  }
}

function registrySnapshotFingerprint(snapshot: ModelRegistrySnapshot): string {
  return JSON.stringify({
    active_entry_id: snapshot.active_entry_id,
    can_manage: snapshot.can_manage,
    experiment_id: snapshot.experiment_id,
    items: snapshot.items
      .map((entry) => ({
        approved_at: entry.approved_at,
        created_at: entry.created_at,
        engine_mode: entry.engine_mode,
        experiment_id: entry.experiment_id,
        id: entry.id,
        index: entry.index ? artifactFingerprintValue(entry.index) : null,
        is_active: entry.is_active,
        job_config_sha256: entry.job_config_sha256,
        model: artifactFingerprintValue(entry.model),
        revoke_reason: entry.revoke_reason,
        revoked_at: entry.revoked_at,
        row_version: entry.row_version,
        runtime_asset_manifest_sha256: entry.runtime_asset_manifest_sha256,
        runtime_image_digest: entry.runtime_image_digest,
        rvc_commit_hash: entry.rvc_commit_hash,
        source_attempt_id: entry.source_attempt_id,
        source_attempt_number: entry.source_attempt_number,
        source_job_id: entry.source_job_id,
        source_job_name: entry.source_job_name,
        status: entry.status,
      }))
      .sort((left, right) => left.id.localeCompare(right.id)),
    registry_row_version: snapshot.registry_row_version,
    total: snapshot.total,
  });
}

function artifactFingerprintValue(artifact: ModelRegistryArtifact) {
  return {
    filename: artifact.filename,
    id: artifact.id,
    sha256: artifact.sha256,
    size_bytes: artifact.size_bytes,
  };
}

function cloneCandidateSource(source: RegistryCandidateSource): RegistryCandidateSource {
  return {
    ...source,
    index: source.index ? { ...source.index } : null,
    model: { ...source.model },
  };
}

function cloneEntry(entry: ModelRegistryEntry): ModelRegistryEntry {
  return {
    ...entry,
    index: entry.index ? { ...entry.index } : null,
    model: { ...entry.model },
  };
}

async function readIdentityJson(response: Response): Promise<unknown> {
  try {
    return await response.json();
  } catch {
    return null;
  }
}

function hasExactKeys<T extends readonly string[]>(
  value: unknown,
  keys: T,
): value is Record<T[number], unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return false;
  const actual = Object.keys(value);
  return actual.length === keys.length && actual.every((key) => keys.includes(key));
}
