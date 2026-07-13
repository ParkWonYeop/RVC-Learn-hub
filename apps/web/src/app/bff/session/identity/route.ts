import { NextRequest, type NextResponse } from "next/server";
import { allowlistedQuery, queryRules } from "@/lib/server/bff-security";
import { bffError, proxyManagerProjectedJson } from "@/lib/server/bff-proxy";

const upstreamUserKeys = [
  "id",
  "email",
  "role",
  "disabled",
  "created_at",
  "updated_at",
] as const;

export async function GET(request: NextRequest): Promise<NextResponse> {
  if (allowlistedQuery(request, {}) === null) return bffError("invalid_request", 400);
  if (request.headers.has("authorization")) return bffError("invalid_request", 400);
  return proxyManagerProjectedJson(
    request,
    "/api/v1/auth/me",
    projectSessionIdentity,
    4_096,
  );
}

function projectSessionIdentity(value: unknown): { actor_id: string } | null {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return null;
  const record = value as Record<string, unknown>;
  const keys = Object.keys(record);
  if (
    keys.length !== upstreamUserKeys.length ||
    !keys.every((key) => upstreamUserKeys.includes(key as typeof upstreamUserKeys[number])) ||
    typeof record.id !== "string" ||
    !queryRules.uuid.validate(record.id) ||
    typeof record.email !== "string" ||
    !["admin", "user"].includes(String(record.role)) ||
    record.disabled !== false ||
    typeof record.created_at !== "string" ||
    typeof record.updated_at !== "string"
  ) {
    return null;
  }
  return { actor_id: record.id };
}
