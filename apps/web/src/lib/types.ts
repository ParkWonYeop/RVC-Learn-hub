export type UserRole = "admin" | "user";

export interface UserSummary {
  id: string;
  email: string;
  role: UserRole;
}

export interface ManagedUser {
  id: string;
  email: string;
  role: UserRole;
  active: boolean;
  rowVersion: number;
  createdAt: string;
  updatedAt: string;
}

export type WorkerStatus = "online" | "busy" | "offline" | "draining";

export type EngineMode = "fake" | "rvc_webui" | null;

export type DatasetStatus =
  | "legacy_imported"
  | "upload_pending"
  | "processing"
  | "ready"
  | "decoder_pending"
  | "failed"
  | "deleting"
  | "delete_failed";

export const jobStatuses = [
  "queued",
  "assigned",
  "downloading_dataset",
  "validating_dataset",
  "preparing_flat_dataset",
  "preprocessing",
  "extracting_f0",
  "extracting_features",
  "training",
  "saving_checkpoint",
  "building_index",
  "collecting_small_model",
  "generating_samples",
  "evaluating",
  "uploading_artifacts",
  "completed",
  "failed",
  "cancelled",
  "retrying",
] as const;

export type JobStatus = (typeof jobStatuses)[number];

export interface WorkerSummary {
  id: string;
  name: string;
  status: WorkerStatus;
  engineMode: Exclude<EngineMode, null>;
  gpuName: string | null;
  vramUsedGb: number | null;
  vramTotalGb: number | null;
  gpuUtilization: number | null;
  temperatureC: number | null;
  currentJob: string | null;
  tags: string[];
  workerVersion: string;
  rvcCommit: string;
  rvcAssetsReady: boolean;
  lastHeartbeat: string | null;
}

export interface JobSummary {
  id: string;
  name: string;
  experiment: string;
  status: JobStatus;
  worker: string | null;
  version: "v1" | "v2";
  sampleRate: "40k" | "48k";
  f0Method: string;
  currentEpoch: number | null;
  totalEpoch: number;
  latestLoss: number | null;
  duration: string | null;
  engineMode: EngineMode;
  hasModel: boolean | null;
  hasIndex: boolean | null;
  hasSamples: boolean | null;
}

export interface JobDetail {
  summary: JobSummary;
  experimentId: string | null;
  datasetId: string | null;
  workerId: string | null;
  currentAttemptId: string | null;
  priority: number | null;
  attemptCount: number | null;
  cancelRequestedAt: string | null;
  errorCode: string | null;
  errorMessage: string | null;
  startedAt: string | null;
  completedAt: string | null;
  createdAt: string | null;
  updatedAt: string | null;
  config: {
    useF0: boolean;
    batchSizePerGpu: number;
    gpuIds: number[];
    saveEveryEpoch: number;
    buildIndex: boolean;
    autoSamples: boolean;
    minVramGb: number;
    preferredWorkerTags: string[];
    collectLogs: boolean;
    collectCheckpoints: boolean;
    collectSmallModel: boolean;
    collectIndex: boolean;
    collectSamples: boolean;
  } | null;
  demo: boolean;
}

export interface DatasetSummary {
  id: string;
  name: string;
  status: DatasetStatus;
  isUsable: boolean;
  originalFilename: string | null;
  originalSizeBytes: number | null;
  originalSha256: string | null;
  originalMimeType: string | null;
  preparedFlatSizeBytes: number | null;
  preparedFlatSha256: string | null;
  manifestSha256: string | null;
  qualityReportSha256: string | null;
  durationMinutes: number | null;
  fileCount: number | null;
  sampleRate: string | null;
  decoderPendingCount: number;
  sourceFileEntries: number | null;
  skippedCount: number | null;
  rejectedCount: number | null;
  duplicateCount: number | null;
  pcmQuality: {
    algorithm: "pcm-sample-weighted-v1";
    validatedFileCount: number;
    sampleCount: number;
    clippingRatio: number;
    silenceRatio: number;
    rmsRatio: number;
    silenceThresholdDbfs: number;
    loudness: {
      algorithm: "itu-r-bs1770-4-mono-stereo-v1";
      scope: "global-gate-over-per-file-complete-blocks-v1";
      blockDurationMs: 400;
      blockOverlapPercent: 75;
      absoluteGateLufs: -70;
      relativeGateLu: -10;
      analyzedFileCount: number;
      blockCount: number;
      gatedBlockCount: number;
      integratedLufs: number | null;
      unavailableReason:
        | "below_absolute_gate"
        | "insufficient_duration"
        | "unsupported_channel_layout"
        | "unsupported_sample_rate"
        | null;
    } | null;
  } | null;
  failureCode: string | null;
  retryable: boolean;
  createdAt: string;
  updatedAt: string;
}

export interface ExperimentSummary {
  id: string;
  name: string;
  datasetName: string;
  runCount: number;
  completedCount: number;
  bestRun: string | null;
  updatedAt: string;
}

export interface ListResult<T> {
  items: T[];
  total: number;
  limitation?: ListLimitation;
}

export interface ListLimitation {
  reason: "item_limit_exceeded";
  maximum: number;
  total: number;
  resource: "datasets" | "experiments" | "jobs" | "workers" | "users";
}

export interface SampleMetricValues {
  peakAmplitude: number;
  rms: number;
  clippingRatio: number;
  silenceRatio: number;
}

export interface SampleView {
  id: string;
  jobId: string;
  attemptId: string;
  testSetId: string;
  testSetItemId: string;
  inputSha256: string;
  modelSha256: string;
  indexSha256: string | null;
  inferenceF0Method: "pm" | "harvest" | "crepe" | "rmvpe";
  inferenceConfigSha256: string;
  nativeInferenceManifestSha256: string;
  nativeInferenceRequestSha256: string;
  outputSizeBytes: number;
  outputSha256: string;
  outputSampleRateHz: number;
  outputChannels: number;
  outputDurationSeconds: number;
  metrics: {
    algorithm: "pcm-normalized-v2";
    authoritativeSource: "manager_computed";
    clippingThreshold: number;
    silenceThreshold: number;
    workerReported: SampleMetricValues;
    managerComputed: SampleMetricValues;
    workerReportedDurationSeconds: number;
    managerComputedSampleRateHz: number;
    managerComputedChannels: number;
    managerComputedDurationSeconds: number;
  };
  rvcCommitHash: string;
  runtimeImageDigest: string;
  runtimeAssetManifestSha256: string;
  createdAt: string;
}

export interface SampleListView {
  items: SampleView[];
  total: number;
  offset: number;
  limit: number;
}
