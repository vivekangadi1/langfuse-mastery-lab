"""Read-only listing of the stack's MinIO (S3 API), with stdlib SigV4 (the venv has no boto3). No Langfuse code.

verify uses it to see whether exports (UI batch export, blob storage integration) landed in the bucket.
Settings come from docker-compose.langfuse.yml: S3 API on localhost:9090, user `minio`, password
LANGFUSE_MINIO_ROOT_PASSWORD (repo-root .env), bucket `langfuse`.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone

NS = "{http://s3.amazonaws.com/doc/2006-03-01/}"
BATCH_EXPORT_PREFIX = "exports/"     # compose: LANGFUSE_S3_BATCH_EXPORT_PREFIX
BLOB_EXPORT_PREFIX = "blob-exports/"  # module 11's blob storage integration prefix


@dataclass(frozen=True)
class MinioSettings:
    endpoint: str        # as seen from the host
    bucket: str
    access_key: str
    secret_key: str
    internal_endpoint: str = "http://minio:9000"  # as seen from the Langfuse containers
    region: str = "us-east-1"


def minio_settings(load: bool = True) -> MinioSettings:
    if load:
        from dotenv import load_dotenv

        from .config import ENV_FILE
        load_dotenv(ENV_FILE)
    secret = os.getenv("LANGFUSE_MINIO_ROOT_PASSWORD")
    if not secret:
        raise RuntimeError("LANGFUSE_MINIO_ROOT_PASSWORD is not set (repo-root .env)")
    return MinioSettings(endpoint=os.getenv("LANGFUSE_MINIO_ENDPOINT", "http://localhost:9090"),
                         bucket="langfuse", access_key="minio", secret_key=secret)


def _quote(v: str) -> str:
    return urllib.parse.quote(v, safe="-_.~")


def signed_headers(method: str, url: str, access_key: str, secret_key: str, *, region: str = "us-east-1",
                   now: datetime | None = None) -> dict:
    """AWS SigV4 headers for an unsigned-payload-free GET (empty body)."""
    parts = urllib.parse.urlsplit(url)
    now = now or datetime.now(timezone.utc)
    amz, day = now.strftime("%Y%m%dT%H%M%SZ"), now.strftime("%Y%m%d")
    query = "&".join(f"{_quote(k)}={_quote(v)}" for k, v in sorted(urllib.parse.parse_qsl(parts.query)))
    payload = hashlib.sha256(b"").hexdigest()
    signed = "host;x-amz-content-sha256;x-amz-date"
    canonical = "\n".join([method, parts.path or "/", query,
                           f"host:{parts.netloc}\nx-amz-content-sha256:{payload}\nx-amz-date:{amz}\n", signed, payload])
    scope = f"{day}/{region}/s3/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", amz, scope, hashlib.sha256(canonical.encode()).hexdigest()])
    key = ("AWS4" + secret_key).encode()
    for part in (day, region, "s3", "aws4_request"):
        key = hmac.new(key, part.encode(), hashlib.sha256).digest()
    signature = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
    return {"x-amz-date": amz, "x-amz-content-sha256": payload,
            "Authorization": f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, SignedHeaders={signed}, "
                             f"Signature={signature}"}


def parse_listing(xml_text: str) -> tuple[list[dict], str | None]:
    """(objects, next continuation token) from a ListObjectsV2 response."""
    root = ET.fromstring(xml_text)
    objects = []
    for c in root.findall(f"{NS}Contents"):
        modified = c.findtext(f"{NS}LastModified") or ""
        objects.append({"key": c.findtext(f"{NS}Key"), "size": int(c.findtext(f"{NS}Size") or 0),
                        "last_modified": datetime.fromisoformat(modified.replace("Z", "+00:00")) if modified else None})
    truncated = (root.findtext(f"{NS}IsTruncated") or "").lower() == "true"
    return objects, root.findtext(f"{NS}NextContinuationToken") if truncated else None


def list_objects(prefix: str, settings: MinioSettings | None = None, *, opener=urllib.request.urlopen,
                 max_pages: int = 20) -> list[dict]:
    """Every object under `prefix` in the bucket: [{"key", "size", "last_modified"}]."""
    s = settings or minio_settings()
    out, token = [], None
    for _ in range(max_pages):
        params = {"list-type": "2", "prefix": prefix, "max-keys": "1000"}
        if token:
            params["continuation-token"] = token
        url = f"{s.endpoint}/{s.bucket}?" + "&".join(f"{_quote(k)}={_quote(v)}" for k, v in sorted(params.items()))
        req = urllib.request.Request(url, headers=signed_headers("GET", url, s.access_key, s.secret_key,
                                                                 region=s.region))
        with opener(req, timeout=10) as resp:
            objects, token = parse_listing(resp.read().decode())
        out += objects
        if not token:
            return out
    raise RuntimeError(f"more than {max_pages} pages under {prefix!r}")
