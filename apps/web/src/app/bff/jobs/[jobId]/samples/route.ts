import { NextRequest, type NextResponse } from "next/server";
import {
  allowlistedQuery,
  boundedIntegerRule,
  isSafeResourceId,
  queryRules,
} from "@/lib/server/bff-security";
import {
  bffError,
  proxyManagerProjectedJson,
} from "@/lib/server/bff-proxy";
import {
  projectSampleList,
  SAMPLE_LIST_RESPONSE_MAX_BYTES,
} from "@/lib/server/sample-bff";

const rules = {
  offset: queryRules.unsignedInteger,
  limit: boundedIntegerRule(1, 200),
  attempt_id: queryRules.uuid,
  include_history: queryRules.boolean,
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
  const path: `/api/v1/${string}` =
    `/api/v1/jobs/${encodeURIComponent(jobId)}/samples${serialized ? `?${serialized}` : ""}`;
  return proxyManagerProjectedJson(
    request,
    path,
    (value) => projectSampleList(value, jobId),
    SAMPLE_LIST_RESPONSE_MAX_BYTES,
  );
}
