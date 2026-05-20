import Link from "next/link";
import { EmptyState } from "@/components/empty-state";
import { ListLimitNotice } from "@/components/list-limit-notice";
import { PageHeader } from "@/components/page-header";
import { dashboardDemoMode } from "@/lib/server/auth";
import { loadExperimentsData } from "@/lib/server/dashboard-data";

export const metadata = { title: "실험" };

export default async function ExperimentsPage() {
  const experiments = await loadExperimentsData();
  const demo = dashboardDemoMode();

  return (
    <>
      <PageHeader
        eyebrow="RESEARCH / EXPERIMENTS"
        title="실험 그룹"
        description="현재 사용자가 소유한 Experiment와 연결된 Job 완료 현황을 조회합니다."
        actions={
          demo ? (
            <button className="button button-primary" disabled title="Demo fixture는 읽기 전용입니다.">
              새 실험
            </button>
          ) : (
            <Link className="button button-primary" href="/experiments/new">새 실험</Link>
          )
        }
      />
      {experiments.limitation ? (
        <ListLimitNotice limitation={experiments.limitation} />
      ) : experiments.items.length === 0 ? (
        <section className="panel">
          <EmptyState
            title="생성된 실험 그룹이 없습니다"
            description="검증된 Dataset을 선택해 첫 Experiment와 학습 조건을 만들 수 있습니다."
          />
          {!demo ? (
            <div className="empty-state-actions">
              <Link className="button button-primary" href="/experiments/new">첫 Experiment 만들기</Link>
            </div>
          ) : null}
        </section>
      ) : (
        <section className="experiment-grid">
          {experiments.items.map((experiment) => (
            <article className="panel experiment-card" key={experiment.id}>
              <div className="experiment-card-top">
                <span className="experiment-mark">EXPERIMENT</span>
                <button aria-label={`${experiment.name} 메뉴 (준비 중)`} disabled>
                  •••
                </button>
              </div>
              <h2>{experiment.name}</h2>
              <p>{experiment.datasetName}</p>
              <div className="run-balance">
                <div>
                  <span>Run</span>
                  <strong>{experiment.runCount}</strong>
                </div>
                <div>
                  <span>완료</span>
                  <strong>{experiment.completedCount}</strong>
                </div>
                <div>
                  <span>갱신</span>
                  <strong>{experiment.updatedAt}</strong>
                </div>
              </div>
              <div className="completion-track">
                <span style={{ width: `${completionPercent(experiment)}%` }} />
              </div>
              <div className="best-run">
                <span>BEST RUN</span>
                <strong>{experiment.bestRun ?? "Metric 선택 API 미제공"}</strong>
              </div>
              <div className="card-actions">
                <Link
                  className="button button-secondary"
                  href={`/experiments/${encodeURIComponent(experiment.id)}#sample-compare-heading`}
                >
                  Sample A/B 비교
                </Link>
                <Link className="button button-ghost" href={`/experiments/${encodeURIComponent(experiment.id)}`}>
                  상세·Job 생성
                </Link>
              </div>
            </article>
          ))}
          {demo ? (
            <button className="new-experiment-card" type="button" disabled>
              <span aria-hidden="true">+</span>
              <strong>Demo fixture는 읽기 전용</strong>
              <small>실제 Manager 연결에서 생성할 수 있습니다</small>
            </button>
          ) : (
            <Link className="new-experiment-card" href="/experiments/new">
              <span aria-hidden="true">+</span>
              <strong>실험 그룹 만들기</strong>
              <small>ready / usable Dataset을 하나 선택합니다</small>
            </Link>
          )}
        </section>
      )}
    </>
  );
}

function completionPercent(experiment: {
  runCount: number;
  completedCount: number;
}): number {
  return experiment.runCount > 0
    ? Math.min(100, Math.round((experiment.completedCount / experiment.runCount) * 100))
    : 0;
}
