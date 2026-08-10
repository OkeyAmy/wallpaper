"""Where wallpaper bytes actually live.

Two backends, selected by environment:

  * **R2** when the R2_* variables are set. Images go to a Cloudflare R2
    bucket and the repository keeps nothing but code and a JSON manifest,
    so the git history stops growing with the collection. This is the mode
    the deployed site runs in.
  * **local** otherwise, writing into ``wallpapers/``. Keeps the project
    runnable and previewable with no cloud account at all.

The distinction is deliberately invisible to callers: both backends speak in
keys like ``wallpapers/ab12cd34.webp``, and the frontend prepends a CDN base
when the manifest carries one.

Required for R2 mode:
    R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET
Optional:
    R2_PUBLIC_BASE   public URL of the bucket, e.g. https://cdn.okeyamy.xyz
"""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Loaded here, once, so every entry point (ingest_local, sync_*, build_site,
# prune, migrate) sees the same R2 config without each having to remember to
# call load_dotenv itself — a script that forgot would silently fall back to
# local storage, which is exactly the bug this caused before it lived here.
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env.local")
except ImportError:
    pass


class LocalStorage:
    """Files under the repository, served directly by Cloudflare Pages."""

    kind = "local"
    public_base = ""

    def put(self, key: str, data: bytes, content_type: str = "image/webp") -> None:
        path = ROOT / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def delete(self, key: str) -> None:
        (ROOT / key).unlink(missing_ok=True)

    def exists(self, key: str) -> bool:
        return (ROOT / key).exists()

    def read(self, key: str) -> bytes | None:
        path = ROOT / key
        return path.read_bytes() if path.exists() else None


class R2Storage:
    """Cloudflare R2 over its S3-compatible API.

    R2 charges nothing for egress, which is the whole reason a wallpaper
    archive can be free to run at any traffic level.
    """

    kind = "r2"

    def __init__(self, account_id: str, key_id: str, secret: str, bucket: str, public_base: str = ""):
        import boto3
        from botocore.config import Config

        self.bucket = bucket
        self.public_base = public_base.rstrip("/")
        self.client = boto3.client(
            "s3",
            endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
            aws_access_key_id=key_id,
            aws_secret_access_key=secret,
            region_name="auto",
            # R2 does not implement the trailing-checksum flow newer botocore
            # versions default to; without this, uploads fail on some releases
            config=Config(retries={"max_attempts": 5, "mode": "standard"},
                          request_checksum_calculation="when_required",
                          response_checksum_validation="when_required"),
        )

    def put(self, key: str, data: bytes, content_type: str = "image/webp") -> None:
        self.client.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=data,
            ContentType=content_type,
            # content-addressed filenames, so they can never go stale
            CacheControl="public, max-age=31536000, immutable",
        )

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=key)

    def exists(self, key: str) -> bool:
        from botocore.exceptions import ClientError
        try:
            self.client.head_object(Bucket=self.bucket, Key=key)
            return True
        except ClientError:
            return False

    def read(self, key: str) -> bytes | None:
        from botocore.exceptions import ClientError
        try:
            return self.client.get_object(Bucket=self.bucket, Key=key)["Body"].read()
        except ClientError:
            return None

    def total_bytes(self) -> int:
        """Bytes currently stored, for enforcing the free-tier ceiling."""
        total = 0
        token = None
        while True:
            kwargs = {"Bucket": self.bucket}
            if token:
                kwargs["ContinuationToken"] = token
            page = self.client.list_objects_v2(**kwargs)
            total += sum(o["Size"] for o in page.get("Contents", []))
            if not page.get("IsTruncated"):
                return total
            token = page.get("NextContinuationToken")


def get_storage():
    """Return the backend this environment is configured for."""
    account = os.getenv("R2_ACCOUNT_ID")
    key_id = os.getenv("R2_ACCESS_KEY_ID")
    secret = os.getenv("R2_SECRET_ACCESS_KEY")
    bucket = os.getenv("R2_BUCKET")

    if all([account, key_id, secret, bucket]):
        return R2Storage(account, key_id, secret, bucket, os.getenv("R2_PUBLIC_BASE", ""))
    return LocalStorage()
