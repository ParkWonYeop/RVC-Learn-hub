import type { ApiAdminUser, ApiAdminUserList } from "./api-types";

const safeIdentifierPattern = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/;

export function publicAdminUser(value: unknown): ApiAdminUser | null {
  if (!isRecord(value)) return null;
  const id = safeString(value.id, safeIdentifierPattern, 128);
  const email = safeEmail(value.email);
  const createdAt = safeDate(value.created_at);
  const updatedAt = safeDate(value.updated_at);
  if (
    !id ||
    !email ||
    (value.role !== "admin" && value.role !== "user") ||
    typeof value.active !== "boolean" ||
    !integerInRange(value.row_version, 1, 2_147_483_647) ||
    !createdAt ||
    !updatedAt
  ) {
    return null;
  }
  return {
    id,
    email,
    role: value.role,
    active: value.active,
    row_version: value.row_version,
    created_at: createdAt,
    updated_at: updatedAt,
  };
}

export function publicAdminUserList(value: unknown): ApiAdminUserList | null {
  if (!isRecord(value) || !Array.isArray(value.items)) return null;
  const items = value.items.map(publicAdminUser);
  if (
    items.some((item) => item === null) ||
    !integerInRange(value.total, 0, Number.MAX_SAFE_INTEGER) ||
    !integerInRange(value.offset, 0, Number.MAX_SAFE_INTEGER) ||
    !integerInRange(value.limit, 1, 200) ||
    value.items.length > value.limit ||
    value.offset + value.items.length > value.total
  ) {
    return null;
  }
  return {
    items: items as ApiAdminUser[],
    total: value.total,
    offset: value.offset,
    limit: value.limit,
  };
}

function safeEmail(value: unknown): string | null {
  if (
    typeof value !== "string" ||
    value.length < 3 ||
    value.length > 320 ||
    value !== value.trim() ||
    /[\s\u0000-\u001f\u007f]/.test(value) ||
    !/^[^@]+@[^@]+\.[^@]+$/.test(value)
  ) {
    return null;
  }
  return value;
}

function safeString(value: unknown, pattern: RegExp, maximum: number): string | null {
  return typeof value === "string" && value.length <= maximum && pattern.test(value)
    ? value
    : null;
}

function safeDate(value: unknown): string | null {
  return typeof value === "string" &&
    value.length <= 64 &&
    !/[\u0000-\u001f\u007f]/.test(value) &&
    Number.isFinite(Date.parse(value))
    ? value
    : null;
}

function integerInRange(
  value: unknown,
  minimum: number,
  maximum: number,
): value is number {
  return typeof value === "number" &&
    Number.isSafeInteger(value) &&
    value >= minimum &&
    value <= maximum;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
