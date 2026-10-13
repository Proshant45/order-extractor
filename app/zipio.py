"""Safe, streaming ZIP iteration. Never extracts everything; one entry on disk at a time."""
from __future__ import annotations
import shutil, tempfile, zipfile
from pathlib import Path, PurePosixPath
from . import config

JUNK_NAMES = {".ds_store", "thumbs.db", "desktop.ini", "icon\r"}

def junk_reason(name: str) -> str | None:
    p = PurePosixPath(name.replace("\\", "/"))
    if name.startswith("/") or ".." in p.parts or (len(name) > 1 and name[1] == ":"):
        return "path traversal / absolute path"
    if any(part == "__MACOSX" for part in p.parts):
        return "macOS resource fork (__MACOSX)"
    if p.name.lower() in JUNK_NAMES:
        return f"system file ({p.name})"
    if any(part.startswith(".") for part in p.parts) or p.name.startswith("~$"):
        return "hidden/temp file"
    return None

def iter_zip(path: Path, depth: int = 0):
    """Yields (name, temp_path|None, skip_reason|None). Caller must delete temp_path."""
    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile:
        yield (path.name, None, "corrupt zip"); return
    with zf:
        infos = [i for i in zf.infolist() if not i.is_dir()]
        if len(infos) > config.ZIP_MAX_ENTRIES:
            yield (path.name, None, f"too many entries ({len(infos)} > {config.ZIP_MAX_ENTRIES})"); return
        declared = sum(i.file_size for i in infos)
        if declared > config.ZIP_MAX_TOTAL_BYTES:
            yield (path.name, None, f"total uncompressed size {declared} exceeds limit"); return
        written_total = 0
        for info in infos:
            name = info.filename
            r = junk_reason(name)
            if r:
                yield (name, None, r); continue
            if info.flag_bits & 0x1:
                yield (name, None, "encrypted entry"); continue
            if info.file_size > config.ZIP_MAX_ENTRY_BYTES:
                yield (name, None, f"entry too large ({info.file_size} bytes)"); continue
            if info.compress_size and info.file_size / info.compress_size > config.ZIP_MAX_RATIO:
                yield (name, None, f"suspicious compression ratio {info.file_size/info.compress_size:.0f}x (zip bomb guard)"); continue
            suffix = PurePosixPath(name).suffix[:10]
            fd, tmp = tempfile.mkstemp(suffix=suffix, dir=config.TMP_DIR)
            tmp = Path(tmp)
            try:
                n = 0
                with zf.open(info) as src, open(fd, "wb") as dst:
                    while chunk := src.read(config.CHUNK):
                        n += len(chunk); written_total += len(chunk)
                        # real bytes checked too: header sizes can lie
                        if n > config.ZIP_MAX_ENTRY_BYTES or written_total > config.ZIP_MAX_TOTAL_BYTES:
                            raise ValueError("uncompressed size limit hit while streaming")
                        dst.write(chunk)
            except Exception as e:
                tmp.unlink(missing_ok=True)
                yield (name, None, f"could not extract: {e.__class__.__name__}: {e}"); continue
            with open(tmp, "rb") as f:
                is_zip = f.read(4) == b"PK\x03\x04" and suffix.lower() not in (".xlsx", ".docx")
            if is_zip:
                if depth >= config.ZIP_MAX_DEPTH:
                    tmp.unlink(missing_ok=True)
                    yield (name, None, "nested zip beyond depth limit"); continue
                try:
                    for sub_name, sub_tmp, sub_reason in iter_zip(tmp, depth + 1):
                        yield (f"{name}/{sub_name}", sub_tmp, sub_reason)
                finally:
                    tmp.unlink(missing_ok=True)
                continue
            yield (name, tmp, None)
