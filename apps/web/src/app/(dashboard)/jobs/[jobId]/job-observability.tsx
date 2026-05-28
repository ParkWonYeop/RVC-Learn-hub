"use client";

import {
  useCallback,
  useEffect,
  useMemo,
  useState,
  type FormEvent,
} from "react";
import {
  artifactTypes,
  type ApiArtifact,
  type ApiArtifactList,
  type ApiArtifactType,
  type ApiJobLog,
  type ApiJobLogList,
  type ApiLogLevel,
  type ApiMetric,
  type ApiMetricList,
} from "@/lib/api-types";
import {
  metricDisplayName,
  metricDisplayValue,
} from "@/lib/client/metric-presentation";
import { startNonOverlappingPolling } from "@/lib/client/non-overlapping-poller";

type LoadStatus = "loading" | "ready" | "error";

type RemoteState<T> = {
  status: LoadStatus;
  data: T | null;
  error: BffReadError | null;
};

type MetricFilters = {
  attemptId: string;
  key: string;
  epoch: string;
  step: string;
};

const logLevels: Array<ApiLogLevel | "all"> = [
  "all",
  "debug",
  "info",
  "warning",
  "error",
];

const artifactLabels: Record<ApiArtifactType, string> = {
  final_small_model: "배포 모델",
  final_index: "최종 Index",
  total_features: "전체 Feature",
  generator_checkpoint: "Generator checkpoint",
  discriminator_checkpoint: "Discriminator checkpoint",
  train_log: "학습 로그 파일",
  tensorboard: "TensorBoard",
  sample: "샘플 음성",
  environment: "실행 환경",
  config: "설정",
  dataset_report: "Dataset 보고서",
};

const emptyMetricFilters: MetricFilters = {
  attemptId: "",
  key: "",
  epoch: "",
  step: "",
};
const metricRefreshIntervalMs = 15_000;

export function JobObservability({
  jobId,
  currentAttemptId,
  enabled,
}: {
  jobId: string;
  currentAttemptId: string | null;
  enabled: boolean;
}) {
  const encodedJobId = encodeURIComponent(jobId);
  const [logs, setLogs] = useState<RemoteState<ApiJobLogList>>(loadingState());
  const [metrics, setMetrics] = useState<RemoteState<ApiMetricList>>(loadingState());
  const [artifacts, setArtifacts] = useState<RemoteState<ApiArtifactList>>(loadingState());
  const [attemptId, setAttemptId] = useState("");
  const [knownAttempts, setKnownAttempts] = useState<Map<string, number>>(
    () => new Map(currentAttemptId ? [[currentAttemptId, 0]] : []),
  );
  const [logLevel, setLogLevel] = useState<ApiLogLevel | "all">("all");
  const [logSearch, setLogSearch] = useState("");
  const [streamStartCursor, setStreamStartCursor] = useState<string | null>(null);
  const [streamState, setStreamState] = useState<
    "disabled" | "connecting" | "open" | "reconnecting" | "error"
  >(enabled ? "connecting" : "disabled");
  const [metricDraft, setMetricDraft] = useState<MetricFilters>(emptyMetricFilters);
  const [metricFilters, setMetricFilters] = useState<MetricFilters>(emptyMetricFilters);
  const [chartKey, setChartKey] = useState("");
  const [artifactType, setArtifactType] = useState<ApiArtifactType | "all">("all");

  const loadLogs = useCallback(
    async (selectedAttempt: string, after: string | null, signal?: AbortSignal) => {
      setStreamState("connecting");
      setLogs((current) => ({ ...current, status: "loading", error: null }));
      const query = new URLSearchParams({ limit: "100" });
      if (selectedAttempt) query.set("attempt_id", selectedAttempt);
      if (after) query.set("after", after);
      else query.set("tail", "true");
      try {
        const result = await fetchList<ApiJobLogList>(
          `/bff/jobs/${encodedJobId}/logs?${query}`,
          signal,
        );
        setKnownAttempts((current) => collectAttempts(current, result.items));
        setLogs((current) => ({
          status: "ready",
          data: after && current.data ? mergeLogPages(current.data, result) : result,
          error: null,
        }));
        setStreamStartCursor(result.next_cursor);
      } catch (error) {
        if (isAbortError(error)) return;
        setStreamState("error");
        setLogs((current) => ({
          status: "error",
          data: current.data,
          error: asBffError(error),
        }));
      }
    },
    [encodedJobId],
  );

  const loadMetrics = useCallback(
    async (filters: MetricFilters, signal?: AbortSignal, background = false) => {
      if (!background) {
        setMetrics((current) => ({ ...current, status: "loading", error: null }));
      }
      const query = new URLSearchParams({ tail: "true", limit: "200" });
      if (filters.attemptId) query.set("attempt_id", filters.attemptId);
      if (filters.key) query.set("key", filters.key);
      if (filters.epoch) query.set("epoch", filters.epoch);
      if (filters.step) query.set("step", filters.step);
      try {
        const result = await fetchList<ApiMetricList>(
          `/bff/jobs/${encodedJobId}/metrics?${query}`,
          signal,
        );
        setKnownAttempts((current) => collectAttempts(current, result.items));
        setMetrics({ status: "ready", data: result, error: null });
      } catch (error) {
        if (isAbortError(error)) return;
        setMetrics((current) => ({
          status: "error",
          data: current.data,
          error: asBffError(error),
        }));
      }
    },
    [encodedJobId],
  );

  const loadArtifacts = useCallback(
    async (selectedType: ApiArtifactType | "all", signal?: AbortSignal) => {
      setArtifacts((current) => ({ ...current, status: "loading", error: null }));
      const query = new URLSearchParams({ limit: "200" });
      if (selectedType !== "all") query.set("artifact_type", selectedType);
      try {
        const result = await fetchList<ApiArtifactList>(
          `/bff/jobs/${encodedJobId}/artifacts?${query}`,
          signal,
        );
        setArtifacts({ status: "ready", data: result, error: null });
      } catch (error) {
        if (isAbortError(error)) return;
        setArtifacts((current) => ({
          status: "error",
          data: current.data,
          error: asBffError(error),
        }));
      }
    },
    [encodedJobId],
  );

  useEffect(() => {
    if (!enabled) return;
    const controller = new AbortController();
    void loadLogs(attemptId, null, controller.signal);
    return () => controller.abort();
  }, [attemptId, enabled, loadLogs]);

  useEffect(() => {
    if (!enabled) return;
    return startNonOverlappingPolling(
      (signal, background) => loadMetrics(metricFilters, signal, background),
      metricRefreshIntervalMs,
    );
  }, [enabled, loadMetrics, metricFilters]);

  useEffect(() => {
    if (!enabled) return;
    const controller = new AbortController();
    void loadArtifacts(artifactType, controller.signal);
    return () => controller.abort();
  }, [artifactType, enabled, loadArtifacts]);

  useEffect(() => {
    if (!enabled || logs.status !== "ready") return;
    const query = new URLSearchParams();
    if (attemptId) query.set("attempt_id", attemptId);
    if (streamStartCursor) query.set("after", streamStartCursor);
    const suffix = query.size > 0 ? `?${query}` : "";
    const source = new EventSource(
      `/bff/jobs/${encodedJobId}/logs/stream${suffix}`,
      { withCredentials: true },
    );
    setStreamState("connecting");
    source.onopen = () => setStreamState("open");
    source.onerror = () => setStreamState("reconnecting");
    source.addEventListener("log", (event) => {
      const parsed = parseStreamLog((event as MessageEvent<string>).data, jobId);
      if (!parsed) return;
      setKnownAttempts((current) => collectAttempts(current, [parsed]));
      setLogs((current) => appendStreamLog(current, parsed, event.lastEventId));
    });
    return () => source.close();
  }, [attemptId, enabled, encodedJobId, jobId, logs.status, streamStartCursor]);

  const visibleLogs = useMemo(() => {
    const normalizedSearch = logSearch.trim().toLocaleLowerCase("ko-KR");
    return (logs.data?.items ?? []).filter(
      (entry) =>
        (logLevel === "all" || entry.level === logLevel) &&
        (!normalizedSearch ||
          entry.message.toLocaleLowerCase("ko-KR").includes(normalizedSearch)),
    );
  }, [logLevel, logSearch, logs.data]);
  const attempts = useMemo(
    () =>
      [...knownAttempts.entries()].sort((left, right) => {
        if (left[1] === 0) return 1;
        if (right[1] === 0) return -1;
        return left[1] - right[1];
      }),
    [knownAttempts],
  );
  const metricKeys = useMemo(
    () => [...new Set((metrics.data?.items ?? []).map((metric) => metric.key))].sort(),
    [metrics.data],
  );
  const effectiveChartKey = metricKeys.includes(chartKey) ? chartKey : (metricKeys[0] ?? "");
  const chartMetrics = useMemo(
    () => (metrics.data?.items ?? []).filter((metric) => metric.key === effectiveChartKey),
    [effectiveChartKey, metrics.data],
  );

  if (!enabled) {
    return (
      <section className="telemetry-section" aria-labelledby="telemetry-heading">
        <TelemetryHeading />
        <div className="panel observability-disabled" role="status">
          Demo fixture에서는 Manager 관측 API를 호출하지 않습니다.
        </div>
      </section>
    );
  }

  function applyMetricFilters(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setMetricFilters(metricDraft);
  }

  return (
    <section className="telemetry-section" aria-labelledby="telemetry-heading">
      <TelemetryHeading />
      <div className="observability-stack">
        <article className="panel observability-panel">
          <div className="observability-panel-header">
            <div>
              <p className="panel-kicker">LIVE LOGS</p>
              <h3>학습 로그</h3>
              <span>최근 100개 tail 조회 후 cursor 기반 SSE로 이어 받습니다.</span>
            </div>
            <ConnectionBadge state={streamState} />
          </div>
          <div className="observability-filters log-filters">
            <label>
              <span>Attempt · API 필터</span>
              <select value={attemptId} onChange={(event) => setAttemptId(event.target.value)}>
                <option value="">전체 attempt</option>
                {attempts.map(([id, number]) => (
                  <option key={id} value={id}>
                    {number > 0 ? `#${number}` : "현재"} · {shortIdentifier(id)}
                  </option>
                ))}
              </select>
            </label>
            <label>
              <span>Level · 현재 조회분</span>
              <select
                value={logLevel}
                onChange={(event) => setLogLevel(event.target.value as ApiLogLevel | "all")}
              >
                {logLevels.map((level) => (
                  <option key={level} value={level}>
                    {level === "all" ? "전체 level" : level}
                  </option>
                ))}
              </select>
            </label>
            <label className="filter-grow">
              <span>메시지 검색 · 현재 조회분</span>
              <input
                type="search"
                value={logSearch}
                maxLength={200}
                onChange={(event) => setLogSearch(event.target.value)}
                placeholder="로그 메시지 검색"
              />
            </label>
            <button
              className="button button-secondary"
              disabled={logs.status === "loading"}
              onClick={() => void loadLogs(attemptId, logs.data?.next_cursor ?? null)}
              type="button"
            >
              cursor로 새 로그 확인
            </button>
          </div>
          <RemoteMessage state={logs} noun="로그" />
          {logs.data && visibleLogs.length === 0 && logs.status !== "error" ? (
            <div className="observability-empty" role="status">
              현재 조건에 맞는 로그가 없습니다.
            </div>
          ) : null}
          {visibleLogs.length > 0 ? (
            <ol className="log-console" aria-label="학습 로그 목록">
              {visibleLogs.map((entry) => (
                <li key={entry.id} className={`log-line log-line-${entry.level}`}>
                  <time dateTime={entry.occurred_at}>{formatTimestamp(entry.occurred_at)}</time>
                  <span className="log-level">{entry.level}</span>
                  <code>#{entry.attempt_number}:{entry.sequence}</code>
                  <p>{entry.message}</p>
                </li>
              ))}
            </ol>
          ) : null}
          {logs.data ? (
            <div className="observability-footnote">
              <span>
                조회 {logs.data.items.length}개 · 조건 전체 {logs.data.total}개
              </span>
              <span>
                cursor {logs.data.next_cursor ? "활성" : "없음"} · tail 범위 밖 로그 {logs.data.has_more ? "있음" : "없음"}
              </span>
            </div>
          ) : null}
        </article>

        <article className="panel observability-panel">
          <div className="observability-panel-header">
            <div>
              <p className="panel-kicker">METRICS</p>
              <h3>학습·GPU 시스템 메트릭</h3>
              <span>최신 200개를 15초마다 갱신해 Loss/Epoch와 GPU·VRAM·온도·남은 디스크를 표시합니다.</span>
            </div>
            <span className="read-state-badge">실제 API</span>
          </div>
          <form className="observability-filters metric-filters" onSubmit={applyMetricFilters}>
            <label>
              <span>Attempt</span>
              <select
                value={metricDraft.attemptId}
                onChange={(event) =>
                  setMetricDraft((current) => ({ ...current, attemptId: event.target.value }))
                }
              >
                <option value="">전체 attempt</option>
                {attempts.map(([id, number]) => (
                  <option key={id} value={id}>
                    {number > 0 ? `#${number}` : "현재"} · {shortIdentifier(id)}
                  </option>
                ))}
              </select>
            </label>
            <label className="filter-grow">
              <span>Metric key</span>
              <input
                value={metricDraft.key}
                maxLength={128}
                pattern="[A-Za-z0-9_.-]+"
                onChange={(event) =>
                  setMetricDraft((current) => ({ ...current, key: event.target.value }))
                }
                placeholder="예: train.loss"
                list="metric-key-options"
              />
              <datalist id="metric-key-options">
                {metricKeys.map((key) => (
                  <option key={key} value={key}>{metricDisplayName(key)}</option>
                ))}
              </datalist>
            </label>
            <label>
              <span>Epoch</span>
              <input
                type="number"
                min="0"
                value={metricDraft.epoch}
                onChange={(event) =>
                  setMetricDraft((current) => ({ ...current, epoch: event.target.value }))
                }
              />
            </label>
            <label>
              <span>Step</span>
              <input
                type="number"
                min="0"
                value={metricDraft.step}
                onChange={(event) =>
                  setMetricDraft((current) => ({ ...current, step: event.target.value }))
                }
              />
            </label>
            <button className="button button-primary" type="submit">
              필터 적용
            </button>
          </form>
          <RemoteMessage state={metrics} noun="메트릭" />
          {metrics.data?.items.length === 0 && metrics.status !== "error" ? (
            <div className="observability-empty" role="status">
              현재 조건에 맞는 메트릭이 없습니다.
            </div>
          ) : null}
          {metrics.data && metrics.data.items.length > 0 ? (
            <>
              <div className="metric-visual-row">
                <label>
                  <span>그래프 key</span>
                  <select value={effectiveChartKey} onChange={(event) => setChartKey(event.target.value)}>
                    {metricKeys.map((key) => (
                      <option key={key} value={key}>
                        {metricDisplayName(key)} · {key}
                      </option>
                    ))}
                  </select>
                </label>
                <MetricChart metrics={chartMetrics} metricKey={effectiveChartKey} />
              </div>
              <div className="table-wrap observability-table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>Attempt / Seq</th>
                      <th>Key</th>
                      <th>Value</th>
                      <th>Epoch</th>
                      <th>Step</th>
                      <th>시각</th>
                    </tr>
                  </thead>
                  <tbody>
                    {metrics.data.items.map((metric) => (
                      <tr key={metric.id}>
                        <td className="mono-cell">#{metric.attempt_number}:{metric.sequence}</td>
                        <td className="detail-mono" title={metric.key}>
                          {metricDisplayName(metric.key)}
                        </td>
                        <td className="metric-value">
                          {metricDisplayValue(metric.key, metric.value)}
                        </td>
                        <td>{nullableNumber(metric.epoch)}</td>
                        <td>{nullableNumber(metric.step)}</td>
                        <td><time dateTime={metric.occurred_at}>{formatTimestamp(metric.occurred_at)}</time></td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <div className="observability-footnote">
                최신 조회 {metrics.data.items.length}개 · 조건 전체 {metrics.data.total}개 · 시작 offset {metrics.data.offset}
              </div>
            </>
          ) : null}
        </article>

        <article className="panel observability-panel">
          <div className="observability-panel-header">
            <div>
              <p className="panel-kicker">ARTIFACTS</p>
              <h3>검증된 산출물</h3>
              <span>Manager가 공개한 metadata만 표시하며 storage URI는 전달하지 않습니다.</span>
            </div>
            <span className="read-state-badge">만료 다운로드</span>
          </div>
          <div className="observability-filters artifact-filters">
            <label>
              <span>Artifact type · API 필터</span>
              <select
                value={artifactType}
                onChange={(event) => setArtifactType(event.target.value as ApiArtifactType | "all")}
              >
                <option value="all">전체 유형</option>
                {artifactTypes.map((type) => (
                  <option key={type} value={type}>
                    {artifactLabels[type]}
                  </option>
                ))}
              </select>
            </label>
          </div>
          <RemoteMessage state={artifacts} noun="산출물" />
          {artifacts.data?.items.length === 0 && artifacts.status !== "error" ? (
            <div className="observability-empty" role="status">
              현재 조건에 맞는 검증된 산출물이 없습니다.
            </div>
          ) : null}
          {artifacts.data && artifacts.data.items.length > 0 ? (
            <div className="artifact-list">
              {artifacts.data.items.map((artifact) => (
                <ArtifactRow key={artifact.id} artifact={artifact} />
              ))}
            </div>
          ) : null}
          {artifacts.data ? (
            <div className="observability-footnote">
              조회 {artifacts.data.items.length}개 · 조건 전체 {artifacts.data.total}개
            </div>
          ) : null}
        </article>
      </div>
    </section>
  );
}

function TelemetryHeading() {
  return (
    <div className="section-heading">
      <div>
        <p className="panel-kicker">OBSERVABILITY</p>
        <h2 id="telemetry-heading">로그 · 메트릭 · 산출물</h2>
      </div>
      <span>HttpOnly 세션을 사용하는 same-origin BFF</span>
    </div>
  );
}

function ConnectionBadge({
  state,
}: {
  state: "disabled" | "connecting" | "open" | "reconnecting" | "error";
}) {
  const labels = {
    disabled: "연결 안 함",
    connecting: "연결 중",
    open: "SSE 연결됨",
    reconnecting: "재연결 중",
    error: "연결 불가",
  };
  return <span className={`connection-badge connection-${state}`}>{labels[state]}</span>;
}

function RemoteMessage<T>({ state, noun }: { state: RemoteState<T>; noun: string }) {
  if (state.status === "loading" && state.data === null) {
    return <div className="observability-loading" role="status">{noun}을 불러오는 중입니다.</div>;
  }
  if (state.status !== "error" || !state.error) return null;
  return (
    <div className="observability-error" role="alert">
      <strong>{readErrorTitle(state.error.status)}</strong>
      <span>{readErrorDescription(state.error.status, noun)}</span>
      {state.error.status === 401 ? <a href="/session/expired">로그인 화면으로 이동</a> : null}
    </div>
  );
}

function MetricChart({ metrics, metricKey }: { metrics: ApiMetric[]; metricKey: string }) {
  if (metrics.length === 0) return <div className="metric-chart-empty">그래프 데이터 없음</div>;
  const values = metrics.map((metric) => metric.value);
  const minimum = Math.min(...values);
  const maximum = Math.max(...values);
  const width = 720;
  const height = 170;
  const padding = 20;
  const usableWidth = width - padding * 2;
  const usableHeight = height - padding * 2;
  const points = metrics
    .map((metric, index) => {
      const x =
        metrics.length === 1 ? width / 2 : padding + (index / (metrics.length - 1)) * usableWidth;
      const ratio = maximum === minimum ? 0.5 : (metric.value - minimum) / (maximum - minimum);
      const y = padding + (1 - ratio) * usableHeight;
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(" ");
  return (
    <figure className="metric-chart">
      <figcaption>
        <strong>{metricDisplayName(metricKey)}</strong>
        <span>
          min {metricDisplayValue(metricKey, minimum)} · max {metricDisplayValue(metricKey, maximum)}
        </span>
      </figcaption>
      <svg
        viewBox={`0 0 ${width} ${height}`}
        role="img"
        aria-label={`${metricDisplayName(metricKey)} 메트릭 ${metrics.length}개를 sequence 순서로 그린 그래프`}
      >
        <line x1={padding} x2={width - padding} y1={height / 2} y2={height / 2} />
        <polyline points={points} />
      </svg>
    </figure>
  );
}

function ArtifactRow({ artifact }: { artifact: ApiArtifact }) {
  return (
    <article className="artifact-row">
      <div className="artifact-row-main">
        <span>{artifactLabels[artifact.artifact_type]}</span>
        <strong>{artifact.filename}</strong>
        <small>{artifact.mime_type ?? "MIME 미제공"} · {formatTimestamp(artifact.created_at)}</small>
      </div>
      <dl>
        <div>
          <dt>크기</dt>
          <dd>{formatBytes(artifact.size_bytes)} ({artifact.size_bytes.toLocaleString("ko-KR")} bytes)</dd>
        </div>
        <div>
          <dt>SHA-256</dt>
          <dd><code>{artifact.sha256}</code></dd>
        </div>
        <div>
          <dt>Attempt ID</dt>
          <dd><code>{artifact.attempt_id}</code></dd>
        </div>
      </dl>
      <a
        className="button button-secondary artifact-download"
        href={`/bff/artifacts/${encodeURIComponent(artifact.id)}/download`}
      >
        만료 링크 생성 · 다운로드
      </a>
    </article>
  );
}

class BffReadError extends Error {
  constructor(readonly status: number) {
    super(`BFF request failed with status ${status}`);
  }
}

async function fetchList<T>(url: string, signal?: AbortSignal): Promise<T> {
  const response = await fetch(url, {
    cache: "no-store",
    credentials: "same-origin",
    headers: { Accept: "application/json" },
    signal,
  });
  if (!response.ok) throw new BffReadError(response.status);
  const payload: unknown = await response.json();
  if (typeof payload !== "object" || payload === null || !("items" in payload) || !Array.isArray(payload.items)) {
    throw new BffReadError(502);
  }
  return payload as T;
}

function loadingState<T>(): RemoteState<T> {
  return { status: "loading", data: null, error: null };
}

function collectAttempts<T extends { attempt_id: string; attempt_number: number }>(
  current: Map<string, number>,
  items: T[],
): Map<string, number> {
  const next = new Map(current);
  for (const item of items) next.set(item.attempt_id, item.attempt_number);
  return next;
}

function mergeLogPages(current: ApiJobLogList, incoming: ApiJobLogList): ApiJobLogList {
  const byId = new Map(current.items.map((item) => [item.id, item]));
  for (const item of incoming.items) byId.set(item.id, item);
  return {
    ...incoming,
    items: [...byId.values()].slice(-500),
    total: Math.max(current.total, incoming.total),
  };
}

function appendStreamLog(
  current: RemoteState<ApiJobLogList>,
  entry: ApiJobLog,
  cursor: string,
): RemoteState<ApiJobLogList> {
  const data = current.data ?? {
    items: [],
    total: 0,
    limit: 100,
    has_more: false,
    next_cursor: null,
  };
  if (data.items.some((item) => item.id === entry.id)) return current;
  return {
    status: "ready",
    error: null,
    data: {
      ...data,
      items: [...data.items, entry].slice(-500),
      total: data.total + 1,
      next_cursor: cursor || data.next_cursor,
    },
  };
}

function parseStreamLog(value: string, expectedJobId: string): ApiJobLog | null {
  try {
    const parsed: unknown = JSON.parse(value);
    if (typeof parsed !== "object" || parsed === null) return null;
    if (!("id" in parsed) || typeof parsed.id !== "string") return null;
    if (!("job_id" in parsed) || parsed.job_id !== expectedJobId) return null;
    if (!("attempt_id" in parsed) || typeof parsed.attempt_id !== "string") return null;
    if (!("attempt_number" in parsed) || typeof parsed.attempt_number !== "number" || !Number.isInteger(parsed.attempt_number) || parsed.attempt_number < 1) return null;
    if (!("sequence" in parsed) || typeof parsed.sequence !== "number" || !Number.isInteger(parsed.sequence) || parsed.sequence < 0) return null;
    if (!("message" in parsed) || typeof parsed.message !== "string") return null;
    if (!("level" in parsed) || !["debug", "info", "warning", "error"].includes(String(parsed.level))) return null;
    if (!("occurred_at" in parsed) || typeof parsed.occurred_at !== "string") return null;
    if (!("fields" in parsed) || typeof parsed.fields !== "object" || parsed.fields === null || Array.isArray(parsed.fields)) return null;
    return parsed as ApiJobLog;
  } catch {
    return null;
  }
}

function asBffError(error: unknown): BffReadError {
  return error instanceof BffReadError ? error : new BffReadError(502);
}

function isAbortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}

function readErrorTitle(status: number): string {
  if (status === 401) return "세션이 만료되었습니다";
  if (status === 403) return "조회 권한이 없습니다";
  if (status === 404) return "작업을 찾을 수 없습니다";
  if (status === 422 || status === 400) return "필터가 올바르지 않습니다";
  return "관측 데이터를 불러오지 못했습니다";
}

function readErrorDescription(status: number, noun: string): string {
  if (status === 401) return `${noun}을 다시 보려면 로그인해 주세요.`;
  if (status === 403) return `${noun} 조회가 현재 계정에 허용되지 않았습니다.`;
  if (status === 404) return "작업이 삭제되었거나 접근 범위에 포함되지 않습니다.";
  if (status === 422 || status === 400) return "입력한 조건을 확인하고 다시 시도해 주세요.";
  return "Manager 연결 상태를 확인한 뒤 다시 시도해 주세요.";
}

function formatTimestamp(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("ko-KR", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    timeZone: "Asia/Seoul",
  }).format(date);
}

function nullableNumber(value: number | null): string {
  return value === null ? "—" : String(value);
}

function formatBytes(value: number): string {
  if (value < 1_024) return `${value} B`;
  if (value < 1_024 ** 2) return `${(value / 1_024).toFixed(1)} KiB`;
  if (value < 1_024 ** 3) return `${(value / 1_024 ** 2).toFixed(1)} MiB`;
  return `${(value / 1_024 ** 3).toFixed(2)} GiB`;
}

function shortIdentifier(value: string): string {
  return value.length > 16 ? `${value.slice(0, 12)}…` : value;
}
