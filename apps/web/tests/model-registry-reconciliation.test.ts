import { afterEach, describe, expect, it, vi } from "vitest";
import type {
  ModelRegistryEntry,
  ModelRegistryMutationResponse,
  ModelRegistrySnapshot,
} from "@/lib/api-types";
import type { RegistryCandidateSource } from "@/lib/client/model-registry";
import {
  createRegistryPromoteIntent,
  createRegistryRegisterIntent,
  createRegistryRevokeIntent,
  executeRegistryMutationIntent,
  reconcileRegistryMutation,
  RegistryActorChangedError,
  verifyRegistryIntentActor,
} from "@/lib/client/model-registry-reconciliation";

const EXPERIMENT_ID = "11111111-1111-4111-8111-111111111111";
const JOB_ID = "22222222-2222-4222-8222-222222222222";
const ATTEMPT_ID = "33333333-3333-4333-8333-333333333333";
const ENTRY_ID = "44444444-4444-4444-8444-444444444444";
const MODEL_ID = "55555555-5555-4555-8555-555555555555";
const INDEX_ID = "66666666-6666-4666-8666-666666666666";
const ACTOR_ID = "99999999-9999-4999-8999-999999999999";

afterEach(() => vi.unstubAllGlobals());

describe("model registry response-loss reconciliation", () => {
  it("replays a preserved registration with the byte-identical body and idempotency key", async () => {
    const baseline = snapshot([], 0, null);
    const source = candidateSource();
    const intent = createRegistryRegisterIntent(
      baseline,
      source,
      "preserved-register-key",
      ACTOR_ID,
    );
    const created = entry({ status: "candidate" });
    const response = mutation(created, 1, null);
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      void input;
      void init;
      return Response.json(response, { status: 201 });
    });
    vi.stubGlobal("fetch", fetchMock);

    await executeRegistryMutationIntent(intent);
    await executeRegistryMutationIntent(intent);

    expect(fetchMock).toHaveBeenCalledTimes(2);
    const first = fetchMock.mock.calls[0];
    const second = fetchMock.mock.calls[1];
    expect(second?.[0]).toBe(first?.[0]);
    expect(second?.[1]?.body).toBe(first?.[1]?.body);
    expect(second?.[1]?.body).toBe(JSON.stringify({
      expected_registry_row_version: 0,
      source_job_id: JOB_ID,
      source_attempt_id: ATTEMPT_ID,
      model_artifact_id: MODEL_ID,
    }));
    expect(new Headers(first?.[1]?.headers).get("idempotency-key")).toBe("preserved-register-key");
    expect(new Headers(second?.[1]?.headers).get("idempotency-key")).toBe("preserved-register-key");
    expect(new Headers(second?.[1]?.headers).get("x-rvc-expected-actor-id")).toBe(ACTOR_ID);
  });

  it("recognizes a registration reflected by the exact source attempt and model", () => {
    const baseline = snapshot([], 0, null);
    const intent = createRegistryRegisterIntent(baseline, candidateSource(), "register-key", ACTOR_ID);
    const registered = entry({ status: "candidate" });

    expect(reconcileRegistryMutation(intent, snapshot([registered], 1, null)).state).toBe("applied");
    expect(reconcileRegistryMutation(intent, baseline).state).toBe("unchanged");
  });

  it("recognizes exact promotion and revoke targets but not later conflicting state", () => {
    const candidate = entry({ status: "candidate" });
    const baseline = snapshot([candidate], 1, null);
    const promote = createRegistryPromoteIntent(baseline, candidate, "promote-key", ACTOR_ID);
    const active = entry({ status: "approved", active: true, rowVersion: 2 });

    expect(reconcileRegistryMutation(promote, snapshot([active], 2, active.id)).state).toBe("applied");
    expect(reconcileRegistryMutation(
      promote,
      snapshot([{ ...active, is_active: false }], 3, null),
    ).state).toBe("changed");

    const revoke = createRegistryRevokeIntent(
      snapshot([active], 2, active.id),
      active,
      "security_issue",
      "revoke-key",
      ACTOR_ID,
    );
    const revoked = entry({
      status: "revoked",
      approvedAt: active.approved_at,
      reason: "security_issue",
      rowVersion: 3,
    });
    expect(reconcileRegistryMutation(revoke, snapshot([revoked], 3, null)).state).toBe("applied");
    expect(reconcileRegistryMutation(
      revoke,
      snapshot([{ ...revoked, revoke_reason: "operator_request" }], 3, null),
    ).state).toBe("changed");
  });

  it("requires the complete canonical snapshot to be unchanged and ignores only item order", () => {
    const first = entry({ status: "candidate" });
    const second = entry({
      id: "77777777-7777-4777-8777-777777777777",
      modelId: "88888888-8888-4888-8888-888888888888",
      status: "candidate",
    });
    const baseline = snapshot([first, second], 2, null);
    const intent = createRegistryPromoteIntent(baseline, first, "promote-key", ACTOR_ID);

    expect(reconcileRegistryMutation(intent, snapshot([second, first], 2, null)).state).toBe("unchanged");
    expect(reconcileRegistryMutation(intent, snapshot([first, second], 3, null)).state).toBe("changed");
    expect(reconcileRegistryMutation(
      intent,
      snapshot([{ ...first, row_version: 2 }, second], 2, null),
    ).state).toBe("changed");
  });

  it("binds a preserved intent to the current authenticated actor", async () => {
    const intent = createRegistryRegisterIntent(
      snapshot([], 0, null),
      candidateSource(),
      "actor-bound-key",
      ACTOR_ID,
    );
    const fetchMock = vi.fn(async () => Response.json({ actor_id: ACTOR_ID }));
    vi.stubGlobal("fetch", fetchMock);

    await expect(verifyRegistryIntentActor(intent)).resolves.toBeUndefined();
    expect(fetchMock).toHaveBeenCalledWith("/bff/session/identity", expect.objectContaining({
      cache: "no-store",
      credentials: "same-origin",
    }));

    vi.stubGlobal("fetch", vi.fn(async () => Response.json({
      actor_id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    })));
    await expect(verifyRegistryIntentActor(intent)).rejects.toBeInstanceOf(
      RegistryActorChangedError,
    );
  });
});

function candidateSource(): RegistryCandidateSource {
  return {
    experimentId: EXPERIMENT_ID,
    jobId: JOB_ID,
    jobName: "native-run",
    attemptId: ATTEMPT_ID,
    attemptNumber: 2,
    model: {
      id: MODEL_ID,
      filename: "final-model.pth",
      size_bytes: 1024,
      sha256: "a".repeat(64),
    },
    index: {
      id: INDEX_ID,
      filename: "final.index",
      size_bytes: 2048,
      sha256: "b".repeat(64),
    },
  };
}

function snapshot(
  items: ModelRegistryEntry[],
  registryRowVersion: number,
  activeEntryId: string | null,
): ModelRegistrySnapshot {
  return {
    experiment_id: EXPERIMENT_ID,
    registry_row_version: registryRowVersion,
    active_entry_id: activeEntryId,
    can_manage: true,
    items,
    total: items.length,
  };
}

function mutation(
  value: ModelRegistryEntry,
  registryRowVersion: number,
  activeEntryId: string | null,
): ModelRegistryMutationResponse {
  return {
    experiment_id: EXPERIMENT_ID,
    registry_row_version: registryRowVersion,
    active_entry_id: activeEntryId,
    entry: value,
  };
}

function entry(options: {
  status: "candidate" | "approved" | "revoked";
  active?: boolean;
  approvedAt?: string | null;
  id?: string;
  modelId?: string;
  reason?: "quality_rejected" | "security_issue" | "operator_request";
  rowVersion?: number;
}): ModelRegistryEntry {
  const approvedAt = options.approvedAt === undefined
    ? options.status === "candidate" ? null : "2026-07-12T01:00:00Z"
    : options.approvedAt;
  return {
    id: options.id ?? ENTRY_ID,
    row_version: options.rowVersion ?? 1,
    status: options.status,
    is_active: options.active ?? false,
    experiment_id: EXPERIMENT_ID,
    source_job_id: JOB_ID,
    source_job_name: "native-run",
    source_attempt_id: ATTEMPT_ID,
    source_attempt_number: 2,
    engine_mode: "rvc_webui",
    model: {
      id: options.modelId ?? MODEL_ID,
      filename: "final-model.pth",
      size_bytes: 1024,
      sha256: "a".repeat(64),
    },
    index: {
      id: INDEX_ID,
      filename: "final.index",
      size_bytes: 2048,
      sha256: "b".repeat(64),
    },
    job_config_sha256: "c".repeat(64),
    rvc_commit_hash: "d".repeat(40),
    runtime_image_digest: `sha256:${"e".repeat(64)}`,
    runtime_asset_manifest_sha256: "f".repeat(64),
    created_at: "2026-07-12T00:00:00Z",
    approved_at: approvedAt,
    revoked_at: options.status === "revoked" ? "2026-07-12T02:00:00Z" : null,
    revoke_reason: options.status === "revoked" ? options.reason ?? "operator_request" : null,
  };
}
