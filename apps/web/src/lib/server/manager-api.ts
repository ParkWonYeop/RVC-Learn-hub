import "server-only";

import { randomUUID } from "node:crypto";

export class ManagerApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly requestId: string,
  ) {
    super(message);
    this.name = "ManagerApiError";
  }
}

export class ManagerUnauthorizedError extends ManagerApiError {}
export class ManagerForbiddenError extends ManagerApiError {}

interface ManagerRequestOptions {
  method?: "GET" | "POST" | "PATCH" | "DELETE";
  token?: string;
  body?: unknown;
}

export interface ManagerRawRequestOptions extends ManagerRequestOptions {
  accept?:
    | "application/json"
    | "text/event-stream"
    | "application/octet-stream"
    | "audio/wav";
  lastEventId?: string;
  range?: string;
  ifRange?: string;
  idempotencyKey?: string;
  expectedActorId?: string;
  redirect?: RequestRedirect;
  signal?: AbortSignal;
  timeoutMs?: number | null;
}

export interface ManagerRawResponse {
  response: Response;
  requestId: string;
}

export async function managerRequest<T>(
  path: `/api/v1/${string}`,
  options: ManagerRequestOptions = {},
): Promise<T> {
  const { response, requestId } = await managerRawRequest(path, options);
  const responseRequestId = response.headers.get("X-Request-ID") ?? requestId;
  const payload = await readJson(response);
  if (response.status === 401) {
    throw new ManagerUnauthorizedError("인증 세션이 만료되었습니다.", 401, responseRequestId);
  }
  if (response.status === 403) {
    throw new ManagerForbiddenError("이 리소스를 볼 권한이 없습니다.", 403, responseRequestId);
  }
  if (!response.ok) {
    throw new ManagerApiError(errorDetail(payload), response.status, responseRequestId);
  }
  return payload as T;
}

export async function managerRawRequest(
  path: `/api/v1/${string}`,
  options: ManagerRawRequestOptions = {},
): Promise<ManagerRawResponse> {
  const requestId = randomUUID();
  const headers = new Headers({
    Accept: options.accept ?? "application/json",
    "X-Request-ID": requestId,
  });
  if (options.token) headers.set("Authorization", `Bearer ${options.token}`);
  if (options.body !== undefined) headers.set("Content-Type", "application/json");
  if (options.lastEventId) headers.set("Last-Event-ID", options.lastEventId);
  if (options.range) headers.set("Range", options.range);
  if (options.ifRange) headers.set("If-Range", options.ifRange);
  if (options.idempotencyKey) headers.set("Idempotency-Key", options.idempotencyKey);
  if (options.expectedActorId) headers.set("X-RVC-Expected-Actor-ID", options.expectedActorId);

  const timeoutMs = options.timeoutMs === undefined ? 10_000 : options.timeoutMs;
  const timeoutSignal = timeoutMs === null ? null : AbortSignal.timeout(timeoutMs);
  const signal =
    options.signal && timeoutSignal
      ? AbortSignal.any([options.signal, timeoutSignal])
      : (options.signal ?? timeoutSignal ?? undefined);

  let response: Response;
  try {
    response = await fetch(new URL(path, `${managerBaseUrl()}/`), {
      method: options.method ?? "GET",
      headers,
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
      cache: "no-store",
      redirect: options.redirect ?? "follow",
      signal,
    });
  } catch (error) {
    if (options.signal?.aborted) throw error;
    throw new ManagerApiError("Manager API에 연결할 수 없습니다.", 503, requestId);
  }
  return { response, requestId };
}

function managerBaseUrl(): string {
  const configured = process.env.API_INTERNAL_URL ?? "http://127.0.0.1:8000";
  let parsed: URL;
  try {
    parsed = new URL(configured);
  } catch {
    throw new Error("API_INTERNAL_URL must be an absolute HTTP(S) URL");
  }
  if (!['http:', 'https:'].includes(parsed.protocol)) {
    throw new Error("API_INTERNAL_URL must use HTTP or HTTPS");
  }
  return configured.replace(/\/$/, "");
}

async function readJson(response: Response): Promise<unknown> {
  if (response.status === 204) return null;
  try {
    return await response.json();
  } catch {
    if (response.ok) {
      throw new ManagerApiError(
        "Manager API 응답 형식이 올바르지 않습니다.",
        502,
        response.headers.get("X-Request-ID") ?? "unknown",
      );
    }
    return null;
  }
}

function errorDetail(payload: unknown): string {
  if (
    typeof payload === "object" &&
    payload !== null &&
    "detail" in payload &&
    typeof payload.detail === "string"
  ) {
    return payload.detail.slice(0, 300);
  }
  return "Manager API 요청을 처리하지 못했습니다.";
}
