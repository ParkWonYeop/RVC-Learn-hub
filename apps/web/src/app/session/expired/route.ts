import { NextRequest, NextResponse } from "next/server";
import { safeInternalPath } from "@/lib/server/request-security";
import { clearSessionCookie } from "@/lib/server/session-cookie";

export function GET(request: NextRequest): NextResponse {
  const destination = new URL("http://relative.invalid/login");
  destination.searchParams.set("reason", "session_expired");
  const nextPath = safeInternalPath(request.nextUrl.searchParams.get("next"));
  if (nextPath !== "/") destination.searchParams.set("next", nextPath);
  const response = new NextResponse(null, {
    status: 303,
    headers: { Location: `${destination.pathname}${destination.search}` },
  });
  clearSessionCookie(response, request);
  response.headers.set("Cache-Control", "no-store");
  return response;
}
