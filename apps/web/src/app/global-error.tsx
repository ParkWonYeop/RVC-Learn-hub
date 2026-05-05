"use client";

export default function GlobalError({ reset }: { reset: () => void }) {
  return (
    <html lang="ko">
      <body>
        <main className="login-page">
          <section className="login-card route-error" role="alert">
            <span className="state-mark" aria-hidden="true">
              !
            </span>
            <h1>대시보드를 시작할 수 없습니다</h1>
            <p className="login-description">
              Manager API 연결 또는 대시보드 설정을 확인해 주세요.
            </p>
            <button className="button button-secondary" onClick={reset} type="button">
              다시 시도
            </button>
          </section>
        </main>
      </body>
    </html>
  );
}
