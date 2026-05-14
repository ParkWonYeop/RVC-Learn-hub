import { beforeEach, describe, expect, it, vi } from "vitest";

const auth = vi.hoisted(() => ({
  authenticatedManagerRequest: vi.fn(),
  dashboardDemoMode: vi.fn(),
}));
vi.mock("@/lib/server/auth", () => auth);

import { loadAdminUsers } from "@/lib/server/admin-user-data";

describe("Admin user directory loader", () => {
  beforeEach(() => {
    auth.authenticatedManagerRequest.mockReset();
    auth.dashboardDemoMode.mockReset().mockReturnValue(false);
  });

  it("loads all pages, validates the envelope and projects only public fields", async () => {
    auth.authenticatedManagerRequest.mockImplementation(async (path: string) => {
      const offset = Number(new URL(path, "https://manager.test").searchParams.get("offset"));
      const all = [user("user-1", "one@example.test"), user("user-2", "two@example.test")];
      return {
        items: all.slice(offset, offset + 1).map((item) => ({
          ...item,
          password_hash: "private",
          access_token_version: 99,
        })),
        total: 2,
        offset,
        limit: 200,
      };
    });

    const result = await loadAdminUsers();

    expect(result).toEqual({
      items: [
        managedUser("user-1", "one@example.test"),
        managedUser("user-2", "two@example.test"),
      ],
      total: 2,
    });
    expect(JSON.stringify(result)).not.toContain("password_hash");
    expect(auth.authenticatedManagerRequest).toHaveBeenNthCalledWith(
      1,
      "/api/v1/admin/users?offset=0&limit=200",
    );
    expect(auth.authenticatedManagerRequest).toHaveBeenNthCalledWith(
      2,
      "/api/v1/admin/users?offset=1&limit=200",
    );
  });

  it("fails closed on a duplicate identifier or malformed row version", async () => {
    auth.authenticatedManagerRequest
      .mockResolvedValueOnce({
        items: [user("user-1", "one@example.test")],
        total: 2,
        offset: 0,
        limit: 200,
      })
      .mockResolvedValueOnce({
        items: [user("user-1", "one@example.test")],
        total: 2,
        offset: 1,
        limit: 200,
      });
    await expect(loadAdminUsers()).rejects.toThrow("duplicate user identifier");

    auth.authenticatedManagerRequest.mockReset().mockResolvedValue({
      items: [{ ...user("user-1", "one@example.test"), row_version: 0 }],
      total: 1,
      offset: 0,
      limit: 200,
    });
    await expect(loadAdminUsers()).rejects.toThrow("invalid user pagination envelope");
  });

  it("does not call Manager in read-only Demo mode", async () => {
    auth.dashboardDemoMode.mockReturnValue(true);

    await expect(loadAdminUsers()).resolves.toEqual({ items: [], total: 0 });
    expect(auth.authenticatedManagerRequest).not.toHaveBeenCalled();
  });
});

function user(id: string, email: string) {
  return {
    id,
    email,
    role: "user",
    active: true,
    row_version: 3,
    created_at: "2026-07-12T01:02:03Z",
    updated_at: "2026-07-12T01:02:03Z",
  };
}

function managedUser(id: string, email: string) {
  return {
    id,
    email,
    role: "user",
    active: true,
    rowVersion: 3,
    createdAt: "2026-07-12T01:02:03Z",
    updatedAt: "2026-07-12T01:02:03Z",
  };
}
