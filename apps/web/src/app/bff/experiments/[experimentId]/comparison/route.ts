import { NextRequest, type NextResponse } from "next/server";
import { bffError } from "@/lib/server/bff-proxy";
import { proxyExperimentComparison } from "@/lib/server/experiment-bff";

type Context = { params: Promise<{ experimentId: string }> };

const MAX_COMPARISON_QUERY_BYTES = 1_024;
const canonicalUuidPattern =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

export async function GET(request: NextRequest, context: Context): Promise<NextResponse> {
  const { experimentId } = await context.params;
  const jobIds = comparisonJobIds(request);
  if (!canonicalUuidPattern.test(experimentId) || jobIds === null) {
    return bffError("invalid_request", 400);
  }

  const query = new URLSearchParams();
  for (const jobId of jobIds) query.append("job_ids", jobId);
  return proxyExperimentComparison(
    request,
    `/api/v1/experiments/${experimentId}/comparison?${query.toString()}`,
    experimentId,
    jobIds,
  );
}

function comparisonJobIds(request: NextRequest): string[] | null {
  if (new TextEncoder().encode(request.nextUrl.search).byteLength > MAX_COMPARISON_QUERY_BYTES) {
    return null;
  }
  const jobIds: string[] = [];
  for (const [key, value] of request.nextUrl.searchParams) {
    if (key !== "job_ids" || !canonicalUuidPattern.test(value)) return null;
    jobIds.push(value);
    if (jobIds.length > 16) return null;
  }
  if (jobIds.length < 2 || new Set(jobIds).size !== jobIds.length) return null;
  return jobIds;
}
