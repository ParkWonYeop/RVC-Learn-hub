import Link from "next/link";

export default function JobNotFound() {
  return (
    <div className="route-state" role="status">
      <span className="state-mark" aria-hidden="true">
        404
      </span>
      <strong>학습 작업을 찾을 수 없습니다</strong>
      <p>작업이 삭제되었거나 현재 계정의 접근 범위에 포함되지 않습니다.</p>
      <Link className="button button-primary" href="/jobs">
        작업 목록으로 돌아가기
      </Link>
    </div>
  );
}
