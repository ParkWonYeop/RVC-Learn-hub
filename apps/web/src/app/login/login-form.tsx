"use client";

import { FormEvent, useState } from "react";
import { useRouter } from "next/navigation";

export function LoginForm({ nextPath }: { nextPath: string }) {
  const router = useRouter();
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setPending(true);
    setError(null);
    const form = new FormData(event.currentTarget);
    try {
      const response = await fetch("/session/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          email: form.get("email"),
          password: form.get("password"),
        }),
      });
      if (!response.ok) {
        setError(
          response.status === 401
            ? "이메일 또는 비밀번호를 확인해 주세요."
            : "Manager에 연결할 수 없습니다. 잠시 후 다시 시도해 주세요.",
        );
        return;
      }
      router.replace(nextPath);
      router.refresh();
    } catch {
      setError("로그인 요청을 전송하지 못했습니다. 네트워크 연결을 확인해 주세요.");
    } finally {
      setPending(false);
    }
  }

  return (
    <form className="login-form" onSubmit={submit}>
      <label>
        <span>이메일</span>
        <input
          name="email"
          type="email"
          autoComplete="username"
          maxLength={320}
          required
          autoFocus
        />
      </label>
      <label>
        <span>비밀번호</span>
        <input
          name="password"
          type="password"
          autoComplete="current-password"
          maxLength={1024}
          required
        />
      </label>
      {error ? (
        <p className="form-error" role="alert">
          {error}
        </p>
      ) : null}
      <button className="button button-primary login-submit" disabled={pending} type="submit">
        {pending ? "확인 중…" : "로그인"}
      </button>
    </form>
  );
}
