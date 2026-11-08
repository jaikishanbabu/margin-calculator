"""Find a segment's input files on disk.

Default: data/<SEG>/<kind>/latest/<source id>/ (trade date from INFO.txt).
With a date: data/<SEG>/<kind>/<YYYY-MM-DD>/, the history exfetch keeps.
Files are read unpacked, as exfetch leaves them.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path


class MissingFile(FileNotFoundError):
    pass


@dataclass
class Input:
    key: str
    path: Path
    trade_date: str          # YYYY-MM-DD
    groups: tuple = ()       # regex groups from the file name (e.g. the SPAN cycle)


@dataclass
class Want:
    kind: str                # data/<SEG>/<kind>/
    source: str              # exfetch source id = latest/<source>/
    pattern: str             # file name regex
    required: bool = True
    rank: object = None      # pick the best of several matches in a history folder
    exact: bool = False      # date mode: must be that day's file (else the newest earlier day)


def _info_date(folder: Path) -> str | None:
    f = folder / "INFO.txt"
    if f.exists():
        for line in f.read_text().splitlines():
            if line.startswith("trade_date="):
                return line.split("=", 1)[1].strip()
    return None


def _hits(folder: Path, w: Want) -> list:
    rx = re.compile(w.pattern)
    hits = []
    if folder.is_dir():
        for p in folder.iterdir():
            m = rx.search(p.name)
            if m and p.is_file():
                hits.append((p, m.groups()))
    hits.sort(key=(lambda h: w.rank(h[1])) if w.rank else (lambda h: h[0].name))
    return hits


def _days(data_dir: Path, segment: str, kind: str) -> list[str]:
    """History folder dates for a kind, newest first."""
    base = Path(data_dir) / segment / kind
    return sorted((p.name for p in base.iterdir() if re.fullmatch(r"\d{4}-\d{2}-\d{2}", p.name)),
                  reverse=True) if base.is_dir() else []


def find(data_dir: Path, segment: str, wants: dict[str, Want],
         on_date: date | None = None) -> dict[str, Input]:
    out = {}
    for key, w in wants.items():
        if on_date:
            days = [on_date.isoformat()] if w.exact else \
                [d for d in _days(data_dir, segment, w.kind) if d <= on_date.isoformat()]
            folder, hits, trade_date = None, [], None
            for day in days or [on_date.isoformat()]:
                folder = Path(data_dir) / segment / w.kind / day
                hits = _hits(folder, w)
                if hits:
                    trade_date = day
                    break
        else:
            folder = Path(data_dir) / segment / w.kind / "latest" / w.source
            trade_date = _info_date(folder)
            hits = _hits(folder, w)
        if not hits:
            if w.required:
                raise MissingFile(f"{key}: no file matching {w.pattern} in {folder}")
            continue
        p, groups = hits[-1]
        out[key] = Input(key, p, trade_date or "", groups)
    return out


def find_before(data_dir: Path, segment: str, w: Want, before: str) -> Input | None:
    """Newest file of a kind from a history folder dated strictly before `before` (YYYY-MM-DD)."""
    for day in _days(data_dir, segment, w.kind):
        if day < before:
            hits = _hits(Path(data_dir) / segment / w.kind / day, w)
            if hits:
                return Input("", hits[-1][0], day, hits[-1][1])
    return None
