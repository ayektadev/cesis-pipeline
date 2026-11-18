"""
run_mission.py

One-shot runner for an ENTIRE mission (one year, one site), with a
built-in stop-and-check vetting gate.

The intended workflow (see README_MISSION.md for the plain-English
version):

  Step 1 -- VET a small sample first (default 5 packets):
      python run_mission.py --mission missions/ghana_2025.json \
          --pdf-dir "Ghana 2025 PDFs" --vet 5
    Processes ONLY the first 5 packets, writes a small case log named
    <output>_VET.xlsx plus a VET_CHECKLIST_<prefix>.xlsx, then STOPS.
    A human compares those rows against the paper logs, box by box, and
    fills in the checklist's Match?/Correction columns.

  Step 2 -- if the vet looks good, run the whole mission:
      python run_mission.py --mission missions/ghana_2025.json \
          --pdf-dir "Ghana 2025 PDFs"
    Processes every remaining packet (the 5 vetted ones are NOT
    re-extracted or re-billed -- their JSON already exists) and writes
    the full, mission-named case log.

  If the vet looks BAD: do NOT run the full mission. Fix the cause first
  (usually the mission profile or the form-layout text in extract.py --
  see README_MISSION.md "When the vet fails"), delete the bad JSONs in
  raw_json_output/ so they re-extract, and vet again. The gate exists so
  a systematic misread costs you 5 packets of API spend, not 60.

  Step 3 -- happens automatically, no command needed: once the full run
  finishes, everything specific to that mission (the deliverable, its
  _VET.xlsx and VET_CHECKLIST, the source PDFs, that mission's raw JSON,
  and the run log) is moved into a "Legacy <LETTERS> <YEAR>" folder --
  e.g. "Legacy SL 2023" -- so the top-level folder is clean and ready for
  the next mission's vet/full cycle without anyone archiving by hand. The
  mission profile in missions/ and any _page_images/ scratch cache are
  left where they are on purpose. See archive_mission() below.

Resume behavior: a packet whose JSON already exists in raw_json_output/
is always skipped, so an interrupted run (network drop, etc.) can simply
be re-run and it picks up where it left off.
"""

import argparse
import datetime as dt
import glob
import json
import os
import re
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
RAW_JSON_DIR = HERE / "raw_json_output"

# Copied into every archived raw_json_output subfolder -- see archive_mission()
# below. Kept as one shared constant rather than re-typed per mission so it
# can't drift between missions the way a hand-copied file could.
LEGACY_RAW_JSON_README = """This folder is where the pipeline stores what it read off each page, one
JSON file per patient packet. It ships empty.

Two things follow from that, and both matter:

1. Resume. A packet whose JSON is already here is skipped and never sent
   to the API again. An interrupted run just needs the same command
   again. This is also why the vet run's five packets cost nothing extra
   when you go on to the full mission.

2. Re-extraction. If a packet was read wrong for a systematic reason --
   a bad prompt, a misconfigured mission profile -- deleting its JSON
   here is how you make the pipeline read it again. Deleting a file here
   means paying to re-read that packet, so delete deliberately rather
   than clearing the folder out of tidiness.

The Excel case log is rebuilt from these files every time. They, not the
spreadsheet, are the record of what the pipeline actually saw.

These files contain patient data. They stay on the machine that did the
run; do not forward this folder.
"""


class _Tee:
    """Duplicates every stdout write to a second stream. main() wraps
    sys.stdout in one of these for its whole run so that everything
    printed -- extraction progress, running cost, the duration-bound
    check, the vet checklist summary, all of it -- also lands in a
    per-mission log file, without every print() in this module needing
    to know a log file exists. That log is what archive_mission() files
    away at the end of a full run; a log reconstructed after the fact
    from memory would inevitably be a paraphrase, not a record."""

    def __init__(self, *streams):
        self.streams = streams

    def write(self, data):
        for s in self.streams:
            s.write(data)
            s.flush()

    def flush(self):
        for s in self.streams:
            s.flush()


def _notify_done(message: str, title: str, is_error: bool = False) -> None:
    """Best-effort desktop notification (a beep plus a popup box) so a run
    that takes minutes to hours doesn't finish silently while Kristen is
    away from the terminal. Windows-only -- winsound and ctypes.windll are
    both Windows stdlib, so this quietly does nothing on Mac/Linux rather
    than erroring. Every step is wrapped and swallowed on failure: a
    notification that can't fire is not a reason to make main() think the
    actual mission run failed, so this must never be the thing that raises."""
    if sys.platform != "win32":
        return
    try:
        import winsound
        winsound.MessageBeep(winsound.MB_ICONHAND if is_error else winsound.MB_ICONASTERISK)
    except Exception:
        pass
    try:
        import ctypes
        MB_ICONERROR = 0x10
        MB_ICONINFORMATION = 0x40
        MB_SYSTEMMODAL = 0x1000   # forces the box in front even if PowerShell isn't the active window
        MB_TOPMOST = 0x40000
        icon = MB_ICONERROR if is_error else MB_ICONINFORMATION
        ctypes.windll.user32.MessageBoxW(0, message, title, icon | MB_SYSTEMMODAL | MB_TOPMOST)
    except Exception:
        pass


# Columns a human actually compares against the paper log during vetting,
# grouped into sections so a 30-field-per-patient sheet stays navigable.
#
# The hernia-detail and post-op blocks are new and therefore the LEAST
# trustworthy part of the pipeline, so they are vetted in full rather than
# sampled. That is the point of the gate: the code most likely to be wrong
# is the code nobody has ever checked against paper. It makes the checklist
# longer -- roughly 35 rows per patient instead of 10 -- which is a real
# cost, but it is paid once, on five packets, before the mission-wide
# spend, and it is the only thing standing between a subtle misreading and
# 66 rows of confidently-formatted wrong numbers.
#
# Ordering within each section follows the order the values appear on the
# paper, so a reviewer can work down the packet rather than hunting.
VET_COLUMNS = [
    ("mrn", "MRN", "Identity"),
    ("date_of_surgery", "Date of surgery", "Identity"),
    ("age", "Age", "Identity"),
    ("gender", "Gender", "Identity"),
    ("preop_dx", "Preop diagnosis", "Identity"),
    ("procedure", "Procedure", "Identity"),
    ("procedure_category", "Procedure category (derived)", "Identity"),

    ("or_time_mins", "OR Time (mins)", "Timing"),
    ("surgery_time_mins", "Surgery Time (mins)", "Timing"),
    ("anesthesia_time_mins", "Anesthesia Time (mins)", "Timing"),
    ("operative_time_mins", "Operative time (mins)", "Timing"),

    ("anesthetist", "Anesthetist (name)", "Staff"),
    ("anesthesia_type", "Anesthesia type/technique", "Staff"),
    ("surgeon_assistant", "Surgeon/Assistant", "Staff"),

    # Read off the printed classification table on the 2025 H&P. A blank
    # here should mean the box was genuinely unmarked -- confirming that
    # is exactly what this section is for.
    ("h_score", "Hernia size grade (H1-H4)", "Hernia detail"),
    ("hernia_f_score", "Skin-fold score (F)", "Hernia detail"),
    ("inguinoscrotal_component", "Inguinoscrotal component (a/b/c)", "Hernia detail"),
    ("hernia_laterality", "Hernia side (R/L/bilateral)", "Hernia detail"),
    ("hernia_reducibility", "Reducibility", "Hernia detail"),
    ("hernia_recurrent", "Primary or recurrent", "Hernia detail"),
    ("repair_technique", "Repair technique", "Hernia detail"),
    ("defect_count", "Defect count", "Hernia detail"),

    # Sourced from the PACU and Floor flowsheets and the RN discharge form
    # -- pages the previous build classified but never actually read, so
    # none of this has ever been checked against paper.
    ("pacu_arrival_time", "PACU arrival", "Post-op course"),
    ("pacu_transfer_time", "PACU transfer to floor", "Post-op course"),
    ("pacu_los_mins", "PACU time (mins)", "Post-op course"),
    ("floor_arrival", "Floor arrival", "Post-op course"),
    ("last_entry_date", "Last entry date (discharge fallback)", "Post-op course"),
    ("discharge_datetime", "Discharge date/time", "Post-op course"),
    ("los_hours", "LOS (hours)", "Post-op course"),
    ("los", "LOS (whole days)", "Post-op course"),
    ("max_pod_documented", "Last POD documented", "Post-op course"),
    ("discharge_criteria_met", "Discharge criteria met", "Post-op course"),

    ("seen_post_op", "Seen post-op", "Outcomes"),
    ("postop_visit_date", "Post-op visit date", "Outcomes"),
    ("postop_visit_pod", "Post-op visit POD", "Outcomes"),
    ("postop_visit_findings", "Post-op visit findings", "Outcomes"),
    ("postop_complication", "Post-op complication", "Outcomes"),
    ("further_surgery_planned", "Further surgery planned", "Outcomes"),
    ("complications_notes", "Complications/notes (all pages)", "Outcomes"),

    # New with the Ghana 2026 (October) forms -- never yet checked against paper.
    ("social_nicotine", "Nicotine (Denies/Past/Current)", "Social history"),
    ("social_etoh", "Alcohol (Denies/Past/Current)", "Social history"),
    ("social_substance_use", "Substance use", "Social history"),
    ("social_occupation", "Occupation", "Social history"),
    ("travel_from", "Travel from", "Social history"),
    ("travel_distance", "Travel distance", "Social history"),
    ("pain_rest", "EuraHS pain at rest", "EuraHS-QoL"),
    ("pain_activity", "EuraHS pain during activity", "EuraHS-QoL"),
    ("pain_worst_week", "EuraHS worst pain last week", "EuraHS-QoL"),
    ("restrict_daily", "EuraHS restriction daily", "EuraHS-QoL"),
    ("restrict_outside", "EuraHS restriction outside", "EuraHS-QoL"),
    ("restrict_sports", "EuraHS restriction sports", "EuraHS-QoL"),
    ("restrict_heavy_labour", "EuraHS restriction heavy labour", "EuraHS-QoL"),
    ("cosmetic_abdomen_shape", "EuraHS cosmetic abdomen", "EuraHS-QoL"),
    ("cosmetic_hernia_site", "EuraHS cosmetic hernia site", "EuraHS-QoL"),
    ("qol_total_score", "EuraHS total (derived)", "EuraHS-QoL"),
]


def load_mission_profile(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        mission = json.load(f)
    for key in ("mission_name", "mrn_prefix", "output_xlsx"):
        if not mission.get(key):
            sys.exit(f"Mission profile {path} is missing required key: {key}")
    return mission


# ---------------------------------------------------------------------------
# Auto-calibration: the vet run discovers the mission's date window and
# staff roster from the sample packets themselves and writes them into
# the profile JSON, so nobody has to hand-fill missions/*.json. The vet
# checklist then shows what was detected for a human to CONFIRM against
# the paper -- confirmation stays human, typing doesn't have to.
# ---------------------------------------------------------------------------
def profile_is_calibrated(mission: dict) -> bool:
    """True once the vet step has actually run against this profile.

    Requires a real date_window (every date-plausibility check downstream
    depends on it) AND the "_calibration" marker that apply_calibration()
    writes whenever a --vet run changes anything.

    This used to also require a non-empty roster, which was a broken proxy:
    roster is explicitly a soft sanity check, never enforced anywhere else
    (a handwritten name is never forced to match it -- see missions/
    _TEMPLATE_new_mission.json's own note), so a legitimate small vet
    sample where no staff name happened to repeat left roster["surgeons"]
    etc. all empty -- profile_is_calibrated() then reported "not calibrated"
    forever, blocking the full run even after a human had genuinely
    completed the vet and reviewed the checklist against paper (Sierra
    Leone 2013, 2026-08: 5/5 vetted packets, 0 names repeated twice). The
    "_calibration" marker is the real signal that --vet has run at all,
    which is what this check is supposed to guarantee -- not that the
    roster happened to come back non-empty."""
    from extract import real_date_window
    return bool(real_date_window(mission)) and bool(mission.get("_calibration"))


def _parse_date(s, default_year=None):
    s = str(s).strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
        try:
            return dt.datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    m = re.match(r"^(\d{1,2})/(\d{1,2})$", s)
    if m and default_year:
        try:
            return dt.date(int(default_year), int(m.group(1)), int(m.group(2)))
        except ValueError:
            pass
    return None


def _clean_name(raw: str) -> str:
    return re.sub(r"\(.*?\)", "", str(raw)).strip(" ,;/")


def calibrate_from_jsons(json_paths, mission: dict) -> dict:
    """Reads the vetted packets' raw JSON and derives: the observed
    surgery-date window and the staff names seen, with occurrence counts.
    Returns findings; does not write anything itself."""
    from collections import Counter
    from reconcile import parse_date_day_first
    dates, full_years = [], Counter()
    anes_names, surg_names = Counter(), Counter()

    raw_dates = []
    floor_dates = []  # (raw string, field name) for the day/month-order check below
    for jp in json_paths:
        with open(jp, encoding="utf-8") as f:
            d = json.load(f)
        for page in d.get("pages", []):
            fields = page.get("fields", {})
            v = (fields.get("date") or {}).get("value")
            if v:
                raw_dates.append(str(v))
            for key, bucket in (("anesthesiologist", anes_names),
                                ("surgeon_assistant", surg_names)):
                nv = (fields.get(key) or {}).get("value")
                if nv:
                    for piece in str(nv).replace(",", "/").split("/"):
                        name = _clean_name(piece)
                        if len(name) >= 3:
                            bucket[name] += 1
            for fkey in ("floor_arrival", "floor_discharge"):
                fv = (fields.get(fkey) or {}).get("value")
                if fv:
                    floor_dates.append((str(fv), fkey))

    # First pass: dates that carry their own year tell us the mission year.
    parsed = [(_parse_date(s), s) for s in raw_dates]
    for p, _ in parsed:
        if p:
            full_years[p.year] += 1
    default_year = mission.get("year") or (full_years.most_common(1)[0][0] if full_years else None)
    # Second pass: month/day-only dates get the inferred year.
    for p, s in parsed:
        d = p or _parse_date(s, default_year)
        if d:
            dates.append(d)

    findings = {"raw_date_count": len(raw_dates), "anes": anes_names, "surg": surg_names}
    if dates:
        findings["date_window"] = [min(dates).isoformat(), max(dates).isoformat()]
        findings["year"] = default_year

    # Day/month-order check for the Floor Flowsheet's "Date/Time Arrived" /
    # "Date/Time Discharged" boxes (Sierra Leone 2024's vet turned up
    # packets where this form was filled in day-first even though the rest
    # of the mission's forms, and this file's own _parse_date, are
    # month-first). The H&P "date" field is the trusted anchor -- it is
    # what set findings["date_window"] above -- so a Floor Flowsheet value
    # that only lands inside that window when read DAY-first, and falls
    # outside it read the normal month-first way, is evidence of the mixed
    # convention rather than a one-off misread. This never rewrites the
    # value itself (rule 18); it only earns the mission an extraction_hint
    # telling a human/the model to check both readings before flagging.
    day_first_evidence = 0
    if dates:
        lo, hi = min(dates), max(dates)
        for raw, _fkey in floor_dates:
            date_part = raw.strip().split()[0] if raw.strip() else raw
            mf = _parse_date(date_part, default_year)
            df = parse_date_day_first(date_part, default_year)
            if df is None or mf == df:
                continue  # unambiguous (ISO, or day == month) -- no evidence either way
            mf_in_window = mf is not None and lo <= mf <= hi
            df_in_window = lo <= df <= hi
            if df_in_window and not mf_in_window:
                day_first_evidence += 1
    if day_first_evidence:
        findings["floor_flowsheet_day_first_count"] = day_first_evidence
    return findings


def apply_calibration(findings: dict, mission: dict, profile_path: str, n_packets: int) -> list[str]:
    """Writes detected facts into the profile JSON -- but ONLY into slots
    that are still uncalibrated. Human-entered values are never
    overwritten. Returns plain-language lines describing what changed."""
    from extract import real_date_window
    changed = []
    if not real_date_window(mission) and findings.get("date_window"):
        mission["date_window"] = findings["date_window"]
        if not mission.get("year") and findings.get("year"):
            mission["year"] = findings["year"]
        changed.append(f"date_window set to {findings['date_window'][0]} .. {findings['date_window'][1]}")
    roster = mission.setdefault("roster", {})
    if not any(roster.get(k) for k in ("crnas", "anesthesiologists", "surgeons")):
        # Names seen at least twice go in the roster; one-offs stay in the
        # checklist for a human to promote (a single sighting could be a
        # misread, and the roster must never launder one into "known").
        roster["anesthesiologists"] = sorted(n for n, c in findings["anes"].items() if c >= 2)
        roster["surgeons"] = sorted(n for n, c in findings["surg"].items() if c >= 2)
        changed.append(f"roster seeded with names seen 2+ times "
                       f"({len(roster['anesthesiologists'])} anesthesia, {len(roster['surgeons'])} surgical)")
    # Floor Flowsheet day/month-order hint. Unlike date_window and roster
    # this is never "already calibrated" in a way that blocks re-adding --
    # it is deduped by content instead, so a later vet run against MORE
    # packets can still add it even if the profile already has other hints,
    # but never adds a second copy.
    if findings.get("floor_flowsheet_day_first_count"):
        hints = mission.setdefault("extraction_hints", [])
        marker = "Floor Flowsheet"
        already_present = any(marker in h and "day-first" in h for h in hints)
        if not already_present:
            hints.append(
                "Floor Flowsheet 'Date/Time Arrived' and 'Date/Time Discharged' do NOT "
                "reliably follow one day/month convention across packets, unlike the rest "
                "of this mission's forms (which are month-first). Auto-detected from the "
                f"vetted sample: {findings['floor_flowsheet_day_first_count']} Floor "
                "Flowsheet date value(s) only fell inside the mission window when read "
                "day-first, and fell outside it read the normal month-first way. When a "
                "Floor Flowsheet date looks like it's outside the mission window, check "
                "whether the day-first reading puts it back inside the window before "
                "treating it as a documentation error -- but always TRANSCRIBE exactly "
                "what is written on the page and flag it for human review; never silently "
                "swap day and month (RULES.md rule 18)."
            )
            changed.append(
                f"extraction_hints: added Floor Flowsheet day/month-order warning "
                f"({findings['floor_flowsheet_day_first_count']} value(s) in the vetted "
                f"sample only made sense read day-first)"
            )
    if changed:
        mission["_calibration"] = (f"Auto-calibrated from the first {n_packets} vetted packet(s). "
                                   "CONFIRM against the paper logs via the vet checklist's "
                                   "Calibration tab; correct this file if the paper disagrees.")
        with open(profile_path, "w", encoding="utf-8") as f:
            json.dump(mission, f, indent=2, ensure_ascii=False)
    return changed


def mission_pdfs(pdf_dir: str, prefix: str) -> list[Path]:
    """All packet PDFs for this mission, sorted by patient NUMBER (so
    'PR19-2' comes before 'PR19-10'), matching mrn_sort_key's intent."""
    from reconcile import mrn_sort_key
    pdfs = [Path(p) for p in glob.glob(os.path.join(pdf_dir, "*.pdf"))]
    pdfs = [p for p in pdfs if p.stem.upper().startswith(prefix.upper())]
    return sorted(pdfs, key=lambda p: mrn_sort_key(p.stem))


def json_path_for(packet_id: str) -> Path:
    return RAW_JSON_DIR / f"{packet_id}.json"


def extract_missing(pdfs: list[Path], mission: dict) -> tuple[int, int]:
    """Extract every packet that doesn't already have a JSON. Returns
    (extracted, skipped). Only imports extract.py (and therefore only
    demands an API key) if there is actually work to do."""
    todo = [p for p in pdfs if not json_path_for(p.stem).exists()]
    skipped = len(pdfs) - len(todo)
    if not todo:
        return 0, skipped

    # Key lookup: environment variable, or the api_key.txt file next to
    # the scripts (the novice-friendly option -- see START_HERE checklist).
    key_ok = os.environ.get("ANTHROPIC_API_KEY", "").startswith("sk-ant")
    key_file = HERE / "api_key.txt"
    if not key_ok and key_file.exists():
        key = key_file.read_text().strip()
        if key.startswith("sk-ant"):
            os.environ["ANTHROPIC_API_KEY"] = key
            key_ok = True
    if not key_ok:
        sys.exit(
            f"{len(todo)} packet(s) need extraction but no API key was found.\n"
            "Open the file api_key.txt (in this folder), delete the placeholder\n"
            "text, and paste in the key you were given (it starts with sk-ant).\n"
            "Then run this same command again.\n"
            "(Already-extracted packets are reused for free.)"
        )
    import extract  # deferred: needs anthropic/pytesseract/pdf2image installed

    RAW_JSON_DIR.mkdir(exist_ok=True)
    failed = []
    for n, pdf in enumerate(todo, 1):
        print(f"[{n}/{len(todo)}] extracting {pdf.name} ...")
        try:
            raw = extract.process_packet(str(pdf), pdf.stem, mission)
            # encoding="utf-8" is required here, not decorative. Without it,
            # write_text() falls back to the platform's default text
            # encoding -- on Windows that's a locale codepage (often
            # cp1252), not UTF-8. A verbatim transcription that includes a
            # character outside that codepage (SL22-903 hit one) raises
            # UnicodeEncodeError. This write is inside the same try block as
            # the extraction call for the same reason the comment below
            # explains: one packet's problem, of any kind, must not take
            # the other 51 down with it.
            json_path_for(pdf.stem).write_text(
                json.dumps(raw, indent=2, ensure_ascii=False), encoding="utf-8")
            # Running cost, printed per packet rather than only at the end. A total
            # that appears after the last packet cannot help anyone decide to stop a
            # run that is costing more than they expected, which is the main thing
            # showing it is for.
            print(f"          wrote raw_json_output/{pdf.stem}.json"
                  f"   [running total: ${extract.usage_cost():.2f} est.]")
        except Exception as exc:  # noqa: BLE001
            # One packet with a genuinely broken page (bad scan, a reply
            # that stays malformed after extract_page's own retries), or a
            # failure writing this one packet's JSON to disk, used to take
            # the whole run down with it -- on a 63-packet run that means
            # the other 40-odd packets, already sitting there ready to
            # extract, don't get touched, and nobody finds out until
            # someone happens to check. Record the failure and keep going;
            # every other packet is independent of this one. The failure
            # list is reported at the end and nothing about it is silent.
            print(f"    FAILED: {exc}")
            failed.append((pdf.stem, str(exc)))
            continue
    if todo:
        print()
        print(extract.usage_report(packets=len(todo)))
    if failed:
        print()
        print(f"{len(failed)} packet(s) FAILED extraction and were skipped "
              "(everything else still ran):")
        for pid, msg in failed:
            first_line = msg.splitlines()[0]
            print(f"  - {pid}: {first_line}")
        print("Re-run this same command to retry just the failed packets "
              "(already-extracted ones are reused for free).")
    return len(todo) - len(failed), skipped


def build_case_log(packet_ids: list[str], out_path: Path, mission: dict | None = None) -> None:
    from reconcile import (load_and_reconcile, mrn_sort_key, flag_dates_outside_window,
                           flag_discharge_is_followup, flag_implausible_values,
                           verify_duration_bounds, flag_pmh_row_inconsistency,
                           suppress_implausible_los)
    from xlsx_writer import write_case_log

    rows = []
    for pid in packet_ids:
        jp = json_path_for(pid)
        if jp.exists():
            rows.extend(load_and_reconcile(str(jp), (mission or {}).get("reconcile_options")))
    rows.sort(key=lambda r: mrn_sort_key(r.fields["mrn"].value))
    # Checked here rather than inside reconcile_packet because the window
    # is a property of the MISSION, and reconcile_packet deliberately sees
    # only one packet's raw JSON.
    n = flag_dates_outside_window(rows, mission)
    if n:
        print(f"          {n} date(s) fell outside the mission window and were flagged")
    # Same cross-packet reasoning as the window check above: one packet's
    # own confidence says nothing about whether "no PMH checkbox row" is
    # consistent with what every other packet in this batch found.
    n = flag_pmh_row_inconsistency(rows)
    if n:
        print(f"          {n} packet(s) claimed 'no PMH checkbox row' despite other "
              f"packets in this batch having it -- flagged as suspect misreads")
    n = flag_discharge_is_followup(rows)
    if n:
        print(f"          {n} case(s) look like the follow-up date was read as the "
              f"discharge date -- LOS flagged")
    n = flag_implausible_values(rows)
    if n:
        print(f"          {n} value(s) fell outside physical plausibility and were flagged")
    # LOS/LOS (hours) get blanked, not just flagged, once implausible --
    # see the comment on suppress_implausible_los() in reconcile.py for why
    # a computed duration doesn't get the same "leave the number as a clue"
    # treatment as a directly-read value like age.
    n = suppress_implausible_los(rows)
    if n:
        print(f"          {n} LOS/LOS-hours value(s) were implausible and blanked "
              f"(reason kept in the cell comment)")
    # Rule 22: a bound that doesn't subtract to the minutes beside it looks
    # like corroboration while actually contradicting, so it is checked on
    # every build rather than trusted.
    problems = verify_duration_bounds(rows)
    if problems:
        print(f"          !! {len(problems)} duration(s) disagree with their own start/stop bounds:")
        for p in problems[:10]:
            print(f"             {p}")
        if len(problems) > 10:
            print(f"             ... and {len(problems) - 10} more")
    else:
        print("          all durations agree with their start/stop bounds")
    stats = write_case_log(rows, str(out_path))
    print(f"Wrote {stats['rows_written']} rows to {out_path.name} "
          f"({stats['fields_flagged']}/{stats['total_fields']} fields flagged for review)")


def build_vet_checklist(packet_ids: list[str], mission: dict, out_path: Path,
                        findings: dict | None = None, calibration_notes: list[str] | None = None) -> None:
    """A human-facing xlsx: one row per vetted field value, with empty
    'Paper log says' / 'Match?' / 'Correction' columns to fill in while
    sitting with the physical paper logs."""
    from openpyxl import Workbook
    from openpyxl.comments import Comment
    from openpyxl.styles import Font, PatternFill, Alignment
    from reconcile import (load_and_reconcile, mrn_sort_key, flag_dates_outside_window,
                           flag_discharge_is_followup, flag_implausible_values,
                           verify_duration_bounds, flag_pmh_row_inconsistency,
                           suppress_implausible_los)
    from schema import COLUMN_DEFINITIONS
    from xlsx_writer import _excel_safe

    rows = []
    for pid in packet_ids:
        jp = json_path_for(pid)
        if jp.exists():
            rows.extend(load_and_reconcile(str(jp), (mission or {}).get("reconcile_options")))
    rows.sort(key=lambda r: mrn_sort_key(r.fields["mrn"].value))
    # Must run before the rows are written out, so the reviewer sees the
    # window conflict on the checklist line itself rather than having to
    # notice that an October date is odd for a December mission.
    flag_dates_outside_window(rows, mission)
    flag_pmh_row_inconsistency(rows)
    flag_discharge_is_followup(rows)
    flag_implausible_values(rows)
    suppress_implausible_los(rows)

    wb = Workbook()
    ws = wb.active
    ws.title = "Vet Checklist"
    header = ["MRN", "Section", "Field", "Extracted value", "Confidence", "Pipeline flag",
              "Paper log says", "Match? (Y/N)", "Correction / notes"]
    ws.append(header)
    hfill = PatternFill("solid", fgColor="1F4E79")
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = hfill
    lowfill = PatternFill("solid", fgColor="FFF2CC")  # highlight low/medium confidence

    def annotate_field_cell(key: str) -> None:
        """Hang the column's definition off the Field cell of the row just
        appended. On the case log the definitions sit on the header row,
        but here the fields ARE the rows, so this is the equivalent place.
        It matters more on this sheet than on the case log: the reviewer is
        deciding whether a value matches the paper, and cannot do that
        without knowing which box on the paper the value is supposed to
        have come from."""
        definition = COLUMN_DEFINITIONS.get(key)
        if not definition:
            return
        note = Comment(definition, "ISHI extraction pipeline")
        note.width, note.height = 460, 30 + 13 * (len(definition) // 58 + 1)
        ws.cell(row=ws.max_row, column=3).comment = note

    for r in rows:
        mrn = r.fields["mrn"].value
        for key, label, section in VET_COLUMNS:
            if key == "mrn":
                continue
            f = r.fields.get(key)
            if f is None:
                # A field named in VET_COLUMNS but absent from the row means
                # reconcile.py never filled it -- a pipeline gap. Silently
                # dropping the line would hide it from the one review that
                # exists to catch exactly this, so it gets a visible row.
                ws.append([mrn, section, label, "(NOT PRODUCED BY PIPELINE)", "",
                           "this column is in the schema but nothing filled it in", "", "", ""])
                annotate_field_cell(key)
                for cell in ws[ws.max_row][:6]:
                    cell.fill = lowfill
                continue
            # Same coercion the case log uses, for the same reason: a few
            # fields come back as lists and Excel has no cell type for one.
            # This sheet dodged it at the vet gate only because none of the
            # first five packets happened to contain one.
            val = _excel_safe(f.value)
            if val is None or val == "":
                val = "(absent)"
            conf = getattr(f, "confidence", "") or ""
            flag = getattr(f, "flag_reason", "") or ""
            ws.append([mrn, section, label, val, conf, flag, "", "", ""])
            annotate_field_cell(key)
            if conf in ("low", "medium"):
                for cell in ws[ws.max_row][:6]:
                    cell.fill = lowfill

    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions  # so a reviewer can isolate one section
    widths = [10, 16, 26, 30, 12, 50, 20, 12, 30]
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[chr(64 + i)].width = w
    for row in ws.iter_rows(min_row=2):
        row[5].alignment = Alignment(wrap_text=True, vertical="top")

    # Calibration sheet: what the vet run auto-detected about the mission
    # itself, for a human to CONFIRM (not type in) against the paper.
    if findings:
        cal = wb.create_sheet("Calibration")
        cal.append(["What the computer detected from these packets", "Value / count",
                    "Confirm against paper (Y/N)", "Correction"])
        for c in cal[1]:
            c.font = Font(bold=True, color="FFFFFF")
            c.fill = hfill
        if findings.get("date_window"):
            lo, hi = findings["date_window"]
            cal.append(["Mission date range (first to last surgery seen)", f"{lo} to {hi}", "", ""])
        else:
            cal.append(["Mission date range", "COULD NOT DETECT -- dates unreadable on sample; "
                        "fill missions profile by hand from paper", "", ""])
        for label, counter in (("Anesthesia provider seen", findings.get("anes", {})),
                               ("Surgeon/assistant seen", findings.get("surg", {}))):
            for name, count in sorted(counter.items(), key=lambda kv: -kv[1]):
                note = "" if count >= 2 else "  (seen ONCE -- possible misread, not added to roster)"
                cal.append([label, f"{name}  x{count}{note}", "", ""])
        cal.append([])
        cal.append(["These were written into the mission profile automatically"
                    " (names seen 2+ times only)." , "", "", ""])
        for ln in (calibration_notes or []):
            cal.append([f"  - {ln}", "", "", ""])
        cal.append(["If the paper disagrees with any of this, correct "
                    "missions/<profile>.json before the full run.", "", "", ""])
        for col, w in zip("ABCD", (48, 44, 26, 24)):
            cal.column_dimensions[col].width = w

    # Instructions sheet, so the checklist explains itself.
    info = wb.create_sheet("How to vet")
    lines = [
        f"Vetting sample for {mission['mission_name']} -- {len(packet_ids)} packet(s).",
        "",
        "1. Sit with the physical paper logs / packets for these MRNs.",
        "2. For each row, compare 'Extracted value' to the paper. Fill in",
        "   'Paper log says', mark Match? Y or N, and note any correction.",
        "3. Yellow rows are ones the pipeline itself was unsure about --",
        "   check those especially carefully.",
        "4. '(absent)' means the box was empty/illegible on the scan and the",
        "   pipeline deliberately did NOT guess. Confirm the paper box is",
        "   really blank -- a blank that the paper actually has a value for",
        "   means the scan quality or crop needs attention. On the full",
        "   Case Log workbook, absent fields keep a real empty cell (so",
        "   Summary formulas still work) but carry the same '(absent)' note",
        "   as a hover comment, shaded light gray, instead of looking like",
        "   an untouched cell.",
        "",
        "When done, count the N's:",
        "- Scattered one-off misreads (a digit here and there): fix those",
        "  values, then proceed to the full run.",
        "- A REPEATED error (same box wrong on multiple packets, invented",
        "  values, wrong form layout): STOP. Fix the mission profile /",
        "  form layout first, delete these packets' JSONs, and vet again.",
        "  See README_MISSION.md 'When the vet fails'.",
    ]
    for ln in lines:
        info.append([ln])
    info.column_dimensions["A"].width = 80

    wb.save(out_path)
    print(f"Wrote {out_path.name}")


def _prefix_letters_year(prefix: str, mission: dict) -> str:
    """Derives the '<LETTERS> [QUALIFIER] <YEAR>' stem used in 'Legacy
    <LETTERS> [QUALIFIER] <YEAR>' -- e.g. mrn_prefix 'SL23' + mission['year']
    2023 -> 'SL 2023'. The year comes from the mission profile's own year
    field, NOT by assuming the prefix's trailing digits are the year: a
    two-digit prefix year and the mission's real four-digit year are not
    always the same number one-to-one, and the profile's year field is the
    one place that's unambiguous about it.

    Same-site, same-year missions collide on letters+year alone -- Ghana
    2025 (May), prefix 'GH25', and Ghana 2025 (September), prefix 'GH25s',
    both reduce to letters 'GH' -- so 'Legacy GH 2025' and its
    'raw_json_output GH 2025' subfolder would be shared by two unrelated
    missions' archives, and the second archive_mission() call would file
    its raw JSON into a folder the first mission already claimed (found
    2026-08, while adding a third same-year Ghana mission; 'Legacy GH MAY
    2025' and 'Legacy GH SEPTEMBER 2025' already existed on disk with this
    disambiguation, done by hand at the time rather than by this function,
    which is what let the mismatch go unnoticed until now). Fixed by
    pulling the parenthetical qualifier out of mission_name when present
    ('Ghana 2025 (May)' -> 'MAY') and inserting it between the letters and
    the year. Missions whose mission_name has no parenthetical (one Ghana
    mission a year, or any Sierra Leone mission so far) are unaffected --
    'Sierra Leone 2023' still reduces to 'SL 2023', matching the existing
    Legacy SL* folders exactly."""
    m = re.match(r"^[A-Za-z]+", prefix)
    letters = m.group(0).upper() if m else prefix.upper()
    year = mission.get("year") or "UNKNOWN_YEAR"
    qualifier_match = re.search(r"\(([^)]+)\)", mission.get("mission_name", ""))
    qualifier = f"{qualifier_match.group(1).upper()} " if qualifier_match else ""
    return f"{letters} {qualifier}{year}"


def archive_mission(mission: dict, prefix: str, pdf_dir: str, log_path: Path) -> Path | None:
    """Files away everything specific to a just-completed mission run
    into a 'Legacy <LETTERS> <YEAR>' folder, reproducing exactly the
    structure built by hand for Legacy SL 2023 / Legacy SL 2024: the
    deliverable workbook, its _VET.xlsx and VET_CHECKLIST, the source
    PDF folder, that mission's raw JSON (moved into its own README'd
    subfolder), and the run log.

    Deliberately NOT touched, so the pipeline stays ready for the next
    mission without anyone having to reconstruct it: the mission profile
    in missions/*.json (still needed for an audit or a re-run) and any
    _page_images/<prefix>-* scratch cache (a regenerable cache, not a
    record of anything). Only called from the FULL run branch of main()
    -- never after a --vet run, since a vet run isn't a finished mission.

    Returns the archive folder, or None if there was nothing to move
    (e.g. main() is re-run against a mission that's already archived).
    """
    stem = _prefix_letters_year(prefix, mission)
    dest = HERE / f"Legacy {stem}"
    moved = []

    def _move(src: Path, dst: Path) -> None:
        if src.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
            moved.append(dst.name)

    dest.mkdir(exist_ok=True)

    out_xlsx = HERE / mission["output_xlsx"]
    _move(out_xlsx, dest / out_xlsx.name)
    vet_xlsx_name = mission["output_xlsx"].replace(".xlsx", "_VET.xlsx")
    _move(HERE / vet_xlsx_name, dest / vet_xlsx_name)
    checklist_name = f"VET_CHECKLIST_{prefix}.xlsx"
    _move(HERE / checklist_name, dest / checklist_name)
    # Excel's own lock file, left behind if the workbook was ever opened
    # on the machine doing the run -- archived alongside for parity with
    # how Legacy SL 2023/2024 were assembled by hand.
    lock_name = f"~${out_xlsx.name}"
    _move(out_xlsx.parent / lock_name, dest / lock_name)

    # Source PDFs -- only if they live inside this pipeline's own folder
    # and aren't already under a Legacy folder. A --pdf-dir the user
    # pointed at somewhere else on their machine is theirs to manage,
    # not ours to relocate.
    pdf_path = Path(pdf_dir)
    pdf_path = pdf_path.resolve() if pdf_path.is_absolute() else (HERE / pdf_dir).resolve()
    try:
        inside_here = pdf_path.is_relative_to(HERE.resolve())
    except AttributeError:  # Python <3.9 fallback, unused here but harmless
        inside_here = str(pdf_path).startswith(str(HERE.resolve()))
    if inside_here and "Legacy" not in pdf_path.name and pdf_path.exists():
        _move(pdf_path, dest / pdf_path.name)

    # This mission's raw JSON only -- never the whole raw_json_output/
    # folder, since another mission's not-yet-archived JSON could be
    # sitting in it too. Matched the same way mission_pdfs() matches
    # PDFs, so this can never disagree with what the run itself treated
    # as "this mission's packets".
    packet_jsons = sorted(p for p in RAW_JSON_DIR.glob("*.json")
                          if p.stem.upper().startswith(prefix.upper()))
    if packet_jsons:
        raw_dest = dest / f"raw_json_output {stem}"
        raw_dest.mkdir(exist_ok=True)
        for jp in packet_jsons:
            _move(jp, raw_dest / jp.name)
        readme = raw_dest / "README.txt"
        if not readme.exists():
            readme.write_text(LEGACY_RAW_JSON_README, encoding="utf-8")

    _move(log_path, dest / log_path.name)

    if not moved:
        return None
    print(f"\nArchived to {dest.name}/: deliverable, vet artifacts, source PDFs, "
          f"raw JSON, and the run log ({len(moved)} item(s) moved).")
    print("Left in place for the next mission: the profile in missions/ and any "
          "_page_images/ scratch cache.")
    return dest


def main():
    ap = argparse.ArgumentParser(description="Run a full mission (or a vetting sample) in one shot.")
    ap.add_argument("--mission", required=True, help="missions/<profile>.json")
    ap.add_argument("--pdf-dir", required=True, help="folder containing this mission's packet PDFs")
    ap.add_argument("--vet", type=int, default=0, metavar="N",
                    help="process only the first N packets, emit a vet checklist, then STOP")
    args = ap.parse_args()

    mission = load_mission_profile(args.mission)
    prefix = mission["mrn_prefix"]

    # Every invocation appends to one per-mission log (vet run and full
    # run alike), so by the time a full run finishes it holds a complete
    # transcript across both stages, not just the final command -- and
    # archive_mission() below has something honest to file away. Append
    # mode also means a run that hits Bash's own timeout and gets
    # re-invoked doesn't lose the half that already printed.
    log_path = HERE / f"run_log_{prefix}.log"
    real_stdout = sys.stdout
    log_file = open(log_path, "a", encoding="utf-8")
    sys.stdout = _Tee(real_stdout, log_file)
    should_archive = False
    stop_message = None   # set on any sys.exit()/exception so the notification can report it
    try:
        try:
            should_archive = _run(args, mission, prefix, log_path)
        except SystemExit as exc:
            stop_message = str(exc.code) if exc.code else "Stopped early (see terminal for details)."
            raise
        except Exception as exc:
            stop_message = f"{type(exc).__name__}: {exc}"
            raise
    finally:
        sys.stdout = real_stdout
        log_file.close()

    mission_label = mission.get("mission_name", prefix)
    if stop_message is not None:
        _notify_done(
            f"{mission_label} run stopped before finishing:\n\n{stop_message}",
            title="ISHI pipeline -- stopped",
            is_error=True,
        )
        return
    if not should_archive:
        # Vet mode's normal, successful stopping point.
        _notify_done(
            f"{mission_label}: vet run finished.\nCheck VET_CHECKLIST_{prefix}.xlsx "
            "against the paper logs before running the full mission.",
            title="ISHI pipeline -- vet done",
        )
        return

    # archive_mission() moves run_log_<prefix>.log itself, which means it
    # MUST run after the log file above is closed, not before. On Windows,
    # a file still open by this same process cannot be renamed or deleted
    # -- shutil.move() fails with WinError 32 ("used by another process"),
    # even though it's this very process using it. Mac/Linux allow renaming
    # an open file, which is why this went unnoticed until the first
    # Windows run of a full mission (SL19, 2026-08). archive_mission()'s
    # own "Archived to ..." confirmation therefore prints to the terminal
    # only, after the log has already been closed -- it is not itself
    # captured in the log, which is an acceptable trade rather than
    # reopening a file that's about to be relocated out from under itself.
    archive_mission(mission, prefix, args.pdf_dir, log_path)
    _notify_done(
        f"{mission_label}: full run finished and archived.\nDeliverable: {mission['output_xlsx']}",
        title="ISHI pipeline -- done",
    )


def _run(args, mission: dict, prefix: str, log_path: Path) -> bool:
    """Returns True if the caller should archive the mission afterward
    (a completed full run), False otherwise (vet mode, or an early exit)."""
    pdfs = mission_pdfs(args.pdf_dir, prefix)
    if not pdfs:
        sys.exit(f"No PDFs starting with '{prefix}' found in {args.pdf_dir}")

    print(f"Mission: {mission['mission_name']}  ({len(pdfs)} packets found, prefix {prefix})")

    if args.vet > 0:
        sample = pdfs[: args.vet]
        print(f"\n=== VET MODE: processing only the first {len(sample)} packet(s), then stopping ===\n")
        if not profile_is_calibrated(mission):
            print("Mission profile not calibrated yet -- this vet run will auto-detect the\n"
                  "date range and staff roster from the sample and write them into the profile.\n")
        done, skipped = extract_missing(sample, mission)
        print(f"\nExtracted {done} packet(s), reused {skipped} existing JSON(s).")

        ids = [p.stem for p in sample]

        # Auto-calibration: derive mission facts from what was just read,
        # write them into the profile (uncalibrated slots only), and put
        # them on the checklist's Calibration tab for human confirmation.
        findings = calibrate_from_jsons([json_path_for(i) for i in ids if json_path_for(i).exists()], mission)
        cal_notes = apply_calibration(findings, mission, args.mission, len(ids))
        for ln in cal_notes:
            print(f"  calibrated: {ln}")

        vet_log = HERE / mission["output_xlsx"].replace(".xlsx", "_VET.xlsx")
        checklist = HERE / f"VET_CHECKLIST_{prefix}.xlsx"
        build_case_log(ids, vet_log, mission)
        build_vet_checklist(ids, mission, checklist, findings, cal_notes)

        print(f"""
=== STOPPED FOR HUMAN VETTING -- nothing else was processed ===

Next steps:
  1. Open {checklist.name} and compare every row against the paper logs.
  2. If it holds up (only scattered one-off misreads, if any):
         python run_mission.py --mission {args.mission} --pdf-dir "{args.pdf_dir}"
     The {len(ids)} vetted packets are reused as-is -- no double API cost.
  3. If a REPEATED error shows up, do NOT run the full mission yet.
     See README_MISSION.md, section "When the vet fails".
""")
        return False

    if not profile_is_calibrated(mission):
        sys.exit(
            "\nThis mission profile hasn't been calibrated yet (no date window /"
            "\nroster). Run the vetting step first -- it auto-calibrates the"
            "\nprofile from the sample AND gives you the accuracy check:\n"
            f"    python3 run_mission.py --mission {args.mission} --pdf-dir \"{args.pdf_dir}\" --vet 5\n"
        )

    print("\n=== FULL MISSION RUN ===\n")
    done, skipped = extract_missing(pdfs, mission)
    print(f"\nExtracted {done} packet(s), reused {skipped} existing JSON(s) (incl. any vetted ones).")
    build_case_log([p.stem for p in pdfs], HERE / mission["output_xlsx"], mission)
    print(f"\nDone. Deliverable: {mission['output_xlsx']}")
    print("Reminder: flagged fields still need the normal human review pass -- "
          "the vet gate checks for SYSTEMATIC errors, it doesn't replace review.")

    # Only a completed FULL run gets archived -- a --vet run isn't a
    # finished mission, and archiving it would move the sample PDFs/JSON
    # out from under the full run that's about to reuse them. The actual
    # archive_mission() call happens in main(), after the log file this
    # function was writing through has been closed (see the comment there).
    return True


if __name__ == "__main__":
    main()
