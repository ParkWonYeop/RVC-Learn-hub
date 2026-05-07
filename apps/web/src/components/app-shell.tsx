"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useState, type ReactNode } from "react";
import type { UserSummary } from "@/lib/types";

const navigation = [
  { href: "/", label: "개요", mark: "OV", adminOnly: false },
  { href: "/workers", label: "학습 서버", mark: "WK", adminOnly: true },
  { href: "/users", label: "사용자", mark: "US", adminOnly: true },
  { href: "/datasets", label: "데이터셋", mark: "DS", adminOnly: false },
  { href: "/experiments", label: "실험", mark: "EX", adminOnly: false },
  { href: "/jobs", label: "학습 작업", mark: "JB", adminOnly: false },
];

export function AppShell({
  children,
  user,
  demoMode,
}: {
  children: ReactNode;
  user: UserSummary;
  demoMode: boolean;
}) {
  const pathname = usePathname();
  const visibleNavigation = navigation.filter(
    (item) => !item.adminOnly || user.role === "admin",
  );

  return (
    <div className="app-shell">
      <a className="skip-link" href="#main-content">
        본문으로 건너뛰기
      </a>
      <aside className="sidebar">
        <div className="brand-block">
          <div className="brand-mark" aria-hidden="true">
            R
          </div>
          <div>
            <strong>RVC Orchestrator</strong>
            <span>Training Control Plane</span>
          </div>
        </div>

        <nav className="primary-nav" aria-label="주 메뉴">
          <p className="nav-kicker">WORKSPACE</p>
          {visibleNavigation.map((item) => {
            const active =
              item.href === "/" ? pathname === "/" : pathname.startsWith(item.href);
            return (
              <Link
                className={active ? "nav-link nav-link-active" : "nav-link"}
                href={item.href}
                key={item.href}
              >
                <span className="nav-mark" aria-hidden="true">
                  {item.mark}
                </span>
                {item.label}
              </Link>
            );
          })}
        </nav>

        <div className="sidebar-foot">
          <div className="control-plane-state">
            <span className="pulse-dot" aria-hidden="true" />
            <div>
              <strong>{demoMode ? "Demo fixture 활성" : "Manager API 연결됨"}</strong>
              <span>{demoMode ? "운영 데이터 아님" : "인증 세션 확인됨"}</span>
            </div>
          </div>
          <div className="version-line">
            <span>Manager UI</span>
            <code>v0.1.0-dev</code>
          </div>
        </div>
      </aside>

      <div className="content-shell">
        <header className="topbar">
          <div className={demoMode ? "environment-badge demo" : "environment-badge live"}>
            <span>{demoMode ? "DEMO" : "LIVE"}</span>
            <strong>{demoMode ? "FAKE FIXTURE" : "MANAGER API"}</strong>
          </div>
          <label className="global-search">
            <span className="sr-only">전역 검색은 아직 구현되지 않았습니다</span>
            <input
              aria-label="전역 검색 (준비 중)"
              placeholder="전역 검색 · 준비 중"
              type="search"
              disabled
            />
          </label>
          <div className="topbar-actions">
            <button className="icon-button" type="button" aria-label="알림 (준비 중)" disabled>
              <span aria-hidden="true">!</span>
            </button>
            <div className="profile-button">
              <span className="avatar">{initials(user.email)}</span>
              <span>
                <strong>{user.role === "admin" ? "관리자" : "사용자"}</strong>
                <small>{user.email}</small>
              </span>
            </div>
            <LogoutButton />
          </div>
        </header>
        <main id="main-content" className="main-content">
          {children}
        </main>
      </div>
    </div>
  );
}

function LogoutButton() {
  const [pending, setPending] = useState(false);
  const [failed, setFailed] = useState(false);

  async function logout() {
    setPending(true);
    setFailed(false);
    try {
      const response = await fetch("/session/logout", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
      });
      if (response.status !== 204 && response.status !== 502) {
        throw new Error("logout rejected");
      }
      window.location.assign("/login");
    } catch {
      setFailed(true);
      setPending(false);
    }
  }

  return (
    <button
      className="logout-button"
      disabled={pending}
      onClick={logout}
      type="button"
      title={failed ? "로그아웃 요청에 실패했습니다. 다시 시도해 주세요." : undefined}
    >
      {pending ? "종료 중…" : failed ? "로그아웃 재시도" : "로그아웃"}
    </button>
  );
}

function initials(email: string): string {
  return email.slice(0, 2).toUpperCase();
}
