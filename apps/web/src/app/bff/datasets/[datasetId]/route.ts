import { NextRequest, type NextResponse } from "next/server";
import { allowlistedQuery, isSafeResourceId } from "@/lib/server/bff-security";
import { bffError } from "@/lib/server/bff-proxy";
import { deleteDataset, proxyDatasetDetail } from "@/lib/server/dataset-bff";

type Context = { params: Promise<{ datasetId: string }> };

export async function GET(request: NextRequest, context: Context): Promise<NextResponse> {
  const { datasetId } = await context.params;
  if (!isSafeResourceId(datasetId) || allowlistedQuery(request, {}) === null) {
    return bffError("invalid_request", 400);
  }
  return proxyDatasetDetail(
    request,
    `/api/v1/datasets/${encodeURIComponent(datasetId)}`,
  );
}

export async function DELETE(request: NextRequest, context: Context): Promise<NextResponse> {
  const { datasetId } = await context.params;
  if (!isSafeResourceId(datasetId) || allowlistedQuery(request, {}) === null) {
    return bffError("invalid_request", 400);
  }
  return deleteDataset(request, `/api/v1/datasets/${encodeURIComponent(datasetId)}`);
}
