"""Supabase Storage backend (private bucket), using the Storage REST API with stdlib urllib.

The bucket must be PRIVATE. Objects are write-once (x-upsert: false).
"""
import json
import ssl
import urllib.error
import urllib.parse
import urllib.request

from . import AlreadyExists, ArchiveStore, NotFound, StorageError, StoredObject, sha256, validate_key


class SupabaseStore(ArchiveStore):
    name = "supabase"

    def __init__(self, url, key, bucket, timeout=120):
        if not url or not key:
            raise StorageError("SUPABASE_URL / SUPABASE_SERVICE_KEY missing")
        self.base = url.rstrip("/") + "/storage/v1"
        self.key = key
        self.bucket = bucket
        self.timeout = timeout
        self.ctx = ssl.create_default_context()

    def _req(self, method, path, body=None, headers=None):
        h = {"Authorization": f"Bearer {self.key}", "apikey": self.key}
        h.update(headers or {})
        req = urllib.request.Request(self.base + path, data=body, method=method, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=self.ctx if self.base.startswith("https") else None) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()
        except urllib.error.URLError as e:
            raise StorageError(f"Supabase Storage unreachable: {e.reason}")

    def _obj(self, key):
        return f"/object/{self.bucket}/" + urllib.parse.quote(validate_key(key))

    def put(self, key, data, content_type="application/octet-stream"):
        st, body = self._req("POST", self._obj(key), data,
                             {"Content-Type": content_type, "x-upsert": "false", "cache-control": "no-store"})
        if st in (200, 201):
            return StoredObject(self.name, key, len(data), sha256(data))
        text = body.decode(errors="replace")
        if st == 409 or "Duplicate" in text or "already exists" in text:
            raise AlreadyExists(key)
        raise StorageError(f"Supabase put {key} failed: HTTP {st} {text[:200]}")

    def get(self, key):
        st, body = self._req("GET", f"/object/authenticated/{self.bucket}/" + urllib.parse.quote(validate_key(key)))
        if st == 200:
            return body
        if st in (400, 404):
            raise NotFound(key)
        raise StorageError(f"Supabase get {key} failed: HTTP {st}")

    def exists(self, key):
        try:
            self.get(key)
            return True
        except NotFound:
            return False

    def list(self, prefix):
        """List recursively under prefix (Supabase lists one folder level per call)."""
        out = []
        folders = [prefix.rstrip("/")]
        while folders:
            folder = folders.pop()
            offset = 0
            while True:
                payload = json.dumps({"prefix": folder, "limit": 1000, "offset": offset,
                                      "sortBy": {"column": "name", "order": "asc"}}).encode()
                st, body = self._req("POST", f"/object/list/{self.bucket}", payload, {"Content-Type": "application/json"})
                if st != 200:
                    raise StorageError(f"Supabase list failed: HTTP {st}")
                items = json.loads(body or b"[]")
                for it in items:
                    full = f"{folder}/{it['name']}" if folder else it["name"]
                    if it.get("id") is None and it.get("metadata") is None:
                        folders.append(full)  # sub-folder
                    else:
                        out.append(full)
                if len(items) < 1000:
                    break
                offset += 1000
        return sorted(out)

    def delete(self, key):
        payload = json.dumps({"prefixes": [validate_key(key)]}).encode()
        st, body = self._req("DELETE", f"/object/{self.bucket}", payload, {"Content-Type": "application/json"})
        if st not in (200, 204):
            raise StorageError(f"Supabase delete {key} failed: HTTP {st}")
