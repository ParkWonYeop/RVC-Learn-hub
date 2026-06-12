import "server-only";

import { NextRequest, NextResponse } from "next/server";
import { ManagerApiError, managerRawRequest } from "./manager-api";
import { isSameOriginRead, publicOrigin } from "./request-security";
import {
  SESSION_COOKIE_NAME,
  clearSessionCookie,
} from "./session-cookie";
import { queryRules } from "./bff-security";

const privateNoStore = "private, no-cache, no-store, must-revalidate";

export function bffError(
  code: string,
  status: number,
  request?: NextRequest,
): NextResponse {
  const response = NextResponse.json({ error: code }, { status });
  setPrivateHeaders(response.headers);
  if (status === 401 && request) clearSessionCookie(response, request);
  return response;
}

export async function proxyManagerJson(
  request: NextRequest,
  path: `/api/v1/${string}`,
): Promise<NextResponse> {
  const guard = guardRead(request);
  if (guard) return guard;
  const token = request.cookies.get(SESSION_COOKIE_NAME)?.value;
  if (!token) return bffError("session_required", 401, request);

  let upstream: Response;
  try {
    ({ response: upstream } = await managerRawRequest(path, {
      token,
      signal: request.signal,
    }));
  } catch (error) {
    if (request.signal.aborted) throw error;
    return bffError(
      error instanceof ManagerApiError ? "manager_unavailable" : "proxy_failed",
      502,
    );
  }
  if (!upstream.ok) return upstreamError(request, upstream);
  if (!upstream.headers.get("content-type")?.startsWith("application/json")) {
    await cancelBody(upstream.body);
    return bffError("invalid_upstream_response", 502);
  }

  const headers = new Headers({ "Content-Type": "application/json; charset=utf-8" });
  setPrivateHeaders(headers);
  return new NextResponse(upstream.body, { status: 200, headers });
}

export async function proxyManagerProjectedJson<T>(
  request: NextRequest,
  path: `/api/v1/${string}`,
  project: (value: unknown) => T | null,
  maxResponseBytes: number,
): Promise<NextResponse> {
  const guard = guardRead(request);
  if (guard) return guard;
  const token = request.cookies.get(SESSION_COOKIE_NAME)?.value;
  if (!token) return bffError("session_required", 401, request);

  let upstream: Response;
  try {
    ({ response: upstream } = await managerRawRequest(path, {
      token,
      signal: request.signal,
    }));
  } catch (error) {
    if (request.signal.aborted) throw error;
    return bffError(
      error instanceof ManagerApiError ? "manager_unavailable" : "proxy_failed",
      502,
    );
  }
  if (!upstream.ok) return upstreamError(request, upstream);
  if (!upstream.headers.get("content-type")?.startsWith("application/json")) {
    await cancelBody(upstream.body);
    return bffError("invalid_upstream_response", 502);
  }
  const payload = await readBoundedJson(upstream, maxResponseBytes);
  if (payload === invalidJson) return bffError("invalid_upstream_response", 502);
  let projected: T | null;
  try {
    projected = project(payload);
  } catch {
    projected = null;
  }
  if (projected === null) return bffError("invalid_upstream_response", 502);
  const response = NextResponse.json(projected);
  setPrivateHeaders(response.headers);
  return response;
}

export async function proxyManagerEventStream(
  request: NextRequest,
  path: `/api/v1/${string}`,
): Promise<NextResponse> {
  const guard = guardRead(request);
  if (guard) return guard;
  const token = request.cookies.get(SESSION_COOKIE_NAME)?.value;
  if (!token) return bffError("session_required", 401, request);
  const lastEventId = request.headers.get("last-event-id");
  if (
    lastEventId !== null &&
    (lastEventId.length > queryRules.cursor.maxLength ||
      !queryRules.cursor.validate(lastEventId))
  ) {
    return bffError("invalid_last_event_id", 400);
  }

  const upstreamAbort = new AbortController();
  const stopForRequest = () => upstreamAbort.abort(request.signal.reason);
  if (request.signal.aborted) stopForRequest();
  else request.signal.addEventListener("abort", stopForRequest, { once: true });
  const cleanup = () => request.signal.removeEventListener("abort", stopForRequest);

  let upstream: Response;
  try {
    ({ response: upstream } = await managerRawRequest(path, {
      accept: "text/event-stream",
      lastEventId: lastEventId ?? undefined,
      signal: upstreamAbort.signal,
      timeoutMs: null,
      token,
    }));
  } catch (error) {
    cleanup();
    if (request.signal.aborted) throw error;
    return bffError(
      error instanceof ManagerApiError ? "manager_unavailable" : "stream_failed",
      502,
    );
  }
  if (!upstream.ok) {
    cleanup();
    upstreamAbort.abort();
    return upstreamError(request, upstream);
  }
  if (!upstream.body || !upstream.headers.get("content-type")?.startsWith("text/event-stream")) {
    cleanup();
    upstreamAbort.abort();
    await cancelBody(upstream.body);
    return bffError("invalid_upstream_stream", 502);
  }

  const headers = new Headers({
    "Cache-Control": privateNoStore,
    "Content-Security-Policy": "default-src 'none'",
    "Content-Type": "text/event-stream; charset=utf-8",
    "Referrer-Policy": "no-referrer",
    Vary: "Cookie",
    "X-Accel-Buffering": "no",
    "X-Content-Type-Options": "nosniff",
  });
  const body = relayReadableBody(upstream.body, upstreamAbort, cleanup);
  return new NextResponse(body, { status: 200, headers });
}

export async function proxyManagerDownload(
  request: NextRequest,
  path: `/api/v1/${string}`,
  options: {
    accept?: "application/octet-stream" | "audio/wav";
    allowRedirect?: boolean;
    expectedContentType?: string;
    inlineFilename?: string;
    maxContentLength?: number;
    forwardRange?: boolean;
  } = {},
): Promise<NextResponse> {
  const guard = guardRead(request);
  if (guard) return guard;
  const token = request.cookies.get(SESSION_COOKIE_NAME)?.value;
  if (!token) return bffError("session_required", 401, request);

  const upstreamAbort = new AbortController();
  const stopForRequest = () => upstreamAbort.abort(request.signal.reason);
  if (request.signal.aborted) stopForRequest();
  else request.signal.addEventListener("abort", stopForRequest, { once: true });
  const cleanup = () => request.signal.removeEventListener("abort", stopForRequest);
  const rangeHeaders = options.forwardRange ? validatedRangeHeaders(request) : {};
  if (rangeHeaders === null) {
    cleanup();
    upstreamAbort.abort();
    return bffError("invalid_range", 400);
  }

  let upstream: Response;
  try {
    ({ response: upstream } = await managerRawRequest(path, {
      accept: options.accept ?? "application/octet-stream",
      redirect: "manual",
      signal: upstreamAbort.signal,
      timeoutMs: null,
      token,
      ...rangeHeaders,
    }));
  } catch (error) {
    cleanup();
    if (request.signal.aborted) throw error;
    return bffError(
      error instanceof ManagerApiError ? "manager_unavailable" : "download_failed",
      502,
    );
  }

  if (upstream.status === 307) {
    if (options.allowRedirect === false) {
      await cancelBody(upstream.body);
      cleanup();
      upstreamAbort.abort();
      return bffError("unexpected_download_redirect", 502);
    }
    const location = safeDownloadLocation(request, upstream.headers.get("location"));
    await cancelBody(upstream.body);
    cleanup();
    upstreamAbort.abort();
    if (!location) return bffError("invalid_download_redirect", 502);
    const response = NextResponse.redirect(location, 307);
    setPrivateHeaders(response.headers);
    response.headers.set("Referrer-Policy", "no-referrer");
    return response;
  }
  if (upstream.status === 416 && rangeHeaders.range) {
    const total = unsatisfiedRangeTotal(
      upstream.headers.get("content-range"),
      options.maxContentLength,
    );
    await cancelBody(upstream.body);
    cleanup();
    upstreamAbort.abort();
    if (total === null) return bffError("invalid_upstream_download", 502);
    const response = bffError("range_not_satisfiable", 416);
    response.headers.set("Accept-Ranges", "bytes");
    response.headers.set("Content-Range", `bytes */${total}`);
    return response;
  }
  if (!upstream.ok) {
    cleanup();
    upstreamAbort.abort();
    return upstreamError(request, upstream);
  }
  if (upstream.status !== 200 && upstream.status !== 206) {
    cleanup();
    upstreamAbort.abort();
    await cancelBody(upstream.body);
    return bffError("invalid_upstream_download", 502);
  }
  if (!rangeHeaders.range && upstream.status === 206) {
    cleanup();
    upstreamAbort.abort();
    await cancelBody(upstream.body);
    return bffError("invalid_upstream_download", 502);
  }
  if (rangeHeaders.range && !rangeHeaders.ifRange && upstream.status !== 206) {
    cleanup();
    upstreamAbort.abort();
    await cancelBody(upstream.body);
    return bffError("range_not_supported", 502);
  }
  if (!upstream.body) {
    cleanup();
    upstreamAbort.abort();
    return bffError("invalid_upstream_download", 502);
  }

  const contentType = upstream.headers.get("content-type")?.split(";", 1)[0]?.trim();
  if (options.expectedContentType && contentType !== options.expectedContentType) {
    cleanup();
    upstreamAbort.abort();
    await cancelBody(upstream.body);
    return bffError("invalid_upstream_download", 502);
  }
  const contentLength = boundedContentLength(
    upstream.headers.get("content-length"),
    options.maxContentLength,
  );
  if (
    contentLength === null ||
    (options.maxContentLength !== undefined &&
      (contentLength === undefined || contentLength === 0))
  ) {
    cleanup();
    upstreamAbort.abort();
    await cancelBody(upstream.body);
    return bffError("invalid_upstream_download", 502);
  }

  const headers = new Headers({
    "Cache-Control": privateNoStore,
    "Content-Type": upstream.headers.get("content-type") ?? "application/octet-stream",
    Vary: "Cookie",
    "X-Content-Type-Options": "nosniff",
  });
  if (
    !copyValidatedRangeResponseHeaders(
      upstream,
      headers,
      options.maxContentLength,
    )
  ) {
    cleanup();
    upstreamAbort.abort();
    await cancelBody(upstream.body);
    return bffError("invalid_upstream_download", 502);
  }
  if (options.inlineFilename) {
    headers.set("Content-Disposition", `inline; filename="${options.inlineFilename}"`);
  } else {
    copyHeader(upstream.headers, headers, "content-disposition");
  }
  copyHeader(upstream.headers, headers, "content-length");
  headers.set("Referrer-Policy", "no-referrer");
  const body = relayReadableBody(upstream.body, upstreamAbort, cleanup);
  return new NextResponse(body, { status: upstream.status, headers });
}

function guardRead(request: NextRequest): NextResponse | null {
  return isSameOriginRead(request) ? null : bffError("forbidden", 403);
}

async function upstreamError(
  request: NextRequest,
  upstream: Response,
): Promise<NextResponse> {
  await cancelBody(upstream.body);
  const status = [401, 403, 404, 409, 422, 429, 503].includes(upstream.status)
    ? upstream.status
    : 502;
  const code =
    status === 401
      ? "session_expired"
      : status === 403
        ? "forbidden"
        : status === 404
          ? "not_found"
          : status === 409
            ? "conflict"
            : status === 422
              ? "invalid_query"
              : status === 429
                ? "rate_limited"
                : "manager_unavailable";
  return bffError(code, status, request);
}

function relayReadableBody(
  upstream: ReadableStream<Uint8Array>,
  upstreamAbort: AbortController,
  cleanup: () => void,
): ReadableStream<Uint8Array> {
  const reader = upstream.getReader();
  let finished = false;
  const finish = () => {
    if (finished) return;
    finished = true;
    cleanup();
  };
  return new ReadableStream<Uint8Array>({
    async pull(controller) {
      try {
        const result = await reader.read();
        if (result.done) {
          finish();
          controller.close();
          return;
        }
        controller.enqueue(result.value);
      } catch (error) {
        finish();
        upstreamAbort.abort(error);
        controller.error(error);
      }
    },
    async cancel(reason) {
      finish();
      upstreamAbort.abort(reason);
      try {
        await reader.cancel(reason);
      } catch {
        // The upstream fetch may already be aborted; cancellation is still complete.
      }
    },
  });
}

function safeDownloadLocation(request: NextRequest, value: string | null): URL | null {
  if (!value || value.length > 8_192) return null;
  let location: URL;
  let browserOrigin: URL;
  try {
    location = new URL(value);
    browserOrigin = new URL(publicOrigin(request));
  } catch {
    return null;
  }
  if (location.protocol !== "http:" && location.protocol !== "https:") return null;
  if (browserOrigin.protocol === "https:" && location.protocol !== "https:") return null;
  if (location.username || location.password || location.hash) return null;
  return location;
}

function setPrivateHeaders(headers: Headers): void {
  headers.set("Cache-Control", privateNoStore);
  headers.set("Pragma", "no-cache");
  headers.set("Vary", "Cookie");
  headers.set("X-Content-Type-Options", "nosniff");
}

function copyHeader(source: Headers, target: Headers, name: string): void {
  const value = source.get(name);
  if (value !== null) target.set(name, value);
}

async function cancelBody(body: ReadableStream<Uint8Array> | null): Promise<void> {
  if (!body) return;
  try {
    await body.cancel();
  } catch {
    // Closing an already-completed response body is harmless.
  }
}

const invalidJson = Symbol("invalid-json");

async function readBoundedJson(
  response: Response,
  maximumBytes: number,
): Promise<unknown | typeof invalidJson> {
  if (!Number.isSafeInteger(maximumBytes) || maximumBytes <= 0 || !response.body) {
    await cancelBody(response.body);
    return invalidJson;
  }
  const declared = boundedContentLength(
    response.headers.get("content-length"),
    maximumBytes,
  );
  if (declared === null) {
    await cancelBody(response.body);
    return invalidJson;
  }
  const reader = response.body.getReader();
  const chunks: Uint8Array[] = [];
  let total = 0;
  try {
    while (true) {
      const next = await reader.read();
      if (next.done) break;
      total += next.value.byteLength;
      if (total > maximumBytes) {
        await reader.cancel("bounded JSON response exceeded its limit");
        return invalidJson;
      }
      chunks.push(next.value);
    }
    if (declared !== undefined && total !== declared) return invalidJson;
    const joined = new Uint8Array(total);
    let offset = 0;
    for (const chunk of chunks) {
      joined.set(chunk, offset);
      offset += chunk.byteLength;
    }
    return JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(joined)) as unknown;
  } catch {
    try {
      await reader.cancel("invalid JSON response");
    } catch {
      // The body may already have completed.
    }
    return invalidJson;
  }
}

function boundedContentLength(
  rawValue: string | null,
  maximum: number | undefined,
): number | undefined | null {
  if (rawValue === null) return undefined;
  if (!/^(0|[1-9][0-9]*)$/.test(rawValue)) return null;
  const value = Number(rawValue);
  if (!Number.isSafeInteger(value) || (maximum !== undefined && value > maximum)) {
    return null;
  }
  return value;
}

function validatedRangeHeaders(
  request: NextRequest,
): { range?: string; ifRange?: string } | null {
  const range = request.headers.get("range");
  const ifRange = request.headers.get("if-range");
  if (range === null) return ifRange === null ? {} : null;
  if (range.length > 80 || !/^bytes=(?:[0-9]+-[0-9]*|-[0-9]+)$/.test(range)) {
    return null;
  }
  const [startValue, endValue] = range.slice(6).split("-");
  if (startValue === "") {
    const suffixLength = Number(endValue);
    if (!Number.isSafeInteger(suffixLength) || suffixLength <= 0) return null;
  } else {
    const start = Number(startValue);
    if (!Number.isSafeInteger(start)) return null;
    if (endValue !== "") {
      const end = Number(endValue);
      if (!Number.isSafeInteger(end) || start > end) return null;
    }
  }
  if (ifRange === null) return { range };
  if (ifRange.length > 200 || /[\u0000-\u001f\u007f]/.test(ifRange)) return null;
  const isEntityTag = /^"[\x21\x23-\x7e]{1,160}"$/.test(ifRange);
  const isHttpDate =
    /^(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun), [0-9]{2} (?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) [0-9]{4} [0-9]{2}:[0-9]{2}:[0-9]{2} GMT$/.test(
      ifRange,
    ) && Number.isFinite(Date.parse(ifRange));
  return isEntityTag || isHttpDate ? { range, ifRange } : null;
}

function copyValidatedRangeResponseHeaders(
  upstream: Response,
  target: Headers,
  maximumContentLength: number | undefined,
): boolean {
  const acceptRanges = upstream.headers.get("accept-ranges");
  if (acceptRanges !== null && acceptRanges !== "bytes") return false;
  if (upstream.status === 206 && acceptRanges !== "bytes") return false;
  if (acceptRanges === "bytes") target.set("Accept-Ranges", "bytes");

  const contentRange = upstream.headers.get("content-range");
  if (upstream.status === 206) {
    const match = contentRange?.match(
      /^bytes (0|[1-9][0-9]*)-(0|[1-9][0-9]*)\/([1-9][0-9]*)$/,
    );
    if (!match) return false;
    const start = Number(match[1]);
    const end = Number(match[2]);
    const total = Number(match[3]);
    const declaredLength = boundedContentLength(
      upstream.headers.get("content-length"),
      maximumContentLength,
    );
    if (
      !Number.isSafeInteger(start) ||
      !Number.isSafeInteger(end) ||
      !Number.isSafeInteger(total) ||
      start > end ||
      end >= total ||
      (maximumContentLength !== undefined && total > maximumContentLength) ||
      declaredLength === null ||
      (declaredLength !== undefined && declaredLength !== end - start + 1)
    ) {
      return false;
    }
    target.set("Content-Range", match[0]);
  } else if (contentRange !== null) {
    return false;
  }

  const etag = upstream.headers.get("etag");
  if (etag !== null) {
    if (etag.length > 200 || !/^(?:W\/)?"[\x21\x23-\x7e]{1,160}"$/.test(etag)) {
      return false;
    }
    target.set("ETag", etag);
  }
  return true;
}

function unsatisfiedRangeTotal(
  contentRange: string | null,
  maximumContentLength: number | undefined,
): number | null {
  const match = contentRange?.match(/^bytes \*\/([1-9][0-9]*)$/);
  if (!match) return null;
  const total = Number(match[1]);
  return Number.isSafeInteger(total) &&
    (maximumContentLength === undefined || total <= maximumContentLength)
    ? total
    : null;
}
