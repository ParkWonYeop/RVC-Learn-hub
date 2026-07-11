import { NextRequest, type NextResponse } from "next/server";
import { allowlistedQuery } from "@/lib/server/bff-security";
import { bffError } from "@/lib/server/bff-proxy";
import {
  isCanonicalRegistryId,
  revokeModelRegistryEntry,
} from "@/lib/server/model-registry-bff";

type Context = { params: Promise<{ experimentId: string; entryId: string }> };

export async function POST(request: NextRequest, context: Context): Promise<NextResponse> {
  const { experimentId, entryId } = await context.params;
  if (
    !isCanonicalRegistryId(experimentId) ||
    !isCanonicalRegistryId(entryId) ||
    allowlistedQuery(request, {}) === null
  ) {
    return bffError("invalid_request", 400);
  }
  return revokeModelRegistryEntry(request, experimentId, entryId);
}
