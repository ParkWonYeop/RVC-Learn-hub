import { NextRequest, type NextResponse } from "next/server";
import { allowlistedQuery } from "@/lib/server/bff-security";
import { bffError } from "@/lib/server/bff-proxy";
import { createAdminUser } from "@/lib/server/admin-user-bff";

export async function POST(request: NextRequest): Promise<NextResponse> {
  if (allowlistedQuery(request, {}) === null) return bffError("invalid_request", 400);
  return createAdminUser(request);
}
