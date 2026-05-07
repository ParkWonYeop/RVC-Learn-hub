import { NextRequest, NextResponse } from "next/server";
import type { ApiAccessToken } from "@/lib/api-types";
import {
  ManagerApiError,
  ManagerUnauthorizedError,
  managerRequest,
} from "@/lib/server/manager-api";
import { isSameOriginMutation } from "@/lib/server/request-security";
import {
  SESSION_COOKIE_NAME,
  sessionCookieOptions,
} from "@/lib/server/session-cookie";

export async function POST(request: NextRequest): Promise<NextResponse> {
  if (!isSameOriginMutation(request)) {
    return noStoreJson({ error: "forbidden" }, 403);
  }
  const credentials = await readCredentials(request);
  if (!credentials) {
    return noStoreJson({ error: "invalid_request" }, 400);
  }
  try {
    const session = await managerRequest<ApiAccessToken>("/api/v1/auth/login", {
      method: "POST",
      body: credentials,
    });
    if (
      typeof session.access_token !== "string" ||
      session.access_token.length < 20 ||
      !Number.isInteger(session.expires_in) ||
      session.expires_in <= 0
    ) {
      return noStoreJson({ error: "invalid_upstream_response" }, 502);
    }
    const response = noStoreJson({ ok: true }, 200);
    response.cookies.set(
      SESSION_COOKIE_NAME,
      session.access_token,
      sessionCookieOptions(Math.min(session.expires_in, 86_400), request),
    );
    return response;
  } catch (error) {
    if (error instanceof ManagerUnauthorizedError) {
      return noStoreJson({ error: "invalid_credentials" }, 401);
    }
    const status = error instanceof ManagerApiError ? 502 : 500;
    return noStoreJson({ error: "manager_unavailable" }, status);
  }
}

async function readCredentials(
  request: NextRequest,
): Promise<{ email: string; password: string } | null> {
  if (!request.headers.get("content-type")?.startsWith("application/json")) return null;
  try {
    const payload: unknown = await request.json();
    if (typeof payload !== "object" || payload === null) return null;
    const email = "email" in payload ? payload.email : null;
    const password = "password" in payload ? payload.password : null;
    if (
      typeof email !== "string" ||
      email.length < 3 ||
      email.length > 320 ||
      typeof password !== "string" ||
      password.length < 1 ||
      password.length > 1_024
    ) {
      return null;
    }
    return { email, password };
  } catch {
    return null;
  }
}

function noStoreJson(body: Record<string, unknown>, status: number): NextResponse {
  const response = NextResponse.json(body, { status });
  response.headers.set("Cache-Control", "no-store");
  response.headers.set("Pragma", "no-cache");
  return response;
}
