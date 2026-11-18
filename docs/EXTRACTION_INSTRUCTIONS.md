# Extraction instructions for ISHI packet subagents (READ FULLY BEFORE STARTING)

You are extracting structured data from scanned, mostly-handwritten surgical
mission packets (Peru 2019). You will be given a list of packet IDs (e.g.
PR19-23). For each packet:

- Page images: `_page_images/<packet_id>/page-01.png`, `page-02.png`, ... (in
  document order). READ EVERY PAGE IMAGE with the Read tool and extract by
  looking at the image directly. Do NOT call any API, do NOT run extract.py.
- OCR pre-classification for each page is in
  `_page_images/classification_manifest.json` (key = packet id, then filename).
  Use it as a starting hint, but YOU decide the true page type by looking at
  the image. The classifier's fallback type is "H&P", so blank or unusual pages
  are sometimes mislabeled H&P. Record any disagreement (see report section).
- Output: write `raw_json_output/<packet_id>.json` (UTF-8, indent 2).

## Output JSON shape (must match existing files exactly)

```json
{
  "packet_id": "PR19-23",
  "patient_name": "<full name as written on the forms>",
  "pages": [
    {"page_type": "<one of the 10 types below>",
     "page_label": "<short human label, e.g. 'H&P p1' or 'Anesthesia Record'>",
     "fields": { "<field>": {"value": ..., "confidence": "high"|"medium"|"low", "flag_reason": "<string, ONLY when there is something to flag>"} }}
  ]
}
```

- Include EVERY page of the packet in "pages", in document order. Pages with
  nothing worth extracting (e.g. Consent, blank pages) still appear, with
  `"fields": {}`.
- `flag_reason` is omitted entirely when there's nothing to flag (do not write
  null). Confidence "medium"/"low" should almost always come with a flag_reason.
- NEVER guess silegibly. If a value is illegible/ambiguous: value null (or your
  best reading), confidence "low", flag_reason explaining the problem. NEVER
  fabricate a timestamp that is not on the form — leaving it out is correct;
  inventing it is a serious error.

Valid page_type values: "H&P", "Consent", "Pre-Anesthesia Evaluation",
"Anesthesia Record", "OR Record", "PACU Orders", "PACU Flowsheet",
"Floor Flowsheet", "Progress Note", "RN Discharge".

## Field names per page type (downstream code depends on these EXACT keys)

**H&P** (History & Physical / Brief Op Note — the only form with no boxed
title; has "History and Physical" / "Brief Op Note" printed sections):
`age` (int), `gender` ("M"/"F"), `preop_dx`, `postop_dx`, `procedure`,
`case_duration_mins` (int, from the handwritten "Case duration" line if
present), `symptom_duration_years` (number; "6 months" -> 0.5),
`surgeon_assistant` (e.g. "Smith/Jones"), `anesthesiologist`,
`ebl_complications`, `date` ("YYYY-MM-DD").

**Pre-Anesthesia Evaluation**: `asa_class` (e.g. "II"), `anesthesia_plan`.
IMPORTANT: if the HPI line states an age (e.g. "60 y/o F"), also extract
`age` here — the pipeline cross-checks it against the H&P age (rule 3).
If the MRN field looks wrong/corrected, add `mrn_as_written` with a flag.

**Anesthesia Record**: `date`, `or_time_in`, `or_time_out` (only if present),
`surgery_start`, `surgery_end` (the form's own Surgery/Procedure Start-End
line), `anesthesia_start`, `anesthesia_end` (ONLY if actually filled in —
frequently blank; never substitute), `anesthesiologist`, `event_note` (only
for notable clinical events written in the events area — complications,
med reactions, chest pain, etc., with times as written).
Times are 24h "HH:MM". Observed pattern on these forms: Anesthesia Start is
often identical to OR Time In — when you use that reading, keep the
flag_reason wording used in existing files: "Anesthesia Start read as
identical to OR Time In on this form (observed pattern across these packets);
Anesthesia End was not filled in".

**OR Record**: `in_room`, `out_of_room`, `procedure_start`, `procedure_end`
(the OR Record's OWN Procedure Start/End line — separate fields from In/Out;
extract them whenever filled: they are the fallback source of truth for
operative duration), `surgeon` (initials/code ok), `surgical_procedure`,
`anesthesia_type` ("General"/"Spinal"/etc.).

**PACU Orders**: `dx`, `other_instruction` (only if a non-routine instruction
was written).

**PACU Flowsheet**: `notes` — transcribe clinically notable handwritten notes
(desats, uncontrolled pain, sensory deficits, events), with times. Routine
"stable, no complaints" can be summarized briefly or omitted (fields {}).

**Floor Flowsheet**: `notes` — same idea; ALWAYS transcribe anything with
chest pain, bleeding, recurrence, vomiting, or other complications.

**Progress Note**: `pod` (int), `s_p` (what the patient is s/p), `disposition`
(e.g. "discharge to home") — ONLY if the page actually has content. A blank
Progress Note page gets `"fields": {}` (rule 4 depends on this).

**RN Discharge**: `discharge_note`, `floor_order_note` (later follow-up
addenda often appear here — transcribe them; if one implies a post-discharge
complication/recurrence, flag it).

**Consent**: `"fields": {}` (nothing extracted at this tier).

## Critical reconciliation rules you must respect

1. **Same-day repeat procedure (rule 1)**: If a packet truly contains TWO
   H&P/Brief-Op-Note pages (an initial procedure + a same-day
   re-exploration), keep them as two H&P page entries — label them
   "H&P p1 (initial)" and "H&P p1 (re-op, same packet)". The FIRST H&P in
   the list must be the initial procedure. Likewise, if there are two
   Anesthesia Records / OR Records (one per procedure), order them
   primary-first — downstream code pairs anes/OR page #1 with the primary
   procedure and #2 with the re-op, purely by order.
   BUT: verify a second "H&P"-classified page really IS a second procedure's
   H&P. Continuation pages, second flowsheet sheets, or blank forms
   misclassified as H&P must get their TRUE page_type instead — a false
   extra H&P would wrongly create a "repeat procedure" row note.
2. **Timestamps (rule 2)**: operative duration comes from the Anesthesia
   Record's Surgery Start/End; case/anesthesia episode from Anesthesia
   Start/End falling back to OR In/Out. YOUR job is only to transcribe each
   timestamp pair faithfully from the correct line of the correct form —
   the pipeline computes durations. Do not copy a value from one line into
   another. If a start/end box is blank, it stays absent from your JSON.
3. **Age conflicts (rule 3)**: extract age wherever it is stated (H&P AND
   Pre-Anesthesia Eval) — do not harmonize them yourself.
4. Blank Progress Note -> fields {} (see above).
5. Transcribe complication-relevant notes (events, chest pain, bleeding,
   recurrence, vomiting...) — the pipeline aggregates them.
6. If two pages contradict each other (e.g. right vs left extremity finding),
   transcribe both faithfully and flag in the affected fields naming both
   pages.

Read `raw_json_output/PR19-01.json` and `raw_json_output/PR19-07.json` first
as style templates (PR19-01 shows the two-procedure layout; PR19-07 shows the
OR Record procedure_start/procedure_end usage).

## Report file (required)

After writing the JSON files, write `_agent_reports/<first_id>_<last_id>.md`
containing, per packet:
- patient name, number of pages, page-type sequence you settled on
- any classifier disagreements (manifest said X, image is actually Y)
- count of flagged fields and one-line reasons
- anything that looks like a NEW edge case not covered by the rules above
  (e.g. missing forms, dates that disagree across pages, second admissions on
  different days, unusual procedures, unreadable pages)

Keep your final response message SHORT (a few lines per packet) — the report
file carries the detail.
