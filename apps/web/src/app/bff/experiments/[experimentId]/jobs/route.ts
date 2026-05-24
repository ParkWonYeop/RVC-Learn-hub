import { NextRequest, type NextResponse } from "next/server";
import {
  allowlistedQuery,
  boundedIntegerRule,
  isSafeResourceId,
  queryRules,
} from "@/lib/server/bff-security";
import { bffError } from "@/lib/server/bff-proxy";
import { proxyExperimentJobNames } from "@/lib/server/experiment-bff";

type Context = { params: Promise<{ experimentId: string }> };
const listRules = {
  offset: queryRules.unsignedInteger,
  limit: boundedIntegerRule(1, 200),
};

export async function GET(request: NextRequest, context: Context): Promise<NextResponse> {
  const { experimentId } = await context.params;
  const query = allowlistedQuery(request, listRules);
  if (!isSafeResourceId(experimentId) || query === null) {
    return bffError("invalid_request", 400);
  }
  query.set("experiment_id", experimentId);
  const path: `/api/v1/${string}` = `/api/v1/jobs?${query.toString()}`;
  return proxyExperimentJobNames(request, path);
}
