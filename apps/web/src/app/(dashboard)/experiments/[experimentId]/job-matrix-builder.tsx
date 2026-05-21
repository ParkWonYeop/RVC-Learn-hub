"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { FormEvent, useMemo, useState } from "react";
import type {
  ApiCreatedJob,
  ApiExperimentJobNameList,
} from "@/lib/api-types";
import {
  buildJobMatrix,
  MAX_JOB_MATRIX_SIZE,
  normalizeJobPrefix,
  parseGpuIds,
  parseWorkerTags,
  type RvcVersion,
  type SampleRate,
  type TrainingF0Method,
} from "@/lib/client/job-matrix";
import {
  blockUnsettledSubmissionOutcomes,
  initializeSubmissionOutcomes,
  recoverSubmissionTransportFailure,
  withSubmissionOutcome,
  type SubmissionOutcome,
} from "@/lib/client/job-submission";
import type { JobStatus, ListLimitation } from "@/lib/types";

interface ExistingJob {
  id: string;
  name: string;
  status: JobStatus;
  createdAt: string | null;
}

const allVersions: RvcVersion[] = ["v1", "v2"];
const allRates: SampleRate[] = ["40k", "48k"];
const allF0Methods: TrainingF0Method[] = ["pm", "harvest", "dio", "rmvpe", "rmvpe_gpu"];
const MAX_EXISTING_JOB_NAMES = 10_000;

export function JobMatrixBuilder({
  experimentId,
  experimentName,
  datasetId,
  datasetUsable,
  initialJobs,
  jobLimitation,
  demo,
}: {
  experimentId: string;
  experimentName: string;
  datasetId: string;
  datasetUsable: boolean;
  initialJobs: ExistingJob[];
  jobLimitation?: ListLimitation;
  demo: boolean;
}) {
  const router = useRouter();
  const [prefix, setPrefix] = useState(normalizeJobPrefix(experimentName));
  const [versions, setVersions] = useState<RvcVersion[]>(["v2"]);
  const [rates, setRates] = useState<SampleRate[]>(["40k"]);
  const [useF0, setUseF0] = useState(true);
  const [f0Methods, setF0Methods] = useState<TrainingF0Method[]>(["rmvpe"]);
  const [epochs, setEpochs] = useState("80");
  const [batchSize, setBatchSize] = useState("8");
  const [saveEvery, setSaveEvery] = useState("5");
  const [saveOnlyLatest, setSaveOnlyLatest] = useState(false);
  const [saveEveryWeights, setSaveEveryWeights] = useState(true);
  const [cacheDataset, setCacheDataset] = useState(false);
  const [gpuIdsText, setGpuIdsText] = useState("0");
  const [buildIndex, setBuildIndex] = useState(true);
  const [minVram, setMinVram] = useState("0");
  const [workerTags, setWorkerTags] = useState("");
  const [priority, setPriority] = useState("5");
  const [outcomes, setOutcomes] = useState<Record<string, SubmissionOutcome>>({});
  const [summary, setSummary] = useState(
    jobLimitation
      ? "기존 Job 전체를 검증 상한 안에서 조회할 수 없어 새 Job 생성을 차단했습니다."
      : "조합을 검토한 뒤 생성 요청을 시작할 수 있습니다.",
  );
  const [pending, setPending] = useState(false);
  const [jobs, setJobs] = useState(initialJobs);

  const matrix = useMemo(() => {
    const gpuIds = parseGpuIds(gpuIdsText);
    const tags = parseWorkerTags(workerTags);
    const result = buildJobMatrix(experimentId, datasetId, {
      prefix,
      versions,
      sampleRates: rates,
      useF0,
      f0Methods,
      epochs: numericValue(epochs),
      batchSizePerGpu: numericValue(batchSize),
      saveEveryEpoch: numericValue(saveEvery),
      saveOnlyLatest,
      saveEveryWeights,
      cacheDatasetInGpu: cacheDataset,
      gpuIds: gpuIds ?? [],
      buildIndex,
      minVramGb: numericValue(minVram),
      preferredWorkerTags: tags ?? [""],
      priority: numericValue(priority),
    });
    if (!gpuIds) result.errors.unshift("GPU ID는 예: 0 또는 0,1 형식으로 중복 없이 입력해 주세요.");
    if (!tags) result.errors.unshift("Worker tag는 쉼표로 구분하고 중복·제어 문자를 제거해 주세요.");
    return result;
  }, [
    batchSize,
    buildIndex,
    cacheDataset,
    datasetId,
    epochs,
    experimentId,
    f0Methods,
    gpuIdsText,
    minVram,
    prefix,
    priority,
    rates,
    saveEvery,
    saveEveryWeights,
    saveOnlyLatest,
    useF0,
    versions,
    workerTags,
  ]);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (pending || demo || !datasetUsable || matrix.errors.length > 0 || matrix.plans.length === 0) return;
    setPending(true);
    setSummary("기존 Job 이름을 Manager에서 다시 조회하고 있습니다.");
    const allKeys = matrix.plans.map((plan) => plan.key);
    setOutcomes(initializeSubmissionOutcomes(allKeys));

    let succeeded = 0;
    let conflicted = 0;
    let failed = 0;
    const settledKeys = new Set<string>();
    let inFlightKey: string | null = null;
    const settleOutcome = (key: string, outcome: SubmissionOutcome) => {
      settledKeys.add(key);
      setOutcomes((current) => withSubmissionOutcome(current, key, outcome));
    };
    const blockUnsettled = (message: string) => {
      setOutcomes((current) =>
        blockUnsettledSubmissionOutcomes(current, allKeys, settledKeys, message),
      );
    };
    try {
      const existingNames = await fetchExistingJobNames(experimentId);
      const candidates = [];
      for (const plan of matrix.plans) {
        if (existingNames.has(plan.jobName)) {
          conflicted += 1;
          settleOutcome(plan.key, {
            phase: "conflict",
            message: "기존 Job 이름과 중복되어 제출하지 않음",
          });
        } else {
          candidates.push(plan);
        }
      }
      if (candidates.length === 0) {
        setSummary(`모든 조합이 기존 이름과 중복됩니다. 중복 ${conflicted}건, 생성 요청 0건.`);
        return;
      }

      for (let index = 0; index < candidates.length; index += 1) {
        const plan = candidates[index];
        inFlightKey = plan.key;
        updateOutcome(plan.key, { phase: "creating", message: "Manager 검증 및 생성 중" });
        const response = await fetch("/bff/jobs", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(plan.config),
        });
        if (response.status === 401) {
          setSummary("인증 세션이 만료되었습니다. 다시 로그인해 주세요.");
          router.push("/session/expired");
          settledKeys.add(plan.key);
          setOutcomes((current) =>
            withSubmissionOutcome(current, plan.key, {
              phase: "blocked",
              message: "세션 만료로 제출하지 않음",
            }),
          );
          inFlightKey = null;
          blockUnsettled("세션 만료로 제출하지 않음");
          return;
        }
        const deferredId = response.status === 503
          ? await projectionDeferredResourceId(response, "job")
          : null;
        if (deferredId) {
          succeeded += 1;
          existingNames.add(plan.jobName);
          settleOutcome(plan.key, {
            phase: "success",
            message: "Job 원장 생성 완료 · MLflow 투영 지연",
            jobId: deferredId,
          });
          setJobs((current) => [
            { id: deferredId, name: plan.jobName, status: "queued", createdAt: new Date().toISOString() },
            ...current,
          ]);
          inFlightKey = null;
          continue;
        }
        if (response.ok) {
          const created = await response.json() as Partial<ApiCreatedJob>;
          if (typeof created.id !== "string" || typeof created.job_name !== "string") {
            failed += 1;
            settleOutcome(plan.key, {
              phase: "error",
              message: "생성 응답 형식을 확인할 수 없음 · Job 목록 확인 필요",
            });
            inFlightKey = null;
            continue;
          }
          succeeded += 1;
          existingNames.add(plan.jobName);
          settleOutcome(plan.key, {
            phase: "success",
            message: "queued Job 생성 완료",
            jobId: created.id,
          });
          setJobs((current) => [
            { id: created.id as string, name: plan.jobName, status: "queued", createdAt: new Date().toISOString() },
            ...current,
          ]);
          inFlightKey = null;
          continue;
        }

        const retryAfter = retryAfterSeconds(response);
        if (response.status === 409) {
          conflicted += 1;
          settleOutcome(plan.key, {
            phase: "conflict",
            message: "동시 생성 또는 서버 상태 충돌 (409)",
          });
          inFlightKey = null;
          continue;
        }
        failed += 1;
        settleOutcome(plan.key, {
          phase: "error",
          message: jobErrorMessage(response.status, retryAfter),
        });
        inFlightKey = null;
        if (response.status === 429 || response.status >= 500) {
          blockUnsettled(
            response.status === 429
              ? `rate limit으로 제출 보류${retryAfter === null ? "" : ` · ${retryAfter}초 후 재시도`}`
              : "Manager 연결 오류로 제출 보류",
          );
          break;
        }
      }
      setSummary(
        `생성 ${succeeded}건 · 중복/충돌 ${conflicted}건 · 실패 ${failed}건. 성공한 Job은 되돌리지 않았습니다.`,
      );
      if (succeeded > 0) router.refresh();
    } catch (error) {
      const failedInFlight = inFlightKey;
      setOutcomes((current) =>
        recoverSubmissionTransportFailure(
          current,
          allKeys,
          settledKeys,
          failedInFlight,
        ),
      );
      if (error instanceof SessionExpiredError) {
        setSummary("인증 세션이 만료되었습니다. 다시 로그인해 주세요.");
        router.push("/session/expired");
      } else {
        setSummary(
          failedInFlight
            ? "POST 응답이 유실됐습니다. 성공한 Job 상태는 보존했으며 현재 Job의 원장 생성 여부를 목록에서 확인해야 합니다."
            : error instanceof Error
              ? error.message
              : "Job 이름 확인 요청에 실패했습니다.",
        );
      }
    } finally {
      setPending(false);
    }
  }

  function updateOutcome(key: string, outcome: SubmissionOutcome) {
    setOutcomes((current) => withSubmissionOutcome(current, key, outcome));
  }

  const disabled = pending || demo || !datasetUsable;
  return (
    <>
      <form className="job-matrix-form" onSubmit={submit}>
        <section className="panel matrix-options-panel">
          <div className="panel-header">
            <div>
              <p className="panel-kicker">CONDITION MATRIX</p>
              <h2>Job 조합 설정</h2>
            </div>
            <span className="matrix-limit-label">최대 {MAX_JOB_MATRIX_SIZE}개 / 단건 API 순차 제출</span>
          </div>
          <div className="matrix-form-grid">
            <label className="form-field-wide">
              <span>Job 이름 prefix</span>
              <input
                disabled={disabled}
                maxLength={128}
                onChange={(event) => setPrefix(event.target.value)}
                value={prefix}
              />
              <small>공백·한글 등은 preview에서 안전한 ASCII 식별자로 결정적으로 정규화됩니다.</small>
            </label>

            <fieldset>
              <legend>RVC version</legend>
              <div className="choice-grid">
                {allVersions.map((version) => (
                  <Choice key={version} checked={versions.includes(version)} disabled={disabled} label={version}
                    onChange={() => setVersions((current) => toggle(current, version))} />
                ))}
              </div>
            </fieldset>
            <fieldset>
              <legend>Sample rate</legend>
              <div className="choice-grid">
                {allRates.map((rate) => (
                  <Choice key={rate} checked={rates.includes(rate)} disabled={disabled} label={rate}
                    onChange={() => setRates((current) => toggle(current, rate))} />
                ))}
              </div>
            </fieldset>
            <fieldset className="form-field-wide">
              <legend>F0 학습</legend>
              <Choice checked={useF0} disabled={disabled} label="use_f0 활성"
                onChange={() => setUseF0((current) => !current)} />
              <div className="choice-grid choice-grid-five">
                {allF0Methods.map((method) => (
                  <Choice key={method} checked={f0Methods.includes(method)} disabled={disabled || !useF0} label={method}
                    onChange={() => setF0Methods((current) => toggle(current, method))} />
                ))}
              </div>
              <small>use_f0=false이면 F0 방식은 null인 하나의 nof0 조건으로 생성됩니다.</small>
            </fieldset>

            <label><span>Epochs</span><input disabled={disabled} min={1} max={100000} onChange={(event) => setEpochs(event.target.value)} type="number" value={epochs} /></label>
            <label><span>GPU당 batch</span><input disabled={disabled} min={1} max={1024} onChange={(event) => setBatchSize(event.target.value)} type="number" value={batchSize} /></label>
            <label><span>Checkpoint 간격</span><input disabled={disabled} min={1} max={100000} onChange={(event) => setSaveEvery(event.target.value)} type="number" value={saveEvery} /></label>
            <label><span>GPU IDs</span><input disabled={disabled} maxLength={48} onChange={(event) => setGpuIdsText(event.target.value)} placeholder="0 또는 0,1" value={gpuIdsText} /></label>

            <fieldset className="form-field-wide">
              <legend>저장·메모리 옵션</legend>
              <div className="choice-grid choice-grid-four">
                <Choice checked={saveOnlyLatest} disabled={disabled} label="최신 checkpoint만" onChange={() => setSaveOnlyLatest((value) => !value)} />
                <Choice checked={saveEveryWeights} disabled={disabled} label="주기별 weights" onChange={() => setSaveEveryWeights((value) => !value)} />
                <Choice checked={cacheDataset} disabled={disabled} label="Dataset GPU cache" onChange={() => setCacheDataset((value) => !value)} />
                <Choice checked={buildIndex} disabled={disabled} label="FAISS index 생성" onChange={() => setBuildIndex((value) => !value)} />
              </div>
            </fieldset>

            <label><span>최소 VRAM (GiB)</span><input disabled={disabled} min={0} max={1024} step="0.5" onChange={(event) => setMinVram(event.target.value)} type="number" value={minVram} /></label>
            <label><span>우선순위 (0~10)</span><input disabled={disabled} min={0} max={10} onChange={(event) => setPriority(event.target.value)} type="number" value={priority} /></label>
            <label className="form-field-wide"><span>선호 Worker tags</span><input disabled={disabled} maxLength={512} onChange={(event) => setWorkerTags(event.target.value)} placeholder="예: 24gb,rmvpe,studio" value={workerTags} /><small>쉼표로 구분합니다. Worker capability matching의 선호 조건입니다.</small></label>

            <div className="auto-sample-disabled form-field-wide" role="note">
              <strong>자동 sample 생성: 비활성 고정</strong>
              <span>Native fixed-TestSet inference와 Sample 등록 경로는 구현됐지만 CREPE offline asset 및 실제 GPU runtime matrix release gate가 닫혀 있어 enabled=false, test_set_id=null, collect_samples=false만 전송합니다.</span>
            </div>
          </div>
        </section>

        <section className="panel matrix-preview-panel">
          <div className="panel-header">
            <div>
              <p className="panel-kicker">PREVIEW / DUPLICATE GATE</p>
              <h2>생성 미리보기</h2>
            </div>
            <strong className={matrix.errors.length > 0 ? "matrix-count matrix-count-error" : "matrix-count"}>
              {matrix.errors.length > 0 ? "설정 확인 필요" : `${matrix.plans.length} jobs`}
            </strong>
          </div>
          {matrix.errors.length > 0 ? (
            <ul className="matrix-error-list" role="alert">
              {[...new Set(matrix.errors)].map((error) => <li key={error}>{error}</li>)}
            </ul>
          ) : (
            <div className="table-wrap">
              <table>
                <thead><tr><th>Job name</th><th>Version</th><th>Rate</th><th>F0</th><th>Epoch / Batch</th><th>Index</th><th>제출 상태</th></tr></thead>
                <tbody>
                  {matrix.plans.map((plan) => {
                    const outcome = outcomes[plan.key];
                    return (
                      <tr key={plan.key}>
                        <td><code className="matrix-job-name">{plan.jobName}</code></td>
                        <td>{plan.version}</td><td>{plan.sampleRate}</td><td>{plan.f0Method ?? "nof0"}</td>
                        <td>{plan.config.training.epochs} / {plan.config.training.batch_size_per_gpu}</td>
                        <td>{plan.config.index.build_index ? "생성" : "생략"}</td>
                        <td><span className={`submission-state submission-state-${outcome?.phase ?? "ready"}`}>{outcome?.message ?? "제출 전"}</span></td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
          <div className="matrix-submit-row">
            <p aria-live="polite">{summary}</p>
            <button className="button button-primary" disabled={disabled || matrix.errors.length > 0 || matrix.plans.length === 0} type="submit">
              {pending ? "순차 생성 중…" : `${matrix.plans.length}개 Job 생성`}
            </button>
          </div>
        </section>
      </form>

      <section className="panel experiment-jobs-panel">
        <div className="panel-header">
          <div><p className="panel-kicker">EXISTING RUNS</p><h2>Experiment Job</h2></div>
          <span className="matrix-limit-label">
            {jobLimitation ? `${jobLimitation.total}개 · 상한 초과` : `${jobs.length}개 표시`}
          </span>
        </div>
        {jobs.length === 0 ? (
          <p className="experiment-jobs-empty">
            {jobLimitation
              ? "부분 Job 목록은 전체처럼 표시하지 않습니다. 필터 또는 보존 정책으로 상한 이하로 줄여 주세요."
              : "아직 Job이 없습니다. 위 조합에서 첫 작업을 만들 수 있습니다."}
          </p>
        ) : (
          <div className="table-wrap">
            <table><thead><tr><th>Job</th><th>Status</th><th>Created</th><th /></tr></thead><tbody>
              {jobs.map((job) => (
                <tr key={job.id}>
                  <td><strong className="table-primary">{job.name}</strong></td>
                  <td>{job.status}</td>
                  <td>{job.createdAt ? formatTimestamp(job.createdAt) : "—"}</td>
                  <td><Link className="row-action" aria-label={`${job.name} 상세 보기`} href={`/jobs/${encodeURIComponent(job.id)}`}>→</Link></td>
                </tr>
              ))}
            </tbody></table>
          </div>
        )}
      </section>
    </>
  );
}

function Choice({ checked, disabled, label, onChange }: { checked: boolean; disabled: boolean; label: string; onChange: () => void }) {
  return <label className="matrix-choice"><input checked={checked} disabled={disabled} onChange={onChange} type="checkbox" /><span>{label}</span></label>;
}

function toggle<T>(values: T[], value: T): T[] {
  return values.includes(value) ? values.filter((item) => item !== value) : [...values, value];
}

function numericValue(value: string): number {
  return value.trim() === "" ? Number.NaN : Number(value);
}

async function fetchExistingJobNames(experimentId: string): Promise<Set<string>> {
  const names = new Set<string>();
  let offset = 0;
  while (true) {
    const response = await fetch(
      `/bff/experiments/${encodeURIComponent(experimentId)}/jobs?offset=${offset}&limit=200`,
      { cache: "no-store" },
    );
    if (response.status === 401) throw new SessionExpiredError();
    if (!response.ok) throw new Error(jobErrorMessage(response.status, retryAfterSeconds(response)));
    const payload = await response.json() as Partial<ApiExperimentJobNameList>;
    if (!Array.isArray(payload.items) || typeof payload.total !== "number") {
      throw new Error("기존 Job 이름 응답을 검증할 수 없어 안전하게 생성을 중단했습니다.");
    }
    if (payload.total > MAX_EXISTING_JOB_NAMES) {
      throw new Error(`기존 Job이 ${MAX_EXISTING_JOB_NAMES}개를 초과해 중복 검사를 안전하게 완료할 수 없습니다.`);
    }
    for (const item of payload.items) {
      if (!item || typeof item.job_name !== "string") {
        throw new Error("기존 Job 이름 응답을 검증할 수 없어 안전하게 생성을 중단했습니다.");
      }
      names.add(item.job_name);
    }
    offset += payload.items.length;
    if (offset >= payload.total) return names;
    if (payload.items.length === 0) {
      throw new Error("기존 Job 이름 pagination이 진행되지 않아 생성을 중단했습니다.");
    }
  }
}

class SessionExpiredError extends Error {}

function retryAfterSeconds(response: Response): number | null {
  const value = response.headers.get("retry-after");
  return value && /^(0|[1-9][0-9]{0,5})$/.test(value) ? Number(value) : null;
}

async function projectionDeferredResourceId(
  response: Response,
  resourceType: "job",
): Promise<string | null> {
  try {
    const value = await response.json() as Record<string, unknown>;
    return value.error === "projection_deferred" &&
      value.ledger_committed === true &&
      value.resource_type === resourceType &&
      typeof value.resource_id === "string" &&
      /^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/.test(value.resource_id)
      ? value.resource_id
      : null;
  } catch {
    return null;
  }
}

function jobErrorMessage(status: number, retryAfter: number | null): string {
  if (status === 409) return "Job 이름 또는 Dataset 상태가 충돌했습니다 (409).";
  if (status === 422) return "Manager가 Job 설정을 거부했습니다 (422).";
  if (status === 429) return retryAfter === null ? "Manager 요청 제한에 도달했습니다 (429)." : `Manager 요청 제한 · ${retryAfter}초 후 재시도 (429).`;
  if (status === 403 || status === 404) return "Experiment를 찾을 수 없거나 접근 권한이 없습니다.";
  return "Manager가 Job 요청을 처리하지 못했습니다.";
}

function formatTimestamp(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return new Intl.DateTimeFormat("ko-KR", { dateStyle: "short", timeStyle: "short" }).format(date);
}
