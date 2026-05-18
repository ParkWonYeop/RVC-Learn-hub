import { NextRequest, type NextResponse } from "next/server";
import { allowlistedQuery, boundedIntegerRule, enumRule, queryRules } from "@/lib/server/bff-security";
import { bffError } from "@/lib/server/bff-proxy";
import { proxyDatasetList } from "@/lib/server/dataset-bff";

const statuses = [
  "legacy_imported",
  "upload_pending",
  "processing",
  "ready",
  "decoder_pending",
  "failed",
  "deleting",
  "delete_failed",
] as const;
const rules = {
  status: enumRule(statuses),
  offset: queryRules.unsignedInteger,
  limit: boundedIntegerRule(1, 200),
};

export async function GET(request: NextRequest): Promise<NextResponse> {
  const query = allowlistedQuery(request, rules);
  if (query === null) return bffError("invalid_request", 400);
  const serialized = query.toString();
  const path: `/api/v1/${string}` = `/api/v1/datasets${serialized ? `?${serialized}` : ""}`;
  return proxyDatasetList(request, path);
}
