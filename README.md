# CESIS chart-recovery pipeline (ISHI edition)

Open-source software that turns scanned, handwritten surgical charts into a structured, analysis-ready case log. It was built for the International Surgical Health Initiative (ISHI). It is part of the Center for Equitable Surgical Information Systems (CESIS) effort: a bridge between paper charts and an electronic medical record for surgical programs in low-resource settings.

This repository holds **version 1.0**, the exact code used in:

> Faizullabhoy E, Grotheer K, Alvarado F, Berry C, Sifri Z. *Closing the Surgical Data Gap: Low-Cost Recovery of Handwritten Records from Short-Term Surgical Trips to Sierra Leone and Ghana.* Manuscript in preparation (2026).

If you use this software, please cite that paper (see [Citing](#citing)).

## What it does

Each patient's scanned chart (one PDF "packet") goes through three stages:

1. **Page classification on your own computer.** Tesseract OCR reads each page and matches the printed form title (H&P, Anesthesia Record, OR Record, PACU Flowsheet, Post-Op Visit and so on). Pages with nothing to extract, such as consent forms, are dropped here at no cost.
2. **Extraction by a vision-language model.** Each remaining page is sent to the model with a prompt written for that form. The model returns every field with a confidence rating, and it leaves a field blank rather than guess when the handwriting is unreadable. Version 1.0 uses Claude Sonnet 5 through the Anthropic API. The model is a swappable component.
3. **Rule-based reconciliation.** The pages are merged into one record per patient. Formats and plausible ranges are checked, the forms are cross-checked against each other (for example, operative times on the OR Record against the Anesthesia Record), and every derived value keeps the source it came from.

The output is an Excel case log (one row per patient, with low-confidence cells highlighted) plus a vetting checklist for spot-checking values against the paper.

In the study above, the pipeline processed 1,076 charts (9,087 pages) from 18 trips for **US$197 in API costs, about $0.18 per patient**.

## What this repository contains, and what it does not

| Included | Not included |
|---|---|
| Pipeline code (`extract.py`, `reconcile.py`, `run_mission.py`, `schema.py`, `xlsx_writer.py`) | Any patient chart, image, extraction output or case log |
| Mission configurations for ISHI's Sierra Leone and Ghana trips (`missions/`) | API keys |
| Offline tests with synthetic data (`tests/`) | The internal development log |
| A synthetic example case log (`examples/`) | |
| The concordance analysis code from the paper (`analysis/`) | Configurations for other ISHI sites |

Before release, chart numbers cited in code comments and configurations were replaced with placeholders (900-series numbers, such as `SL25-901`). Staff names were removed and the test fixtures made synthetic. The pipeline itself is unchanged.

## Requirements

- Python 3.11 or later
- [Tesseract OCR](https://github.com/tesseract-ocr/tesseract) and [Poppler](https://poppler.freedesktop.org/) (Mac: `brew install tesseract poppler`; Ubuntu: `sudo apt install tesseract-ocr poppler-utils`)
- An API key from your own model provider account (version 1.0: an Anthropic key from console.anthropic.com). **You pay for your own usage; no key is bundled.**

```bash
git clone https://github.com/ayektadev/cesis-pipeline.git
cd cesis-pipeline
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...      # or put the key in a file named api_key.txt (git-ignored)
python3 check_setup.py                    # confirms Tesseract, Poppler, libraries and key
```

## Running a mission

A "mission" is one trip: one site and one set of charts.

1. Copy `missions/_TEMPLATE_new_mission.json` and fill it in from two or three sample charts: the chart-number prefix, date range and any form quirks.
2. **Vet first.** Extract five packets, then compare them with the paper:
   ```bash
   python3 run_mission.py --mission missions/my_trip.json --pdf-dir "path/to/packet PDFs" --vet 5
   ```
3. Once the vetting checklist looks right, run the full trip:
   ```bash
   python3 run_mission.py --mission missions/my_trip.json --pdf-dir "path/to/packet PDFs"
   ```
   To process packets in parallel, use `par_extract.py --mission ... --pdf-dir ... --workers 6`.

For the full walkthrough, see [`docs/NEW_MISSION.md`](docs/NEW_MISSION.md) and [`docs/START_HERE.md`](docs/START_HERE.md). Field definitions are in `schema.py` and [`docs/GLOSSARY.md`](docs/GLOSSARY.md), and cost notes are in [`docs/COSTS.md`](docs/COSTS.md).

The prompts and form layouts in version 1.0 are written for ISHI's chart templates. Programs with different forms will need to adapt the page-title keywords and prompts in `extract.py`. A form-agnostic version is planned.

## Patient privacy

The software runs on your own computer. Page classification is local, but **the pages selected for extraction are sent to the model provider's API**. Before processing real charts, make sure you have the approvals your institution and partners require: ethics review, data use agreements, and an API account whose data-retention terms fit your setting. Keep chart PDFs, extraction outputs and case logs out of version control (the included `.gitignore` excludes them by default).

## Tests

```bash
python3 tests/test_reconcile.py
python3 tests/test_ghana26o_options.py
```

The tests run offline with synthetic data and make no API calls.

## Citing

GitHub's **"Cite this repository"** button (from `CITATION.cff`) gives formatted citations. Please cite the paper above and, where possible, the software version you used.

## License

Licensed under the [Apache License 2.0](LICENSE). You may use, modify and redistribute this software, including commercially. If you redistribute it or a derivative, you must keep the copyright and attribution notices in [`NOTICE`](NOTICE). Citation of the paper in academic work is requested.

## Acknowledgments

Developed with support from Dr. Ziad Sifri and the International Surgical Health Initiative (ISHI). We thank the surgical teams at Kabala Government Hospital (Sierra Leone) and Tetteh Quarshie Memorial Hospital (Ghana), and the ISHI volunteers whose records made this work possible.
