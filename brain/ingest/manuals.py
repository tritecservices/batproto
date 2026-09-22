"""Manuals and professional guidelines: QGIS docs, CIEEM, BCT, your own SOPs.

Listed in a YAML file (see manuals.example.yaml), because a manual needs facts that
can't be guessed from the file: publisher, edition, licence, and whether it may ever
be shown outside the company.

    python -m brain.cli ingest manuals --config manuals.yaml [--download]

What gets stored:

* PDF    one document per page, titled "<title> (<edition>), p. <label>" and linked
         to "<url>#page=<n>". The page is the citation unit, so an agent can say
         "CIEEM EcIA v1.3, p. 23" and a person can check it in seconds.
* rst    a folder of reStructuredText (the QGIS docs are written in it), one
         document per file, linked to the published HTML page via `base_url`.
* text   a folder of .md / .txt / .html files, one document per file.

Licensing is recorded, not assumed:

* `public: false` (the default) tags every page `internal-only`. The public website
  demo never serves those, and agents are told to cite and paraphrase them rather
  than reproduce them. Free-to-download is not the same as free-to-republish:
  CIEEM and BCT guidance is copyright.
* `public: true` is for openly licensed material such as the QGIS manual (CC BY-SA).
  Attribution and share-alike still apply, so the licence travels with every page.

Re-ingesting a new edition replaces the old pages rather than leaving them mixed in:
anything under the manual's id that the new edition didn't produce is deleted.

Manuals are published guidance, not site data, so they are NOT run through the
survey sensitivity classifier. A guideline that mentions badger setts is not a
record of one.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

import yaml

from ..store import Document, Store

MIN_PAGE_CHARS = 40          # cover pages, blank versos, full-page figures


@dataclass
class Manual:
    id: str
    title: str
    path: str
    publisher: str = ""
    edition: str = ""
    licence: str = "copyright - internal reference only"
    public: bool = False
    url: str | None = None
    kind: str | None = None          # pdf | rst | text; inferred from path if absent
    base_url: str | None = None      # rst/text: published HTML root
    tags: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        return f"{self.title} ({self.edition})" if self.edition else self.title

    def resolved_kind(self) -> str:
        if self.kind:
            return self.kind
        p = Path(self.path)
        if p.suffix.lower() == ".pdf":
            return "pdf"
        if p.is_dir() and any(p.rglob("*.rst")):
            return "rst"
        return "text"

    def base_tags(self) -> list[str]:
        tags = ["manual", *(t for t in [self.publisher.lower()] if t), *self.tags]
        if not self.public:
            tags.append("internal-only")
        return tags

    def base_meta(self) -> dict:
        return {"manual_id": self.id, "publisher": self.publisher, "edition": self.edition,
                "licence": self.licence, "public": self.public}


def load_config(path: str | Path) -> list[Manual]:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    base = Path(path).resolve().parent
    manuals = []
    for raw in data.get("manuals", []):
        missing = {"id", "title", "path"} - raw.keys()
        if missing:
            raise ValueError(f"manual entry missing {sorted(missing)}: {raw}")
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", raw["id"]):
            raise ValueError(f"manual id must be lowercase-with-dashes: {raw['id']!r}")
        m = Manual(**{k: v for k, v in raw.items() if k in Manual.__dataclass_fields__})
        if not Path(m.path).is_absolute():            # relative to the config file
            m.path = str(base / m.path)
        manuals.append(m)
    ids = [m.id for m in manuals]
    if len(ids) != len(set(ids)):
        raise ValueError("manual ids must be unique")
    return manuals


# ----------------------------------------------------------------------- PDF
def _clean_pdf_text(text: str) -> str:
    text = text.replace("­", "")                        # soft hyphens
    text = re.sub(r"(\w)-\n(\w)", r"\1\2", text)              # re-join hyphenated words
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def pdf_documents(m: Manual) -> Iterator[Document]:
    from pypdf import PdfReader                               # pip install pypdf

    reader = PdfReader(m.path)
    try:
        labels = list(reader.page_labels)
    except Exception:                                         # malformed label tree
        labels = []
    for i, page in enumerate(reader.pages):
        text = _clean_pdf_text(page.extract_text() or "")
        if len(text) < MIN_PAGE_CHARS:
            continue
        label = labels[i] if i < len(labels) and labels[i] else str(i + 1)
        yield Document(
            id=f"manual:{m.id}:p{i + 1:04d}",
            source="manuals",
            origin=m.publisher or m.id,
            url=f"{m.url}#page={i + 1}" if m.url else None,
            title=f"{m.label}, p. {label}",
            author=m.publisher or None,
            tags=m.base_tags(),
            meta={**m.base_meta(), "page_index": i + 1, "page_label": label},
            text=text,
        )


# ----------------------------------------------------------------------- rst
_RST_UNDERLINE = re.compile(r"^([=\-~^\"'`#*+<>])\1{2,}\s*$")
_RST_ROLE = re.compile(r":[a-z:]+:`([^`<]*?)(?:\s*<[^>]*>)?`")     # :ref:`Text <target>`
_RST_SUBST = re.compile(r"\|[\w\- ]+\|")                             # |icon| substitutions


def rst_to_text(src: str) -> tuple[str, str]:
    """(title, readable text). Keeps prose, headings, lists and code; drops
    directives' option lines, substitution images and underline rows."""
    lines, title = [], ""
    raw = src.splitlines()
    skip_block = False
    for i, line in enumerate(raw):
        if _RST_UNDERLINE.match(line):
            continue
        nxt = raw[i + 1] if i + 1 < len(raw) else ""
        if not title and line.strip() and _RST_UNDERLINE.match(nxt):
            title = line.strip()
        stripped = line.strip()
        if stripped.startswith(".. "):
            # keep the text of note/warning/tip admonitions, skip images, indexes etc.
            skip_block = not re.match(r"\.\. (note|warning|tip|important|caution)::", stripped)
            if not skip_block:
                lines.append(stripped.split("::", 1)[1].strip())
            continue
        if skip_block:
            if stripped and not line.startswith(" "):
                skip_block = False
            elif stripped.startswith(":"):                   # directive options
                continue
            else:
                continue
        line = _RST_ROLE.sub(r"\1", line)
        line = _RST_SUBST.sub("", line).replace("**", "").replace("``", "`")
        lines.append(line.rstrip())
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return title, text


def rst_documents(m: Manual) -> Iterator[Document]:
    root = Path(m.path)
    for f in sorted(root.rglob("*.rst")):
        if any(part.startswith(("_", ".")) for part in f.relative_to(root).parts):
            continue                                      # _static, _templates, includes
        title, text = rst_to_text(f.read_text(encoding="utf-8", errors="replace"))
        if len(text) < MIN_PAGE_CHARS:
            continue
        rel = f.relative_to(root).with_suffix(".html").as_posix()
        yield Document(
            id=f"manual:{m.id}:{f.relative_to(root).with_suffix('').as_posix()}",
            source="manuals",
            origin=m.publisher or m.id,
            url=(m.base_url.rstrip("/") + "/" + rel) if m.base_url else None,
            title=f"{m.label}: {title or f.stem}",
            author=m.publisher or None,
            tags=m.base_tags(),
            meta={**m.base_meta(), "file": f.relative_to(root).as_posix()},
            text=text,
        )


# ---------------------------------------------------------------------- text
def text_documents(m: Manual) -> Iterator[Document]:
    root = Path(m.path)
    files = [root] if root.is_file() else sorted(
        p for p in root.rglob("*") if p.suffix.lower() in {".md", ".txt", ".html", ".htm"})
    for f in files:
        text = f.read_text(encoding="utf-8", errors="replace")
        if f.suffix.lower() in {".html", ".htm"}:
            text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", text, flags=re.S | re.I)
            text = re.sub(r"<[^>]+>", " ", text)
            text = re.sub(r"[ \t]+", " ", text)
        if len(text.strip()) < MIN_PAGE_CHARS:
            continue
        rel = f.name if root.is_file() else f.relative_to(root).as_posix()
        yield Document(
            id=f"manual:{m.id}:{rel}",
            source="manuals",
            origin=m.publisher or m.id,
            url=(m.base_url.rstrip("/") + "/" + rel) if m.base_url else m.url,
            title=f"{m.label}: {f.stem}",
            author=m.publisher or None,
            tags=m.base_tags(),
            meta={**m.base_meta(), "file": rel},
            text=text.strip(),
        )


# ----------------------------------------------------------------------- run
def download(m: Manual) -> str:
    """Fetch a PDF that is listed with a url but not yet on disk."""
    import requests

    dest = Path(m.path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    r = requests.get(m.url, timeout=120,
                     headers={"User-Agent": "ecomsp-brain/1.0 (internal reference library)"})
    r.raise_for_status()
    if not r.content.startswith(b"%PDF"):
        raise ValueError(f"{m.url} did not return a PDF")
    tmp = dest.with_suffix(".part")
    tmp.write_bytes(r.content)
    tmp.replace(dest)
    return str(dest)


def run(store: Store, config: str, only: list[str] | None = None,
        fetch: bool = False) -> dict:
    manuals = load_config(config)
    if only:
        manuals = [m for m in manuals if m.id in only]
    out: dict = {}
    for m in manuals:
        try:
            if not Path(m.path).exists():
                if fetch and m.url and m.url.lower().split("?")[0].endswith(".pdf"):
                    download(m)
                else:
                    out[m.id] = f"MISSING {m.path} (add it, or use --download for PDF urls)"
                    continue
            kind = m.resolved_kind()
            docs = list({"pdf": pdf_documents, "rst": rst_documents,
                         "text": text_documents}[kind](m))
            if not docs:
                out[m.id] = "no extractable text (scanned PDF? needs OCR)"
                continue
            stats = store.bulk_upsert(docs)
            stats["removed_stale"] = store.delete_prefix(f"manual:{m.id}:",
                                                         keep={d.key() for d in docs})
            stats["kind"] = kind
            stats["public"] = m.public
            out[m.id] = stats
        except Exception as exc:                  # one bad manual must not stop the rest
            out[m.id] = f"FAILED {exc.__class__.__name__}: {exc}"
    return out
