import "server-only";

import type { ApiAdminUserList } from "@/lib/api-types";
import { publicAdminUserList } from "@/lib/admin-user-projections";
import type { ListResult, ManagedUser } from "@/lib/types";
import { authenticatedManagerRequest, dashboardDemoMode } from "./auth";

const PAGE_SIZE = 200;
const COLLECTION_LIMIT = 10_000;

export async function loadAdminUsers(): Promise<ListResult<ManagedUser>> {
  if (dashboardDemoMode()) return { items: [], total: 0 };
  const items: ManagedUser[] = [];
  const seen = new Set<string>();
  let expectedTotal: number | null = null;
  let offset = 0;

  while (true) {
    const value = await authenticatedManagerRequest<ApiAdminUserList>(
      `/api/v1/admin/users?offset=${offset}&limit=${PAGE_SIZE}`,
    );
    const page = publicAdminUserList(value);
    if (!page || page.offset !== offset || page.limit !== PAGE_SIZE) {
      throw new Error("Manager returned an invalid user pagination envelope");
    }
    if (expectedTotal === null) {
      expectedTotal = page.total;
      if (expectedTotal > COLLECTION_LIMIT) {
        return {
          items: [],
          total: expectedTotal,
          limitation: {
            reason: "item_limit_exceeded",
            maximum: COLLECTION_LIMIT,
            total: expectedTotal,
            resource: "users",
          },
        };
      }
    } else if (page.total !== expectedTotal) {
      throw new Error("User total changed during pagination; reload is required");
    }
    for (const user of page.items) {
      if (seen.has(user.id)) throw new Error("Manager returned a duplicate user identifier");
      seen.add(user.id);
      items.push({
        id: user.id,
        email: user.email,
        role: user.role,
        active: user.active,
        rowVersion: user.row_version,
        createdAt: user.created_at,
        updatedAt: user.updated_at,
      });
    }
    if (items.length === expectedTotal) return { items, total: expectedTotal };
    if (page.items.length === 0 || items.length > COLLECTION_LIMIT) {
      throw new Error("User pagination did not make bounded progress");
    }
    offset += page.items.length;
    if (offset > expectedTotal) throw new Error("User pagination exceeded its declared total");
  }
}
