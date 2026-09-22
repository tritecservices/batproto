"""Shared sensitivity classifier.

Used by every ingester that can carry field-survey detail (files, Discord, NightArc),
not just the file share: a roost location pasted into a Discord thread is exactly as
harmful as one in a Word report.
"""
from __future__ import annotations

import re

# Deliberately conservative. A false positive costs you a tag; a false negative could
# put a protected-species location into an AI answer that ends up in a public document.
SENSITIVE_HINTS = re.compile(
    r"\b(rhinolophus|barbastella|myotis bechsteinii|bechstein|horseshoe bat|"
    r"great crested newt|triturus cristatus|badger|meles meles|dormouse|"
    r"muscardinus|otter|lutra|schedule 1|annex ii|licence number|licen[cs]ed site|"
    r"nest site|hibernacul|maternity roost|sett)\b", re.I)
GRID_REF = re.compile(r"\b[A-Z]{2}\s?\d{2,5}\s?\d{2,5}\b")
# Kaleidoscope/Anabat 6-letter codes. Case-sensitive on purpose: lowercase "barbar"
# in prose is not a species record, and these appear in CSV columns, not sentences.
SENSITIVE_CODES = re.compile(r"\b(RHIHIP|RHIFER|BARBAR|MYOBEC|MYODAS|PLEAUS)\b")

# Tags that must never leave the building through a public endpoint.
# `internal-only` marks copyrighted reference material (CIEEM, BCT...) that may be
# searched internally but never served to the public.
RESTRICTED_TAGS = {"sensitive", "has-grid-ref", "internal-only"}


def classify_sensitivity(text: str) -> list[str]:
    tags = []
    if SENSITIVE_HINTS.search(text) or SENSITIVE_CODES.search(text):
        tags.append("sensitive")
    if GRID_REF.search(text):
        tags.append("has-grid-ref")
    return tags
