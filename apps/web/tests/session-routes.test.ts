import { NextRequest } from "next/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => {
  class ManagerApiError extends Error {
    constructor(
      message: string,
      readonly status: number,
      readonly requestId: string,
    ) {
      super(message);
    }
  }
  class ManagerUnauthorizedError extends ManagerApiError {}
  return {
    ManagerApiError,
    ManagerUnauthorizedError,
    managerRequest: vi.fn(),
  };
});

vi.mock("@/lib/server/manager-api", () => api);

import { GET as expireSession } from "@/app/session/expired/route";
import { POST as login } from "@/app/session/login/route";
import { POST as logout } from "@/app/session/logout/route";
import { safeInternalPath } from "@/lib/server/request-security";

const TOKEN = "unit.test.jwt.value.that.must.never.enter.json";

describe("session route security", () => {
  beforeEach(() => {
    api.managerRequest.mockReset();
  });

  afterEach(() => {
    vi.unstubAllEnvs();
  });

  it("rejects cross-origin login before contacting Manager", async () => {
    const response = await login(
      jsonRequest("http://manager.test/session/login", {
        origin: "https://evil.test",
        fetchSite: "cross-site",
      }),
    );

    expect(response.status).toBe(403);
    expect(response.headers.get("cache-control")).toBe("no-store");
    expect(api.managerRequest).not.toHaveBeenCalled();
  });

  it("returns no token in JSON and stores an HttpOnly Strict cookie", async () => {
    api.managerRequest.mockResolvedValue({
      access_token: TOKEN,
      token_type: "bearer",
      expires_in: 900,
    });

    const response = await login(jsonRequest("http://manager.test/session/login"));
    const body = await response.json();
    const cookie = response.headers.get("set-cookie") ?? "";

    expect(response.status).toBe(200);
    expect(body).toEqual({ ok: true });
    expect(JSON.stringify(body)).not.toContain(TOKEN);
    expect(cookie).toContain("rvc_manager_session=");
    expect(cookie).toContain("HttpOnly");
    expect(cookie).toContain("SameSite=strict");
    expect(cookie).toContain("Path=/");
    expect(cookie).not.toContain("Secure");
  });

  it("sets Secure when the operator-owned public scheme is HTTPS", async () => {
    vi.stubEnv("PUBLIC_SCHEME", "https");
    vi.stubEnv("SESSION_COOKIE_SECURE", "false");
    api.managerRequest.mockResolvedValue({
      access_token: TOKEN,
      token_type: "bearer",
      expires_in: 900,
    });
    const request = jsonRequest("http://web:3000/session/login", {
      origin: "https://manager.test:8443",
      forwardedHost: "manager.test:8443",
      forwardedProtocol: "https",
    });

    const response = await login(request);

    expect(response.status).toBe(200);
    expect(response.headers.get("set-cookie")).toContain("Secure");
  });

  it("ignores a client-supplied forwarded protocol", async () => {
    vi.stubEnv("PUBLIC_SCHEME", "http");
    api.managerRequest.mockResolvedValue({
      access_token: TOKEN,
      token_type: "bearer",
      expires_in: 900,
    });
    const request = jsonRequest("http://manager.test/session/login", {
      forwardedHost: "manager.test",
      forwardedProtocol: "https",
    });

    const response = await login(request);

    expect(response.status).toBe(200);
    expect(response.headers.get("set-cookie")).not.toContain("Secure");
  });

  it("fails origin validation for an invalid configured public scheme", async () => {
    vi.stubEnv("PUBLIC_SCHEME", "https; injected");
    const response = await login(jsonRequest("http://manager.test/session/login"));

    expect(response.status).toBe(403);
    expect(api.managerRequest).not.toHaveBeenCalled();
  });

  it("revokes the bearer before returning a cleared cookie", async () => {
    api.managerRequest.mockResolvedValue(null);
    const request = jsonRequest("http://manager.test/session/logout", {
      cookie: `rvc_manager_session=${TOKEN}`,
    });

    const response = await logout(request);

    expect(response.status).toBe(204);
    expect(api.managerRequest).toHaveBeenCalledWith("/api/v1/auth/logout", {
      method: "POST",
      token: TOKEN,
    });
    expect(response.headers.get("set-cookie")).toContain("Max-Age=0");
    expect(response.headers.get("cache-control")).toBe("no-store");
  });

  it("uses a relative safe redirect and clears an expired session", () => {
    const request = new NextRequest(
      "http://manager.test/session/expired?next=https%3A%2F%2Fevil.test",
      { headers: { cookie: `rvc_manager_session=${TOKEN}` } },
    );

    const response = expireSession(request);

    expect(response.status).toBe(303);
    expect(response.headers.get("location")).toBe("/login?reason=session_expired");
    expect(response.headers.get("set-cookie")).toContain("Max-Age=0");
  });
});

describe("safeInternalPath", () => {
  it.each([undefined, null, "", "https://evil.test", "//evil.test", "/\\evil", "/a\u0000b"])(
    "rejects unsafe next value %s",
    (value) => {
      expect(safeInternalPath(value)).toBe("/");
    },
  );

  it("keeps an internal dashboard path", () => {
    expect(safeInternalPath("/jobs/job-1?tab=metrics")).toBe("/jobs/job-1?tab=metrics");
  });
});

function jsonRequest(
  url: string,
  options: {
    origin?: string;
    fetchSite?: string;
    forwardedHost?: string;
    forwardedProtocol?: string;
    cookie?: string;
  } = {},
): NextRequest {
  const parsed = new URL(url);
  return new NextRequest(url, {
    method: "POST",
    headers: {
      "content-type": "application/json",
      origin: options.origin ?? parsed.origin,
      "sec-fetch-site": options.fetchSite ?? "same-origin",
      ...(options.forwardedHost ? { "x-forwarded-host": options.forwardedHost } : {}),
      ...(options.forwardedProtocol
        ? { "x-forwarded-proto": options.forwardedProtocol }
        : {}),
      ...(options.cookie ? { cookie: options.cookie } : {}),
    },
    body: JSON.stringify({ email: "admin@example.com", password: "valid-test-password" }),
  });
}
