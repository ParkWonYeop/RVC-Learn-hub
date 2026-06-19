#!/usr/bin/env python3
"""Create and restore metadata-preserving, unversioned S3 bucket snapshots."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlencode

_BUCKET = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
_LABEL = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_BUFFER_SIZE = 1024 * 1024
_RESTORED_HEADERS = {
    "CacheControl": "cache_control",
    "ContentDisposition": "content_disposition",
    "ContentEncoding": "content_encoding",
    "ContentLanguage": "content_language",
    "ContentType": "content_type",
    "WebsiteRedirectLocation": "website_redirect_location",
}


class SnapshotError(RuntimeError):
    """Raised when bucket state cannot be preserved exactly."""


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise SnapshotError(f"duplicate inventory key: {key}")
        result[key] = value
    return result


def _parse_bucket(value: str) -> tuple[str, str]:
    label, separator, bucket = value.partition("=")
    if not separator or not _LABEL.fullmatch(label) or not _BUCKET.fullmatch(bucket):
        raise argparse.ArgumentTypeError("bucket must use safe-label=safe-bucket-name")
    return label, bucket


def _read_secret(path: Path) -> str:
    if not path.is_file() or path.is_symlink():
        raise SnapshotError(f"S3 credential file is missing or unsafe: {path}")
    value = path.read_text(encoding="utf-8").strip("\r\n")
    if not value or "\n" in value or "\r" in value:
        raise SnapshotError("S3 credential file must contain exactly one non-empty value")
    return value


def _client(arguments: argparse.Namespace) -> Any:
    try:
        import boto3
        from botocore.config import Config
    except ImportError as exc:
        raise SnapshotError("the recovery image must contain boto3/botocore") from exc
    if arguments.access_key_file is None or arguments.secret_key_file is None:
        raise SnapshotError("S3 credential files are required for backup and restore")
    return boto3.client(
        "s3",
        endpoint_url=arguments.endpoint,
        region_name="us-east-1",
        aws_access_key_id=_read_secret(arguments.access_key_file),
        aws_secret_access_key=_read_secret(arguments.secret_key_file),
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )


def _assert_unversioned(client: Any, bucket: str) -> None:
    status = client.get_bucket_versioning(Bucket=bucket).get("Status")
    if status is not None:
        raise SnapshotError(
            f"bucket {bucket} has versioning state {status}; "
            "this format requires never-versioned buckets"
        )
    paginator = client.get_paginator("list_object_versions")
    seen: set[str] = set()
    for page in paginator.paginate(Bucket=bucket):
        if page.get("DeleteMarkers"):
            raise SnapshotError(f"bucket {bucket} contains delete markers")
        for version in page.get("Versions", []):
            key = version.get("Key")
            version_id = version.get("VersionId")
            if not isinstance(key, str) or version_id not in {None, "null"} or key in seen:
                raise SnapshotError(f"bucket {bucket} contains version history")
            seen.add(key)


def _safe_data_file(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise SnapshotError(f"{label} is not a safe data path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise SnapshotError(f"{label} is not a safe data path")
    return value


def _stream_to_file(body: Any, path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        with os.fdopen(fd, "wb", closefd=False) as stream:
            while True:
                chunk = body.read(_BUFFER_SIZE)
                if not chunk:
                    break
                size += len(chunk)
                digest.update(chunk)
                stream.write(chunk)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        os.close(fd)
        body.close()
    return size, digest.hexdigest()


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(_BUFFER_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _object_headers(head: dict[str, Any]) -> dict[str, Any]:
    unsupported = {
        "SSEKMSKeyId",
        "BucketKeyEnabled",
        "ObjectLockMode",
        "ObjectLockRetainUntilDate",
        "ObjectLockLegalHoldStatus",
    }
    if unsupported.intersection(head):
        raise SnapshotError("object uses KMS, bucket-key, or object-lock state not supported here")
    encryption = head.get("ServerSideEncryption")
    if encryption not in {None, "AES256"}:
        raise SnapshotError("object encryption mode cannot be reproduced by this snapshot format")
    headers: dict[str, Any] = {
        inventory_name: head.get(api_name) for api_name, inventory_name in _RESTORED_HEADERS.items()
    }
    expires = head.get("Expires")
    headers["expires"] = expires.isoformat() if isinstance(expires, datetime) else None
    headers["server_side_encryption"] = encryption
    headers["storage_class"] = head.get("StorageClass", "STANDARD")
    return headers


def _list_keys(client: Any, bucket: str) -> list[str]:
    keys: list[str] = []
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket):
        for item in page.get("Contents", []):
            key = item.get("Key")
            if not isinstance(key, str) or not key:
                raise SnapshotError("S3 returned an invalid object key")
            keys.append(key)
    return sorted(keys)


def _backup_object(client: Any, root: Path, label: str, bucket: str, key: str) -> dict[str, Any]:
    head = client.head_object(Bucket=bucket, Key=key)
    if head.get("VersionId") not in {None, "null"}:
        raise SnapshotError(f"versioned object cannot be snapshotted: {bucket}/{key}")
    key_hash = hashlib.sha256(key.encode("utf-8")).hexdigest()
    data_file = f"data/{label}/{key_hash}.bin"
    response = client.get_object(Bucket=bucket, Key=key)
    size, digest = _stream_to_file(response["Body"], root / data_file)
    if size != head.get("ContentLength"):
        raise SnapshotError(f"object changed while being read: {bucket}/{key}")
    tags = sorted(
        client.get_object_tagging(Bucket=bucket, Key=key).get("TagSet", []),
        key=lambda item: (item.get("Key", ""), item.get("Value", "")),
    )
    checksums = {
        field: head[field]
        for field in ("ChecksumCRC32", "ChecksumCRC32C", "ChecksumSHA1", "ChecksumSHA256")
        if field in head
    }
    return {
        "key": key,
        "data_file": data_file,
        "size": size,
        "sha256": digest,
        "etag": str(head.get("ETag", "")).strip('"'),
        "metadata": dict(sorted(head.get("Metadata", {}).items())),
        "tags": tags,
        "headers": _object_headers(head),
        "source_checksums": checksums,
        "version_id": None,
    }


def backup(client: Any, root: Path, buckets: list[tuple[str, str]]) -> dict[str, Any]:
    if root.exists():
        if not root.is_dir() or root.is_symlink() or any(root.iterdir()):
            raise SnapshotError("object snapshot root must be an empty regular directory")
    else:
        root.mkdir(mode=0o700, parents=True)
    records: list[dict[str, Any]] = []
    for label, bucket in buckets:
        _assert_unversioned(client, bucket)
        objects = [
            _backup_object(client, root, label, bucket, key) for key in _list_keys(client, bucket)
        ]
        records.append(
            {
                "label": label,
                "bucket": bucket,
                "versioning": "disabled",
                "objects": objects,
            }
        )
    inventory = {
        "schema_version": 1,
        "kind": "rvc-s3-object-snapshot",
        "version_semantics": "unversioned-current-object",
        "buckets": records,
    }
    temporary = root / ".inventory.json.tmp"
    temporary.write_text(json.dumps(inventory, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    with temporary.open("rb") as stream:
        os.fsync(stream.fileno())
    temporary.replace(root / "inventory.json")
    return {"buckets": len(records), "objects": sum(len(item["objects"]) for item in records)}


def _load_inventory(root: Path, expected: list[tuple[str, str]]) -> list[dict[str, Any]]:
    inventory_path = root / "inventory.json"
    if not inventory_path.is_file() or inventory_path.is_symlink():
        raise SnapshotError("object inventory is missing or unsafe")
    try:
        inventory = json.loads(
            inventory_path.read_text(encoding="utf-8"), object_pairs_hook=_strict_object
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SnapshotError("object inventory is not strict readable JSON") from exc
    if not isinstance(inventory, dict) or set(inventory) != {
        "schema_version",
        "kind",
        "version_semantics",
        "buckets",
    }:
        raise SnapshotError("object inventory fields differ from schema")
    if (
        inventory["schema_version"] != 1
        or inventory["kind"] != "rvc-s3-object-snapshot"
        or inventory["version_semantics"] != "unversioned-current-object"
        or not isinstance(inventory["buckets"], list)
    ):
        raise SnapshotError("unsupported object inventory format")
    buckets: list[dict[str, Any]] = []
    observed: list[tuple[str, str]] = []
    expected_files = {"inventory.json"}
    for bucket_record in inventory["buckets"]:
        if not isinstance(bucket_record, dict) or set(bucket_record) != {
            "label",
            "bucket",
            "versioning",
            "objects",
        }:
            raise SnapshotError("bucket inventory fields differ from schema")
        label = bucket_record["label"]
        bucket = bucket_record["bucket"]
        if (
            not isinstance(label, str)
            or not isinstance(bucket, str)
            or not _LABEL.fullmatch(label)
            or not _BUCKET.fullmatch(bucket)
            or bucket_record["versioning"] != "disabled"
            or not isinstance(bucket_record["objects"], list)
        ):
            raise SnapshotError("bucket inventory identity is invalid")
        observed.append((label, bucket))
        seen_keys: set[str] = set()
        for record in bucket_record["objects"]:
            required = {
                "key",
                "data_file",
                "size",
                "sha256",
                "etag",
                "metadata",
                "tags",
                "headers",
                "source_checksums",
                "version_id",
            }
            if not isinstance(record, dict) or set(record) != required:
                raise SnapshotError("object inventory fields differ from schema")
            key = record["key"]
            data_file = _safe_data_file(record["data_file"], "object data_file")
            if (
                not isinstance(key, str)
                or not key
                or key in seen_keys
                or not isinstance(record["size"], int)
                or record["size"] < 0
                or not isinstance(record["sha256"], str)
                or not _SHA256.fullmatch(record["sha256"])
                or record["version_id"] is not None
            ):
                raise SnapshotError("object inventory value is invalid")
            seen_keys.add(key)
            data_path = root / data_file
            if not data_path.is_file() or data_path.is_symlink():
                raise SnapshotError("object snapshot data file is missing or unsafe")
            if data_path.stat().st_size != record["size"]:
                raise SnapshotError("object snapshot data size differs from inventory")
            digest = _hash_file(data_path)
            if digest != record["sha256"]:
                raise SnapshotError("object snapshot data checksum differs from inventory")
            expected_files.add(data_file)
        buckets.append(bucket_record)
    if observed != expected:
        raise SnapshotError("object inventory buckets differ from this installation")
    discovered: set[str] = set()
    for path in root.rglob("*"):
        if path.is_symlink():
            raise SnapshotError("object snapshot contains a symbolic link")
        if path.is_file():
            discovered.add(path.relative_to(root).as_posix())
    if discovered != expected_files:
        raise SnapshotError("object snapshot has an unlisted or missing file")
    return buckets


def _put_headers(record: dict[str, Any]) -> dict[str, Any]:
    headers = record["headers"]
    if not isinstance(headers, dict) or set(headers) != {
        "cache_control",
        "content_disposition",
        "content_encoding",
        "content_language",
        "content_type",
        "expires",
        "server_side_encryption",
        "storage_class",
        "website_redirect_location",
    }:
        raise SnapshotError("object header inventory is invalid")
    result: dict[str, Any] = {}
    for api_name, inventory_name in _RESTORED_HEADERS.items():
        value = headers[inventory_name]
        if value is not None:
            if not isinstance(value, str):
                raise SnapshotError("object header must be a string or null")
            result[api_name] = value
    expires = headers["expires"]
    if expires is not None:
        if not isinstance(expires, str):
            raise SnapshotError("object expiry must be an ISO string or null")
        result["Expires"] = datetime.fromisoformat(expires)
    encryption = headers["server_side_encryption"]
    if encryption is not None:
        if encryption != "AES256":
            raise SnapshotError("unsupported object encryption in inventory")
        result["ServerSideEncryption"] = encryption
    storage_class = headers["storage_class"]
    if storage_class not in {None, "STANDARD"}:
        if not isinstance(storage_class, str):
            raise SnapshotError("invalid storage class in inventory")
        result["StorageClass"] = storage_class
    metadata = record["metadata"]
    tags = record["tags"]
    if not isinstance(metadata, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in metadata.items()
    ):
        raise SnapshotError("object user metadata is invalid")
    if not isinstance(tags, list) or not all(
        isinstance(item, dict)
        and set(item) == {"Key", "Value"}
        and isinstance(item["Key"], str)
        and isinstance(item["Value"], str)
        for item in tags
    ):
        raise SnapshotError("object tag inventory is invalid")
    result["Metadata"] = metadata
    if tags:
        result["Tagging"] = urlencode([(item["Key"], item["Value"]) for item in tags])
    return result


def _clear_bucket(client: Any, bucket: str) -> None:
    keys = _list_keys(client, bucket)
    for start in range(0, len(keys), 1000):
        response = client.delete_objects(
            Bucket=bucket,
            Delete={"Objects": [{"Key": key} for key in keys[start : start + 1000]], "Quiet": True},
        )
        if response.get("Errors"):
            raise SnapshotError(f"could not clear bucket before restore: {bucket}")


def _verify_restored(client: Any, bucket: str, record: dict[str, Any]) -> None:
    head = client.head_object(Bucket=bucket, Key=record["key"])
    if (
        head.get("ContentLength") != record["size"]
        or dict(sorted(head.get("Metadata", {}).items())) != record["metadata"]
    ):
        raise SnapshotError("restored object size or user metadata differs from inventory")
    actual_headers = _object_headers(head)
    if actual_headers != record["headers"]:
        raise SnapshotError("restored object headers differ from inventory")
    tags = sorted(
        client.get_object_tagging(Bucket=bucket, Key=record["key"]).get("TagSet", []),
        key=lambda item: (item.get("Key", ""), item.get("Value", "")),
    )
    if tags != record["tags"]:
        raise SnapshotError("restored object tags differ from inventory")
    body = client.get_object(Bucket=bucket, Key=record["key"])["Body"]
    digest = hashlib.sha256()
    size = 0
    try:
        while True:
            chunk = body.read(_BUFFER_SIZE)
            if not chunk:
                break
            size += len(chunk)
            digest.update(chunk)
    finally:
        body.close()
    if size != record["size"] or digest.hexdigest() != record["sha256"]:
        raise SnapshotError("restored object bytes differ from inventory")


def restore(client: Any, root: Path, expected: list[tuple[str, str]]) -> dict[str, Any]:
    buckets = _load_inventory(root, expected)
    restored = 0
    for record in buckets:
        bucket = record["bucket"]
        _assert_unversioned(client, bucket)
        _clear_bucket(client, bucket)
        for object_record in record["objects"]:
            data_path = root / object_record["data_file"]
            with data_path.open("rb") as stream:
                client.put_object(
                    Bucket=bucket,
                    Key=object_record["key"],
                    Body=stream,
                    ContentLength=object_record["size"],
                    **_put_headers(object_record),
                )
            _verify_restored(client, bucket, object_record)
            restored += 1
        if _list_keys(client, bucket) != sorted(item["key"] for item in record["objects"]):
            raise SnapshotError("restored bucket key inventory differs from backup")
    return {"buckets": len(buckets), "objects": restored}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("backup", "verify", "restore"))
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--endpoint", default="http://minio:9000")
    parser.add_argument("--access-key-file", type=Path)
    parser.add_argument("--secret-key-file", type=Path)
    parser.add_argument("--bucket", type=_parse_bucket, action="append", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    if len(set(arguments.bucket)) != len(arguments.bucket):
        print("object snapshot failed: duplicate bucket mapping", file=sys.stderr)
        return 1
    try:
        if arguments.command == "verify":
            buckets = _load_inventory(arguments.root, arguments.bucket)
            result = {
                "buckets": len(buckets),
                "objects": sum(len(record["objects"]) for record in buckets),
            }
        else:
            client = _client(arguments)
            result = (
                backup(client, arguments.root, arguments.bucket)
                if arguments.command == "backup"
                else restore(client, arguments.root, arguments.bucket)
            )
    except (OSError, UnicodeError, ValueError, SnapshotError) as exc:
        print(f"object snapshot failed: {exc}", file=sys.stderr)
        return 1
    except Exception:  # noqa: BLE001 - backend exceptions are intentionally redacted
        print("object snapshot failed: S3 backend operation failed", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
