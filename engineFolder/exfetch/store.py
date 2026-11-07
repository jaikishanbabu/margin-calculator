"""Where files go, what's already been fetched, the latest/ folder, and unpacking."""
from __future__ import annotations

import gzip
import hashlib
import io
import shutil
import sqlite3
import zipfile
from datetime import date, datetime
from pathlib import Path


class Store:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.root / "manifest.sqlite")
        self.db.execute("""CREATE TABLE IF NOT EXISTS files(
            source TEXT, trade_date TEXT, variant TEXT, url TEXT, path TEXT,
            sha256 TEXT, bytes INTEGER, fetched_at TEXT,
            PRIMARY KEY(source, trade_date, variant, path))""")
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(files)")}
        if "rank" not in cols:                     # upgrade older manifests
            self.db.execute("ALTER TABLE files ADD COLUMN rank INTEGER DEFAULT 0")
        self.db.execute("""CREATE TABLE IF NOT EXISTS attempts(
            source TEXT, trade_date TEXT, variant TEXT, at TEXT, status TEXT, detail TEXT)""")
        self.db.commit()

    def folder(self, segment: str, kind: str, d: date) -> Path:
        p = self.root / segment / kind / d.isoformat()
        p.mkdir(parents=True, exist_ok=True)
        return p

    # ---- what we already have ------------------------------------------------
    def have(self, source: str, d: date, variant: str) -> bool:
        return self.db.execute("SELECT 1 FROM files WHERE source=? AND trade_date=? AND variant=?",
                               (source, d.isoformat(), variant)).fetchone() is not None

    def have_name(self, source: str, d: date, name: str) -> bool:
        return self.db.execute("SELECT 1 FROM files WHERE source=? AND trade_date=? AND path LIKE ?",
                               (source, d.isoformat(), f"%/{name}")).fetchone() is not None

    def same_as_saved(self, source: str, d: date, name: str, data: bytes) -> bool:
        row = self.db.execute("SELECT sha256 FROM files WHERE source=? AND trade_date=? AND path LIKE ?",
                              (source, d.isoformat(), f"%/{name}")).fetchone()
        return bool(row) and row[0] == hashlib.sha256(data).hexdigest()

    # ---- saving ----------------------------------------------------------------
    def save(self, source: str, segment: str, kind: str, d: date, variant: str,
             url: str, name: str, data: bytes, extract: bool, rank: int = 0) -> Path:
        folder = self.folder(segment, kind, d)
        path = folder / name
        tmp = path.with_suffix(path.suffix + ".part")
        tmp.write_bytes(data)
        tmp.replace(path)                       # atomic: never a half-written file
        sha = hashlib.sha256(data).hexdigest()
        self.db.execute("DELETE FROM files WHERE source=? AND trade_date=? AND path=?",
                        (source, d.isoformat(), str(path)))
        self.db.execute("""INSERT INTO files(source, trade_date, variant, url, path, sha256,
                           bytes, fetched_at, rank) VALUES(?,?,?,?,?,?,?,?,?)""",
                        (source, d.isoformat(), variant, url, str(path), sha, len(data),
                         datetime.now().isoformat(timespec="seconds"), rank))
        self.db.commit()
        if extract:
            unpack(path, folder)
        return path

    def prune(self, source: str, keep: int) -> list[str]:
        """Retention for one source (`retain_days: N` in sources.yaml): keep its files for
        the newest N trade dates, delete the older ones (the files this source saved, and
        their manifest rows). latest/ and every other source are never touched."""
        days = [r[0] for r in self.db.execute(
            "SELECT DISTINCT trade_date FROM files WHERE source=? ORDER BY trade_date DESC", (source,))]
        old = days[keep:]
        removed = []
        for d in old:
            for (path,) in self.db.execute("SELECT path FROM files WHERE source=? AND trade_date=?", (source, d)).fetchall():
                p = Path(path)
                if p.is_file():
                    p.unlink()
                    removed.append(str(p))
            self.db.execute("DELETE FROM files WHERE source=? AND trade_date=?", (source, d))
        if old:
            self.db.commit()
        return removed

    def log(self, source: str, d: date, variant: str, status: str, detail: str = ""):
        self.db.execute("INSERT INTO attempts VALUES(?,?,?,?,?,?)",
                        (source, d.isoformat(), variant,
                         datetime.now().isoformat(timespec="seconds"), status, detail[:500]))
        self.db.commit()

    # ---- latest/ ----------------------------------------------------------------
    def newest(self, source: str) -> tuple[str, str, int] | None:
        """(path, trade_date, rank) of the newest file for a source."""
        return self.db.execute("""SELECT path, trade_date, rank FROM files WHERE source=?
                                  ORDER BY trade_date DESC, rank DESC, fetched_at DESC LIMIT 1""",
                               (source,)).fetchone()

    def update_latest(self, source: str, segment: str, kind: str) -> Path | None:
        """Mirror the newest file of `source` (and its unpacked contents) into
        data/<SEG>/<kind>/latest/<source>/  so the calculator always reads one place."""
        row = self.newest(source)
        if not row or not Path(row[0]).exists():
            return None
        src = Path(row[0])
        dest = self.root / segment / kind / "latest" / source
        marker = dest / ".source"
        if marker.exists() and marker.read_text() == str(src):
            return dest                                  # already current
        tmp = dest.with_name(dest.name + ".tmp")
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True)
        shutil.copy2(src, tmp / src.name)
        unpack(tmp / src.name, tmp)
        (tmp / ".source").write_text(str(src))
        (tmp / "INFO.txt").write_text(f"trade_date={row[1]}\nfile={src.name}\nupdated={datetime.now():%Y-%m-%d %H:%M:%S}\n")
        shutil.rmtree(dest, ignore_errors=True)
        tmp.rename(dest)
        return dest

    def status(self, d: date):
        return self.db.execute("""SELECT source, variant, bytes, path FROM files
                                  WHERE trade_date=? ORDER BY source, rank, variant""",
                               (d.isoformat(),)).fetchall()


def unpack(path: Path, dest: Path):
    """Unzip / gunzip next to the original. Guards against zip-slip."""
    name = path.name.lower()
    try:
        if name.endswith(".zip") and zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as z:
                for m in z.infolist():
                    if m.is_dir():
                        continue
                    target = (dest / Path(m.filename).name).resolve()
                    if dest.resolve() not in target.parents:
                        continue
                    with z.open(m) as src, open(target, "wb") as out:
                        shutil.copyfileobj(src, out)
                    if target.name.lower().endswith((".zip", ".gz")):
                        unpack(target, dest)
        elif name.endswith(".gz"):
            target = dest / path.name[:-3]
            with gzip.open(path) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out)
    except (zipfile.BadZipFile, OSError, EOFError) as e:
        print(f"[store] could not unpack {path.name}: {e}")


def looks_like_archive_ok(name: str, data: bytes) -> bool:
    """Reject truncated zips/gz before they reach disk."""
    n = name.lower()
    if n.endswith(".zip"):
        return zipfile.is_zipfile(io.BytesIO(data))
    if n.endswith(".gz"):
        try:
            gzip.decompress(data)
            return True
        except (OSError, EOFError):
            return False
    return True
