"use client";

import { useEffect } from "react";

export default function DashboardError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  useEffect(() => {
    // Report only the opaque digest. Error messages may contain upstream detail.
    if (error.digest) console.error(`dashboard-render-failed:${error.digest}`);
  }, [error.digest]);

  return (
    <section className="route-state route-error" role="alert">
      <span className="state-mark" aria-hidden="true">
        !
      </span>
      <div>
        <strong>Manager 데이터를 표시할 수 없습니다</strong>
        <p>API 연결과 서비스 상태를 확인한 뒤 다시 시도해 주세요.</p>
        <button className="button button-secondary" onClick={reset} type="button">
          다시 시도
        </button>
      </div>
    </section>
  );
}
