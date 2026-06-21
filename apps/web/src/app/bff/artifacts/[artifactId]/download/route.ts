import { NextRequest, type NextResponse } from "next/server";
import {
  allowlistedQuery,
  isSafeResourceId,
} from "@/lib/server/bff-security";
import { bffError, proxyManagerDownload } from "@/lib/server/bff-proxy";

export async function GET(
  request: NextRequest,
  context: { params: Promise<{ artifactId: string }> },
): Promise<NextResponse> {
  const { artifactId } = await context.params;
  if (!isSafeResourceId(artifactId) || allowlistedQuery(request, {}) === null) {
    return bffError("invalid_request", 400);
  }
  return proxyManagerDownload(
    request,
    `/api/v1/artifacts/${encodeURIComponent(artifactId)}/download`,
  );
}
