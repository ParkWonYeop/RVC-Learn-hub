"use client";

import { useMemo, useState, type FormEvent } from "react";
import { publicAdminUser } from "@/lib/admin-user-projections";
import type { ManagedUser, UserRole } from "@/lib/types";

type Draft = { role: UserRole; active: boolean };
type Feedback = { kind: "success" | "error" | "uncertain"; text: string } | null;

export function UserAdminPanel({
  currentUserId,
  demoMode,
  initialUsers,
}: {
  currentUserId: string;
  demoMode: boolean;
  initialUsers: ManagedUser[];
}) {
  const [users, setUsers] = useState(initialUsers);
  const [drafts, setDrafts] = useState<Record<string, Draft>>(() =>
    Object.fromEntries(initialUsers.map((user) => [user.id, draftOf(user)])),
  );
  const [pending, setPending] = useState<string | null>(null);
  const [feedback, setFeedback] = useState<Feedback>(null);
  const [uncertain, setUncertain] = useState(false);
  const [resetUserId, setResetUserId] = useState<string | null>(null);
  const [resetPassword, setResetPassword] = useState("");
  const [resetConfirmation, setResetConfirmation] = useState("");

  const activeAdmins = useMemo(
    () => users.filter((user) => user.active && user.role === "admin").length,
    [users],
  );
  const disabled = demoMode || uncertain;

  async function createUser(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (disabled || pending) return;
    const formElement = event.currentTarget;
    const form = new FormData(formElement);
    const email = String(form.get("email") ?? "").trim().toLowerCase();
    const password = String(form.get("password") ?? "");
    const confirmation = String(form.get("password_confirmation") ?? "");
    const role = String(form.get("role") ?? "user");
    const active = form.get("active") === "on";
    if (!validEmail(email) || !validPassword(password) || password !== confirmation) {
      setFeedback({ kind: "error", text: "이메일과 16자 이상 비밀번호 확인 값을 점검해 주세요." });
      return;
    }
    if (role !== "admin" && role !== "user") return;
    setPending("create");
    setFeedback(null);
    try {
      const result = await userMutation("/bff/admin/users", "POST", {
        email,
        password,
        role,
        active,
      });
      if (!result.ok) {
        handleFailure(result, setFeedback, setUncertain);
        return;
      }
      const user = toManagedUser(result.user);
      setUsers((current) => [...current, user].sort(compareUsers));
      setDrafts((current) => ({ ...current, [user.id]: draftOf(user) }));
      formElement.reset();
      setFeedback({ kind: "success", text: `${user.email} 계정을 생성했습니다.` });
    } catch {
      markUncertain(setFeedback, setUncertain);
    } finally {
      setPending(null);
    }
  }

  async function saveUser(user: ManagedUser) {
    const draft = drafts[user.id] ?? draftOf(user);
    if (disabled || pending || user.id === currentUserId) return;
    if (draft.role === user.role && draft.active === user.active) return;
    setPending(`update:${user.id}`);
    setFeedback(null);
    try {
      const result = await userMutation(`/bff/admin/users/${encodeURIComponent(user.id)}`, "PATCH", {
        expected_row_version: user.rowVersion,
        role: draft.role,
        active: draft.active,
      });
      if (!result.ok) {
        handleFailure(result, setFeedback, setUncertain);
        return;
      }
      replaceUser(result.user);
      setFeedback({ kind: "success", text: `${result.user.email} 계정 상태를 변경했습니다.` });
    } catch {
      markUncertain(setFeedback, setUncertain);
    } finally {
      setPending(null);
    }
  }

  async function resetPasswordFor(user: ManagedUser) {
    if (disabled || pending || resetUserId !== user.id) return;
    if (!validPassword(resetPassword) || resetPassword !== resetConfirmation) {
      setFeedback({ kind: "error", text: "새 비밀번호는 16자 이상이어야 하며 확인 값과 같아야 합니다." });
      return;
    }
    setPending(`password:${user.id}`);
    setFeedback(null);
    try {
      const result = await userMutation(
        `/bff/admin/users/${encodeURIComponent(user.id)}/password-reset`,
        "POST",
        { expected_row_version: user.rowVersion, new_password: resetPassword },
      );
      if (!result.ok) {
        handleFailure(result, setFeedback, setUncertain);
        return;
      }
      setResetPassword("");
      setResetConfirmation("");
      setResetUserId(null);
      if (user.id === currentUserId) {
        window.location.assign("/session/expired");
        return;
      }
      replaceUser(result.user);
      setFeedback({ kind: "success", text: `${result.user.email} 비밀번호를 재설정했습니다.` });
    } catch {
      markUncertain(setFeedback, setUncertain);
    } finally {
      setPending(null);
    }
  }

  function replaceUser(value: NonNullable<ReturnType<typeof publicAdminUser>>) {
    const updated = toManagedUser(value);
    setUsers((current) => current.map((user) => user.id === updated.id ? updated : user));
    setDrafts((current) => ({ ...current, [updated.id]: draftOf(updated) }));
  }

  return (
    <div className="user-admin-layout">
      {demoMode ? (
        <div className="detail-notice detail-notice-warning" role="status">
          <strong>Demo 모드는 읽기 전용입니다</strong>
          <span>운영 Manager에 로그인하면 계정 변경 기능을 사용할 수 있습니다.</span>
        </div>
      ) : null}
      {feedback ? (
        <div
          className={`user-feedback user-feedback-${feedback.kind}`}
          role={feedback.kind === "success" ? "status" : "alert"}
        >
          <span>{feedback.text}</span>
          {feedback.kind === "uncertain" ? (
            <button className="button button-secondary" onClick={() => window.location.reload()} type="button">
              목록 새로고침
            </button>
          ) : null}
        </div>
      ) : null}

      <section className="panel user-create-panel">
        <div className="panel-header">
          <div>
            <span className="section-kicker">NEW ACCOUNT</span>
            <h2>사용자 생성</h2>
          </div>
          <span className="user-count">총 {users.length.toLocaleString("ko-KR")}명</span>
        </div>
        <form className="user-create-form" onSubmit={createUser}>
          <label>
            <span>이메일</span>
            <input autoComplete="off" disabled={disabled || pending !== null} maxLength={320} name="email" required type="email" />
          </label>
          <label>
            <span>초기 비밀번호</span>
            <input autoComplete="new-password" disabled={disabled || pending !== null} maxLength={1024} minLength={16} name="password" required type="password" />
          </label>
          <label>
            <span>비밀번호 확인</span>
            <input autoComplete="new-password" disabled={disabled || pending !== null} maxLength={1024} minLength={16} name="password_confirmation" required type="password" />
          </label>
          <label>
            <span>역할</span>
            <select defaultValue="user" disabled={disabled || pending !== null} name="role">
              <option value="user">사용자</option>
              <option value="admin">관리자</option>
            </select>
          </label>
          <label className="user-active-choice">
            <input defaultChecked disabled={disabled || pending !== null} name="active" type="checkbox" />
            <span>생성 즉시 활성화</span>
          </label>
          <button className="button button-primary" disabled={disabled || pending !== null} type="submit">
            {pending === "create" ? "생성 중…" : "계정 생성"}
          </button>
        </form>
      </section>

      <section className="panel user-list-panel">
        <div className="panel-header">
          <div>
            <span className="section-kicker">ACCOUNT DIRECTORY</span>
            <h2>계정과 권한</h2>
          </div>
          <span className="user-count">활성 관리자 {activeAdmins}명</span>
        </div>
        {users.length === 0 ? (
          <div className="user-list-empty">표시할 사용자 계정이 없습니다.</div>
        ) : (
          <div className="table-wrap">
            <table className="user-table">
              <thead>
                <tr>
                  <th>계정</th>
                  <th>역할</th>
                  <th>활성</th>
                  <th>변경 시각</th>
                  <th>계정 변경</th>
                  <th>비밀번호</th>
                </tr>
              </thead>
              <tbody>
                {users.map((user) => {
                  const self = user.id === currentUserId;
                  const draft = drafts[user.id] ?? draftOf(user);
                  const changed = draft.role !== user.role || draft.active !== user.active;
                  return (
                    <tr key={user.id}>
                      <td>
                        <strong className="table-primary">{user.email}</strong>
                        <span className="table-secondary">{self ? "현재 로그인 계정" : `ID ${user.id}`}</span>
                      </td>
                      <td>
                        <select
                          aria-label={`${user.email} 역할`}
                          disabled={disabled || pending !== null || self}
                          onChange={(event) => setDrafts((current) => ({
                            ...current,
                            [user.id]: { ...draft, role: event.target.value as UserRole },
                          }))}
                          value={draft.role}
                        >
                          <option value="user">사용자</option>
                          <option value="admin">관리자</option>
                        </select>
                      </td>
                      <td>
                        <label className="user-state-toggle">
                          <input
                            aria-label={`${user.email} 활성 상태`}
                            checked={draft.active}
                            disabled={disabled || pending !== null || self}
                            onChange={(event) => setDrafts((current) => ({
                              ...current,
                              [user.id]: { ...draft, active: event.target.checked },
                            }))}
                            type="checkbox"
                          />
                          <span>{draft.active ? "활성" : "비활성"}</span>
                        </label>
                      </td>
                      <td>
                        <span className="table-primary">{formatDate(user.updatedAt)}</span>
                        <span className="table-secondary">version {user.rowVersion}</span>
                      </td>
                      <td>
                        <button
                          className="button button-secondary user-row-button"
                          disabled={disabled || pending !== null || self || !changed}
                          onClick={() => saveUser(user)}
                          title={self ? "현재 관리자 계정은 스스로 비활성화하거나 강등할 수 없습니다." : undefined}
                          type="button"
                        >
                          {pending === `update:${user.id}` ? "저장 중…" : "변경 저장"}
                        </button>
                      </td>
                      <td>
                        <button
                          className="button button-ghost user-row-button"
                          disabled={disabled || pending !== null}
                          onClick={() => {
                            setResetUserId(resetUserId === user.id ? null : user.id);
                            setResetPassword("");
                            setResetConfirmation("");
                          }}
                          type="button"
                        >
                          재설정
                        </button>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
        {resetUserId ? (
          <PasswordReset
            confirmation={resetConfirmation}
            disabled={disabled || pending !== null}
            onCancel={() => {
              setResetUserId(null);
              setResetPassword("");
              setResetConfirmation("");
            }}
            onConfirmation={setResetConfirmation}
            onPassword={setResetPassword}
            onSubmit={() => {
              const user = users.find((candidate) => candidate.id === resetUserId);
              if (user) void resetPasswordFor(user);
            }}
            password={resetPassword}
            pending={pending === `password:${resetUserId}`}
            user={users.find((candidate) => candidate.id === resetUserId) ?? null}
          />
        ) : null}
      </section>
    </div>
  );
}

function PasswordReset({
  confirmation,
  disabled,
  onCancel,
  onConfirmation,
  onPassword,
  onSubmit,
  password,
  pending,
  user,
}: {
  confirmation: string;
  disabled: boolean;
  onCancel: () => void;
  onConfirmation: (value: string) => void;
  onPassword: (value: string) => void;
  onSubmit: () => void;
  password: string;
  pending: boolean;
  user: ManagedUser | null;
}) {
  if (!user) return null;
  return (
    <div className="user-password-panel">
      <div>
        <strong>{user.email} 비밀번호 재설정</strong>
        <p>16자 이상이며 계정명과 무관한 새 비밀번호를 입력합니다. 기존 로그인 토큰은 즉시 무효화됩니다.</p>
      </div>
      <label>
        <span>새 비밀번호</span>
        <input autoComplete="new-password" disabled={disabled} maxLength={1024} minLength={16} onChange={(event) => onPassword(event.target.value)} type="password" value={password} />
      </label>
      <label>
        <span>비밀번호 확인</span>
        <input autoComplete="new-password" disabled={disabled} maxLength={1024} minLength={16} onChange={(event) => onConfirmation(event.target.value)} type="password" value={confirmation} />
      </label>
      <div className="user-password-actions">
        <button className="button button-ghost" disabled={disabled} onClick={onCancel} type="button">취소</button>
        <button className="button button-danger" disabled={disabled} onClick={onSubmit} type="button">
          {pending ? "재설정 중…" : "비밀번호 재설정"}
        </button>
      </div>
    </div>
  );
}

type MutationResult =
  | { ok: true; user: NonNullable<ReturnType<typeof publicAdminUser>> }
  | { ok: false; code: string; status: number };

async function userMutation(
  path: string,
  method: "POST" | "PATCH",
  body: unknown,
): Promise<MutationResult> {
  const response = await fetch(path, {
    method,
    headers: {
      "Content-Type": "application/json",
      "Idempotency-Key": crypto.randomUUID(),
    },
    body: JSON.stringify(body),
  });
  const value: unknown = await response.json().catch(() => null);
  if (!response.ok) {
    const code = isRecord(value) && typeof value.error === "string" ? value.error : "request_failed";
    return { ok: false, code, status: response.status };
  }
  const user = publicAdminUser(value);
  if (!user) return { ok: false, code: "invalid_upstream_response", status: 502 };
  return { ok: true, user };
}

function handleFailure(
  result: Extract<MutationResult, { ok: false }>,
  setFeedback: (value: Feedback) => void,
  setUncertain: (value: boolean) => void,
) {
  if (result.code === "session_expired") {
    window.location.assign("/session/expired");
    return;
  }
  if (result.status === 502 || result.status === 503) {
    markUncertain(setFeedback, setUncertain);
    return;
  }
  const messages: Record<string, string> = {
    user_email_exists: "이미 등록된 이메일입니다.",
    stale_user: "다른 관리자가 먼저 변경했습니다. 목록을 새로고침해 주세요.",
    self_admin_change_forbidden: "현재 로그인한 관리자 계정은 스스로 비활성화하거나 강등할 수 없습니다.",
    last_active_admin_required: "최소 한 명의 활성 관리자가 필요합니다.",
    idempotency_conflict: "요청 식별자가 이전 요청과 충돌했습니다. 목록을 새로고침해 주세요.",
    weak_password: "관리자 비밀번호 정책을 만족하지 않습니다.",
    user_not_found: "사용자를 찾을 수 없습니다. 목록을 새로고침해 주세요.",
    session_expired: "세션이 만료되었습니다. 다시 로그인해 주세요.",
    forbidden: "관리자 권한이 필요합니다.",
    payload_too_large: "요청 크기가 허용 범위를 초과했습니다.",
    invalid_request: "입력 값을 확인해 주세요.",
  };
  if (["stale_user", "idempotency_conflict", "user_not_found"].includes(result.code)) {
    setUncertain(true);
    setFeedback({
      kind: "uncertain",
      text: messages[result.code] ?? "목록이 변경되었습니다. 새로고침해 최신 상태를 확인해 주세요.",
    });
    return;
  }
  setFeedback({ kind: "error", text: messages[result.code] ?? "사용자 관리 요청을 처리하지 못했습니다." });
}

function markUncertain(
  setFeedback: (value: Feedback) => void,
  setUncertain: (value: boolean) => void,
) {
  setUncertain(true);
  setFeedback({
    kind: "uncertain",
    text: "응답을 확인하지 못해 실행 결과가 불명확합니다. 같은 요청을 다시 보내지 말고 목록을 새로고침해 확인해 주세요.",
  });
}

function toManagedUser(user: NonNullable<ReturnType<typeof publicAdminUser>>): ManagedUser {
  return {
    id: user.id,
    email: user.email,
    role: user.role,
    active: user.active,
    rowVersion: user.row_version,
    createdAt: user.created_at,
    updatedAt: user.updated_at,
  };
}

function draftOf(user: ManagedUser): Draft {
  return { role: user.role, active: user.active };
}

function compareUsers(left: ManagedUser, right: ManagedUser): number {
  return left.email.localeCompare(right.email, "ko");
}

function validEmail(value: string): boolean {
  return value.length >= 3 && value.length <= 320 && /^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(value);
}

function validPassword(value: string): boolean {
  return value.length >= 16 && value.length <= 1_024;
}

function formatDate(value: string): string {
  return new Intl.DateTimeFormat("ko-KR", {
    dateStyle: "short",
    timeStyle: "short",
  }).format(new Date(value));
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
