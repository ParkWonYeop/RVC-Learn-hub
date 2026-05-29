import { EmptyState } from "@/components/empty-state";
import { EngineModeBadge } from "@/components/engine-mode-badge";
import { PageHeader } from "@/components/page-header";
import { ListLimitNotice } from "@/components/list-limit-notice";
import { StatusPill } from "@/components/status-pill";
import { requireCurrentUser } from "@/lib/server/auth";
import { loadWorkersData } from "@/lib/server/dashboard-data";

export const metadata = { title: "학습 서버" };

export default async function WorkersPage() {
  const user = await requireCurrentUser();
  if (user.role !== "admin") {
    return (
      <>
        <PageHeader
          eyebrow="INFRASTRUCTURE / WORKERS"
          title="학습 서버"
          description="Worker 인프라 정보는 관리자 역할에서만 조회할 수 있습니다."
        />
        <section className="panel">
          <EmptyState
            title="관리자 권한이 필요합니다"
            description="Dataset, Experiment와 Job은 계속 사용할 수 있습니다."
          />
        </section>
      </>
    );
  }
  const workers = await loadWorkersData();
  const online = workers.limitation
    ? null
    : workers.items.filter((worker) => worker.status !== "offline").length;
  const offline = workers.limitation
    ? null
    : workers.items.filter((worker) => worker.status === "offline").length;

  return (
    <>
      <PageHeader
        eyebrow="INFRASTRUCTURE / WORKERS"
        title="학습 서버"
        description="Manager heartbeat에서 Worker 연결, GPU 자원과 RVC runtime 상태를 조회합니다."
        actions={
          <button className="button button-primary" disabled title="Worker 등록 UI 미구현">
            Worker 등록 · 준비 중
          </button>
        }
      />
      {workers.limitation ? <ListLimitNotice limitation={workers.limitation} /> : null}
      <section className="panel">
        <div className="panel-toolbar">
          <div className="segmented" aria-label="Worker 상태 요약">
            <button className="active" disabled>
              전체 {workers.total}
            </button>
            <button disabled>온라인 {online ?? "—"}</button>
            <button disabled>오프라인 {offline ?? "—"}</button>
          </div>
          <input
            className="compact-search"
            placeholder="Worker 검색 · 준비 중"
            type="search"
            disabled
          />
        </div>
        {workers.limitation ? null : workers.items.length === 0 ? (
          <EmptyState
            title="등록된 Worker가 없습니다"
            description="Worker 등록 UI는 아직 연결되지 않았습니다."
          />
        ) : (
          <div className="worker-card-grid">
            {workers.items.map((worker) => (
              <article className="worker-card" key={worker.id}>
                <div className="worker-card-top">
                  <div className="machine-mark" aria-hidden="true">
                    GPU
                  </div>
                  <StatusPill status={worker.status} />
                </div>
                <h2>{worker.name}</h2>
                <p>{worker.gpuName ?? "GPU 정보 미제공"}</p>
                <dl className="definition-grid">
                  <div>
                    <dt>GPU 사용률</dt>
                    <dd>{valueWithUnit(worker.gpuUtilization, "%")}</dd>
                  </div>
                  <div>
                    <dt>온도</dt>
                    <dd>{valueWithUnit(worker.temperatureC, "°C")}</dd>
                  </div>
                  <div>
                    <dt>VRAM</dt>
                    <dd>{vramLabel(worker.vramUsedGb, worker.vramTotalGb)}</dd>
                  </div>
                  <div>
                    <dt>Heartbeat</dt>
                    <dd>{worker.lastHeartbeat ?? "없음"}</dd>
                  </div>
                </dl>
                <div className="gauge-track gauge-large">
                  <span
                    style={{ width: `${vramPercent(worker.vramUsedGb, worker.vramTotalGb)}%` }}
                  />
                </div>
                <div className="worker-engine-mode">
                  <span>Worker 광고 엔진</span>
                  <EngineModeBadge mode={worker.engineMode} />
                </div>
                <div className="tag-row">
                  <span>{worker.rvcAssetsReady ? "RVC ready" : "RVC assets 없음"}</span>
                  {worker.tags.map((tag) => (
                    <span key={tag}>{tag}</span>
                  ))}
                </div>
                <div className="worker-card-foot">
                  <span>{worker.currentJob ?? "Idle — 배정 가능"}</span>
                  <button aria-label={`${worker.name} 상세 보기 (준비 중)`} disabled>
                    →
                  </button>
                </div>
              </article>
            ))}
          </div>
        )}
      </section>
    </>
  );
}

function valueWithUnit(value: number | null, unit: string): string {
  return value === null ? "미제공" : `${value}${unit}`;
}

function vramLabel(used: number | null, total: number | null): string {
  return used === null || total === null ? "미제공" : `${used.toFixed(1)} / ${total} GB`;
}

function vramPercent(used: number | null, total: number | null): number {
  return used !== null && total !== null && total > 0 ? Math.min(100, (used / total) * 100) : 0;
}
