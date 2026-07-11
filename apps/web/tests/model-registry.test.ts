import { afterEach, describe, expect, it, vi } from "vitest";
import type {
  ExperimentComparisonJob,
  ModelRegistryEntry,
  ModelRegistryMutationResponse,
  ModelRegistrySnapshot,
} from "@/lib/api-types";
import {
  applyRegistryMutation,
  fetchModelRegistrySnapshot,
  ModelRegistryReadError,
  promoteModelRegistryEntry,
  registerModelCandidate,
  registryCandidateSource,
  registryMutationIsUncertain,
  revokeModelRegistryEntry,
} from "@/lib/client/model-registry";

const EXPERIMENT_ID = "11111111-1111-4111-8111-111111111111";
const JOB_ID = "22222222-2222-4222-8222-222222222222";
const ATTEMPT_ID = "33333333-3333-4333-8333-333333333333";
const ENTRY_ID = "44444444-4444-4444-8444-444444444444";
const MODEL_ID = "55555555-5555-4555-8555-555555555555";
const INDEX_ID = "66666666-6666-4666-8666-666666666666";

afterEach(() => vi.unstubAllGlobals());

describe("model registry client boundary", () => {
  it("accepts the empty registry version zero without inventing a champion", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => Response.json(page([], 0, null, 0))));

    await expect(fetchModelRegistrySnapshot(EXPERIMENT_ID)).resolves.toEqual({
      experiment_id: EXPERIMENT_ID,
      registry_row_version: 0,
      active_entry_id: null,
      can_manage: true,
      items: [],
      total: 0,
    });
  });

  it("loads every bounded page and rejects a version change while paging", async () => {
    const first = entryFixture({ id: ENTRY_ID, status: "candidate" });
    const second = entryFixture({
      id: "77777777-7777-4777-8777-777777777777",
      modelId: "88888888-8888-4888-8888-888888888888",
      status: "candidate",
    });
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(Response.json(page([first], 7, null, 2, 0)))
      .mockResolvedValueOnce(Response.json(page([second], 7, null, 2, 1)));
    vi.stubGlobal("fetch", fetchMock);

    const result = await fetchModelRegistrySnapshot(EXPERIMENT_ID);
    expect(result.items.map((entry) => entry.id)).toEqual([first.id, second.id]);
    expect(fetchMock.mock.calls[1]?.[0]).toContain("offset=1&limit=200");

    vi.stubGlobal("fetch", vi.fn()
      .mockResolvedValueOnce(Response.json(page([first], 7, null, 2, 0)))
      .mockResolvedValueOnce(Response.json(page([second], 8, null, 2, 1))));
    await expect(fetchModelRegistrySnapshot(EXPERIMENT_ID)).rejects.toEqual(
      expect.objectContaining<Partial<ModelRegistryReadError>>({
        status: 409,
        code: "registry_changed_during_read",
      }),
    );
  });

  it("accepts direct candidate revocation with no approved timestamp", async () => {
    const revoked = entryFixture({ status: "revoked", approvedAt: null });
    vi.stubGlobal("fetch", vi.fn(async () => Response.json(page([revoked], 4, null, 1))));

    const result = await fetchModelRegistrySnapshot(EXPERIMENT_ID);
    expect(result.items[0]).toMatchObject({
      status: "revoked",
      approved_at: null,
      revoke_reason: "operator_request",
    });
  });

  it("derives a registration selector only from an exact completed native current attempt", () => {
    const native = comparisonJob("rvc_webui");
    expect(registryCandidateSource(EXPERIMENT_ID, native)).toEqual({
      experimentId: EXPERIMENT_ID,
      jobId: JOB_ID,
      jobName: "native-run",
      attemptId: ATTEMPT_ID,
      attemptNumber: 2,
      model: native.availability.final_model,
      index: native.availability.final_index,
    });
    expect(registryCandidateSource(EXPERIMENT_ID, comparisonJob("fake"))).toBeNull();
    expect(registryCandidateSource(EXPERIMENT_ID, { ...native, status: "failed" })).toBeNull();
  });

  it("sends only selector/CAS fields to fixed same-origin mutation routes", async () => {
    const candidate = registryCandidateSource(EXPERIMENT_ID, comparisonJob("rvc_webui"));
    if (!candidate) throw new Error("fixture must be eligible");
    const candidateResponse = mutation(entryFixture({ status: "candidate" }), 1, null);
    const approvedEntry = entryFixture({ status: "approved", active: true, rowVersion: 2 });
    const approvedResponse = mutation(approvedEntry, 2, approvedEntry.id);
    const revokedEntry = entryFixture({ status: "revoked", approvedAt: null, rowVersion: 2 });
    const revokedResponse = mutation(revokedEntry, 2, null);
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(Response.json(candidateResponse, { status: 201 }))
      .mockResolvedValueOnce(Response.json(approvedResponse))
      .mockResolvedValueOnce(Response.json(revokedResponse));
    vi.stubGlobal("fetch", fetchMock);

    await registerModelCandidate(candidate, 0, "register-key-1");
    await promoteModelRegistryEntry(EXPERIMENT_ID, candidateResponse.entry, 1, "promote-key-1");
    await revokeModelRegistryEntry(
      EXPERIMENT_ID,
      candidateResponse.entry,
      1,
      "operator_request",
      "revoke-key-1",
    );

    expect(fetchMock.mock.calls[0]?.[0]).toBe(
      `/bff/experiments/${EXPERIMENT_ID}/model-registry/candidates`,
    );
    expect(JSON.parse(String(fetchMock.mock.calls[0]?.[1]?.body))).toEqual({
      expected_registry_row_version: 0,
      source_job_id: JOB_ID,
      source_attempt_id: ATTEMPT_ID,
      model_artifact_id: MODEL_ID,
    });
    expect(fetchMock.mock.calls[0]?.[1]).toMatchObject({
      cache: "no-store",
      credentials: "same-origin",
      method: "POST",
    });
    expect(new Headers(fetchMock.mock.calls[0]?.[1]?.headers).get("authorization")).toBeNull();
    expect(new Headers(fetchMock.mock.calls[0]?.[1]?.headers).get("idempotency-key")).toBe("register-key-1");
    expect(JSON.parse(String(fetchMock.mock.calls[2]?.[1]?.body))).toEqual({
      expected_registry_row_version: 1,
      expected_entry_row_version: 1,
      reason_code: "operator_request",
    });
  });

  it("fails closed on extra BFF fields and classifies only transport/502/503 as uncertain", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({
      ...page([], 0, null, 0),
      storage_uri: "s3://private",
    })));
    await expect(fetchModelRegistrySnapshot(EXPERIMENT_ID)).rejects.toEqual(
      expect.objectContaining<Partial<ModelRegistryReadError>>({ status: 502 }),
    );
    expect(registryMutationIsUncertain(new Error("network"))).toBe(true);
  });

  it("updates the active pointer without revoking the previous approved entry", () => {
    const previous = entryFixture({ status: "approved", active: true, id: ENTRY_ID });
    const next = entryFixture({
      id: "77777777-7777-4777-8777-777777777777",
      modelId: "88888888-8888-4888-8888-888888888888",
      status: "approved",
      active: true,
    });
    const snapshot: ModelRegistrySnapshot = {
      experiment_id: EXPERIMENT_ID,
      registry_row_version: 3,
      active_entry_id: previous.id,
      can_manage: true,
      items: [previous, { ...next, is_active: false }],
      total: 2,
    };

    const updated = applyRegistryMutation(snapshot, mutation(next, 4, next.id));
    expect(updated.active_entry_id).toBe(next.id);
    expect(updated.items.find((entry) => entry.id === previous.id)).toMatchObject({
      status: "approved",
      is_active: false,
    });
  });
});

function page(
  items: ModelRegistryEntry[],
  registryRowVersion: number,
  activeEntryId: string | null,
  total: number,
  offset = 0,
) {
  return {
    experiment_id: EXPERIMENT_ID,
    registry_row_version: registryRowVersion,
    active_entry_id: activeEntryId,
    can_manage: true,
    items,
    total,
    offset,
    limit: 200,
  };
}

function mutation(
  entry: ModelRegistryEntry,
  registryRowVersion: number,
  activeEntryId: string | null,
): ModelRegistryMutationResponse {
  return {
    experiment_id: EXPERIMENT_ID,
    registry_row_version: registryRowVersion,
    active_entry_id: activeEntryId,
    entry: { ...entry, is_active: entry.id === activeEntryId },
  };
}

function entryFixture(options: {
  id?: string;
  modelId?: string;
  status: "candidate" | "approved" | "revoked";
  active?: boolean;
  rowVersion?: number;
  approvedAt?: string | null;
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
    revoke_reason: options.status === "revoked" ? "operator_request" : null,
  };
}

function comparisonJob(engine: "fake" | "rvc_webui"): ExperimentComparisonJob {
  return {
    id: JOB_ID,
    job_name: "native-run",
    status: "completed",
    config: {
      schema_version: "1.0",
      job_name: "native-run",
      experiment_id: EXPERIMENT_ID,
      dataset_id: "99999999-9999-4999-8999-999999999999",
      rvc_backend: {
        backend_type: "rvc_webui",
        repository: "RVC-Project/Retrieval-based-Voice-Conversion-WebUI",
        rvc_version: "v2",
        rvc_commit_hash: "d".repeat(40),
      },
      model: { version: "v2", sample_rate: "40k", use_f0: true, speaker_id: 0 },
      pretrained: { mode: "auto", g_path: null, d_path: null, allow_custom_override: false },
      training_feature: {
        feature_dir_policy: "auto",
        v1_feature_dir: "3_feature256",
        v2_feature_dir: "3_feature768",
      },
      training: {
        epochs: 80,
        batch_size_per_gpu: 8,
        save_every_epoch: 5,
        save_only_latest: false,
        save_every_weights: true,
        cache_dataset_in_gpu: false,
        gpu_ids: [0],
      },
      f0_extraction: { training_f0_method: "rmvpe", rmvpe_gpu_ids: null },
      index: { build_index: true, collect_total_fea: true, collect_added_index: true },
      auto_inference_samples: {
        enabled: false,
        test_set_id: null,
        inference_f0_method: "rmvpe",
        transpose: 0,
        index_rate: 0,
        filter_radius: 3,
        resample_sr: 0,
        rms_mix_rate: 0.25,
        protect: 0.33,
      },
      artifacts: {
        collect_checkpoints: true,
        collect_small_model: true,
        extract_small_model_if_missing: true,
        collect_index: true,
        collect_tensorboard: true,
        collect_logs: true,
        collect_samples: false,
      },
      resource: { min_vram_gb: 12, preferred_worker_tags: [], priority: 5 },
    },
    current_epoch: 80,
    total_epoch: 80,
    current_attempt: {
      id: ATTEMPT_ID,
      attempt_number: 2,
      engine_mode: engine,
      status: "completed",
      started_at: "2026-07-12T00:00:00Z",
      finished_at: "2026-07-12T01:00:00Z",
    },
    metrics: [],
    availability: {
      final_model: {
        id: MODEL_ID,
        filename: "final-model.pth",
        size_bytes: 1024,
        sha256: "a".repeat(64),
      },
      final_index: {
        id: INDEX_ID,
        filename: "final.index",
        size_bytes: 2048,
        sha256: "b".repeat(64),
      },
      samples: [],
    },
  };
}
