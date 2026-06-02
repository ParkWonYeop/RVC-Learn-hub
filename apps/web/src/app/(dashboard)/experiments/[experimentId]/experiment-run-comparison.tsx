"use client";

import Link from "next/link";
import { useEffect, useMemo, useRef, useState } from "react";
import { EngineModeBadge, FakeEngineResultWarning } from "@/components/engine-mode-badge";
import type {
  ExperimentComparisonArtifact,
  ExperimentComparisonJob,
  ExperimentComparisonMetricSeries,
  ExperimentComparisonResponse,
} from "@/lib/api-types";
import {
  comparisonErrorMessage,
  comparisonMetricKeys,
  defaultComparisonJobIds,
  ExperimentComparisonReadError,
  fetchExperimentComparison,
  formatAttemptDuration,
  formatComparisonBytes,
  metricPolyline,
  selectedMetricSeries,
  type ComparisonSelectableJob,
} from "@/lib/client/experiment-comparison";
import { metricDisplayName, metricDisplayValue } from "@/lib/client/metric-presentation";
import {
  registryCandidateSource,
  type RegistryCandidateSource,
} from "@/lib/client/model-registry";
import type { JobStatus } from "@/lib/types";

type ComparisonState =
  | { status: "idle" | "loading"; data: null; errorStatus: null }
  | { status: "ready"; data: ExperimentComparisonResponse; errorStatus: null }
  | { status: "error"; data: null; errorStatus: number };

const jobStatusLabels: Record<JobStatus, string> = {
  queued: "대기",
  assigned: "배정",
  downloading_dataset: "Dataset 수신",
  validating_dataset: "Dataset 검증",
  preparing_flat_dataset: "Dataset 준비",
  preprocessing: "전처리",
  extracting_f0: "F0 추출",
  extracting_features: "Feature 추출",
  training: "학습",
  saving_checkpoint: "Checkpoint 저장",
  building_index: "Index 생성",
  collecting_small_model: "Small model 수집",
  generating_samples: "Sample 생성",
  evaluating: "평가",
  uploading_artifacts: "Artifact 업로드",
  completed: "완료",
  failed: "실패",
  cancelled: "취소",
  retrying: "재시도",
};

const chartColors = [
  "#178f83",
  "#5278ff",
  "#e07a45",
  "#8a5fd1",
  "#b94d67",
  "#4083a6",
  "#677b2b",
  "#b06d17",
  "#3f665f",
  "#7a5b83",
  "#b34d35",
  "#5074ad",
  "#55723c",
  "#935d2d",
  "#624e9d",
  "#8d5268",
] as const;

export function ExperimentRunComparison({
  experimentId,
  jobs,
  onRegisterCandidate,
  registeredModelArtifactIds = [],
  registryLocked = false,
}: {
  experimentId: string;
  jobs: ComparisonSelectableJob[];
  onRegisterCandidate?: (source: RegistryCandidateSource) => void;
  registeredModelArtifactIds?: readonly string[];
  registryLocked?: boolean;
}) {
  const availableJobs = useMemo(() => uniqueJobs(jobs), [jobs]);
  const [selectedJobIds, setSelectedJobIds] = useState(() =>
    defaultComparisonJobIds(availableJobs),
  );
  const [state, setState] = useState<ComparisonState>(() =>
    selectedJobIds.length >= 2
      ? { status: "loading", data: null, errorStatus: null }
      : { status: "idle", data: null, errorStatus: null },
  );
  const [requestRevision, setRequestRevision] = useState(0);
  const requestGeneration = useRef(0);
  useEffect(() => {
    if (selectedJobIds.length < 2) return;
    const controller = new AbortController();
    const generation = ++requestGeneration.current;
    void fetchExperimentComparison(experimentId, selectedJobIds, controller.signal)
      .then((data) => {
        if (controller.signal.aborted || requestGeneration.current !== generation) return;
        setState({ status: "ready", data, errorStatus: null });
      })
      .catch((error: unknown) => {
        if (
          isAbortError(error) ||
          controller.signal.aborted ||
          requestGeneration.current !== generation
        ) {
          return;
        }
        setState({
          status: "error",
          data: null,
          errorStatus: error instanceof ExperimentComparisonReadError ? error.status : 502,
        });
      });
    return () => controller.abort();
  }, [experimentId, requestRevision, selectedJobIds]);

  function toggleJob(jobId: string) {
    requestGeneration.current += 1;
    const selected = new Set(selectedJobIds);
    if (selected.has(jobId)) selected.delete(jobId);
    else if (selected.size < 16) selected.add(jobId);
    const next = availableJobs
      .filter((job) => selected.has(job.id))
      .map((job) => job.id)
      .slice(0, 16);
    setSelectedJobIds(next);
    setState(
      next.length >= 2
        ? { status: "loading", data: null, errorStatus: null }
        : { status: "idle", data: null, errorStatus: null },
    );
  }

  function retry() {
    requestGeneration.current += 1;
    setState({ status: "loading", data: null, errorStatus: null });
    setRequestRevision((current) => current + 1);
  }

  return (
    <section
      aria-labelledby="experiment-run-comparison-heading"
      className="experiment-run-comparison-section"
    >
      <div className="section-heading">
        <div>
          <p className="panel-kicker">RUN ANALYSIS · CURRENT ATTEMPT</p>
          <h2 id="experiment-run-comparison-heading">학습 Run 비교</h2>
        </div>
        <span>동일 Experiment의 immutable 설정·지표·검증된 결과물</span>
      </div>
      <div className="panel experiment-run-comparison-panel">
        <JobSelection
          jobs={availableJobs}
          selectedJobIds={selectedJobIds}
          onToggle={toggleJob}
        />
        {availableJobs.length < 2 ? (
          <ComparisonNotice kind="empty">
            비교 가능한 Job이 두 개 이상 생기면 설정과 Metric을 나란히 확인할 수 있습니다.
          </ComparisonNotice>
        ) : selectedJobIds.length < 2 ? (
          <ComparisonNotice kind="empty">
            서로 다른 Job을 두 개 이상 선택해 주세요.
          </ComparisonNotice>
        ) : state.status === "loading" ? (
          <ComparisonNotice kind="loading">
            선택한 {selectedJobIds.length}개 Job의 current-attempt 원장을 검증하는 중입니다.
          </ComparisonNotice>
        ) : state.status === "error" ? (
          <ComparisonError status={state.errorStatus} onRetry={retry} />
        ) : state.status === "ready" ? (
          <ExperimentComparisonResult
            data={state.data}
            onRegisterCandidate={onRegisterCandidate}
            registeredModelArtifactIds={registeredModelArtifactIds}
            registryLocked={registryLocked}
          />
        ) : null}
      </div>
    </section>
  );
}

function JobSelection({
  jobs,
  selectedJobIds,
  onToggle,
}: {
  jobs: ComparisonSelectableJob[];
  selectedJobIds: string[];
  onToggle: (jobId: string) => void;
}) {
  const selected = new Set(selectedJobIds);
  return (
    <fieldset
      aria-describedby="experiment-comparison-selection-help experiment-comparison-selection-status"
      className="experiment-comparison-selector"
    >
      <legend>비교할 Job 선택</legend>
      <p id="experiment-comparison-selection-help">
        2~16개를 선택할 수 있습니다. 화면 진입 시 목록 상단의 최대 4개를 자동 선택합니다.
      </p>
      <div className="experiment-comparison-job-options">
        {jobs.map((job) => {
          const checked = selected.has(job.id);
          return (
            <label key={job.id}>
              <input
                checked={checked}
                disabled={!checked && selected.size >= 16}
                onChange={() => onToggle(job.id)}
                type="checkbox"
              />
              <span>
                <strong>{job.name}</strong>
                <small>{jobStatusLabels[job.status]}</small>
              </span>
            </label>
          );
        })}
      </div>
      <output id="experiment-comparison-selection-status" aria-live="polite">
        {selected.size}개 선택 · 최소 2개 / 최대 16개
      </output>
    </fieldset>
  );
}

function ComparisonError({ status, onRetry }: { status: number; onRetry: () => void }) {
  const retryable = ![401, 403, 422].includes(status);
  return (
    <div className="experiment-comparison-error" role="alert">
      <strong>비교 요청 실패</strong>
      {status === 401 ? (
        <Link href="/session/expired">{comparisonErrorMessage(status)}</Link>
      ) : (
        <span>{comparisonErrorMessage(status)}</span>
      )}
      {retryable ? (
        <button className="button button-secondary" onClick={onRetry} type="button">
          다시 검증
        </button>
      ) : null}
    </div>
  );
}

function ComparisonNotice({
  children,
  kind,
}: {
  children: React.ReactNode;
  kind: "empty" | "loading";
}) {
  return (
    <div className={`experiment-comparison-notice experiment-comparison-notice-${kind}`} role="status">
      {children}
    </div>
  );
}

export function ExperimentComparisonResult({
  data,
  onRegisterCandidate,
  registeredModelArtifactIds = [],
  registryLocked = false,
}: {
  data: ExperimentComparisonResponse;
  onRegisterCandidate?: (source: RegistryCandidateSource) => void;
  registeredModelArtifactIds?: readonly string[];
  registryLocked?: boolean;
}) {
  const fakeJobs = data.jobs.filter((job) => job.current_attempt?.engine_mode === "fake");
  return (
    <div className="experiment-comparison-result">
      <div className="experiment-comparison-boundary" role="note">
        <strong>판정 경계</strong>
        <span>
          이 화면은 best model을 자동 선정하지 않습니다. 검증된 native 결과만 아래 Model
          Registry에서 사람이 명시적으로 후보 등록·승인할 수 있습니다.
        </span>
      </div>
      {fakeJobs.map((job) => (
        <div className="experiment-comparison-fake-warning" key={job.id}>
          <strong>{job.job_name}</strong>
          <FakeEngineResultWarning mode="fake" />
        </div>
      ))}
      <ConfigurationTable jobs={data.jobs} />
      <RunSummaryTable jobs={data.jobs} />
      <MetricComparison jobs={data.jobs} pointLimit={data.metric_point_limit_per_key} />
      <AvailabilityTable
        experimentId={data.experiment.id}
        jobs={data.jobs}
        onRegisterCandidate={onRegisterCandidate}
        registeredModelArtifactIds={registeredModelArtifactIds}
        registryLocked={registryLocked}
      />
    </div>
  );
}

function ConfigurationTable({ jobs }: { jobs: ExperimentComparisonJob[] }) {
  const rows: Array<{ label: string; value: (job: ExperimentComparisonJob) => React.ReactNode }> = [
    { label: "RVC version", value: (job) => job.config.model.version },
    { label: "Sample rate", value: (job) => job.config.model.sample_rate },
    {
      label: "F0 사용",
      value: (job) => (job.config.model.use_f0 ? "사용" : "미사용"),
    },
    {
      label: "Training F0 method",
      value: (job) => job.config.f0_extraction.training_f0_method ?? "해당 없음",
    },
    { label: "Epochs", value: (job) => job.config.training.epochs.toLocaleString("ko-KR") },
    {
      label: "Batch / GPU",
      value: (job) => job.config.training.batch_size_per_gpu.toLocaleString("ko-KR"),
    },
    {
      label: "GPU IDs",
      value: (job) =>
        job.config.training.gpu_ids.length > 0 ? job.config.training.gpu_ids.join(", ") : "없음",
    },
    {
      label: "Index 생성",
      value: (job) => (job.config.index.build_index ? "생성" : "미생성"),
    },
  ];
  return (
    <ComparisonTableSection heading="Immutable 학습 설정" id="experiment-config-comparison-heading">
      <div className="table-wrap experiment-comparison-table-wrap">
        <table>
          <caption>선택한 Job의 배정 후 변경되지 않는 RVC 학습 설정 비교</caption>
          <ComparisonTableHead jobs={jobs} firstColumn="설정" />
          <tbody>
            {rows.map((row) => (
              <tr key={row.label}>
                <th scope="row">{row.label}</th>
                {jobs.map((job) => <td key={job.id}>{row.value(job)}</td>)}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </ComparisonTableSection>
  );
}

function RunSummaryTable({ jobs }: { jobs: ExperimentComparisonJob[] }) {
  return (
    <ComparisonTableSection heading="현재 실행과 학습 시간" id="experiment-run-summary-heading">
      <div className="table-wrap experiment-comparison-table-wrap">
        <table>
          <caption>Job 상태와 exact current attempt에서 읽은 실행 엔진 및 학습 시간</caption>
          <ComparisonTableHead jobs={jobs} firstColumn="관측값" />
          <tbody>
            <SummaryRow label="Job 상태" jobs={jobs} value={(job) => jobStatusLabels[job.status]} />
            <SummaryRow
              label="Current epoch"
              jobs={jobs}
              value={(job) => `${job.current_epoch ?? "—"} / ${job.total_epoch}`}
            />
            <SummaryRow
              label="Current attempt"
              jobs={jobs}
              value={(job) =>
                job.current_attempt
                  ? `#${job.current_attempt.attempt_number} · ${jobStatusLabels[job.current_attempt.status]}`
                  : "실행 전"
              }
            />
            <SummaryRow
              label="실행 엔진"
              jobs={jobs}
              value={(job) => <EngineModeBadge mode={job.current_attempt?.engine_mode ?? null} />}
            />
            <SummaryRow
              label="학습 시간"
              jobs={jobs}
              value={(job) =>
                job.current_attempt
                  ? formatAttemptDuration(
                      job.current_attempt.started_at,
                      job.current_attempt.finished_at,
                    )
                  : "—"
              }
            />
            <SummaryRow
              label="시작"
              jobs={jobs}
              value={(job) => formatTimestamp(job.current_attempt?.started_at ?? null)}
            />
            <SummaryRow
              label="종료"
              jobs={jobs}
              value={(job) => formatTimestamp(job.current_attempt?.finished_at ?? null)}
            />
          </tbody>
        </table>
      </div>
    </ComparisonTableSection>
  );
}

function MetricComparison({ jobs, pointLimit }: { jobs: ExperimentComparisonJob[]; pointLimit: 200 }) {
  const metricKeys = useMemo(() => comparisonMetricKeys(jobs), [jobs]);
  const [selectedKey, setSelectedKey] = useState("");
  const effectiveKey = metricKeys.includes(selectedKey)
    ? selectedKey
    : metricKeys.includes("loss_g_total")
      ? "loss_g_total"
      : (metricKeys[0] ?? "");
  if (metricKeys.length === 0) {
    return (
      <ComparisonTableSection heading="Metric 비교" id="experiment-metric-comparison-heading">
        <ComparisonNotice kind="empty">
          선택한 current attempt에 비교 가능한 Metric이 아직 없습니다.
        </ComparisonNotice>
      </ComparisonTableSection>
    );
  }
  const compared = selectedMetricSeries(jobs, effectiveKey);
  const allValues = compared.flatMap(({ series }) => series?.points.map((point) => point.value) ?? []);
  const allSequences = compared.flatMap(
    ({ series }) => series?.points.map((point) => point.sequence) ?? [],
  );
  const minimum = Math.min(...allValues);
  const maximum = Math.max(...allValues);
  const minimumSequence = Math.min(...allSequences);
  const maximumSequence = Math.max(...allSequences);
  const truncated = compared.filter(({ series }) => series?.truncated);
  return (
    <ComparisonTableSection heading="Metric 비교" id="experiment-metric-comparison-heading">
      <div className="experiment-comparison-metric-controls">
        <label htmlFor="experiment-comparison-metric-key">Metric</label>
        <select
          id="experiment-comparison-metric-key"
          onChange={(event) => setSelectedKey(event.target.value)}
          value={effectiveKey}
        >
          {metricKeys.map((key) => (
            <option key={key} value={key}>{metricDisplayName(key)}</option>
          ))}
        </select>
        <span>Job·key마다 최근 최대 {pointLimit}개 · sequence 오름차순</span>
      </div>
      {truncated.length > 0 ? (
        <div className="experiment-comparison-truncation" role="status">
          <strong>이전 Metric 생략됨</strong>
          <ul>
            {truncated.map(({ job, series }) => (
              <li key={job.id}>
                {job.job_name}: 전체 {series?.total_points.toLocaleString("ko-KR")}개 중 최근 {series?.points.length.toLocaleString("ko-KR")}개
              </li>
            ))}
          </ul>
        </div>
      ) : null}
      <MetricOverlayChart
        compared={compared}
        keyName={effectiveKey}
        maximum={maximum}
        maximumSequence={maximumSequence}
        minimum={minimum}
        minimumSequence={minimumSequence}
      />
      <MetricComparisonTable compared={compared} keyName={effectiveKey} pointLimit={pointLimit} />
    </ComparisonTableSection>
  );
}

function MetricOverlayChart({
  compared,
  keyName,
  maximum,
  maximumSequence,
  minimum,
  minimumSequence,
}: {
  compared: Array<{ job: ExperimentComparisonJob; series: ExperimentComparisonMetricSeries | null }>;
  keyName: string;
  maximum: number;
  maximumSequence: number;
  minimum: number;
  minimumSequence: number;
}) {
  return (
    <figure className="experiment-comparison-chart">
      <figcaption>
        <strong>{metricDisplayName(keyName)}</strong>
        <span>
          min {metricDisplayValue(keyName, minimum)} · max {metricDisplayValue(keyName, maximum)}
        </span>
      </figcaption>
      <svg
        aria-label={`${metricDisplayName(keyName)}를 Job별 sequence 오름차순으로 겹쳐 그린 그래프`}
        role="img"
        viewBox="0 0 760 220"
      >
        <title>{`${metricDisplayName(keyName)} Run 비교`}</title>
        <line x1="18" x2="742" y1="110" y2="110" />
        {compared.map(({ job, series }, index) =>
          series ? (
            <polyline
              key={job.id}
              points={metricPolyline(
                series.points,
                minimum,
                maximum,
                minimumSequence,
                maximumSequence,
              )}
              stroke={chartColors[index % chartColors.length]}
            />
          ) : null,
        )}
      </svg>
      <ul aria-label="그래프 범례" className="experiment-comparison-chart-legend">
        {compared.map(({ job, series }, index) => (
          <li key={job.id}>
            <span aria-hidden="true" style={{ background: chartColors[index % chartColors.length] }} />
            <strong>{job.job_name}</strong>
            <small>{series ? `${series.points.length}개` : "Metric 없음"}</small>
          </li>
        ))}
      </ul>
    </figure>
  );
}

function MetricComparisonTable({
  compared,
  keyName,
  pointLimit,
}: {
  compared: Array<{ job: ExperimentComparisonJob; series: ExperimentComparisonMetricSeries | null }>;
  keyName: string;
  pointLimit: 200;
}) {
  return (
    <details className="experiment-comparison-metric-ledger">
      <summary>Metric 원장 표 열기</summary>
      <div className="table-wrap experiment-comparison-metric-table-wrap">
        <table>
          <caption>
            {metricDisplayName(keyName)} · Job별 sequence 오름차순 · Job당 최대 {pointLimit}개
          </caption>
          <thead>
            <tr>
              <th scope="col">Job</th>
              <th scope="col">Sequence</th>
              <th scope="col">Epoch</th>
              <th scope="col">Step</th>
              <th scope="col">Value</th>
              <th scope="col">관측 시각</th>
            </tr>
          </thead>
          {compared.map(({ job, series }) => (
            <tbody key={job.id}>
              {series ? series.points.map((point, index) => (
                <tr key={point.sequence}>
                  {index === 0 ? <th rowSpan={series.points.length} scope="rowgroup">{job.job_name}</th> : null}
                  <td>{point.sequence}</td>
                  <td>{point.epoch ?? "—"}</td>
                  <td>{point.step ?? "—"}</td>
                  <td className="metric-value">{metricDisplayValue(keyName, point.value)}</td>
                  <td><time dateTime={point.occurred_at}>{formatTimestamp(point.occurred_at)}</time></td>
                </tr>
              )) : (
                <tr>
                  <th scope="row">{job.job_name}</th>
                  <td colSpan={5}>이 Metric이 없습니다.</td>
                </tr>
              )}
            </tbody>
          ))}
        </table>
      </div>
    </details>
  );
}

function AvailabilityTable({
  experimentId,
  jobs,
  onRegisterCandidate,
  registeredModelArtifactIds,
  registryLocked,
}: {
  experimentId: string;
  jobs: ExperimentComparisonJob[];
  onRegisterCandidate?: (source: RegistryCandidateSource) => void;
  registeredModelArtifactIds: readonly string[];
  registryLocked: boolean;
}) {
  const registered = new Set(registeredModelArtifactIds);
  return (
    <ComparisonTableSection heading="검증된 결과물" id="experiment-artifact-comparison-heading">
      <div className="experiment-comparison-availability-note" role="note">
        Manager가 현재 canonical byte를 다시 검증한 model/index만 다운로드할 수 있습니다. Sample
        음성 A/B 재생은 동일 TestSet item 기준의 별도 비교 화면에서 수행합니다.
      </div>
      <div className="table-wrap experiment-comparison-table-wrap">
        <table>
          <caption>선택한 current attempt의 검증된 final model, index와 Sample 가용성</caption>
          <ComparisonTableHead jobs={jobs} firstColumn="결과물" />
          <tbody>
            <tr>
              <th scope="row">Final model</th>
              {jobs.map((job) => {
                const source = registryCandidateSource(experimentId, job);
                const modelId = job.availability.final_model?.id ?? null;
                const alreadyRegistered = modelId !== null && registered.has(modelId);
                const fake = job.current_attempt?.engine_mode === "fake";
                return (
                  <td key={job.id}>
                    <ArtifactAvailability artifact={job.availability.final_model} label="model" />
                    {fake && job.availability.final_model ? (
                      <span className="experiment-comparison-registry-blocked" role="note">
                        FAKE 결과는 Registry 후보로 등록하거나 승인할 수 없습니다.
                      </span>
                    ) : source && onRegisterCandidate ? (
                      <button
                        className="button button-primary experiment-comparison-register-button"
                        disabled={registryLocked || alreadyRegistered}
                        onClick={() => onRegisterCandidate(source)}
                        type="button"
                      >
                        {alreadyRegistered ? "Registry 등록됨" : "후보 등록 검증"}
                      </button>
                    ) : null}
                  </td>
                );
              })}
            </tr>
            <tr>
              <th scope="row">Final index</th>
              {jobs.map((job) => <td key={job.id}><ArtifactAvailability artifact={job.availability.final_index} label="index" /></td>)}
            </tr>
            <tr>
              <th scope="row">Sample</th>
              {jobs.map((job) => {
                const totalDuration = job.availability.samples.reduce(
                  (sum, sample) => sum + sample.output_duration_seconds,
                  0,
                );
                return (
                  <td key={job.id}>
                    <span className="experiment-comparison-sample-summary">
                      <strong>{job.availability.samples.length}개</strong>
                      <small>
                        {job.availability.samples.length > 0
                          ? `총 ${totalDuration.toFixed(2)}초 · current attempt 검증 완료`
                          : "검증된 Sample 없음"}
                      </small>
                    </span>
                  </td>
                );
              })}
            </tr>
          </tbody>
        </table>
      </div>
    </ComparisonTableSection>
  );
}

function ArtifactAvailability({
  artifact,
  label,
}: {
  artifact: ExperimentComparisonArtifact | null;
  label: "model" | "index";
}) {
  if (!artifact) return <span className="experiment-comparison-unavailable">검증된 파일 없음</span>;
  return (
    <span className="experiment-comparison-artifact">
      <strong>{artifact.filename}</strong>
      <small>{formatComparisonBytes(artifact.size_bytes)} · SHA-256 {shortHash(artifact.sha256)}</small>
      <a
        aria-label={`${artifact.filename} 검증된 final ${label} 다운로드`}
        className="button button-secondary"
        href={`/bff/artifacts/${encodeURIComponent(artifact.id)}/download`}
      >
        {label === "model" ? "Model" : "Index"} 다운로드
      </a>
    </span>
  );
}

function ComparisonTableSection({
  children,
  heading,
  id,
}: {
  children: React.ReactNode;
  heading: string;
  id: string;
}) {
  return (
    <section aria-labelledby={id} className="experiment-comparison-subsection">
      <h3 id={id}>{heading}</h3>
      {children}
    </section>
  );
}

function ComparisonTableHead({
  jobs,
  firstColumn,
}: {
  jobs: ExperimentComparisonJob[];
  firstColumn: string;
}) {
  return (
    <thead>
      <tr>
        <th scope="col">{firstColumn}</th>
        {jobs.map((job) => <th key={job.id} scope="col">{job.job_name}</th>)}
      </tr>
    </thead>
  );
}

function SummaryRow({
  jobs,
  label,
  value,
}: {
  jobs: ExperimentComparisonJob[];
  label: string;
  value: (job: ExperimentComparisonJob) => React.ReactNode;
}) {
  return (
    <tr>
      <th scope="row">{label}</th>
      {jobs.map((job) => <td key={job.id}>{value(job)}</td>)}
    </tr>
  );
}

function uniqueJobs(jobs: ComparisonSelectableJob[]): ComparisonSelectableJob[] {
  const ids = new Set<string>();
  return jobs.filter((job) => {
    if (ids.has(job.id)) return false;
    ids.add(job.id);
    return true;
  });
}

function formatTimestamp(value: string | null): string {
  if (!value) return "—";
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) return "—";
  return new Intl.DateTimeFormat("ko-KR", {
    dateStyle: "short",
    timeStyle: "medium",
  }).format(date);
}

function shortHash(value: string): string {
  return value.length > 18 ? `${value.slice(0, 14)}…` : value;
}

function isAbortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}
