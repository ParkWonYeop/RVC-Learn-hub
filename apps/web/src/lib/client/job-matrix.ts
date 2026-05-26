import type { ApiJobConfig } from "@/lib/api-types";

export const MAX_JOB_MATRIX_SIZE = 16;

export const rvcVersions = ["v1", "v2"] as const;
export const sampleRates = ["40k", "48k"] as const;
export const trainingF0Methods = [
  "pm",
  "harvest",
  "dio",
  "rmvpe",
  "rmvpe_gpu",
] as const;

export type RvcVersion = (typeof rvcVersions)[number];
export type SampleRate = (typeof sampleRates)[number];
export type TrainingF0Method = (typeof trainingF0Methods)[number];

export interface JobMatrixOptions {
  prefix: string;
  versions: RvcVersion[];
  sampleRates: SampleRate[];
  useF0: boolean;
  f0Methods: TrainingF0Method[];
  epochs: number;
  batchSizePerGpu: number;
  saveEveryEpoch: number;
  saveOnlyLatest: boolean;
  saveEveryWeights: boolean;
  cacheDatasetInGpu: boolean;
  gpuIds: number[];
  buildIndex: boolean;
  minVramGb: number;
  preferredWorkerTags: string[];
  priority: number;
}

export interface JobMatrixPlan {
  key: string;
  jobName: string;
  version: RvcVersion;
  sampleRate: SampleRate;
  f0Method: TrainingF0Method | null;
  config: ApiJobConfig;
}

export interface JobMatrixResult {
  plans: JobMatrixPlan[];
  errors: string[];
}

const officialRepository = "RVC-Project/Retrieval-based-Voice-Conversion-WebUI";
const safeIdentifier = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/;

export function buildJobMatrix(
  experimentId: string,
  datasetId: string,
  options: JobMatrixOptions,
): JobMatrixResult {
  const errors = validateOptions(experimentId, datasetId, options);
  if (errors.length > 0) return { plans: [], errors };

  const versions = orderedUnique(options.versions, rvcVersions);
  const rates = orderedUnique(options.sampleRates, sampleRates);
  const methods = options.useF0
    ? orderedUnique(options.f0Methods, trainingF0Methods)
    : [null];
  const size = versions.length * rates.length * methods.length;
  if (size > MAX_JOB_MATRIX_SIZE) {
    return {
      plans: [],
      errors: [`조합은 최대 ${MAX_JOB_MATRIX_SIZE}개까지 한 번에 만들 수 있습니다.`],
    };
  }

  const normalizedPrefix = normalizeJobPrefix(options.prefix);
  const plans: JobMatrixPlan[] = [];
  for (const version of versions) {
    for (const sampleRate of rates) {
      for (const f0Method of methods) {
        const jobName = deterministicJobName(normalizedPrefix, version, sampleRate, f0Method, options);
        const config = createJobConfig(
          experimentId,
          datasetId,
          jobName,
          version,
          sampleRate,
          f0Method,
          options,
        );
        plans.push({ key: jobName, jobName, version, sampleRate, f0Method, config });
      }
    }
  }
  return { plans, errors: [] };
}

export function normalizeJobPrefix(value: string): string {
  const normalized = value
    .normalize("NFKD")
    .replace(/[\u0300-\u036f]/g, "")
    .toLowerCase()
    .trim()
    .replace(/[^a-z0-9_.-]+/g, "-")
    .replace(/^[_.-]+|[_.-]+$/g, "")
    .replace(/[-_.]{2,}/g, "-");
  return normalized || "rvc-job";
}

export function parseGpuIds(value: string): number[] | null {
  const parts = value.split(",").map((part) => part.trim());
  if (parts.length < 1 || parts.length > 16 || parts.some((part) => !/^(0|[1-9][0-9]*)$/.test(part))) {
    return null;
  }
  const parsed = parts.map(Number);
  if (parsed.some((item) => !Number.isSafeInteger(item) || item < 0 || item > 31)) return null;
  return new Set(parsed).size === parsed.length ? parsed : null;
}

export function parseWorkerTags(value: string): string[] | null {
  if (!value.trim()) return [];
  const tags = value.split(",").map((tag) => tag.trim());
  if (
    tags.length > 64 ||
    tags.some(
      (tag) =>
        tag.length < 1 ||
        tag.length > 128 ||
        /[\u0000-\u001f\u007f]/.test(tag),
    ) ||
    new Set(tags).size !== tags.length
  ) {
    return null;
  }
  return tags;
}

function validateOptions(
  experimentId: string,
  datasetId: string,
  options: JobMatrixOptions,
): string[] {
  const errors: string[] = [];
  if (!safeIdentifier.test(experimentId) || !safeIdentifier.test(datasetId)) {
    errors.push("Experiment 또는 Dataset ID가 올바르지 않습니다.");
  }
  if (options.versions.length === 0) errors.push("RVC 버전을 하나 이상 선택해 주세요.");
  if (options.sampleRates.length === 0) errors.push("sample rate를 하나 이상 선택해 주세요.");
  if (options.useF0 && options.f0Methods.length === 0) {
    errors.push("F0를 사용할 때는 학습용 F0 방식을 하나 이상 선택해 주세요.");
  }
  if (!integerInRange(options.epochs, 1, 100_000)) errors.push("epoch는 1~100000이어야 합니다.");
  if (!integerInRange(options.batchSizePerGpu, 1, 1_024)) {
    errors.push("GPU당 batch는 1~1024여야 합니다.");
  }
  if (!integerInRange(options.saveEveryEpoch, 1, 100_000)) {
    errors.push("checkpoint 저장 간격은 1~100000이어야 합니다.");
  }
  if (
    options.gpuIds.length < 1 ||
    options.gpuIds.length > 16 ||
    options.gpuIds.some((id) => !integerInRange(id, 0, 31)) ||
    new Set(options.gpuIds).size !== options.gpuIds.length
  ) {
    errors.push("GPU ID는 중복 없이 0~31 범위에서 1~16개여야 합니다.");
  }
  if (!Number.isFinite(options.minVramGb) || options.minVramGb < 0 || options.minVramGb > 1_024) {
    errors.push("최소 VRAM은 0~1024 GiB여야 합니다.");
  }
  if (!integerInRange(options.priority, 0, 10)) errors.push("우선순위는 0~10이어야 합니다.");
  if (
    options.preferredWorkerTags.length > 64 ||
    options.preferredWorkerTags.some(
      (tag) => tag.length < 1 || tag.length > 128 || /[\u0000-\u001f\u007f]/.test(tag),
    ) ||
    new Set(options.preferredWorkerTags).size !== options.preferredWorkerTags.length
  ) {
    errors.push("Worker tag는 중복 없이 각 1~128자, 최대 64개여야 합니다.");
  }
  return errors;
}

function createJobConfig(
  experimentId: string,
  datasetId: string,
  jobName: string,
  version: RvcVersion,
  sampleRate: SampleRate,
  f0Method: TrainingF0Method | null,
  options: JobMatrixOptions,
): ApiJobConfig {
  const buildIndex = options.buildIndex;
  return {
    schema_version: "1.0",
    job_name: jobName,
    experiment_id: experimentId,
    dataset_id: datasetId,
    rvc_backend: {
      backend_type: "rvc_webui",
      repository: officialRepository,
      rvc_version: version,
      rvc_commit_hash: null,
    },
    model: { version, sample_rate: sampleRate, use_f0: options.useF0, speaker_id: 0 },
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
    training: {
      epochs: options.epochs,
      batch_size_per_gpu: options.batchSizePerGpu,
      save_every_epoch: options.saveEveryEpoch,
      save_only_latest: options.saveOnlyLatest,
      save_every_weights: options.saveEveryWeights,
      cache_dataset_in_gpu: options.cacheDatasetInGpu,
      gpu_ids: [...options.gpuIds],
    },
    f0_extraction: {
      training_f0_method: f0Method,
      rmvpe_gpu_ids: f0Method === "rmvpe_gpu" ? [...options.gpuIds] : null,
    },
    index: {
      build_index: buildIndex,
      collect_total_fea: buildIndex,
      collect_added_index: buildIndex,
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
      collect_index: buildIndex,
      collect_tensorboard: true,
      collect_logs: true,
      collect_samples: false,
    },
    resource: {
      min_vram_gb: options.minVramGb,
      preferred_worker_tags: [...options.preferredWorkerTags],
      priority: options.priority,
    },
  };
}

function deterministicJobName(
  prefix: string,
  version: RvcVersion,
  sampleRate: SampleRate,
  f0Method: TrainingF0Method | null,
  options: JobMatrixOptions,
): string {
  const condition = `${version}-${sampleRate}-${f0Method ?? "nof0"}`;
  const signature = stableSignature([
    condition,
    options.epochs,
    options.batchSizePerGpu,
    options.saveEveryEpoch,
    Number(options.saveOnlyLatest),
    Number(options.saveEveryWeights),
    Number(options.cacheDatasetInGpu),
    options.gpuIds.join("."),
    Number(options.buildIndex),
    options.minVramGb,
    options.preferredWorkerTags.join("."),
    options.priority,
  ].join("|"));
  const suffix = `${condition}-e${options.epochs}-b${options.batchSizePerGpu}-${signature}`;
  const available = 128 - suffix.length - 1;
  const safePrefix = prefix.slice(0, Math.max(1, available)).replace(/[_.-]+$/g, "") || "rvc";
  return `${safePrefix}-${suffix}`;
}

function stableSignature(value: string): string {
  let hash = 0x811c9dc5;
  for (let index = 0; index < value.length; index += 1) {
    hash ^= value.charCodeAt(index);
    hash = Math.imul(hash, 0x01000193);
  }
  return (hash >>> 0).toString(16).padStart(8, "0");
}

function orderedUnique<T extends string>(values: T[], order: readonly T[]): T[] {
  const selected = new Set(values);
  return order.filter((value) => selected.has(value));
}

function integerInRange(value: number, minimum: number, maximum: number): boolean {
  return Number.isSafeInteger(value) && value >= minimum && value <= maximum;
}
