import { NextRequest, type NextResponse } from "next/server";
import {
  allowlistedQuery,
  isSafeResourceId,
  queryRules,
} from "@/lib/server/bff-security";
import { bffError, proxyManagerEventStream } from "@/lib/server/bff-proxy";

const rules = {
  attempt_id: queryRules.identifier,
  after: queryRules.cursor,
};

export async function GET(
  request: NextRequest,
  context: { params: Promise<{ jobId: string }> },
): Promise<NextResponse> {
  const { jobId } = await context.params;
  const query = allowlistedQuery(request, rules);
  if (!isSafeResourceId(jobId) || query === null) {
    return bffError("invalid_request", 400);
  }
  // EventSource reconnects with the newest Last-Event-ID while retaining the
  // original URL. Forward only the header on reconnect so Manager does not
  // reject a stale `after` query that no longer matches it.
  if (request.headers.has("last-event-id")) query.delete("after");
  const serialized = query.toString();
  const path: `/api/v1/${string}` = `/api/v1/jobs/${encodeURIComponent(jobId)}/logs/stream${serialized ? `?${serialized}` : ""}`;
  return proxyManagerEventStream(request, path);
}
