"""Manuals ingestion: PDF pages, QGIS-style rst, licensing tags, edition replacement.

Run: python tests/test_manuals.py    (PDF tests are skipped if pypdf isn't installed)
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from brain.embed import HashEmbedder, embed_pending          # noqa: E402
from brain.ingest import manuals as M                        # noqa: E402
from brain.search import search                              # noqa: E402
from brain.sensitivity import RESTRICTED_TAGS                # noqa: E402
from brain.store import Store                                # noqa: E402

try:
    import pypdf  # noqa: F401
    HAVE_PYPDF = True
except ImportError:
    HAVE_PYPDF = False


def tiny_pdf(pages: list[str]) -> bytes:
    """A valid PDF with one line of Helvetica text per page, built by hand so the
    test needs no PDF-writing library. Empty strings make near-blank pages."""
    objs = ["<< /Type /Catalog /Pages 2 0 R >>", None,
            "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    kids = []
    for text in pages:
        safe = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        stream = f"BT /F1 11 Tf 50 700 Td ({safe}) Tj ET".encode()
        objs.append(f"<< /Length {len(stream)} >>\nstream\n{stream.decode()}\nendstream")
        content_id = len(objs)
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                    f"/Resources << /Font << /F1 3 0 R >> >> /Contents {content_id} 0 R >>")
        kids.append(f"{len(objs)} 0 R")
    objs[1] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(kids)} >>"
    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{body}\nendobj\n".encode()
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += "".join(f"{o:010d} 00000 n \n" for o in offsets).encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


RST = """:orphan:

.. _label_vector_props:

****************************
Vector Properties Dialog
****************************

.. only:: html

   .. contents::
      :local:

The |vectorProperties| dialog opens from :menuselection:`Layer --> Properties`.
See :ref:`the symbology section <vector_style_menu>` for **styling**.

.. note:: Changes apply to the current project only.

Use ``Save Style`` to reuse a style in other projects.
"""


class Manuals(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.store = Store(str(self.tmp / "t.db"))
        docs = self.tmp / "manuals" / "qgis" / "user_manual"
        (docs / "working_with_vector").mkdir(parents=True)
        (docs / "_static").mkdir()
        (docs / "working_with_vector" / "vector_properties.rst").write_text(RST, encoding="utf-8")
        (docs / "_static" / "ignored.rst").write_text("Title\n=====\n\n" + "x " * 50,
                                                      encoding="utf-8")
        (self.tmp / "manuals" / "guide.pdf").write_bytes(tiny_pdf([
            "Emergence surveys should start fifteen minutes before sunset.",
            "",
            "Report the survey limitations, including weather and equipment faults."]))
        self.cfg = self.tmp / "manuals.yaml"
        self.cfg.write_text(f"""manuals:
  - id: test-guide
    title: Test Survey Guidelines
    publisher: TestBody
    edition: 2nd edition
    url: https://example.org/guide.pdf
    path: manuals/guide.pdf
  - id: qgis-user-manual
    title: QGIS User Manual
    publisher: QGIS Project
    edition: "3.40 LTR"
    licence: CC BY-SA 3.0
    public: true
    kind: rst
    path: manuals/qgis/user_manual
    base_url: https://docs.qgis.org/3.40/en/docs/user_manual/
""", encoding="utf-8")

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_config_paths_are_relative_to_the_config_file(self):
        ms = M.load_config(self.cfg)
        self.assertEqual(Path(ms[0].path), self.tmp / "manuals" / "guide.pdf")
        self.assertFalse(ms[0].public)                     # default is internal-only

    def test_config_rejects_bad_ids(self):
        bad = self.tmp / "bad.yaml"
        bad.write_text("manuals:\n  - {id: 'Bad Id', title: x, path: y}\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            M.load_config(bad)

    def test_rst_to_text(self):
        title, text = M.rst_to_text(RST)
        self.assertEqual(title, "Vector Properties Dialog")
        self.assertIn("Layer --> Properties", text)
        self.assertIn("the symbology section", text)       # role text kept, target dropped
        self.assertIn("Changes apply to the current project only.", text)
        self.assertNotIn("|vectorProperties|", text)
        self.assertNotIn(":local:", text)
        self.assertNotIn("****", text)

    def test_rst_manual_is_public_with_published_urls(self):
        out = M.run(self.store, str(self.cfg), only=["qgis-user-manual"])
        self.assertEqual(out["qgis-user-manual"]["written"], 1)   # _static skipped
        d = self.store.get("manual:qgis-user-manual:working_with_vector/vector_properties")
        self.assertEqual(d["url"], "https://docs.qgis.org/3.40/en/docs/user_manual/"
                                   "working_with_vector/vector_properties.html")
        self.assertNotIn("internal-only", d["tags"])
        self.assertEqual(d["meta"]["licence"], "CC BY-SA 3.0")

    def test_missing_file_is_reported_not_raised(self):
        (self.tmp / "manuals" / "guide.pdf").unlink()
        out = M.run(self.store, str(self.cfg), only=["test-guide"])
        self.assertTrue(out["test-guide"].startswith("MISSING"))

    @unittest.skipUnless(HAVE_PYPDF, "pypdf not installed")
    def test_pdf_one_document_per_page_with_page_links(self):
        out = M.run(self.store, str(self.cfg), only=["test-guide"])
        self.assertEqual(out["test-guide"]["written"], 2)          # blank page 2 skipped
        p3 = self.store.get("manual:test-guide:p0003")
        self.assertEqual(p3["title"], "Test Survey Guidelines (2nd edition), p. 3")
        self.assertEqual(p3["url"], "https://example.org/guide.pdf#page=3")
        self.assertIn("internal-only", p3["tags"])
        self.assertIn("limitations", p3["text"])

    @unittest.skipUnless(HAVE_PYPDF, "pypdf not installed")
    def test_new_edition_replaces_old_pages(self):
        M.run(self.store, str(self.cfg), only=["test-guide"])
        (self.tmp / "manuals" / "guide.pdf").write_bytes(
            tiny_pdf(["Only one page in the new edition about sunset timing."]))
        out = M.run(self.store, str(self.cfg), only=["test-guide"])
        self.assertEqual(out["test-guide"]["removed_stale"], 1)    # old p3 gone
        self.assertIsNone(self.store.get("manual:test-guide:p0003"))
        self.assertIsNotNone(self.store.get("manual:test-guide:p0001"))

    @unittest.skipUnless(HAVE_PYPDF, "pypdf not installed")
    def test_public_filter_keeps_open_manual_drops_copyright(self):
        M.run(self.store, str(self.cfg))
        embed_pending(self.store, HashEmbedder())
        allh = search(self.store, "survey sunset properties dialog", source="manuals",
                      embedder=HashEmbedder(), limit=10)
        pub = search(self.store, "survey sunset properties dialog", source="manuals",
                     embedder=HashEmbedder(), limit=10, exclude_tags=RESTRICTED_TAGS)
        self.assertTrue(any("test-guide" in h["doc_id"] for h in allh))     # staff see it
        self.assertFalse(any("test-guide" in h["doc_id"] for h in pub))     # public don't
        self.assertTrue(any("qgis-user-manual" in h["doc_id"] for h in pub))


if __name__ == "__main__":
    unittest.main(verbosity=2)
