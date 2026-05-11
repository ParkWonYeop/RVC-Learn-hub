import "server-only";

import type { NextRequest } from "next/server";

export type QueryRule = {
  maxLength: number;
  validate: (value: string) => boolean;
};

const safeIdentifierPattern = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/;
const cursorPattern = /^[A-Za-z0-9_-]+$/;
const metricKeyPattern = /^[A-Za-z0-9_.-]+$/;
const timestampPattern = /^[0-9T:+Z.-]+$/;
const uuidPattern =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

export const queryRules = {
  identifier: rule(128, (value) => safeIdentifierPattern.test(value)),
  cursor: rule(512, (value) => cursorPattern.test(value)),
  metricKey: rule(128, (value) => metricKeyPattern.test(value)),
  timestamp: rule(40, (value) => timestampPattern.test(value)),
  uuid: rule(36, (value) => uuidPattern.test(value)),
  boolean: rule(5, (value) => value === "true" || value === "false"),
  unsignedInteger: rule(16, (value) => /^(0|[1-9][0-9]*)$/.test(value)),
};

export function boundedIntegerRule(minimum: number, maximum: number): QueryRule {
  const maximumLength = Math.max(String(minimum).length, String(maximum).length);
  return rule(maximumLength, (value) => {
    if (!/^(0|[1-9][0-9]*)$/.test(value)) return false;
    const parsed = Number(value);
    return Number.isSafeInteger(parsed) && parsed >= minimum && parsed <= maximum;
  });
}

export function enumRule(values: readonly string[]): QueryRule {
  const allowed = new Set(values);
  return rule(Math.max(...values.map((value) => value.length)), (value) => allowed.has(value));
}

export function isSafeResourceId(value: string): boolean {
  return safeIdentifierPattern.test(value);
}

export function allowlistedQuery(
  request: NextRequest,
  rules: Readonly<Record<string, QueryRule>>,
): URLSearchParams | null {
  if (request.nextUrl.search.length > 2_048) return null;
  const forwarded = new URLSearchParams();
  const seen = new Set<string>();
  for (const [key, value] of request.nextUrl.searchParams) {
    const queryRule = rules[key];
    if (!queryRule || seen.has(key) || value.length > queryRule.maxLength) return null;
    if (!queryRule.validate(value)) return null;
    seen.add(key);
    forwarded.set(key, value);
  }
  return forwarded;
}

function rule(maxLength: number, validate: QueryRule["validate"]): QueryRule {
  return { maxLength, validate };
}
