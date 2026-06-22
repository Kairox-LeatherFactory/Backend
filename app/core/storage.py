"""
================================================================================
core/storage.py — Pluggable object storage (Stage 1 §6)
================================================================================

WHY AN ABSTRACTION (not a direct Supabase call)
    The repo deliberately keeps NO hard Supabase dependency and must preserve the
    SQLite/local test path. `Document.storage_url` stays a backend-agnostic URI.
    A thin `StorageBackend` interface (put/get/delete/exists/presign) is selected
    by `settings.storage_backend`:

        local     (default, dev + tests)  -> local filesystem, no external dep
        s3/minio  (self-hosted staging)   -> S3-compatible, lazy boto3 import
        supabase  (PRODUCTION)            -> Supabase Storage, lazy SDK import

    The s3/supabase drivers import their SDK lazily INSIDE the driver, so a machine
    that never sets STORAGE_BACKEND=supabase never needs the SDK installed.

KEY LAYOUT (identical across backends, §6)
    quarantine/<submission_id>/<kind>/<sha256><ext>   ← lands here first, scanned
    submissions/<submission_id>/<slot>/<sha256><ext>  ← promoted only after clean

    <sha256> as the object name gives free dedupe and a self-verifying URL. A file
    is written to quarantine first; only a clean/skipped scan promotes it to
    submissions/, and the DB storage_url is written only after promotion — so a
    half-uploaded / infected file is never referenced by an accepted document.

FUNCTION GUIDE
  quarantine_key / submission_key(submission_id, kind/slot, sha256, ext) -> str
      Build the object key for the two prefix trees. CALLED FROM: pipeline.process_upload.
  StorageBackend   the interface: put/get/delete/exists (abstract) + move/url_for/presign
      (defaults). `move` promotes quarantine → submissions (copy+delete unless overridden).
  LocalStorageBackend   default (dev/tests) — local filesystem, path-traversal guarded.
  S3StorageBackend      s3/minio — lazy boto3; real presigned URLs.
  SupabaseStorageBackend production — lazy SDK; private bucket via the service key.
  get_storage() -> StorageBackend   the configured singleton (defaults to local).
      CALLED FROM: pipeline (Stage 1), bom.export, supplier_po.send (store the rendered PDFs).
  reset_storage_cache()   test hook — drop the cached backend.
================================================================================
"""
from __future__ import annotations

import os
import shutil
from abc import ABC, abstractmethod

from app.core.config import settings


# ── Key helpers (shared by every backend) ───────────────────────────────────
def quarantine_key(submission_id: str, kind: str, sha256: str, ext: str) -> str:
    return f"quarantine/{submission_id}/{kind}/{sha256}{ext}"


def submission_key(submission_id: str, slot: str, sha256: str, ext: str) -> str:
    return f"submissions/{submission_id}/{slot}/{sha256}{ext}"


class StorageBackend(ABC):
    """The interface every driver implements. URIs are scheme-prefixed so the
    stored `storage_url` is self-describing and backend-portable."""

    scheme = "mem"

    @abstractmethod
    def put(self, key: str, data: bytes) -> str:
        """Write bytes at `key`; return the backend-agnostic storage URL."""

    @abstractmethod
    def get(self, key: str) -> bytes: ...

    @abstractmethod
    def delete(self, key: str) -> None: ...

    @abstractmethod
    def exists(self, key: str) -> bool: ...

    def move(self, src_key: str, dst_key: str) -> str:
        """Promote quarantine → submissions. Default = copy+delete; drivers may
        override with a native server-side move."""
        data = self.get(src_key)
        url = self.put(dst_key, data)
        self.delete(src_key)
        return url

    def url_for(self, key: str) -> str:
        return f"{self.scheme}://{settings.supabase_bucket}/{key}"

    def presign(self, key: str, expires_seconds: int = 3600) -> str:
        """Default: return the stored URL (private API-mediated access). S3/Supabase
        override with a real signed URL."""
        return self.url_for(key)


# ── local filesystem (default; dev + tests) ─────────────────────────────────
class LocalStorageBackend(StorageBackend):
    """Mirrors the same prefix tree under LOCAL_STORAGE_DIR. No external dependency,
    so the SQLite/in-memory test path runs unchanged."""

    scheme = "file"

    def __init__(self, root: str | None = None):
        self.root = os.path.abspath(root or settings.local_storage_dir)

    def _path(self, key: str) -> str:
        # key is a forward-slash relative path; never allow escaping the root.
        safe = os.path.normpath(key).replace("\\", "/").lstrip("/")
        full = os.path.normpath(os.path.join(self.root, safe))
        # Guard against traversal. A bare `startswith(self.root)` is bypassable
        # (root "/data/store" would accept "/data/store-evil/x"), so require an exact
        # match or a real path-separator boundary.
        if full != self.root and not full.startswith(self.root + os.sep):
            raise ValueError(f"unsafe storage key: {key!r}")
        return full

    def put(self, key: str, data: bytes) -> str:
        path = self._path(key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
        return self.url_for(key)

    def get(self, key: str) -> bytes:
        with open(self._path(key), "rb") as f:
            return f.read()

    def delete(self, key: str) -> None:
        try:
            os.remove(self._path(key))
        except FileNotFoundError:
            pass

    def exists(self, key: str) -> bool:
        return os.path.exists(self._path(key))

    def move(self, src_key: str, dst_key: str) -> str:
        src, dst = self._path(src_key), self._path(dst_key)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.move(src, dst)
        return self.url_for(dst_key)

    def url_for(self, key: str) -> str:
        return f"file://{self.root}/{key}"


# ── S3 / MinIO (self-hosted staging) ────────────────────────────────────────
class S3StorageBackend(StorageBackend):
    """S3-compatible (AWS S3 or self-hosted MinIO). boto3 is imported lazily so it
    is only required when STORAGE_BACKEND=s3/minio is actually selected."""

    scheme = "s3"

    def __init__(self):
        import boto3  # lazy: only needed for this backend

        self.bucket = settings.supabase_bucket
        self._s3 = boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint_url or None,
            region_name=settings.s3_region or None,
            aws_access_key_id=settings.s3_access_key_id or None,
            aws_secret_access_key=settings.s3_secret_access_key or None,
        )

    def put(self, key: str, data: bytes) -> str:
        self._s3.put_object(Bucket=self.bucket, Key=key, Body=data)
        return self.url_for(key)

    def get(self, key: str) -> bytes:
        return self._s3.get_object(Bucket=self.bucket, Key=key)["Body"].read()

    def delete(self, key: str) -> None:
        self._s3.delete_object(Bucket=self.bucket, Key=key)

    def exists(self, key: str) -> bool:
        from botocore.exceptions import ClientError

        try:
            self._s3.head_object(Bucket=self.bucket, Key=key)
            return True
        except ClientError:
            return False

    def presign(self, key: str, expires_seconds: int = 3600) -> str:
        return self._s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket, "Key": key},
            ExpiresIn=expires_seconds,
        )


# ── Supabase Storage (PRODUCTION) ───────────────────────────────────────────
class SupabaseStorageBackend(StorageBackend):
    """Production driver. Private bucket, accessed server-side with the service key
    (never a public URL). The supabase SDK is imported lazily so the repo keeps no
    hard Supabase dependency — the explicit Stage-1 constraint."""

    scheme = "supabase"

    def __init__(self):
        from supabase import create_client  # lazy: only for this backend

        if not (settings.supabase_url and settings.supabase_service_key):
            raise RuntimeError(
                "STORAGE_BACKEND=supabase requires SUPABASE_URL + SUPABASE_SERVICE_KEY"
            )
        self.bucket = settings.supabase_bucket
        self._client = create_client(settings.supabase_url, settings.supabase_service_key)

    def _b(self):
        return self._client.storage.from_(self.bucket)

    def put(self, key: str, data: bytes) -> str:
        self._b().upload(key, data, {"upsert": "true"})
        return self.url_for(key)

    def get(self, key: str) -> bytes:
        return self._b().download(key)

    def delete(self, key: str) -> None:
        self._b().remove([key])

    def exists(self, key: str) -> bool:
        try:
            self._b().download(key)
            return True
        except Exception:
            return False

    def presign(self, key: str, expires_seconds: int = 3600) -> str:
        res = self._b().create_signed_url(key, expires_seconds)
        return res.get("signedURL") or res.get("signed_url") or self.url_for(key)


# ── factory (one instance per process) ──────────────────────────────────────
_BACKENDS = {
    "local": LocalStorageBackend,
    "s3": S3StorageBackend,
    "minio": S3StorageBackend,
    "supabase": SupabaseStorageBackend,
}

_instance: StorageBackend | None = None


def get_storage() -> StorageBackend:
    """Return the configured storage backend (singleton). Defaults to local."""
    global _instance
    if _instance is None:
        cls = _BACKENDS.get(settings.storage_backend.lower(), LocalStorageBackend)
        _instance = cls()
    return _instance


def reset_storage_cache() -> None:
    """Test hook: drop the cached backend so a changed setting / dir takes effect."""
    global _instance
    _instance = None