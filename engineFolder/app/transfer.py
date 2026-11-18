"""Direct storage transfer for the fetcher job (keeps big files off the engine host's bandwidth).

Render's free plan meters OUTBOUND bytes. Before this module, every exchange file went
GitHub Actions -> engine -> Supabase Storage, and every rebuild pulled every input back
Supabase -> engine -> GitHub Actions: the engine sent each file out at least twice.

Now, when the archive backend can sign links (Supabase):
  * downloads: the engine checks the request and hands back a short-lived signed link; the job
    downloads straight from storage and checks the SHA-256 itself.
  * uploads: the job asks for a signed upload link under incoming/<run>/, puts the bytes there, then
    calls the usual upload endpoint naming the staged key. The engine reads the staged copy (inbound,
    free), runs the SAME checks as before, makes the archive copy inside storage, verifies it and
    deletes the staged object.
Backends that cannot sign (local disk, tests) keep the old path: bytes through the engine.
"""
import gzip
import io
import re
import secrets

from . import config
from .storage import NotFound, StorageError, get_store
from .uploads import decode, safe_filename

STAGE_PREFIX = "incoming"
ENCODINGS = ("identity", "gzip")
_KEY_RE = re.compile(r"^incoming/(\d+)/[0-9a-f]{16}_[A-Za-z0-9._-]{1,140}$")


class TransferError(Exception):
    pass


def stage_slot(run_id, filename):
    """Signed upload link for one staged object, or None if the backend can't sign."""
    key = f"{STAGE_PREFIX}/{int(run_id)}/{secrets.token_hex(8)}_{safe_filename(filename)}"
    url = get_store().sign_upload(key)
    return {"key": key, "url": url} if url else None


def _check_key(run_id, key):
    m = _KEY_RE.match(key or "")
    if not m or int(m.group(1)) != int(run_id):
        raise TransferError("staged_key does not belong to this run")
    return key


def load_staged(run_id, key, encoding, max_bytes):
    """Read a staged object (inbound to the engine). Returns (payload_as_stored, original_bytes)."""
    _check_key(run_id, key)
    if encoding not in ENCODINGS:
        raise TransferError("staged_encoding must be identity or gzip")
    try:
        payload = get_store().get(key)
    except NotFound:
        raise TransferError("staged file not found - upload it to the signed link first")
    except StorageError as e:
        raise TransferError(f"could not read staged file: {e}")
    if len(payload) > max_bytes:
        raise TransferError(f"staged file is larger than {max_bytes // 1048576} MB")
    if encoding == "gzip":
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(payload)) as g:
                data = g.read(max_bytes + 1)
        except Exception as e:
            raise TransferError(f"staged file is not valid gzip: {e}")
        if len(data) > max_bytes:
            raise TransferError(f"staged file is larger than {max_bytes // 1048576} MB when decompressed")
    else:
        data = payload
    return payload, data


def discard(run_id, key):
    """Best effort: staged objects are temporary. Leftovers are swept by sweep_incoming()."""
    try:
        _check_key(run_id, key)
        get_store().delete(key)
    except Exception:
        pass


def sweep_incoming(keep_runs=()):
    """Delete staged objects of finished runs (called when a run finishes)."""
    store, removed = get_store(), 0
    try:
        for k in store.list(STAGE_PREFIX):
            m = _KEY_RE.match(k)
            if m and int(m.group(1)) in {int(r) for r in keep_runs}:
                continue
            try:
                store.delete(k)
                removed += 1
            except StorageError:
                pass
    except StorageError:
        pass
    return removed


def original_link(row, expires=900):
    """Signed download link for an archived original, or None if the backend can't sign."""
    url = get_store().sign_download(row["archive_key"], expires)
    if not url:
        return None
    return {"url": url, "encoding": row.get("archive_encoding") or "identity", "sha256": row["sha256"],
            "filename": safe_filename(row["filename"]), "expires_in": expires}


def max_upload_bytes():
    return config.MAX_UPLOAD_MB * 1024 * 1024


__all__ = ["TransferError", "stage_slot", "load_staged", "discard", "sweep_incoming", "original_link", "decode"]
