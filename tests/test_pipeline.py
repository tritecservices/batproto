"""Offline tests: store, chunking, hybrid search, foundry spec parsing."""
from __future__ import annotations

import os
import time
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from brain.embed import HashEmbedder, embed_pending          # noqa: E402
from brain.foundry import AgentSpec, _load_spec_document      # noqa: E402
from brain.search import keyword_search, search              # noqa: E402
from brain.store import Document, Store, chunk_text          # noqa: E402

DOCS = [
    Document(id="d1", source="discord", origin="MSP/#gis-support",
             title="QGIS crashes opening a large GeoPackage",
             url="https://discord.com/channels/1/2", created_at=1_700_000_000,
             text="Sam: QGIS 3.34 crashes on opening a 4GB GeoPackage on Windows 11.\n\n"
                  "Ade: it is the spatial index. Run VACUUM then rebuild the rtree, "
                  "and turn off the overview thumbnails in the browser panel."),
    Document(id="d2", source="stackexchange", title="PyQGIS processing.run writes no output",
             url="https://gis.stackexchange.com/q/1", created_at=1_600_000_000,
             text="# processing.run output missing\n\nUse 'OUTPUT': 'TEMPORARY_OUTPUT' or a "
                  "full path. The algorithm silently discards memory layers otherwise."),
    Document(id="d3", source="nightarc", title="NightArc schema: dbo.CallSequence",
             created_at=1_700_100_000,
             text="# NightArc.dbo.CallSequence\n\nPrimary key: SequenceId\n\n"
                  "| column | type |\n|---|---|\n| SequenceId | int |\n"
                  "| SiteId | int |\n| SpeciesAuto | nvarchar(50) |"),
]


def build() -> Store:
    path = os.path.join(tempfile.mkdtemp(), "t.db")
    st = Store(path)
    st.bulk_upsert(DOCS)
    embed_pending(st, HashEmbedder())
    return st


def test_chunking_preserves_short_docs():
    assert chunk_text("hello world") == ["hello world"]
    long = "\n\n".join(f"paragraph {i} " + "x" * 300 for i in range(20))
    parts = chunk_text(long, target=1200)
    assert len(parts) > 1 and all(len(p) < 2200 for p in parts)


def test_upsert_is_idempotent():
    st = build()
    _, changed = st.upsert(DOCS[0])
    assert changed is False
    n = st.db.execute("SELECT COUNT(*) c FROM documents").fetchone()["c"]
    assert n == 3


def test_reindex_on_change_leaves_no_orphans():
    st = build()
    st.upsert(Document(id="d1", source="discord", title="changed", text="totally new body"))
    chunks = st.db.execute("SELECT COUNT(*) c FROM chunks WHERE doc_id='d1'").fetchone()["c"]
    fts = st.db.execute("SELECT COUNT(*) c FROM chunks_fts").fetchone()["c"]
    total = st.db.execute("SELECT COUNT(*) c FROM chunks").fetchone()["c"]
    assert chunks == 1 and fts == total


def test_keyword_search_finds_error_text():
    st = build()
    hits = keyword_search(st, "GeoPackage crashes spatial index")
    assert hits and any(cid.startswith("d1") for cid, _ in hits)


def test_hybrid_search_ranks_and_dedupes():
    st = build()
    hits = search(st, "qgis crash large geopackage", limit=5, embedder=HashEmbedder())
    assert hits[0]["doc_id"] == "d1"
    assert len({h["doc_id"] for h in hits}) == len(hits)
    assert hits[0]["url"].startswith("https://discord.com/")


def test_source_filter():
    st = build()
    hits = search(st, "output path algorithm", source="stackexchange",
                  embedder=HashEmbedder())
    assert all(h["source"] == "stackexchange" for h in hits)


def test_fts_query_sanitiser_survives_punctuation():
    st = build()
    # a raw FTS5 query would throw on these
    assert isinstance(search(st, 'processing.run("qgis:buffer") -- failed?',
                             embedder=HashEmbedder()), list)


def test_sources_inventory():
    st = build()
    got = {s["source"]: s["n"] for s in st.sources()}
    assert got == {"discord": 1, "stackexchange": 1, "nightarc": 1}


def test_agent_specs_parse():
    root = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "agents")
    files = [f for f in os.listdir(root) if f.endswith((".yaml", ".yml"))]
    assert files
    for f in files:
        spec = AgentSpec.from_file(os.path.join(root, f))
        assert spec.name and spec.model and len(spec.instructions) > 200
        for t in spec.tools:
            assert t.get("type") in {"openapi", "mcp", "web_search",
                                     "code_interpreter", "file_search"}


def test_openapi_spec_resolution_requires_a_source():
    try:
        _load_spec_document({"name": "x"})
    except ValueError as e:
        assert "spec" in str(e)
    else:
        raise AssertionError("should have raised")


def test_foundry_payload_builds_with_real_sdk():
    """Skips silently if azure-ai-projects is not installed."""
    try:
        from azure.ai.projects.models import PromptAgentDefinition
    except ImportError:
        print("  (azure-ai-projects not installed, skipped)")
        return
    import json
    from brain.foundry import build_tools
    tools = build_tools([
        {"type": "openapi", "name": "brain", "description": "search",
         "spec": {"openapi": "3.0.3", "info": {"title": "t", "version": "1"}, "paths": {}},
         "auth": "connection", "connection_id": "brain-api-key"},
        {"type": "mcp", "server_label": "brain", "server_url": "https://x/mcp"},
        {"type": "web_search"},
        {"type": "code_interpreter"},
    ])
    payload = json.loads(json.dumps(
        PromptAgentDefinition(model="gpt-5-mini", instructions="hi", tools=tools).as_dict()))
    assert [t["type"] for t in payload["tools"]] == [
        "openapi", "mcp", "web_search", "code_interpreter"]
    assert payload["tools"][0]["openapi"]["auth"]["type"] == "project_connection"


def test_api_openapi_is_foundry_shaped():
    from fastapi.testclient import TestClient
    from brain import api
    api._store = build()                       # inject the temp corpus
    client = TestClient(api.app)
    spec = client.get("/openapi-for-foundry").json()
    assert spec["openapi"].startswith("3.0")
    # must be the exact string used as the Key of the Foundry custom-keys connection
    assert spec["components"]["securitySchemes"]["apiKeyHeader"]["name"] == "x-api-key"
    assert spec["security"] == [{"apiKeyHeader": []}]
    ops = [o.get("operationId") for p in spec["paths"].values() for o in p.values()]
    assert "searchBrain" in ops and "openapiForFoundry" not in ops
    # no header param duplicating the connection-supplied key
    for path in spec["paths"].values():
        for op in path.values():
            for p in op.get("parameters", []):
                assert p.get("name", "").lower() != "x-api-key"


def test_api_search_requires_key_when_configured():
    from fastapi.testclient import TestClient
    from brain import api
    api._store = build()
    os.environ["BRAIN_API_KEY"] = "secret"
    try:
        client = TestClient(api.app)
        assert client.get("/search", params={"q": "qgis"}).status_code == 401
        ok = client.get("/search", params={"q": "geopackage crash"},
                        headers={"X-API-Key": "secret"})
        assert ok.status_code == 200 and ok.json()["count"] >= 1
    finally:
        os.environ.pop("BRAIN_API_KEY", None)


def test_nightarc_write_guard():
    from brain.ingest.nightarc_sql import WRITE_GUARD
    assert WRITE_GUARD.search("DELETE FROM dbo.CallSequence")
    assert WRITE_GUARD.search("drop table x")
    assert not WRITE_GUARD.search("SELECT TOP 3 SequenceId FROM dbo.CallSequence")


# ---------------------------------------------------------------- file share
def _make_share(tmp):
    """Build byte-accurate sample files: a real zip-based .qgz, a real RIFF wav with
    a GUANO chunk after `data`, a real OOXML .docx. No mocks."""
    import struct, zipfile
    root = Path(tmp)
    (root / "gis").mkdir(parents=True)
    (root / "acoustics").mkdir(parents=True)

    qgs = (b"<?xml version='1.0'?><qgis version='3.40'>"
           b"<title>Bexhill North</title>"
           b"<projectCrs><spatialrefsys><authid>EPSG:27700</authid></spatialrefsys>"
           b"</projectCrs><projectlayers>"
           b"<maplayer geometry='Point'><layername>Detectors</layername>"
           b"<provider>ogr</provider>"
           b"<datasource>\\\\OLDSERVER\\Survey$\\d.gpkg</datasource></maplayer>"
           b"</projectlayers></qgis>")
    with zipfile.ZipFile(root / "gis" / "p.qgz", "w") as z:
        z.writestr("p.qgs", qgs)
        z.writestr("p.qgd", b"")

    guano = (b"GUANO|Version: 1.0\nMake: Wildlife Acoustics\nModel: SM4BAT-FS\n"
             b"Species Manual ID: RHIHIP\nNote: maternity roost nearby\n")
    if len(guano) % 2:
        guano += b" "
    def ck(cid, payload):
        return cid + struct.pack("<I", len(payload)) + payload + (
            b"\x00" if len(payload) % 2 else b"")
    body = (b"WAVE" + ck(b"fmt ", struct.pack("<HHIIHH", 1, 1, 256000, 512000, 2, 16))
            + ck(b"data", b"\x00\x01" * 64) + ck(b"guan", guano))
    (root / "acoustics" / "rec.wav").write_bytes(
        b"RIFF" + struct.pack("<I", len(body)) + body)

    (root / "acoustics" / "id.csv").write_text(
        "FOLDER,IN FILE,AUTO ID*,MANUAL ID\n"
        + "".join(f"BEX01,f{i}.wav,PIPPIP,PIPPIP\n" for i in range(50)))

    docx = root / "method.docx"
    with zipfile.ZipFile(docx, "w") as z:
        z.writestr("word/document.xml",
                   "<w:document xmlns:w='x'><w:body><w:p><w:r><w:t>"
                   "Detectors at TQ 7390 0820 for five nights."
                   "</w:t></w:r></w:p></w:body></w:document>")

    (root / "notes.md").write_text("# Restore\n\nODBC Driver 18, TrustServerCertificate.\n")
    (root / "._notes.md").write_bytes(b"appledouble")
    (root / ".DS_Store").write_bytes(b"junk")
    (root / "~$method.docx").write_bytes(b"lock")
    return root


def test_files_guano_wav_reader():
    from brain.ingest.files import read_guano
    with tempfile.TemporaryDirectory() as tmp:
        root = _make_share(tmp)
        fields = read_guano(root / "acoustics" / "rec.wav")
        assert fields["GUANO|Version"] == "1.0"
        assert fields["Make"] == "Wildlife Acoustics"
        assert fields["Species Manual ID"] == "RHIHIP"
        # a plain wav with no guan chunk must return None, not raise
        plain = root / "acoustics" / "plain.wav"
        plain.write_bytes(b"RIFF" + (24).to_bytes(4, "little") + b"WAVE"
                          + b"fmt " + (16).to_bytes(4, "little") + b"\x00" * 16)
        assert read_guano(plain) is None


def test_files_qgz_layer_extraction():
    from brain.ingest.files import extract_qgis
    with tempfile.TemporaryDirectory() as tmp:
        root = _make_share(tmp)
        ex = extract_qgis(root / "gis" / "p.qgz")
        assert ex.title == "Bexhill North"
        assert ex.meta["crs"] == "EPSG:27700"
        assert "Detectors" in ex.meta["layers"]
        # the data source must survive: this is what answers "what broke when we
        # decommissioned that server"
        assert "OLDSERVER" in ex.text


def test_files_csv_is_summarised_not_dumped():
    from brain.ingest.files import extract_table
    with tempfile.TemporaryDirectory() as tmp:
        root = _make_share(tmp)
        ex = extract_table(root / "acoustics" / "id.csv")
        assert ex.meta["rows"] == 50
        assert "MANUAL ID" in ex.meta["columns"]
        assert ex.text.count("PIPPIP") < 50      # summarised, not 50 rows of it


def test_files_skips_junk_and_tags_sensitivity():
    from brain.ingest import files as F
    with tempfile.TemporaryDirectory() as tmp:
        root = _make_share(tmp)
        found, stats = F.iter_files(root, 25_000_000)
        names = {p.name for p in found}
        assert names == {"p.qgz", "rec.wav", "id.csv", "method.docx", "notes.md"}
        assert stats["skipped_junk"] == 3         # ._, .DS_Store, ~$
        assert "sensitive" in F.classify_sensitivity("Species Manual ID: RHIHIP")
        assert "sensitive" in F.classify_sensitivity("a maternity roost was found")
        assert "has-grid-ref" in F.classify_sensitivity("recorded at TQ 7390 0820")
        assert F.classify_sensitivity("just a normal sentence about qgis") == []


def test_files_ingest_is_incremental_and_survives_touch():
    from brain.ingest import files as F
    with tempfile.TemporaryDirectory() as tmp:
        root = _make_share(tmp)
        store = Store(":memory:")
        first = F.run(store, roots=[str(root)])["roots"][str(root)]
        assert first["written"] == 5 and first["untouched"] == 0

        second = F.run(store, roots=[str(root)])["roots"][str(root)]
        assert second["written"] == 0 and second["untouched"] == 5

        # touching without editing must not rewrite: otherwise a share restore or
        # rsync -a re-embeds the entire corpus for nothing
        os.utime(root / "notes.md", (time.time() + 10, time.time() + 10))
        third = F.run(store, roots=[str(root)])["roots"][str(root)]
        assert third["written"] == 0 and third["reparsed"] == 1

        (root / "notes.md").write_text("# Restore\n\nNow with TCP/IP enabled.\n")
        fourth = F.run(store, roots=[str(root)])["roots"][str(root)]
        assert fourth["written"] == 1

        # and the content is findable
        hits = search(store, "OLDSERVER gpkg layer", limit=5)
        assert any("Bexhill" in (h.get("title") or "") for h in hits)
        store.close()


def test_files_missing_root_is_reported_not_raised():
    from brain.ingest import files as F
    store = Store(":memory:")
    out = F.run(store, roots=["/definitely/not/mounted"])
    assert "MISSING" in out["roots"]["/definitely/not/mounted"]
    assert out["written"] == 0
    store.close()


# ---------------------------------------------------------------- review fixes
def test_search_multi_source_and_tag_exclusion():
    st = build()
    st.upsert(Document(id="sens", source="stackexchange", title="GeoPackage roost layer",
                       tags=["sensitive"], text="GeoPackage crash near the maternity roost"))
    hits = search(st, "geopackage crash", source={"stackexchange", "discord"},
                  embedder=HashEmbedder())
    assert {h["source"] for h in hits} <= {"stackexchange", "discord"}
    assert any(h["doc_id"] == "sens" for h in hits)
    hits = search(st, "geopackage crash", source={"stackexchange", "discord"},
                  embedder=HashEmbedder(), exclude_tags={"sensitive"})
    assert not any(h["doc_id"] == "sens" for h in hits)


def test_search_tolerates_discourse_tag_objects():
    st = build()
    st.upsert(Document(id="dc", source="discourse", title="Rtree index rebuild",
                       tags=[{"id": 1, "name": "qgis", "slug": "qgis"}],
                       text="rebuild the rtree spatial index"))
    hits = search(st, "rtree spatial index", embedder=HashEmbedder(),
                  exclude_tags={"sensitive"})
    dc = next(h for h in hits if h["doc_id"] == "dc")
    assert dc["tags"] == ["qgis"]


def test_sensitivity_shared_classifier():
    from brain.sensitivity import classify_sensitivity
    assert "sensitive" in classify_sensitivity("Lesser horseshoe bat maternity roost")
    assert "has-grid-ref" in classify_sensitivity("detector at TQ 12345 67890")
    assert classify_sensitivity("QGIS crashed opening the project") == []


def test_file_roots_split_keeps_windows_drive_letters():
    import re as _re
    sep = r"[,%s]" % _re.escape(";")          # what os.pathsep is on Windows
    assert _re.split(sep, r"C:\\Survey,D:\\Bats") == [r"C:\\Survey", r"D:\\Bats"]


def test_api_fails_closed_without_key():
    from fastapi.testclient import TestClient
    from brain import api
    api._store = build()
    for k in ("BRAIN_API_KEY", "BRAIN_ALLOW_NO_KEY"):
        os.environ.pop(k, None)
    client = TestClient(api.app)
    assert client.get("/search", params={"q": "qgis"}).status_code == 503
    os.environ["BRAIN_API_KEY"] = "change-me"
    try:
        assert client.get("/search", params={"q": "qgis"},
                          headers={"X-API-Key": "change-me"}).status_code == 503
    finally:
        os.environ.pop("BRAIN_API_KEY", None)


def test_public_search_only_serves_public_nonsensitive_sources():
    from fastapi.testclient import TestClient
    from brain import api
    st = build()
    st.upsert(Document(id="se-sens", source="stackexchange", title="GeoPackage roost",
                       tags=["sensitive"], text="GeoPackage crash at the roost"))
    api._store = st
    client = TestClient(api.app)
    assert client.get("/public/search", params={"q": "geopackage"}).status_code == 404
    os.environ["BRAIN_PUBLIC_DEMO"] = "1"
    try:
        r = client.get("/public/search", params={"q": "geopackage crash"})
        assert r.status_code == 200
        hits = r.json()["hits"]
        assert all(h["source"] in api.PUBLIC_SOURCES for h in hits)   # d1 is discord
        assert not any(h["title"] == "GeoPackage roost" for h in hits)
    finally:
        os.environ.pop("BRAIN_PUBLIC_DEMO", None)


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(list(globals().items())):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception as exc:
                fails += 1
                print(f"FAIL {name}: {exc.__class__.__name__}: {exc}")
    print("\n", "all passed" if not fails else f"{fails} failed")
    sys.exit(1 if fails else 0)
