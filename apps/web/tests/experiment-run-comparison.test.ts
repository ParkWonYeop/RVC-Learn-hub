import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  ExperimentComparisonResult,
  ExperimentRunComparison,
} from "@/app/(dashboard)/experiments/[experimentId]/experiment-run-comparison";
import type {
  ApiJobConfig,
  ExperimentComparisonJob,
  ExperimentComparisonResponse,
} from "@/lib/api-types";
import {
  comparisonErrorMessage,
  defaultComparisonJobIds,
  ExperimentComparisonReadError,
  fetchExperimentComparison,
  formatAttemptDuration,
  metricPolyline,
} from "@/lib/client/experiment-comparison";

const experimentId = "00000000-0000-4000-8000-000000000001";
const jobIds = [
  "00000000-0000-4000-8000-000000000002",
  "00000000-0000-4000-8000-000000000003",
] as const;

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("Experiment run comparison client", () => {
  it("selects at most the first four distinct Jobs and requires at least two", () => {
    const jobs = [
      selectable(jobIds[0], "run-a"),
      selectable(jobIds[0], "duplicate"),
      selectable(jobIds[1], "run-b"),
      selectable("00000000-0000-4000-8000-000000000004", "run-c"),
      selectable("00000000-0000-4000-8000-000000000005", "run-d"),
      selectable("00000000-0000-4000-8000-000000000006", "run-e"),
    ];
    expect(defaultComparisonJobIds(jobs)).toEqual([
      jobIds[0],
      jobIds[1],
      "00000000-0000-4000-8000-000000000004",
      "00000000-0000-4000-8000-000000000005",
    ]);
    expect(defaultComparisonJobIds(jobs.slice(0, 1))).toEqual([]);
  });

  it("calls only the same-origin repeated-query BFF and preserves selected order", async () => {
    const payload = responseFixture();
    const fetchMock = vi.fn<typeof fetch>(async () =>
      new Response(JSON.stringify(payload), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await expect(fetchExperimentComparison(experimentId, [...jobIds])).resolves.toEqual(payload);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0] ?? [];
    expect(url).toBe(
      `/bff/experiments/${experimentId}/comparison?job_ids=${jobIds[0]}&job_ids=${jobIds[1]}`,
    );
    expect(init).toMatchObject({
      cache: "no-store",
      credentials: "same-origin",
      headers: { Accept: "application/json" },
    });
  });

  it("rejects invalid local selections without sending a request", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    await expect(fetchExperimentComparison(experimentId, [jobIds[0]])).rejects.toEqual(
      expect.objectContaining<Partial<ExperimentComparisonReadError>>({ status: 422 }),
    );
    await expect(fetchExperimentComparison(experimentId, [jobIds[0], jobIds[0]])).rejects.toEqual(
      expect.objectContaining<Partial<ExperimentComparisonReadError>>({ status: 422 }),
    );
    const tooMany = Array.from(
      { length: 17 },
      (_, index) => `00000000-0000-4000-8000-${String(index + 10).padStart(12, "0")}`,
    );
    await expect(fetchExperimentComparison(experimentId, tooMany)).rejects.toEqual(
      expect.objectContaining<Partial<ExperimentComparisonReadError>>({ status: 422 }),
    );
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("accepts sequence-ordered points even when wall clocks move backwards", async () => {
    const payload = responseFixture();
    payload.jobs[0].metrics[0].points = [
      {
        sequence: 10,
        epoch: 1,
        step: 10,
        value: 2,
        occurred_at: "2026-07-12T00:00:02Z",
      },
      {
        sequence: 11,
        epoch: 1,
        step: 11,
        value: 1,
        occurred_at: "2026-07-12T00:00:01Z",
      },
    ];
    payload.jobs[0].metrics[0].total_points = 2;
    payload.jobs[0].metrics[0].truncated = false;
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response(JSON.stringify(payload), { status: 200 })),
    );

    await expect(fetchExperimentComparison(experimentId, [...jobIds])).resolves.toEqual(payload);
  });

  it("fails closed on an out-of-order or malformed public metric ledger", async () => {
    const payload = responseFixture();
    payload.jobs[0].metrics[0].points.reverse();
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response(JSON.stringify(payload), { status: 200 })),
    );

    await expect(fetchExperimentComparison(experimentId, [...jobIds])).rejects.toEqual(
      expect.objectContaining<Partial<ExperimentComparisonReadError>>({ status: 502 }),
    );
  });

  it("maps required failure classes to explicit operator-facing messages", () => {
    expect(comparisonErrorMessage(401)).toContain("세션");
    expect(comparisonErrorMessage(403)).toContain("권한");
    expect(comparisonErrorMessage(409)).toContain("current attempt");
    expect(comparisonErrorMessage(422)).toContain("2개 이상 16개 이하");
    expect(comparisonErrorMessage(503)).toContain("Artifact 저장소");
  });
});

describe("Experiment run comparison presentation", () => {
  it("uses a shared global sequence domain for overlay coordinates", () => {
    expect(
      metricPolyline(
        [
          { sequence: 50, value: 0 },
          { sequence: 100, value: 1 },
        ],
        0,
        1,
        0,
        100,
      ),
    ).toBe("380.00,202.00 742.00,18.00");
    expect(metricPolyline([{ sequence: 7, value: 2 }], 2, 2, 7, 7)).toBe("380.00,110.00");
  });

  it("formats only finished attempt duration without inventing active elapsed time", () => {
    expect(formatAttemptDuration("2026-07-12T00:00:00Z", "2026-07-12T01:02:03Z")).toBe(
      "1시간 2분 3초",
    );
    expect(formatAttemptDuration("2026-07-12T00:00:00Z", null)).toBe("진행 중");
  });

  it("renders configuration, authoritative engines, truncation and verified downloads", () => {
    const html = renderToStaticMarkup(
      createElement(ExperimentComparisonResult, { data: responseFixture() }),
    );

    expect(html).toContain("Immutable 학습 설정");
    expect(html).toContain("Training F0 method");
    expect(html).toContain("FAKE · 운영 결과 아님");
    expect(html).toContain("best model을 자동 선정하지 않습니다");
    expect(html).toContain("이전 Metric 생략됨");
    expect(html).toContain("sequence 오름차순");
    expect(html).toContain(`/bff/artifacts/${jobIds[0]}/download`);
    expect(html).toContain("Model 다운로드");
    expect(html).toContain("검증된 파일 없음");
    expect(html).toContain("Sample 음성 A/B 재생");
    expect(html).toContain('role="img"');
    expect(html).toContain("Metric 원장 표 열기");
  });

  it("offers registration only for a completed native model and explicitly blocks Fake output", () => {
    const data = responseFixture();
    data.jobs[1].availability.final_model = {
      id: "00000000-0000-4000-8000-000000000010",
      filename: "native-final.pth",
      size_bytes: 4096,
      sha256: "c".repeat(64),
    };
    const html = renderToStaticMarkup(
      createElement(ExperimentComparisonResult, {
        data,
        onRegisterCandidate: () => undefined,
      }),
    );

    expect(html).toContain("후보 등록 검증");
    expect(html).toContain("FAKE 결과는 Registry 후보로 등록하거나 승인할 수 없습니다");
    expect((html.match(/후보 등록 검증/g) ?? []).length).toBe(1);
  });

  it("starts with the first two Jobs selected and exposes accessible loading status", () => {
    const html = renderToStaticMarkup(
      createElement(ExperimentRunComparison, {
        experimentId,
        jobs: [selectable(jobIds[0], "run-a"), selectable(jobIds[1], "run-b")],
      }),
    );

    expect(html).toContain("비교할 Job 선택");
    expect(html).toContain("2개 선택 · 최소 2개 / 최대 16개");
    expect(html).toContain("current-attempt 원장을 검증하는 중");
    expect(html).toContain('aria-live="polite"');
  });
});

function selectable(id: string, name: string) {
  return { id, name, status: "completed" as const };
}

function responseFixture(): ExperimentComparisonResponse {
  return {
    experiment: {
      id: experimentId,
      row_version: 1,
      name: "voice-comparison",
      dataset_id: "00000000-0000-4000-8000-000000000099",
      description: null,
      created_at: "2026-07-12T00:00:00Z",
      updated_at: "2026-07-12T00:00:00Z",
    },
    jobs: [
      jobFixture(jobIds[0], "run-a", "fake", true),
      jobFixture(jobIds[1], "run-b", "rvc_webui", false),
    ],
    metric_point_limit_per_key: 200,
  };
}

function jobFixture(
  id: string,
  name: string,
  engine: "fake" | "rvc_webui",
  withArtifact: boolean,
): ExperimentComparisonJob {
  return {
    id,
    job_name: name,
    status: "completed",
    config: configFixture(name),
    current_epoch: 80,
    total_epoch: 80,
    current_attempt: {
      id: `00000000-0000-4000-8000-${id.slice(-12)}`,
      attempt_number: 2,
      engine_mode: engine,
      status: "completed",
      started_at: "2026-07-12T00:00:00Z",
      finished_at: "2026-07-12T01:02:03Z",
    },
    metrics: [
      {
        key: "loss_g_total",
        total_points: 203,
        truncated: true,
        points: [
          {
            sequence: 3,
            epoch: 79,
            step: 300,
            value: 2,
            occurred_at: "2026-07-12T00:30:00Z",
          },
          {
            sequence: 4,
            epoch: 80,
            step: 320,
            value: 1,
            occurred_at: "2026-07-12T00:40:00Z",
          },
        ],
      },
    ],
    availability: {
      final_model: withArtifact
        ? {
            id,
            filename: `${name}.pth`,
            size_bytes: 1_024,
            sha256: "a".repeat(64),
          }
        : null,
      final_index: null,
      samples: withArtifact
        ? [
            {
              id: `00000000-0000-4000-8000-${id.slice(-12)}`,
              test_set_item_id: "fixed-voice-1",
              output_size_bytes: 32_044,
              output_sha256: "b".repeat(64),
              output_sample_rate_hz: 40_000,
              output_channels: 1,
              output_duration_seconds: 0.4,
              created_at: "2026-07-12T01:02:03Z",
            },
          ]
        : [],
    },
  };
}

function configFixture(jobName: string): ApiJobConfig {
  return {
    schema_version: "1.0",
    job_name: jobName,
    experiment_id: experimentId,
    dataset_id: "00000000-0000-4000-8000-000000000099",
    rvc_backend: {
      backend_type: "rvc_webui",
      repository: "pinned",
      rvc_version: "v2",
      rvc_commit_hash: "a".repeat(40),
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
      save_every_epoch: 10,
      save_only_latest: true,
      save_every_weights: true,
      cache_dataset_in_gpu: false,
      gpu_ids: [0],
    },
    f0_extraction: { training_f0_method: "rmvpe_gpu", rmvpe_gpu_ids: [0] },
    index: { build_index: true, collect_total_fea: true, collect_added_index: true },
    auto_inference_samples: {
      enabled: false,
      test_set_id: null,
      inference_f0_method: "rmvpe",
      transpose: 0,
      index_rate: 0,
      filter_radius: 3,
      resample_sr: 0,
      rms_mix_rate: 1,
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
    resource: { min_vram_gb: 8, preferred_worker_tags: [], priority: 50 },
  };
}
