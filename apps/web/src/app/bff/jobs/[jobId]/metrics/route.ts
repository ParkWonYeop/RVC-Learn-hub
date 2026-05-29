import { NextRequest, type NextResponse } from "next/server";
import {
  allowlistedQuery,
  boundedIntegerRule,
  isSafeResourceId,
  queryRules,
} from "@/lib/server/bff-security";
import { bffError, proxyManagerJson } from "@/lib/server/bff-proxy";

const rules = {
  attempt_id: queryRules.identifier,
  key: queryRules.metricKey,
  epoch: queryRules.unsignedInteger,
  step: queryRules.unsignedInteger,
  offset: queryRules.unsignedInteger,
  tail: queryRules.boolean,
  limit: boundedIntegerRule(1, 500),
};

export async function GET(
  request: NextRequest,
  context: { params: Promise<{ jobId: string }> },
): Promise<NextResponse> {
  const { jobId } = await context.params;
  const query = allowlistedQuery(request, rules);
  if (!isSafeResourceId(jobId) || query === null) {
    return bffError("invalid_request", 400);
  }
  const serialized = query.toString();
  const path: `/api/v1/${string}` = `/api/v1/jobs/${encodeURIComponent(jobId)}/metrics${serialized ? `?${serialized}` : ""}`;
  return proxyManagerJson(request, path);
}
