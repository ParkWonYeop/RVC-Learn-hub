import { NextRequest } from "next/server";
import { beforeEach, describe, expect, it, vi } from "vitest";

const manager = vi.hoisted(() => ({ managerRawRequest: vi.fn() }));
vi.mock("@/lib/server/manager-api", () => manager);

import { GET as getSessionIdentity } from "@/app/bff/session/identity/route";

const TOKEN = "private.jwt.only.in.httponly.cookie";
const ACTOR_ID = "77777777-7777-4777-8777-777777777777";

describe("Session identity BFF", () => {
  beforeEach(() => manager.managerRawRequest.mockReset());

  it("projects only the current canonical actor id from the cookie session", async () => {
    manager.managerRawRequest.mockResolvedValue(rawResponse(Response.json(userFixture())));

    const response = await getSessionIdentity(readRequest(identityUrl()));
    const body = await response.json();

    expect(response.status).toBe(200);
    expect(body).toEqual({ actor_id: ACTOR_ID });
    expect(JSON.stringify(body)).not.toContain("actor@example.test");
    expect(JSON.stringify(body)).not.toContain(TOKEN);
    expect(response.headers.get("cache-control")).toContain("no-store");
    expect(response.headers.get("vary")).toBe("Cookie");
    expect(manager.managerRawRequest).toHaveBeenCalledWith(
      "/api/v1/auth/me",
      expect.objectContaining({ token: TOKEN }),
    );
  });

  it("rejects query injection, cross-origin reads, Authorization injection and missing sessions", async () => {
    const responses = await Promise.all([
      getSessionIdentity(readRequest(`${identityUrl()}?path=/api/v1/admin/users`)),
      getSessionIdentity(readRequest(identityUrl(), { forwardedHost: "other.test" })),
      getSessionIdentity(readRequest(identityUrl(), { browserAuthorization: "Bearer injected" })),
      getSessionIdentity(readRequest(identityUrl(), { cookie: false })),
    ]);

    expect(responses.map((response) => response.status)).toEqual([400, 403, 400, 401]);
    expect(manager.managerRawRequest).not.toHaveBeenCalled();
  });

  it.each([
    [{ ...userFixture(), private_token: "secret" }],
    [{ ...userFixture(), id: "NOT-A-UUID" }],
    [{ ...userFixture(), disabled: true }],
    [{ ...userFixture(), role: "worker" }],
  ])("fails closed on a malformed upstream identity projection", async (payload) => {
    manager.managerRawRequest.mockResolvedValue(rawResponse(Response.json(payload)));

    const response = await getSessionIdentity(readRequest(identityUrl()));

    expect(response.status).toBe(502);
    expect(await response.json()).toEqual({ error: "invalid_upstream_response" });
  });
});

function identityUrl(): string {
  return "https://manager.test/bff/session/identity";
}

function userFixture() {
  return {
    id: ACTOR_ID,
    email: "actor@example.test",
    role: "admin",
    disabled: false,
    created_at: "2026-07-13T00:00:00Z",
    updated_at: "2026-07-13T00:00:00Z",
  };
}

function readRequest(
  url: string,
  options: {
    browserAuthorization?: string;
    cookie?: boolean;
    forwardedHost?: string;
  } = {},
): NextRequest {
  const headers = new Headers({
    Host: "manager.test",
    Origin: "https://manager.test",
    "Sec-Fetch-Site": "same-origin",
    "X-Forwarded-Host": options.forwardedHost ?? "manager.test",
    "X-Forwarded-Proto": "https",
  });
  if (options.cookie !== false) headers.set("Cookie", `rvc_manager_session=${TOKEN}`);
  if (options.browserAuthorization) headers.set("Authorization", options.browserAuthorization);
  return new NextRequest(url, { headers });
}

function rawResponse(response: Response) {
  return { response, requestId: "request-1" };
}
