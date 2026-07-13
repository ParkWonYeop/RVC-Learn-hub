import Link from "next/link";
import { notFound } from "next/navigation";
import { PageHeader } from "@/components/page-header";
import { ListLimitNotice } from "@/components/list-limit-notice";
import { isSafeResourceId } from "@/lib/server/bff-security";
import { requireCurrentUser } from "@/lib/server/auth";
import { loadExperimentWorkspace } from "@/lib/server/dashboard-data";
import { JobMatrixBuilder } from "./job-matrix-builder";
import { SampleComparison } from "./sample-comparison";
import { ExperimentSettings } from "./experiment-settings";
import { ExperimentModelGovernance } from "./experiment-model-governance";

type Props = { params: Promise<{ experimentId: string }> };

export default async function ExperimentDetailPage({ params }: Props) {
  const { experimentId } = await params;
  if (!isSafeResourceId(experimentId)) notFound();
  const [workspace, currentUser] = await Promise.all([
    loadExperimentWorkspace(experimentId),
    requireCurrentUser(),
  ]);
  if (!workspace) notFound();

  return (
    <>
      <Link className="detail-back-link" href="/experiments">
        ← 실험 목록
      </Link>
      <PageHeader
        eyebrow="RESEARCH / EXPERIMENT"
        title={workspace.name}
        description={workspace.description ?? "동일 Dataset에서 생성한 immutable Job 조건을 관리합니다."}
        actions={
          <Link
            className="button button-secondary"
            href={`/jobs?experiment_id=${encodeURIComponent(workspace.id)}`}
          >
            Job 목록 필터
          </Link>
        }
      />
      {workspace.demo ? (
        <div className="detail-notice" role="note">
          <strong>Demo fixture</strong>
          <span>조합 preview는 확인할 수 있지만 Manager Job 생성 요청은 차단됩니다.</span>
        </div>
      ) : null}
      {!workspace.dataset.isUsable ? (
        <div className="detail-notice detail-notice-error" role="alert">
          <strong>Dataset 사용 불가</strong>
          <span>
            현재 상태는 {workspace.dataset.status}입니다. ready / is_usable 조건을 다시 만족하기 전에는 Job을 만들 수 없습니다.
          </span>
        </div>
      ) : null}
      {workspace.jobLimitation ? (
        <ListLimitNotice limitation={workspace.jobLimitation} />
      ) : null}

      <section className="experiment-context-grid" aria-label="Experiment 요약">
        <article className="panel experiment-context-card">
          <span>DATASET</span>
          <strong>{workspace.dataset.name}</strong>
          <small>{workspace.dataset.id}</small>
        </article>
        <article className="panel experiment-context-card">
          <span>READY FILES</span>
          <strong>{workspace.dataset.fileCount ?? "—"}</strong>
          <small>{workspace.dataset.status} / {workspace.dataset.isUsable ? "usable" : "blocked"}</small>
        </article>
        <article className="panel experiment-context-card">
          <span>DURATION</span>
          <strong>{workspace.dataset.durationMinutes === null ? "—" : `${workspace.dataset.durationMinutes.toFixed(1)}분`}</strong>
          <small>Manager 검증 결과</small>
        </article>
        <article className="panel experiment-context-card">
          <span>EXISTING JOBS</span>
          <strong>{workspace.jobLimitation?.total ?? workspace.jobs.length}</strong>
          <small>{workspace.jobLimitation ? "검증 상한 초과 · 상세 목록 미표시" : "완전한 pagination 조회"}</small>
        </article>
      </section>

      <ExperimentSettings
        datasetId={workspace.dataset.id}
        demo={workspace.demo}
        experimentId={workspace.id}
        experimentName={workspace.name}
        initialDescription={workspace.description}
        initialRowVersion={workspace.rowVersion}
        knownJobCount={workspace.jobLimitation?.total ?? workspace.jobs.length}
      />

      <JobMatrixBuilder
        datasetId={workspace.dataset.id}
        datasetUsable={workspace.dataset.isUsable && !workspace.jobLimitation}
        demo={workspace.demo}
        experimentId={workspace.id}
        experimentName={workspace.name}
        initialJobs={workspace.jobs}
        jobLimitation={workspace.jobLimitation}
      />

      {!workspace.demo ? (
        <ExperimentModelGovernance
          actorId={currentUser.id}
          comparisonAvailable={!workspace.jobLimitation}
          experimentId={workspace.id}
          jobs={workspace.jobs}
          key={`${workspace.id}:${currentUser.id}`}
        />
      ) : null}

      <SampleComparison demo={workspace.demo} jobs={workspace.jobs} />
    </>
  );
}
