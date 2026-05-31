import type { JobStatus, WorkerStatus } from "@/lib/types";

const labels: Record<JobStatus | WorkerStatus, string> = {
  online: "온라인",
  busy: "실행 중",
  offline: "오프라인",
  draining: "종료 준비",
  queued: "대기",
  assigned: "배정됨",
  downloading_dataset: "데이터 수신",
  validating_dataset: "검증",
  preparing_flat_dataset: "평탄화",
  preprocessing: "전처리",
  extracting_f0: "F0 추출",
  extracting_features: "Feature 추출",
  training: "학습 중",
  saving_checkpoint: "Checkpoint 저장",
  building_index: "Index 생성",
  collecting_small_model: "모델 수집",
  generating_samples: "Sample 생성",
  evaluating: "평가",
  uploading_artifacts: "업로드",
  completed: "완료",
  failed: "실패",
  cancelled: "취소",
  retrying: "재시도",
};

export function StatusPill({ status }: { status: JobStatus | WorkerStatus }) {
  return (
    <span className={`status-pill status-${status}`}>
      <span className="status-dot" aria-hidden="true" />
      {labels[status]}
    </span>
  );
}
