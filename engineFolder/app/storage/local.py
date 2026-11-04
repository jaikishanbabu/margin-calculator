"""Local-folder backend (dev/test only - Render's disk is wiped on restart)."""
import os
from pathlib import Path

from . import AlreadyExists, ArchiveStore, NotFound, StoredObject, sha256, validate_key


class LocalStore(ArchiveStore):
    name = "local"

    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key):
        p = (self.root / validate_key(key)).resolve()
        if self.root not in p.parents:
            raise NotFound(key)
        return p

    def put(self, key, data, content_type="application/octet-stream"):
        p = self._path(key)
        if p.exists():
            raise AlreadyExists(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".part")
        tmp.write_bytes(data)
        os.replace(tmp, p)
        return StoredObject(self.name, key, len(data), sha256(data))

    def get(self, key):
        p = self._path(key)
        if not p.is_file():
            raise NotFound(key)
        return p.read_bytes()

    def exists(self, key):
        return self._path(key).is_file()

    def list(self, prefix):
        base = self.root
        out = []
        for p in base.rglob("*"):
            if p.is_file() and not p.name.endswith(".part"):
                k = p.relative_to(base).as_posix()
                if k.startswith(prefix):
                    out.append(k)
        return sorted(out)

    def delete(self, key):
        p = self._path(key)
        if p.is_file():
            p.unlink()
