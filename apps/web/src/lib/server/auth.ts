import "server-only";

import { cookies } from "next/headers";
import { redirect } from "next/navigation";
import { cache } from "react";
import type { ApiUser } from "@/lib/api-types";
import type { UserSummary } from "@/lib/types";
import {
  ManagerUnauthorizedError,
  managerRequest,
} from "./manager-api";
import { SESSION_COOKIE_NAME } from "./session-cookie";

export const requireCurrentUser = cache(async (): Promise<UserSummary> => {
  const user = await authenticatedManagerRequest<ApiUser>("/api/v1/auth/me");
  return { id: user.id, email: user.email, role: user.role };
});

export async function authenticatedManagerRequest<T>(
  path: `/api/v1/${string}`,
): Promise<T> {
  return authenticatedRequest(path, "GET");
}

export async function authenticatedManagerMutation<T>(
  path: `/api/v1/${string}`,
): Promise<T> {
  return authenticatedRequest(path, "POST");
}

async function authenticatedRequest<T>(
  path: `/api/v1/${string}`,
  method: "GET" | "POST",
): Promise<T> {
  const token = (await cookies()).get(SESSION_COOKIE_NAME)?.value;
  if (!token) redirect("/login");
  try {
    return await managerRequest<T>(path, { token, method });
  } catch (error) {
    if (error instanceof ManagerUnauthorizedError) {
      redirect("/session/expired");
    }
    throw error;
  }
}

export function dashboardDemoMode(): boolean {
  return process.env.DASHBOARD_DEMO_MODE === "true";
}
