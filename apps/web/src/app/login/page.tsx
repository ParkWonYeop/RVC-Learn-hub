import type { Metadata } from "next";
import { cookies } from "next/headers";
import { redirect } from "next/navigation";
import { LoginForm } from "./login-form";
import { safeInternalPath } from "@/lib/server/request-security";
import { SESSION_COOKIE_NAME } from "@/lib/server/session-cookie";

export const metadata: Metadata = { title: "로그인" };

export default async function LoginPage({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const params = await searchParams;
  const nextPath = safeInternalPath(singleValue(params.next));
  if ((await cookies()).has(SESSION_COOKIE_NAME)) redirect(nextPath);
  const expired = singleValue(params.reason) === "session_expired";

  return (
    <main className="login-page">
      <section className="login-card" aria-labelledby="login-title">
        <div className="login-brand">
          <span className="brand-mark" aria-hidden="true">
            R
          </span>
          <div>
            <strong>RVC Orchestrator</strong>
            <span>Training Control Plane</span>
          </div>
        </div>
        <p className="eyebrow">SECURE MANAGER ACCESS</p>
        <h1 id="login-title">운영 대시보드 로그인</h1>
        <p className="login-description">
          Manager 계정으로 인증합니다. 세션 토큰은 브라우저 스크립트에서 읽을 수 없는
          HttpOnly 쿠키에만 보관됩니다.
        </p>
        {expired ? (
          <p className="session-notice" role="status">
            세션이 만료되어 안전하게 로그아웃되었습니다. 다시 로그인해 주세요.
          </p>
        ) : null}
        <LoginForm nextPath={nextPath} />
      </section>
    </main>
  );
}

function singleValue(value: string | string[] | undefined): string | undefined {
  return Array.isArray(value) ? value[0] : value;
}
