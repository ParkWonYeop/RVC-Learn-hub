import { NextRequest, NextResponse } from "next/server";
import {
  ManagerUnauthorizedError,
  managerRequest,
} from "@/lib/server/manager-api";
import { isSameOriginMutation } from "@/lib/server/request-security";
import {
  SESSION_COOKIE_NAME,
  clearSessionCookie,
} from "@/lib/server/session-cookie";

export async function POST(request: NextRequest): Promise<NextResponse> {
  if (!isSameOriginMutation(request)) {
    return noStoreJson({ error: "forbidden" }, 403);
  }
  const token = request.cookies.get(SESSION_COOKIE_NAME)?.value;
  let upstreamFailed = false;
  if (token) {
    try {
      await managerRequest<null>("/api/v1/auth/logout", { method: "POST", token });
    } catch (error) {
      upstreamFailed = !(error instanceof ManagerUnauthorizedError);
    }
  }
  const response = upstreamFailed
    ? noStoreJson({ error: "revocation_unavailable" }, 502)
    : new NextResponse(null, { status: 204 });
  response.headers.set("Cache-Control", "no-store");
  clearSessionCookie(response, request);
  return response;
}

function noStoreJson(body: Record<string, unknown>, status: number): NextResponse {
  const response = NextResponse.json(body, { status });
  response.headers.set("Cache-Control", "no-store");
  response.headers.set("Pragma", "no-cache");
  return response;
}
