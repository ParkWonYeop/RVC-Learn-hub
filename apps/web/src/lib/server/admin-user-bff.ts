import "server-only";

import { NextRequest, NextResponse } from "next/server";
import type {
  ApiAdminUserCreateRequest,
  ApiAdminUserPasswordResetRequest,
  ApiAdminUserUpdateRequest,
} from "@/lib/api-types";
import { publicAdminUser } from "@/lib/admin-user-projections";
import { bffError } from "./bff-proxy";
import { managerRawRequest } from "./manager-api";
import { isSameOriginMutation } from "./request-security";
import { SESSION_COOKIE_NAME } from "./session-cookie";

const MAX_BODY_BYTES = 4_096;
const privateNoStore = "private, no-cache, no-store, must-revalidate";
const idempotencyKeyPattern = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;
const emailPattern = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;

export async function createAdminUser(request: NextRequest): Promise<NextResponse> {
  const guarded = await guardedMutation(request, adminUserCreateRequest);
  if (guarded instanceof NextResponse) return guarded;
  return mutate(
    request,
    "/api/v1/admin/users",
    "POST",
    guarded.payload,
    guarded.idempotencyKey,
    201,
  );
}

export async function updateAdminUser(
  request: NextRequest,
  userId: string,
): Promise<NextResponse> {
  const guarded = await guardedMutation(request, adminUserUpdateRequest);
  if (guarded instanceof NextResponse) return guarded;
  return mutate(
    request,
    `/api/v1/admin/users/${encodeURIComponent(userId)}`,
    "PATCH",
    guarded.payload,
    guarded.idempotencyKey,
    200,
  );
}

export async function resetAdminUserPassword(
  request: NextRequest,
  userId: string,
): Promise<NextResponse> {
  const guarded = await guardedMutation(request, adminUserPasswordResetRequest);
  if (guarded instanceof NextResponse) return guarded;
  return mutate(
    request,
    `/api/v1/admin/users/${encodeURIComponent(userId)}/password-reset`,
    "POST",
    guarded.payload,
    guarded.idempotencyKey,
    200,
  );
}

async function guardedMutation<T>(
  request: NextRequest,
  parse: (value: unknown) => T | null,
): Promise<{ payload: T; idempotencyKey: string } | NextResponse> {
  if (!isSameOriginMutation(request)) return bffError("forbidden", 403);
  if (process.env.DASHBOARD_DEMO_MODE === "true") {
    return bffError("demo_mode_read_only", 409);
  }
  const idempotencyKey = request.headers.get("idempotency-key");
  if (!idempotencyKey || !idempotencyKeyPattern.test(idempotencyKey)) {
    return bffError("invalid_idempotency_key", 400);
  }
  const body = await readBoundedJson(request, MAX_BODY_BYTES);
  if (!body.ok) {
    return bffError(
      body.tooLarge ? "payload_too_large" : "invalid_request",
      body.tooLarge ? 413 : 400,
    );
  }
  const payload = parse(body.value);
  return payload ? { payload, idempotencyKey } : bffError("invalid_request", 400);
}

async function mutate(
  request: NextRequest,
  path: `/api/v1/${string}`,
  method: "POST" | "PATCH",
  body: unknown,
  idempotencyKey: string,
  expectedStatus: 200 | 201,
): Promise<NextResponse> {
  const token = request.cookies.get(SESSION_COOKIE_NAME)?.value;
  if (!token) return bffError("session_required", 401, request);
  let upstream: Response;
  try {
    ({ response: upstream } = await managerRawRequest(path, {
      body,
      idempotencyKey,
      method,
      signal: request.signal,
      token,
    }));
  } catch (error) {
    if (request.signal.aborted) throw error;
    return bffError("manager_unavailable", 502);
  }
  if (!upstream.ok) return mutationError(request, upstream);
  if (upstream.status !== expectedStatus) {
    await cancelBody(upstream.body);
    return bffError("invalid_upstream_response", 502);
  }
  const user = publicAdminUser(await readJson(upstream));
  if (!user) return bffError("invalid_upstream_response", 502);
  const response = privateJson(user, upstream.status);
  if (upstream.headers.get("idempotency-replayed") === "true") {
    response.headers.set("Idempotency-Replayed", "true");
  }
  return response;
}

function adminUserCreateRequest(value: unknown): ApiAdminUserCreateRequest | null {
  if (!hasExactKeys(value, ["email", "password", "role", "active"])) return null;
  if (
    !validEmail(value.email) ||
    !validPassword(value.password) ||
    (value.role !== "admin" && value.role !== "user") ||
    typeof value.active !== "boolean"
  ) {
    return null;
  }
  return {
    email: value.email,
    password: value.password,
    role: value.role,
    active: value.active,
  };
}

function adminUserUpdateRequest(value: unknown): ApiAdminUserUpdateRequest | null {
  if (!hasExactKeys(value, ["expected_row_version", "role", "active"])) return null;
  if (
    !integerInRange(value.expected_row_version, 1, 2_147_483_647) ||
    (value.role !== "admin" && value.role !== "user") ||
    typeof value.active !== "boolean"
  ) {
    return null;
  }
  return {
    expected_row_version: value.expected_row_version,
    role: value.role,
    active: value.active,
  };
}

function adminUserPasswordResetRequest(
  value: unknown,
): ApiAdminUserPasswordResetRequest | null {
  if (!hasExactKeys(value, ["expected_row_version", "new_password"])) return null;
  if (
    !integerInRange(value.expected_row_version, 1, 2_147_483_647) ||
    !validPassword(value.new_password)
  ) {
    return null;
  }
  return {
    expected_row_version: value.expected_row_version,
    new_password: value.new_password,
  };
}

async function mutationError(
  request: NextRequest,
  upstream: Response,
): Promise<NextResponse> {
  const payload = await readJson(upstream);
  const detail = isRecord(payload) && typeof payload.detail === "string"
    ? payload.detail
    : null;
  const conflicts: Record<string, string> = {
    "user email already exists": "user_email_exists",
    "user changed; refresh and retry": "stale_user",
    "administrators cannot disable or demote their own account": "self_admin_change_forbidden",
    "at least one active administrator is required": "last_active_admin_required",
    "idempotency key conflicts with a prior user lifecycle request": "idempotency_conflict",
  };
  let status = upstream.status;
  let code = "invalid_upstream_response";
  if (status === 401) code = "session_expired";
  else if (status === 403) code = "forbidden";
  else if (status === 404) code = "user_not_found";
  else if (status === 409) code = detail ? (conflicts[detail] ?? "conflict") : "conflict";
  else if (status === 413) code = "payload_too_large";
  else if (status === 422) {
    code = detail === "password does not satisfy the administrator password policy"
      ? "weak_password"
      : "invalid_request";
  } else if (status === 429) code = "rate_limited";
  else if (status === 503) code = "manager_unavailable";
  else status = 502;
  const response = bffError(code, status, request);
  const retryAfter = upstream.headers.get("retry-after");
  if (retryAfter && /^(0|[1-9][0-9]{0,5})$/.test(retryAfter)) {
    response.headers.set("Retry-After", retryAfter);
  }
  return response;
}

function validEmail(value: unknown): value is string {
  return typeof value === "string" &&
    value.length >= 3 &&
    value.length <= 320 &&
    value === value.trim() &&
    !/[\u0000-\u001f\u007f]/.test(value) &&
    emailPattern.test(value);
}

function validPassword(value: unknown): value is string {
  return typeof value === "string" && value.length >= 16 && value.length <= 1_024;
}

function hasExactKeys<T extends readonly string[]>(
  value: unknown,
  keys: T,
): value is Record<T[number], unknown> {
  if (!isRecord(value)) return false;
  const actual = Object.keys(value);
  return actual.length === keys.length && actual.every((key) => keys.includes(key));
}

function integerInRange(
  value: unknown,
  minimum: number,
  maximum: number,
): value is number {
  return typeof value === "number" &&
    Number.isSafeInteger(value) &&
    value >= minimum &&
    value <= maximum;
}

async function readBoundedJson(
  request: NextRequest,
  maximumBytes: number,
): Promise<{ ok: true; value: unknown } | { ok: false; tooLarge: boolean }> {
  const mediaType = request.headers.get("content-type")?.split(";", 1)[0]?.trim().toLowerCase();
  if (mediaType !== "application/json") return { ok: false, tooLarge: false };
  const declared = request.headers.get("content-length");
  if (declared && (!/^(0|[1-9][0-9]*)$/.test(declared) || Number(declared) > maximumBytes)) {
    return { ok: false, tooLarge: true };
  }
  if (!request.body) return { ok: false, tooLarge: false };
  const reader = request.body.getReader();
  const chunks: Uint8Array[] = [];
  let length = 0;
  try {
    while (true) {
      const chunk = await reader.read();
      if (chunk.done) break;
      length += chunk.value.byteLength;
      if (length > maximumBytes) {
        await reader.cancel("body limit exceeded");
        return { ok: false, tooLarge: true };
      }
      chunks.push(chunk.value);
    }
  } catch {
    return { ok: false, tooLarge: false };
  }
  const bytes = new Uint8Array(length);
  let offset = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, offset);
    offset += chunk.byteLength;
  }
  try {
    return {
      ok: true,
      value: JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes)),
    };
  } catch {
    return { ok: false, tooLarge: false };
  }
}

async function readJson(response: Response): Promise<unknown> {
  if (!response.headers.get("content-type")?.startsWith("application/json")) {
    await cancelBody(response.body);
    return null;
  }
  try {
    return await response.json();
  } catch {
    return null;
  }
}

function privateJson(body: unknown, status: number): NextResponse {
  const response = NextResponse.json(body, { status });
  response.headers.set("Cache-Control", privateNoStore);
  response.headers.set("Pragma", "no-cache");
  response.headers.set("Vary", "Cookie");
  response.headers.set("X-Content-Type-Options", "nosniff");
  return response;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

async function cancelBody(body: ReadableStream<Uint8Array> | null): Promise<void> {
  if (!body) return;
  try {
    await body.cancel();
  } catch {
    // A fully consumed response body is already closed.
  }
}
