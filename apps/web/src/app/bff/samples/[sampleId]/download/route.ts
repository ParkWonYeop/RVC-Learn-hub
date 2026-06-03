import { NextRequest, type NextResponse } from "next/server";
import {
  allowlistedQuery,
  isSafeResourceId,
} from "@/lib/server/bff-security";
import { bffError, proxyManagerDownload } from "@/lib/server/bff-proxy";
import { SAMPLE_DOWNLOAD_MAX_BYTES } from "@/lib/server/sample-bff";

export async function GET(
  request: NextRequest,
  context: { params: Promise<{ sampleId: string }> },
): Promise<NextResponse> {
  const { sampleId } = await context.params;
  if (!isSafeResourceId(sampleId) || allowlistedQuery(request, {}) === null) {
    return bffError("invalid_request", 400);
  }
  return proxyManagerDownload(
    request,
    `/api/v1/samples/${encodeURIComponent(sampleId)}/download`,
    {
      accept: "audio/wav",
      allowRedirect: false,
      expectedContentType: "audio/wav",
      forwardRange: true,
      inlineFilename: "sample.wav",
      maxContentLength: SAMPLE_DOWNLOAD_MAX_BYTES,
    },
  );
}
