import type { ApiDataset, ApiExperiment, ApiJob, ApiWorker } from "./api-types";
import type {
  DatasetSummary,
  ExperimentSummary,
  JobDetail,
  JobSummary,
  WorkerSummary,
} from "./types";

export function projectWorker(worker: ApiWorker): WorkerSummary {
  const gpu = worker.capabilities.gpus[0];
  const status = !worker.online
    ? "offline"
    : worker.status === "busy"
      ? "busy"
      : worker.status === "draining"
        ? "draining"
        : "online";
  return {
    id: worker.id,
    name: worker.name,
    status,
    engineMode: worker.capabilities.engine_mode,
    gpuName: gpu?.name ?? null,
    vramUsedGb: gpu ? megabytesToGigabytes(gpu.total_vram_mb - gpu.free_vram_mb) : null,
    vramTotalGb: gpu ? megabytesToGigabytes(gpu.total_vram_mb) : null,
    gpuUtilization: gpu?.utilization_percent ?? null,
    temperatureC: gpu?.temperature_c ?? null,
    currentJob: worker.current_job_id,
    tags: worker.capabilities.tags,
    workerVersion: worker.worker_version,
    rvcCommit: worker.rvc_commit_hash,
    rvcAssetsReady: worker.capabilities.rvc_assets_ready,
    lastHeartbeat: worker.last_heartbeat_at ? formatTimestamp(worker.last_heartbeat_at) : null,
  };
}

export function projectDataset(dataset: ApiDataset): DatasetSummary {
  return {
    id: dataset.id,
    name: dataset.name,
    status: dataset.status,
    isUsable: dataset.is_usable,
    originalFilename: dataset.original_filename,
    originalSizeBytes: dataset.original_size_bytes,
    originalSha256: dataset.original_sha256,
    originalMimeType: dataset.original_mime_type,
    preparedFlatSizeBytes: dataset.prepared_flat_size_bytes,
    preparedFlatSha256: dataset.prepared_flat_sha256,
    manifestSha256: dataset.manifest_sha256,
    qualityReportSha256: dataset.quality_report_sha256,
    durationMinutes:
      dataset.duration_sec === null ? null : round(dataset.duration_sec / 60, 1),
    fileCount: dataset.file_count,
    sampleRate: dataset.sample_rate === null ? null : `${dataset.sample_rate / 1000} kHz`,
    decoderPendingCount: dataset.decoder_pending_count,
    sourceFileEntries: dataset.source_file_entry_count ?? null,
    skippedCount: dataset.skipped_file_count ?? null,
    rejectedCount: dataset.rejected_file_count ?? null,
    duplicateCount: dataset.duplicate_file_count ?? null,
    pcmQuality: dataset.pcm_quality === null || dataset.pcm_quality === undefined
      ? null
      : {
          algorithm: dataset.pcm_quality.algorithm,
          validatedFileCount: dataset.pcm_quality.validated_file_count,
          sampleCount: dataset.pcm_quality.sample_count,
          clippingRatio: dataset.pcm_quality.clipping_ratio,
          silenceRatio: dataset.pcm_quality.silence_ratio,
          rmsRatio: dataset.pcm_quality.rms_ratio,
          silenceThresholdDbfs: dataset.pcm_quality.silence_threshold_dbfs,
          loudness: dataset.pcm_quality.loudness === null
            ? null
            : {
                algorithm: dataset.pcm_quality.loudness.algorithm,
                scope: dataset.pcm_quality.loudness.scope,
                blockDurationMs: dataset.pcm_quality.loudness.block_duration_ms,
                blockOverlapPercent: dataset.pcm_quality.loudness.block_overlap_percent,
                absoluteGateLufs: dataset.pcm_quality.loudness.absolute_gate_lufs,
                relativeGateLu: dataset.pcm_quality.loudness.relative_gate_lu,
                analyzedFileCount: dataset.pcm_quality.loudness.analyzed_file_count,
                blockCount: dataset.pcm_quality.loudness.block_count,
                gatedBlockCount: dataset.pcm_quality.loudness.gated_block_count,
                integratedLufs: dataset.pcm_quality.loudness.integrated_lufs,
                unavailableReason: dataset.pcm_quality.loudness.unavailable_reason,
              },
        },
    failureCode: dataset.failure_code,
    retryable: dataset.retryable,
    createdAt: formatDate(dataset.created_at),
    updatedAt: formatTimestamp(dataset.updated_at),
  };
}

export function projectExperiment(
  experiment: ApiExperiment,
  datasets: Map<string, ApiDataset>,
  jobs: ApiJob[],
): ExperimentSummary {
  const experimentJobs = jobs.filter((job) => job.experiment_id === experiment.id);
  return {
    id: experiment.id,
    name: experiment.name,
    datasetName: datasets.get(experiment.dataset_id)?.name ?? shortId(experiment.dataset_id),
    runCount: experimentJobs.length,
    completedCount: experimentJobs.filter((job) => job.status === "completed").length,
    bestRun: null,
    updatedAt: formatTimestamp(experiment.updated_at),
  };
}

export function projectJob(
  job: ApiJob,
  experiments: Map<string, ApiExperiment>,
): JobSummary {
  return {
    id: job.id,
    name: job.job_name,
    experiment: experiments.get(job.experiment_id)?.name ?? shortId(job.experiment_id),
    status: job.status,
    worker: job.worker_id ? shortId(job.worker_id) : null,
    version: job.config.model.version,
    sampleRate: job.config.model.sample_rate,
    f0Method: job.config.f0_extraction.training_f0_method ?? "F0 미사용",
    currentEpoch: job.current_epoch,
    totalEpoch: job.total_epoch,
    latestLoss: null,
    duration: elapsedDuration(job.started_at, job.completed_at),
    engineMode: job.current_attempt_engine_mode,
    hasModel: null,
    hasIndex: null,
    hasSamples: null,
  };
}

export function projectJobDetail(
  job: ApiJob,
  experiments: Map<string, ApiExperiment>,
): JobDetail {
  return {
    summary: projectJob(job, experiments),
    experimentId: job.experiment_id,
    datasetId: job.dataset_id,
    workerId: job.worker_id,
    currentAttemptId: job.current_attempt_id,
    priority: job.priority,
    attemptCount: job.attempt_count,
    cancelRequestedAt: job.cancel_requested_at,
    errorCode: job.error_code,
    errorMessage: job.error_message,
    startedAt: job.started_at,
    completedAt: job.completed_at,
    createdAt: job.created_at,
    updatedAt: job.updated_at,
    config: {
      useF0: job.config.model.use_f0,
      batchSizePerGpu: job.config.training.batch_size_per_gpu,
      gpuIds: job.config.training.gpu_ids,
      saveEveryEpoch: job.config.training.save_every_epoch,
      buildIndex: job.config.index.build_index,
      autoSamples: job.config.auto_inference_samples.enabled,
      minVramGb: job.config.resource.min_vram_gb,
      preferredWorkerTags: job.config.resource.preferred_worker_tags,
      collectLogs: job.config.artifacts.collect_logs,
      collectCheckpoints: job.config.artifacts.collect_checkpoints,
      collectSmallModel: job.config.artifacts.collect_small_model,
      collectIndex: job.config.artifacts.collect_index,
      collectSamples: job.config.artifacts.collect_samples,
    },
    demo: false,
  };
}

function megabytesToGigabytes(value: number): number {
  return round(Math.max(0, value) / 1024, 1);
}

function elapsedDuration(start: string | null, end: string | null): string | null {
  if (!start) return null;
  const startTime = Date.parse(start);
  const endTime = end ? Date.parse(end) : Date.now();
  if (!Number.isFinite(startTime) || !Number.isFinite(endTime) || endTime < startTime) {
    return null;
  }
  const minutes = Math.floor((endTime - startTime) / 60_000);
  if (minutes < 60) return `${minutes}분`;
  return `${Math.floor(minutes / 60)}시간 ${minutes % 60}분`;
}

function formatDate(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return new Intl.DateTimeFormat("ko-KR", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    timeZone: "UTC",
  }).format(date);
}

function formatTimestamp(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return new Intl.DateTimeFormat("ko-KR", {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

function shortId(value: string): string {
  return value.length > 12 ? `${value.slice(0, 8)}…` : value;
}

function round(value: number, precision: number): number {
  const factor = 10 ** precision;
  return Math.round(value * factor) / factor;
}
