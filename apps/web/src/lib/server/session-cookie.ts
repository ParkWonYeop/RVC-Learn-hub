import "server-only";

import type { NextRequest, NextResponse } from "next/server";
import { trustedPublicProtocol } from "./request-security";

export const SESSION_COOKIE_NAME = "rvc_manager_session";

function secureCookie(request: NextRequest): boolean {
  if (process.env.PUBLIC_SCHEME !== undefined) {
    return trustedPublicProtocol(request) === "https";
  }
  const configured = process.env.SESSION_COOKIE_SECURE;
  if (configured === "true") return true;
  if (configured === "false") return false;
  return request.nextUrl.protocol === "https:";
}

export function sessionCookieOptions(maxAge: number, request: NextRequest) {
  return {
    httpOnly: true,
    secure: secureCookie(request),
    sameSite: "strict" as const,
    path: "/",
    maxAge,
    priority: "high" as const,
  };
}

export function clearSessionCookie(response: NextResponse, request: NextRequest): void {
  response.cookies.set(SESSION_COOKIE_NAME, "", {
    ...sessionCookieOptions(0, request),
    expires: new Date(0),
  });
}
