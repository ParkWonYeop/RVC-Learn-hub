import "server-only";

import type {
  SampleListView,
  SampleMetricValues,
  SampleView,
} from "@/lib/types";

const uuidPattern =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const sha256Pattern = /^[0-9a-f]{64}$/;
const commitPattern = /^[0-9a-f]{40}$/;
const imageDigestPattern = /^sha256:[0-9a-f]{64}$/;
const inferenceF0Methods = new Set(["pm", "harvest", "crepe", "rmvpe"]);

export const SAMPLE_LIST_RESPONSE_MAX_BYTES = 512 * 1024;
export const SAMPLE_DOWNLOAD_MAX_BYTES = 256 * 1024 * 1024;

export function projectSampleList(
  value: unknown,
  expectedJobId: string,
): SampleListView | null {
  const root = record(value);
  if (!root || !Array.isArray(root.items)) return null;
  const total = integer(root.total, 0, Number.MAX_SAFE_INTEGER);
  const offset = integer(root.offset, 0, Number.MAX_SAFE_INTEGER);
  const limit = integer(root.limit, 1, 200);
  if (total === null || offset === null || limit === null || root.items.length > limit) {
    return null;
  }
  if (root.items.length > total) return null;

  const items: SampleView[] = [];
  const seenIds = new Set<string>();
  for (const candidate of root.items) {
    const sample = projectSample(candidate, expectedJobId);
    if (!sample || seenIds.has(sample.id)) return null;
    seenIds.add(sample.id);
    items.push(sample);
  }
  return { items, total, offset, limit };
}

function projectSample(value: unknown, expectedJobId: string): SampleView | null {
  const sample = record(value);
  if (!sample) return null;
  const id = uuid(sample.id);
  const jobId = uuid(sample.job_id);
  const attemptId = uuid(sample.attempt_id);
  const testSetId = uuid(sample.test_set_id);
  const testSetItemId = uuid(sample.test_set_item_id);
  const inputSha256 = sha256(sample.input_sha256);
  const modelSha256 = sha256(sample.model_sha256);
  const indexSha256 = nullableSha256(sample.index_sha256);
  const inferenceConfigSha256 = sha256(sample.inference_config_sha256);
  const nativeInferenceManifestSha256 = sha256(
    sample.native_inference_manifest_sha256,
  );
  const nativeInferenceRequestSha256 = sha256(
    sample.native_inference_request_sha256,
  );
  const outputSha256 = sha256(sample.output_sha256);
  const rvcCommitHash = stringPattern(sample.rvc_commit_hash, commitPattern);
  const runtimeImageDigest = stringPattern(
    sample.runtime_image_digest,
    imageDigestPattern,
  );
  const runtimeAssetManifestSha256 = sha256(
    sample.runtime_asset_manifest_sha256,
  );
  const outputSizeBytes = integer(sample.output_size_bytes, 1, SAMPLE_DOWNLOAD_MAX_BYTES);
  const outputSampleRateHz = integer(sample.output_sample_rate_hz, 8_000, 192_000);
  const outputChannels = integer(sample.output_channels, 1, 2);
  const outputDurationSeconds = finiteNumber(sample.output_duration_seconds, 0, 600, false);
  const createdAt = timestamp(sample.created_at);
  const inferenceF0Method =
    typeof sample.inference_f0_method === "string" &&
    inferenceF0Methods.has(sample.inference_f0_method)
      ? (sample.inference_f0_method as SampleView["inferenceF0Method"])
      : null;
  const metrics = projectMetrics(
    sample.metrics,
    outputSampleRateHz,
    outputChannels,
    outputDurationSeconds,
  );
  if (
    !id ||
    !jobId ||
    jobId !== expectedJobId ||
    !attemptId ||
    !testSetId ||
    !testSetItemId ||
    !inputSha256 ||
    !modelSha256 ||
    indexSha256 === undefined ||
    !inferenceF0Method ||
    !inferenceConfigSha256 ||
    !nativeInferenceManifestSha256 ||
    !nativeInferenceRequestSha256 ||
    outputSizeBytes === null ||
    !outputSha256 ||
    outputSampleRateHz === null ||
    outputChannels === null ||
    outputDurationSeconds === null ||
    !metrics ||
    !rvcCommitHash ||
    !runtimeImageDigest ||
    !runtimeAssetManifestSha256 ||
    !createdAt
  ) {
    return null;
  }
  return {
    id,
    jobId,
    attemptId,
    testSetId,
    testSetItemId,
    inputSha256,
    modelSha256,
    indexSha256,
    inferenceF0Method,
    inferenceConfigSha256,
    nativeInferenceManifestSha256,
    nativeInferenceRequestSha256,
    outputSizeBytes,
    outputSha256,
    outputSampleRateHz,
    outputChannels,
    outputDurationSeconds,
    metrics,
    rvcCommitHash,
    runtimeImageDigest,
    runtimeAssetManifestSha256,
    createdAt,
  };
}

function projectMetrics(
  value: unknown,
  sampleRate: number | null,
  channels: number | null,
  duration: number | null,
): SampleView["metrics"] | null {
  const metrics = record(value);
  if (!metrics || sampleRate === null || channels === null || duration === null) {
    return null;
  }
  const workerReported = metricValues(metrics.worker_reported);
  const managerComputed = metricValues(metrics.manager_computed);
  const clippingThreshold = finiteNumber(metrics.clipping_threshold, 0, 1);
  const silenceThreshold = finiteNumber(metrics.silence_threshold, 0, 1);
  const workerDuration = finiteNumber(
    metrics.worker_reported_duration_seconds,
    0,
    600,
    false,
  );
  const managerSampleRate = integer(
    metrics.manager_computed_sample_rate_hz,
    8_000,
    192_000,
  );
  const managerChannels = integer(metrics.manager_computed_channels, 1, 2);
  const managerDuration = finiteNumber(
    metrics.manager_computed_duration_seconds,
    0,
    600,
    false,
  );
  const durationTolerance = Math.max(1 / sampleRate, 0.000001);
  if (
    metrics.algorithm !== "pcm-normalized-v2" ||
    metrics.authoritative_source !== "manager_computed" ||
    !workerReported ||
    !managerComputed ||
    clippingThreshold !== 0.999 ||
    silenceThreshold !== 0.0001 ||
    workerDuration === null ||
    managerSampleRate !== sampleRate ||
    managerChannels !== channels ||
    managerDuration === null ||
    Math.abs(workerDuration - duration) > durationTolerance ||
    Math.abs(managerDuration - duration) > durationTolerance
  ) {
    return null;
  }
  return {
    algorithm: "pcm-normalized-v2",
    authoritativeSource: "manager_computed",
    clippingThreshold,
    silenceThreshold,
    workerReported,
    managerComputed,
    workerReportedDurationSeconds: workerDuration,
    managerComputedSampleRateHz: managerSampleRate,
    managerComputedChannels: managerChannels,
    managerComputedDurationSeconds: managerDuration,
  };
}

function metricValues(value: unknown): SampleMetricValues | null {
  const metrics = record(value);
  if (!metrics) return null;
  const peakAmplitude = finiteNumber(metrics.peak_amplitude, 0, 1);
  const rms = finiteNumber(metrics.rms, 0, 1);
  const clippingRatio = finiteNumber(metrics.clipping_ratio, 0, 1);
  const silenceRatio = finiteNumber(metrics.silence_ratio, 0, 1);
  if (
    peakAmplitude === null ||
    rms === null ||
    clippingRatio === null ||
    silenceRatio === null
  ) {
    return null;
  }
  return { peakAmplitude, rms, clippingRatio, silenceRatio };
}

function record(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function uuid(value: unknown): string | null {
  return stringPattern(value, uuidPattern);
}

function sha256(value: unknown): string | null {
  return stringPattern(value, sha256Pattern);
}

function nullableSha256(value: unknown): string | null | undefined {
  if (value === null) return null;
  return sha256(value) ?? undefined;
}

function stringPattern(value: unknown, pattern: RegExp): string | null {
  return typeof value === "string" && pattern.test(value) ? value : null;
}

function integer(value: unknown, minimum: number, maximum: number): number | null {
  return typeof value === "number" &&
    Number.isSafeInteger(value) &&
    value >= minimum &&
    value <= maximum
    ? value
    : null;
}

function finiteNumber(
  value: unknown,
  minimum: number,
  maximum: number,
  inclusiveMinimum = true,
): number | null {
  return typeof value === "number" &&
    Number.isFinite(value) &&
    (inclusiveMinimum ? value >= minimum : value > minimum) &&
    value <= maximum
    ? value
    : null;
}

function timestamp(value: unknown): string | null {
  return typeof value === "string" &&
    value.length <= 64 &&
    Number.isFinite(Date.parse(value))
    ? value
    : null;
}
