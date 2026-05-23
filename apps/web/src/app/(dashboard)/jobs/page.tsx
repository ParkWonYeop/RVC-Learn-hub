import Link from "next/link";
import { PageHeader } from "@/components/page-header";
import { ListLimitNotice } from "@/components/list-limit-notice";
import { loadJobListData } from "@/lib/server/dashboard-data";
import { jobStatuses, type JobStatus } from "@/lib/types";
import { JobsTable } from "./jobs-table";

export const metadata = { title: "학습 작업" };

const statusLabels: Record<JobStatus, string> = {
  queued: "대기",
  assigned: "배정됨",
  downloading_dataset: "데이터 수신",
  validating_dataset: "데이터 검증",
  preparing_flat_dataset: "평탄화",
  preprocessing: "전처리",
  extracting_f0: "F0 추출",
  extracting_features: "Feature 추출",
  training: "학습 중",
  saving_checkpoint: "Checkpoint 저장",
  building_index: "Index 생성",
  collecting_small_model: "모델 수집",
  generating_samples: "샘플 생성",
  evaluating: "평가",
  uploading_artifacts: "Artifact 업로드",
  completed: "완료",
  failed: "실패",
  cancelled: "취소",
  retrying: "재시도",
};

export default async function JobsPage({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const query = await searchParams;
  const status = parseStatus(query.status);
  const experimentId = parseIdentifier(query.experiment_id);
  const { jobs, experiments, experimentLimitation } = await loadJobListData({ status, experimentId });

  return (
    <>
      <PageHeader
        eyebrow="TRAINING / JOBS"
        title="학습 작업"
        description="Manager의 status·experiment 필터를 적용하고, 현재 조회 결과는 브라우저에서 즉시 검색합니다."
        actions={
          <Link className="button button-primary" href="/experiments">
            Experiment에서 Job 생성
          </Link>
        }
      />
      {jobs.limitation ? <ListLimitNotice limitation={jobs.limitation} /> : null}
      {experimentLimitation && experimentLimitation !== jobs.limitation ? (
        <ListLimitNotice limitation={experimentLimitation} />
      ) : null}
      <section className="panel">
        <form className="jobs-server-filters" method="get">
          <label>
            <span>상태 · Manager API</span>
            <select defaultValue={status ?? ""} name="status">
              <option value="">전체 상태</option>
              {jobStatuses.map((value) => (
                <option key={value} value={value}>{statusLabels[value]}</option>
              ))}
            </select>
          </label>
          <label>
            <span>실험 · Manager API</span>
            <select defaultValue={experimentId ?? ""} name="experiment_id">
              <option value="">전체 실험</option>
              {experiments.map((experiment) => (
                <option key={experiment.id} value={experiment.id}>{experiment.name}</option>
              ))}
            </select>
          </label>
          <button className="button button-primary" type="submit">필터 적용</button>
          <Link className="button button-ghost" href="/jobs">필터 초기화</Link>
        </form>
        {jobs.limitation ? null : <JobsTable jobs={jobs.items} total={jobs.total} />}
      </section>
    </>
  );
}

function parseStatus(value: string | string[] | undefined): JobStatus | null {
  return typeof value === "string" && jobStatuses.includes(value as JobStatus)
    ? (value as JobStatus)
    : null;
}

function parseIdentifier(value: string | string[] | undefined): string | null {
  return typeof value === "string" && /^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/.test(value)
    ? value
    : null;
}
