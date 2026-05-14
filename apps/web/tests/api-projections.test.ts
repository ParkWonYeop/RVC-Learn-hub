import { describe, expect, it } from "vitest";
import type { ApiDataset, ApiExperiment, ApiJob, ApiWorker } from "@/lib/api-types";
import {
  projectDataset,
  projectExperiment,
  projectJob,
  projectJobDetail,
  projectWorker,
} from "@/lib/api-projections";

describe("API projections never invent unavailable metrics", () => {
  it("keeps missing GPU values null and honors API online state", () => {
    const worker: ApiWorker = {
      id: "worker-1",
      name: "gpu-01",
      status: "idle",
      capabilities: {
        engine_mode: "rvc_webui",
        gpus: [],
        tags: [],
        rvc_assets_ready: false,
      },
      worker_version: "0.1.0",
      rvc_commit_hash: "0123456",
      last_heartbeat_at: null,
      current_job_id: null,
      is_active: true,
      online: false,
      created_at: "2026-07-11T00:00:00Z",
      updated_at: "2026-07-11T00:00:00Z",
    };

    const projected = projectWorker(worker);

    expect(projected.status).toBe("offline");
    expect(projected.gpuName).toBeNull();
    expect(projected.vramUsedGb).toBeNull();
    expect(projected.gpuUtilization).toBeNull();
  });

  it("does not convert absent Dataset quality values into zero", () => {
    const dataset = datasetFixture();

    const projected = projectDataset(dataset);

    expect(projected.status).toBe("upload_pending");
    expect(projected.durationMinutes).toBeNull();
    expect(projected.pcmQuality).toBeNull();
    expect(projected.duplicateCount).toBeNull();
    expect(projected.originalSha256).toBeNull();
    expect(projected.preparedFlatSha256).toBeNull();
  });

  it("projects authoritative Dataset LUFS metadata without deriving a file average", () => {
    const dataset: ApiDataset = {
      ...datasetFixture(),
      status: "ready",
      pcm_quality: {
        algorithm: "pcm-sample-weighted-v1",
        validated_file_count: 2,
        sample_count: 96_000,
        clipping_ratio: 0,
        silence_ratio: 0.1,
        rms_ratio: 0.2,
        silence_threshold_dbfs: -50,
        loudness: {
          algorithm: "itu-r-bs1770-4-mono-stereo-v1",
          scope: "global-gate-over-per-file-complete-blocks-v1",
          block_duration_ms: 400,
          block_overlap_percent: 75,
          absolute_gate_lufs: -70,
          relative_gate_lu: -10,
          analyzed_file_count: 2,
          block_count: 14,
          gated_block_count: 7,
          integrated_lufs: -23.004,
          unavailable_reason: null,
        },
      },
    };

    const projected = projectDataset(dataset);

    expect(projected.pcmQuality?.loudness).toMatchObject({
      scope: "global-gate-over-per-file-complete-blocks-v1",
      blockCount: 14,
      gatedBlockCount: 7,
      integratedLufs: -23.004,
    });
  });

  it("uses only the authoritative current attempt engine mode", () => {
    const job = { ...jobFixture(), current_attempt_engine_mode: "fake" as const };

    const projected = projectJob(job, new Map());

    expect(job.config.rvc_backend.backend_type).toBe("rvc_webui");
    expect(projected.engineMode).toBe("fake");
    expect(projected.latestLoss).toBeNull();
    expect(projected.currentEpoch).toBeNull();
    expect(projected.hasModel).toBeNull();
    expect(projected.hasIndex).toBeNull();
    expect(projected.hasSamples).toBeNull();
  });

  it("keeps the engine mode null before an attempt instead of falling back to config", () => {
    const projected = projectJob(jobFixture(), new Map());

    expect(projected.engineMode).toBeNull();
  });

  it("projects only actual JobRead configuration into Job detail", () => {
    const projected = projectJobDetail(jobFixture(), new Map());

    expect(projected.config).toMatchObject({
      batchSizePerGpu: 8,
      gpuIds: [0],
      buildIndex: true,
      autoSamples: false,
      collectLogs: true,
    });
    expect(projected.errorMessage).toBeNull();
    expect(projected.summary.latestLoss).toBeNull();
    expect(projected.summary.hasModel).toBeNull();
  });

  it("derives Experiment counts only from returned Job records", () => {
    const experiment: ApiExperiment = {
      id: "experiment-1",
      row_version: 1,
      name: "comparison",
      dataset_id: "dataset-1",
      description: null,
      created_at: "2026-07-11T00:00:00Z",
      updated_at: "2026-07-11T00:00:00Z",
    };
    const completed = { ...jobFixture(), status: "completed" as const };
    const queued = { ...jobFixture(), id: "job-2", status: "queued" as const };

    const projected = projectExperiment(
      experiment,
      new Map([["dataset-1", datasetFixture()]]),
      [completed, queued],
    );

    expect(projected.runCount).toBe(2);
    expect(projected.completedCount).toBe(1);
    expect(projected.bestRun).toBeNull();
  });
});

function datasetFixture(): ApiDataset {
  return {
    id: "dataset-1",
    name: "speaker-a",
    status: "upload_pending",
    original_filename: "speaker-a.zip",
    original_size_bytes: null,
    original_sha256: null,
    original_mime_type: "application/zip",
    prepared_flat_size_bytes: null,
    prepared_flat_sha256: null,
    manifest_sha256: null,
    quality_report_sha256: null,
    duration_sec: null,
    file_count: null,
    sample_rate: null,
    decoder_pending_count: 0,
    source_file_entry_count: null,
    skipped_file_count: null,
    rejected_file_count: null,
    duplicate_file_count: null,
    pcm_quality: null,
    is_usable: false,
    failure_code: null,
    retryable: false,
    created_at: "2026-07-11T00:00:00Z",
    updated_at: "2026-07-11T00:00:00Z",
  };
}

function jobFixture(): ApiJob {
  return {
    id: "job-1",
    experiment_id: "experiment-1",
    dataset_id: "dataset-1",
    worker_id: null,
    job_name: "speaker-a-v2",
    status: "queued",
    config: {
      schema_version: "1.0",
      job_name: "speaker-a-v2",
      experiment_id: "experiment-1",
      dataset_id: "dataset-1",
      model: { version: "v2", sample_rate: "40k", use_f0: true, speaker_id: 0 },
      rvc_backend: {
        backend_type: "rvc_webui",
        repository: "RVC-Project/Retrieval-based-Voice-Conversion-WebUI",
        rvc_version: null,
        rvc_commit_hash: null,
      },
      pretrained: {
        mode: "auto",
        g_path: null,
        d_path: null,
        allow_custom_override: false,
      },
      training_feature: {
        feature_dir_policy: "auto",
        v1_feature_dir: "3_feature256",
        v2_feature_dir: "3_feature768",
      },
      f0_extraction: { training_f0_method: "rmvpe", rmvpe_gpu_ids: null },
      training: {
        epochs: 80,
        batch_size_per_gpu: 8,
        save_every_epoch: 5,
        save_only_latest: false,
        save_every_weights: true,
        cache_dataset_in_gpu: false,
        gpu_ids: [0],
      },
      index: {
        build_index: true,
        collect_total_fea: true,
        collect_added_index: true,
      },
      auto_inference_samples: {
        enabled: false,
        test_set_id: null,
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
      resource: { min_vram_gb: 0, preferred_worker_tags: [], priority: 5 },
    },
    priority: 5,
    current_epoch: null,
    total_epoch: 80,
    attempt_count: 0,
    current_attempt_id: null,
    current_attempt_engine_mode: null,
    cancel_requested_at: null,
    error_code: null,
    error_message: null,
    started_at: null,
    completed_at: null,
    created_at: "2026-07-11T00:00:00Z",
    updated_at: "2026-07-11T00:00:00Z",
  };
}
