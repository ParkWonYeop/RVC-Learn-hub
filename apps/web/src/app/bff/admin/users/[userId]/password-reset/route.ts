import { NextRequest, type NextResponse } from "next/server";
import { allowlistedQuery, isSafeResourceId } from "@/lib/server/bff-security";
import { bffError } from "@/lib/server/bff-proxy";
import { resetAdminUserPassword } from "@/lib/server/admin-user-bff";

type Context = { params: Promise<{ userId: string }> };

export async function POST(request: NextRequest, context: Context): Promise<NextResponse> {
  const { userId } = await context.params;
  if (!isSafeResourceId(userId) || allowlistedQuery(request, {}) === null) {
    return bffError("invalid_request", 400);
  }
  return resetAdminUserPassword(request, userId);
}
