import { NextRequest, type NextResponse } from "next/server";
import {
  allowlistedQuery,
  boundedIntegerRule,
} from "@/lib/server/bff-security";
import { bffError } from "@/lib/server/bff-proxy";
import {
  isCanonicalRegistryId,
  proxyModelRegistryList,
} from "@/lib/server/model-registry-bff";

type Context = { params: Promise<{ experimentId: string }> };

const listRules = {
  offset: boundedIntegerRule(0, 2_147_483_647),
  limit: boundedIntegerRule(1, 200),
};

export async function GET(request: NextRequest, context: Context): Promise<NextResponse> {
  const { experimentId } = await context.params;
  const query = allowlistedQuery(request, listRules);
  if (!isCanonicalRegistryId(experimentId) || query === null) {
    return bffError("invalid_request", 400);
  }
  const offset = Number(query.get("offset") ?? "0");
  const limit = Number(query.get("limit") ?? "50");
  return proxyModelRegistryList(
    request,
    `/api/v1/experiments/${experimentId}/model-registry?offset=${offset}&limit=${limit}`,
    experimentId,
    offset,
    limit,
  );
}
