import Link from "next/link";
import { EmptyState } from "@/components/empty-state";
import { ListLimitNotice } from "@/components/list-limit-notice";
import { PageHeader } from "@/components/page-header";
import { dashboardDemoMode } from "@/lib/server/auth";
import { loadDatasetsData } from "@/lib/server/dashboard-data";
import { ExperimentCreateForm } from "./experiment-create-form";

export const metadata = { title: "새 실험" };

export default async function NewExperimentPage() {
  const datasets = await loadDatasetsData();
  const readyDatasets = datasets.items
    .filter((dataset) => dataset.status === "ready" && dataset.isUsable)
    .map((dataset) => ({
      id: dataset.id,
      name: dataset.name,
      fileCount: dataset.fileCount,
      durationMinutes: dataset.durationMinutes,
    }));
  const demo = dashboardDemoMode();

  return (
    <>
      <Link className="detail-back-link" href="/experiments">
        ← 실험 목록
      </Link>
      <PageHeader
        eyebrow="RESEARCH / NEW EXPERIMENT"
        title="Experiment 만들기"
        description="검증이 끝난 하나의 Dataset을 고정해 여러 학습 조건을 같은 그룹에서 비교합니다."
      />
      {demo ? (
        <div className="detail-notice" role="note">
          <strong>Demo fixture</strong>
          <span>예시 화면은 읽기 전용이므로 Experiment를 만들 수 없습니다.</span>
        </div>
      ) : null}
      {datasets.limitation ? (
        <>
          <ListLimitNotice limitation={datasets.limitation} />
          <section className="panel">
            <EmptyState
              title="Dataset 선택을 안전하게 완료할 수 없습니다"
              description="부분 Dataset 목록에서는 ready/is_usable 선택을 허용하지 않습니다. 필터 API 또는 보존 정책으로 목록을 상한 이하로 줄여 주세요."
            />
          </section>
        </>
      ) : readyDatasets.length === 0 ? (
        <section className="panel">
          <EmptyState
            title="학습 가능한 Dataset이 없습니다"
            description="Dataset 상태가 ready이고 is_usable=true가 된 뒤 Experiment를 만들 수 있습니다."
          />
          <div className="empty-state-actions">
            <Link className="button button-primary" href="/datasets">
              Dataset 확인
            </Link>
          </div>
        </section>
      ) : (
        <ExperimentCreateForm datasets={readyDatasets} demo={demo} />
      )}
    </>
  );
}
