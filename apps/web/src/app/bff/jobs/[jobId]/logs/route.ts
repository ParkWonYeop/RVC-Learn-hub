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
  sequence_gte: queryRules.unsignedInteger,
  sequence_lte: queryRules.unsignedInteger,
  occurred_at_gte: queryRules.timestamp,
  occurred_at_lte: queryRules.timestamp,
  after: queryRules.cursor,
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
  return proxyManagerJson(
    request,
    withQuery(`/api/v1/jobs/${encodeURIComponent(jobId)}/logs`, query),
  );
}

function withQuery(path: `/api/v1/${string}`, query: URLSearchParams): `/api/v1/${string}` {
  const serialized = query.toString();
  return serialized ? `${path}?${serialized}` : path;
}
