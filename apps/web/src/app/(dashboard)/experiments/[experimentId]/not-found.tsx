import Link from "next/link";
import { EmptyState } from "@/components/empty-state";

export default function ExperimentNotFound() {
  return (
    <section className="panel">
      <EmptyState
        title="Experiment를 찾을 수 없습니다"
        description="삭제되었거나 현재 계정에 조회 권한이 없는 Experiment입니다."
      />
      <div className="empty-state-actions">
        <Link className="button button-primary" href="/experiments">실험 목록</Link>
      </div>
    </section>
  );
}
