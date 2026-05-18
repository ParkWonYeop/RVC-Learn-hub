import { NextRequest, type NextResponse } from "next/server";
import { allowlistedQuery, isSafeResourceId } from "@/lib/server/bff-security";
import { bffError } from "@/lib/server/bff-proxy";
import { finalizeDatasetUpload } from "@/lib/server/dataset-bff";

export async function POST(
  request: NextRequest,
  context: { params: Promise<{ uploadSessionId: string }> },
): Promise<NextResponse> {
  const { uploadSessionId } = await context.params;
  if (!isSafeResourceId(uploadSessionId) || allowlistedQuery(request, {}) === null) {
    return bffError("invalid_request", 400);
  }
  return finalizeDatasetUpload(
    request,
    `/api/v1/datasets/uploads/${encodeURIComponent(uploadSessionId)}/finalize`,
  );
}
