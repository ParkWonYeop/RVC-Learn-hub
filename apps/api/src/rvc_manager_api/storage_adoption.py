from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from collections.abc import Sequence

from .config import Settings
from .database import Database
from .services.storage_adoption import StorageAdoptionResult, adopt_storage_sessions
from .storage import create_storage_adapter


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Verify legacy upload objects before binding their ledger rows to the current "
            "storage namespace. The default does not bind rows, but it does write audit events."
        ),
        allow_abbrev=False,
    )
    parser.add_argument("--kind", choices=("dataset", "artifact", "all"), required=True)
    parser.add_argument(
        "--session-id",
        action="append",
        default=[],
        help="canonical upload session UUID; repeatable and incompatible with --kind all",
    )
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="persist verified namespace bindings; omitted verifies and writes an audit event",
    )
    return parser


def _canonical_session_ids(values: list[str]) -> tuple[str, ...]:
    result: list[str] = []
    for value in values:
        try:
            parsed = str(uuid.UUID(value))
        except (ValueError, AttributeError) as exc:
            raise ValueError("storage adoption session IDs must be UUIDs") from exc
        if parsed != value:
            raise ValueError("storage adoption session IDs must use canonical UUID form")
        if parsed in result:
            raise ValueError("storage adoption session IDs must be unique")
        result.append(parsed)
    return tuple(result)


async def _run(
    args: argparse.Namespace,
    session_ids: tuple[str, ...],
) -> StorageAdoptionResult:
    settings = Settings()
    database = Database(settings)
    storage = create_storage_adapter(settings)
    try:
        result = await adopt_storage_sessions(
            database,
            storage,
            kind=args.kind,
            session_ids=session_ids,
            limit=args.limit,
            chunk_size=settings.artifact_stream_chunk_bytes,
            dry_run=not args.apply,
        )
        return result
    finally:
        await storage.close()
        await database.dispose()


def main(argv: Sequence[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    try:
        session_ids = _canonical_session_ids(args.session_id)
        if args.limit < 1 or args.limit > 500:
            raise ValueError("storage adoption limit must be between 1 and 500")
        if len(session_ids) > args.limit:
            raise ValueError("session ID count exceeds the storage adoption limit")
        if session_ids and args.kind == "all":
            raise ValueError("explicit session IDs require dataset or artifact kind")
    except ValueError as exc:
        raise SystemExit(f"invalid storage namespace adoption request: {exc}") from None
    try:
        result = asyncio.run(_run(args, session_ids))
    except Exception:
        raise SystemExit("storage namespace adoption failed safely") from None
    print(json.dumps(result.as_json(), sort_keys=True, separators=(",", ":")))
    if result.rejected > 0:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
