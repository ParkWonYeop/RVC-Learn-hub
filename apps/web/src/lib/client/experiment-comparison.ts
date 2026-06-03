import type {
  ExperimentComparisonArtifact,
  ExperimentComparisonJob,
  ExperimentComparisonMetricSeries,
  ExperimentComparisonResponse,
} from "@/lib/api-types";
import { jobStatuses, type JobStatus } from "@/lib/types";

const canonicalId =
  /^[a-f0-9]{8}-[a-f0-9]{4}-[1-5][a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$/;
const metricKey = /^[A-Za-z0-9_.-]{1,128}$/;
const sha256 = /^[a-f0-9]{64}$/;
const jobStatusSet = new Set<string>(jobStatuses);

export interface ComparisonSelectableJob {
  id: string;
  name: string;
  status: JobStatus;
}

export class ExperimentComparisonReadError extends Error {
  constructor(readonly status: number) {
    super(`Experiment comparison BFF request failed with status ${status}`);
    this.name = "ExperimentComparisonReadError";
  }
}

export function defaultComparisonJobIds(jobs: ComparisonSelectableJob[]): string[] {
  const selected: string[] = [];
  const seen = new Set<string>();
  for (const job of jobs) {
    if (seen.has(job.id)) continue;
    seen.add(job.id);
    selected.push(job.id);
    if (selected.length === 4) break;
  }
  return selected.length >= 2 ? selected : [];
}

export async function fetchExperimentComparison(
  experimentId: string,
  jobIds: string[],
  signal?: AbortSignal,
): Promise<ExperimentComparisonResponse> {
  if (!canonicalId.test(experimentId) || !isValidSelection(jobIds)) {
    throw new ExperimentComparisonReadError(422);
  }
  const query = new URLSearchParams();
  for (const jobId of jobIds) query.append("job_ids", jobId);
  const response = await fetch(
    `/bff/experiments/${encodeURIComponent(experimentId)}/comparison?${query.toString()}`,
    {
      cache: "no-store",
      credentials: "same-origin",
      headers: { Accept: "application/json" },
      signal,
    },
  );
  if (!response.ok) throw new ExperimentComparisonReadError(response.status);
  let value: unknown;
  try {
    value = await response.json();
  } catch {
    throw new ExperimentComparisonReadError(502);
  }
  if (!isComparisonResponse(value, experimentId, jobIds)) {
    throw new ExperimentComparisonReadError(502);
  }
  return value;
}

export function comparisonMetricKeys(jobs: ExperimentComparisonJob[]): string[] {
  const keys = new Set(jobs.flatMap((job) => job.metrics.map((series) => series.key)));
  return [...keys].sort((left, right) => {
    const leftRank = metricRank(left);
    const rightRank = metricRank(right);
    return leftRank === rightRank ? left.localeCompare(right) : leftRank - rightRank;
  });
}

export function selectedMetricSeries(
  jobs: ExperimentComparisonJob[],
  key: string,
): Array<{ job: ExperimentComparisonJob; series: ExperimentComparisonMetricSeries | null }> {
  return jobs.map((job) => ({
    job,
    series: job.metrics.find((candidate) => candidate.key === key) ?? null,
  }));
}

export function metricPolyline(
  points: Array<{ sequence: number; value: number }>,
  minimumValue: number,
  maximumValue: number,
  minimumSequence: number,
  maximumSequence: number,
  width = 760,
  height = 220,
  padding = 18,
): string {
  if (
    points.length === 0 ||
    !points.every(
      (point) => Number.isSafeInteger(point.sequence) && Number.isFinite(point.value),
    ) ||
    !Number.isFinite(minimumValue) ||
    !Number.isFinite(maximumValue) ||
    !Number.isSafeInteger(minimumSequence) ||
    !Number.isSafeInteger(maximumSequence) ||
    maximumSequence < minimumSequence ||
    width <= padding * 2 ||
    height <= padding * 2
  ) {
    return "";
  }
  const valueSpread = maximumValue - minimumValue;
  const sequenceSpread = maximumSequence - minimumSequence;
  return points
    .map((point) => {
      const x =
        sequenceSpread === 0
          ? width / 2
          : padding +
            ((point.sequence - minimumSequence) / sequenceSpread) * (width - padding * 2);
      const y =
        valueSpread === 0
          ? height / 2
          : height -
            padding -
            ((point.value - minimumValue) / valueSpread) * (height - padding * 2);
      return `${x.toFixed(2)},${y.toFixed(2)}`;
    })
    .join(" ");
}

export function comparisonErrorMessage(status: number | null): string {
  switch (status) {
    case 401:
      return "세션이 만료되었습니다. 다시 로그인해 주세요.";
    case 403:
      return "이 Experiment의 비교 데이터를 볼 권한이 없습니다.";
    case 409:
      return "current attempt 원장이 변경되었거나 일관되지 않아 비교를 중단했습니다.";
    case 422:
      return "서로 다른 Job을 2개 이상 16개 이하로 선택해 주세요.";
    case 503:
      return "Artifact 저장소를 현재 검증할 수 없습니다. 잠시 후 다시 시도해 주세요.";
    default:
      return "실험 비교 데이터를 불러오지 못했습니다.";
  }
}

export function formatAttemptDuration(
  startedAt: string,
  finishedAt: string | null,
): string {
  if (finishedAt === null) return "진행 중";
  const start = Date.parse(startedAt);
  const finish = Date.parse(finishedAt);
  if (!Number.isFinite(start) || !Number.isFinite(finish) || finish < start) return "—";
  const totalSeconds = Math.floor((finish - start) / 1_000);
  const hours = Math.floor(totalSeconds / 3_600);
  const minutes = Math.floor((totalSeconds % 3_600) / 60);
  const seconds = totalSeconds % 60;
  if (hours > 0) return `${hours}시간 ${minutes}분 ${seconds}초`;
  if (minutes > 0) return `${minutes}분 ${seconds}초`;
  return `${seconds}초`;
}

export function formatComparisonBytes(value: number): string {
  const units = ["B", "KiB", "MiB", "GiB", "TiB"] as const;
  let amount = value;
  let unit = 0;
  while (amount >= 1_024 && unit < units.length - 1) {
    amount /= 1_024;
    unit += 1;
  }
  return `${new Intl.NumberFormat("ko-KR", { maximumFractionDigits: 2 }).format(amount)} ${units[unit]}`;
}

function isValidSelection(jobIds: string[]): boolean {
  return (
    jobIds.length >= 2 &&
    jobIds.length <= 16 &&
    new Set(jobIds).size === jobIds.length &&
    jobIds.every((jobId) => canonicalId.test(jobId))
  );
}

function isComparisonResponse(
  value: unknown,
  experimentId: string,
  expectedJobIds: string[],
): value is ExperimentComparisonResponse {
  const root = record(value);
  const experiment = record(root?.experiment);
  if (
    !root ||
    !experiment ||
    experiment.id !== experimentId ||
    root.metric_point_limit_per_key !== 200 ||
    !Array.isArray(root.jobs) ||
    root.jobs.length !== expectedJobIds.length
  ) {
    return false;
  }
  return root.jobs.every(
    (job, index) => isComparisonJob(job) && job.id === expectedJobIds[index],
  );
}

function isComparisonJob(value: unknown): value is ExperimentComparisonJob {
  const job = record(value);
  const config = record(job?.config);
  const model = record(config?.model);
  const backend = record(config?.rvc_backend);
  const training = record(config?.training);
  const f0 = record(config?.f0_extraction);
  const index = record(config?.index);
  const availability = record(job?.availability);
  if (
    !job ||
    typeof job.id !== "string" ||
    typeof job.job_name !== "string" ||
    !jobStatusSet.has(String(job.status)) ||
    !isNullableInteger(job.current_epoch, 0) ||
    !isInteger(job.total_epoch, 1) ||
    !config ||
    !model ||
    !backend ||
    !training ||
    !f0 ||
    !index ||
    !isModel(model) ||
    !isTraining(training) ||
    !isF0(f0) ||
    !isIndex(index) ||
    !["v1", "v2", null].includes(backend.rvc_version as "v1" | "v2" | null) ||
    !isAttempt(job.current_attempt) ||
    !Array.isArray(job.metrics) ||
    !uniqueSeries(job.metrics) ||
    !availability ||
    !isArtifactOrNull(availability.final_model) ||
    !isArtifactOrNull(availability.final_index) ||
    !Array.isArray(availability.samples) ||
    availability.samples.length > 128 ||
    !uniqueSamples(availability.samples)
  ) {
    return false;
  }
  return true;
}

function uniqueSeries(values: unknown[]): boolean {
  const keys = new Set<string>();
  for (const value of values) {
    const series = record(value);
    if (!series || !isSeries(series) || keys.has(series.key as string)) return false;
    keys.add(series.key as string);
  }
  return true;
}

function isSeries(series: Record<string, unknown>): boolean {
  if (
    typeof series.key !== "string" ||
    !metricKey.test(series.key) ||
    !isInteger(series.total_points, 1) ||
    typeof series.truncated !== "boolean" ||
    !Array.isArray(series.points) ||
    series.points.length < 1 ||
    series.points.length > 200 ||
    series.total_points < series.points.length ||
    series.truncated !== (series.total_points > series.points.length)
  ) {
    return false;
  }
  let lastSequence = -1;
  for (const value of series.points) {
    const point = record(value);
    const observed = typeof point?.occurred_at === "string" ? Date.parse(point.occurred_at) : NaN;
    if (
      !point ||
      !isInteger(point.sequence, 0) ||
      point.sequence <= lastSequence ||
      !isNullableInteger(point.epoch, 0) ||
      !isNullableInteger(point.step, 0) ||
      typeof point.value !== "number" ||
      !Number.isFinite(point.value) ||
      !Number.isFinite(observed)
    ) {
      return false;
    }
    lastSequence = point.sequence;
  }
  return true;
}

function isAttempt(value: unknown): boolean {
  if (value === null) return true;
  const attempt = record(value);
  return Boolean(
    attempt &&
      typeof attempt.id === "string" &&
      isInteger(attempt.attempt_number, 1) &&
      (attempt.engine_mode === "fake" || attempt.engine_mode === "rvc_webui") &&
      jobStatusSet.has(String(attempt.status)) &&
      isTimestamp(attempt.started_at) &&
      (attempt.finished_at === null || isTimestamp(attempt.finished_at)),
  );
}

function isArtifactOrNull(value: unknown): value is ExperimentComparisonArtifact | null {
  if (value === null) return true;
  const artifact = record(value);
  return Boolean(
    artifact &&
      typeof artifact.id === "string" &&
      typeof artifact.filename === "string" &&
      artifact.filename.length >= 1 &&
      artifact.filename.length <= 255 &&
      isInteger(artifact.size_bytes, 1) &&
      typeof artifact.sha256 === "string" &&
      sha256.test(artifact.sha256),
  );
}

function uniqueSamples(values: unknown[]): boolean {
  const itemIds = new Set<string>();
  for (const value of values) {
    const sample = record(value);
    if (
      !sample ||
      typeof sample.id !== "string" ||
      typeof sample.test_set_item_id !== "string" ||
      itemIds.has(sample.test_set_item_id) ||
      !isInteger(sample.output_size_bytes, 1) ||
      typeof sample.output_sha256 !== "string" ||
      !sha256.test(sample.output_sha256) ||
      !isInteger(sample.output_sample_rate_hz, 1) ||
      !isInteger(sample.output_channels, 1) ||
      typeof sample.output_duration_seconds !== "number" ||
      !Number.isFinite(sample.output_duration_seconds) ||
      sample.output_duration_seconds <= 0 ||
      !isTimestamp(sample.created_at)
    ) {
      return false;
    }
    itemIds.add(sample.test_set_item_id);
  }
  return true;
}

function isModel(value: Record<string, unknown>): boolean {
  return (
    (value.version === "v1" || value.version === "v2") &&
    (value.sample_rate === "40k" || value.sample_rate === "48k") &&
    typeof value.use_f0 === "boolean"
  );
}

function isTraining(value: Record<string, unknown>): boolean {
  return (
    isInteger(value.epochs, 1) &&
    isInteger(value.batch_size_per_gpu, 1) &&
    Array.isArray(value.gpu_ids) &&
    value.gpu_ids.every((gpu) => isInteger(gpu, 0))
  );
}

function isF0(value: Record<string, unknown>): boolean {
  return ["pm", "harvest", "dio", "rmvpe", "rmvpe_gpu", null].includes(
    value.training_f0_method as string | null,
  );
}

function isIndex(value: Record<string, unknown>): boolean {
  return typeof value.build_index === "boolean";
}

function metricRank(key: string): number {
  const preferred = [
    "loss_g_total",
    "loss_d_total",
    "loss_mel",
    "loss_kl",
    "loss_fm",
    "learning_rate",
    "grad_norm_g",
    "grad_norm_d",
    "current_epoch",
    "total_epoch",
    "step",
  ];
  const index = preferred.indexOf(key);
  return index === -1 ? preferred.length : index;
}

function record(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function isTimestamp(value: unknown): value is string {
  return typeof value === "string" && Number.isFinite(Date.parse(value));
}

function isInteger(value: unknown, minimum: number): value is number {
  return Number.isSafeInteger(value) && (value as number) >= minimum;
}

function isNullableInteger(value: unknown, minimum: number): boolean {
  return value === null || isInteger(value, minimum);
}
