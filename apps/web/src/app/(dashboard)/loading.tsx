export default function DashboardLoading() {
  return (
    <div className="route-state route-loading" role="status" aria-live="polite">
      <span className="loading-mark" aria-hidden="true" />
      <div>
        <strong>Manager 데이터를 불러오는 중입니다</strong>
        <p>인증과 최신 운영 상태를 확인하고 있습니다.</p>
      </div>
    </div>
  );
}
