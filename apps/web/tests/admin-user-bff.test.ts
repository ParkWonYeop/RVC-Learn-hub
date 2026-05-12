import { NextRequest } from "next/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const manager = vi.hoisted(() => ({ managerRawRequest: vi.fn() }));
vi.mock("@/lib/server/manager-api", () => manager);

import { POST as createUser } from "@/app/bff/admin/users/route";
import { PATCH as updateUser } from "@/app/bff/admin/users/[userId]/route";
import { POST as resetPassword } from "@/app/bff/admin/users/[userId]/password-reset/route";

const TOKEN = "server-only.jwt";
const USER_ID = "fd51afdd-3936-4c2e-8206-e7165961ca0b";
const context = { params: Promise.resolve({ userId: USER_ID }) };

describe("Admin user lifecycle BFF", () => {
  beforeEach(() => {
    manager.managerRawRequest.mockReset();
    vi.stubEnv("DASHBOARD_DEMO_MODE", "false");
  });

  afterEach(() => vi.unstubAllEnvs());

  it("creates through the fixed Manager path and forwards only the server token and idempotency key", async () => {
    manager.managerRawRequest.mockResolvedValue(rawResponse(Response.json(
      {
        ...userFixture(),
        password_hash: "argon-secret",
        auth_version: 91,
        internal_note: "private",
      },
      { status: 201, headers: { "Idempotency-Replayed": "true" } },
    )));
    const payload = {
      email: "new.user@example.test",
      password: "Violet-river!Clouds-924",
      role: "user",
      active: true,
    };
    const response = await createUser(mutationRequest(
      "https://manager.test/bff/admin/users",
      payload,
      { browserAuthorization: "Bearer browser-injected", idempotencyKey: "create-user-001" },
    ));
    const body = await response.json();

    expect(response.status).toBe(201);
    expect(response.headers.get("cache-control")).toContain("no-store");
    expect(response.headers.get("idempotency-replayed")).toBe("true");
    expect(body).toEqual(userFixture());
    expect(body.password_hash).toBeUndefined();
    expect(body.auth_version).toBeUndefined();
    expect(JSON.stringify(body)).not.toContain(TOKEN);
    expect(manager.managerRawRequest).toHaveBeenCalledWith(
      "/api/v1/admin/users",
      expect.objectContaining({
        body: payload,
        idempotencyKey: "create-user-001",
        method: "POST",
        token: TOKEN,
      }),
    );
    expect(manager.managerRawRequest.mock.calls[0]?.[1]).not.toHaveProperty("headers");
  });

  it("rejects cross-origin, arbitrary fields, missing/unsafe idempotency keys and oversized bodies", async () => {
    const payload = {
      email: "new.user@example.test",
      password: "Violet-river!Clouds-924",
      role: "user",
      active: true,
    };
    const responses = await Promise.all([
      createUser(mutationRequest("https://manager.test/bff/admin/users", payload, { forwardedHost: "other.test" })),
      createUser(mutationRequest("https://manager.test/bff/admin/users", { ...payload, manager_path: "/api/v1/admin/users/admin" })),
      createUser(mutationRequest("https://manager.test/bff/admin/users", payload, { omitIdempotencyKey: true })),
      createUser(mutationRequest("https://manager.test/bff/admin/users", payload, { idempotencyKey: "bad key" })),
      createUser(mutationRequest("https://manager.test/bff/admin/users", payload, { contentLength: "999999" })),
      createUser(mutationRequest("https://manager.test/bff/admin/users?path=/api/v1/workers", payload)),
    ]);

    expect(responses.map((response) => response.status)).toEqual([403, 400, 400, 400, 413, 400]);
    expect(manager.managerRawRequest).not.toHaveBeenCalled();
  });

  it("updates desired role/active state with row-version CAS on a fixed path", async () => {
    manager.managerRawRequest.mockResolvedValue(rawResponse(Response.json({
      ...userFixture(),
      role: "admin",
      row_version: 8,
      auth_version: 44,
    })));
    const payload = { expected_row_version: 7, role: "admin", active: true };
    const response = await updateUser(
      mutationRequest(
        `https://manager.test/bff/admin/users/${USER_ID}`,
        payload,
        { idempotencyKey: "update-user-001", method: "PATCH" },
      ),
      context,
    );

    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({ ...userFixture(), role: "admin", row_version: 8 });
    expect(manager.managerRawRequest).toHaveBeenCalledWith(
      `/api/v1/admin/users/${USER_ID}`,
      expect.objectContaining({
        body: payload,
        idempotencyKey: "update-user-001",
        method: "PATCH",
        token: TOKEN,
      }),
    );
  });

  it("resets a password without returning or storing the submitted secret", async () => {
    manager.managerRawRequest.mockResolvedValue(rawResponse(Response.json({
      ...userFixture(),
      row_version: 8,
      auth_version: 45,
      password_hash: "never-forward",
    })));
    const payload = {
      expected_row_version: 7,
      new_password: "another correct horse battery staple",
    };
    const response = await resetPassword(
      mutationRequest(
        `https://manager.test/bff/admin/users/${USER_ID}/password-reset`,
        payload,
        { idempotencyKey: "password-user-001" },
      ),
      context,
    );
    const body = await response.json();

    expect(response.status).toBe(200);
    expect(body).toEqual({ ...userFixture(), row_version: 8 });
    expect(JSON.stringify(body)).not.toContain(payload.new_password);
    expect(manager.managerRawRequest).toHaveBeenCalledWith(
      `/api/v1/admin/users/${USER_ID}/password-reset`,
      expect.objectContaining({
        body: payload,
        idempotencyKey: "password-user-001",
        method: "POST",
        token: TOKEN,
      }),
    );
  });

  it.each([
    ["user email already exists", "user_email_exists"],
    ["user changed; refresh and retry", "stale_user"],
    ["administrators cannot disable or demote their own account", "self_admin_change_forbidden"],
    ["at least one active administrator is required", "last_active_admin_required"],
    ["idempotency key conflicts with a prior user lifecycle request", "idempotency_conflict"],
    ["private implementation detail", "conflict"],
  ] as const)("maps a conflict without exposing its upstream detail: %s", async (detail, code) => {
    manager.managerRawRequest.mockResolvedValue(rawResponse(Response.json(
      { detail, password_hash: "private" },
      { status: 409 },
    )));
    const response = await updateUser(
      mutationRequest(
        `https://manager.test/bff/admin/users/${USER_ID}`,
        { expected_row_version: 7, role: "user", active: false },
        { idempotencyKey: `conflict-${code}`, method: "PATCH" },
      ),
      context,
    );

    expect(response.status).toBe(409);
    expect(await response.json()).toEqual({ error: code });
  });

  it("rejects malformed success responses and unsafe resource identifiers", async () => {
    manager.managerRawRequest.mockResolvedValue(rawResponse(Response.json({
      ...userFixture(),
      auth_version: 10,
      row_version: 0,
    })));
    const malformed = await updateUser(
      mutationRequest(
        `https://manager.test/bff/admin/users/${USER_ID}`,
        { expected_row_version: 7, role: "user", active: true },
        { idempotencyKey: "malformed-response", method: "PATCH" },
      ),
      context,
    );
    const unsafe = await updateUser(
      mutationRequest(
        "https://manager.test/bff/admin/users/bad%2Fpath",
        { expected_row_version: 7, role: "user", active: true },
        { idempotencyKey: "unsafe-path", method: "PATCH" },
      ),
      { params: Promise.resolve({ userId: "bad/path" }) },
    );

    expect(malformed.status).toBe(502);
    expect(unsafe.status).toBe(400);
    expect(manager.managerRawRequest).toHaveBeenCalledTimes(1);
  });

  it("enforces password length and exact reset shape before forwarding", async () => {
    const requests = [
      { expected_row_version: 7, new_password: "too-short" },
      { expected_row_version: 7, new_password: "Violet-river!Clouds-924", force_logout: false },
      { expected_row_version: 0, new_password: "Violet-river!Clouds-924" },
    ];
    const responses = await Promise.all(requests.map((payload, index) => resetPassword(
      mutationRequest(
        `https://manager.test/bff/admin/users/${USER_ID}/password-reset`,
        payload,
        { idempotencyKey: `bad-reset-${index}` },
      ),
      context,
    )));

    expect(responses.map((response) => response.status)).toEqual([400, 400, 400]);
    expect(manager.managerRawRequest).not.toHaveBeenCalled();
  });
});

function userFixture() {
  return {
    id: USER_ID,
    email: "new.user@example.test",
    role: "user" as const,
    active: true,
    row_version: 7,
    created_at: "2026-07-12T01:02:03Z",
    updated_at: "2026-07-12T01:02:03Z",
  };
}

function rawResponse(response: Response) {
  return { response, requestId: "request-1" };
}

function mutationRequest(
  url: string,
  body: unknown,
  options: {
    browserAuthorization?: string;
    contentLength?: string;
    forwardedHost?: string;
    idempotencyKey?: string;
    method?: "POST" | "PATCH";
    omitIdempotencyKey?: boolean;
  } = {},
) {
  const headers = new Headers({
    "Content-Type": "application/json",
    Cookie: `rvc_manager_session=${TOKEN}`,
    Host: "manager.test",
    Origin: "https://manager.test",
    "Sec-Fetch-Site": "same-origin",
    "X-Forwarded-Host": options.forwardedHost ?? "manager.test",
    "X-Forwarded-Proto": "https",
  });
  if (!options.omitIdempotencyKey) {
    headers.set("Idempotency-Key", options.idempotencyKey ?? "test-idempotency-key");
  }
  if (options.browserAuthorization) headers.set("Authorization", options.browserAuthorization);
  if (options.contentLength) headers.set("Content-Length", options.contentLength);
  return new NextRequest(url, {
    body: JSON.stringify(body),
    headers,
    method: options.method ?? "POST",
  });
}
