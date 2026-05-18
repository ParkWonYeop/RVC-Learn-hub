import { NextRequest, type NextResponse } from "next/server";
import { allowlistedQuery } from "@/lib/server/bff-security";
import { bffError } from "@/lib/server/bff-proxy";
import { initializeDatasetUpload } from "@/lib/server/dataset-bff";

export async function POST(request: NextRequest): Promise<NextResponse> {
  if (allowlistedQuery(request, {}) === null) return bffError("invalid_request", 400);
  return initializeDatasetUpload(request);
}
