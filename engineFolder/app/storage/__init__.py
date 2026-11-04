"""Archive storage interface.

Backup, archive and restore code talks ONLY to `ArchiveStore`. Adding Google Drive in
Stage 2 means adding one class here; nothing that uses the store changes.

Contract every backend must satisfy (tests/test_storage.py runs it against each one):
  put(key, data, content_type)  -> StoredObject   (fails if key exists: archives are write-once)
  get(key)                      -> bytes          (raises NotFound)
  exists(key)                   -> bool
  list(prefix)                  -> list[str]      (keys under prefix)
  delete(key)                   -> None           (only used by retention pruning)
"""
import hashlib
from dataclasses import dataclass

from .. import config


class StorageError(Exception):
    pass


class NotFound(StorageError):
    pass


class AlreadyExists(StorageError):
    pass


@dataclass
class StoredObject:
    backend: str
    key: str
    size: int
    sha256: str


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class ArchiveStore:
    name = "base"

    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> StoredObject:
        raise NotImplementedError

    def get(self, key: str) -> bytes:
        raise NotImplementedError

    def exists(self, key: str) -> bool:
        raise NotImplementedError

    def list(self, prefix: str) -> list:
        raise NotImplementedError

    def delete(self, key: str) -> None:
        raise NotImplementedError

    def put_verified(self, key, data, content_type="application/octet-stream") -> StoredObject:
        """Write, read back and compare checksums. Archive writes always go through this."""
        expected = sha256(data)
        obj = self.put(key, data, content_type)
        back = self.get(key)
        if sha256(back) != expected:
            raise StorageError(f"Checksum mismatch after writing {key}")
        obj.sha256 = expected
        return obj


def validate_key(key: str):
    if not key or key.startswith("/") or ".." in key.split("/") or "\\" in key or "\x00" in key:
        raise StorageError(f"Unsafe storage key: {key!r}")
    return key


_store = None


def get_store() -> ArchiveStore:
    global _store
    if _store is None or _store.name != config.STORAGE_BACKEND:
        backend = config.STORAGE_BACKEND
        if backend == "local":
            from .local import LocalStore
            _store = LocalStore(config.LOCAL_STORE_DIR)
        elif backend == "supabase":
            from .supabase import SupabaseStore
            _store = SupabaseStore(config.SUPABASE_URL, config.SUPABASE_SERVICE_KEY, config.SUPABASE_BUCKET)
        elif backend == "gdrive":
            raise StorageError("Google Drive backend arrives in Stage 2")
        else:
            raise StorageError(f"Unknown STORAGE_BACKEND {backend!r}")
    return _store


def set_store(store):
    """Tests: inject a backend."""
    global _store
    _store = store
