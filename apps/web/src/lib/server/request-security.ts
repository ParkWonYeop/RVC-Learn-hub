import "server-only";

import type { NextRequest } from "next/server";

export function isSameOriginMutation(request: NextRequest): boolean {
  const origin = request.headers.get("origin");
  if (!origin || !originMatchesForwardedHost(origin, request)) return false;
  const fetchSite = request.headers.get("sec-fetch-site");
  return fetchSite === null || fetchSite === "same-origin" || fetchSite === "none";
}

export function isSameOriginRead(request: NextRequest): boolean {
  const fetchSite = request.headers.get("sec-fetch-site");
  if (fetchSite !== "same-origin" && fetchSite !== "none") return false;
  const origin = request.headers.get("origin");
  return origin === null || originMatchesForwardedHost(origin, request);
}

function originMatchesForwardedHost(origin: string, request: NextRequest): boolean {
  let received: URL;
  let expected: URL;
  try {
    received = new URL(origin);
    expected = new URL(publicOrigin(request));
  } catch {
    return false;
  }
  return received.origin === expected.origin;
}

export function publicOrigin(request: NextRequest): string {
  const protocol = trustedPublicProtocol(request);
  const host =
    request.headers.get("x-forwarded-host")?.split(",")[0]?.trim() ??
    request.headers.get("host") ??
    request.nextUrl.host;
  if (protocol === null || !host || /[\s/\\]/.test(host)) {
    return "invalid:";
  }
  return `${protocol}://${host}`;
}

export function trustedPublicProtocol(request: NextRequest): "http" | "https" | null {
  const configured = process.env.PUBLIC_SCHEME;
  if (configured !== undefined) {
    return configured === "http" || configured === "https" ? configured : null;
  }
  const requestProtocol = request.nextUrl.protocol.replace(":", "");
  return requestProtocol === "http" || requestProtocol === "https" ? requestProtocol : null;
}

export function safeInternalPath(value: string | null | undefined): string {
  if (
    !value ||
    !value.startsWith("/") ||
    value.startsWith("//") ||
    /[\\\u0000-\u001f\u007f]/.test(value)
  ) {
    return "/";
  }
  return value;
}
