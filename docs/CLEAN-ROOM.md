# Clean-room policy

**Purpose:** make sure the product can be sold without carrying anyone else's data or
work inside it. This is phase 1 of the enterprise roadmap ("right to sell"), and it is
what a buyer's due diligence will ask us to evidence.

## The rule

Client and MSP data may be *processed by* the product, at runtime, inside the
customer's own environment. It must never become *part of* the product.

"Part of the product" means anything we ship or sell: source code, default settings,
detection thresholds, test fixtures, sample data, trained models, prompts, agent
instructions, documentation examples, screenshots, installers and the website.

## What the product may be built from

| Allowed | Examples |
| --- | --- |
| Our own code and writing | Everything in this repository |
| Synthetic data we generate | `tools/emergence-kit/devtools/make_fake_card.py` and its ground truth |
| Openly licensed material, with its licence honoured | QGIS manual (CC BY-SA), Stack Exchange posts (CC BY-SA), linked, attributed and fetched at runtime |
| Published file-format specifications | GUANO, AVCHD/XAVC S folder layouts, MP4 |
| Public APIs used within their terms | Stack Exchange API, GitHub API, Discord bot API |

## What it must never be built from

| Not allowed | Why |
| --- | --- |
| Client survey data, recordings, reports, QGIS projects | Client property; often protected-species locations |
| Discord support threads or MSP tickets | Belong to the MSP and the people in them; Discord policy restricts reuse |
| NightArc databases or other vendors' software internals | Client data and third-party IP |
| Copyright guidance text (CIEEM, BCT) | Copyright; internal reference only, paraphrase and cite |
| Anything learned under an NDA | Contractual |

Support history can tell us **what** hurts, so we know what to build. It must not
supply **how**: no copying code, configuration, wording or data from it.

## How it's enforced

1. `provenance.yaml` lists every data source, its owner, licence and whether it may
   ship. `PROVENANCE.md` is the readable version. Review both quarterly and whenever a
   source is added.
2. `scripts/check_provenance.py` fails the build if the repository contains unlisted
   data files, files over 5 MB, Discord channel links, private keys or real-looking
   credentials. It runs on every change (roadmap phase 7) and before every release.
3. Detection defaults are tuned only on synthetic data, and later on real footage
   used **with written permission** of its owner, recorded in `provenance.yaml`.
4. Development and tests use synthetic data. Real client data is only touched when
   doing paid client work, on the client's systems.

## When in doubt

Don't commit it. Ask, and record the answer in `provenance.yaml`.
