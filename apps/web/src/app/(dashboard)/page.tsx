import Link from "next/link";
import { EngineModeBadge } from "@/components/engine-mode-badge";
import { EmptyState } from "@/components/empty-state";
import { PageHeader } from "@/components/page-header";
import { ListLimitNotice } from "@/components/list-limit-notice";
import { StatusPill } from "@/components/status-pill";
import { dashboardDemoMode, requireCurrentUser } from "@/lib/server/auth";
import { loadOverviewData } from "@/lib/server/dashboard-data";

const runningStatuses = new Set([
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
]);

export default async function OverviewPage() {
  const user = await requireCurrentUser();
  const { jobs, workers } = await loadOverviewData(user.role);
  const demoMode = dashboardDemoMode();
  const activeWorkers = workers?.items.filter((worker) => worker.status !== "offline").length;
  const runningJobs = jobs.limitation
    ? null
    : jobs.items.filter((job) => runningStatuses.has(job.status)).length;
  const queuedJobs = jobs.limitation
    ? null
    : jobs.items.filter((job) => job.status === "queued").length;
  const completedJobs = jobs.limitation
    ? null
    : jobs.items.filter((job) => job.status === "completed").length;
  const gpuValues = workers?.items.flatMap((worker) =>
    worker.gpuUtilization === null ? [] : [worker.gpuUtilization],
  );
  const averageGpu = gpuValues?.length
    ? Math.round(gpuValues.reduce((total, value) => total + value, 0) / gpuValues.length)
    : null;

  return (
    <>
      <PageHeader
        eyebrow="CONTROL ROOM / OVERVIEW"
        title="학습 인프라를 한눈에"
        description={
          demoMode
            ? "DASHBOARD_DEMO_MODE로 활성화된 Fake fixture입니다. 운영 결과가 아닙니다."
            : "Manager API에서 Worker 권한 범위, 대기열과 최근 실행 상태를 조회합니다."
        }
        actions={
          <>
            {demoMode ? (
              <button className="button button-secondary" disabled title="Demo fixture는 읽기 전용입니다.">
                데이터셋 추가
              </button>
            ) : (
              <Link className="button button-secondary" href="/datasets">데이터셋 추가</Link>
            )}
            {demoMode ? (
              <button className="button button-primary" disabled title="Demo fixture는 읽기 전용입니다.">
                새 실험
              </button>
            ) : (
              <Link className="button button-primary" href="/experiments/new">새 실험</Link>
            )}
          </>
        }
      />
      {jobs.limitation ? <ListLimitNotice limitation={jobs.limitation} /> : null}
      {workers?.limitation ? <ListLimitNotice limitation={workers.limitation} /> : null}

      <section className="metric-grid" aria-label="요약 지표">
        <article className="metric-card metric-card-accent">
          <div className="metric-topline">
            <span>활성 Worker</span>
            <span className="mini-trend positive">
              {workers ? "실시간" : "관리자 전용"}
            </span>
          </div>
          <strong>{activeWorkers ?? "제한"}</strong>
          <p>{workers ? `전체 ${workers.total}대 중 연결됨` : "Worker 조회 권한 없음"}</p>
        </article>
        <article className="metric-card">
          <div className="metric-topline">
            <span>실행 중</span>
            <span className="mini-trend">GPU</span>
          </div>
          <strong>{runningJobs ?? "—"}</strong>
          <p>학습 또는 후처리 단계</p>
          <div className="metric-foot">
            <span>평균 GPU</span>
            <b>{averageGpu === null ? "—" : `${averageGpu}%`}</b>
          </div>
        </article>
        <article className="metric-card">
          <div className="metric-topline">
            <span>대기 작업</span>
            <span className="mini-trend warning">Queue</span>
          </div>
          <strong>{queuedJobs ?? "—"}</strong>
          <p>조건에 맞는 Worker 대기</p>
          <div className="metric-foot">
            <span>조회한 작업</span>
            <b>{jobs.total}</b>
          </div>
        </article>
        <article className="metric-card">
          <div className="metric-topline">
            <span>완료된 Run</span>
            <span className="mini-trend positive">전체</span>
          </div>
          <strong>{completedJobs ?? "—"}</strong>
          <p>현재 조회 범위의 완료 작업</p>
          <div className="metric-foot">
            <span>Artifact 상태</span>
            <b>별도 API 예정</b>
          </div>
        </article>
      </section>

      <div className="overview-grid">
        <section className="panel panel-wide">
          <div className="panel-header">
            <div>
              <p className="panel-kicker">LIVE QUEUE</p>
              <h2>최근 학습 작업</h2>
            </div>
            <Link href="/jobs" className="text-link">
              전체 작업 보기 →
            </Link>
          </div>
          {jobs.limitation ? null : jobs.items.length === 0 ? (
            <EmptyState
              title="표시할 학습 작업이 없습니다"
              description="작업 생성 기능이 연결되면 이곳에 최근 실행 상태가 표시됩니다."
            />
          ) : (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>작업</th>
                    <th>상태</th>
                    <th>엔진</th>
                    <th>Worker</th>
                    <th>진행</th>
                    <th>Loss</th>
                  </tr>
                </thead>
                <tbody>
                  {jobs.items.slice(0, 4).map((job) => {
                    const progress = progressPercent(job.currentEpoch, job.totalEpoch);
                    return (
                      <tr key={job.id}>
                        <td>
                          <Link
                            className="table-primary"
                            href={`/jobs/${encodeURIComponent(job.id)}`}
                          >
                            {job.name}
                          </Link>
                          <span className="table-secondary">
                            {job.version} · {job.sampleRate} · {job.f0Method}
                          </span>
                        </td>
                        <td>
                          <StatusPill status={job.status} />
                        </td>
                        <td>
                          <EngineModeBadge mode={job.engineMode} />
                        </td>
                        <td className="mono-cell">{job.worker ?? "미배정"}</td>
                        <td>
                          <div className="progress-label">
                            <span>
                              {job.currentEpoch === null
                                ? `epoch 미제공 / ${job.totalEpoch}`
                                : `${job.currentEpoch}/${job.totalEpoch} epoch`}
                            </span>
                            <b>{progress === null ? "—" : `${progress}%`}</b>
                          </div>
                          <div className="progress-track">
                            <span style={{ width: `${progress ?? 0}%` }} />
                          </div>
                        </td>
                        <td className="mono-cell">
                          {job.latestLoss === null ? "미제공" : job.latestLoss.toFixed(3)}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </section>

        <aside className="panel worker-pulse-panel">
          <div className="panel-header">
            <div>
              <p className="panel-kicker">WORKER PULSE</p>
              <h2>서버 상태</h2>
            </div>
          </div>
          {!workers ? (
            <EmptyState
              title="관리자 전용 정보"
              description="Worker 인프라는 관리자 역할에서만 조회할 수 있습니다."
            />
          ) : workers.items.length === 0 ? (
            <EmptyState
              title="등록된 Worker가 없습니다"
              description="Worker가 등록되면 heartbeat와 GPU 정보가 표시됩니다."
            />
          ) : (
            <div className="worker-pulse-list">
              {workers.items.map((worker) => (
                <article className="worker-pulse" key={worker.id}>
                  <div className="worker-pulse-heading">
                    <div>
                      <strong>{worker.name}</strong>
                      <span>{worker.gpuName ?? "GPU 정보 미제공"}</span>
                    </div>
                    <StatusPill status={worker.status} />
                  </div>
                  <div className="gauge-label">
                    <span>VRAM</span>
                    <b>{vramLabel(worker.vramUsedGb, worker.vramTotalGb)}</b>
                  </div>
                  <div className="gauge-track">
                    <span
                      style={{ width: `${vramPercent(worker.vramUsedGb, worker.vramTotalGb)}%` }}
                    />
                  </div>
                </article>
              ))}
            </div>
          )}
        </aside>
      </div>

      <section className="panel system-strip">
        <div className="system-heading">
          <span className="pulse-dot" aria-hidden="true" />
          <div>
            <strong>중앙 서비스 연결</strong>
            <span>현재 요청 기준</span>
          </div>
        </div>
        <div className="service-state">
          <span>Manager API</span>
          <b className="state-healthy">인증됨</b>
        </div>
        {['PostgreSQL', 'Redis', 'Object Storage', 'MLflow'].map((service) => (
          <div className="service-state" key={service}>
            <span>{service}</span>
            <b className="state-muted">상태 API 미제공</b>
          </div>
        ))}
      </section>
    </>
  );
}

function progressPercent(current: number | null, total: number): number | null {
  if (current === null || total <= 0) return null;
  return Math.min(100, Math.max(0, Math.round((current / total) * 100)));
}

function vramPercent(used: number | null, total: number | null): number {
  return used !== null && total !== null && total > 0 ? Math.min(100, (used / total) * 100) : 0;
}

function vramLabel(used: number | null, total: number | null): string {
  return used === null || total === null ? "미제공" : `${used.toFixed(1)} / ${total} GB`;
}
