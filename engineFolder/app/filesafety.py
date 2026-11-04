"""Upload safety: decide what a file really is from its bytes, and open archives defensively.

Rules
- The file type is decided from content (magic bytes), never from the extension alone.
- Zip: <= ZIP_MAX_MEMBERS members, no directories-escaping names, no absolute paths, no
  symlinks, no encryption, no nested archives, per-member compression ratio limit, and the
  *actual* bytes read are counted against the total limit (declared sizes can lie).
- Gzip: decompressed in chunks with the same total limit.
- Executables / unknown binary content are rejected.
"""
import gzip
import hashlib
import io
import json
import stat
import zipfile
import zlib

from . import config

VALIDATOR_VERSION = "1.0.0"


class UnsafeFile(Exception):
    pass


ALLOWED_KINDS = {
    "span": {"zip", "gzip", "xml"},
    "contract_master": {"zip", "gzip", "text"},
    "lot_sizes": {"text"},
    "elm": {"zip", "gzip", "text", "xlsx"},
    "bhavcopy": {"zip", "gzip", "text"},
    "settlement": {"zip", "text"},
    "ban_list": {"text"},
    "var": {"zip", "gzip", "text"},
    "security_master": {"zip", "text"},
    "surveillance": {"text", "json", "xlsx"},
}
INNER_KINDS = {"xml", "text", "json"}   # what may sit inside a zip/gzip

BAD_MAGIC = [b"MZ", b"\x7fELF", b"\xca\xfe\xba\xbe", b"\xcf\xfa\xed\xfe", b"#!", b"%PDF", b"Rar!", b"7z\xbc\xaf", b"\xfd7zXZ", b"BZh"]


def _limit_bytes():
    return config.ZIP_MAX_TOTAL_UNCOMPRESSED_MB * 1024 * 1024


def sniff(data: bytes) -> str:
    head = data[:8]
    if head.startswith(b"PK\x03\x04") or head.startswith(b"PK\x05\x06"):
        try:
            names = zipfile.ZipFile(io.BytesIO(data)).namelist()
        except zipfile.BadZipFile:
            raise UnsafeFile("Corrupt zip file")
        return "xlsx" if "[Content_Types].xml" in names else "zip"
    if head.startswith(b"\x1f\x8b"):
        return "gzip"
    for m in BAD_MAGIC:
        if data.startswith(m):
            raise UnsafeFile("Executable, script or unsupported archive content is not accepted")
    sample = data[:65536]
    if b"\x00" in sample:
        raise UnsafeFile("Binary content of unknown type is not accepted")
    try:
        text = sample.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            text = sample.decode("cp1252")
        except UnicodeDecodeError:
            raise UnsafeFile("File is not readable text")
    t = text.lstrip()
    if t.startswith("<?xml") or t.startswith("<spanFile") or (t.startswith("<") and not t.lower().startswith(("<html", "<!doctype html"))):
        return "xml"
    if t.startswith(("{", "[")):
        try:
            json.loads(data.decode("utf-8-sig"))
            return "json"
        except Exception:
            pass
    return "text"


def _safe_name(name: str):
    n = name.replace("\\", "/")
    if n.startswith("/") or (len(n) > 1 and n[1] == ":") or any(p == ".." for p in n.split("/")) or "\x00" in n:
        raise UnsafeFile(f"Archive member has an unsafe path: {name!r}")


def _read_capped(stream, cap, label):
    out = bytearray()
    while True:
        chunk = stream.read(1024 * 1024)
        if not chunk:
            break
        out += chunk
        if len(out) > cap:
            raise UnsafeFile(f"{label} expands beyond the {config.ZIP_MAX_TOTAL_UNCOMPRESSED_MB} MB limit (possible zip bomb)")
    return bytes(out)


def inspect(data: bytes, file_type: str):
    """Validate an upload. Returns dict(kind, members=[{name,size,sha256,kind}], primary=bytes|None)."""
    if not data:
        raise UnsafeFile("File is empty")
    kind = sniff(data)
    allowed = ALLOWED_KINDS.get(file_type)
    if allowed is None:
        raise UnsafeFile(f"Unknown file type {file_type!r}")
    if kind not in allowed:
        raise UnsafeFile(f"A {file_type} file must be one of {sorted(allowed)}, but this file's content is {kind}")
    members, primary = [], None
    if kind == "zip":
        zf = zipfile.ZipFile(io.BytesIO(data))
        infos = zf.infolist()
        files = [i for i in infos if not i.is_dir()]
        if not files:
            raise UnsafeFile("Zip file is empty")
        if len(infos) > config.ZIP_MAX_MEMBERS:
            raise UnsafeFile(f"Zip has {len(infos)} entries; the limit is {config.ZIP_MAX_MEMBERS}")
        total = 0
        for i in infos:
            _safe_name(i.filename)
            mode = (i.external_attr >> 16) & 0xFFFF
            if stat.S_ISLNK(mode):
                raise UnsafeFile(f"Zip contains a symbolic link: {i.filename}")
            if i.flag_bits & 0x1:
                raise UnsafeFile("Encrypted zip files are not accepted")
            if i.is_dir():
                continue
            if i.compress_size and i.file_size / max(i.compress_size, 1) > config.ZIP_MAX_RATIO:
                raise UnsafeFile(f"{i.filename} has a suspicious compression ratio (possible zip bomb)")
            try:
                with zf.open(i) as fh:
                    body = _read_capped(fh, _limit_bytes() - total, i.filename)
            except (zipfile.BadZipFile, zlib.error, EOFError) as e:
                raise UnsafeFile(f"Corrupt zip member {i.filename}: {e}")
            total += len(body)
            inner = sniff(body)
            if inner not in INNER_KINDS:
                raise UnsafeFile(f"Nested archives are not accepted ({i.filename} is {inner})")
            members.append({"name": i.filename, "size": len(body), "sha256": hashlib.sha256(body).hexdigest(), "kind": inner})
            if primary is None or (file_type == "span" and inner == "xml" and members[-1]["size"] > len(primary)):
                primary = body
    elif kind == "gzip":
        try:
            body = _read_capped(gzip.GzipFile(fileobj=io.BytesIO(data)), _limit_bytes(), "gzip content")
        except (OSError, EOFError, zlib.error) as e:
            raise UnsafeFile(f"Corrupt gzip file: {e}")
        if len(body) / max(len(data), 1) > config.ZIP_MAX_RATIO:
            raise UnsafeFile("Suspicious compression ratio (possible gzip bomb)")
        inner = sniff(body)
        if inner not in INNER_KINDS:
            raise UnsafeFile(f"Nested archives are not accepted (gzip contains {inner})")
        members.append({"name": "(gzip content)", "size": len(body), "sha256": hashlib.sha256(body).hexdigest(), "kind": inner})
        primary = body
    elif kind == "xlsx":
        zf = zipfile.ZipFile(io.BytesIO(data))
        if len(zf.infolist()) > 200 or sum(i.file_size for i in zf.infolist()) > _limit_bytes():
            raise UnsafeFile("Workbook is too large")
        primary = None
    else:
        primary = data
    for blob in ([primary] if primary else []):
        head = blob[:200000].upper()
        if b"<!DOCTYPE" in head or b"<!ENTITY" in head:
            raise UnsafeFile("XML with DOCTYPE/ENTITY declarations is not accepted")
    if file_type == "span":
        xml_members = [m for m in members if m["kind"] == "xml"]
        if kind in ("zip", "gzip") and not xml_members:
            raise UnsafeFile("SPAN archive contains no XML/.spn file (only the XML SPAN format is supported)")
        if kind == "zip" and len(xml_members) > 1:
            raise UnsafeFile("SPAN zip contains more than one XML file - upload them separately")
    return {"kind": kind, "members": members, "primary": primary, "validator_version": VALIDATOR_VERSION}
