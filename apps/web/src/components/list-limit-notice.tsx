import type { ListLimitation } from "@/lib/types";

const resourceLabels: Record<ListLimitation["resource"], string> = {
  datasets: "Dataset",
  experiments: "Experiment",
  jobs: "Job",
  workers: "Worker",
  users: "사용자",
};

export function ListLimitNotice({ limitation }: { limitation: ListLimitation }) {
  const resource = resourceLabels[limitation.resource];
  return (
    <div className="detail-notice detail-notice-warning" role="alert">
      <strong>{resource} 목록 상한 초과</strong>
      <span>
        Manager가 {limitation.total.toLocaleString("ko-KR")}개를 보고했지만 대시보드는 한 번에
        최대 {limitation.maximum.toLocaleString("ko-KR")}개까지만 완전하게 검증합니다. 일부
        결과를 전체처럼 표시하지 않았습니다. API 필터를 좁히거나 보존 정책을 적용해 주세요.
      </span>
    </div>
  );
}
