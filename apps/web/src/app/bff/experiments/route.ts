import { NextRequest, type NextResponse } from "next/server";
import { allowlistedQuery, boundedIntegerRule, queryRules } from "@/lib/server/bff-security";
import { bffError } from "@/lib/server/bff-proxy";
import { createExperiment, proxyExperimentList } from "@/lib/server/experiment-bff";

const listRules = {
  offset: queryRules.unsignedInteger,
  limit: boundedIntegerRule(1, 200),
};

export async function GET(request: NextRequest): Promise<NextResponse> {
  const query = allowlistedQuery(request, listRules);
  if (query === null) return bffError("invalid_request", 400);
  const serialized = query.toString();
  const path: `/api/v1/${string}` = `/api/v1/experiments${serialized ? `?${serialized}` : ""}`;
  return proxyExperimentList(request, path);
}

export async function POST(request: NextRequest): Promise<NextResponse> {
  if (allowlistedQuery(request, {}) === null) return bffError("invalid_request", 400);
  return createExperiment(request);
}
