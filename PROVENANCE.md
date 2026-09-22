# Data provenance register

Readable version of `provenance.yaml` (the machine-checked one). Reviewed 22 Sep 2026.
Policy: [docs/CLEAN-ROOM.md](docs/CLEAN-ROOM.md).

| Source | Owner | Licence | Used for | Ships in product? |
| --- | --- | --- | --- | --- |
| Synthetic camera cards and ground truth | Us (generated) | Own work | Tests, detection tuning, demos | Allowed |
| QGIS User Manual 3.40 LTR | QGIS Project | CC BY-SA 3.0 | Knowledge base, cited | No: fetched per install |
| CIEEM EcIA (2024) and Report Writing (2017) | CIEEM | Copyright, internal only | Internal retrieval, paraphrased | No: each customer downloads their own |
| BCT bat survey guidelines | Bat Conservation Trust | Licensed copy per organisation | Internal retrieval | Never |
| GIS Stack Exchange | Post authors | CC BY-SA | Knowledge base, attributed | No: fetched at runtime |
| OSGeo QGIS forum | Post authors / OSGeo | Author copyright, forum terms | Knowledge base, linked | No: fetched at runtime |
| QGIS GitHub issues | Issue authors | GitHub terms | Knowledge base, linked | No: fetched at runtime |
| Discord support threads | MSP and participants | None for reuse | Internal MSP retrieval | Never |
| Client file shares (NAS) | Each client | Client contract | Retrieval in that client's environment | Never |
| NightArc databases | Client (data), Arbology (software) | Client contract, vendor licence | Read-only schema cards in that client's environment | Never |

Only the files listed under `repo_data_allowlist` in `provenance.yaml` may be data-like
files in this repository. `python scripts/check_provenance.py` checks it.
