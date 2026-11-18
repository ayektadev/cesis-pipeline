"""
extract.py

The REAL extraction step -- replaces raw_json_output/*.json with actual
processing once you have your key set as an environment variable.

Two-stage design, deliberately kept separate:

  Stage 1 -- Page classification: FREE. The ISHI template prints a bold
  title at the top of every page ("Anesthesia Record", "PACU Orders",
  etc.) except the H&P, whose absence of any such title IS its signature.
  We OCR just the top ~20% of the page locally (pytesseract) and fuzzy-
  match against the known list of titles. No API call, no per-page cost
  -- this is what makes classification scale for free across a full
  mission's packets.

  Stage 2 -- Field extraction: costs one Claude API call per page, only
  for page types we have a prompt for. This is the part that actually
  needs a vision model, because the content is handwritten.

Usage:
    export ANTHROPIC_API_KEY=your_key_here
    python extract.py path/to/scanned_packet.pdf

Do NOT hardcode your key in this file or paste it into chat -- the script
reads it from the environment so the key never has to live in source code.
"""

import base64
import json
import os
import re
import subprocess
import sys
import tempfile
import difflib
from pathlib import Path

import anthropic          # pip install anthropic
import pytesseract         # pip install pytesseract (+ system tesseract-ocr)
from PIL import Image
from pdf2image import convert_from_path  # pip install pdf2image (needs poppler)

MODEL = "claude-sonnet-5"

# ---------------------------------------------------------------------------
# Cost accounting.
#
# This exists because a mission run is the only part of this pipeline that
# spends money, and until now it spent it silently -- the Sierra Leone run
# cost about fourteen dollars and nobody found that out until the invoice.
# A number that only appears on a bill is a number nobody can act on.
#
# Rates are dollars per MILLION tokens and are a local guess at published
# pricing, not something the API tells us. They will drift. The estimate is
# therefore labelled an estimate everywhere it is printed, and the TOKEN
# counts beside it are exact -- if the dollars ever look wrong, trust the
# tokens and update these two numbers.
PRICE_PER_MTOK_IN = 3.00
PRICE_PER_MTOK_OUT = 15.00
# Cached input is billed at a fraction of the normal input rate on read, and
# at a premium on the call that writes the cache. Those multipliers are what
# make caching worth doing only when the same text is reused many times.
CACHE_READ_MULTIPLIER = 0.10
CACHE_WRITE_MULTIPLIER = 1.25

# Accumulated across every API call this process makes. A plain dict rather
# than a class because it gets read from several threads and never needs to
# be anything cleverer than a set of running totals.
USAGE = {"calls": 0, "input": 0, "output": 0, "cache_read": 0, "cache_write": 0}


def record_usage(resp) -> None:
    """Add one response's token counts to the running total.

    Wrapped in a try because usage accounting must never be the thing that
    breaks a run. If a future SDK renames a field, the worst acceptable
    outcome is an undercounted total; losing a page that was already paid
    for because the counter raised would be absurd.
    """
    try:
        u = resp.usage
        USAGE["calls"] += 1
        USAGE["input"] += getattr(u, "input_tokens", 0) or 0
        USAGE["output"] += getattr(u, "output_tokens", 0) or 0
        USAGE["cache_read"] += getattr(u, "cache_read_input_tokens", 0) or 0
        USAGE["cache_write"] += getattr(u, "cache_creation_input_tokens", 0) or 0
    except Exception:  # noqa: BLE001
        pass


def usage_cost() -> float:
    """Estimated dollars spent so far. See the caveat on the rates above."""
    return (
        USAGE["input"] * PRICE_PER_MTOK_IN
        + USAGE["cache_read"] * PRICE_PER_MTOK_IN * CACHE_READ_MULTIPLIER
        + USAGE["cache_write"] * PRICE_PER_MTOK_IN * CACHE_WRITE_MULTIPLIER
        + USAGE["output"] * PRICE_PER_MTOK_OUT
    ) / 1_000_000


def usage_report(packets: int = 0) -> str:
    """A few lines of plain text for the end of a run.

    Per-packet cost is the useful figure, not the total: it is the one that
    lets somebody look at a mission of 60 packets and know roughly what it
    will cost BEFORE running it, which is the whole point of measuring.
    """
    if not USAGE["calls"]:
        return "No API calls were made (every packet was already extracted)."
    cost = usage_cost()
    lines = [
        f"API usage: {USAGE['calls']} call(s), "
        f"{USAGE['input'] + USAGE['cache_read'] + USAGE['cache_write']:,} input "
        f"+ {USAGE['output']:,} output tokens",
    ]
    if USAGE["cache_read"] or USAGE["cache_write"]:
        lines.append(f"           {USAGE['cache_read']:,} of those input tokens were "
                     f"served from cache at {int(CACHE_READ_MULTIPLIER * 100)}% price")
    lines.append(f"Estimated cost: ${cost:.2f}"
                 + (f"  (~${cost / packets:.3f} per packet)" if packets else ""))
    lines.append("Estimate only -- token counts are exact, the price per token is "
                 "hard-coded in extract.py and may be out of date.")
    return "\n".join(lines)


# The API key is looked for in two places, in order:
#   1. the ANTHROPIC_API_KEY environment variable (for people comfortable
#      with terminals), or
#   2. a plain file named api_key.txt sitting next to this script (the
#      novice-friendly path -- paste the key in once and forget it).
# The key must NEVER be hardcoded in source, committed, or zipped up.
KEY_FILE = Path(__file__).resolve().parent / "api_key.txt"


def ensure_api_key() -> bool:
    """Returns True if a usable key is set (loading api_key.txt into the
    environment if needed). Placeholder text doesn't count."""
    if os.environ.get("ANTHROPIC_API_KEY", "").startswith("sk-ant"):
        return True
    if KEY_FILE.exists():
        key = KEY_FILE.read_text().strip()
        if key.startswith("sk-ant"):
            os.environ["ANTHROPIC_API_KEY"] = key
            return True
    return False

# Distinguishing keywords/phrases per page type -- NOT just the printed
# title. Titles are boxed/stylized on several ISHI forms and OCR drops or
# mangles them often enough (tested against real scans) that title-only
# matching misclassified ~20% of pages. Body vocabulary that's unique to
# each form type survives OCR much more reliably. Each is checked as a
# normalized substring (letters only, case-insensitive).
PAGE_KEYWORDS = {
    # "consentimientoinformado" added for Peru 2015: this mission's consent
    # form is entirely in Spanish ("Formulario de consentimiento informado").
    # "riesgosdeestacirugia" was already meant to cover Spanish consents, but
    # on PR15-901 OCR misread "Riesgos" as "Hiesgos" (R/H confusion on a faint
    # scan) and the page fell through to Unclassified. "consentimientoinformado"
    # is the form's own printed title, read cleanly on that same page, and is
    # a second independent anchor so a single misread word doesn't cost the
    # whole page its label.
    "Consent": ["informedconsent", "physicianobtainingconsent", "riesgosdeestacirugia",
                "surgicalanesthesiainformedconsent", "havereceivedinformationabouttheproposed",
                "consentimientoinformado"],
    # "previousanesthesia", "mouthopening" and "looseteeth" were added for the
    # same reason as the Anesthesia Record additions below: on three packets
    # (SL25-901, SL25-902, SL25-903) OCR mangled "Mallampati" -- it is printed in
    # small type next to a diagram -- and the page fell through to
    # Unclassified. All three are airway-history rows that no other form in
    # the packet carries, so they are features of THIS form rather than of
    # this mission. Verified against all 715 cached pages: 3 recovered, all
    # previously Unclassified, 0 pages that already had a label changed.
    #
    # "preoperativediagnosis" and "proposedprocedure" also tested clean here
    # and were deliberately NOT adopted: they are generic enough to appear on
    # an H&P or OR Record of some future mission's forms, and the three
    # keywords above already recover every affected page without them.
    "Pre-Anesthesia Evaluation": ["mallampati", "proposedoperationprocedure", "anesthesiaplangeneral",
                                  "preanesthesiaassessment", "preprocedurevitalsigns",
                                  "postanesthesiacarenotes", "preanesthesiaevaluation",
                                  "previousanesthesia", "mouthopening", "looseteeth"],
    # The keywords on the second line below were added after an offline
    # audit of all 715 pages found the 2025 Anesthesia Record was being
    # recognised on only 32 of 66 packets: 26 of the 42 unclassified pages
    # in the mission sat at the Anesthesia Record's slot in the packet.
    # The cause is that this form's title is printed hard against the left
    # margin and is bold/boxed, so on a slightly skewed scan it is either
    # cropped or mangled and "anesthesiarecord" never appears in the OCR
    # text -- and none of the original keywords were features of the 2025
    # layout. The additions are drawn from printed option rows that only an
    # anesthesia record has (oxygen delivery, GA maintenance, regional
    # technique, eye care, temperature probe site, ETCO2), chosen for being
    # distinctive to the FORM rather than merely rare in this one mission.
    # Verified across every cached page: they recovered 32 pages, all of
    # them previously Unclassified, and changed the label of no page that
    # was already classified. See tune_keywords.py.
    # "trainoffour" added for Peru 2014: PR14-901's Anesthesia Record page (a
    # dense, skewed scan) mangled the boxed title to "ENESTHESIA RECORD"
    # (losing the leading A) and none of the keywords above happened to
    # survive that scan either, so the page scored 0 for Anesthesia Record.
    # It did NOT fall through to Unclassified, though -- it scored 1 for OR
    # Record, because this form's own "MDA/CRNA [TIME IN/TIME OUT]" provider
    # hand-off row OCR'd cleanly enough to trip OR Record's "timeout"
    # keyword (added for a genuinely different field, the WHO surgical
    # safety checklist's "Time Out" step). With no competing Anesthesia
    # Record score, OR Record won outright -- not even a tie for
    # PAGE_TYPE_PRIORITY to break. "Train of Four" is the printed name of
    # the neuromuscular-blockade monitoring row on this form, read cleanly
    # on the same scan ("TRAIN OF FOUR:"), and is anesthesia-charting
    # jargon specific enough that it has no business appearing on any other
    # ISHI page type. Confirmed against every other Anesthesia Record page
    # cached from this and the Sierra Leone 2022 mission (which happens to
    # still have images on disk): present on essentially every genuine
    # Anesthesia Record page checked, and never once on a PACU, Floor,
    # Progress Note, RN Discharge, H&P, Consent, Pre-Anesthesia Evaluation,
    # or OR Record page. With this keyword added, PR14-901's page now scores
    # 1-1 with OR Record, and PAGE_TYPE_PRIORITY (which ranks Anesthesia
    # Record above OR Record) breaks the tie correctly.
    "Anesthesia Record": ["intubatuion", "eventsposposcheck", "fio2analyzer",
                          "anesthesiarecord", "airwaymanagement", "anesthetictechnique",
                          "periopmedications", "postoppainnerveblock", "monitorsandequipment",
                          "simplemask", "nasalcannula", "gamaintenance", "epiduralcse",
                          "ivsedation", "careawaketaped", "nervestim", "etcopresent",
                          "skintympanictemporal", "trainoffour"],
    "OR Record": ["operatingroomrecord", "suturesused", "countcorrect", "timeout",
                  "metzenbaumscissor", "armynavy", "spongestick"],
    "PACU Orders": ["painregimen", "floorphase", "pacuphase",
                    "pacuorders", "admittoishiward", "hlivwhentoleratingpo"],
    # "timetransferredto" is a deliberate TRUNCATION of the previous
    # "timetransferredtofloor", not an addition alongside it. On SL25-904 the
    # word "floor" at the end of that printed row was clipped by the scan
    # edge, and one missing word cost the whole page its label. The two must
    # not both be listed: every page containing the long form contains the
    # short one, so keeping both would score that one printed row twice and
    # silently bias the tie-break toward PACU Flowsheet.
    "PACU Flowsheet": ["pacuflowsheet", "timearrivedinpacu", "tapreceived",
                       "arrivaltimeinpacu", "timetransferredto", "medicationsgiveninpacu"],
    # Same treatment, same reason: on SL25-905 and SL25-906 OCR rendered
    # "Date/Time Arrived" as "Datefrine Anived" and the title as "FLOOR
    # FLOWSHFFT". Truncating each keyword to the part that survives leaves
    # the row still recognisable without adding a second scoring token.
    # Second line added for Ghana 2026 (October): this mission's Floor
    # Flowsheet is a newer revision whose header reads "Arrival time to
    # Floor:" (not "Date/Time Arrived"), and whose stylised title OCRs as
    # garbage ("flrfloyheet"). With none of the original three keywords
    # surviving, GH26O-901 p16 scored 1 for Post-Op Visit ("vaginalbleeding",
    # printed in this form's GYN row too) and was extracted as a follow-up
    # visit dated the day of surgery -- a fabricated follow-up. These five
    # are printed row labels of the new floor form only (verified absent from
    # every other page type in the GH26O vet sample).
    "Floor Flowsheet": ["floorflowsh", "datetimearriv", "datetimedisch",
                        "arrivaltimetofloor", "floornurseassessment", "vsonarrivaltofloor",
                        "anyopioidsrequiredpostop", "incisionsiteinspected"],
    "Progress Note": ["progressnotes", "aaox", "woundslocal", "tolerating",
                      "postoperativeday", "advancediet", "oobambulate", "dischargetohome"],
    "RN Discharge": ["rnnotesordersanddischarge", "dischargecriteria", "mdnamesignature",
                     "rnnotesordersdischarge", "dischargeorders", "nursingnotes",
                     "rnverifying", "voidingspontaneously"],
    # NEW for Sierra Leone 2025: an outpatient follow-up clinic visit form.
    # It shares a lot of vocabulary with Progress Notes (POD#, s/p, AAOx3,
    # "Tolerating"), so it is matched on phrases unique to the follow-up
    # context and is given priority ABOVE Progress Note below.
    "Post-Op Visit": ["ishipostopvisit", "headachespinalrelated", "returntosexualactivity",
                      "donotsubmergeinwater", "vaginalbleeding", "otherabnormalfindings",
                      "noheavyliftingforweeks"],
    # "pertinentfamilyhistory" and "radiographiclaboratorystudies" added for
    # Peru 2015: minor-procedure packets (e.g. PR15-901) use a "Surgical
    # Consultation" template instead of the standard "History, Physical,
    # Brief Op Note" page -- same clinical role (it's the H&P-equivalent
    # intake note for that packet), but its printed section headers
    # ("Chief Complaint", "Social History", "Pertinent Family History",
    # "Physical Exam", "Radiographic/Laboratory Studies", "Recommendations")
    # didn't match any existing H&P keyword and the page fell through to
    # Unclassified. Both additions read cleanly off PR15-901's OCR text and
    # are distinctive to this form -- no other page type in PAGE_KEYWORDS
    # uses either phrase.
    "H&P": ["historyandphysical", "briefopnote", "currentpainlevel", "attendingsurgeon",
            "historyphysicalbriefopnote", "clinicalclassificationforinguinalhernia",
            "durationofsymptoms", "subscapularskinfold", "plannedoperation",
            "preopchecklisttimeoutor", "pertinentfamilyhistory", "radiographiclaboratorystudies"],
    # NEW for Ghana 2026 (October): the patient-completed EuraHS-QoL hernia
    # quality-of-life questionnaire (European Hernia Society). It is usually
    # scanned ROTATED 90 degrees, so it only becomes readable after
    # _autorotate_for_ocr() (see process_packet). "operativequestionnaire"
    # is deliberately the shared tail of "Preoperative Questionnaire" and
    # "Postoperative Questionnaire" (and survives the dropped leading "P" seen
    # on GH26O-902: "reoperativequestionnaire"), so a post-op version of the
    # form is caught too -- the prompt then reports which one it is.
    "EuraHS-QoL": ["eurahsqol", "operativequestionnaire", "worstpainfeltduringthelastweek",
                   "cosmeticdiscomfort", "theshapeofyourabdomen", "duringheavylabour"],
}
# Try page types in this order when scores tie -- most-specific forms
# first. "Post-Op Visit" sits ABOVE "Progress Note" because it is the
# more specific of the two overlapping post-operative forms; putting it
# below would let a follow-up visit be absorbed as a Progress Note and
# silently satisfy the "Seen Post Op" test (RULES.md rule 4).
PAGE_TYPE_PRIORITY = [
    "EuraHS-QoL", "Consent", "Pre-Anesthesia Evaluation", "Anesthesia Record", "OR Record",
    "PACU Orders", "PACU Flowsheet", "Floor Flowsheet", "Post-Op Visit",
    "Progress Note", "RN Discharge", "H&P",
]

# A small set of page types carry a printed title distinctive enough --
# a single long, concatenated phrase -- that seeing it at all should decide
# the page outright, rather than being counted as one vote among several.
# Added after the Sierra Leone 2022 vet gate: SL22-901's page 11 IS the
# "ISHI Post-Op Visit" form, and "ishipostopvisit" (already in Post-Op
# Visit's own keyword list above) DID match -- but the rest of that scan
# was blurry enough that it also picked up 2 Progress Note keywords ("aaox",
# "tolerating", vocabulary the two forms genuinely share), and Progress
# Note's count won OUTRIGHT (2 vs 2 would have gone to Post-Op Visit via
# PAGE_TYPE_PRIORITY below, but this wasn't a tie on the actual scan).
# "ishipostopvisit" is a 15-character concatenation of the boxed title --
# not the kind of string tesseract produces by accident on an unrelated
# form -- so it is treated as decisive.
STRONG_TITLE_KEYWORDS = {
    "Post-Op Visit": ["ishipostopvisit"],
    # Added after the Peru 2016 vet-5 gate: this mission's short, unnumbered
    # "intake/pre-op sign-up slip" substitutes for the full H&P page in
    # several packets and carries real Identity-field data (Age/Gender/
    # Procedure/Anesthesia-type/Surgery-date), but it turns out to come in
    # TWO printed variants:
    #  1) PR16-901/PR16-902 style: top intake block PLUS a "Pre-op Check
    #     List" / "Time Out (OR)" checklist section below it. That
    #     checklist header contains the substring "timeout" -- already an
    #     OR Record keyword above -- so the page was scoring >=1 for OR
    #     Record and, via PAGE_TYPE_PRIORITY (which ranks OR Record well
    #     above H&P), losing that tie outright.
    #  2) PR16-903/PR16-904 style: intake block ONLY, no checklist section at
    #     all (bottom two-thirds of the page is blank) -- this variant
    #     doesn't contain "timeout" so it wasn't being mis-scored as OR
    #     Record, but it also doesn't hit MIN_CLASSIFY_SCORE for anything,
    #     so it fell through to "Unclassified" instead.
    # Both variants silently discarded the slip's real Identity data.
    # "preopchecklisttimeoutor" (the concatenation of the two adjacent
    # checklist headers, confirmed absent from a genuine OR Record page's
    # OCR text) catches variant 1. "surgerydate" (the intake block's own
    # "Surgery Date:" row label, common to both variants and not printed
    # on any other ISHI form page in this pipeline) catches variant 2 too.
    # Deliberately kept short rather than concatenated with the day-of-week
    # checkboxes that follow it: on PR16-903 tesseract dropped "Wed" and
    # "Morning" from that row entirely, which would have broken a longer,
    # more fragile concatenated match. Both are safe to treat as decisive
    # for H&P, the same way "ishipostopvisit" is decisive for Post-Op
    # Visit above.
    "H&P": ["preopchecklisttimeoutor", "surgerydate"],
    # The printed form name, and the questionnaire's title tail -- neither
    # is plausible OCR noise on any other ISHI page (Ghana 2026 October).
    "EuraHS-QoL": ["eurahsqol", "operativequestionnaire"],
}

# Minimum keyword hits before a page is called anything at all. A page
# that matches nothing is labelled "Unclassified", NOT H&P. The old
# behaviour (fall back to H&P) is precisely the failure mode RULES.md
# rule 13 documents: four Peru packets had junk pages absorbed as a
# second H&P, which then wrongly triggered rule 1's secondary-procedure
# handling and invented a re-operation that never happened. An
# unclassified page is visible and harmless; a fabricated H&P is not.
MIN_CLASSIFY_SCORE = 1

# Seconds allowed for ONE local OCR attempt on ONE page. Measured on this
# mission's scans with the machine otherwise idle: typical page 1-2s, worst
# legitimate page 10.6s. 40 is roughly 4x that worst case -- enough headroom
# that no real page is cut off, tight enough that a pathological scan cannot
# stall a 715-page run.
#
# Note that tesseract parallelises internally (it will happily use every
# core), so page classification should be run SINGLE-threaded. Running two
# OCR processes side by side on a 4-core box pushed 1.5s pages to 40s --
# slower in total, not faster. See classify_page_free.
OCR_TIMEOUT_SECS = 40

# ---------------------------------------------------------------------------
# Extraction integrity rules (RULES.md 9-15, condensed for the model that
# reads each page). These are UNIVERSAL -- they apply to every mission.
# Mission-specific facts (date window, roster, quirks) come from the
# mission profile JSON in missions/ and get appended at call time.
# ---------------------------------------------------------------------------
INTEGRITY_RULES = """
CRITICAL EXTRACTION RULES (violations of these caused real data errors):
1. Transcribe ONLY what is visibly handwritten in the specifically labeled
   box. Never infer, pattern-fill, or "complete" a set of values. Never
   assume Anesthesia Start = OR Time In or copy a nearby box into an
   empty one.
2. If a box is EMPTY, overwritten, or illegible, output:
   {"value": null, "confidence": "low", "flag_reason": "<box name> blank/unclear on form -- left blank"}
   A null with a note is ALWAYS better than a guessed value. Do NOT
   produce a plausible-looking time for an empty box under any
   circumstances.
3. Read handwritten digits digit-by-digit. Sloppy open 4s look like 5s or
   3s. If a digit is ambiguous, use "medium" or "low" confidence and name
   the plausible alternative readings in flag_reason (e.g. "reads 1338,
   could be 1538").
4. "high" confidence is reserved for clearly legible values you are sure
   of. Anything sloppy or partially obscured is "medium" at best.
5. flag_reason must state what the scan LITERALLY reads and which box it
   came from, so a reviewer can audit without reopening the scan.
6. Names (surgeon, anesthesiologist, CRNA) come only from the actual
   name/signature line. Never invent a name; if illegible, use null + flag.
7. Some forms capture staff and findings as PRINTED options with tick
   boxes or circles rather than as handwriting. For those, report the
   option that is actually marked, exactly as printed. If nothing is
   marked, that is null + flag -- do not pick the most common option. If
   more than one is marked, report all of them and say so in flag_reason.
   Never mark an option because it seems clinically likely.
8. When the same fact appears in two places on one page (for example a
   hernia class circled in a printed table AND written out in a note),
   report what each place says and, if they disagree, say so explicitly in
   flag_reason instead of silently choosing one.
"""

# ---------------------------------------------------------------------------
# FORM LAYOUTS.
#
# The ISHI paper forms were REDESIGNED between the 2019 Peru mission and the
# 2025 missions, and the two versions put the timing data in genuinely
# different boxes. This is the single most dangerous kind of drift in this
# pipeline: describing the wrong layout does not produce obvious garbage, it
# produces plausible, well-formatted times read out of the wrong box, on
# every packet, silently. So the layout is chosen by the mission profile's
# "form_template" key instead of being hardcoded, and old missions keep
# their old description.
# ---------------------------------------------------------------------------
LAYOUT_TOKEN = "<<FORM_LAYOUT>>"

FORM_LAYOUTS = {
    "ISHI-2019": """
EXACT BOX LAYOUT OF THIS FORM TEMPLATE (do not confuse adjacent boxes):
- Anesthesia Record: header has Date / OR # / Concurrent CRNA / Page #.
  Row "Anesthesia" has THREE boxes: Start | Ready | End. The middle
  "Ready" box is NOT Surgery Start and NOT OR Time In -- never report it
  as either. Row "OR Time" has TWO boxes: In | Out. Row "Surgery" has
  Start | End plus the Surgeon line. CRNA/Anesthesiologist name lines are
  near the header/signature area.
- OR Record: DATE box; row "OR Time" = In room | Out of room; row
  "Anesthesia" = Start | End; row "Procedure" = Start | End.
Chronology sanity: In-room <= Anesthesia Start <= Surgery Start <
Surgery End <= Anesthesia End <= Out-of-room. A reversal usually means
you read the wrong box -- re-check before answering, and if still
ambiguous transcribe as written with a flag rather than "fixing" it.
""",
    "ISHI-2025": """
EXACT BOX LAYOUT OF THIS FORM TEMPLATE (do not confuse adjacent boxes):

- Anesthesia Record: the ONLY timing on this form is a small grid at the
  TOP RIGHT of the header with exactly two columns, START and STOP, and
  exactly two rows, "Anesthesia" and "Procedure". That is four times in
  total. There is NO "Ready" box and NO "OR Time" row anywhere on this
  form -- if you believe you see one, you are looking at the OR Record or
  at a neighbouring page overlapping the edge of the scan. Times here are
  written as bare 4-digit 24h numbers (e.g. 0937, 1059). The rest of the
  header carries NAME, MRN, Diagnosis, Procedure, DATE, OR #, Surgeon and
  Anesthetist; anesthetic technique is a checkbox block (General /
  Regional / Spinal / Epidural / MAC-IV sedation).

- OR Record: the DATE is written as three separate slots, MONTH , DAY ,
  YEAR. There are three timing ROWS, each with two labelled boxes:
      "OR Time:"     In room -  |  Out of room -
      "Anesthesia:"  Start      |  End
      "Procedure:"   Start      |  End
  plus one standalone "Time Out - Time:", which is the surgical safety
  checklist timeout. "Time Out" is NOT "Out of room" -- they are different
  fields in different places and must never be reported as each other.
  "Out of room" is often left blank on this mission. Blank is null; it is
  never a reason to substitute the Anesthesia End time.

- PACU Flowsheet: its own Date box, plus "Arrival time in PACU" and "Time
  Transferred to Floor/Discharged".
- Floor Flowsheet: "Date/Time Arrived" and "Date/Time Discharged".

Chronology sanity: In-room <= Anesthesia Start <= Time Out <= Procedure
Start < Procedure End <= Anesthesia End <= Out-of-room <= PACU arrival <=
PACU transfer <= Floor arrival <= Floor discharge. A reversal usually means
you read the wrong box -- re-check before answering, and if it still reads
that way, transcribe it as written with a flag rather than "fixing" it.

IMPORTANT: small disagreements BETWEEN forms are real and expected -- the
Anesthesia Record and the OR Record are filled in by different people and
routinely differ by a few minutes. Report each form's own numbers exactly
as that form has them. Never reconcile two forms against each other; that
is done downstream by a step that can show its working.
""",
}
DEFAULT_FORM_TEMPLATE = "ISHI-2019"


def timing_layout(mission: dict | None) -> str:
    """Which paper form template this mission used. Defaults to the 2019
    layout so already-calibrated older missions are unaffected."""
    key = (mission or {}).get("form_template") or DEFAULT_FORM_TEMPLATE
    return FORM_LAYOUTS.get(key, FORM_LAYOUTS[DEFAULT_FORM_TEMPLATE])

FIELD_SHAPE = '{"value": <...or null>, "confidence": "high"|"medium"|"low", "flag_reason": <string or null>}'

# One prompt per page type we actually extract fields from. Extend this
# dict as you add richer (Tier 2) fields.
PAGE_PROMPTS = {
    "H&P": f"""You are extracting structured data from a scanned, handwritten
History & Physical / Brief Operative Note form from a surgical mission trip.
Return ONLY valid JSON with this shape (every field uses {FIELD_SHAPE}):
{{
  "age": {{...}},
  "gender": {{...}},
  "preop_dx": {{...}},
  "postop_dx": {{...}},
  "procedure": {{...}},
  "case_duration_mins": {{...}},
  "symptom_duration_years": {{...}},
  "current_pain_level": {{...}},
  "pmh_htn": {{...}},
  "pmh_dmii": {{...}},
  "pmh_copd": {{...}},
  "pmh_bph": {{...}},
  "pmh_other": {{...}},
  "psh": {{...}},
  "attending_surgeon": {{...}},
  "surgeon_assistant": {{...}},
  "anesthesiologist": {{...}},
  "anesthesia_planned": {{...}},
  "ebl_complications": {{...}},
  "condition_postop": {{...}},
  "planned_operation": {{...}},
  "hernia_class_h": {{...}},
  "hernia_class_f": {{...}},
  "inguinoscrotal_component": {{...}},
  "hernia_laterality": {{...}},
  "hernia_reducibility": {{...}},
  "hernia_recurrent": {{...}},
  "repair_technique": {{...}},
  "defect_count": {{...}},
  "date": {{...}},
  "mesh_mentioned": {{...}},
  "occupation": {{...}},
  "etoh": {{...}},
  "substance_use": {{...}},
  "nicotine": {{...}},
  "travel_from": {{...}},
  "travel_distance": {{...}}
}}

Where to find each of the less obvious fields:
- "occupation", "etoh", "substance_use", "nicotine", "travel_from",
  "travel_distance": a SOCIAL HISTORY block printed on newer H&P revisions,
  in the left column below Allergies: "Occupation: ____", then three
  checkbox rows "ETOH: [] Denies [] Past [] Current", "Substance use: []
  Denies [] Past [] Current", "Nicotine: [] Denies [] Past [] Current",
  then "Travel from/distance: ____".
  For etoh / substance_use / nicotine report exactly the ticked option as
  printed ("Denies", "Past" or "Current"). Nothing ticked = null with
  flag_reason "no option ticked" -- never assume Denies. More than one
  ticked = report both, joined with "/", and flag it. Any handwritten
  qualifier beside the row (e.g. "10 cig/day", "quit 2015") goes in
  flag_reason verbatim.
  "occupation" is the handwritten text verbatim.
  "travel_from" is the place name written on the Travel line, verbatim.
  "travel_distance" is the distance or travel time written there, verbatim
  WITH its unit exactly as written (e.g. "45 km", "3 hrs", "2h by bus"); if
  only a place is written with no distance, travel_distance is null with
  flag_reason "place only, no distance written".
  If this page has no social-history block at all (an older H&P layout),
  report all six null with flag_reason "no social history block on this
  form template".
- "attending_surgeon": the "Attending Surgeon:" line lists printed initials
  (e.g. DHL, AG, AK, FA, ZS). Report the one that is CIRCLED or ticked, as
  initials. The separate "Surgeon/Assistant:" line inside the Brief Op Note
  is handwritten full names -- that one goes in "surgeon_assistant",
  verbatim, as written (e.g. "SMITH/JONES").
- "pmh_htn" / "pmh_dmii" / "pmh_copd" / "pmh_bph": look for a printed PMH
  checkbox row -- printed (typeset) text reading "PMH:" followed by four
  printed condition labels HTN, DMII, COPD, BPH, each preceded by a small
  printed box glyph (a hollow square, or a small circle) to tick. It sits
  in the left-hand "History and Physical" column, directly below the
  "Current Pain level (0-10):" line and directly above the "PSH:" line --
  look at that specific strip of the page before deciding anything about
  this field, do not skim the whole page and conclude absence from a
  glance.
  THIS ROW IS PRINTED ON THE FORM ITSELF, THE SAME WAY "Age:" AND
  "Gender:" ARE -- it is part of the template, not something that varies
  by patient. If the page has a "PSH:" line and a "Meds:" line in the
  usual place (i.e. this is the standard ISHI H&P layout), assume the PMH
  checkbox row is printed immediately above "PSH:" and read that exact
  strip carefully -- do not report "no checkbox row" just because every
  box in it happens to be unticked. An unticked box is still a printed
  box: the row is PRESENT (report "N" for that box), not absent. Only
  report the row as absent (null with flag_reason "no PMH checkbox row on
  this form template" for all four) if you have specifically looked at
  the strip between "Current Pain level" and "PSH:" and there is no "PMH:"
  label and no box glyphs printed there at all -- e.g. a genuinely
  different/older form layout with no PMH line of any kind. This should
  be rare; if you find yourself about to report "no checkbox row" on a
  page that otherwise looks like the standard ISHI H&P layout, look again
  before committing to that answer.
  When the row IS present: report "Y" for each box that is ticked (X,
  checkmark, filled box, or circled) and "N" for each box left unticked --
  every one of the four fields gets a Y or N, never null, because a form
  that prints this row is a complete checklist and a blank box is a
  negative, not a skip. Do not confuse this checkbox row with the CC/HPI
  narrative above it, which is a separate field this schema does not
  capture.
- "pmh_other": the checkbox row above has a second line directly under it,
  "[] Other: ______". Report the handwritten text on that line ONLY if the
  Other box itself is ticked -- if there is handwritten text on the Other
  line but its own checkbox is NOT ticked, report null with flag_reason
  quoting the unticked text verbatim (e.g. "unticked Other line reads
  'None'") so a reviewer can check it, rather than trusting it outright.
  Null with no flag if the Other line is blank. Like the four boxes above,
  this only applies when the page prints this row at all.
- "psh": Past Surgical History, from the form's own "PSH:" line, directly
  below the PMH checkbox row (this is a plain printed label with no
  checkbox of its own, just a blank line to write on). Report the
  handwritten text verbatim ONLY when it is an actual surgical history
  entry (e.g. "C-section x2", "Appy 2019", "Hernia repair 2018"). If the
  line instead contains a null/negative indicator -- an "Ø" (circle-slash),
  a plain slash or dash, "n/a", "none", "nil", or similar -- report VALUE
  AS NULL (do not transcribe the symbol or word itself into the value),
  but ALWAYS attach flag_reason quoting exactly what was written (e.g.
  "PSH line reads 'Ø' (none/no prior surgery)") so a reviewer can confirm
  it was genuinely a none-indicator and not an illegible entry. Null with
  NO flag only if the line is completely blank (nothing written at all).
  Null with flag_reason "PSH line illegible" if something is written but
  cannot be read with reasonable confidence. Never infer PSH from the
  CC/HPI narrative or from Meds. Do not confuse this with the PMH row
  above it; PSH is prior OPERATIONS, PMH is medical conditions.
  Some form layouts do not print a "PSH:" line on the H&P page at all (a
  genuinely different/older template, the same way some layouts lack the
  PMH checkbox row). Only after specifically checking the strip below the
  PMH row/pain level line and confirming there is no "PSH:" label printed
  anywhere on this page, report null with flag_reason "no PSH line on this
  form template". This should be rare -- do not report it just because the
  line is blank or hard to find at a glance.
- "anesthesiologist": from the Brief Op Note "Anesthesia/anesthesiologist:"
  line, verbatim.
- "anesthesia_planned": the ticked box under "Anesthesia planned" (General /
  Spinal / Regional / Local +- sedation).
- "planned_operation": the ticked options in the "Planned Operation" block
  at the bottom left, INCLUDING the side box (Inguinal Hernia Repair - L / R
  / B/L, Ventral/Umbilical Hernia Repair, Hysterectomy, Prostatectomy,
  Hydrocelectomy - L / R / B/L, Mass Excision - Location). Report the ticked
  text, e.g. "Inguinal Hernia Repair - R".
- "hernia_class_h": the H grade (H1, H2, H3 or H4) for THIS patient. It can
  appear in two places: circled in the printed "Clinical classification for
  inguinal hernia" table on the lower right, and/or written on the "Post op
  Dx/Operative Findings/Hernia Class:" line. If both are present and they
  disagree, report the value written on the Hernia Class line and state the
  conflict in flag_reason. Value must be exactly one of "H1","H2","H3","H4"
  or null. Non-inguinal cases have no H grade -- that is null with
  flag_reason "not an inguinal hernia case", NOT a guess.
- "hernia_class_f": the subscapular skin-fold grade, exactly one of
  "F1","F2","F3","F4" or null. Only report it if a specific F box is circled
  or an F value is written. The printed table lists F1<=15mm, F2=15-25mm,
  F3=26-35mm, F4>35mm; do NOT derive a grade from a millimetre measurement
  you found elsewhere, and do NOT assume F follows H.
- "inguinoscrotal_component": only applies to H3/H4. Exactly "a", "b" or "c"
  from the printed sub-table (a = <10 cm, b = 10-20 cm, c = >20 cm), or null.
- "hernia_laterality": "R", "L" or "Bilateral", taken from the Diagnosis and
  Planned Operation lines. If the CC/HPI says bilateral but only one side is
  operated on, report the OPERATED side here and note the bilateral finding
  in flag_reason.
- "hernia_reducibility": what the Physical Exam / classification actually
  says, e.g. "reducible", "partially reducible", "irreducible". Verbatim
  phrasing is fine. Null if the exam does not address it.
- "hernia_recurrent": "recurrent" only if the form explicitly says recurrent
  or mentions a prior repair on that side (check PSH too); "primary" if the
  form explicitly indicates a first repair; otherwise null with a flag. Do
  not infer recurrence from age or hernia size.
- "repair_technique": the technique words written on the Procedure line of
  the Brief Op Note, e.g. "plug/patch", "mesh", "Lichtenstein", "tissue
  repair". Verbatim, null if not stated.
- "defect_count": an integer, only if the operative findings state a number
  of defects (e.g. "small defects x 2" -> 2). Null otherwise. Never infer a
  count from the word "hernias" being plural.
- "mesh_mentioned": the word "mesh" (or a specific mesh product/brand name)
  can appear ANYWHERE on this page, not just on the Procedure line that
  "repair_technique" is scoped to -- e.g. in the CC/HPI, PMH, PSH, or a
  margin note. Scan the WHOLE page and report the exact phrase verbatim if
  it appears anywhere, even outside the fields listed above. Null if the
  word does not appear anywhere on the page.

Use "low" confidence for anything illegible, ambiguous, or contradicted
elsewhere on the page. Never guess silently -- if you are not sure, say so
in flag_reason rather than picking the most likely value with high confidence.
If the age has been crossed out and corrected, report the correction and
name the struck-out original in flag_reason.
""",
    "Anesthesia Record": f"""You are extracting timing and anesthetic data from
a scanned, handwritten Anesthesia Record from a surgical mission trip.
{LAYOUT_TOKEN}
Return ONLY valid JSON with this shape (every field uses {FIELD_SHAPE}):
{{
  "date": {{...}},
  "anesthesia_start": {{...}},
  "anesthesia_end": {{...}},
  "procedure_start": {{...}},
  "procedure_end": {{...}},
  "or_time_in": {{...}},
  "or_time_out": {{...}},
  "anesthetist": {{...}},
  "surgeon": {{...}},
  "anesthetic_technique": {{...}},
  "asa_class": {{...}},
  "mesh_mentioned": {{...}}
}}
Times as HH:MM 24h, converted from the bare 4-digit numbers written in the
START/STOP grid (0937 -> "09:37"), and from no other part of the page: the
long drug/vitals chart down the middle of this form has its own time axis
which is NOT a source for these four fields.
"or_time_in" and "or_time_out" come from THIS form's own "OR Time" row --
on the ISHI-2019 layout that is a separate row (In | Out) from the
"Anesthesia" row (Start | Ready | End) and the "Surgery" row (Start | End);
never take these two from the Anesthesia or Surgery rows instead. On the
ISHI-2025 layout this form has NO "OR Time" row at all -- if the layout
description above doesn't mention one, both fields are null with flag_reason
"no OR Time row on this form template", not a guess from elsewhere on the
page.
"anesthetic_technique" is the ticked option in the ANESTHETIC TECHNIQUE
block (General / Regional / Spinal / Epidural / CSE / MAC-IV sedation).
"anesthetist" and "surgeon" come from the header lines of the same names.
An empty box is null + flag, never a guessed time.
"mesh_mentioned": scan the WHOLE page for the word "mesh" (or a specific
mesh product/brand name) -- e.g. in the AGENTS/periop-medications freetext,
the EVENTS margin notes, or anywhere else -- and report the exact phrase
verbatim if found. Null if the word does not appear anywhere on the page.
""",
    "OR Record": f"""You are extracting timing, staffing and procedure data from
a scanned, handwritten Operating Room Record from a surgical mission trip.
{LAYOUT_TOKEN}
Return ONLY valid JSON with this shape (every field uses {FIELD_SHAPE}):
{{
  "date": {{...}},
  "in_room": {{...}},
  "out_of_room": {{...}},
  "anesthesia_start": {{...}},
  "anesthesia_end": {{...}},
  "procedure_start": {{...}},
  "procedure_end": {{...}},
  "time_out": {{...}},
  "surgeon": {{...}},
  "assistant": {{...}},
  "anesthesiologist": {{...}},
  "anesthesia_type": {{...}},
  "position": {{...}},
  "surgical_procedure": {{...}},
  "procedure_laterality": {{...}},
  "count_correct": {{...}},
  "sutures_used": {{...}},
  "mesh_mentioned": {{...}},
  "mesh_product": {{...}},
  "mesh_ref": {{...}},
  "mesh_lot": {{...}},
  "mesh_expiry": {{...}}
}}
"mesh_product" / "mesh_ref" / "mesh_lot" / "mesh_expiry": some OR Records
carry a PRINTED implant sticker (e.g. "Bard(R) Mesh" with REF, LOT and
dates, a QR/barcode block) stuck onto the page, usually near the bottom.
Read these only from that printed sticker: "mesh_product" is the product
name as printed (e.g. "Bard Mesh", "Bard Mesh PerFix Plug"); "mesh_ref" the
number beside "REF"; "mesh_lot" the code beside "LOT"; "mesh_expiry" the
use-by date (the date beside the hourglass symbol, or labelled EXP/use by)
-- NOT the manufacture date (the date beside the factory symbol). Copy
printed characters exactly; this is implant traceability data. No sticker =
all four null with no flag. A sticker that is partly cut off or illegible =
null + flag for the unreadable parts. Never take these from handwriting, and
these sticker dates are never the surgery date.
Times as HH:MM 24h exactly as written in each labelled box. in_room and
out_of_room come from the "OR Time" row only; anesthesia_start/end from the
"Anesthesia" row only; procedure_start/end from the "Procedure" row only.
These are three separate rows -- never substitute one row's value for
another's, and never report "Time Out" as any of them.
"surgeon", "assistant" and "anesthesiologist" are PRINTED INITIALS with tick
boxes, each with an "Other:" write-in line. The exact initials printed on
this mission's form are NOT fixed across missions -- they vary from site to
site, and the real set (when already known) is supplied in the MISSION
CONTEXT block above, never assumed or hardcoded here. Report the initials of
whichever option is actually ticked, exactly as printed on THIS page, or the
handwritten text on the Other line if that is what is filled instead.
Nothing ticked and nothing written = null + flag.
"surgical_procedure" is the ticked option in the Surgical Procedure row (IHR,
VHR/UHR, Hysterectomy, Prostatectomy, Hydrocele repair, Mass Excision, Other)
and "procedure_laterality" is the R or L box ticked beside it ("R", "L",
"Bilateral", or null if the procedure has no side).
"count_correct" is "Y", "N" or null from the Count Correct box.
An empty box is null + flag, never a guessed value.
"mesh_mentioned": scan the WHOLE page for the word "mesh" (or a specific
mesh product/brand name) -- this can be ticked under "Implant/Special
Equipment?", written on the "Other:" line, or noted in the COUNT/supplies
section -- and report the exact phrase verbatim if found anywhere on the
page. Null if the word does not appear anywhere.
""",
    "Pre-Anesthesia Evaluation": f"""You are extracting data from a scanned,
handwritten Pre-Anesthesia Assessment from a surgical mission trip.
Return ONLY valid JSON with this shape (every field uses {FIELD_SHAPE}):
{{
  "asa_class": {{...}},
  "anesthesia_plan": {{...}},
  "age_gender_hpi": {{...}},
  "preop_diagnosis": {{...}},
  "date": {{...}},
  "pmh_htn": {{...}},
  "pmh_dmii": {{...}},
  "pmh_copd": {{...}},
  "pmh_bph": {{...}},
  "pmh_other": {{...}},
  "psh": {{...}},
  "mesh_mentioned": {{...}}
}}
"age_gender_hpi" is the age and gender exactly as written on the HPI line
(e.g. "60 y/o F") -- transcribe it verbatim; it is used to cross-check the
H&P's age, so do NOT normalize or correct it.
"date": transcribe the digit groups exactly as written and say in
flag_reason which order they appear in, e.g. "written 7-12-25". Do not
reorder or reformat it -- this form is sometimes completed by local staff
using a different date convention from the rest of the packet, and the
downstream step needs the raw reading to detect that.

"pmh_htn" / "pmh_dmii" / "pmh_copd" / "pmh_bph" / "pmh_other": THIS PAGE'S
OWN medical-history area is NOT the same layout as the H&P's PMH checkbox
row. It is a "PMH & Systems Review" (or similarly labelled) table organized
by body system -- rows typically along the lines of Cardiovascular,
Pulmonary, Endocrine, Renal, Hepatic, Other -- each with a generic mark
(X, checkmark, circle) and sometimes a handwritten condition name or
abbreviation next to/inside that row.
These fields exist on THIS page so that the packet still has SOME PMH
signal on the rare packets where the H&P page itself reports "no PMH
checkbox row on this form template". Fill them in whenever this page's
Systems Review table is present and legible, regardless of whether the
H&P also had its own row -- the reconciliation step (not you) decides
which source to prefer.
Rules for reading the table:
- A bare mark (X, checkmark, circle) in a row with NO condition name or
  abbreviation written is NOT enough evidence for any of the four named
  fields -- do not guess which specific condition it means. It also does
  NOT get reported into "pmh_other" (a bare mark is not a "something
  else", it's ambiguous ROS shorthand that could mean either "positive,
  unspecified" or "reviewed, normal" depending on the author's
  convention). Leave the field null in that case; do not attach a flag
  unless you want to note the bare mark for a reviewer's awareness.
- If a row has an actual condition name/abbreviation written (e.g.
  "HTN", "HTN x10yrs", "DM2", "DM", "COPD", "BPH", "prostate/BPH") that
  matches one of the four clean fields, report "Y" for that field. Match
  on meaning, not exact spelling (e.g. "high BP" -> HTN Y, "diabetes" or
  "sugar" -> DM II Y). The other three of the four clean fields stay null
  UNLESS they are separately addressed elsewhere in the table (do not
  infer "N" for the others just because you found one match -- unlike
  the H&P's checklist, this table is not a fixed 4-box form, so absence
  of mention is not the same as a documented negative).
- If a row has a specific condition name written that does NOT match any
  of the four clean fields (e.g. "asthma", "seizure disorder", "sickle
  cell", "thyroid disease", "prior TB"), report that condition verbatim
  in "pmh_other", combined across rows with a semicolon if more than one
  (e.g. "Pulmonary: asthma; Renal: CKD"). Include which system row it
  came from if it's not obvious from the text itself.
- If the whole table is blank/unmarked, report all five fields null with
  no flag. If the table is present but illegible, null with flag_reason
  "PMH & Systems Review table illegible".
- If this page does not have a Systems Review table of this kind at all
  (a different Pre-Anesthesia form layout), report all five fields null
  with flag_reason "no PMH & Systems Review table on this form template".

"psh": this page sometimes has its own "PSH:" entry (separate from, and
sometimes worded slightly differently than, the H&P's own PSH line -- for
example directly beside a "PAH:"/planned-anesthesia checkbox row). Apply
the exact same transcription rules as the H&P's "psh" field: verbatim
text for an actual surgical history entry; value null but flag_reason
quoting the symbol/word verbatim for a null-indicator ("Ø", slash, "n/a",
"none", "nil", etc); null with no flag if genuinely blank; null with
flag_reason "PSH line illegible" if unreadable; null with flag_reason "no
PSH line on this form template" if this page has no PSH entry at all.
This field exists here so the packet still has a PSH signal on packets
where the H&P's own "PSH:" line is genuinely absent -- fill it in
whenever legible regardless of what the H&P page has.

"mesh_mentioned": scan the WHOLE page for the word "mesh" (or a specific
mesh product/brand name) and report the exact phrase verbatim if found
anywhere. Null if the word does not appear anywhere on the page.
""",
    "Progress Note": f"""You are extracting data from a scanned, handwritten
Progress Note from a surgical mission trip.
Return ONLY valid JSON with this shape (every field uses {FIELD_SHAPE}):
{{
  "pod": {{...}},
  "date_time": {{...}},
  "s_p": {{...}},
  "pain_control": {{...}},
  "tolerating": {{...}},
  "wound_findings": {{...}},
  "complications": {{...}},
  "disposition": {{...}},
  "mesh_mentioned": {{...}}
}}
"pod" is the integer after "Post-operative day #".
"wound_findings" is the ticked/circled wound description (CDI, serosang
staining, saturated, hematoma, induration, drainage, purulent) plus any
handwritten note beside it.
"complications": only what the note actually documents as a problem. A note
that documents a normal course has null here, NOT "none" invented by you.
One page may contain SEVERAL dated progress entries; if so, report the LAST
(highest POD) entry in these fields and summarise the earlier ones in the
flag_reason of "pod".
If the page is a blank/unfilled template with no POD number or content
written in, return null values with flag_reason "Progress Note page present
but unfilled". Do not invent content.
"mesh_mentioned": scan the WHOLE page for the word "mesh" (or a specific
mesh product/brand name) and report the exact phrase verbatim if found
anywhere. Null if the word does not appear anywhere on the page.
""",
    "Post-Op Visit": f"""You are extracting data from a scanned, handwritten
ISHI Post-Op Visit form -- an outpatient follow-up visit after the patient
has already been discharged. Do not confuse it with an inpatient Progress
Note: this one is titled "ISHI Post-Op Visit" and has a Follow Up block at
the bottom.
Return ONLY valid JSON with this shape (every field uses {FIELD_SHAPE}):
{{
  "visit_date": {{...}},
  "visit_time": {{...}},
  "pod": {{...}},
  "s_p_procedure": {{...}},
  "pain_control": {{...}},
  "tolerating": {{...}},
  "wound_findings": {{...}},
  "complication": {{...}},
  "other_abnormal_findings": {{...}},
  "diet": {{...}},
  "activity_restrictions": {{...}},
  "followup_plan": {{...}},
  "further_surgery_planned": {{...}},
  "mesh_mentioned": {{...}}
}}
Where things are:
- "pod" is the integer on the "POD#" line; "s_p_procedure" is what follows
  "s/p" on the same line (e.g. "(R) IHR").
- "wound_findings": the ticked options on the "Wounds:" line (CDI, serosang
  staining, saturated, hematoma, induration, drainage, purulent).
- "complication": a short description of any abnormality documented anywhere
  on this page, including in the free-text Wound Care / Assessment-Plan
  writing (e.g. "possible seroma/hematoma, expected to reabsorb"). If the
  visit documents an entirely normal recovery, this is null with
  flag_reason "no complication documented" -- do not write "none".
- "further_surgery_planned": any note that another operation is needed or
  planned, including handwriting in the page margins (e.g. a starred note
  that a contralateral repair is needed next year). Null if absent.
- "mesh_mentioned": scan the WHOLE page for the word "mesh" (or a specific
  mesh product/brand name) and report the exact phrase verbatim if found
  anywhere, even outside the fields listed above. Null if the word does not
  appear anywhere on the page.
An unticked box or an empty line is null + flag, never an assumed normal.
""",
    "PACU Flowsheet": f"""You are extracting data from a scanned, handwritten
PACU (recovery room) Flowsheet from a surgical mission trip.
{LAYOUT_TOKEN}
Return ONLY valid JSON with this shape (every field uses {FIELD_SHAPE}):
{{
  "date": {{...}},
  "pacu_arrival_time": {{...}},
  "pacu_transfer_time": {{...}},
  "handoff_received_from": {{...}},
  "pacu_complications": {{...}},
  "mesh_mentioned": {{...}}
}}
"pacu_arrival_time" is the "Arrival time in PACU" box and
"pacu_transfer_time" is the "Time Transferred to Floor/Discharged" box.
Times as HH:MM 24h. Do NOT read times off the vital-signs grid for these
two fields -- the grid records observations, not arrival or transfer.
"pacu_complications": only events actually written down (e.g. desaturation,
vomiting, reintubation). Null if none are documented.
"mesh_mentioned": scan the WHOLE page for the word "mesh" (or a specific
mesh product/brand name) and report the exact phrase verbatim if found
anywhere. Null if the word does not appear anywhere on the page.
""",
    "Floor Flowsheet": f"""You are extracting data from a scanned, handwritten
Floor (ward) Flowsheet from a surgical mission trip.
{LAYOUT_TOKEN}
Return ONLY valid JSON with this shape (every field uses {FIELD_SHAPE}):
{{
  "floor_arrival": {{...}},
  "floor_discharge": {{...}},
  "floor_complications": {{...}},
  "mesh_mentioned": {{...}}
}}
"floor_arrival" comes from "Date/Time Arrived" and "floor_discharge" from
"Date/Time Discharged". Report each as the date and time together in the
order written, e.g. "12/8 12:35". If only part is filled in, report the part
that is there and flag the missing part. Blank slashes with nothing written
between them are null, not today's date.
"mesh_mentioned": scan the WHOLE page for the word "mesh" (or a specific
mesh product/brand name) and report the exact phrase verbatim if found
anywhere. Null if the word does not appear anywhere on the page.
""",
    "RN Discharge": f"""You are extracting data from a scanned, handwritten
"RN Notes, Orders, Discharge" form from a surgical mission trip.
Return ONLY valid JSON with this shape (every field uses {FIELD_SHAPE}):
{{
  "discharge_date": {{...}},
  "discharge_time": {{...}},
  "followup_appointment_date": {{...}},
  "followup_appointment_time": {{...}},
  "discharge_criteria_met": {{...}},
  "discharge_orders": {{...}},
  "nursing_complications": {{...}},
  "last_entry_date": {{...}},
  "mesh_mentioned": {{...}}
}}

CRITICAL -- WHICH DATE IS THE DISCHARGE DATE.
This form has TWO dates on it and they mean opposite things. Read this
before filling either field.

The "Discharge Orders" block contains a printed line reading
"Discharge to home [ ] Follow up on ______ at ______".
The date written on that line is the date of a FUTURE OUTPATIENT CLINIC
APPOINTMENT. It is NOT the date the patient left the ward. It is usually
several days later, and on a mission it is often the SAME date for many
patients at once, because the team runs one follow-up clinic day.
Put that date in "followup_appointment_date" and that time in
"followup_appointment_time". NEVER put it in "discharge_date".

The actual discharge date/time is the "Date/Time:" written on the
signature lines at the BOTTOM of the form, next to "MD Name/Signature"
and "RN verifying". Prefer the MD Name/Signature Date/Time. If only the
RN verifying line is dated, use that and say so in the reason.

Sanity check before you answer: the discharge date can not be earlier
than the surgery, and on this kind of mission it is nearly always the
same day or the next day. If the date you are about to put in
"discharge_date" is a week later AND it matches the "Follow up on" line,
you have read the wrong line -- go back and read the signature block.
If the signature block is genuinely undated, return null for
"discharge_date" with a reason saying so. Null is the correct answer
there; a follow-up date in its place is not.

"discharge_criteria_met": "Y" if the printed discharge criteria are all
ticked, "N" if any is explicitly not met, null if the block is untouched.
"last_entry_date" is the date on the final dated nursing entry, which is
used as a fallback when the discharge date box itself is blank -- report it
separately, never copy it into "discharge_date".
"nursing_complications": events actually documented. Null if none.
"mesh_mentioned": scan the WHOLE page for the word "mesh" (or a specific
mesh product/brand name) and report the exact phrase verbatim if found
anywhere. Null if the word does not appear anywhere on the page.
""",
    # NEW for Ghana 2026 (October). Patient-reported, so the "transcribe
    # only what is marked" rule matters even more than usual: an unmarked
    # row is a missing answer, never a zero.
    "EuraHS-QoL": f"""You are extracting a scanned, patient-completed EuraHS-QoL
hernia quality-of-life questionnaire (European Hernia Society) from a surgical
mission packet. The image may still be rotated; read it in whatever
orientation makes the printed text upright.
Return ONLY valid JSON with this shape (every field uses {FIELD_SHAPE}):
{{
  "form_timepoint": {{...}},
  "date_of_birth": {{...}},
  "approximate_age": {{...}},
  "sex": {{...}},
  "pain_rest": {{...}},
  "pain_activity": {{...}},
  "pain_worst_week": {{...}},
  "restrict_daily": {{...}},
  "restrict_outside": {{...}},
  "restrict_sports": {{...}},
  "restrict_heavy_labour": {{...}},
  "cosmetic_abdomen_shape": {{...}},
  "cosmetic_hernia_site": {{...}},
  "unassigned_marks": {{...}}
}}
The form has three printed sections. Each item row prints the numbers 0 to
10 and the patient circles ONE number:
- "Pain at the site of the hernia": rows "In rest (lying down)" ->
  pain_rest; "During activities (walking, biking, sports)" -> pain_activity;
  "Worst pain felt during the last week" -> pain_worst_week.
- "Restrictions of activities because of pain or discomfort at the site of
  the hernia": rows "Daily activities (inside the house)" -> restrict_daily;
  "Outside the house (walking, biking, driving)" -> restrict_outside;
  "During sports" -> restrict_sports; "During heavy labour" ->
  restrict_heavy_labour. These rows also have a "Not done" column with a
  printed X; if the patient circled/marked that X, report the string "X".
- "Cosmetic discomfort": rows "The shape of your abdomen" ->
  cosmetic_abdomen_shape; "The site of the hernia" -> cosmetic_hernia_site.
Item values: an integer 0-10 (the circled number), or "X" (Not done), or
null if the row has no mark. Do NOT treat an unmarked row as 0. If two
numbers are circled on one row, report null and name both in flag_reason.
Match each circle to its row carefully -- rows are narrow and the scan may
be skewed; a circle that sits between two rows is medium/low confidence
with the alternative row named in flag_reason.
"unassigned_marks": each section starts with a SCALE/HEADER row (e.g. "0 =
no pain ... 10 = worst pain imaginable") that is not a question. If a
number is circled on a header row, or a mark cannot be tied to any item
row, describe each such mark here verbatim (e.g. "8 circled on the Pain
section header row"). Never move such a mark into an item. Null if none.
"form_timepoint": "pre" if the printed title says "Preoperative
Questionnaire", "post" if it says "Postoperative" (or a follow-up/post-op
version), null + flag if the title is not legible.
"date_of_birth": exactly as written, digit groups in the order written
(e.g. "24/9/1964"), with the order noted in flag_reason. "approximate_age":
the "If unknown, approximate age" box, verbatim. "sex": "M" or "F" from the
ticked box.
""",
    # TODO: "PACU Orders" and "Consent" are classified but not extracted --
    # they carry no case-log fields today. Add prompts here if the record
    # ever needs medication orders or consent-signature auditing.
}


# ---------------------------------------------------------------------------
# Mission profile support -- everything mission-specific (date window,
# roster, site quirks) is injected from missions/<mission>.json at call
# time, never hardcoded here. See missions/_TEMPLATE_new_mission.json.
# ---------------------------------------------------------------------------
def load_mission(profile_path: str) -> dict:
    with open(profile_path, encoding="utf-8") as f:
        return json.load(f)


def real_date_window(mission: dict) -> tuple[str, str] | None:
    """The date window only counts if it holds actual dates -- placeholder
    text ('FILL-IN...') means the profile hasn't been calibrated yet and
    must NOT be injected into prompts."""
    win = mission.get("date_window") or []
    if len(win) == 2 and all(isinstance(d, str) and any(c.isdigit() for c in d) for d in win):
        return win[0], win[1]
    return None


def mission_context(mission: dict | None) -> str:
    """Builds the mission-specific prompt block. Uncalibrated pieces
    (placeholder dates, empty roster) are simply omitted -- the first vet
    run works without them, auto-calibrates the profile from what it
    reads (see run_mission.py), and later runs get the full context.
    Never inject placeholder text into a prompt."""
    if not mission:
        return ""
    parts = [f"\nMISSION CONTEXT ({mission.get('mission_name', 'unknown mission')}):"]

    window = real_date_window(mission)
    if window:
        parts.append(f"""- All surgeries took place between {window[0]} and {window[1]}. A date you
  read outside this window is almost certainly a digit misread -- re-examine
  it, and if it still clearly reads out-of-window, transcribe it as written
  with a flag_reason noting it falls outside the mission window. NEVER
  change a date to fit the window.""")

    roster = mission.get("roster", {})
    roster_txt = "; ".join(
        f"{role}: {', '.join(names)}" for role, names in roster.items() if names
    )
    if roster_txt:
        parts.append(f"""- Known staff (sanity check ONLY, never force a match): {roster_txt}.
  If a name you read matches none of these, transcribe exactly what is
  written at medium/low confidence and flag it -- do NOT substitute a
  roster name.""")

    parts.extend(f"- {h}" for h in mission.get("extraction_hints", []))
    if len(parts) == 1:
        return ""  # nothing calibrated yet -- no mission block at all
    return "\n".join(parts) + "\n"


def _normalize(s: str) -> str:
    return re.sub(r"[^a-z]", "", s.lower())


def classify_page_free(image_path: str) -> str:
    """Stage 1: classify a page's form type via LOCAL OCR. No API call --
    this is meant to run at zero marginal cost across a full mission's
    worth of packets.

    Uses the FULL page, not just a top crop: the boxed form titles
    ("Anesthesia Record", "Pre-Anesthesia Evaluation") don't always land
    in the same vertical position across scans (skew, varying margins,
    the ISHI logo taking up different amounts of space), so a fixed crop
    window missed real headers on some packets during testing. OCR'ing
    the whole page costs a bit more CPU time but no more API cost, and
    is far more robust.

    Bounded time. A typical page here OCRs in about 1.5 seconds, but a
    handful of noisy scans send tesseract's layout analysis into a hole
    and burn MINUTES of CPU on a single page. Across a mission that is
    the difference between twenty minutes and most of a day, and it fails
    in the worst way: no error, just a run that appears to have hung. So
    each attempt is capped, and a page that blows the cap is retried once
    at half resolution -- which is plenty for a large printed form title
    and is usually what unsticks it. If even that times out the page
    comes back "Unclassified", which is honest and visible, rather than
    stalling the run or being quietly mislabelled."""
    ocr_text = ""
    for max_edge, budget in ((None, OCR_TIMEOUT_SECS), (800, OCR_TIMEOUT_SECS)):
        try:
            with Image.open(image_path) as img:
                if max_edge is not None:
                    img = img.convert("L")
                    w, h = img.size
                    if max(w, h) > max_edge:
                        s = max_edge / max(w, h)
                        img = img.resize((int(w * s), int(h * s)), Image.LANCZOS)
                ocr_text = pytesseract.image_to_string(img, timeout=budget)
            break
        except (RuntimeError, pytesseract.TesseractError):
            # RuntimeError is what pytesseract raises when its own timeout
            # fires. Fall through to the reduced-resolution retry; if that
            # one raises too, ocr_text stays empty and the scoring below
            # returns "Unclassified" on its own.
            continue

    norm_text = _normalize(ocr_text)

    # Decisive titles win outright, before the general vote-counting below
    # ever runs -- see STRONG_TITLE_KEYWORDS' docstring-comment for why.
    for ptype, kws in STRONG_TITLE_KEYWORDS.items():
        if any(kw in norm_text for kw in kws):
            return ptype

    scores = {ptype: sum(1 for kw in kws if kw in norm_text) for ptype, kws in PAGE_KEYWORDS.items()}
    best_score = max(scores.values())
    if best_score < MIN_CLASSIFY_SCORE:
        # Nothing matched. This used to fall back to "H&P" on the theory
        # that H&P is the only form without a distinct boxed title. That
        # was wrong and it is exactly RULES.md rule 13's failure mode: a
        # blank page, a scanned consent back, or an OCR failure got
        # labelled H&P, reconcile.py saw a SECOND H&P in the packet, and
        # rule 1's secondary-procedure handling fired on four Peru
        # packets that had no second procedure. An unclassified page is
        # visible and harmless; a fabricated H&P is not.
        return "Unclassified"
    for ptype in PAGE_TYPE_PRIORITY:
        if scores[ptype] == best_score:
            return ptype
    return max(scores, key=scores.get)


# The known vocabulary of ISHI form page types. Anything the vision fallback
# below says that is NOT one of these (or is spelled differently) is treated
# as "did not match" rather than coerced into a nearest guess.
CLASSIFY_TYPES = list(PAGE_KEYWORDS.keys())

CLASSIFY_FALLBACK_PROMPT = (
    "This is one page from a scanned ISHI surgical mission packet. Local OCR "
    "could not confidently identify which of the packet's known forms this "
    "is -- usually because the page is a dense handwritten grid whose scan "
    "quality defeats plain text recognition.\n\n"
    "Look at the page image and decide which ONE of the following ISHI form "
    "types it is:\n"
    + "\n".join(f"- {t}" for t in CLASSIFY_TYPES)
    + "\n\nReply with ONLY the exact form name from the list above, nothing "
    "else -- no punctuation, no explanation.\n"
    "If the page genuinely does not match any of these forms (for example a "
    "blank page, a product sticker, or a pre-op scheduling slip that predates "
    "the case record), reply with exactly: Unclassified"
)


def classify_page_vision(image_path: str) -> str:
    """Stage 1 fallback: costs one short, cheap API call, and ONLY runs for
    the minority of pages where classify_page_free's free local OCR failed
    outright.

    Added after the Sierra Leone 2023 vet gate found the Anesthesia Record
    page classified as Unclassified on 4 of 5 vetted packets (and
    Pre-Anesthesia Evaluation on 1 of 5): these are dense handwritten-grid
    forms whose scan quality for this mission produced near-empty OCR text,
    not merely a missing keyword -- several tesseract page-segmentation
    modes and contrast tweaks were tried at the vet stage and none recovered
    usable text. Losing the Anesthesia Record silently is a real gap (it is
    where the anesthetist name / anesthesia type / vitals timeline live), so
    rather than accept that gap or invent a positional guess (which RULES.md
    rule 13 warns against -- a wrong guess here is worse than an honest
    blank), a second, more capable pair of eyes gets to read the page
    directly: the same vision model that already reads these forms for
    extraction, which does not depend on tesseract recognizing a title.

    Deliberately conservative and mission-agnostic (this lives here, not in
    a mission profile, because any mission's scans can hit this same OCR
    failure mode): if the model itself is not confident the page matches one
    of the known forms, it is told to answer "Unclassified" -- the same
    honest, visible non-guess classify_page_free falls back to. This
    function never invents structure; it only escalates the SAME question
    ("what form is this?") to a reader that isn't defeated by bad OCR."""
    client = anthropic.Anthropic()
    with open(image_path, "rb") as f:
        img_b64 = base64.standard_b64encode(f.read()).decode()
    media_type = "image/jpeg" if Path(image_path).suffix.lower() in (".jpg", ".jpeg") else "image/png"
    resp = client.messages.create(
        model=MODEL,
        max_tokens=20,
        thinking={"type": "disabled"},
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": img_b64}},
            {"type": "text", "text": CLASSIFY_FALLBACK_PROMPT},
        ]}],
    )
    record_usage(resp)
    text = next((b.text for b in resp.content if getattr(b, "type", None) == "text"), "") or ""
    answer = text.strip()
    # Exact match required -- no fuzzy matching. A reply that didn't come
    # back clean should fall through to Unclassified, not be coerced into
    # the nearest-looking label.
    for ptype in CLASSIFY_TYPES:
        if answer == ptype:
            return ptype
    return "Unclassified"


def _json_from_reply(text: str) -> dict:
    """Pull the JSON object out of a model reply that may have prose around it.

    Asking for "JSON only" gets JSON only most of the time. It does not get
    it every time, and on a 715-page run "most of the time" is dozens of
    crashes. On the very first Anesthesia Record of this mission the model
    reasoned aloud about an ambiguous START/STOP grid for two paragraphs and
    THEN produced a correct fenced JSON object -- a good answer that a bare
    json.loads threw away.

    The old approach (strip a leading ``` and a trailing ```) only worked
    when the fence was the entire reply. This looks for the JSON instead of
    assuming its position:

      1. a ```json fenced block, which is where a well-behaved reply puts it
      2. any ``` fenced block
      3. failing both, the first balanced JSON object anywhere in the reply

    Step 3 uses raw_decode rather than a regex because a regex cannot match
    balanced braces, and these payloads nest ({"value": ..., ...} per field).
    Deliberately NOT tolerated: trailing commas, single quotes, unquoted
    keys. Those are signs the reply is genuinely malformed, and quietly
    repairing malformed clinical data is exactly the kind of silent
    guesswork this pipeline is built to avoid -- better to raise and look.
    """
    text = text.strip()

    fenced = re.search(r"```(?:json)?\s*\n(.*?)\n?```", text, re.DOTALL)
    if fenced:
        return json.loads(fenced.group(1).strip())

    start = text.find("{")
    if start == -1:
        raise json.JSONDecodeError("no '{' anywhere in the reply", text or "", 0)
    # raw_decode stops cleanly at the end of the first complete object and
    # ignores whatever trails it, so a model that appends a closing remark
    # after valid JSON is handled too.
    obj, _ = json.JSONDecoder().raw_decode(text, start)
    return obj


def extract_page(image_path: str, page_type: str, mission: dict | None = None) -> dict:
    """Stage 2: costs one API call. Only runs for page types we have a
    prompt for; everything else is recorded with an empty fields dict so
    reconcile.py knows the page existed even before Tier 2 prompts exist.

    The prompt sent = page prompt + universal integrity rules (RULES.md
    9-15 condensed) + mission-specific context from the profile JSON."""
    if page_type not in PAGE_PROMPTS:
        return {"page_type": page_type, "fields": {}, "note": "no extraction prompt defined yet for this page type"}

    full_prompt = (PAGE_PROMPTS[page_type].replace(LAYOUT_TOKEN, timing_layout(mission))
                   + INTEGRITY_RULES + mission_context(mission))
    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from env
    with open(image_path, "rb") as f:
        img_b64 = base64.standard_b64encode(f.read()).decode()
    media_type = "image/jpeg" if Path(image_path).suffix.lower() in (".jpg", ".jpeg") else "image/png"

    # The image goes FIRST and the text SECOND, which is what the prompts were
    # tuned against, so that order is not changed here.
    #
    # The text block is marked cacheable. Roughly 1,100 of its tokens -- the
    # integrity rules and the mission context -- are byte-identical on every
    # single call of the run, and the page prompt is identical across all
    # calls of that page type. Caching them means paying full price once and
    # a tenth of the price on every repeat.
    #
    # Two honest limits on how much this saves. The image is unique per page
    # and cannot be cached, and it is the larger half of the input, so this
    # trims the input bill rather than halving it. And a cache entry expires
    # after a few minutes of disuse, so it pays off during a continuous run
    # and does nothing for a page extracted on its own.
    text_block = {"type": "text", "text": full_prompt,
                  "cache_control": {"type": "ephemeral"}}

    def _call(blocks):
        return client.messages.create(
            model=MODEL,
            # 4000, not 2000. This is a ceiling, not a purchase -- you are
            # billed for the tokens actually produced -- so the only thing a
            # tight limit buys is a truncated reply. A dense OR Record can
            # run long.
            max_tokens=4000,
            # Thinking OFF, explicitly. This model decides for itself whether
            # to think, and on these prompts it decided to: the first real run
            # spent its entire budget on a thinking block and returned no
            # answer at all. Leaving it to adaptive behaviour makes both the
            # cost and the token ceiling unpredictable across 700-odd pages.
            # Off is also the mode this pipeline's prompts and confidence
            # rules were tuned under.
            #
            # If extraction accuracy on the messiest handwriting turns out to
            # need it, the lever is {"type": "enabled", "budget_tokens": N}
            # with max_tokens raised well above N -- but measure at the vet
            # gate first rather than paying for it across the whole mission
            # on faith.
            thinking={"type": "disabled"},
            messages=[{"role": "user", "content": blocks}],
        )

    image_block = {"type": "image",
                   "source": {"type": "base64", "media_type": media_type, "data": img_b64}}

    # Malformed JSON from the model (an unescaped quote inside a
    # flag_reason string, an odd truncation, etc.) is a formatting
    # accident, not an ambiguity in the clinical data -- so asking the
    # model again is not "silently repairing" anything covered by rule 18;
    # it is just retrying a request that came back garbled, the same as
    # the cache retry just above. Bounded at 3 tries total so a page that
    # is genuinely and repeatedly broken still surfaces as a hard failure
    # rather than looping forever or burning unbounded API spend.
    last_exc = None
    for attempt in range(1, 4):
        try:
            resp = _call([image_block, text_block])
        except Exception as exc:  # noqa: BLE001
            # Caching is a billing optimisation, not a feature of the answer. If
            # the installed SDK or the endpoint rejects cache_control, the right
            # response is to pay full price and get the extraction, not to fail
            # the run over a discount. Retried exactly once, and only for errors
            # that name the cache -- a genuine network or auth failure must still
            # surface as itself rather than being retried into a confusing shape.
            if "cache" not in str(exc).lower():
                raise
            print(f"    (prompt caching unavailable, continuing without it: {exc})")
            resp = _call([image_block, {"type": "text", "text": full_prompt}])

        record_usage(resp)
        # Take the first TEXT block, not content[0]. The model may return
        # thinking blocks ahead of its answer, and this code used to assume
        # block zero was the answer -- which crashed the run on its very first
        # page with an AttributeError about ThinkingBlock, after the call had
        # already been paid for. Selecting by block type is correct whether or
        # not thinking is on, so it cannot break again if that setting changes.
        text = next((b.text for b in resp.content if getattr(b, "type", None) == "text"), None)
        if text is None:
            last_exc = RuntimeError(
                f"{Path(image_path).name}: model returned no text block "
                f"(stop_reason={resp.stop_reason}, blocks={[getattr(b, 'type', '?') for b in resp.content]})")
            print(f"    (attempt {attempt}/3: {last_exc}; retrying)")
            continue
        try:
            fields = _json_from_reply(text)
        except json.JSONDecodeError as e:
            # The usual cause is a response cut off at max_tokens, which leaves
            # syntactically broken JSON. Say so plainly -- a bare "Expecting
            # ',' delimiter: line 1 column 1999" sends you looking at the
            # prompt when the answer is simply that the reply did not fit.
            hint = ("  The response hit max_tokens and was truncated; raise max_tokens."
                    if resp.stop_reason == "max_tokens" else "")
            last_exc = RuntimeError(
                f"{Path(image_path).name} ({page_type}): model reply was not valid JSON: {e}.{hint}\n"
                f"--- reply begins ---\n{text[:600]}\n--- ends ---")
            if attempt < 3:
                print(f"    (attempt {attempt}/3: malformed JSON reply; retrying)")
            continue
        return {"page_type": page_type, "fields": fields}
    raise last_exc


# Long edge, in pixels, of the page images we build and send.
#
# 1568 is not arbitrary: it is the longest edge the vision API keeps before
# it downsamples the image itself. Anything larger is uploaded, paid for in
# latency, and then thrown away.
#
# Rendering these packets at 300 DPI (what this used to do) was actively
# harmful. Each PDF page here is a single embedded scan of about 1700x2250
# px sitting on a page box of the same size in points -- i.e. the file is
# already 1:1 at 72 DPI. Asking poppler for 300 DPI therefore upscaled an
# existing bitmap 4.2x, adding no information at all, at roughly 28 seconds
# per page. Pulling the embedded image out directly and shrinking it takes
# about 5 seconds per page and is strictly closer to the original scan.
MAX_EDGE = 1568
FALLBACK_RENDER_DPI = 200  # only used for PDFs that are not scans


def page_images(pdf_path: str, out_dir: Path) -> list[Path]:
    """Produce one JPEG per PDF page in out_dir and return them in page
    order. Re-uses whatever is already there, so an interrupted run (or a
    separately pre-built cache) costs nothing to resume."""
    out_dir.mkdir(parents=True, exist_ok=True)
    existing = sorted(out_dir.glob("page_*.jpg"))
    if existing and (out_dir / "done.txt").exists():
        return existing

    def _save(im: Image.Image, dest: Path) -> None:
        im = im.convert("RGB")
        w, h = im.size
        if max(w, h) > MAX_EDGE:
            s = MAX_EDGE / max(w, h)
            im = im.resize((int(w * s), int(h * s)), Image.LANCZOS)
        im.save(dest, "JPEG", quality=88)

    made: list[Path] = []
    try:
        with tempfile.TemporaryDirectory() as td:
            # -p writes the SOURCE PAGE NUMBER into each filename. Without
            # it, a page carrying two embedded images (a logo plus the
            # scan) shifts every subsequent page's index by one and every
            # later page gets extracted under the wrong page number --
            # silently, and only on some packets.
            subprocess.run(["pdfimages", "-p", "-png", pdf_path, f"{td}/x"],
                           check=True, capture_output=True)
            by_page: dict[int, list[Path]] = {}
            for f in sorted(Path(td).glob("x-*.png")):
                by_page.setdefault(int(f.stem.split("-")[1]), []).append(f)
            for page in sorted(by_page):
                # Largest file on the page is the scan; the small ones are
                # the ISHI logo and other page furniture.
                best = max(by_page[page], key=lambda p: p.stat().st_size)
                dest = out_dir / f"page_{page:02d}.jpg"
                with Image.open(best) as im:
                    _save(im, dest)
                made.append(dest)
    except Exception:
        made = []

    if not made:
        # Not a scanned PDF (or no poppler pdfimages): fall back to
        # rendering. Correct, just slower.
        for i, page_img in enumerate(convert_from_path(pdf_path, dpi=FALLBACK_RENDER_DPI), 1):
            dest = out_dir / f"page_{i:02d}.jpg"
            _save(page_img, dest)
            made.append(dest)

    (out_dir / "done.txt").write_text(str(len(made)))
    return made


def _autorotate_for_ocr(img_path: Path) -> str:
    """Free (local) second chance for a page that classified as nothing:
    ask tesseract's orientation detector (OSD, `--psm 0`) whether the page
    is turned, and if so re-classify an upright copy.

    Only ever called on pages that already came back "Unclassified", so a
    correctly-oriented page is never touched. The rotation is written back
    over the cached page image ONLY if the upright copy then classifies as a
    known form -- that is the evidence the rotation was right (OSD on a page
    that is mostly handwriting reports low confidence and is sometimes
    wrong; a rotation that produces no recognisable form is discarded).
    Persisting it means the vision extraction that follows also sees the
    page upright. Returns the new page type, or "Unclassified".

    Needs tesseract's 'osd' language data (`tesseract --list-langs` shows
    'osd'; on a Mac it ships with `brew install tesseract`). If it is
    missing, this quietly returns "Unclassified" and the vision fallback
    still gets its turn."""
    try:
        with Image.open(img_path) as im:
            osd = pytesseract.image_to_osd(im, timeout=OCR_TIMEOUT_SECS)
            m = re.search(r"Rotate:\s*(\d+)", osd)
            rotate = int(m.group(1)) if m else 0
            if rotate not in (90, 180, 270):
                return "Unclassified"
            # OSD's "Rotate" is the clockwise turn needed; PIL rotates
            # counter-clockwise for positive angles.
            upright = im.rotate(-rotate, expand=True).convert("RGB")
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td) / "upright.jpg"
            upright.save(tmp, "JPEG", quality=88)
            new_type = classify_page_free(str(tmp))
        if new_type != "Unclassified":
            upright.save(img_path, "JPEG", quality=88)
        return new_type
    except Exception:  # noqa: BLE001 -- OSD failure is never fatal
        return "Unclassified"


_EURAHS_UPRIGHT_WORDS = ["eurahsqol", "operativequestionnaire", "inrestlyingdown",
                         "duringactivities", "worstpainfelt", "dailyactivities",
                         "outsidethehouse", "duringsports", "duringheavylabour",
                         "theshapeofyourabdomen", "thesiteofthehernia", "cosmeticdiscomfort"]


def ensure_landscape_upright(img_path: Path) -> int:
    """The EuraHS-QoL form is printed LANDSCAPE. Found on the Ghana 2026
    October full run: tesseract can often read sideways text well enough to
    CLASSIFY the page, so _autorotate_for_ocr() never ran, and 29 of 50
    questionnaires reached the vision model still on their side. Sideways,
    the model confuses the rows (items) with the columns (0-10) -- GH26O-903
    came back with 5 of 9 items wrong. So every page classified as
    EuraHS-QoL is made landscape here, whatever classified it.

    A portrait image is tried both ways (90 degrees clockwise and
    counter-clockwise) and kept in the orientation where local OCR reads
    more of the form's printed row labels -- a direct test of "is the
    text upright", rather than trusting OSD, which on these scans reported
    low confidence and sometimes the wrong side. Returns the degrees
    rotated clockwise (0 if left alone). Free: local OCR only."""
    try:
        with Image.open(img_path) as im:
            im.load()
            if im.height <= im.width:
                return 0
            best_deg, best_score, best_img = 0, -1, None
            for deg in (90, 270):
                cand = im.rotate(-deg, expand=True).convert("RGB")
                small = cand.convert("L")
                s = 1400 / max(small.size)
                if s < 1:
                    small = small.resize((int(small.width * s), int(small.height * s)), Image.LANCZOS)
                txt = _normalize(pytesseract.image_to_string(small, timeout=OCR_TIMEOUT_SECS))
                score = sum(1 for w in _EURAHS_UPRIGHT_WORDS if w in txt)
                if score > best_score:
                    best_deg, best_score, best_img = deg, score, cand
        if best_img is not None and best_score > 0:
            best_img.save(img_path, "JPEG", quality=88)
            return best_deg
    except Exception:  # noqa: BLE001 -- never fatal; the page is still extracted
        pass
    return 0


def process_packet(pdf_path: str, packet_id: str, mission: dict | None = None) -> dict:
    out_dir = Path("_page_images") / packet_id  # scratch space, not a deliverable
    imgs = page_images(pdf_path, out_dir)

    page_results = []
    for i, img_path in enumerate(imgs, 1):
        page_type = classify_page_free(str(img_path))   # free
        if page_type == "Unclassified":
            # A sideways scan defeats keyword OCR entirely (Ghana 2026 Oct:
            # the EuraHS-QoL questionnaire is printed landscape and scanned
            # rotated on every packet). Try once more upright, still free.
            rot_type = _autorotate_for_ocr(img_path)
            if rot_type != "Unclassified":
                print(f"    (p{i}: page was rotated; turned upright -> {rot_type})")
                page_type = rot_type
        if page_type == "Unclassified":
            # Free OCR could not read this page's title. Escalate to the
            # vision model ONCE before giving up on classifying it -- see
            # classify_page_vision's docstring (added at the Sierra Leone
            # 2023 vet gate). Cheap (a ~20-token reply) and only runs for
            # the minority of pages that actually need it.
            vision_type = classify_page_vision(str(img_path))
            if vision_type != "Unclassified":
                print(f"    (p{i}: local OCR unclassified; vision fallback -> {vision_type})")
            page_type = vision_type
        if page_type == "EuraHS-QoL":
            turned = ensure_landscape_upright(img_path)
            if turned:
                print(f"    (p{i}: EuraHS-QoL page turned {turned} degrees to landscape before extraction)")
        result = extract_page(str(img_path), page_type, mission)  # costs an API call
        result["page_label"] = f"{page_type} p{i}"
        page_results.append(result)

    return {"packet_id": packet_id, "pages": page_results}


if __name__ == "__main__":
    if not ensure_api_key():
        sys.exit("No API key found. Paste your key (starts with sk-ant) into api_key.txt "
                 "next to this script, or set the ANTHROPIC_API_KEY environment variable.")
    if len(sys.argv) < 2:
        sys.exit("Usage: python extract.py path/to/packet.pdf [missions/profile.json]")
    pdf_path = sys.argv[1]
    mission = load_mission(sys.argv[2]) if len(sys.argv) > 2 else None
    packet_id = Path(pdf_path).stem
    raw = process_packet(pdf_path, packet_id, mission)
    out_path = Path("raw_json_output") / f"{packet_id}.json"
    out_path.write_text(json.dumps(raw, indent=2))
    print(f"Wrote {out_path}")
