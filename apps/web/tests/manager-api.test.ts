import { afterEach, describe, expect, it, vi } from "vitest";
import { managerRawRequest } from "@/lib/server/manager-api";

describe("Manager API server transport", () => {
  afterEach(() => {
    vi.unstubAllEnvs();
    vi.unstubAllGlobals();
  });

  it("forwards the server bearer and explicit idempotency key as separate fixed headers", async () => {
    const fetchMock = vi.fn().mockResolvedValue(Response.json({ ok: true }));
    vi.stubGlobal("fetch", fetchMock);
    vi.stubEnv("API_INTERNAL_URL", "http://api.internal:8000");

    await managerRawRequest("/api/v1/admin/users", {
      body: { email: "managed@example.test" },
      idempotencyKey: "create-managed-001",
      method: "POST",
      token: "server.jwt.only",
    });

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [target, init] = fetchMock.mock.calls[0] as [URL, RequestInit];
    const headers = new Headers(init.headers);
    expect(target.toString()).toBe("http://api.internal:8000/api/v1/admin/users");
    expect(init.method).toBe("POST");
    expect(init.body).toBe(JSON.stringify({ email: "managed@example.test" }));
    expect(headers.get("Authorization")).toBe("Bearer server.jwt.only");
    expect(headers.get("Idempotency-Key")).toBe("create-managed-001");
    expect(headers.get("Content-Type")).toBe("application/json");
  });
});
