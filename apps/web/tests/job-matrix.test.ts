import { describe, expect, it } from "vitest";
import {
  buildJobMatrix,
  MAX_JOB_MATRIX_SIZE,
  normalizeJobPrefix,
  parseGpuIds,
  parseWorkerTags,
  type JobMatrixOptions,
} from "@/lib/client/job-matrix";

describe("Experiment Job matrix", () => {
  it("builds deterministic, safe and unique names with immutable configs", () => {
    const options = baseOptions({
      prefix: "화자 A / comparison !!!",
      versions: ["v2", "v1"],
      sampleRates: ["48k", "40k"],
      f0Methods: ["rmvpe_gpu", "harvest"],
      gpuIds: [0, 1],
    });
    const first = buildJobMatrix("experiment-1", "dataset-1", options);
    const second = buildJobMatrix("experiment-1", "dataset-1", options);

    expect(first.errors).toEqual([]);
    expect(first.plans).toHaveLength(8);
    expect(first.plans.map((plan) => plan.jobName)).toEqual(
      second.plans.map((plan) => plan.jobName),
    );
    expect(new Set(first.plans.map((plan) => plan.jobName))).toHaveLength(8);
    for (const plan of first.plans) {
      expect(plan.jobName).toMatch(/^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/);
      expect(plan.config.experiment_id).toBe("experiment-1");
      expect(plan.config.dataset_id).toBe("dataset-1");
      expect(plan.config.rvc_backend.rvc_version).toBe(plan.version);
      expect(plan.config.auto_inference_samples).toMatchObject({
        enabled: false,
        test_set_id: null,
      });
      expect(plan.config.artifacts.collect_samples).toBe(false);
      expect(plan.config.f0_extraction.rmvpe_gpu_ids).toEqual(
        plan.f0Method === "rmvpe_gpu" ? [0, 1] : null,
      );
    }
  });

  it("collapses use_f0=false to one null-F0 condition", () => {
    const result = buildJobMatrix(
      "experiment-1",
      "dataset-1",
      baseOptions({ useF0: false, f0Methods: ["pm", "rmvpe_gpu"] }),
    );

    expect(result.errors).toEqual([]);
    expect(result.plans).toHaveLength(1);
    expect(result.plans[0]?.f0Method).toBeNull();
    expect(result.plans[0]?.config.model.use_f0).toBe(false);
    expect(result.plans[0]?.config.f0_extraction).toEqual({
      training_f0_method: null,
      rmvpe_gpu_ids: null,
    });
  });

  it("fails closed when the cartesian product exceeds the UI limit", () => {
    const result = buildJobMatrix(
      "experiment-1",
      "dataset-1",
      baseOptions({
        versions: ["v1", "v2"],
        sampleRates: ["40k", "48k"],
        f0Methods: ["pm", "harvest", "dio", "rmvpe", "rmvpe_gpu"],
      }),
    );

    expect(MAX_JOB_MATRIX_SIZE).toBe(16);
    expect(result.plans).toEqual([]);
    expect(result.errors.join(" ")).toContain("최대 16개");
  });

  it("parses GPU IDs and Worker tags without accepting duplicates", () => {
    expect(parseGpuIds("0, 2,10")).toEqual([0, 2, 10]);
    expect(parseGpuIds("0,0")).toBeNull();
    expect(parseGpuIds("-1")).toBeNull();
    expect(parseWorkerTags("24gb, rmvpe")).toEqual(["24gb", "rmvpe"]);
    expect(parseWorkerTags("rmvpe,rmvpe")).toBeNull();
    expect(normalizeJobPrefix(" 한글 이름 ")).toBe("rvc-job");
  });
});

function baseOptions(overrides: Partial<JobMatrixOptions> = {}): JobMatrixOptions {
  return {
    prefix: "speaker-a",
    versions: ["v2"],
    sampleRates: ["40k"],
    useF0: true,
    f0Methods: ["rmvpe"],
    epochs: 80,
    batchSizePerGpu: 8,
    saveEveryEpoch: 5,
    saveOnlyLatest: false,
    saveEveryWeights: true,
    cacheDatasetInGpu: false,
    gpuIds: [0],
    buildIndex: true,
    minVramGb: 12,
    preferredWorkerTags: ["24gb"],
    priority: 5,
    ...overrides,
  };
}
