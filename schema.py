"""
schema.py

Defines the target shape of one Case Log row (matches the existing
ISHI_PR19_Case_Logs.xlsx columns exactly) and a generic wrapper that carries
confidence + provenance for every extracted field, so the pipeline can flag
uncertain values instead of silently guessing.

This is Tier 1 (case-log fields only). Tier 2 (rich per-patient record:
meds, vitals timelines, anesthesia agents, consent, etc.) reuses the same
FieldValue wrapper and page-classification step, but is out of scope for
this demo per the agreed sequencing.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional, List, Any
from datetime import date


CONFIDENCE_HIGH = "high"
CONFIDENCE_MEDIUM = "medium"
CONFIDENCE_LOW = "low"


@dataclass
class FieldValue:
    """A single extracted value plus how much to trust it."""
    value: Any
    confidence: str = CONFIDENCE_HIGH          # high | medium | low
    source_pages: List[str] = field(default_factory=list)  # e.g. ["H&P p1"]
    flag_reason: Optional[str] = None           # human-readable, shown in review

    @property
    def needs_review(self) -> bool:
        return self.confidence != CONFIDENCE_HIGH or self.flag_reason is not None


# Column order matches ISHI_PR19_Case_Logs.xlsx exactly, PLUS additional
# columns appended at the end (new columns never go in the middle -- that
# would silently shift every downstream reference to the original sheet).
# The three timing columns are measured as genuinely separate things per
# the source forms (each prints all three independently): OR Time In/Out,
# Anesthesia Start/End, and Surgery/Procedure Start/End. "operative time
# (mins)" is kept as its own column too, matching the original required
# schema -- operative_time_mins is set equal to surgery_time_mins (same
# underlying measurement, kept for compatibility with the pre-existing
# sheet).
#
# "Case Duration" was retired at the client's direction (questions_for_doc.md
# #2, resolved): it never had a fixed definition and the pipeline's fallback
# behavior meant it silently mixed two different windows (Anesthesia Time or
# OR Time depending on what was available) under one column name, which made
# it unsafe to average across patients. OR Time (mins) is now the sole
# duration column carried over from that concept, and it is reported ONLY
# as the literal OR-record in-room-to-out-of-room measurement -- no
# fallback to Anesthesia Time or anything else was added when the column
# was retired. If OR Time is blank, that means the source boxes were blank;
# nothing else is substituted in.
CASE_LOG_COLUMNS = [
    "date_of_surgery",
    "mrn",
    "age",
    "gender",
    # -- PMH checkbox block (added 2026-08) --------------------------------
    # DELIBERATE EXCEPTION to the "always append at the end" rule above.
    # Inserted here, not at the end, because this is patient background
    # information and belongs next to Age/Gender, not buried after outcome
    # columns -- see RULES.md rule 24 before adding anything else mid-list.
    # These are populated on ANY mission whose H&P prints the explicit PMH
    # checkbox row ("PMH: [] HTN [] DMII [] COPD [] BPH" + a separate []
    # Other line) -- that row is the source of truth wherever it exists.
    # Missions whose H&P records past medical history differently (e.g. as
    # free text, or not at all) get blank cells here, to be handled case by
    # case as those missions come up -- this is NOT limited to Ghana.
    "pmh_htn",
    "pmh_dmii",
    "pmh_copd",
    "pmh_bph",
    "pmh_other",
    # -- PSH free text (added 2026-08, same vet gate as PMH) ---------------
    # Past Surgical History. Sits directly after the PMH block for the same
    # reason PMH sits after Gender -- patient background, next to PMH, not
    # buried after outcome columns. Unlike PMH this is captured as plain
    # free text (there is no printed PSH checkbox row on these forms), so
    # there is no Y/N-per-condition or "blank means the row didn't exist"
    # logic here -- see RULES.md rule 24 addendum.
    "psh",
    "preop_dx",
    "postop_dx",
    "procedure",
    "secondary_procedure",
    "anesthetist",
    "anesthesia_type",
    "surgeon_assistant",
    "los",
    "complications_notes",
    "seen_post_op",
    "post_op_comments",
    "operative_time_start",
    "operative_time_stop",
    "operative_time_mins",
    "h_score",
    "symptom_duration_years",
    # -- appended columns (not part of the original required schema) --
    "or_time_start",
    "or_time_stop",
    "or_time_mins",
    "surgery_time_start",
    "surgery_time_stop",
    "surgery_time_mins",
    "anesthesia_time_start",
    "anesthesia_time_stop",
    "anesthesia_time_mins",
    "procedure_category",
    "fields_flagged_count",
    "secondary_surgery_time_mins",
    # -- hernia detail & severity (added for Sierra Leone 2025) ------------
    # The 2025 H&P prints a "Clinical classification for inguinal hernia"
    # table, so these are transcribed from a marked box rather than
    # inferred from prose. h_score above already holds the H grade; it is
    # NOT duplicated here. Every one of these is null for a non-hernia
    # case, and null is the correct answer -- see GLOSSARY.md.
    "hernia_f_score",
    "inguinoscrotal_component",
    "hernia_laterality",
    "hernia_reducibility",
    "hernia_recurrent",
    "repair_technique",
    "defect_count",
    # -- post-op course & outcomes (added for Sierra Leone 2025) ----------
    # Sourced from the PACU Flowsheet, Floor Flowsheet, Progress Notes and
    # the Post-Op Visit form, which the earlier Tier 1 build classified but
    # never extracted. los above is the whole-day count kept for
    # compatibility with the original sheet; los_hours is the real measured
    # interval and is the one to analyse.
    "pacu_arrival_time",
    "pacu_transfer_time",
    "pacu_los_mins",
    "floor_arrival",
    "last_entry_date",
    "discharge_datetime",
    "los_hours",
    "max_pod_documented",
    "discharge_criteria_met",
    "postop_visit_date",
    "postop_visit_pod",
    "postop_visit_findings",
    "postop_complication",
    "further_surgery_planned",
    # -- social history (added for Ghana 2026 October) ---------------------
    # Printed on the newer H&P: Occupation, ETOH / Substance use / Nicotine
    # (Denies / Past / Current) and Travel from/distance. Blank on every
    # mission whose H&P has no such block. Kept together as one block.
    "social_occupation",
    "social_etoh",
    "social_substance_use",
    "social_nicotine",
    "travel_from",
    "travel_distance",
    # -- EuraHS-QoL questionnaire block (added for Ghana 2026 October) -----
    # Patient-reported pre-operative hernia quality of life, from its own
    # page (European Hernia Society QoL score). One block, at the end, so
    # it reads as a self-contained survey section of the same patient row.
    "qol_timepoint",
    "pain_rest",
    "pain_activity",
    "pain_worst_week",
    "restrict_daily",
    "restrict_outside",
    "restrict_sports",
    "restrict_heavy_labour",
    "cosmetic_abdomen_shape",
    "cosmetic_hernia_site",
    "qol_pain_score",
    "qol_restriction_score",
    "qol_cosmetic_score",
    "qol_total_score",
]

# Visual blocks on the Case Log sheet: columns listed here get their own
# header colour (and the header text already carries the block's prefix),
# so a survey or a social-history section reads as one unit inside the same
# patient row. Columns not listed keep the standard header colour.
COLUMN_GROUPS = {
    **{c: "Social history" for c in ("social_occupation", "social_etoh", "social_substance_use",
                                     "social_nicotine", "travel_from", "travel_distance")},
    **{c: "EuraHS-QoL" for c in ("qol_timepoint", "pain_rest", "pain_activity", "pain_worst_week",
                                 "restrict_daily", "restrict_outside", "restrict_sports",
                                 "restrict_heavy_labour", "cosmetic_abdomen_shape",
                                 "cosmetic_hernia_site", "qol_pain_score", "qol_restriction_score",
                                 "qol_cosmetic_score", "qol_total_score")},
}

# What every column actually MEANS, attached to the header cell of the
# sheet itself as a hover note.
#
# The reason this lives in the workbook and not only in GLOSSARY.md: the
# spreadsheet is the artefact that gets emailed, copied into a paper, and
# read a year from now by someone who has never seen this repository. A
# definition that lives somewhere else is a definition that will not be
# read, and the columns most likely to be misread are exactly the derived
# ones -- two columns here are both called "LOS" and they are measured
# from DIFFERENT anchors and in different units.
#
# Rule for anything added later (see RULES.md rule 21): a derived column
# must state what it is computed FROM, not just what it represents. "Length
# of stay" is not a definition; "OR in-room to discharge, in hours" is.
COLUMN_DEFINITIONS = {
    # --- identity and case description -------------------------------
    "date_of_surgery": "Date the operation took place. Read from the Anesthesia Record's own "
        "Date box by default, falling back to the OR Record's Date box if the Anesthesia "
        "Record's is blank (changed 2026-08 at the SL22 vet gate; previously the H&P's Brief "
        "Op Note date line, which is frequently left blank). Written month-first "
        "(12/8/25 = 8 Dec 2025) per the mission profile.",
    "mrn": "Medical record number, from the '#SL25-' box on the form. Also the packet/PDF name.",
    "age": "Patient age in years as written on the H&P. Not computed from a birth date.",
    "gender": "M or F as ticked on the H&P.",

    # --- PMH checkbox block (added 2026-08) ---------------------------
    # Sourced ONLY from the H&P's printed "PMH: [] HTN [] DMII [] COPD []
    # BPH" checkbox row plus its separate "[] Other" line, on missions
    # whose form actually prints that row. Not derived from CC/HPI prose,
    # Meds, or anywhere else on the packet, even when a medication implies
    # a condition (e.g. Metformin implies DM) -- only the box itself is
    # authoritative. Blank across all four means the form did not print
    # this checkbox row at all (a different mission/form template); a
    # mission that DOES print the row gets Y or N in every one of the four
    # columns for every case, since a mission with this box is treated as
    # a complete checklist -- see questions_for_doc.md before changing.
    # A "blank" claim is itself cross-checked: reconcile.py's
    # flag_pmh_row_inconsistency() downgrades and re-flags any packet that
    # claims the row is absent when sibling packets in the same batch show
    # it present -- see that function and RULES.md rule 24's addendum
    # (added after the row was misread as absent once at the Ghana 2025
    # May vet gate).
    # --- PMH/PSH Pre-Anesthesia Evaluation fallback (added 2026-08, ahead
    # of the GH24 2024-09 vet gate) -----------------------------------
    # Some form templates (first seen on Ghana 2024-09, "similar to" Sierra
    # Leone 2024) print no PMH checkbox row and/or no PSH line on the H&P
    # at all. On those packets, extract.py's Pre-Anesthesia Evaluation
    # prompt reads an equivalent source on that page instead -- a "PMH &
    # Systems Review" table organized by body system (Cardiovascular/
    # Pulmonary/Endocrine/Renal/Hepatic/Other), and/or that page's own PSH
    # entry -- and reconcile.py's reconcile_packet() falls back to it ONLY
    # when the H&P's own field is genuinely absent (missing entirely, or
    # explicitly flagged "no PMH checkbox row"/"no PSH line on this form
    # template"). A bare mark in a Systems Review row with no named
    # condition is NOT mapped to any of the four clean Y/N fields (too
    # ambiguous -- could mean "positive" or "reviewed, normal" depending on
    # the author's convention); only an actual condition name/abbreviation
    # matching HTN/DM II/COPD/BPH triggers a Y, and anything else specific
    # goes into pmh_other. Fallback-sourced values are tagged in
    # flag_reason ("sourced from Pre-Anesthesia Evaluation...") so a
    # reviewer can tell at a glance which page it came from.
    "pmh_htn": "Y/N from the H&P's PMH checkbox row -- hypertension ticked. Y = box "
        "ticked, N = box explicitly left unticked on a form that prints this row. "
        "If the H&P has no PMH checkbox row on its form template, falls back to a "
        "named 'HTN'/'high BP' condition in the Pre-Anesthesia Evaluation's PMH & "
        "Systems Review table (see fallback note above). Blank means neither source "
        "had this field -- a claim that is itself cross-checked against sibling "
        "packets, see reconcile.py's flag_pmh_row_inconsistency().",
    "pmh_dmii": "Y/N from the H&P's PMH checkbox row -- Type II diabetes ticked. Same "
        "Y/N/blank convention as PMH: HTN above, including the Pre-Anesthesia "
        "Evaluation fallback.",
    "pmh_copd": "Y/N from the H&P's PMH checkbox row -- COPD ticked. Same Y/N/blank "
        "convention as PMH: HTN above, including the Pre-Anesthesia Evaluation "
        "fallback.",
    "pmh_bph": "Y/N from the H&P's PMH checkbox row -- BPH ticked. Same Y/N/blank "
        "convention as PMH: HTN above, including the Pre-Anesthesia Evaluation "
        "fallback.",
    "pmh_other": "Free text from the PMH row's own '[] Other' line, transcribed verbatim "
        "-- captured ONLY when that Other box is itself ticked. If the line has "
        "handwritten text but the box was not ticked, this is left blank and flagged "
        "for a reviewer, rather than trusting unticked handwriting as a confirmed "
        "entry. When the H&P has no PMH checkbox row at all, falls back to any "
        "specifically-named condition in the Pre-Anesthesia Evaluation's Systems "
        "Review table that does NOT match HTN/DM II/COPD/BPH (e.g. 'asthma', "
        "'sickle cell') -- see fallback note above.",

    # --- PSH free text (added 2026-08, null-indicator rule tightened same day) ---
    # Past Surgical History. Transcribed verbatim from the H&P's own "PSH:"
    # line -- there is no checkbox row for this the way there is for PMH,
    # so this is plain free text with no Y/N convention. An actual entry
    # (e.g. "C-section x2") is transcribed as-is. A null/negative indicator
    # written on the line -- "Ø", a slash/dash, "n/a", "none", "nil" -- is
    # NOT transcribed literally; it is recorded as blank (same as if the
    # line were empty), but still flagged, quoting what was written, so a
    # reviewer can confirm it really meant "none" and wasn't illegible.
    # Blank with no flag means the line was genuinely empty. Blank means
    # "no surgical history to report" (whether stated as a null-indicator
    # or truly empty), not that history was reviewed and confirmed empty
    # vs. not asked at all -- same spirit as "blank means unknown" (RULES.md
    # rule 20), refined here because the paper form gives an explicit
    # none-indicator most other free-text fields don't have.
    "psh": "Past surgical history, transcribed verbatim from the H&P's own 'PSH:' line "
        "(prior operations, e.g. 'C-section x2', 'Appy 2019'). Free text, not a "
        "checkbox. A null-indicator on the line (Ø, slash/dash, 'n/a', 'none', 'nil') "
        "is recorded as blank rather than transcribing the symbol/word itself, but "
        "still flagged quoting exactly what was written, so a reviewer can confirm "
        "it wasn't actually illegible text. Blank with no flag means the line was "
        "genuinely empty. If the H&P has no 'PSH:' line at all on its form template, "
        "falls back to the Pre-Anesthesia Evaluation page's own PSH entry, if any "
        "(see the Pre-Anesthesia Evaluation fallback note above pmh_htn) -- same "
        "verbatim/null-indicator rules apply there too, and the fallback is tagged "
        "in flag_reason.",

    "preop_dx": "Pre-operative diagnosis, transcribed from the H&P/OR Record as written "
        "(often theatre shorthand, e.g. 'RIH' = right inguinal hernia).",
    "postop_dx": "Post-operative diagnosis / operative findings line of the OR Record.",
    "procedure": "Operation performed, transcribed verbatim from the OR Record.",
    "secondary_procedure": "A second, distinct operation in the same admission, if one is "
        "documented. Blank means no second procedure was recorded -- not that none happened.",
    "procedure_category": "DERIVED. The free-text 'Procedure' mapped into a fixed category "
        "list so the Summary sheet can count them. Text that matches nothing becomes "
        "'Other/Uncategorized' at low confidence rather than being forced into a bucket.",
    "anesthetist": "Who gave the anesthesia -- a NAME (initials or written-out), never a "
        "technique word. Sourced from the Anesthesia Record's own 'Anesthetist' header line "
        "first (it is filled out during/after the case, by the person who was there); falls "
        "back to the OR Record's 'Anesthesiologist' line; falls back to the H&P's "
        "'Anesthesia/anesthesiologist' line last and flagged, since that line is filled in "
        "pre-op and has occasionally been transcribed with the planned technique bundled into "
        "the same field (e.g. 'Spinal/[name removed]') -- the two earlier sources don't have that "
        "problem, which is why they're preferred.",
    "anesthesia_type": "The anesthesia TECHNIQUE actually used -- General / Regional / Spinal / "
        "Epidural / CSE / MAC-IV sedation / Local, etc -- never a name. Sourced from the "
        "Anesthesia Record's own 'Anesthetic Technique' box first, then the OR Record's "
        "'Anesthesia Type' line, then the H&P's 'Anesthesia planned' box last (flagged, since "
        "that one is the pre-op PLAN and may not match what was actually given).",
    "surgeon_assistant": "Surgeon and assistant initials, from the ticked boxes on the OR Record.",

    # --- timing ------------------------------------------------------
    # Four separate durations that people routinely conflate. Each note says
    # which pair of clock times it subtracts, because that is the only way
    # to know why two of them disagree.
    #
    # Every duration is preceded by the two clock times it was computed
    # from (RULES.md rule 22). These are TRANSCRIBED, not derived -- they
    # are what is written on the form, so a reviewer can subtract them by
    # eye and see whether the minutes column is right. Where a bound is
    # blank but the duration is filled, the duration did not come from a
    # timestamp pair at all, and its own note says where it did come from.
    "or_time_start": "TRANSCRIBED, 24h clock. The 'In room' time from the OR Time row of the "
        "OR Record -- the left-hand bound of OR Time (mins). Blank means the box was not "
        "filled in or was unreadable.",
    "or_time_stop": "TRANSCRIBED, 24h clock. The 'Out of room' time from the OR Time row of "
        "the OR Record -- the right-hand bound of OR Time (mins). Frequently blank on these "
        "forms, which is why OR Time (mins) is so often blank too.",
    "surgery_time_start": "TRANSCRIBED, 24h clock. Procedure/Surgery Start -- knife to skin, "
        "and the left-hand bound of Surgery Time (mins). Taken from the Anesthesia Record "
        "where it is filled in, otherwise the OR Record's own Procedure Start; the flag says "
        "which page it came from when the two disagree.",
    "surgery_time_stop": "TRANSCRIBED, 24h clock. Procedure/Surgery End -- close, and the "
        "right-hand bound of Surgery Time (mins). Same page-preference and conflict flagging "
        "as Surgery Time Start.",
    "anesthesia_time_start": "TRANSCRIBED, 24h clock. Anesthesia Start, the left-hand bound "
        "of Anesthesia Time (mins). Earlier than Surgery Start, because it precedes induction.",
    "anesthesia_time_stop": "TRANSCRIBED, 24h clock. Anesthesia End, the right-hand bound of "
        "Anesthesia Time (mins). Later than Surgery End, because it includes emergence.",
    "operative_time_start": "TRANSCRIBED, 24h clock. Identical to Surgery Time Start -- "
        "operative time is the same measurement, duplicated only because the original ISHI "
        "sheet carries both columns.",
    "operative_time_stop": "TRANSCRIBED, 24h clock. Identical to Surgery Time Stop, for the "
        "same reason.",
    "or_time_mins": "DERIVED. 'Out' minus 'In' from the OR Time row -- the Anesthesia Record's "
        "own OR Time row by default, falling back to the OR Record's own In-room/Out-of-room "
        "row if the Anesthesia Record's is blank (questions_for_doc.md #2, amended 2026-08; "
        "originally OR Record only, reversed at the SL22 vet gate because the Anesthesia "
        "Record's copy was filled in more reliably). If an In was found but the Out is blank on "
        "BOTH forms, the Anesthesia End time is substituted as a last-resort Out (questions_for_"
        "doc.md #2, amended again 2026-08 at the SL19 vet gate) -- always at low confidence, "
        "always named in the flag, since this genuinely substitutes a different measurement for "
        "the missing one rather than reading the same one from its usual place. Beyond that one "
        "case, still no fallback to Surgery Time or any other window; blank means neither form's "
        "OR Time row was filled in AND no Anesthesia End was available either.",
    "anesthesia_time_mins": "DERIVED. Anesthesia End minus Anesthesia Start. Longer than "
        "surgery time, because it includes induction and emergence.",
    "surgery_time_mins": "DERIVED. Procedure/Surgery End minus Procedure/Surgery Start -- "
        "knife-to-skin to close. Cross-checked between the OR Record and the Anesthesia "
        "Record; if the two pages disagree the difference is stated in the flag.",
    "operative_time_mins": "DERIVED. Same measurement as Surgery Time (mins); kept as a "
        "separate column only because the original ISHI sheet has both.",
    "secondary_surgery_time_mins": "DERIVED. Surgery time for the second procedure, when one exists.",
    "symptom_duration_years": "How long the patient reported the problem before surgery, in "
        "years, from the H&P history.",

    # --- hernia detail (new for Sierra Leone 2025) --------------------
    "h_score": "Hernia SIZE grade H1-H4, read off the printed classification table on the "
        "H&P. Transcribed from the ticked box only -- never inferred from the operative "
        "description. Blank means the box was unmarked.",
    "hernia_f_score": "Skin-fold (F) score from the same printed classification table on "
        "the H&P. Records body habitus at the hernia site. ISHI does not routinely collect "
        "this grade on mission (questions_for_doc.md #5, answered) -- captured when the box "
        "IS marked, left blank at high confidence with no flag when it isn't, since an "
        "unmarked F box is the expected case rather than a documentation gap.",
    "inguinoscrotal_component": "a / b / c sub-grade from the classification table, "
        "describing how far the hernia extends into the scrotum. Only meaningful for H3 "
        "and H4; blank for smaller hernias.",
    "hernia_laterality": "Side of the hernia: R, L or bilateral. If the diagnosis and the "
        "operative note disagree on the side, the value is refused and flagged SIDE "
        "MISMATCH rather than resolved by guessing.",
    "hernia_reducibility": "Whether the hernia reduces on examination (reducible / "
        "irreducible / incarcerated / strangulated), from the H&P exam line.",
    "hernia_recurrent": "Primary (first repair) or recurrent (a repair of a previous "
        "repair), from the surgical history.",
    "repair_technique": "How the defect was closed -- e.g. mesh, plug/patch, primary "
        "suture repair -- from the H&P's Brief Op Note Procedure line. If the word "
        "'mesh' is found anywhere else in the packet (OR Record, Progress Note, RN "
        "Discharge, etc.) that the Procedure line itself doesn't already state, a "
        "flagged note naming where it was seen is appended so a reviewer can check "
        "the paper -- see reconcile.py's cross-record mesh check and "
        "questions_for_doc.md.",
    "defect_count": "Number of distinct fascial defects described in the operative note.",

    # --- post-op course (new for Sierra Leone 2025) -------------------
    "pacu_arrival_time": "Clock time the patient arrived in recovery, from the PACU Flowsheet.",
    "pacu_transfer_time": "Clock time the patient left recovery for the ward, from the PACU Flowsheet.",
    "pacu_los_mins": "DERIVED. PACU transfer minus PACU arrival, in minutes. A result over "
        "12 hours is flagged, because it almost always means the two times were swapped "
        "or a digit was misread rather than a genuinely day-long recovery stay.",
    "floor_arrival": "Date and time the patient reached the ward, from the Floor Flowsheet "
        "'Date/Time Arrived' box.",
    "last_entry_date": "The date on the final dated entry on the RN Discharge form, "
        "reported for transparency whether or not it ends up used. It becomes the "
        "discharge-date fallback (see Discharge Date/Time below) only when the discharge "
        "signature block itself is genuinely undated -- questions_for_doc.md #4, answered: "
        "'the date the surgeon documented' is preferred over inventing one or leaving the "
        "row blank. Never confused with a true, directly-recorded discharge time.",
    "discharge_datetime": "Date and time of discharge -- OPERATIONALIZED as: the date the "
        "patient left the ward, with a clock time where the form records one. Resolved in "
        "this order of preference: (1) RN Discharge date AND time; (2) the Floor "
        "Flowsheet's Date/Time Discharged; (3) RN Discharge date only, with the time set to "
        "00:00; (4) the Last Entry Date column above, with the time set to 00:00, when the "
        "discharge signature block is genuinely undated (questions_for_doc.md #4); (5) as a "
        "last resort the PACU transfer time, which is explicitly flagged as NOT a recorded "
        "discharge. The source used is named in the flag, so a 00:00 time is recognisable "
        "as 'no time recorded' rather than midnight.",
    "los_hours": "DERIVED, and the column to analyse. OPERATIONALIZED (questions_for_doc.md "
        "#3, answered) as: hours from OR in-room (falling back to anesthesia start, then "
        "PACU arrival, whichever is the first readable anchor on the date of surgery) to "
        "the Discharge Date/Time above. Reported to one decimal place -- minutes divided by "
        "60 -- because the source forms record clock times, not elapsed minutes directly. "
        "Unlike LOS in days this distinguishes a 4-hour stay from a 20-hour one.",
    "los": "DERIVED. Whole calendar days between the date of surgery and the discharge "
        "date -- same-day discharge is 0, not 1. Kept because the original ISHI sheet has "
        "it, but it cannot tell a 4-hour stay from a 20-hour one; prefer LOS (hours), which "
        "is the operationalized measure now used for analysis. Bounded 0-5 days like LOS "
        "(hours)'s 5-day ceiling (added 2026-08 after a date_of_surgery misread produced an "
        "unflagged 126-day value on SL19-901); also inherits a low-confidence flag whenever "
        "the date_of_surgery it's built from was itself caught as outside the mission window.",
    "max_pod_documented": "Highest post-operative day number appearing on any Progress "
        "Note. A proxy for how long the chart was followed, NOT for length of stay.",
    "discharge_criteria_met": "Y/N from the printed discharge-criteria checkboxes on the "
        "RN Discharge form (voiding, tolerating fluids, pain controlled, and so on).",

    # --- outcomes (new for Sierra Leone 2025) ------------------------
    "seen_post_op": "Whether a Post-Op Visit form exists in this packet. BLANK MEANS "
        "UNKNOWN, NOT NO -- an absent form may mean the patient was never seen, or that "
        "the page was never scanned. Never defaulted to Y or N.",
    "postop_visit_date": "Date of the documented follow-up visit, from the Post-Op Visit form.",
    "postop_visit_pod": "Post-operative day of that follow-up visit.",
    "postop_visit_findings": "Wound/clinical findings recorded at follow-up. 'CDI' = clean, "
        "dry, intact, i.e. a normal wound.",
    "postop_complication": "Any complication documented at follow-up. IMPORTANT for rate "
        "calculations: the denominator is the number of DOCUMENTED FOLLOW-UP VISITS, not "
        "the total number of cases -- patients never seen again cannot be counted as "
        "uncomplicated.",
    "further_surgery_planned": "Any additional operation recorded as planned or needed, "
        "e.g. repair of the other side at a later date.",
    "complications_notes": "Free-text complications and notes aggregated from every page in "
        "the packet, each tagged with the page it came from.",
    "post_op_comments": "Free-text post-operative comments from the progress notes and "
        "discharge paperwork.",

    # --- social history (Ghana 2026 October H&P) -----------------------
    "social_occupation": "Occupation, verbatim from the H&P social-history block.",
    "social_etoh": "Alcohol use as ticked on the H&P: Denies / Past / Current. Blank with a flag "
        "= the row was left unticked (not the same as Denies); blank with no flag = this H&P "
        "has no social-history block.",
    "social_substance_use": "Substance use as ticked on the H&P: Denies / Past / Current. Same "
        "blank rules as Alcohol.",
    "social_nicotine": "Nicotine/tobacco use as ticked on the H&P: Denies / Past / Current. Same "
        "blank rules as Alcohol. Any written qualifier (e.g. packs/day) is in the cell comment.",
    "travel_from": "Place the patient travelled from, verbatim from the H&P 'Travel from/distance' line.",
    "travel_distance": "Distance or travel time as written on the H&P 'Travel from/distance' line, "
        "WITH its unit as written (km, miles, hours). Not converted -- units are mixed on the paper.",

    # --- EuraHS-QoL (Ghana 2026 October) --------------------------------
    "qol_timepoint": "Which EuraHS-QoL questionnaire these columns hold: 'pre' (pre-operative). A "
        "post-operative questionnaire, if present, stays in the raw JSON and is noted in this "
        "cell's comment. Blank = no questionnaire in the packet.",
    "pain_rest": "EuraHS-QoL pain item: pain at the hernia site at rest (lying down). Circled 0-10 "
        "(0 = no pain, 10 = worst imaginable). Blank = no number circled, never 0.",
    "pain_activity": "EuraHS-QoL pain item: during activities (walking, biking, sports). 0-10.",
    "pain_worst_week": "EuraHS-QoL pain item: worst pain felt during the last week. 0-10.",
    "restrict_daily": "EuraHS-QoL restriction item: daily activities inside the house. 0-10 "
        "(0 = no restriction, 10 = completely restricted) or X = activity not done.",
    "restrict_outside": "EuraHS-QoL restriction item: outside the house (walking, biking, driving). "
        "0-10 or X.",
    "restrict_sports": "EuraHS-QoL restriction item: during sports. 0-10 or X.",
    "restrict_heavy_labour": "EuraHS-QoL restriction item: during heavy labour. 0-10 or X.",
    "cosmetic_abdomen_shape": "EuraHS-QoL cosmetic item: the shape of the abdomen. 0-10 "
        "(0 = very beautiful, 10 = extremely ugly).",
    "cosmetic_hernia_site": "EuraHS-QoL cosmetic item: the site of the hernia. 0-10.",
    "qol_pain_score": "DERIVED. Sum of the 3 pain items (0-30). Computed only when all 3 are numbers; "
        "otherwise blank with the reason in the comment. Higher = worse.",
    "qol_restriction_score": "DERIVED. Sum of the 4 restriction items (0-40). Computed only when all 4 "
        "are numbers -- an X (not done) or a blank leaves it blank, pending a team decision on how to "
        "score not-done items. Higher = worse.",
    "qol_cosmetic_score": "DERIVED. Sum of the 2 cosmetic items (0-20). Higher = worse.",
    "qol_total_score": "DERIVED. Pain + restriction + cosmetic scores (0-90). Blank if any domain is "
        "blank. Higher = worse quality of life.",

    # --- review bookkeeping ------------------------------------------
    "fields_flagged_count": "How many fields in THIS row the pipeline judged less than "
        "certain. A count of 0 means nothing was flagged; it does not mean the row was "
        "checked against the paper chart. Use it to triage which rows to vet first.",
}


# Fixed vocabulary used by reconcile.py's procedure categorizer and by the
# Summary sheet's per-category counts. Keep this list and the categorizer
# logic in reconcile.py in sync.
PROCEDURE_CATEGORIES = [
    "Laparoscopic Cholecystectomy",
    "Open Cholecystectomy",
    "Inguinal Hernia Repair",
    "Umbilical/Ventral Hernia Repair",
    "Femoral Hernia Repair",
    "Lipoma Excision",
    "Other Hernia Repair",
    # The 2025 OR Record prints its own tick-box list of procedures, so
    # these four are categories recorded on the paper rather than
    # interpretations of free text. Appended at the end, like the columns.
    "Hydrocelectomy",
    "Hysterectomy",
    "Prostatectomy",
    "Mass Excision",
    "Other/Uncategorized",
]

# Human-readable headers, matching the existing sheet's row-2 header text
# for the original columns; new columns get a clearly-labeled header. Kept
# free of square brackets -- Excel structured table references (e.g.
# CaseLog[Column Name]) also use brackets, so a bracket inside a header
# name breaks formulas that reference it. Full definitions of each new
# column live in GLOSSARY.md, not crammed into the header text.
CASE_LOG_HEADERS = {
    "date_of_surgery": "Date of surgery",
    "mrn": "MRN",
    "age": "Age",
    "gender": "Gender",
    "pmh_htn": "PMH: HTN",
    "pmh_dmii": "PMH: DM II",
    "pmh_copd": "PMH: COPD",
    "pmh_bph": "PMH: BPH",
    "pmh_other": "PMH: Other",
    "psh": "PSH",
    "preop_dx": "Preop DX",
    "postop_dx": "Postop Dx",
    "procedure": "Procedure",
    "secondary_procedure": "Secondary Procedure",
    "anesthetist": "Anesthetist",
    "anesthesia_type": "Anesthesia Type",
    "surgeon_assistant": "Surgeon/Assistant",
    "los": "LOS",
    "complications_notes": "Complications/Notes",
    "seen_post_op": "Seen Post Op",
    "post_op_comments": "Post Op Comments",
    "operative_time_start": "operative time Start",
    "operative_time_stop": "operative time Stop",
    "operative_time_mins": "operative time (mins)",
    "h_score": "inguinal hernia score (H score)",
    "symptom_duration_years": "symptom duration (years)",
    "or_time_start": "OR Time Start (in room)",
    "or_time_stop": "OR Time Stop (out of room)",
    "or_time_mins": "OR Time (mins)",
    "surgery_time_start": "Surgery Time Start",
    "surgery_time_stop": "Surgery Time Stop",
    "surgery_time_mins": "Surgery Time (mins)",
    "anesthesia_time_start": "Anesthesia Time Start",
    "anesthesia_time_stop": "Anesthesia Time Stop",
    "anesthesia_time_mins": "Anesthesia Time (mins)",
    "procedure_category": "Procedure Category",
    "fields_flagged_count": "Fields Flagged (count)",
    "secondary_surgery_time_mins": "Secondary Surgery Time (mins)",
    "hernia_f_score": "skin-fold score (F score)",
    "inguinoscrotal_component": "inguinoscrotal component (a/b/c)",
    "hernia_laterality": "Hernia Side",
    "hernia_reducibility": "Hernia Reducibility",
    "hernia_recurrent": "Primary or Recurrent",
    "repair_technique": "Repair Technique",
    "defect_count": "Defect Count",
    "pacu_arrival_time": "PACU Arrival",
    "pacu_transfer_time": "PACU Transfer to Floor",
    "pacu_los_mins": "PACU Time (mins)",
    "floor_arrival": "Floor Arrival",
    "last_entry_date": "Last Entry Date",
    "discharge_datetime": "Discharge Date/Time",
    "los_hours": "LOS (hours)",
    "max_pod_documented": "Last POD Documented",
    "discharge_criteria_met": "Discharge Criteria Met",
    "postop_visit_date": "Post-Op Visit Date",
    "postop_visit_pod": "Post-Op Visit POD",
    "postop_visit_findings": "Post-Op Visit Findings",
    "postop_complication": "Post-Op Complication",
    "further_surgery_planned": "Further Surgery Planned",
    "social_occupation": "Social: Occupation",
    "social_etoh": "Social: Alcohol",
    "social_substance_use": "Social: Substance use",
    "social_nicotine": "Social: Nicotine",
    "travel_from": "Social: Travel from",
    "travel_distance": "Social: Travel distance",
    "qol_timepoint": "QoL: Timepoint",
    "pain_rest": "QoL: Pain at rest",
    "pain_activity": "QoL: Pain during activity",
    "pain_worst_week": "QoL: Worst pain last week",
    "restrict_daily": "QoL: Restriction daily activities",
    "restrict_outside": "QoL: Restriction outside house",
    "restrict_sports": "QoL: Restriction sports",
    "restrict_heavy_labour": "QoL: Restriction heavy labour",
    "cosmetic_abdomen_shape": "QoL: Cosmetic abdomen shape",
    "cosmetic_hernia_site": "QoL: Cosmetic hernia site",
    "qol_pain_score": "QoL: Pain score (0-30)",
    "qol_restriction_score": "QoL: Restriction score (0-40)",
    "qol_cosmetic_score": "QoL: Cosmetic score (0-20)",
    "qol_total_score": "QoL: Total score (0-90)",
}


@dataclass
class CaseLogRow:
    """One row of the case log = one procedure. A packet with two
    procedures (e.g. an initial repair + a same-day re-exploration)
    produces two of these, linked by packet_id."""
    packet_id: str                    # e.g. "PR19-01" -- links rows from one packet
    fields: dict                      # column_name -> FieldValue

    def flagged_fields(self) -> List[str]:
        return [c for c, fv in self.fields.items() if fv.needs_review]

    def to_row_dict(self) -> dict:
        """Plain values only, for writing to the sheet / diffing."""
        return {c: fv.value for c, fv in self.fields.items()}
