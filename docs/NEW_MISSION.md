# Running a whole mission in one shot (with the vetting gate)

This is the plain-English guide for digitizing an entire mission's
packets (one year, one site -- e.g. Ghana 2025) the way Peru 2019 was
done, but autonomously in one go.

## One-time machine setup

1. Get an API key at console.anthropic.com (starts with `sk-ant-`).
2. Install system tools: Tesseract OCR and Poppler
   (Mac: `brew install tesseract poppler`).
3. Install Python libraries: `pip install -r requirements.txt`.
4. In your terminal: `export ANTHROPIC_API_KEY=sk-ant-your-key`
   (never paste the key into any file).

## Before the first run of a NEW mission: calibration

Do this BEFORE spending any API money:

1. Copy `missions/_TEMPLATE_new_mission.json` to e.g.
   `missions/ghana_2025.json`.
2. Open 2-3 sample packet PDFs by eye and fill the profile in from what
   you actually see: MRN prefix, real date range, staff names, and any
   handwriting quirks worth warning the extractor about.
3. If the mission's forms are laid out differently from the Peru ISHI
   template (different timing boxes, different labels), update
   `TIMING_FORM_LAYOUT` in `extract.py` to describe the REAL layout.
   This matters a lot: on Peru, most timing errors came from reading the
   wrong (adjacent) box.

Everything mission-specific lives in the profile JSON. The code stays
mission-agnostic -- that's what stops stale Peru facts from
contaminating Ghana data.

## Step 1: vet 5 packets (the stop-and-check)

```
python run_mission.py --mission missions/ghana_2025.json \
    --pdf-dir "Ghana 2025 PDFs" --vet 5
```

This extracts ONLY the first 5 packets and then **stops on purpose**.
It produces two files:

- `..._VET.xlsx` -- a mini case log of just those 5 patients.
- `VET_CHECKLIST_<prefix>.xlsx` -- one row per extracted value with empty
  "Paper log says / Match? / Correction" columns, plus a "How to vet"
  tab. Rows the pipeline was itself unsure about are highlighted yellow.

Sit down with the physical paper logs and fill the checklist in. Two
things to check besides plain wrong values:

- **Blanks**: `(absent)` means the pipeline refused to guess. Confirm the
  paper box is really empty. If the paper HAS a value the pipeline
  blanked, the scan or prompt needs attention -- that's a finding, not
  a pass. (The full Case Log workbook marks these the same way, just as
  a hover comment + light gray shading on an otherwise-real-blank cell,
  so Excel formulas that depend on blank cells still work.)
- **Repeats**: one wrong digit on one packet is noise; the SAME box
  wrong on 3 of 5 packets is a systematic error.

## Step 2a: the vet passed -- run everything

```
python run_mission.py --mission missions/ghana_2025.json \
    --pdf-dir "Ghana 2025 PDFs"
```

This processes every remaining packet and writes the mission-named log
(e.g. `ISHI_Case_Log_Ghana_2025.xlsx`). The 5 vetted packets are reused,
not re-extracted -- you never pay for them twice. If the run is
interrupted (network, laptop closed), just run the same command again;
it resumes where it stopped.

When the full run finishes cleanly, the pipeline archives itself
automatically -- see "Step 3: automatic archiving" below. There's
nothing to run for this; it just happens.

## Step 2b: the vet FAILED ("When the vet fails")

Do NOT run the full mission. The whole point of the gate is that a
systematic misread costs you 5 packets, not 60. In order of likelihood:

1. **Same box misread across packets** (e.g. every Surgery Start is
   actually the box next to it): the form layout description is wrong
   for this mission. Fix `TIMING_FORM_LAYOUT` in `extract.py` to match
   the real form.
2. **Dates or names systematically off**: the mission profile is wrong
   or too vague -- fix the date window / roster / add an
   `extraction_hints` line describing the failure you saw (these hints
   are injected straight into the extraction prompt).
3. **Pages misclassified** (a flowsheet extracted as an H&P): the page
   keywords in `extract.py` don't match this mission's forms -- the
   `PAGE_KEYWORDS` vocabulary needs terms from the new forms.

Then delete the affected files in `raw_json_output/` (only the new
mission's -- never touch another mission's JSONs) so they re-extract
fresh, and run `--vet 5` again. Repeat until the checklist comes back
clean, then do Step 2a. If you're unsure how to make any of these fixes,
open a Claude session in this folder, attach the filled-in checklist,
and describe what the vet found -- the checklist's flag text is written
so it can diagnose the problem from it.

## Step 3: automatic archiving

The moment a **full** run (not a `--vet` run) finishes writing its
deliverable, `run_mission.py` files everything specific to that mission
into a folder named `Legacy <LETTERS> <YEAR>` next to it -- e.g. a
Ghana 2025 run (prefix `GH25`, profile `"year": 2025`) lands in
`Legacy GH 2025`. Moved in: the deliverable workbook, its `_VET.xlsx`
and `VET_CHECKLIST`, the source PDF folder (only if it lives inside this
pipeline's own folder -- a `--pdf-dir` pointed elsewhere on your machine
is left alone), that mission's raw JSON (into its own
`raw_json_output <LETTERS> <YEAR>` subfolder, with a README explaining
what it's for), and the run log covering both the vet and full run.

Left where it is on purpose, so the next mission can just start:
`missions/<profile>.json` (still there for an audit or a re-run) and any
`_page_images/` scratch cache (regenerable, not a record of anything).

If you re-run the full-mission command against an already-archived
mission (nothing left to extract, nothing left to move), archiving is a
no-op -- it only ever moves files that are still sitting at the top
level.

## What "one-shot" does NOT skip

The vet gate catches systematic errors early; it does not replace the
normal human review of flagged fields. The final xlsx still highlights
every low/medium-confidence value, and anything
illegible is left blank with a "time unclear" note rather than guessed
-- those blanks are review work by design.

## Costs, roughly

Only Stage 2 (field extraction) calls the API -- about one vision call
per extractable page, a few cents each. Page classification is free and
local. A 60-packet mission lands in the tens of dollars, and the vet
gate means a bad configuration only ever burns the 5-packet sample.
