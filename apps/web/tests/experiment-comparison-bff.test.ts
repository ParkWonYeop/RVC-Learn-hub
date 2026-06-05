import { NextRequest } from "next/server";
import { beforeEach, describe, expect, it, vi } from "vitest";

const manager = vi.hoisted(() => ({ managerRawRequest: vi.fn() }));
vi.mock("@/lib/server/manager-api", () => manager);

import { GET as getExperimentComparison } from "@/app/bff/experiments/[experimentId]/comparison/route";

const TOKEN = "private.jwt.only.in.httponly.cookie";
const EXPERIMENT_ID = "11111111-1111-4111-8111-111111111111";
const DATASET_ID = "22222222-2222-4222-8222-222222222222";
const JOB_ONE_ID = "33333333-3333-4333-8333-333333333333";
const JOB_TWO_ID = "44444444-4444-4444-8444-444444444444";
const ATTEMPT_ID = "55555555-5555-4555-8555-555555555555";
const MODEL_ID = "66666666-6666-4666-8666-666666666666";
const SAMPLE_ID = "77777777-7777-4777-8777-777777777777";
const TEST_SET_ITEM_ID = "88888888-8888-4888-8888-888888888888";
const TEST_SET_ID = "99999999-9999-4999-8999-999999999999";

describe("Experiment comparison BFF", () => {
  beforeEach(() => manager.managerRawRequest.mockReset());

  it("forwards only ordered canonical UUIDs with the cookie token and allowlists the response", async () => {
    const orderedIds = [JOB_TWO_ID, JOB_ONE_ID];
    manager.managerRawRequest.mockResolvedValue(rawResponse(Response.json(
      comparisonFixture(orderedIds, true),
    )));
    const url = comparisonUrl(orderedIds);
    const response = await getExperimentComparison(
      readRequest(url, { browserAuthorization: "Bearer browser-injected" }),
      experimentContext(),
    );
    const body = await response.json();

    expect(response.status).toBe(200);
    expect(response.headers.get("cache-control")).toContain("no-store");
    expect(response.headers.get("vary")).toBe("Cookie");
    expect(body.jobs.map((job: { id: string }) => job.id)).toEqual(orderedIds);
    expect(body.metric_point_limit_per_key).toBe(200);
    expect(body.jobs[1].metrics[0].points[0]).toEqual({
      sequence: 7,
      epoch: 80,
      step: 900,
      value: 1.25,
      occurred_at: "2026-07-12T12:10:00Z",
    });
    expect(body.jobs[1].availability.final_model).toEqual({
      id: MODEL_ID,
      filename: "final-model.pth",
      size_bytes: 1024,
      sha256: "a".repeat(64),
    });
    expect(body.jobs[1].availability.samples[0].id).toBe(SAMPLE_ID);
    const serialized = JSON.stringify(body);
    for (const secret of [
      TOKEN,
      "browser-injected",
      "storage_uri",
      "canonical_object_key",
      "metadata_json",
      "s3://private",
    ]) {
      expect(serialized).not.toContain(secret);
    }
    expect(manager.managerRawRequest).toHaveBeenCalledWith(
      `/api/v1/experiments/${EXPERIMENT_ID}/comparison?job_ids=${JOB_TWO_ID}&job_ids=${JOB_ONE_ID}`,
      expect.objectContaining({ method: "GET", token: TOKEN }),
    );
    expect(manager.managerRawRequest.mock.calls[0]?.[1]).not.toHaveProperty("headers");
  });

  it("rejects non-canonical, missing, duplicate, oversized and injected query selections", async () => {
    const seventeen = Array.from(
      { length: 17 },
      (_, index) => `00000000-0000-4000-8000-${String(index).padStart(12, "0")}`,
    );
    const cases: Array<[string, string]> = [
      [EXPERIMENT_ID, ""],
      [EXPERIMENT_ID, `?job_ids=${JOB_ONE_ID}`],
      [EXPERIMENT_ID, `?job_ids=${JOB_ONE_ID}&job_ids=${JOB_ONE_ID}`],
      [EXPERIMENT_ID, `?job_ids=${JOB_ONE_ID}&job_ids=${JOB_TWO_ID}&path=%2Fapi%2Fv1%2Fusers`],
      [EXPERIMENT_ID, `?${seventeen.map((id) => `job_ids=${id}`).join("&")}`],
      [EXPERIMENT_ID, `?job_ids=AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA&job_ids=${JOB_TWO_ID}`],
      [EXPERIMENT_ID, `?job_ids=not-a-uuid&job_ids=${JOB_TWO_ID}`],
      ["experiment-1", `?job_ids=${JOB_ONE_ID}&job_ids=${JOB_TWO_ID}`],
      [EXPERIMENT_ID, `?job_ids=${JOB_ONE_ID}&job_ids=${JOB_TWO_ID}&junk=${"x".repeat(1_100)}`],
    ];

    const responses = await Promise.all(cases.map(([experimentId, query]) =>
      getExperimentComparison(
        readRequest(`https://manager.test/bff/experiments/${experimentId}/comparison${query}`),
        { params: Promise.resolve({ experimentId }) },
      ),
    ));

    expect(responses.map((response) => response.status)).toEqual(cases.map(() => 400));
    expect(manager.managerRawRequest).not.toHaveBeenCalled();
  });

  it("enforces same-origin reads and requires the HttpOnly session cookie", async () => {
    const wrongOrigin = await getExperimentComparison(
      readRequest(comparisonUrl([JOB_ONE_ID, JOB_TWO_ID]), { forwardedHost: "other.test" }),
      experimentContext(),
    );
    const noCookie = await getExperimentComparison(
      readRequest(comparisonUrl([JOB_ONE_ID, JOB_TWO_ID]), { cookie: false }),
      experimentContext(),
    );

    expect(wrongOrigin.status).toBe(403);
    expect(noCookie.status).toBe(401);
    expect(await noCookie.json()).toEqual({ error: "session_required" });
    expect(manager.managerRawRequest).not.toHaveBeenCalled();
  });

  it("fails closed on reordered, private-key, inconsistent metric and non-finite upstream ledgers", async () => {
    const reordered = comparisonFixture([JOB_TWO_ID, JOB_ONE_ID]);
    const privateMetric = comparisonFixture([JOB_ONE_ID, JOB_TWO_ID]);
    privateMetric.jobs[0]!.metrics[0]!.key = "private.unsupported.metric";
    const inconsistentTruncation = comparisonFixture([JOB_ONE_ID, JOB_TWO_ID]);
    inconsistentTruncation.jobs[0]!.metrics[0]!.total_points = 2;
    inconsistentTruncation.jobs[0]!.metrics[0]!.truncated = false;
    const invalidNumber = comparisonFixture([JOB_ONE_ID, JOB_TWO_ID]);
    invalidNumber.jobs[0]!.metrics[0]!.points[0]!.value = null as unknown as number;
    manager.managerRawRequest
      .mockResolvedValueOnce(rawResponse(Response.json(reordered)))
      .mockResolvedValueOnce(rawResponse(Response.json(privateMetric)))
      .mockResolvedValueOnce(rawResponse(Response.json(inconsistentTruncation)))
      .mockResolvedValueOnce(rawResponse(Response.json(invalidNumber)));

    const responses = [];
    for (let index = 0; index < 4; index += 1) {
      responses.push(await getExperimentComparison(
        readRequest(comparisonUrl([JOB_ONE_ID, JOB_TWO_ID])),
        experimentContext(),
      ));
    }

    expect(responses.map((response) => response.status)).toEqual([502, 502, 502, 502]);
    for (const response of responses) {
      expect(await response.json()).toEqual({ error: "invalid_upstream_response" });
    }
  });

  it.each([
    [404, "not_found", null],
    [409, "conflict", null],
    [422, "invalid_job", null],
    [429, "rate_limited", "17"],
    [503, "manager_unavailable", "5"],
  ] as const)("preserves safe upstream status %s while redacting details", async (status, code, retryAfter) => {
    manager.managerRawRequest.mockResolvedValue(rawResponse(Response.json(
      { detail: "private ledger path s3://private", token: TOKEN },
      { status, headers: retryAfter ? { "Retry-After": retryAfter } : undefined },
    )));

    const response = await getExperimentComparison(
      readRequest(comparisonUrl([JOB_ONE_ID, JOB_TWO_ID])),
      experimentContext(),
    );

    expect(response.status).toBe(status);
    expect(await response.json()).toEqual({ error: code });
    expect(response.headers.get("retry-after")).toBe(retryAfter);
  });
});

function comparisonUrl(jobIds: readonly string[]): string {
  return `https://manager.test/bff/experiments/${EXPERIMENT_ID}/comparison?${jobIds
    .map((jobId) => `job_ids=${jobId}`)
    .join("&")}`;
}

function experimentContext() {
  return { params: Promise.resolve({ experimentId: EXPERIMENT_ID }) };
}

function comparisonFixture(jobIds: readonly string[], includePrivate = false) {
  return {
    experiment: {
      id: EXPERIMENT_ID,
      row_version: 3,
      name: "speaker-a",
      dataset_id: DATASET_ID,
      description: "comparison",
      created_at: "2026-07-12T12:00:00Z",
      updated_at: "2026-07-12T12:00:00Z",
      ...(includePrivate ? { created_by: "private-owner", storage_uri: "s3://private" } : {}),
    },
    jobs: jobIds.map((jobId) => comparisonJob(jobId, includePrivate)),
    metric_point_limit_per_key: 200 as const,
    ...(includePrivate ? { internal_storage_uri: "s3://private" } : {}),
  };
}

function comparisonJob(jobId: string, includePrivate: boolean) {
  const completed = jobId === JOB_ONE_ID;
  const jobName = completed ? "speaker-a-v2" : "speaker-a-v1";
  return {
    id: jobId,
    job_name: jobName,
    status: completed ? "completed" : "queued",
    config: comparisonConfig(jobName),
    current_epoch: completed ? 80 : null,
    total_epoch: 80,
    current_attempt: completed
      ? {
          id: ATTEMPT_ID,
          attempt_number: 1,
          engine_mode: "rvc_webui",
          status: "completed",
          started_at: "2026-07-12T12:00:00Z",
          finished_at: "2026-07-12T13:00:00Z",
          ...(includePrivate ? { worker_id: "private-worker" } : {}),
        }
      : null,
    metrics: completed
      ? [{
          key: "loss_g_total",
          total_points: 1,
          truncated: false,
          points: [{
            sequence: 7,
            epoch: 80,
            step: 900,
            value: 1.25,
            occurred_at: "2026-07-12T12:10:00Z",
            ...(includePrivate ? { storage_uri: "s3://private" } : {}),
          }],
          ...(includePrivate ? { raw_query: "private" } : {}),
        }]
      : [],
    availability: completed
      ? {
          final_model: {
            id: MODEL_ID,
            filename: "final-model.pth",
            size_bytes: 1024,
            sha256: "a".repeat(64),
            ...(includePrivate ? { storage_uri: "s3://private/model" } : {}),
          },
          final_index: null,
          samples: [{
            id: SAMPLE_ID,
            test_set_item_id: TEST_SET_ITEM_ID,
            output_size_bytes: 32044,
            output_sha256: "b".repeat(64),
            output_sample_rate_hz: 40_000,
            output_channels: 1,
            output_duration_seconds: 0.4,
            created_at: "2026-07-12T12:59:00Z",
            ...(includePrivate ? { canonical_object_key: "private/sample.wav" } : {}),
          }],
          ...(includePrivate ? { metadata_json: { secret: true } } : {}),
        }
      : { final_model: null, final_index: null, samples: [] },
    ...(includePrivate ? { storage_uri: "s3://private/job" } : {}),
  };
}

function comparisonConfig(jobName: string) {
  return {
    schema_version: "1.0",
    job_name: jobName,
    experiment_id: EXPERIMENT_ID,
    dataset_id: DATASET_ID,
    rvc_backend: {
      backend_type: "rvc_webui",
      repository: "RVC-Project/Retrieval-based-Voice-Conversion-WebUI",
      rvc_version: "v2",
      rvc_commit_hash: null,
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
      enabled: true,
      test_set_id: TEST_SET_ID,
      inference_f0_method: "rmvpe",
      transpose: 0,
      index_rate: 0.75,
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
      collect_samples: true,
    },
    resource: { min_vram_gb: 12, preferred_worker_tags: ["24gb"], priority: 5 },
  };
}

function readRequest(
  url: string,
  options: {
    browserAuthorization?: string;
    cookie?: boolean;
    forwardedHost?: string;
  } = {},
): NextRequest {
  return new NextRequest(url, {
    headers: {
      ...(options.cookie === false ? {} : { cookie: `rvc_manager_session=${TOKEN}` }),
      origin: new URL(url).origin,
      "sec-fetch-site": "same-origin",
      ...(options.browserAuthorization ? { authorization: options.browserAuthorization } : {}),
      ...(options.forwardedHost ? { "x-forwarded-host": options.forwardedHost } : {}),
    },
  });
}

function rawResponse(response: Response) {
  return { response, requestId: "request-1" };
}
