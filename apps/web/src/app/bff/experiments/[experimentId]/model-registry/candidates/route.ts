import { NextRequest, type NextResponse } from "next/server";
import { allowlistedQuery } from "@/lib/server/bff-security";
import { bffError } from "@/lib/server/bff-proxy";
import {
  createModelRegistryCandidate,
  isCanonicalRegistryId,
} from "@/lib/server/model-registry-bff";

type Context = { params: Promise<{ experimentId: string }> };

export async function POST(request: NextRequest, context: Context): Promise<NextResponse> {
  const { experimentId } = await context.params;
  if (
    !isCanonicalRegistryId(experimentId) ||
    allowlistedQuery(request, {}) === null
  ) {
    return bffError("invalid_request", 400);
  }
  return createModelRegistryCandidate(request, experimentId);
}
