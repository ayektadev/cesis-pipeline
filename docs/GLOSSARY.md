# Glossary of terms

A plain-language dictionary for every term used in the case log, the raw
extraction JSON, and this pipeline's own vocabulary. If you're handing this
to someone unfamiliar with the project (or to the ISHI team for review),
start here.

## Clinical/form terms (as used on the ISHI paper forms)

| Term | Meaning |
|---|---|
| **H&P** | History & Physical -- the intake form: patient history, exam findings, planned operation. Also carries the "Brief Op Note" (what was actually done). |
| **MRN** | Medical Record Number. On these packets, it's the `#PR19-XX` number stamped on every page -- used here as the unique patient identifier. |
| **PMH** | Past Medical History. On forms that print an explicit checkbox row -- "PMH: [] HTN [] DMII [] COPD [] BPH" plus a separate "[] Other" line (first seen on Ghana's 2025-style H&P, also shared by Sierra Leone 2025) -- this is captured as five of its own columns (`PMH: HTN`, `PMH: DM II`, `PMH: COPD`, `PMH: BPH`, `PMH: Other`), sitting right after Gender rather than at the end of the sheet, since it's patient background rather than an outcome. See the extraction rules for the full Y/N/blank logic: on a mission whose form prints this row, every case gets Y or N in the four condition columns (an unticked box is a recorded negative there, not a skip); a mission whose form doesn't print the row gets blank cells across all five, to be revisited case by case rather than parsed out of free text. `PMH: Other` is captured only when its own checkbox is ticked -- text on that line without a tick is flagged for review, not trusted outright. A packet claiming "the row isn't on this form" is cross-checked against its sibling packets in the same batch (the extraction rules addendum) -- one lone "absent" claim surrounded by packets that DO have the row gets downgraded and re-flagged as a suspect misread rather than trusted at face value. **Pre-Anesthesia Evaluation fallback (added 2026-08, Ghana 2024-09):** on packets where the H&P genuinely has no checkbox row at all, these five columns instead fall back to the packet's "Pre-Anesthesia Evaluation" page, which carries an equivalent "PMH & Systems Review" table organized by body system (Cardiovascular/Pulmonary/Endocrine/Renal/Hepatic/Other). Only a row with an actual NAMED condition (e.g. "HTN", "DM2") maps to one of the four clean fields or, if unmatched, into `PMH: Other`; a bare, unelaborated mark in a row is too ambiguous (positive vs. "reviewed, normal") to map anywhere and is left blank. Fallback-sourced cells are tagged in their flag_reason so a reviewer can tell which page they came from -- see the extraction rules's Pre-Anesthesia Evaluation fallback addendum. |
| **PSH** | Past Surgical History. Captured as its own free-text column (`PSH`), sitting right after `PMH: Other` -- same reasoning as PMH, patient background belongs next to Age/Gender rather than at the end of the sheet. Unlike PMH, there's no checkbox row for this on the form, just a plain "PSH:" line, so it's transcribed verbatim with the ordinary "blank means unknown, not no" convention rather than PMH's Y/N-per-condition checklist logic. A null-indicator written on the line (Ø, slash/dash, "n/a", "none", "nil") is recorded as blank rather than transcribed literally, but still flagged quoting what was written, so a reviewer can confirm it wasn't actually illegible text. Added 2026-08, same vet gate as PMH -- see the extraction rules's addendum. **Pre-Anesthesia Evaluation fallback (added 2026-08, Ghana 2024-09):** on packets where the H&P has no "PSH:" line at all, this column falls back to the Pre-Anesthesia Evaluation page's own PSH-equivalent entry, using the same verbatim/null-indicator rules, tagged in flag_reason so a reviewer knows it came from a different page. |
| **CC/HPI** | Chief Complaint / History of Present Illness -- why the patient came in, in their own timeline. |
| **EBL** | Estimated Blood Loss during the operation. |
| **ASA class** | American Society of Anesthesiologists physical status classification (I-VI) -- a standard pre-op risk score. |
| **PACU** | Post-Anesthesia Care Unit -- where a patient recovers immediately after surgery, before going to the floor or being discharged. |
| **NPO** | Nil per os -- nothing by mouth (a pre-op/post-op fasting instruction). |
| **POD#** | Post-Operative Day number (POD#1 = the day after surgery). |
| **AAOx3 / AAOx4** | "Alert and oriented x3 (or x4)" -- a standard mental-status check (oriented to person, place, time, [situation]). |
| **C/D/I** | Clean, Dry, Intact -- standard wound-check shorthand. |
| **NAD** | No Acute Distress. |
| **H-score (H1-H4)** | A hernia-severity grade printed on the inguinal hernia forms. **This differs by mission year.** On the 2019 (Peru) forms it was rarely marked, so the pipeline left it blank. On the 2025 (Sierra Leone) H&P it is a printed "Clinical classification for inguinal hernia" table with a box actually circled, so it is *transcribed*, not inferred. A blank still means the box was unmarked -- never that the hernia was small. |
| **F-score** | The skin-fold measurement printed alongside the H grade in the same 2025 classification table. Recorded separately from the H grade because they are separate boxes measuring separate things; one being marked does not imply the other is. **ISHI does not routinely collect this grade on mission** (`questions_for_doc.md` #5, answered): it is captured faithfully whenever the box IS marked, but an unmarked box is treated as the expected, normal case -- high confidence, no flag -- rather than as a documentation gap. |
| **Inguinoscrotal component (a/b/c)** | The size sub-classification that accompanies an H3 or H4 grade on the 2025 form. It is only meaningful for H3/H4; if it appears against an H1 or H2 the pipeline flags the contradiction rather than picking a winner. |
| **Reducible / irreducible** | Whether the hernia can be pushed back through the defect. Recorded as written (including forms of words like "b/l reducible R>L") rather than forced into a yes/no, because the nuance is clinically real. |
| **Primary vs recurrent** | Whether this is the first repair at this site or a repair of a hernia that came back. A blank means the form did not say -- it is **not** evidence of a primary hernia, and the Summary sheet says so next to the count. |
| **Mesh / plug-and-patch / tissue repair** | The repair technique recorded in the Brief Op Note ("Plug/patch" is a mesh technique) -- this stays `repair_technique`'s authoritative source. As of 2026-08 (SL22 vet gate), every extracted page type is also scanned independently for the word "mesh" anywhere on it (not just its own listed fields); if it turns up somewhere the Procedure line doesn't already say so, a flagged note is appended to `repair_technique` naming which page(s) and what they said, rather than being silently added to or left out of the field. See `the extraction rules` rule 23. |
| **CDI** | See C/D/I above -- clean, dry, intact. The usual wound finding on the Post-Op Visit form. |
| **Seroma / hematoma** | A post-op fluid or blood collection at the wound. Common, usually self-resolving, and recorded as a complication when the surgeon writes it down. |

## The three separate timing measurements

These are measured as three genuinely distinct things, not interchangeable,
per the source forms (both the Anesthesia Record and OR Record print all
three independently):

| Term | Definition | Column in the log |
|---|---|---|
| **OR Time** | In-room to out-of-room -- the broadest window, total time the patient physically occupied the operating room. Read from the Anesthesia Record's own OR Time row by default, falling back to the OR Record's own In-room/Out-of-room row if the Anesthesia Record's is blank (reversed 2026-08; originally OR Record only). This is the sole "duration" column since `Case Duration` was retired (see below). As of 2026-08 (SL19 vet gate) it has exactly one narrow fallback beyond that: if an In was found but the Out is blank on BOTH forms, the Anesthesia End time is substituted as a last-resort Out -- always at low confidence, always named in the flag. Beyond that one case, still no fallback to Anesthesia Time or anything else, so a blank here means neither form's OR Time row was filled in AND no Anesthesia End was available either. | `OR Time (mins)` |
| **Surgery Time** (a.k.a. **Procedure Time**, a.k.a. **operative duration**) | First incision to closing. **Source of truth: the Anesthesia Record's own Surgery Start/End.** Falls back to the OR Record's own Procedure Start/End if that wasn't filled in; falls back further to the H&P's handwritten "Case duration" only if neither timestamp pair exists. **Confirmed by Dr. Grotheer** as the correct definition of operative duration. | `Surgery Time (mins)` and `operative time (mins)` (same measurement, two columns for compatibility with the original required schema) |
| **Anesthesia Time** | Anesthesia Start to Anesthesia End. Often has no recorded End on these forms (patient leaves for PACU still under residual effects before an "end" gets written down) -- when that happens, this is left blank and flagged, not silently substituted with another timestamp. | `Anesthesia Time (mins)` |
| **Case Duration** *(retired)* | Formerly Anesthesia Time when available, else OR Time, always flagged as provisional. Retired at the client's direction (`questions_for_doc.md` #2, resolved) because it never had a fixed definition and silently mixed two different windows under one column name. `OR Time (mins)` above is now the sole surviving "duration" column from that concept, reported as the literal OR-record measurement only -- no fallback was added to it when `Case Duration` was removed. This row is kept here as a historical pointer for anyone who remembers the old column name. | *(none -- column removed)* |
| **Secondary Surgery Time** | Only populated when a same-day repeat procedure occurred (see "Same-day repeat procedure" below) -- the Surgery Time of the SECOND operation specifically, computed from its own Anesthesia Record/OR Record pages, never mixed with the primary procedure's timestamps. Blank for single-procedure packets. The former `Secondary Case Duration (mins)` counterpart was retired along with `Case Duration` itself. | `Secondary Surgery Time (mins)` |

## Anesthetist vs Anesthesia Type

Two separate columns, split apart during the Sierra Leone 2024 full run
after `anesthetist` was found conflating the two on some packets (e.g. the
H&P line read `"Spinal/[name removed]"` -- a technique and a name mashed
together):

| Term | Definition | Column in the log |
|---|---|---|
| **Anesthetist** | WHO gave the anesthesia -- a name (initials or written out), never a technique word. Sourced from the Anesthesia Record's own "Anesthetist" line first (filled in during/after the case, by the person who was there); falls back to the OR Record's "Anesthesiologist" line; falls back to the H&P's pre-op line last, flagged, since that page is filled in before the case and has occasionally been transcribed with the planned technique bundled in. | `Anesthetist` |
| **Anesthesia Type** | WHAT technique was used -- General / Regional / Spinal / Epidural / CSE / MAC-IV sedation / Local, etc, never a name. Same page-preference order as Anesthetist: Anesthesia Record's technique box, then OR Record's, then the H&P's pre-op "planned" box last, flagged (since that one is the plan, not necessarily what was actually given). | `Anesthesia Type` |

## Post-op course and outcome columns (added for Sierra Leone 2025)

These come from the PACU Flowsheet, Floor Flowsheet, Progress Notes, RN
Discharge form and the ISHI Post-Op Visit form -- pages the earlier build
classified but never actually read.

| Term | Definition | Column in the log |
|---|---|---|
| **PACU arrival / transfer** | The times printed on the PACU Flowsheet as "Arrival time in PACU" and "Time Transferred to Floor/Discharged". Transcribed from that form only; never inferred from the OR Record. | `PACU Arrival`, `PACU Transfer to Floor` |
| **PACU time** | Transfer minus arrival, in minutes. If the interval comes out longer than 12 hours the pipeline treats the two times as likely swapped or misread and flags rather than reporting a number. | `PACU Time (mins)` |
| **Floor arrival** | "Date/Time Arrived" on the Floor Flowsheet, normalised to a full date and time. The form often writes only a day and time ("12/8 12:35"); the year comes from the surgery date, not from a guess. On some missions this form's day/month order is inconsistent -- see the mission profile's `extraction_hints` for that mission before trusting an out-of-window Floor Flowsheet date. | `Floor Arrival` |
| **Last Entry Date** | The date on the final dated nursing entry on the RN Discharge form. Reported for every packet whether or not it ends up used, but only becomes the discharge-date fallback (see Discharge date/time below) when the discharge signature block itself is genuinely undated. | `Last Entry Date` |
| **Discharge date/time** | **OPERATIONALIZED** (`questions_for_doc.md` #3/#4, answered) as the date the patient left the ward, with a clock time where the form records one. Resolved in a fixed order of preference: (1) the RN Discharge form's date **and** time; (2) the Floor Flowsheet's discharge date/time; (3) the RN Discharge date alone, time set to 00:00; (4) the Last Entry Date above, time set to 00:00, used when the discharge signature block is undated ("the date the surgeon documented" -- Q4's answer); (5) as a last resort, the PACU transfer time, explicitly flagged as NOT a recorded discharge. Which source was used is always written into the cell's flag, so a 00:00 time reads as "no time recorded," not midnight. If none of these exist the cell is blank and flagged -- a discharge is never invented. | `Discharge Date/Time` |
| **LOS (hours)** | **The operationalized measure to analyse** (`questions_for_doc.md` #3, answered): hours from OR in-room (falling back to anesthesia start, then PACU arrival, whichever is the first readable anchor on the day of surgery) to the Discharge Date/Time above, reported to one decimal place. Distinguishes a 4-hour stay from a 20-hour one, which whole-day LOS cannot. | `LOS (hours)` |
| **LOS** | The same stay expressed in whole calendar days (same-day discharge is 0, not 1), kept only for compatibility with the original required schema. Coarser and less useful than the hours column -- prefer LOS (hours) for analysis. | `LOS` |
| **Last POD documented** | The highest post-operative day number appearing on any Progress Note in the packet. A proxy for how long the patient was followed on the ward, not for length of stay. | `Last POD Documented` |
| **Discharge criteria met** | Transcribed from the RN Discharge form's own tick box. Blank means unticked, which is not the same as "not met". | `Discharge Criteria Met` |
| **Post-Op Visit form** | An outpatient follow-up form new in 2025 with no 2019 equivalent. Carries the visit date, POD, wound findings, any complication, and whether further surgery is planned. | `Post-Op Visit *` columns |
| **Seen post-op** | Whether there is an actual Progress Note or Post-Op Visit page in the packet. Formerly this answered "Y" whether or not any evidence existed, so the column read as 100% follow-up. It is now blank when there is no evidence either way. **Blank means unknown, not no.** | `Seen Post Op` |
| **Post-op complication** | Only what a clinician wrote down as a complication. The denominator is the number of documented follow-up visits, **not** the total number of cases -- a patient who never returned cannot be counted as complication-free. | `Post-Op Complication` |

## Pipeline-specific terms

| Term | Meaning |
|---|---|
| **Packet** | One patient's full set of scanned forms -- everything under one MRN (`#PR19-XX` in 2019, `SL25-XX` in 2025). The 2025 packets run to about 11 pages: post-op visit, H&P, consent, pre-anesthesia evaluation, anesthesia record, OR record, PACU orders, PACU flowsheet, floor flowsheet, progress note, RN discharge. |
| **Unclassified** | A page the free local OCR classifier could not identify. It is a deliberate outcome, not a failure mode: an unidentified page is visible in the audit and simply contributes nothing, whereas the old behaviour of defaulting such pages to "H&P" invented a second H&P and, through the extraction rules, invented a second operation. |
| **Tier 1 / Tier 2** | Tier 1 = the case-log fields (current scope). Tier 2 = a richer per-patient record (meds, vitals timeline, anesthesia agents, consent, allergies) -- planned but not yet built; see `extract.py`'s `PAGE_PROMPTS` placeholder. |
| **Reconciliation** | The step that merges a packet's many pages into one case-log row, resolving conflicts and computing derived fields (durations, categories). Lives in `reconcile.py`. |
| **Confidence** (`high` / `medium` / `low`) | How much to trust an extracted value. `high` = clearly legible and uncontested. `medium` = legible but ambiguous, estimated, or from a weaker source. `low` = illegible, missing, conflicting across pages, or physically implausible. |
| **Flag / flag_reason** | A specific, human-readable note explaining *why* a field needs a second look -- shown as a comment on the highlighted (yellow) cell in the Excel output. |
| **Procedure Category** | A normalized bucket (from a small fixed list -- see `PROCEDURE_CATEGORIES` in `schema.py`) that free-text procedure descriptions get mapped into, so the Summary sheet can count/group by procedure type despite spelling variation (e.g. "laparoscopic" vs. "laparascopic") in the source notes. |
| **Same-day repeat procedure** | A second operation on the same patient, same admission (e.g. an immediate re-exploration after a recurrence). Folded into one case-log row (`Secondary Procedure` + `Complications/Notes`), not split into two rows -- see the extraction rules. |
| **`_extracted_pages_cache` / `raw_json_output`** | Per-patient JSON holding everything read off every page of a packet, before reconciliation collapses it into one case-log row. This is a real, browsable output now (see `raw_json_output/`), not a hidden temp file. |

## Excel-specific terms (for the output file itself)

| Term | Meaning |
|---|---|
| **Excel Table (`CaseLog`)** | The case-log data is registered as a named Excel Table (not just a plain cell range), so future pipeline runs can regenerate/expand it cleanly and the Summary sheet's formulas (`CaseLog[Column Name]`) stay valid as rows are added. |
| **Yellow cell** | Needs a human to check the original scan. Hover or right-click -> Show Comment for the specific reason. |
| **Summary sheet** | A second tab, built entirely from formulas referencing the `CaseLog` table (case counts, procedure mix, average durations, data-quality snapshot) -- it recalculates automatically whenever the Case Log table changes; nothing on it is a hardcoded number. |
