import type { DatasetStatus, EngineMode, JobStatus, UserRole } from "./types";

export interface ApiUser {
  id: string;
  email: string;
  role: UserRole;
  disabled: boolean;
  created_at: string;
  updated_at: string;
}

export interface ApiAdminUser {
  id: string;
  email: string;
  role: UserRole;
  active: boolean;
  row_version: number;
  created_at: string;
  updated_at: string;
}

export interface ApiAdminUserList {
  items: ApiAdminUser[];
  total: number;
  offset: number;
  limit: number;
}

export interface ApiAdminUserCreateRequest {
  email: string;
  password: string;
  role: UserRole;
  active: boolean;
}

export interface ApiAdminUserUpdateRequest {
  expected_row_version: number;
  role: UserRole;
  active: boolean;
}

export interface ApiAdminUserPasswordResetRequest {
  expected_row_version: number;
  new_password: string;
}

export interface ApiGpuCapability {
  index: number;
  uuid: string | null;
  name: string;
  total_vram_mb: number;
  free_vram_mb: number;
  utilization_percent: number | null;
  temperature_c: number | null;
}

export interface ApiWorker {
  id: string;
  name: string;
  status: "idle" | "busy" | "draining";
  capabilities: {
    engine_mode: "fake" | "rvc_webui";
    gpus: ApiGpuCapability[];
    tags: string[];
    rvc_assets_ready: boolean;
  };
  worker_version: string;
  rvc_commit_hash: string;
  last_heartbeat_at: string | null;
  current_job_id: string | null;
  is_active: boolean;
  online: boolean;
  created_at: string;
  updated_at: string;
}

export interface ApiDataset {
  id: string;
  name: string;
  status: DatasetStatus;
  original_filename: string | null;
  original_size_bytes: number | null;
  original_sha256: string | null;
  original_mime_type: string | null;
  prepared_flat_size_bytes: number | null;
  prepared_flat_sha256: string | null;
  manifest_sha256: string | null;
  quality_report_sha256: string | null;
  duration_sec: number | null;
  file_count: number | null;
  sample_rate: number | null;
  decoder_pending_count: number;
  source_file_entry_count: number | null;
  skipped_file_count: number | null;
  rejected_file_count: number | null;
  duplicate_file_count: number | null;
  pcm_quality: ApiDatasetPcmQuality | null;
  is_usable: boolean;
  failure_code: string | null;
  retryable: boolean;
  created_at: string;
  updated_at: string;
}

export interface ApiDatasetPcmQuality {
  algorithm: "pcm-sample-weighted-v1";
  validated_file_count: number;
  sample_count: number;
  clipping_ratio: number;
  silence_ratio: number;
  rms_ratio: number;
  silence_threshold_dbfs: number;
  loudness: ApiDatasetPcmLoudness | null;
}

export interface ApiDatasetPcmLoudness {
  algorithm: "itu-r-bs1770-4-mono-stereo-v1";
  scope: "global-gate-over-per-file-complete-blocks-v1";
  block_duration_ms: 400;
  block_overlap_percent: 75;
  absolute_gate_lufs: -70;
  relative_gate_lu: -10;
  analyzed_file_count: number;
  block_count: number;
  gated_block_count: number;
  integrated_lufs: number | null;
  unavailable_reason:
    | "below_absolute_gate"
    | "insufficient_duration"
    | "unsupported_channel_layout"
    | "unsupported_sample_rate"
    | null;
}

export interface ApiDatasetUploadInitRequest {
  name: string;
  filename: string;
  content_type: string;
  size_bytes: number;
  sha256: string;
  idempotency_key: string;
}

export type ApiDatasetUploadStatus =
  | "pending"
  | "finalizing"
  | "completed"
  | "failed"
  | "expired";

export interface ApiDatasetUploadInitResponse {
  upload_session_id: string;
  dataset_id: string;
  status: ApiDatasetUploadStatus;
  method: "PUT" | null;
  upload_url: string | null;
  upload_headers: Record<string, string>;
  expires_at: string;
  dataset: ApiDataset | null;
  failure_code: string | null;
  retryable: boolean;
  retry_after_seconds: number | null;
}

export interface ApiExperiment {
  id: string;
  row_version: number;
  name: string;
  dataset_id: string;
  description: string | null;
  created_at: string;
  updated_at: string;
}

export interface ApiExperimentCreateRequest {
  name: string;
  dataset_id: string;
  description: string | null;
}

export interface ApiExperimentUpdateRequest {
  expected_row_version: number;
  description: string | null;
}

export interface ApiExperimentJobName {
  id: string;
  job_name: string;
  status: JobStatus;
  created_at: string;
}

export interface ApiExperimentJobNameList {
  items: ApiExperimentJobName[];
  total: number;
  offset: number;
  limit: number;
}

export interface ExperimentComparisonMetricPoint {
  sequence: number;
  epoch: number | null;
  step: number | null;
  value: number;
  occurred_at: string;
}

export interface ExperimentComparisonMetricSeries {
  key: string;
  total_points: number;
  truncated: boolean;
  points: ExperimentComparisonMetricPoint[];
}

export interface ExperimentComparisonAttempt {
  id: string;
  attempt_number: number;
  engine_mode: Exclude<EngineMode, null>;
  status: JobStatus;
  started_at: string;
  finished_at: string | null;
}

export interface ExperimentComparisonArtifact {
  id: string;
  filename: string;
  size_bytes: number;
  sha256: string;
}

export interface ExperimentComparisonSample {
  id: string;
  test_set_item_id: string;
  output_size_bytes: number;
  output_sha256: string;
  output_sample_rate_hz: number;
  output_channels: number;
  output_duration_seconds: number;
  created_at: string;
}

export interface ExperimentComparisonAvailability {
  final_model: ExperimentComparisonArtifact | null;
  final_index: ExperimentComparisonArtifact | null;
  samples: ExperimentComparisonSample[];
}

export interface ExperimentComparisonJob {
  id: string;
  job_name: string;
  status: JobStatus;
  config: ApiJobConfig;
  current_epoch: number | null;
  total_epoch: number;
  current_attempt: ExperimentComparisonAttempt | null;
  metrics: ExperimentComparisonMetricSeries[];
  availability: ExperimentComparisonAvailability;
}

export interface ExperimentComparisonResponse {
  experiment: ApiExperiment;
  jobs: ExperimentComparisonJob[];
  metric_point_limit_per_key: 200;
}

export type ModelRegistryEntryStatus = "candidate" | "approved" | "revoked";

export type ModelRegistryRevokeReason =
  | "quality_rejected"
  | "security_issue"
  | "operator_request";

export interface ModelRegistryArtifact {
  id: string;
  filename: string;
  size_bytes: number;
  sha256: string;
}

export interface ModelRegistryEntry {
  id: string;
  row_version: number;
  status: ModelRegistryEntryStatus;
  is_active: boolean;
  experiment_id: string;
  source_job_id: string;
  source_job_name: string;
  source_attempt_id: string;
  source_attempt_number: number;
  engine_mode: "rvc_webui";
  model: ModelRegistryArtifact;
  index: ModelRegistryArtifact | null;
  job_config_sha256: string;
  rvc_commit_hash: string;
  runtime_image_digest: string;
  runtime_asset_manifest_sha256: string;
  created_at: string;
  approved_at: string | null;
  revoked_at: string | null;
  revoke_reason: ModelRegistryRevokeReason | null;
}

export interface ModelRegistryPage {
  experiment_id: string;
  registry_row_version: number;
  active_entry_id: string | null;
  can_manage: boolean;
  items: ModelRegistryEntry[];
  total: number;
  offset: number;
  limit: number;
}

export interface ModelRegistrySnapshot {
  experiment_id: string;
  registry_row_version: number;
  active_entry_id: string | null;
  can_manage: boolean;
  items: ModelRegistryEntry[];
  total: number;
}

export interface ModelRegistryMutationResponse {
  experiment_id: string;
  registry_row_version: number;
  active_entry_id: string | null;
  entry: ModelRegistryEntry;
}

export interface ApiCreatedJob extends ApiExperimentJobName {
  experiment_id: string;
  dataset_id: string;
}

export interface ApiJobConfig {
  schema_version: "1.0";
  job_name: string;
  experiment_id: string;
  dataset_id: string;
  rvc_backend: {
    backend_type: "rvc_webui";
    repository: string;
    rvc_version: "v1" | "v2" | null;
    rvc_commit_hash: string | null;
  };
  model: {
    version: "v1" | "v2";
    sample_rate: "40k" | "48k";
    use_f0: boolean;
    speaker_id: number;
  };
  pretrained: {
    mode: "auto" | "custom";
    g_path: string | null;
    d_path: string | null;
    allow_custom_override: boolean;
  };
  training_feature: {
    feature_dir_policy: "auto";
    v1_feature_dir: "3_feature256";
    v2_feature_dir: "3_feature768";
  };
  training: {
    epochs: number;
    batch_size_per_gpu: number;
    save_every_epoch: number;
    save_only_latest: boolean;
    save_every_weights: boolean;
    cache_dataset_in_gpu: boolean;
    gpu_ids: number[];
  };
  f0_extraction: {
    training_f0_method: "pm" | "harvest" | "dio" | "rmvpe" | "rmvpe_gpu" | null;
    rmvpe_gpu_ids: number[] | null;
  };
  index: {
    build_index: boolean;
    collect_total_fea: boolean;
    collect_added_index: boolean;
  };
  auto_inference_samples: {
    enabled: boolean;
    test_set_id: string | null;
    inference_f0_method: "pm" | "harvest" | "crepe" | "rmvpe";
    transpose: number;
    index_rate: number;
    filter_radius: number;
    resample_sr: number;
    rms_mix_rate: number;
    protect: number;
  };
  artifacts: {
    collect_checkpoints: boolean;
    collect_small_model: boolean;
    extract_small_model_if_missing: boolean;
    collect_index: boolean;
    collect_tensorboard: boolean;
    collect_logs: boolean;
    collect_samples: boolean;
  };
  resource: {
    min_vram_gb: number;
    preferred_worker_tags: string[];
    priority: number;
  };
}

export interface ApiJob {
  id: string;
  experiment_id: string;
  dataset_id: string;
  worker_id: string | null;
  job_name: string;
  status: JobStatus;
  config: ApiJobConfig;
  priority: number;
  current_epoch: number | null;
  total_epoch: number;
  attempt_count: number;
  current_attempt_id: string | null;
  current_attempt_engine_mode: EngineMode;
  cancel_requested_at: string | null;
  error_code: string | null;
  error_message: string | null;
  started_at: string | null;
  completed_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface ApiList<T> {
  items: T[];
  total: number;
  offset: number;
  limit: number;
}

export interface ApiAccessToken {
  access_token: string;
  token_type: "bearer";
  expires_in: number;
}

export type ApiLogLevel = "debug" | "info" | "warning" | "error";

export interface ApiJobLog {
  id: string;
  job_id: string;
  attempt_id: string;
  attempt_number: number;
  sequence: number;
  level: ApiLogLevel;
  message: string;
  fields: Record<string, unknown>;
  occurred_at: string;
}

export interface ApiJobLogList {
  items: ApiJobLog[];
  total: number;
  limit: number;
  has_more: boolean;
  next_cursor: string | null;
}

export interface ApiMetric {
  id: string;
  job_id: string;
  attempt_id: string;
  attempt_number: number;
  sequence: number;
  epoch: number | null;
  step: number | null;
  key: string;
  value: number;
  occurred_at: string;
}

export interface ApiMetricList {
  items: ApiMetric[];
  total: number;
  offset: number;
  limit: number;
}

export const artifactTypes = [
  "final_small_model",
  "final_index",
  "total_features",
  "generator_checkpoint",
  "discriminator_checkpoint",
  "train_log",
  "tensorboard",
  "sample",
  "environment",
  "config",
  "dataset_report",
] as const;

export type ApiArtifactType = (typeof artifactTypes)[number];

export interface ApiArtifact {
  id: string;
  job_id: string;
  attempt_id: string;
  artifact_type: ApiArtifactType;
  filename: string;
  size_bytes: number;
  sha256: string;
  mime_type: string | null;
  metadata_json: Record<string, unknown>;
  created_at: string;
}

export interface ApiArtifactList {
  items: ApiArtifact[];
  total: number;
  offset: number;
  limit: number;
}

export interface ApiSampleMetricValues {
  peak_amplitude: number;
  rms: number;
  clipping_ratio: number;
  silence_ratio: number;
}

export interface ApiSampleMetricsEvidence {
  algorithm: "pcm-normalized-v2";
  authoritative_source: "manager_computed";
  clipping_threshold: number;
  silence_threshold: number;
  worker_reported: ApiSampleMetricValues;
  manager_computed: ApiSampleMetricValues;
  worker_reported_duration_seconds: number;
  manager_computed_sample_rate_hz: number;
  manager_computed_channels: number;
  manager_computed_duration_seconds: number;
}

export interface ApiSample {
  id: string;
  job_id: string;
  attempt_id: string;
  test_set_id: string;
  test_set_item_id: string;
  artifact_id: string;
  input_sha256: string;
  model_sha256: string;
  index_sha256: string | null;
  inference_f0_method: "pm" | "harvest" | "crepe" | "rmvpe";
  inference_config_sha256: string;
  native_inference_manifest_sha256: string;
  native_inference_request_sha256: string;
  output_size_bytes: number;
  output_sha256: string;
  output_sample_rate_hz: number;
  output_channels: number;
  output_duration_seconds: number;
  metrics: ApiSampleMetricsEvidence;
  rvc_commit_hash: string;
  runtime_image_digest: string;
  runtime_asset_manifest_sha256: string;
  created_at: string;
}

export interface ApiSampleList {
  items: ApiSample[];
  total: number;
  offset: number;
  limit: number;
}
