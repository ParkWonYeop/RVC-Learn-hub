import { NextRequest, type NextResponse } from "next/server";
import {
  allowlistedQuery,
  boundedIntegerRule,
  enumRule,
  isSafeResourceId,
  queryRules,
} from "@/lib/server/bff-security";
import { bffError, proxyManagerJson } from "@/lib/server/bff-proxy";
import { artifactTypes } from "@/lib/api-types";

const rules = {
  artifact_type: enumRule(artifactTypes),
  offset: queryRules.unsignedInteger,
  limit: boundedIntegerRule(1, 200),
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
  const path: `/api/v1/${string}` = `/api/v1/jobs/${encodeURIComponent(jobId)}/artifacts${serialized ? `?${serialized}` : ""}`;
  return proxyManagerJson(request, path);
}
