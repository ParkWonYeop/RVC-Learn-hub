import { NextRequest, type NextResponse } from "next/server";
import {
  allowlistedQuery,
  boundedIntegerRule,
  isSafeResourceId,
} from "@/lib/server/bff-security";
import { bffError } from "@/lib/server/bff-proxy";
import {
  deleteExperiment,
  proxyExperimentDetail,
  updateExperiment,
} from "@/lib/server/experiment-bff";

type Context = { params: Promise<{ experimentId: string }> };

export async function GET(request: NextRequest, context: Context): Promise<NextResponse> {
  const { experimentId } = await context.params;
  if (!isSafeResourceId(experimentId) || allowlistedQuery(request, {}) === null) {
    return bffError("invalid_request", 400);
  }
  return proxyExperimentDetail(
    request,
    `/api/v1/experiments/${encodeURIComponent(experimentId)}`,
  );
}

export async function PATCH(request: NextRequest, context: Context): Promise<NextResponse> {
  const { experimentId } = await context.params;
  if (!isSafeResourceId(experimentId) || allowlistedQuery(request, {}) === null) {
    return bffError("invalid_request", 400);
  }
  return updateExperiment(
    request,
    `/api/v1/experiments/${encodeURIComponent(experimentId)}`,
  );
}

export async function DELETE(request: NextRequest, context: Context): Promise<NextResponse> {
  const { experimentId } = await context.params;
  const query = allowlistedQuery(request, {
    expected_row_version: boundedIntegerRule(1, 2_147_483_647),
  });
  const expectedRowVersion = query?.get("expected_row_version");
  if (
    !isSafeResourceId(experimentId) ||
    query === null ||
    query.size !== 1 ||
    expectedRowVersion === null ||
    request.body !== null ||
    request.headers.has("transfer-encoding") ||
    !validEmptyContentLength(request.headers.get("content-length"))
  ) {
    return bffError("invalid_request", 400);
  }
  return deleteExperiment(
    request,
    `/api/v1/experiments/${encodeURIComponent(experimentId)}`,
    Number(expectedRowVersion),
  );
}

function validEmptyContentLength(value: string | null): boolean {
  return value === null || value === "0";
}
