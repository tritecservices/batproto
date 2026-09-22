"""Ingest a file share - the NAS, a mapped drive, a project folder.

This is the highest-value source in the whole system and the only one nobody else
has: your own survey data, QGIS projects, method statements, Kaleidoscope exports and
NightArc outputs. Everything else in this archive is public knowledge.

Handled specially rather than as flat text:
  .qgs/.qgz   project title, CRS, and every layer with its provider and data source -
              so you can answer "which projects point at that moved network path"
  .wav        GUANO metadata from the `guan` RIFF sub-chunk only. Audio is never read.
  .csv/.tsv   header, row count and a sample. A 200k-row id.csv is a table, not prose.
  .docx/.xlsx unzipped XML, no third-party dependency needed
  .pdf        via pypdf if installed, skipped with a note otherwise

Safety:
  * Mount the share READ-ONLY. See docs/NAS-INGEST.md.
  * Files matching SENSITIVE_HINTS are tagged `sensitive` so agents can be told to
    cite them without reproducing grid references for protected species.
  * Per-file size cap, and a directory exclusion list that covers the junk macOS and
    Windows scatter across shares.
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
import struct
import time
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from pathlib import Path

from ..store import Document, Store

# ---------------------------------------------------------------------- config
DEFAULT_MAX_MB = float(os.environ.get("BRAIN_FILE_MAX_MB", "25"))
DEFAULT_CSV_SAMPLE = int(os.environ.get("BRAIN_CSV_SAMPLE_ROWS", "15"))

EXCLUDE_DIRS = {
    ".git", ".svn", "node_modules", "__pycache__", ".venv", "venv",
    ".Trashes", ".Spotlight-V100", ".fseventsd", ".DocumentRevisions-V100",
    ".TemporaryItems", "@eaDir", "$RECYCLE.BIN", "System Volume Information",
    ".DS_Store", "Network Trash Folder", "Temporary Items",
}
EXCLUDE_FILE_PATTERNS = (
    re.compile(r"^\._"),          # macOS AppleDouble resource forks, everywhere on SMB
    re.compile(r"^~\$"),          # Office lock files
    re.compile(r"^\.DS_Store$"),
    re.compile(r"^Thumbs\.db$", re.I),
    re.compile(r"^desktop\.ini$", re.I),
)

TEXT_EXT = {".txt", ".md", ".markdown", ".rst", ".log", ".json", ".xml", ".sql",
            ".py", ".ps1", ".bat", ".sh", ".yaml", ".yml", ".ini", ".cfg", ".r",
            ".vbs", ".js", ".css", ".html", ".htm", ".gpkg-journal"}
TABLE_EXT = {".csv", ".tsv"}
QGIS_EXT = {".qgs", ".qgz"}
OFFICE_EXT = {".docx", ".xlsx", ".pptx"}
AUDIO_EXT = {".wav"}
PDF_EXT = {".pdf"}

from ..sensitivity import (GRID_REF, SENSITIVE_CODES, SENSITIVE_HINTS,  # noqa: F401
                           classify_sensitivity)


@dataclass
class Extracted:
    text: str
    title: str
    kind: str
    meta: dict


# ------------------------------------------------------------------ extractors
def _read_text(path: Path, cap: int) -> str:
    raw = path.read_bytes()[:cap]
    for enc in ("utf-8", "utf-8-sig", "cp1252", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def extract_table(path: Path, sample: int = DEFAULT_CSV_SAMPLE) -> Extracted:
    """Summarise, don't dump. Kaleidoscope id.csv files run to hundreds of thousands
    of rows; what you want indexed is the shape plus which species appear."""
    delim = "\t" if path.suffix.lower() == ".tsv" else ","
    rows: list[list[str]] = []
    total = 0
    with path.open("r", encoding="utf-8", errors="replace", newline="") as fh:
        for i, row in enumerate(csv.reader(fh, delimiter=delim)):
            total += 1
            if i <= sample:
                rows.append(row)
    header = rows[0] if rows else []
    body = rows[1:] if len(rows) > 1 else []

    lines = [f"# {path.name}", "",
             f"Delimited table, {total - 1 if total else 0} data rows, "
             f"{len(header)} columns.", "", "## Columns", ""]
    lines += [f"- {c}" for c in header if c.strip()]

    # value summaries for the columns that actually carry meaning in this domain
    interesting = {}
    for idx, col in enumerate(header):
        if re.search(r"(auto|manual)\s*id|species|site|folder|filter", col, re.I):
            vals = {r[idx].strip() for r in body if idx < len(r) and r[idx].strip()}
            if vals:
                interesting[col] = sorted(vals)[:25]
    if interesting:
        lines += ["", "## Values seen in the sample", ""]
        for col, vals in interesting.items():
            lines.append(f"- **{col}**: {', '.join(vals)}")
    if body:
        lines += ["", "## Sample rows", "", "| " + " | ".join(header) + " |",
                  "|" + "---|" * len(header)]
        for r in body:
            cells = [(c or "").replace("|", "\\|")[:60] for c in r[:len(header)]]
            cells += [""] * (len(header) - len(cells))
            lines.append("| " + " | ".join(cells) + " |")

    return Extracted("\n".join(lines), path.stem, "table",
                     {"rows": max(total - 1, 0), "columns": header})


def _parse_qgs(xml_bytes: bytes, name: str) -> Extracted:
    root = ET.fromstring(xml_bytes)
    title = (root.findtext("title") or "").strip() or name
    crs = (root.findtext(".//projectCrs/spatialrefsys/authid")
           or root.findtext(".//mapcanvas/destinationsrs/spatialrefsys/authid") or "")
    layers = []
    for ml in root.iter("maplayer"):
        layers.append({
            "name": (ml.findtext("layername") or "").strip(),
            "provider": (ml.findtext("provider") or "").strip(),
            "source": (ml.findtext("datasource") or "").strip(),
            "geometry": ml.get("geometry", ""),
        })
    lines = [f"# QGIS project: {title}", "",
             f"File: {name}", f"Project CRS: {crs or 'not recorded'}",
             f"Layers: {len(layers)}", ""]
    if layers:
        lines += ["## Layers", ""]
        for L in layers:
            bits = [f"**{L['name'] or '(unnamed)'}**"]
            if L["provider"]:
                bits.append(f"provider `{L['provider']}`")
            if L["geometry"]:
                bits.append(L["geometry"])
            lines.append("- " + ", ".join(bits))
            if L["source"]:
                # the data source is the whole point: it is what breaks when a
                # server is renamed or a drive letter changes
                lines.append(f"  - source: `{L['source']}`")
    return Extracted("\n".join(lines), title, "qgis-project",
                     {"crs": crs, "layer_count": len(layers),
                      "layers": [L["name"] for L in layers if L["name"]],
                      "sources": [L["source"] for L in layers if L["source"]][:200]})


def extract_qgis(path: Path) -> Extracted:
    if path.suffix.lower() == ".qgz":
        # a .qgz is a zip holding the .qgs XML plus an optional .qgd sqlite sidecar
        with zipfile.ZipFile(path) as z:
            inner = next((n for n in z.namelist() if n.lower().endswith(".qgs")), None)
            if not inner:
                raise ValueError("no .qgs inside .qgz")
            return _parse_qgs(z.read(inner), path.name)
    return _parse_qgs(path.read_bytes(), path.name)


def read_guano(path: Path) -> dict | None:
    """Pull the GUANO block out of a WAV without reading the audio.

    Walks RIFF sub-chunks looking for `guan`. Fields are UTF-8 `Namespace|Key: value`
    lines per the GUANO 1.0 spec, and the block may sit anywhere in the container -
    recorders often append it after `data`.
    """
    with path.open("rb") as fh:
        if fh.read(4) != b"RIFF":
            return None
        fh.read(4)                                   # riff size, unused
        if fh.read(4) != b"WAVE":
            return None
        while True:
            head = fh.read(8)
            if len(head) < 8:
                return None
            cid, size = struct.unpack("<4sI", head)
            if cid == b"guan":
                raw = fh.read(size)
                break
            fh.seek(size + (size % 2), os.SEEK_CUR)  # sub-chunks pad to even bytes

    fields: dict[str, str] = {}
    for line in raw.decode("utf-8", errors="replace").split("\n"):
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key, value = key.strip().strip("\x00"), value.strip().strip("\x00")
        if key:
            fields[key] = value
    return fields or None


def extract_wav(path: Path) -> Extracted | None:
    fields = read_guano(path)
    if not fields:
        return None
    ordered = sorted(fields.items(), key=lambda kv: (kv[0] != "GUANO|Version", kv[0]))
    lines = [f"# Recording: {path.name}", "",
             "GUANO metadata embedded in the WAV. Audio not read.", "",
             "| field | value |", "|---|---|"]
    lines += [f"| `{k}` | {v[:200]} |" for k, v in ordered]
    species = [v for k, v in fields.items() if re.search(r"species|auto.?id|manual.?id", k, re.I)]
    if species:
        lines += ["", f"Identifications recorded: {', '.join(species)}"]
    return Extracted("\n".join(lines), path.stem, "recording", {"guano": fields})


def extract_office(path: Path) -> Extracted:
    """docx/xlsx/pptx are zipped XML, so no third-party dependency is needed."""
    ext = path.suffix.lower()
    texts: list[str] = []
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        if ext == ".docx":
            targets = [n for n in names if n == "word/document.xml"
                       or n.startswith("word/header") or n.startswith("word/footer")]
        elif ext == ".pptx":
            targets = sorted(n for n in names if re.match(r"ppt/slides/slide\d+\.xml$", n))
        else:
            shared = []
            if "xl/sharedStrings.xml" in names:
                shared = re.findall(r"<t[^>]*>(.*?)</t>",
                                    z.read("xl/sharedStrings.xml").decode("utf-8", "replace"),
                                    re.S)
            targets = [n for n in names if re.match(r"xl/worksheets/sheet\d+\.xml$", n)]
            if shared:
                texts.append(" ".join(shared))
        for n in targets:
            body = z.read(n).decode("utf-8", errors="replace")
            body = re.sub(r"</w:p>|</a:p>|</row>", "\n", body)
            texts.append(re.sub(r"<[^>]+>", " ", body))
    text = re.sub(r"[ \t]{2,}", " ", "\n".join(texts))
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return Extracted(f"# {path.stem}\n\n{text}", path.stem, "document", {})


def extract_pdf(path: Path) -> Extracted | None:
    try:
        from pypdf import PdfReader
    except ImportError:
        return None
    reader = PdfReader(str(path))
    pages = [(p.extract_text() or "").strip() for p in reader.pages[:80]]
    text = "\n\n".join(f"## Page {i + 1}\n\n{t}" for i, t in enumerate(pages) if t)
    if not text.strip():
        return None                                  # scanned PDF, would need OCR
    title = (reader.metadata.title if reader.metadata else None) or path.stem
    return Extracted(f"# {title}\n\n{text}", str(title), "document",
                     {"pages": len(reader.pages)})


# --------------------------------------------------------------------- walking
def should_skip(path: Path) -> bool:
    return any(p.match(path.name) for p in EXCLUDE_FILE_PATTERNS)


def iter_files(root: Path, max_bytes: int) -> tuple[list[Path], dict]:
    found, stats = [], {"skipped_junk": 0, "too_big": 0, "unreadable": 0}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS
                       and not d.startswith("._")]
        for name in filenames:
            p = Path(dirpath) / name
            if should_skip(p):
                stats["skipped_junk"] += 1
                continue
            ext = p.suffix.lower()
            if ext not in (TEXT_EXT | TABLE_EXT | QGIS_EXT | OFFICE_EXT
                           | AUDIO_EXT | PDF_EXT):
                continue
            try:
                if p.stat().st_size > max_bytes and ext not in AUDIO_EXT:
                    stats["too_big"] += 1        # wav is fine, we only read the header
                    continue
            except OSError:
                stats["unreadable"] += 1
                continue
            found.append(p)
    return found, stats


def extract(path: Path, max_bytes: int) -> Extracted | None:
    ext = path.suffix.lower()
    if ext in QGIS_EXT:
        return extract_qgis(path)
    if ext in AUDIO_EXT:
        return extract_wav(path)
    if ext in TABLE_EXT:
        return extract_table(path)
    if ext in OFFICE_EXT:
        return extract_office(path)
    if ext in PDF_EXT:
        return extract_pdf(path)
    if ext in TEXT_EXT:
        text = _read_text(path, max_bytes)
        if not text.strip():
            return None
        if ext == ".json":
            try:                                     # pretty-print so chunking works
                text = json.dumps(json.loads(text), indent=2)[:max_bytes]
            except (ValueError, TypeError):
                pass
        return Extracted(f"# {path.name}\n\n{text}", path.stem, "text", {})
    return None


def build_document(path: Path, root: Path, ex: Extracted) -> Document:
    st = path.stat()
    rel = path.relative_to(root).as_posix()
    tags = ["files", ex.kind] + classify_sensitivity(ex.text)
    # The path goes in the text so retrieval can match on it, but the mtime must NOT:
    # it would make the content hash change every time a file is merely touched, and a
    # share restore or bulk copy moves every mtime at once. That would re-embed the
    # whole corpus for no content change. The timestamp lives in created_at and meta.
    header = f"Path: {rel}\n\n"
    return Document(
        source="files",
        id=f"files:{rel}",                       # stable across re-runs; content hash
        origin=rel,                              # decides whether it is re-chunked
        url=path.resolve().as_uri(),
        title=ex.title or path.stem,
        text=header + ex.text,
        created_at=int(st.st_mtime),
        tags=tags,
        meta={"kind": ex.kind, "bytes": st.st_size, "relpath": rel,
              "modified": time.strftime("%Y-%m-%dT%H:%M:%S",
                                        time.localtime(st.st_mtime)), **ex.meta},
    )


def run(store: Store, roots: list[str] | None = None, max_mb: float = DEFAULT_MAX_MB,
        full: bool = False) -> dict:
    # comma, or the OS path separator (':' on Debian/macOS, ';' on Windows). Splitting
    # on ':' everywhere would cut "C:\\Survey" in half on a Windows laptop.
    sep = r"[,%s]" % re.escape(os.pathsep)
    roots = roots or [r.strip() for r in re.split(sep,
                      os.environ.get("BRAIN_FILE_ROOTS", "")) if r.strip()]
    if not roots:
        raise SystemExit("set BRAIN_FILE_ROOTS to one or more mounted paths")
    max_bytes = int(max_mb * 1_000_000)
    summary = {"roots": {}, "written": 0, "unchanged": 0, "failed": 0,
               "sensitive": 0, "skipped": {}}

    for raw_root in roots:
        root = Path(raw_root).expanduser()
        if not root.is_dir():
            summary["roots"][str(root)] = "MISSING - is the share mounted?"
            continue

        seen_cursor = f"files:mtime:{root}"
        prior = json.loads(store.get_cursor(seen_cursor) or "{}") if not full else {}
        files, stats = iter_files(root, max_bytes)
        current: dict[str, str] = {}
        # written   = content actually changed in the store
        # reparsed  = mtime moved so we re-extracted, but the text was identical
        # untouched = mtime unchanged, never opened
        written = reparsed = untouched = failed = 0

        for path in files:
            rel = path.relative_to(root).as_posix()
            try:
                stat = path.stat()
            except OSError:
                failed += 1
                continue
            # mtime AND size, compared exactly. A second's tolerance would miss an
            # edit made in the same second as the previous scan, which is exactly what
            # happens when a script writes a file mid-ingest.
            fingerprint = f"{stat.st_mtime:.3f}:{stat.st_size}"
            current[rel] = fingerprint
            if not full and prior.get(rel) == fingerprint:
                untouched += 1          # not even opened
                continue
            try:
                ex = extract(path, max_bytes)
            except (OSError, ValueError, ET.ParseError, zipfile.BadZipFile,
                    struct.error, UnicodeError) as exc:
                summary.setdefault("errors", []).append(f"{rel}: {exc}")
                failed += 1
                continue
            if ex is None or not ex.text.strip():
                continue
            doc = build_document(path, root, ex)
            if "sensitive" in doc.tags:
                summary["sensitive"] += 1
            _, changed = store.upsert(doc)
            if changed:
                written += 1
            else:
                reparsed += 1

        store.set_cursor(seen_cursor, json.dumps(current))
        summary["roots"][str(root)] = {"files_considered": len(files),
                                       "written": written, "reparsed": reparsed,
                                       "untouched": untouched, "failed": failed,
                                       **stats}
        summary["written"] += written
        summary["unchanged"] += reparsed + untouched
        summary["failed"] += failed
    return summary
