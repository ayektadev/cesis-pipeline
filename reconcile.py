"""
reconcile.py

Takes the raw per-page extraction JSON (what a vision-model call returns per
page) and produces ONE CaseLogRow per packet -- matching your convention
that a same-day repeat procedure (e.g. an immediate re-exploration) folds
into Secondary Procedure / Complications, rather than becoming a second row.

Key jobs this module does that a single-page extraction cannot:
  1. Detects a same-day repeat procedure and folds it into
     secondary_procedure + complications_notes instead of splitting rows.
  2. Computes case duration: prefers an explicitly handwritten duration
     (common on non-hernia forms, e.g. "Case duration: 45 min"); falls
     back to deriving it from OR record in-room/out-of-room timestamps
     when nothing is written.
  3. Cross-checks fields that appear on more than one page and raises the
     confidence bar (or flags) when they disagree.
  4. Surfaces anything a human should double check as a review flag.
"""

from __future__ import annotations
from datetime import date, datetime, timedelta
from typing import List, Optional
import json
import re

from schema import CASE_LOG_COLUMNS, CaseLogRow, FieldValue, PROCEDURE_CATEGORIES


CONF_RANK = {"high": 2, "medium": 1, "low": 0}


def _fv(value, confidence="high", source=None, flag=None) -> FieldValue:
    return FieldValue(
        value=value,
        confidence=confidence,
        source_pages=[source] if source else [],
        flag_reason=flag,
    )


def _weaker(*confidences: str) -> str:
    return min(confidences, key=lambda c: CONF_RANK.get(c, 0))


def _minutes_between(start: str, end: str):
    # Normalise first: the 2025 Anesthesia Record's START/STOP grid is
    # written as bare four-digit numbers ("0937"), and strptime("%H:%M")
    # simply raises on those, which silently produced a null duration for
    # every case on that form.
    start, end = _parse_time(start) or start, _parse_time(end) or end
    try:
        t1 = datetime.strptime(start, "%H:%M")
        t2 = datetime.strptime(end, "%H:%M")
        delta = (t2 - t1).total_seconds() / 60
        if delta < 0:
            delta += 24 * 60
        return int(delta)
    except Exception:
        return None


def _page_has_content(page: dict) -> bool:
    """True if ANY field on this page was actually filled in (a non-null,
    non-empty value), as opposed to a blank template scan."""
    fields = (page or {}).get("fields", {}) or {}
    return any(
        isinstance(f, dict) and f.get("value") not in (None, "", [])
        for f in fields.values()
    )


def _find_pages(raw: dict, page_type: str, prefer_content: bool = False) -> List[dict]:
    """All pages of a given type, in physical scan order by default.

    prefer_content=True partitions them instead: filled/content-bearing
    pages FIRST (in their original relative order), then blank ones (also
    in their original relative order) -- a stable partition, not a full
    re-sort. This is opt-in, NOT the default, and is only passed by
    callers whose "primary page" field is read with NO fallback to any
    other page if that primary happens to be blank (OR Record, PACU
    Flowsheet, Floor Flowsheet, RN Discharge, Post-Op Visit -- see each
    call site). Found on Peru 2014 PR14-902 (2026-08): the OR Record was
    scanned twice (a blank template, then the real filled page with the
    Bard Mesh Perfix Plug sticker), with the BLANK one first. Plain
    or_pages[0] pointed at the blank page, so surgeon, assistant,
    anesthesia_type, or_date, and every other OR-Record-sourced field on
    that packet's case-log row came back None -- not because the data
    wasn't there, but because the wrong physical copy of the form was
    chosen as "primary", with nothing downstream to catch it.

    Deliberately NOT applied to H&P or Anesthesia Record, even though both
    can also appear twice with one blank: both already have their own
    fallback logic elsewhere in this function when the primary page is
    blank (H&P: pmh/psh/preop_dx/postop_dx search every hp_pages entry,
    not just hp_pages[0]; anesthetist and anesthesia_type fall back
    hp/anes -> OR Record -> H&P). Regression-testing this fix against all
    archived packets (2026-08) confirmed that applying prefer_content to
    Anesthesia Record actively made things WORSE on four packets (GH14-901,
    GH16-901, GH18-901, GH23-901, GH25-901 and others): each has a genuinely
    blank first Anesthesia Record page followed by a filled second one,
    and the existing fallback chain was already correctly recovering the
    anesthetist/timing data from the OR Record or H&P in that situation --
    reordering anes_pages bypassed that already-correct, already-vetted
    fallback path and substituted a DIFFERENT (not wrong, just different
    and unverified against those missions' original vetting) source for
    the same field. Since those missions are already archived and their
    vetting already happened against the OLD behavior, changing their
    output is out of scope for this fix and would need its own separate
    review -- so Anesthesia Record and H&P keep their original,
    unreordered behavior.

    When a page type appears twice and BOTH occurrences are genuinely
    filled (e.g. a real same-day secondary procedure's second OR Record),
    prefer_content changes nothing: both land in the content group and
    keep their original relative order, so has_genuine_secondary_procedure
    below still sees them exactly as it always has."""
    pages = [p for p in raw["pages"] if p["page_type"] == page_type]
    if not prefer_content or len(pages) <= 1:
        return pages
    content_pages = [p for p in pages if _page_has_content(p)]
    blank_pages = [p for p in pages if not _page_has_content(p)]
    return content_pages + blank_pages


def _raw_field(page: dict, key: str):
    return (page or {}).get("fields", {}).get(key)


def _raw_any(page: dict, *keys):
    """First of several possible field names that is actually PRESENT AND
    FILLED. Two things this guards against:

    - Field names differ between form generations: the 2019 Anesthesia
      Record has surgery_start/surgery_end, the 2025 one has
      procedure_start/procedure_end. Looking up only one name means a
      whole mission's surgery times come back empty, quietly.
    - A field can exist with value None (the extractor correctly reporting
      "this box was blank"). `if _raw_field(...)` is True for that dict, so
      an unfilled box used to read as a filled one. Presence is not a
      value."""
    for k in keys:
        f = _raw_field(page, k)
        if f and f.get("value") not in (None, ""):
            return f
    return None


def _value(page: dict, *keys):
    f = _raw_any(page, *keys)
    return f.get("value") if f else None


# --- Reading dates and times off the forms ---------------------------------
# These arrive as whatever the person wrote: "12/8/25", "12/8", "0937",
# "09:37", "12/8 12:35", "9:40 am". Parsing is deliberately forgiving about
# format and deliberately strict about ambiguity -- anything that cannot be
# read confidently returns None so the caller can flag it. Nothing here ever
# falls back to today's date or to a plausible-looking substitute.
_TIME_RE = re.compile(r"\b([0-2]?\d)\s*[:.]?\s*([0-5]\d)\b")
_DATE_RE = re.compile(r"\b(\d{1,2})\s*[/\-.]\s*(\d{1,2})(?:\s*[/\-.]\s*(\d{2,4}))?\b")

# ISO dates must be recognised SEPARATELY and tried FIRST.
#
# This was the single worst bug in the Sierra Leone vet run. _DATE_RE was
# written when the model handed back whatever was written on the paper
# ("12/8/25"), and its first group is \d{1,2}, so it cannot match the 2025
# in "2025-12-08". The model now normalises most dates to ISO before
# returning them -- so _parse_date silently returned None for every
# CLEANLY-READ date, while the messy ones still parsed.
#
# The damage was invisible rather than loud: surgery_date became None, so
# LOS, LOS-hours and the OR/anesthesia anchors all came back blank with a
# "cannot compute: no readable surgery date" flag -- which reads like bad
# handwriting rather than a parser that cannot read its own input format.
# In the 5-packet vet, LOS was blank on the 4 packets with PERFECT dates
# and populated only on the one with a misread date.
#
# ISO is checked before _DATE_RE because _DATE_RE will happily match the
# "12-08" inside "2025-12-08" and quietly drop the century.
_ISO_DATE_RE = re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b")


def _parse_time(text):
    """'HH:MM', or None. Tries every candidate in the string and returns
    the first that is a real clock time, so a stray number earlier in the
    text can't shadow the actual time."""
    if text is None:
        return None
    s = str(text)
    for m in _TIME_RE.finditer(s):
        h, mi = int(m.group(1)), int(m.group(2))
        if h > 23:
            continue
        tail = s[m.end():m.end() + 4]
        if re.match(r"\s*p\.?m", tail, re.I) and h < 12:
            h += 12
        elif re.match(r"\s*a\.?m", tail, re.I) and h == 12:
            h = 0
        return f"{h:02d}:{mi:02d}"
    return None


def _parse_date(text, default_year=None):
    """A date object from a MONTH-FIRST written date, or None.

    Month-first is a mission-level convention, not a universal one -- see
    the Sierra Leone profile's extraction_hints and RULES.md. The one form
    that is written day-first is handled at the point of use, not here."""
    if text is None:
        return None
    iso = _ISO_DATE_RE.search(str(text))
    if iso:
        try:
            return date(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)))
        except ValueError:
            # An ISO-SHAPED string with impossible numbers ("2025-13-08") is
            # a misread, not a date. Return None so it is flagged, rather
            # than falling through to _DATE_RE, which would match the
            # "13-08" and turn a nonsense month into a plausible day.
            return None
    m = _DATE_RE.search(str(text))
    if not m:
        return None
    return _date_from_match(m, default_year)


def _date_from_match(m, default_year):
    month, day = int(m.group(1)), int(m.group(2))
    year = m.group(3)
    if year is None:
        if default_year is None:
            return None
        year = default_year
    else:
        year = int(year)
        if year < 100:
            year += 2000
    try:
        return date(year, month, day)
    except ValueError:
        return None


def parse_date_day_first(text, default_year=None):
    """The DAY-FIRST reading of the same digits _parse_date would read
    month-first, or None. Used only to CHECK whether a date that fails the
    mission-window test would be plausible under the other convention --
    never to silently correct a value (rule 18: transcribed, not inferred).

    See run_mission.py's calibrate_from_jsons, which uses this to notice
    when a mission has an inconsistent day/month convention on a
    particular form (as Sierra Leone 2024's Floor Flowsheet does) and to
    write a warning into the profile's extraction_hints automatically."""
    if text is None:
        return None
    if _ISO_DATE_RE.search(str(text)):
        return None  # ISO dates aren't ambiguous; nothing to swap
    m = _DATE_RE.search(str(text))
    if not m:
        return None
    day, month = int(m.group(1)), int(m.group(2))
    year = m.group(3)
    if year is None:
        if default_year is None:
            return None
        year = default_year
    else:
        year = int(year)
        if year < 100:
            year += 2000
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _split_date_time(text, default_year=None):
    """('Date/Time Arrived: 12/8 12:35') -> (date(2025,12,8), '12:35').

    The date is cut out of the string before the time is looked for.
    Otherwise the digits of the date get read as a time -- '12/8/25 09:40'
    parses as 25:09 if you search the whole string naively.

    ISO input is handled first and excised whole. Letting _DATE_RE loose on
    "2025-12-09 09:55" was actively dangerous: it matches the "12-09"
    inside the ISO date and cuts only that out, leaving "2025 09:55" -- in
    which the leftover "2025" parses as the valid clock time 20:25 and is
    returned in preference to the real 09:55, because _parse_time takes the
    first candidate it finds. That is the worst class of bug in this
    pipeline: not a blank, not a flag, but a confident wrong number."""
    if text is None:
        return None, None
    s = str(text)
    d = None
    m = _ISO_DATE_RE.search(s) or _DATE_RE.search(s)
    if m:
        if m.re is _ISO_DATE_RE:
            try:
                d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except ValueError:
                d = None
        else:
            d = _date_from_match(m, default_year)
        s = s[:m.start()] + " " + s[m.end():]
    return d, _parse_time(s)


def _combine(d, t):
    """date + 'HH:MM' -> datetime, or None if either half is missing."""
    if d is None or not t:
        return None
    try:
        return datetime.combine(d, datetime.strptime(t, "%H:%M").time())
    except Exception:
        return None


def _categorize_procedure(procedure_text) -> tuple:
    """Maps free-text procedure descriptions into a small, fixed set of
    categories for the summary sheet to group by. Order matters -- more
    specific categories are checked first. Returns (value, confidence).
    Unmatched text gets a low-confidence "Other/Uncategorized" bucket
    rather than silently miscategorizing.

    The abbreviations matter as much as the spelled-out words. The 2025
    forms are written in theatre shorthand -- "RIH plug/patch", "IHR",
    "B/L IH", "VHR" -- and matching only on "inguinal" AND "hernia" put
    an entire inguinal-hernia mission into Other/Uncategorized. That is
    worse than an obvious crash: the bucket looks like a data-entry
    problem rather than the wrong answer it actually is.

    Every abbreviation is matched with word boundaries, so the "ih"
    inside a word like "PATHOLOGY" or "WHILE" cannot trigger anything.
    The old two-word rule is also relaxed to one word, because "L
    inguinal repair" and "hernie inguinale" are both written on these
    forms without the word "hernia" next to it."""
    if not procedure_text:
        return None, "low"
    t = str(procedure_text).lower()

    def has(*patterns) -> bool:
        return any(re.search(p, t) for p in patterns)

    # Gallbladder. "lap chole" / "chole" are the usual shorthand.
    if has(r"cholecystectomy", r"\blap\s*chole\b", r"\bchole\b"):
        # "open" is only meaningful here; a laparoscopic case converted to
        # open is still an open cholecystectomy, which the same test catches.
        return ("Open Cholecystectomy" if "open" in t
                else "Laparoscopic Cholecystectomy"), "high"

    # Umbilical / ventral BEFORE inguinal: a packet listing both should
    # count once, and the umbilical/ventral repair is the larger operation.
    # UH/UHR/PUH = umbilical, VH/VHR = ventral, IVH = incisional/ventral.
    if has(r"umbilical", r"ventral", r"epigastric", r"incisional",
           r"\bp?uhr?\b", r"\bi?vhr?\b"):
        return "Umbilical/Ventral Hernia Repair", "high"

    if has(r"femoral", r"\b[rlb]?fhr?\b"):
        return "Femoral Hernia Repair", "high"

    # IH, IHR, RIH, LIH, BIH, RIHR ... all one category. The laterality is
    # captured properly in hernia_laterality; it is not a category.
    if has(r"inguinal", r"\b[rlb]?ihr?\b"):
        return "Inguinal Hernia Repair", "high"

    if has(r"hydrocele"):
        return "Hydrocelectomy", "high"
    if has(r"hysterectomy", r"\b[tv]?ah\b"):
        return "Hysterectomy", "high"
    if has(r"prostatectomy", r"\bturp\b", r"\bsimple prostate\b"):
        return "Prostatectomy", "high"
    if has(r"lipoma"):
        return "Lipoma Excision", "high"

    # Anything still containing "hernia" is a hernia repair of a kind this
    # list does not name -- better an honest "Other Hernia Repair" than a
    # guess at which kind.
    if has(r"hernia", r"\bhernie\b"):
        return "Other Hernia Repair", "medium"

    # Deliberately last and deliberately only "medium": "excision of mass"
    # is a real category on the 2025 OR Record, but "excision" on its own
    # is also how a dozen other operations get described.
    if has(r"mass excision", r"excision of (?:a |the )?mass", r"\bmass\b",
           r"\bexcision\b", r"\bexcisional biopsy\b"):
        return "Mass Excision", "medium"

    return "Other/Uncategorized", "low"


def mrn_sort_key(mrn: str):
    """Sorts 'PR19-1', 'PR19-01', 'PR19-10' by patient NUMBER, not text,
    so a sheet stays correctly ordered no matter what order packets were
    processed in."""
    match = re.search(r"(\d+)\s*$", mrn or "")
    return (int(match.group(1)) if match else float("inf"), mrn or "")


def _page_clock_key(page: dict, time_keys) -> Optional[tuple]:
    """(date, minutes-since-midnight) for a timed page, from its own Date
    box and the first filled time among time_keys; None if no time parses.
    A missing/unparseable date sorts as 'same day' (date.min) so two pages
    from one admission are still ordered by clock time."""
    t = None
    for k in time_keys:
        f = _raw_field(page, k)
        if f and f.get("value"):
            t = _parse_time(f["value"])
            if t:
                break
    if not t:
        return None
    d_f = _raw_field(page, "date")
    d = _parse_date(d_f["value"]) if d_f and d_f.get("value") else None
    return (d or date.min, int(t[:2]) * 60 + int(t[3:5]))


def _chronological(pages: List[dict], time_keys) -> List[dict]:
    """Earliest operative episode first. Only reorders when EVERY filled
    page has a usable clock key -- anything less certain keeps scan order
    (the long-standing default), because a half-informed reorder is a new
    way to pick the wrong primary page. Blank pages stay at the end."""
    filled = [p for p in pages if _page_has_content(p)]
    blank = [p for p in pages if not _page_has_content(p)]
    if len(filled) < 2:
        return pages
    keys = [_page_clock_key(p, time_keys) for p in filled]
    if any(k is None for k in keys):
        return pages
    order = sorted(range(len(filled)), key=lambda i: (keys[i], i))
    return [filled[i] for i in order] + blank


def _mins(value) -> Optional[int]:
    t = _parse_time(value) if value else None
    return int(t[:2]) * 60 + int(t[3:5]) if t else None


def _anes_checked_against_or(anes: dict, or_page: dict, hp_pages: List[dict], raw: dict) -> dict:
    """reconcile_options.or_record_timing_fallback (Ghana 2026 October).

    The Anesthesia Record is the default source for the surgery date and
    the anesthesia/procedure clock times. On this mission it was the less
    reliable form: 4 of 5 vetted packets had its Date box disagree with
    every other dated form (GH26O-904, -A01), its STOP box blank or written
    across both STOP cells (GH26O-905), or a misread STOP time that landed
    after the patient had already arrived in PACU (GH26O-904: 12:54 vs PACU
    12:19). Returns a COPY of the Anesthesia Record page in which only the
    values that fail an independent check are replaced -- every other
    value, and every downstream rule that prefers this form, is untouched:

      * Date: replaced by the OR Record's date only when the two disagree
        AND the OR Record's date is backed by more of the packet's other
        dated forms (H&P Brief Op Note, PACU Flowsheet, Floor Flowsheet)
        than the Anesthesia Record's is.
      * Times: an anesthesia/procedure time is checked against the OR
        Record's In room (lower bound) and the earlier of OR Out of room /
        PACU arrival (upper bound), with 10 minutes' tolerance. A time
        outside that window is replaced by the OR Record's own box when
        THAT value passes the same check; if the OR Record box is blank or
        also fails, the value is BLANKED with the reason kept -- a time
        known to be impossible is not left in a cell to be averaged.

    Every replaced or blanked value carries a flag_reason naming both
    readings, at medium (replaced) or low (blanked) confidence."""
    if not anes or not or_page:
        return anes
    eff = dict(anes)
    eff["fields"] = dict(anes.get("fields") or {})
    label = anes.get("page_label", "Anesthesia Record")
    or_label = or_page.get("page_label", "OR Record")

    # ---- date -----------------------------------------------------------
    a_f, o_f = _raw_field(anes, "date"), _raw_field(or_page, "date")
    a_d = _parse_date(a_f["value"]) if a_f and a_f.get("value") else None
    o_d = _parse_date(o_f["value"]) if o_f and o_f.get("value") else None
    if a_d and o_d and a_d != o_d:
        votes = []
        for p in hp_pages + _find_pages(raw, "PACU Flowsheet"):
            f = _raw_field(p, "date")
            if f and f.get("value"):
                votes.append(_parse_date(f["value"], o_d.year))
        for p in _find_pages(raw, "Floor Flowsheet"):
            f = _raw_field(p, "floor_arrival")
            if f and f.get("value"):
                d, _ = _split_date_time(f["value"], o_d.year)
                votes.append(d)
        n_or, n_an = votes.count(o_d), votes.count(a_d)
        if n_or <= n_an:
            pass
        else:
            eff["fields"]["date"] = {
                "value": o_d.isoformat(), "confidence": "medium",
                "flag_reason": (f"{label} Date box reads {a_f['value']} but {or_label} reads "
                                f"{o_f['value']}, and {n_or} other dated form(s) in the packet agree "
                                f"with the OR Record ({n_an} with the Anesthesia Record) -- OR Record "
                                f"date used (reconcile option or_record_timing_fallback)")}

    # One date format in the column: a parseable Date box that was kept is
    # written back as ISO (same value, same confidence and flag).
    kept = eff["fields"].get("date")
    if kept is a_f and a_d:
        eff["fields"]["date"] = {**a_f, "value": a_d.isoformat()}

    # ---- times ----------------------------------------------------------
    in_room = _mins(_value(or_page, "in_room"))
    uppers = [m for m in (_mins(_value(or_page, "out_of_room")),) if m is not None]
    pacu_arrivals = [m for m in (_mins(_value(p, "pacu_arrival_time"))
                                 for p in _find_pages(raw, "PACU Flowsheet")) if m is not None]
    if in_room is not None:
        pacu_arrivals = [m for m in pacu_arrivals if m >= in_room]
    if pacu_arrivals:
        uppers.append(min(pacu_arrivals))
    lo = in_room - 10 if in_room is not None else None
    hi = min(uppers) + 10 if uppers else None
    if lo is None and hi is None:
        return eff
    if lo is not None and hi is not None and hi < lo:
        return eff  # crosses midnight or a misread bound -- don't judge with it

    def ok(m):
        return m is not None and (lo is None or m >= lo) and (hi is None or m <= hi)

    def _hhmm(m):
        return f"{(m // 60) % 24:02d}:{m % 60:02d}"
    window = (f"{'?' if in_room is None else _hhmm(in_room)} in room to "
              f"{'?' if not uppers else _hhmm(min(uppers))} out of room/PACU arrival")
    for key in ("anesthesia_start", "procedure_start", "procedure_end", "anesthesia_end"):
        a = _raw_field(anes, key)
        if not (a and a.get("value")):
            continue
        a_m = _mins(a["value"])
        if a_m is None or ok(a_m):
            continue
        o = _raw_field(or_page, key)
        o_m = _mins(o["value"]) if o and o.get("value") else None
        why = (f"{label} {key.replace('_', ' ')} reads {a['value']}, outside the OR episode "
               f"({window}, +/-10 min) -- likely a misread or a time written across the wrong box")
        if ok(o_m):
            eff["fields"][key] = {"value": o["value"], "confidence": "medium",
                                  "flag_reason": f"{why}; {or_label}'s own box ({o['value']}) used instead "
                                                 f"(reconcile option or_record_timing_fallback)"}
        else:
            eff["fields"][key] = {"value": None, "confidence": "low",
                                  "flag_reason": f"{why}; {or_label}'s own box is "
                                                 f"{'blank' if o_m is None else repr(o['value']) + ', which fails the same check'}"
                                                 f" -- left blank rather than keep an impossible time"}
    return eff


SOCIAL_COLUMNS = {  # case-log column -> H&P extraction key
    "social_occupation": "occupation",
    "social_etoh": "etoh",
    "social_substance_use": "substance_use",
    "social_nicotine": "nicotine",
    "travel_from": "travel_from",
    "travel_distance": "travel_distance",
}


def _reconcile_social_history(fields: dict, hp_pages: List[dict]) -> None:
    """Social-history block of the newer H&P (Ghana 2026 Oct onward):
    Occupation, ETOH / Substance use / Nicotine (Denies / Past / Current),
    Travel from / distance. Read from the first H&P page that has a value.
    A packet whose H&P never had this block (every older mission, or an
    older JSON extracted before these keys existed) gets an unflagged
    blank: the form did not ask, so nothing is missing."""
    for col, key in SOCIAL_COLUMNS.items():
        present = [hp for hp in hp_pages if key in ((hp.get("fields") or {}))]
        if not present:
            fields[col] = _fv(None, "high", "H&P")
            continue
        hit = next((hp for hp in present if _value(hp, key) not in (None, "")), None)
        if hit:
            f = _raw_field(hit, key)
            fields[col] = _fv(f["value"], f.get("confidence", "high"), hit.get("page_label"),
                              f.get("flag_reason"))
        else:
            f = _raw_field(present[0], key)
            reason = f.get("flag_reason") if f else None
            if reason and "no social history block" in reason.lower():
                fields[col] = _fv(None, "high", present[0].get("page_label"))
            else:
                fields[col] = _fv(None, "low", present[0].get("page_label"),
                                  reason or "left blank on the H&P social-history block")


EURAHS_DOMAINS = {
    "pain": ("pain_rest", "pain_activity", "pain_worst_week"),
    "restriction": ("restrict_daily", "restrict_outside", "restrict_sports", "restrict_heavy_labour"),
    "cosmetic": ("cosmetic_abdomen_shape", "cosmetic_hernia_site"),
}


def _eurahs_item(raw_val):
    """0-10 integer, 'X' (not done), or None for anything else."""
    if raw_val is None:
        return None
    s = str(raw_val).strip().upper()
    if s == "X":
        return "X"
    try:
        n = int(float(s))
    except ValueError:
        return None
    return n if 0 <= n <= 10 and float(s) == n else None


def _reconcile_eurahs(fields: dict, raw: dict, surgery_date) -> None:
    """EuraHS-QoL questionnaire block (Ghana 2026 Oct onward).

    Columns hold the PRE-operative questionnaire: the 9 items, the three
    validated domain scores and the total. Domain score = plain sum of its
    items (pain 0-30, restriction 0-40, cosmetic 0-20; total 0-90), and
    is computed ONLY when every item in the domain is a number. A missing
    item or an "X" (activity not done) leaves that domain -- and therefore
    the total -- blank with the reason in the flag, rather than summing a
    partial domain that would read as a lower (better) score. How to score
    "not done" items is a team decision (see VET_GATE_REPORT_GH26O.md);
    this is the conservative default until it is made.

    A post-operative questionnaire, if one is ever present, is NOT merged
    into these columns; it stays in the raw JSON and is pointed to from the
    qol_timepoint flag. Date of birth also stays in the JSON (an
    identifier); it is used here only to cross-check the H&P age."""
    cols = (["qol_timepoint"] + [k for ks in EURAHS_DOMAINS.values() for k in ks]
            + ["qol_pain_score", "qol_restriction_score", "qol_cosmetic_score", "qol_total_score"])
    pages = [p for p in raw["pages"] if p["page_type"] == "EuraHS-QoL" and _page_has_content(p)]
    if not pages:
        for c in cols:
            fields[c] = _fv(None, "high", "EuraHS-QoL")
        return
    pre = [p for p in pages if str(_value(p, "form_timepoint") or "pre").lower().startswith("pre")]
    post = [p for p in pages if p not in pre]
    # A questionnaire scanned twice (GH26O-906: a cropped half-page plus the
    # full page) -> use the most complete copy, not simply the first.
    _items = [k for ks in EURAHS_DOMAINS.values() for k in ks]
    pre = sorted(pre, key=lambda p: -sum(1 for k in _items if _value(p, k) not in (None, "")))
    page = pre[0] if pre else None
    if page is None:
        for c in cols:
            fields[c] = _fv(None, "high", "EuraHS-QoL")
        fields["qol_timepoint"] = _fv(None, "low", post[0].get("page_label"),
                                      "only a POST-operative EuraHS-QoL is in this packet; it is kept in "
                                      "the raw JSON, not in these pre-op columns")
        return
    lbl = page.get("page_label", "EuraHS-QoL")
    tp = _raw_field(page, "form_timepoint")
    tp_notes = []
    if len(pre) > 1:
        tp_notes.append(f"{len(pre)} pre-op EuraHS-QoL pages; the most complete "
                        f"({page.get('page_label')}) is used -- compare the others on the scan")
    if post:
        tp_notes.append("a POST-operative EuraHS-QoL is also in this packet "
                        f"({', '.join(p.get('page_label', '') for p in post)}) -- raw JSON only")
    marks = _value(page, "unassigned_marks")
    if marks:
        tp_notes.append(f"marks not tied to any question: {marks}")
    fields["qol_timepoint"] = _fv(
        "pre", (tp or {}).get("confidence", "high") if not tp_notes else "medium", lbl,
        "; ".join(tp_notes) or (tp or {}).get("flag_reason"))

    items = {}
    for keys in EURAHS_DOMAINS.values():
        for k in keys:
            f = _raw_field(page, k)
            v = _eurahs_item(f.get("value")) if f else None
            items[k] = v
            if f is None:
                fields[k] = _fv(None, "low", lbl, "item not extracted")
            elif v is None and f.get("value") not in (None, ""):
                fields[k] = _fv(None, "low", lbl, f"read as {f['value']!r}, not 0-10 or X -- left blank")
            elif v is None:
                fields[k] = _fv(None, "low", lbl, f.get("flag_reason") or "no number circled on this row")
            else:
                fields[k] = _fv(v, f.get("confidence", "high"), lbl, f.get("flag_reason"))

    totals_ok = True
    for dom, keys in EURAHS_DOMAINS.items():
        col = f"qol_{dom}_score"
        vals = [items[k] for k in keys]
        if all(isinstance(v, int) for v in vals):
            conf = _weaker(*(fields[k].confidence for k in keys))
            fields[col] = _fv(sum(vals), conf, lbl,
                              None if conf == "high" else "built on an item read with less than high confidence")
        else:
            totals_ok = False
            gaps = [f"{k} = {'not done (X)' if items[k] == 'X' else 'blank'}" for k in keys
                    if not isinstance(items[k], int)]
            fields[col] = _fv(None, "low", lbl, f"not scored -- {len(gaps)} of {len(keys)} items "
                                                f"unusable: {'; '.join(gaps)}")
    if totals_ok:
        parts = [fields[f"qol_{d}_score"] for d in EURAHS_DOMAINS]
        conf = _weaker(*(p.confidence for p in parts))
        fields["qol_total_score"] = _fv(sum(p.value for p in parts), conf, lbl,
                                        None if conf == "high" else "built on an item read with less than high confidence")
    else:
        fields["qol_total_score"] = _fv(None, "low", lbl, "not scored -- at least one domain is incomplete")

    # Age cross-check against the questionnaire's date of birth.
    dob_raw = _value(page, "date_of_birth")
    dob = parse_date_day_first(dob_raw) if dob_raw else None
    age_fv = fields.get("age")
    if dob and surgery_date and age_fv and isinstance(age_fv.value, (int, float)):
        implied = surgery_date.year - dob.year - ((surgery_date.month, surgery_date.day) < (dob.month, dob.day))
        if abs(implied - age_fv.value) > 1:
            note = (f"EuraHS-QoL date of birth ({dob_raw}) implies age {implied} at surgery, "
                    f"H&P says {age_fv.value}")
            age_fv.flag_reason = f"{age_fv.flag_reason}; {note}" if age_fv.flag_reason else note
            age_fv.confidence = _weaker(age_fv.confidence, "medium")


def reconcile_packet(raw: dict, options: Optional[dict] = None) -> List[CaseLogRow]:
    """Raw extraction dict for one packet -> exactly one CaseLogRow.
    (A packet with more than one H&P page -- e.g. a same-day
    re-exploration -- is folded into one row, not split.)

    `options` comes from the mission profile's "reconcile_options" block.
    Every option defaults OFF, so archived missions rebuild exactly as they
    were vetted; a mission opts in explicitly. See RULES.md rule 25."""
    opts = options or {}
    packet_id = raw["packet_id"]
    hp_pages = _find_pages(raw, "H&P")
    or_pages = _find_pages(raw, "OR Record", prefer_content=True)
    anes_pages = _find_pages(raw, "Anesthesia Record")
    if opts.get("chronological_or_order"):
        # Ghana 2026 Oct, GH26O-901: a same-day return to OR. The takeback's
        # Anesthesia Record and OR Record were scanned AHEAD of the index
        # operation's, so scan order made the hematoma evacuation the
        # "primary" case and the hernia repair the "secondary" one. The
        # index operation is, by definition, the earliest one.
        or_pages = _chronological(or_pages, ("in_room", "or_time_in", "anesthesia_start",
                                             "procedure_start"))
        anes_pages = _chronological(anes_pages, ("anesthesia_start", "procedure_start"))

    primary_hp = hp_pages[0] if hp_pages else {}
    repeat_hp_pages = hp_pages[1:]
    # Extra H&P pages are NOT, by themselves, evidence of a genuine same-day
    # repeat/takeback procedure. This mission's normal packet structure is an
    # abbreviated ISHI intake card plus a full Brief Op Note, both scanned
    # separately and both independently classified as "H&P" -- describing
    # ONE single operation, just at two levels of detail (e.g. "R IHR" vs.
    # "Right inguinal hernia repair with mesh"). Treating every such packet
    # as a takeback was flagging 30 of 62 packets (48%) in the Ghana 2025-09
    # run, and attaching complication_prefix "immediate post-op recurrence
    # requiring takeback" to every one of them -- a real clinical claim with
    # essentially no evidence behind it.
    #
    # Rule 2c: a genuine same-day secondary procedure gets its OWN separate
    # OR Record and timing, not shared with the primary. So require a second,
    # independent OR Record page in the packet before trusting the extra H&P
    # page(s) as a real second procedure -- that's the one signal that can't
    # be explained away by "card + full note for the same operation". A
    # second Anesthesia Record page was tried too but rejected: in the one
    # Ghana 2025-09 packet that had one (GH25s-901), the second page was a
    # thin partial duplicate of the first (same date, no independent timing,
    # same procedure text) -- not evidence of a second operative episode.
    #
    # Checked against all 62 Ghana 2025-09 packets: this clears 29 of the 30
    # false positives and leaves exactly one (GH25s-902, which has two OR
    # Records AND two Anesthesia Records on the same date, and where the two
    # H&P pages actually disagree on the procedure performed -- TAH+BSO vs.
    # SCH+BSO) correctly flagged for a human to check.
    genuine_repeat_hp_pages = repeat_hp_pages if len(or_pages) > 1 else []

    # genuine_repeat_hp_pages (above) requires BOTH a second H&P page AND a
    # second, independent OR Record page. That's correct for SOURCING the
    # secondary procedure's text off a repeat H&P page when one exists, but
    # it silently drops real same-day secondary procedures on packets that
    # only ever have ONE H&P page in the first place -- this mission's
    # "straight into the full clinical note, no separate intake card"
    # architecture variant (see the mission profile's "TWO INTAKE-PAGE-1
    # VARIANTS" note). Found on Peru 2015 PR15-902 (2026-08 full run): two
    # genuine, independent OR Record pages (a hernia repair, then a
    # separate "Exploration of wound" with its own in-room/out-of-room,
    # surgeon and anesthesia data) but only one H&P page -- so
    # genuine_repeat_hp_pages evaluated to [] and secondary_procedure was
    # silently written as None despite the raw JSON containing the real
    # secondary-procedure data.
    #
    # Per this guard's own rationale above ("a genuine same-day secondary
    # procedure gets its OWN separate OR Record and timing, not shared with
    # the primary"), a second OR Record page COULD be sufficient evidence
    # of a genuine secondary procedure on its own, without needing a repeat
    # H&P page too. But a plain page-count check (`len(or_pages) > 1`) turns
    # out to have the SAME failure mode this whole guard exists to avoid --
    # just for OR Record pages instead of H&P pages. A first pass using bare
    # page count, regression-tested against all 1222 archived packets
    # (2026-08), surfaced 16 "new" secondary procedures; manually checking
    # both OR Record pages on each showed most were NOT genuine second
    # operations:
    #   - a blank second OR Record page (GH14-902, GH18-902, GH24-901,
    #     PR16-905, PR17-901) -- an empty scan, not a second episode
    #   - identical timing, i.e. the literal same episode transcribed twice
    #     (SL18-901: In/Out identical on both pages)
    #   - overlapping timing describing the same operation at two levels of
    #     detail, exactly the intake-card/full-note pattern this guard was
    #     built to reject, just on the OR Record form instead of the H&P
    #     (GH16-902: overlapping windows, "RIHR" vs "Right inguinal hernia
    #     repair with Mesh")
    #   - one page missing its own timing altogether, and the two
    #     procedure descriptions naming the same operation at two levels of
    #     detail (GH16-903 "Hysterectomy"/"Total abdominal hysterectomy",
    #     GH25s-903/-904/-905 and PR15-903 likewise)
    # Only a handful (PR15-902, GH25s-906, SL17-901, SL24-901) had a second OR
    # Record page with its own real timing that didn't overlap the
    # primary's -- i.e. hard evidence the patient was physically in the OR
    # twice. So the bar is set on TWO tiers, timing first and text only as
    # a fallback when timing alone can't decide:
    #   1. If BOTH OR Record pages have a real, parseable In room/Out of
    #      room window: genuine only if those windows don't overlap or
    #      exactly match (two different times cannot both be true of one
    #      patient in one OR episode).
    #   2. If only the SECOND page has a real window (the first page's own
    #      timing box was left blank -- e.g. PR15-902, where the primary
    #      hernia-repair OR Record never got an In/Out time at all): fall
    #      back to comparing the two procedure descriptions. Only treated
    #      as genuine if they don't look like the same operation described
    #      at two levels of detail.
    #   3. Anything else (no real timing on the second page at all, or no
    #      real content on the first page to compare against) is left
    #      alone rather than guessed at, matching this pipeline's general
    #      "if uncertain, don't confidently assert" approach.
    # This does not change genuine_repeat_hp_pages itself or its Ghana
    # 2025-09 false-positive fix -- see secondary_procedure and
    # secondary_surgery_time_mins below for the new fallback branches.
    def _or_page_time_window(or_page):
        """(start_minutes, end_minutes) since midnight for an OR Record
        page's own In room/Out of room, or None if either is missing or
        unparseable."""
        in_f = _raw_any(or_page, "in_room", "or_time_in")
        out_f = _raw_any(or_page, "out_of_room", "or_time_out")
        if not (in_f and in_f.get("value") and out_f and out_f.get("value")):
            return None
        t_in, t_out = _parse_time(in_f["value"]), _parse_time(out_f["value"])
        if not (t_in and t_out):
            return None
        m_in = int(t_in[:2]) * 60 + int(t_in[3:5])
        m_out = int(t_out[:2]) * 60 + int(t_out[3:5])
        if m_out < m_in:
            m_out += 24 * 60  # crossed midnight
        return (m_in, m_out)

    _OR_PROC_FILLER_WORDS = {
        "right", "left", "bilateral", "with", "w", "wo", "without", "mesh",
        "total", "abdominal", "other", "open", "the", "and", "of", "a", "an",
        "repair", "procedure",
    }
    _OR_PROC_ABBREV = {
        "tah": "total abdominal hysterectomy",
        "bso": "bilateral salpingo oophorectomy",
        "bs": "bilateral salpingectomy",
        "sch": "subtotal hysterectomy",
        "ihr": "inguinal hernia repair",
        "lihr": "left inguinal hernia repair",
        "rihr": "right inguinal hernia repair",
    }

    def _or_procedures_look_like_same_op(text_a, text_b):
        """True if two procedure strings plausibly describe the SAME
        operation at two levels of detail (e.g. 'IHR' vs 'IHR w/ mesh',
        'Hysterectomy' vs 'Total abdominal hysterectomy'), as opposed to
        two genuinely different procedures. Deliberately crude -- expands
        the handful of abbreviations this pipeline has actually seen
        reused across full-word and abbreviated procedure names (found
        checking GH25s-907, "TAH+BS" vs "Hysterectomy" -- same operation,
        but sharing no literal words until "TAH" is expanded), strips
        common qualifier words and punctuation, then checks for meaningful
        shared tokens. Only consulted when timing alone can't settle the
        question (tier 2 above)."""
        def _tokens(s):
            s = re.sub(r"[^a-z0-9\s]", " ", (s or "").lower())
            words = []
            for w in s.split():
                words.extend(_OR_PROC_ABBREV.get(w, w).split())
            return {t for t in words if t and t not in _OR_PROC_FILLER_WORDS}
        ta, tb = _tokens(text_a), _tokens(text_b)
        if not ta or not tb:
            return False
        shared = ta & tb
        return bool(shared) and len(shared) >= min(len(ta), len(tb)) * 0.5

    primary_or = or_pages[0] if or_pages else {}
    secondary_or_page = or_pages[1] if len(or_pages) > 1 else {}
    _primary_or_proc = _raw_field(primary_or, "surgical_procedure")
    _secondary_or_proc = _raw_field(secondary_or_page, "surgical_procedure")
    _primary_or_window = _or_page_time_window(primary_or)
    _secondary_or_window = _or_page_time_window(secondary_or_page)
    _primary_or_has_content = bool(_primary_or_window is not None
                                    or (_primary_or_proc and _primary_or_proc.get("value")))
    _secondary_or_has_content = bool(_secondary_or_window is not None
                                      or (_secondary_or_proc and _secondary_or_proc.get("value")))

    if len(or_pages) <= 1 or not _secondary_or_has_content or not _primary_or_has_content:
        has_genuine_secondary_procedure = False
    elif _primary_or_window is not None and _secondary_or_window is not None:
        overlap = (_primary_or_window[0] < _secondary_or_window[1]
                   and _secondary_or_window[0] < _primary_or_window[1])
        has_genuine_secondary_procedure = _primary_or_window != _secondary_or_window and not overlap
    elif _secondary_or_window is not None:
        has_genuine_secondary_procedure = not _or_procedures_look_like_same_op(
            _primary_or_proc.get("value") if _primary_or_proc else None,
            _secondary_or_proc.get("value") if _secondary_or_proc else None,
        )
    else:
        has_genuine_secondary_procedure = False

    primary_anes = anes_pages[0] if anes_pages else {}
    if opts.get("or_record_timing_fallback"):
        primary_anes = _anes_checked_against_or(primary_anes, primary_or, hp_pages, raw)
    pae_pages = _find_pages(raw, "Pre-Anesthesia Evaluation")
    primary_pae = pae_pages[0] if pae_pages else {}

    fields = {}

    # _hp_value_across_pages() is defined here (rather than just above the PMH
    # loop further down, which is where it was originally introduced and
    # where the long comment block explaining its rationale still lives) so
    # that the age/gender block immediately below can also use it -- moved
    # up on 2026-09-07 during the Ghana May 2026 age/gender fix (see comment
    # on that block for why). See the comment above the PMH loop further down
    # for the full history/rationale of this helper.
    def _hp_value_across_pages(key):
        """Returns (field_dict, page_label) for the first H&P page with a
        non-null value for `key`, or (None, last_field_dict) if every H&P
        page that had this key at all came back null (or there was no
        H&P page in the packet)."""
        last_null_f = None
        for hp in hp_pages:
            f = _raw_field(hp, key)
            if f is None:
                continue
            if f.get("value") is not None:
                return f, hp.get("page_label")
            last_null_f = f
        return None, last_null_f

    # age/gender used to be read ONLY from primary_hp (hp_pages[0]), same bug
    # shape as the PMH/PSH/preop_dx/postop_dx bugs documented below. Found at
    # the Ghana May 2026 vet gate, 2026-09-07: GH26m-901 has two H&P pages --
    # page 1 is an illegible/blank duplicate (every field null, "illegible"
    # flag_reasons) and page 2 is fully legible with real data (age=53,
    # gender=F, etc). Because primary_hp resolved to page 1, age_f["value"]
    # was None, and the old "cross-page conflict" check below then found
    # page 2's real age=53 and treated the None-vs-53 disagreement as a
    # genuine CONFLICT -- forcing Age to blank at low confidence with a
    # bogus "conflicting" note, even though there was no real conflict at
    # all: one page simply couldn't be read and the other could. That's
    # exactly the failure mode _hp_value_across_pages() exists to prevent
    # (see its docstring/history above, first found on GH25s-908/05), so age
    # and gender are now resolved the same way PMH/PSH/preop_dx/postop_dx
    # are: first non-null value across ALL H&P pages, not just hp_pages[0].
    # The cross-page conflict check is kept, but now only fires against a
    # genuinely different NON-NULL value on some other page in the packet
    # (e.g. the Pre-Anesthesia Eval's own HPI-line age mention), excluding
    # whichever H&P page actually supplied the resolved value -- so a real
    # two-value disagreement is still caught and flagged, but an illegible
    # duplicate page can no longer suppress a real reading found elsewhere.
    age_f, age_source = _hp_value_across_pages("age")
    gender_f, gender_source = _hp_value_across_pages("gender")

    age_conflict_note = None
    if age_f:
        for p in raw["pages"]:
            if p.get("page_label") == age_source:
                continue
            other_age = _raw_field(p, "age")
            if other_age and other_age.get("value") not in (None, age_f["value"]):
                age_conflict_note = (f"{p.get('page_label', p['page_type'])} reports age "
                                      f"{other_age['value']}, conflicting with H&P's {age_f['value']}")
                break
    if age_conflict_note:
        fields["age"] = _fv(age_f["value"], "low", "H&P", age_conflict_note)
    else:
        fields["age"] = _fv(age_f["value"] if age_f else None,
                             age_f.get("confidence", "high") if age_f else "low",
                             "H&P", age_f.get("flag_reason") if age_f else "age not found")
    fields["gender"] = _fv(gender_f["value"] if gender_f else None,
                            gender_f.get("confidence", "high") if gender_f else "low", "H&P")

    # PMH checkbox row (added 2026-08). Populated on ANY mission whose H&P
    # prints the explicit "PMH: [] HTN [] DMII [] COPD [] BPH" + "[] Other"
    # checkbox row -- see extract.py's H&P prompt and schema.py's column
    # definitions. Missions whose form doesn't print that row never produce
    # these keys at all, so _raw_field returns None below and the columns
    # come back blank+unflagged (via the completeness guard at the bottom
    # of this function) -- not a guess, and not an "N".
    # Fallback (added 2026-08, ahead of the GH24 2024-09 vet gate): some form
    # templates (e.g. Ghana 2024-09) print no PMH checkbox row on the H&P at
    # all, but DO have a "PMH & Systems Review" table on the packet's own
    # Pre-Anesthesia Evaluation page instead (categorized by body system --
    # Cardiovascular/Pulmonary/Endocrine/Renal/Hepatic/Other -- see
    # extract.py's Pre-Anesthesia Evaluation prompt). When the H&P's own
    # field is genuinely absent -- either the key is missing entirely (no
    # H&P page at all) or the model explicitly reported "no PMH checkbox row
    # on this form template" -- fall back to the PAE page's equivalent
    # field, tagging flag_reason so a reviewer knows the value came from a
    # different page than usual. If the PAE has no usable value either, the
    # H&P's own absence reason (or a combined one) is preserved so
    # flag_pmh_row_inconsistency() below still has something to check.
    # Some packets scan the SAME physical H&P page more than once -- e.g.
    # once with a routing/ID sticker or a "please return" card stapled over
    # the corner that happens to obscure the printed PMH/PSH strip, once
    # without it -- and the readable copy is not guaranteed to land in
    # hp_pages[0]. First seen on GH25s-902 and GH25s-908 (Ghana 2025-09 vet
    # gate): page 1 in each case had the strip covered and the model
    # correctly reported it couldn't read it, while page 2 (the same
    # physical page, obstruction out of the way) had the real checklist
    # filled in -- but the old code only ever looked at hp_pages[0].
    #
    # Rather than matching the model's specific wording for "couldn't read
    # it" (which drifted across packets -- "no PMH checkbox row on this
    # form template" vs. "PMH checkbox row not visible on this page - page
    # cropped/obscured by attached cards" vs. "no PSH line visible on this
    # form template" -- and any phrase-matching approach is one rewording
    # away from silently missing a case again), this leans on the schema
    # convention directly: per schema.py, these fields are only ever null
    # when the row/line itself couldn't be read (missing, obscured, or
    # illegible) -- a legibly-present-but-unticked box is always reported
    # as "N", never null. So ANY null value from an H&P page means "this
    # page didn't have a usable answer", full stop, regardless of why --
    # and it's always safe to keep looking at the packet's other H&P pages
    # (then the Pre-Anesthesia Evaluation fallback) for a real answer
    # before accepting the null. (_hp_value_across_pages() itself is defined
    # up above, just after `fields = {}`, so the age/gender block can use it
    # too -- see the comment there for why it was moved up.)
    for pmh_col in ("pmh_htn", "pmh_dmii", "pmh_copd", "pmh_bph", "pmh_other"):
        pmh_f, pmh_source = _hp_value_across_pages(pmh_col)
        if pmh_f:
            fields[pmh_col] = _fv(pmh_f["value"], pmh_f.get("confidence", "high"),
                                   pmh_source, pmh_f.get("flag_reason"))
        else:
            pae_f = _raw_field(primary_pae, pmh_col)
            if pae_f and pae_f.get("value") is not None:
                fallback_reason = ("sourced from Pre-Anesthesia Evaluation's PMH & "
                                    "Systems Review table (H&P had no usable value "
                                    "for this field on any of its page(s))")
                if pae_f.get("flag_reason"):
                    fallback_reason += f"; {pae_f['flag_reason']}"
                fields[pmh_col] = _fv(pae_f["value"], pae_f.get("confidence", "medium"),
                                       primary_pae.get("page_label"), fallback_reason)
            elif pmh_source is not None:
                # No H&P page had a usable value, and the PAE fallback came
                # up empty too -- keep that a visible, flagged fact rather
                # than letting it silently blank out.
                combined_reason = pmh_source.get("flag_reason") or _PMH_NO_ROW_REASON
                combined_reason += ("; no usable PMH & Systems Review table found "
                                     "on Pre-Anesthesia Evaluation either")
                fields[pmh_col] = _fv(None, "low", primary_hp.get("page_label"), combined_reason)
            # else: no H&P page and no PAE value -- leave blank+unflagged,
            # consistent with the existing completeness-guard convention.

    # PSH free text (added 2026-08, same vet gate as PMH). Plain pass-through
    # -- no Y/N convention, no "checklist" logic, see extract.py's H&P prompt
    # and schema.py's column definition. Same across-all-H&P-pages search
    # and Pre-Anesthesia Evaluation fallback as PMH above, for the same
    # reason (GH25s-908/05, Ghana 2025-09 vet gate).
    psh_f, psh_source = _hp_value_across_pages("psh")
    if psh_f:
        fields["psh"] = _fv(psh_f["value"], psh_f.get("confidence", "high"),
                             psh_source, psh_f.get("flag_reason"))
    else:
        pae_psh_f = _raw_field(primary_pae, "psh")
        if pae_psh_f and pae_psh_f.get("value") is not None:
            fallback_reason = ("sourced from Pre-Anesthesia Evaluation (H&P had no "
                                "usable PSH value on any of its page(s))")
            if pae_psh_f.get("flag_reason"):
                fallback_reason += f"; {pae_psh_f['flag_reason']}"
            fields["psh"] = _fv(pae_psh_f["value"], pae_psh_f.get("confidence", "medium"),
                                 primary_pae.get("page_label"), fallback_reason)
        elif psh_source is not None:
            combined_reason = psh_source.get("flag_reason") or "no PSH line on this form template"
            combined_reason += ("; no usable PSH entry found on Pre-Anesthesia "
                                 "Evaluation either")
            fields["psh"] = _fv(None, "low", primary_hp.get("page_label"), combined_reason)
        # else: no H&P page and no PAE value -- leave blank+unflagged.

    # preop_dx/postop_dx used to be read ONLY from primary_hp (hp_pages[0]),
    # same bug shape as the PMH/PSH one fixed above. On Peru 2015, packets
    # that lead with the short intake/MRN card (which has no Diagnosis
    # field at all) put the FULL clinical note -- which reliably has real
    # Pre op Dx / Post op Dx lines -- on a LATER H&P page, and primary_hp
    # resolved to the intake card every time. That silently blanked
    # preop_dx/postop_dx on 5 of 5 vetted packets even though the real
    # values were sitting right there on the packet's other H&P page.
    # "procedure" itself was NOT affected (the intake card has its own
    # separate "Procedure:" line that reads correctly), which is exactly
    # why this one was easy to miss until preop_dx/postop_dx were checked
    # directly against the raw JSON. Fixed the same way PMH/PSH were: search
    # every H&P page in the packet for a usable value before giving up.
    preop_f, preop_source = _hp_value_across_pages("preop_dx")
    postop_f, postop_source = _hp_value_across_pages("postop_dx")
    # "procedure" is the Brief Op Note's own "Procedure:" line, filled in
    # AFTER the case. Falls back to "planned_operation" -- the ticked
    # Planned Operation checkbox filled in pre-op -- when the Brief Op Note
    # line was left blank (common: staff sometimes never re-fill it once the
    # pre-op plan already named the operation). NOTE: this fallback key used
    # to read "planned_procedure", which is not a field the H&P prompt in
    # extract.py has ever produced (the real key is "planned_operation") --
    # so the fallback silently never fired. Found at the SL22 vet gate,
    # 2026-08: SL22-902's Procedure came back blank even though "Planned
    # Operation: Inguinal Hernia Repair - R" was clearly ticked. Both are
    # searched across all H&P pages, same reasoning as preop_dx/postop_dx
    # above, for packets where the intake card's own "Procedure:" line is
    # itself blank.
    proc_direct_f, proc_direct_source = _hp_value_across_pages("procedure")
    proc_fallback_f, proc_fallback_source = _hp_value_across_pages("planned_operation")

    # Note on _hp_value_across_pages' return shape (see its docstring above):
    # on a HIT the second element is a page_label STRING; on a MISS it is
    # the field dict from whichever H&P page last had this key at all (or
    # None). The two must not be conflated -- that's exactly the mixed-type
    # bug this comment is here to prevent someone reintroducing.
    if preop_f:
        fields["preop_dx"] = _fv(preop_f["value"], preop_f.get("confidence", "high"),
                                  preop_source, preop_f.get("flag_reason"))
    else:
        combined_reason = (preop_source.get("flag_reason") if preop_source else None) or "no preop dx found on any H&P page"
        fields["preop_dx"] = _fv(None, "low", primary_hp.get("page_label"), combined_reason)

    if postop_f:
        fields["postop_dx"] = _fv(postop_f["value"], postop_f.get("confidence", "high"),
                                   postop_source, postop_f.get("flag_reason"))
    else:
        combined_reason = (postop_source.get("flag_reason") if postop_source else None) or "no postop dx found on any H&P page"
        fields["postop_dx"] = _fv(None, "low", primary_hp.get("page_label"), combined_reason)

    proc_f = proc_direct_f or proc_fallback_f
    if proc_direct_f:
        fields["procedure"] = _fv(proc_direct_f["value"], proc_direct_f.get("confidence", "high"),
                                   proc_direct_source, proc_direct_f.get("flag_reason"))
    elif proc_fallback_f and proc_fallback_f.get("value"):
        fields["procedure"] = _fv(proc_fallback_f["value"], "medium", proc_fallback_source,
                                   "Brief Op Note 'Procedure:' line was blank -- taken from the "
                                   "pre-op 'Planned Operation' checkbox instead; confirm it matches "
                                   "what was actually done")
    else:
        fields["procedure"] = _fv(None, "low", primary_hp.get("page_label"))

    if genuine_repeat_hp_pages:
        repeat_descriptions = []
        for rp in genuine_repeat_hp_pages:
            proc = _raw_field(rp, "procedure")
            # A field can exist with value None (the extractor correctly
            # reporting "this box was blank on this page") -- `if proc:` is
            # True for that dict too, since presence is not a value (see
            # _raw_any()'s docstring above for the same gotcha). Appending
            # that None into repeat_descriptions crashed "; ".join() below
            # with a TypeError the first time a same-day repeat procedure's
            # secondary H&P page left its own Procedure line blank (Sierra
            # Leone 2013 full run, 2026-08).
            if proc and proc.get("value"):
                repeat_descriptions.append(proc["value"])
        fields["secondary_procedure"] = _fv(
            "; ".join(repeat_descriptions) if repeat_descriptions else "repeat procedure, same admission (details not extracted)",
            "medium",
            ", ".join(rp.get("page_label", "") for rp in genuine_repeat_hp_pages),
            "same-day repeat procedure detected via multiple H&P pages AND a second, independent OR "
            "Record page -- confirm wording matches surgeon's intent",
        )
        complication_prefix = "immediate post-op recurrence requiring takeback"
    elif has_genuine_secondary_procedure:
        # No repeat H&P page exists on this packet (see the
        # has_genuine_secondary_procedure comment above -- e.g. PR15-902,
        # Peru 2015), but the second, independent OR Record page is itself
        # sufficient evidence of a genuine same-day secondary procedure.
        # Source the description straight off that page's own
        # surgical_procedure field instead of leaving this blank.
        proc2 = _raw_field(secondary_or_page, "surgical_procedure")
        fields["secondary_procedure"] = _fv(
            proc2["value"] if proc2 and proc2.get("value") else "repeat procedure, same admission (details not extracted)",
            "medium",
            secondary_or_page.get("page_label", ""),
            "same-day repeat procedure detected via a second, independent OR Record page -- no "
            "repeat H&P page exists on this packet, so this is sourced from the OR Record's own "
            "procedure field; confirm wording matches surgeon's intent",
        )
        complication_prefix = "immediate post-op recurrence requiring takeback"
    else:
        fields["secondary_procedure"] = _fv(None, "high")
        complication_prefix = None
    if complication_prefix and opts.get("chronological_or_order"):
        # "recurrence" is a specific clinical claim; a same-day return to OR
        # on this mission was for bleeding (GH26O-901), not recurrence. Say
        # only what the paper establishes: the patient went back to the OR.
        complication_prefix = ("unplanned return to OR during the same admission -- see Secondary "
                               "Procedure and the notes below for the reason")

    def _first_across_hp(key):
        val = _raw_field(primary_hp, key)
        if val:
            return val
        for hp in hp_pages[1:]:
            val = _raw_field(hp, key)
            if val:
                return val
        return None

    # Anesthetist (a NAME) and Anesthesia Type (a TECHNIQUE -- General /
    # Spinal / Regional / Local / etc) are two different things that used
    # to collapse into one "anesthetist" column sourced only from the H&P.
    # That page is filled in pre-op, and its "Anesthesia/anesthesiologist:"
    # line has occasionally been transcribed with the technique bundled
    # into the name (e.g. "Spinal/[name removed]") because that is genuinely what
    # is written on that line for some patients. The Anesthesia Record and
    # OR Record are filled in during/after the case and keep the two
    # cleanly separate on every packet checked, so they are preferred; the
    # H&P is now only a last-resort fallback, and is flagged when used so
    # a reviewer knows it's the least reliable of the three sources.
    anes_name_f = _raw_field(primary_anes, "anesthetist")
    or_name_f = _raw_field(primary_or, "anesthesiologist")
    hp_name_f = _first_across_hp("anesthesiologist")
    if anes_name_f and anes_name_f.get("value"):
        fields["anesthetist"] = _fv(anes_name_f["value"], anes_name_f.get("confidence", "high"),
                                     primary_anes.get("page_label", "Anesthesia Record"),
                                     anes_name_f.get("flag_reason"))
    elif or_name_f and or_name_f.get("value"):
        fields["anesthetist"] = _fv(or_name_f["value"], or_name_f.get("confidence", "high"),
                                     primary_or.get("page_label", "OR Record"),
                                     or_name_f.get("flag_reason"))
    elif hp_name_f and hp_name_f.get("value"):
        fields["anesthetist"] = _fv(hp_name_f["value"], "medium",
                                     "H&P",
                                     "taken from the H&P (Anesthesia Record and OR Record both "
                                     "left this blank) -- this line is filled in pre-op and has "
                                     "occasionally mixed the planned technique into the name, so "
                                     "verify against the paper")
    else:
        fields["anesthetist"] = _fv(None, "low", None, "no anesthetist name found on any page")

    anes_tech_f = _raw_field(primary_anes, "anesthetic_technique")
    or_type_f = _raw_field(primary_or, "anesthesia_type")
    hp_planned_f = _first_across_hp("anesthesia_planned")
    if anes_tech_f and anes_tech_f.get("value"):
        fields["anesthesia_type"] = _fv(anes_tech_f["value"], anes_tech_f.get("confidence", "high"),
                                         primary_anes.get("page_label", "Anesthesia Record"),
                                         anes_tech_f.get("flag_reason"))
    elif or_type_f and or_type_f.get("value"):
        fields["anesthesia_type"] = _fv(or_type_f["value"], or_type_f.get("confidence", "high"),
                                         primary_or.get("page_label", "OR Record"),
                                         or_type_f.get("flag_reason"))
    elif hp_planned_f and hp_planned_f.get("value"):
        fields["anesthesia_type"] = _fv(hp_planned_f["value"], "medium",
                                         "H&P",
                                         "taken from the H&P's 'Anesthesia planned' box (Anesthesia "
                                         "Record and OR Record both left this blank) -- this is the "
                                         "PRE-OP PLAN, not necessarily what was actually given")
    else:
        fields["anesthesia_type"] = _fv(None, "low", None, "no anesthesia type/technique found on any page")

    # Added after the Peru 2016 vet-5 gate: this mission's short intake-slip
    # substitute for the H&P (see the STRONG_TITLE_KEYWORDS comment in
    # extract.py) has NO surgeon/assistant line at all -- only a "Seen By:"
    # initials row for whoever triaged the patient, which is not the same
    # thing. Every packet using that slip was therefore reporting
    # Surgeon/Assistant as blank even though the OR Record's own separate
    # "surgeon"/"assistant" free-text lines had the real names sitting right
    # there, unused -- confirmed on PR16-901 (OR Record: surgeon and assistant
    # names both written, with H&P's
    # surgeon_assistant null). This mirrors the existing Anesthesia
    # Record/OR Record -> H&P fallback chain used for "anesthetist" above:
    # prefer the H&P line when it exists (it is filled in pre-op and is the
    # form this pipeline was originally built around), fall back to the
    # OR Record's own surgeon+assistant lines when the H&P has nothing to
    # offer, and only give up entirely if neither page has a name.
    surg_asst_f = _first_across_hp("surgeon_assistant")
    if surg_asst_f and surg_asst_f.get("value"):
        fields["surgeon_assistant"] = _fv(surg_asst_f["value"], surg_asst_f.get("confidence", "high"),
                                           primary_hp.get("page_label"),
                                           surg_asst_f.get("flag_reason"))
    else:
        or_surgeon_f = _raw_field(primary_or, "surgeon")
        or_assistant_f = _raw_field(primary_or, "assistant")
        or_surgeon_v = or_surgeon_f.get("value") if or_surgeon_f else None
        or_assistant_v = or_assistant_f.get("value") if or_assistant_f else None
        if or_surgeon_v or or_assistant_v:
            parts = []
            if or_surgeon_v:
                parts.append(or_surgeon_v)
            if or_assistant_v:
                parts.append(or_assistant_v)
            conf = _weaker(or_surgeon_f.get("confidence", "medium") if or_surgeon_f else "medium",
                            or_assistant_f.get("confidence", "medium") if or_assistant_f else "medium")
            fields["surgeon_assistant"] = _fv(
                " / ".join(parts), conf,
                primary_or.get("page_label", "OR Record"),
                "taken from the OR Record's own Surgeon/Assistant lines -- the H&P "
                "(or this mission's intake-slip substitute for it) had no "
                "surgeon/assistant field filled in")
        else:
            fields["surgeon_assistant"] = _fv(None, "low", None,
                                               "no surgeon/assistant name found on any page")

    # Date of surgery source order, changed 2026-08 at the client's
    # direction: the Anesthesia Record's own Date box is now the DEFAULT
    # source, falling back to the OR Record's own Date box when the
    # Anesthesia Record's is blank. Previously sourced only from the H&P's
    # "Brief Op Note: Date:" line, which is frequently left blank -- that
    # line is filled in well after the pre-op portion of the form, and staff
    # sometimes never circle back to it even though the same date is right
    # there, filled in, on both same-day forms. The H&P is no longer
    # consulted for this field at all.
    anes_date_f = _raw_field(primary_anes, "date")
    or_date_f = _raw_field(primary_or, "date")
    if anes_date_f and anes_date_f.get("value"):
        fields["date_of_surgery"] = _fv(anes_date_f["value"], anes_date_f.get("confidence", "high"),
                                         primary_anes.get("page_label", "Anesthesia Record"),
                                         anes_date_f.get("flag_reason"))
        # Cross-check against the OR Record's OWN Date box (added 2026-08,
        # Ghana 2025-09 LOS follow-up). Both forms are filled in by
        # different staff on the same day, so they're an independent check
        # on each other -- but until now the OR Record's date was only ever
        # looked at when the Anesthesia Record's was BLANK, never compared
        # against it when both are present. That means a misread Anesthesia
        # Record date (e.g. a transposed digit) was silently accepted even
        # when the OR Record, sitting right there in the same packet, had
        # the correct date on it. Unlike flag_dates_outside_window (which
        # only catches a date that falls outside the mission's calendar),
        # this catches a same-day disagreement even when BOTH dates are
        # inside the mission window -- e.g. a day/month swap that still
        # lands in-range. Downgrades date_of_surgery only when the two
        # genuinely parse to different calendar dates; a formatting
        # difference ("9/23/25" vs "09-23-2025") that parses to the same
        # date is not a disagreement.
        if or_date_f and or_date_f.get("value"):
            anes_d = _parse_date(anes_date_f["value"])
            or_d = _parse_date(or_date_f["value"])
            if anes_d and or_d and anes_d != or_d:
                note = (f"Anesthesia Record reads {anes_d.isoformat()} but the OR Record's own "
                        f"Date box reads {or_d.isoformat()} for the same packet -- one of the two "
                        f"is a misread. Check the paper before trusting either")
                fields["date_of_surgery"].flag_reason = (
                    f"{fields['date_of_surgery'].flag_reason}; {note}"
                    if fields["date_of_surgery"].flag_reason else note)
                fields["date_of_surgery"].confidence = "low"
    elif or_date_f and or_date_f.get("value"):
        fields["date_of_surgery"] = _fv(or_date_f["value"], "medium",
                                         primary_or.get("page_label", "OR Record"),
                                         "Anesthesia Record's own Date box was blank -- taken from the "
                                         "OR Record's Date field instead")
    else:
        fields["date_of_surgery"] = _fv(None, "low", None,
                                         "no date found on the Anesthesia Record or the OR Record")
    if opts.get("or_record_timing_fallback") and isinstance(fields["date_of_surgery"].value, str):
        # One date format in the column for missions that opt in (the value
        # is unchanged, only spelled ISO); unparseable text is left as read.
        _d = _parse_date(fields["date_of_surgery"].value)
        if _d:
            fields["date_of_surgery"].value = _d.isoformat()

    def _bound_fv(raw, conf="high", source=None, flag=None) -> FieldValue:
        """One start/stop cell, normalised to HH:MM.

        Normalising matters more here than it looks. The forms carry
        "1003", "10:03" and "10.03" interchangeably, and a column mixing
        all three cannot be sorted, cannot be subtracted, and makes two
        identical times look different. _parse_time already does this
        everywhere else in the pipeline; the bounds must not be the one
        place that shows the raw string instead.

        An unparseable time keeps its original text rather than going
        blank, and is flagged: the reviewer needs to see what was actually
        written in order to work out what it should have been.
        """
        if raw is None or raw.get("value") in (None, ""):
            return _fv(None, "high", source)
        parsed = _parse_time(raw["value"])
        if parsed is None:
            return _fv(str(raw["value"]), "low", source,
                       f"time {raw['value']!r} could not be read as a clock time -- "
                       f"shown exactly as extracted; check the form")
        return _fv(parsed, _weaker(conf, raw.get("confidence", "high")), source, flag)

    def _bound_pair(start, end, alt_start, alt_end, page, alt_page, what):
        """The start/stop pair for one duration, with a conflict note.

        `start`/`end` are the timestamps actually used to compute the
        minutes. `alt_start`/`alt_end` are the same two times as recorded
        on the OTHER page, where a second copy exists -- the OR Record and
        the Anesthesia Record both carry anesthesia and procedure times,
        independently written, often by different people.

        Where the two copies disagree, the cell shows the one that was
        used (so the bounds always subtract to the minutes beside them)
        and says so, naming the competing value and its page. That is the
        whole point of the choice: a silently-picked winner is exactly the
        kind of invisible decision that produced the discharge-date bug.
        """
        out = []
        for used, alt in ((start, alt_start), (end, alt_end)):
            flag, conf = None, "high"
            u_val = _parse_time(used["value"]) if used and used.get("value") else None
            a_val = _parse_time(alt["value"]) if alt and alt.get("value") else None
            if u_val and a_val and u_val != a_val:
                conf = "medium"
                flag = (f"{page} says {u_val} and {alt_page} says {a_val} for {what} -- "
                        f"the {page} value is shown here and was used for the duration; "
                        f"check which page is right")
            out.append(_bound_fv(used, conf, page, flag))
        return tuple(out)

    def _compute_times_for_procedure(hp_page, anes_page, or_page):
        """Computes OR Time, Surgery Time, and Anesthesia Time for ONE
        procedure's pages. Used once for the primary procedure and once
        more for a same-day secondary procedure, so the two never share
        timestamps by accident.

        OR Time source order, reversed 2026-08 at the client's direction
        (previously OR Record only, no fallback at all -- see
        questions_for_doc.md #2 and RULES.md rule 2's original text):
        the ANESTHESIA RECORD's own "OR Time" row (In | Out -- a real,
        separate row from that form's Anesthesia and Surgery rows, present
        on the ISHI-2019 layout) is now the DEFAULT source, falling back to
        the OR Record's own in-room/out-of-room row only when the
        Anesthesia Record didn't capture it (blank box, or an ISHI-2025-
        template form that has no OR Time row at all). Still no fallback to
        Anesthesia Time or Surgery Time themselves -- this is one OR-room-
        episode measurement recorded in two possible PLACES, not a
        different measurement standing in for OR Time."""
        anes_or_in = _raw_field(anes_page, "or_time_in")
        anes_or_out = _raw_field(anes_page, "or_time_out")
        or_rec_in = _raw_any(or_page, "in_room", "or_time_in") or _raw_field(or_page, "in_room")
        or_rec_out = _raw_any(or_page, "out_of_room", "or_time_out") or _raw_field(or_page, "out_of_room")

        anes_or_in_ok = anes_or_in and anes_or_in.get("value")
        anes_or_out_ok = anes_or_out and anes_or_out.get("value")
        or_rec_in_ok = or_rec_in and or_rec_in.get("value")
        or_rec_out_ok = or_rec_out and or_rec_out.get("value")

        if anes_or_in_ok and anes_or_out_ok:
            p_or_in, p_or_out = anes_or_in, anes_or_out
            or_place = anes_page.get("page_label", "Anesthesia Record") + " (OR Time row)"
        elif or_rec_in_ok and or_rec_out_ok:
            p_or_in, p_or_out = or_rec_in, or_rec_out
            or_place = or_page.get("page_label", "OR Record") + " (In room/Out of room -- Anesthesia Record's OR Time row was blank or not on this form)"
        elif anes_or_in_ok or or_rec_in_ok:
            # An In with no matching Out, on whichever page had the In --
            # prefer the Anesthesia Record's own pairing over mixing an In
            # from one form with an Out from the other (rule 22: bounds
            # must come from the same branch, never re-derived independently).
            p_or_in = anes_or_in if anes_or_in_ok else or_rec_in
            p_or_out = anes_or_out if anes_or_in_ok else or_rec_out
            or_place = None
        else:
            p_or_in, p_or_out, or_place = anes_or_in, anes_or_out, None

        p_or_in_ok = p_or_in and p_or_in.get("value")
        p_or_out_ok = p_or_out and p_or_out.get("value")
        or_out_substituted = False
        if p_or_in_ok and not p_or_out_ok:
            # OR Time Out is blank on both forms. Client direction, 2026-08
            # (SL19 vet gate): "if an end time is missing, assume the end
            # time is the anesthesia end time." Unlike the Anesthesia-
            # Record-OR-Time-row fallback above (the SAME measurement
            # recorded in two places), this genuinely substitutes a
            # DIFFERENT measurement for the missing one -- exactly what
            # questions_for_doc.md #2 originally warned against -- so it is
            # only used as a last resort (Out truly missing everywhere, not
            # merely on the form that won the In), always forced to low
            # confidence, and always named explicitly in the flag.
            fallback_end = _raw_any(anes_page, "anesthesia_end") or _raw_any(or_page, "anesthesia_end")
            if fallback_end and fallback_end.get("value"):
                p_or_out = fallback_end
                p_or_out_ok = True
                or_out_substituted = True
                or_place = (or_place or "OR Time In") + ", Out substituted with Anesthesia End (OR Time Out was blank on both forms; client direction, 2026-08 SL19 vet gate)"

        if p_or_in_ok and p_or_out_ok:
            or_minutes = _minutes_between(p_or_in["value"], p_or_out["value"])
            if or_out_substituted:
                or_conf = "low"
                or_source = or_place
                or_flag = (f"OR Time Out was blank on both the Anesthesia Record and the OR Record -- "
                           f"substituted the Anesthesia End time ({p_or_out['value']}) as a stand-in, "
                           f"per client direction. This is a different measurement standing in for the "
                           f"missing one, not a duplicate recording of the same one -- verify against the "
                           f"paper before trusting this duration")
            else:
                or_conf = _weaker(p_or_in.get("confidence", "high"), p_or_out.get("confidence", "high"))
                or_source = or_place or "OR Time"
                or_flag = None if or_conf == "high" else "derived from an OR-time timestamp flagged as uncertain -- verify source time"
        elif p_or_in_ok and p_or_out and not p_or_out_ok:
            or_minutes, or_conf, or_source = None, "low", "Out of room/OR Time Out missing"
            or_flag = p_or_out.get("flag_reason") or "an OR Time In was found but no matching Out on either form, and no Anesthesia End was available to substitute either"
        else:
            or_minutes, or_conf, or_source, or_flag = None, "low", "none", "insufficient OR Time In/Out timestamps on either the Anesthesia Record or the OR Record"

        # Surgery/Procedure Time: first incision to closing. SOURCE OF
        # TRUTH is the Anesthesia Record's own Surgery Start/End line. If
        # that wasn't filled in, fall back to the OR Record's own
        # Procedure Start/End line (a separate field from OR Time
        # In/Out). Only if NEITHER timestamp source is available does
        # this fall back to the H&P's handwritten "Case duration: X min"
        # field -- which is then just an estimate, not a timestamp
        # calculation.
        # Both field-name generations are accepted: the 2019 Anesthesia
        # Record labelled this row "Surgery", the 2025 one labels it
        # "Procedure". Same measurement, different word on the paper.
        anes_proc_start = _raw_any(anes_page, "surgery_start", "procedure_start")
        anes_proc_end = _raw_any(anes_page, "surgery_end", "procedure_end")
        anes_proc_minutes = None
        if anes_proc_start and anes_proc_end and anes_proc_start.get("value") and anes_proc_end.get("value"):
            anes_proc_minutes = _minutes_between(anes_proc_start["value"], anes_proc_end["value"])

        or_proc_start = _raw_any(or_page, "procedure_start", "surgery_start")
        or_proc_end = _raw_any(or_page, "procedure_end", "surgery_end")
        or_proc_minutes = None
        if or_proc_start and or_proc_end and or_proc_start.get("value") and or_proc_end.get("value"):
            or_proc_minutes = _minutes_between(or_proc_start["value"], or_proc_end["value"])

        explicit_surg_dur = _raw_field(hp_page, "case_duration_mins")  # mislabeled on the form; see RULES.md
        # The extractor is asked for minutes but occasionally returns the
        # text as written (Ghana 2026 Oct: e.g. "1hr 20" crashed the build).
        # A bare number in a string is used; anything else is treated as not
        # usable rather than guessed at.
        if explicit_surg_dur and isinstance(explicit_surg_dur.get("value"), str):
            _txt = explicit_surg_dur["value"].strip()
            try:
                explicit_surg_dur = {**explicit_surg_dur, "value": float(_txt) if "." in _txt else int(_txt)}
            except ValueError:
                explicit_surg_dur = None

        if anes_proc_minutes is not None:
            surg_minutes = anes_proc_minutes
            surg_conf = _weaker(anes_proc_start.get("confidence", "high"), anes_proc_end.get("confidence", "high"))
            surg_source = anes_page.get("page_label", "Anesthesia Record") + " (Procedure/Surgery Start to End -- source of truth)"
            surg_flag = None if surg_conf == "high" else "derived from a Procedure/Surgery timestamp flagged as uncertain -- verify source time"
            # Cross-check against the OR Record's OWN Procedure Start/End
            # line too, when it exists -- two independently-recorded
            # readings of the same event disagreeing is worth surfacing
            # even below the usual 30% flag threshold, since there's no
            # good reason for them to differ by much.
            if or_proc_minutes is not None and or_proc_minutes > 0:
                rel_diff = abs(anes_proc_minutes - or_proc_minutes) / max(anes_proc_minutes, or_proc_minutes)
                if rel_diff > 0.15:
                    surg_conf = "low" if rel_diff > 0.3 else "medium"
                    mismatch_note = (f"OR Record's own Procedure Start/End ({or_proc_minutes} min) disagrees with "
                                      f"the Anesthesia Record's Surgery Start/End ({anes_proc_minutes} min, used here) "
                                      f"by {rel_diff:.0%} -- worth a second look even though Anesthesia Record is the source of truth")
                    surg_flag = f"{surg_flag}; {mismatch_note}" if surg_flag else mismatch_note
        elif or_proc_minutes is not None:
            surg_minutes = or_proc_minutes
            surg_conf = _weaker(or_proc_start.get("confidence", "high"), or_proc_end.get("confidence", "high"))
            surg_source = or_page.get("page_label", "OR Record") + " (Procedure Start to End -- Anesthesia Record's own Surgery time was not filled in)"
            surg_flag = "Anesthesia Record's Surgery Start/End was not filled in on this form -- using the OR Record's own Procedure Start/End instead"
            if surg_conf != "high":
                surg_flag += "; also flagged as an uncertain timestamp -- verify source time"
        elif explicit_surg_dur and explicit_surg_dur.get("value") is not None:
            surg_minutes = explicit_surg_dur["value"]
            surg_conf = explicit_surg_dur.get("confidence", "high")
            surg_flag = explicit_surg_dur.get("flag_reason")
            surg_source = hp_page.get("page_label", "H&P") + " (explicit 'Case duration' field -- used only because neither Anesthesia nor OR Record procedure timestamps were available)"
        else:
            surg_minutes, surg_conf, surg_flag, surg_source = None, "low", "no Anesthesia/OR procedure timestamps and no explicit H&P duration available", "none"

        # Cross-check against the H&P's written duration whenever it
        # wasn't the source actually used -- large disagreement suggests
        # one of the two was misread.
        if (explicit_surg_dur and explicit_surg_dur.get("value") is not None
                and surg_minutes is not None and surg_source and "H&P" not in surg_source):
            rel_diff = abs(surg_minutes - explicit_surg_dur["value"]) / max(surg_minutes, explicit_surg_dur["value"], 1)
            if rel_diff > 0.3:
                surg_conf = "low"
                mismatch_note = (f"H&P's written duration ({explicit_surg_dur['value']} min) disagrees with the "
                                  f"computed Surgery time ({surg_minutes} min) by {rel_diff:.0%} -- verify against the scan")
                surg_flag = f"{surg_flag}; {mismatch_note}" if surg_flag else mismatch_note

        # The 2025 OR Record carries its own Anesthesia Start/End row, so it
        # is a genuine second source when the Anesthesia Record's grid was
        # left blank. The Anesthesia Record still wins when both are filled.
        anes_start = _raw_any(anes_page, "anesthesia_start") or _raw_any(or_page, "anesthesia_start")
        anes_end = _raw_any(anes_page, "anesthesia_end") or _raw_any(or_page, "anesthesia_end")
        if anes_start and anes_end and anes_end.get("value"):
            anes_minutes = _minutes_between(anes_start["value"], anes_end["value"])
            anes_conf = _weaker(anes_start.get("confidence", "high"), anes_end.get("confidence", "high"))
            anes_source = anes_page.get("page_label", "Anesthesia Record") + " (Anesthesia Start to End)"
            anes_flag = None
        else:
            anes_minutes, anes_conf, anes_source = None, "low", "Anesthesia Record (Anesthesia Start/End)"
            anes_flag = "Anesthesia End was not filled in on this form -- not substituting another timestamp for it"
            _blanked = _raw_field(anes_page, "anesthesia_end")
            # Blanked by _anes_checked_against_or (an impossible time, not an
            # empty box) -- say so, instead of "was not filled in".
            if (_blanked and _blanked.get("value") is None
                    and "left blank rather than keep an impossible time" in (_blanked.get("flag_reason") or "")):
                anes_flag = _blanked["flag_reason"]

        if or_minutes is not None and surg_minutes is not None and or_minutes < surg_minutes:
            or_conf = "low"
            mismatch = (f"OR time ({or_minutes} min) is shorter than Surgery time ({surg_minutes} min), which isn't "
                        f"physically possible (OR time should contain the whole surgery) -- a timestamp was likely misread")
            or_flag = f"{or_flag}; {mismatch}" if or_flag else mismatch
        if anes_minutes is not None and surg_minutes is not None and anes_minutes < surg_minutes:
            anes_conf = "low"
            mismatch = (f"Anesthesia time ({anes_minutes} min) is shorter than Surgery time ({surg_minutes} min), "
                        f"which isn't physically possible -- a timestamp was likely misread")
            anes_flag = f"{anes_flag}; {mismatch}" if anes_flag else mismatch

        # The clock times each duration was computed FROM (RULES.md rule
        # 22). These must be taken from the SAME branch that produced the
        # minutes, never re-derived independently -- a bound that does not
        # subtract to the number beside it is worse than no bound at all,
        # because it looks like corroboration while actually contradicting.
        # So each is set from the exact raw fields used above:
        #   - OR: the in/out pair, and only when both were usable
        #   - surgery: whichever page won, Anesthesia Record or OR Record
        # Where a duration came from a handwritten total rather than a
        # timestamp pair (the H&P fallback), the bounds are left blank
        # rather than invented.
        if p_or_in_ok and p_or_out_ok:
            # p_or_in/p_or_out are whichever pair actually won above (rule 22:
            # the bounds must come from the SAME branch as the minutes) --
            # the "alt" side for the cross-check comment is whichever form
            # did NOT win, so the flag still names both readings when they
            # disagree.
            if anes_or_in_ok and anes_or_out_ok:
                or_bounds = _bound_pair(p_or_in, p_or_out, or_rec_in, or_rec_out,
                                        anes_page.get("page_label", "Anesthesia Record"),
                                        or_page.get("page_label", "OR Record"), "OR Time In/Out")
            else:
                or_bounds = _bound_pair(p_or_in, p_or_out, anes_or_in, anes_or_out,
                                        or_page.get("page_label", "OR Record"),
                                        anes_page.get("page_label", "Anesthesia Record"), "OR Time In/Out")
        else:
            or_bounds = (_bound_fv(None), _bound_fv(None))

        if anes_proc_minutes is not None:
            surg_bounds = _bound_pair(anes_proc_start, anes_proc_end, or_proc_start, or_proc_end,
                                      "Anesthesia Record", "OR Record", "procedure start/end")
        elif or_proc_minutes is not None:
            surg_bounds = _bound_pair(or_proc_start, or_proc_end, None, None,
                                      "OR Record", None, "procedure start/end")
        else:
            # The H&P handwritten-duration fallback, or nothing at all.
            surg_bounds = (_bound_fv(None), _bound_fv(None))

        if anes_minutes is not None:
            anes_bounds = _bound_pair(anes_start, anes_end,
                                      _raw_field(or_page, "anesthesia_start"),
                                      _raw_field(or_page, "anesthesia_end"),
                                      "Anesthesia Record", "OR Record", "anesthesia start/end")
        else:
            anes_bounds = (_bound_fv(None), _bound_fv(None))

        return {
            "or": (or_minutes, or_conf, or_source, or_flag),
            "surgery": (surg_minutes, surg_conf, surg_source, surg_flag),
            "anesthesia": (anes_minutes, anes_conf, anes_source, anes_flag),
            "or_bounds": or_bounds,
            "surgery_bounds": surg_bounds,
            "anesthesia_bounds": anes_bounds,
        }

    primary_times = _compute_times_for_procedure(primary_hp, primary_anes, primary_or)
    fields["or_time_mins"] = _fv(*primary_times["or"])
    fields["surgery_time_mins"] = _fv(*primary_times["surgery"])
    fields["operative_time_mins"] = _fv(*primary_times["surgery"])  # required column, same measurement
    fields["anesthesia_time_mins"] = _fv(*primary_times["anesthesia"])

    # The clock times each duration was computed from (RULES.md rule 22).
    # operative_time reuses the surgery pair because it IS the surgery
    # measurement -- the duplicate column exists only to match the
    # original ISHI sheet, and its bounds must agree with its minutes.
    (fields["or_time_start"], fields["or_time_stop"]) = primary_times["or_bounds"]
    (fields["surgery_time_start"], fields["surgery_time_stop"]) = primary_times["surgery_bounds"]
    (fields["operative_time_start"], fields["operative_time_stop"]) = primary_times["surgery_bounds"]
    (fields["anesthesia_time_start"], fields["anesthesia_time_stop"]) = primary_times["anesthesia_bounds"]

    if genuine_repeat_hp_pages:
        secondary_hp = genuine_repeat_hp_pages[0]
        secondary_anes = anes_pages[1] if len(anes_pages) > 1 else {}
        secondary_or = or_pages[1] if len(or_pages) > 1 else {}
        secondary_times = _compute_times_for_procedure(secondary_hp, secondary_anes, secondary_or)
        fields["secondary_surgery_time_mins"] = _fv(*secondary_times["surgery"])
    elif has_genuine_secondary_procedure:
        # Same rationale as secondary_procedure above: no repeat H&P page
        # exists, but the second OR Record page's own timestamps are real,
        # independent data for this secondary procedure. _compute_times_for_
        # procedure only reads hp_page for the "case duration" fallback
        # (unused when real timestamps exist), so an empty dict is safe here.
        secondary_anes = anes_pages[1] if len(anes_pages) > 1 else {}
        secondary_times = _compute_times_for_procedure({}, secondary_anes, secondary_or_page)
        fields["secondary_surgery_time_mins"] = _fv(*secondary_times["surgery"])
    else:
        fields["secondary_surgery_time_mins"] = _fv(None, "high")

    # ----------------------------------------------------------------------
    # POST-OP COURSE AND OUTCOMES
    #
    # These all come off pages the pipeline already classified but never
    # read: the PACU Flowsheet, the Floor Flowsheet, the Progress Notes and
    # the Post-Op Visit form. This block also replaces the placeholder that
    # used to set every patient's LOS to a hardcoded 1 -- a value that was
    # right often enough to look plausible and therefore never questioned.
    # ----------------------------------------------------------------------
    surgery_date = _parse_date(fields["date_of_surgery"].value)
    default_year = surgery_date.year if surgery_date else None

    primary_pacu = (_find_pages(raw, "PACU Flowsheet", prefer_content=True) or [{}])[0]
    primary_floor = (_find_pages(raw, "Floor Flowsheet", prefer_content=True) or [{}])[0]
    primary_disch = (_find_pages(raw, "RN Discharge", prefer_content=True) or [{}])[0]
    visit_pages = _find_pages(raw, "Post-Op Visit", prefer_content=True)
    dropped_visit_note = None
    if opts.get("drop_same_day_postop_visits") and surgery_date:
        # GH26O-901 p16 (Ghana 2026 Oct): a multi-day Floor Flowsheet was
        # classified as a Post-Op Visit and reported a "follow-up" dated the
        # day of surgery with no POD. A real outpatient follow-up is after
        # discharge, so a "visit" with no POD dated on/before the surgery
        # date is not evidence of one. The classifier was fixed too (Floor
        # Flowsheet keywords); this is the belt to that pair of braces.
        kept = []
        for p in visit_pages:
            vd = _parse_date(_value(p, "visit_date"), default_year)
            if _value(p, "pod") in (None, "") and vd and vd <= surgery_date:
                dropped_visit_note = (f"{p.get('page_label')} looks like a Post-Op Visit but is dated "
                                      f"{vd.isoformat()} (surgery day or earlier) with no POD -- not "
                                      f"counted as a follow-up visit; check the page type")
                continue
            kept.append(p)
        visit_pages = kept
    primary_visit = visit_pages[0] if visit_pages else {}

    def _carry(col, page, keys, page_name, transform=None):
        """Copy one extracted field straight through to a column, keeping
        its confidence and its flag. A missing page or an unfilled box
        becomes null WITH a flag, never a silent blank -- a blank cell no
        one was told to check is indistinguishable from a real zero."""
        f = _raw_any(page, *keys)
        if not f:
            fields[col] = _fv(None, "low", page.get("page_label") or page_name,
                              f"no {page_name} value for this packet -- page missing or box blank")
            return None
        val = f["value"]
        if transform:
            converted = transform(val)
            if converted is None:
                fields[col] = _fv(val, "low", page.get("page_label") or page_name,
                                  f"could not be read as a date/time: form says {val!r}")
                return None
            val = converted
        fields[col] = _fv(val, f.get("confidence", "high"),
                          page.get("page_label") or page_name, f.get("flag_reason"))
        return val

    pacu_arrival = _carry("pacu_arrival_time", primary_pacu,
                          ("pacu_arrival_time", "arrival_time"), "PACU Flowsheet", _parse_time)
    pacu_transfer = _carry("pacu_transfer_time", primary_pacu,
                           ("pacu_transfer_time", "transfer_time"), "PACU Flowsheet", _parse_time)

    pacu_mins = _minutes_between(pacu_arrival, pacu_transfer) if pacu_arrival and pacu_transfer else None
    if pacu_mins is None:
        fields["pacu_los_mins"] = _fv(None, "low", "PACU Flowsheet",
                                       "PACU arrival and/or transfer time missing -- not estimated")
    elif pacu_mins > 12 * 60:
        # _minutes_between wraps a negative interval into the next day,
        # which is right for a case running past midnight and wrong for a
        # transposed pair of times. Over half a day in recovery, the second
        # explanation is far likelier, so it gets flagged rather than used.
        fields["pacu_los_mins"] = _fv(pacu_mins, "low", "PACU Flowsheet",
                                       f"implies {pacu_mins/60:.1f} h in PACU ({pacu_arrival} to "
                                       f"{pacu_transfer}) -- check the two times aren't swapped or misread")
    else:
        fields["pacu_los_mins"] = _fv(pacu_mins, _weaker(
            fields["pacu_arrival_time"].confidence, fields["pacu_transfer_time"].confidence),
            "PACU Flowsheet (arrival to transfer)")

    floor_arr_raw = _value(primary_floor, "floor_arrival")
    fa_date, fa_time = _split_date_time(floor_arr_raw, default_year)
    if floor_arr_raw is None:
        fields["floor_arrival"] = _fv(None, "low", "Floor Flowsheet",
                                       "Date/Time Arrived not filled in, or no Floor Flowsheet page")
    else:
        fields["floor_arrival"] = _fv(
            f"{fa_date.isoformat()} {fa_time}" if fa_date and fa_time else str(floor_arr_raw),
            "high" if (fa_date and fa_time) else "low",
            primary_floor.get("page_label", "Floor Flowsheet"),
            None if (fa_date and fa_time) else f"only partly readable: form says {floor_arr_raw!r}")

    # Last dated entry on the RN Discharge form -- the fallback the doc
    # settled on for questions_for_doc.md #4 (answered): when the discharge
    # signature block is genuinely undated, use the date the surgeon/nursing
    # staff last documented on this form rather than leaving discharge blank
    # or reaching for the "Follow up on ___" clinic date (see the changelog
    # entry on that bug in RULES.md). Reported as its own column too, so a
    # reviewer can see what the fallback actually used.
    last_entry_raw = _value(primary_disch, "last_entry_date")
    last_entry_date = _parse_date(last_entry_raw, default_year)
    if last_entry_raw is None:
        fields["last_entry_date"] = _fv(None, "low", "RN Discharge",
                                         "no dated entry found on the RN Discharge form")
    elif last_entry_date is None:
        fields["last_entry_date"] = _fv(str(last_entry_raw), "low", "RN Discharge",
                                         f"could not be read as a date: form says {last_entry_raw!r}")
    else:
        fields["last_entry_date"] = _fv(last_entry_date.isoformat(), "medium", "RN Discharge")

    # Discharge time, in order of how directly the form records it. Each
    # source is named in the flag so a reviewer knows which page to open.
    disch_dt, disch_conf, disch_src = None, "low", None
    d_date = _parse_date(_value(primary_disch, "discharge_date"), default_year)
    d_time = _parse_time(_value(primary_disch, "discharge_time"))
    if d_date and d_time:
        disch_dt, disch_conf, disch_src = _combine(d_date, d_time), "high", "RN Discharge"
    if disch_dt is None:
        fd_date, fd_time = _split_date_time(_value(primary_floor, "floor_discharge"), default_year)
        if fd_date and fd_time:
            disch_dt, disch_conf, disch_src = _combine(fd_date, fd_time), "medium", "Floor Flowsheet Date/Time Discharged"
    if disch_dt is None and d_date:
        disch_dt, disch_conf, disch_src = _combine(d_date, "00:00"), "low", "RN Discharge date only, no time"
    if disch_dt is None and last_entry_date:
        disch_dt, disch_conf, disch_src = (_combine(last_entry_date, "00:00"), "low",
                                           "last dated entry on the RN Discharge form -- inferred stand-in, "
                                           "the signature block itself was undated")
    if disch_dt is None and surgery_date and pacu_transfer:
        disch_dt, disch_conf, disch_src = (_combine(surgery_date, pacu_transfer), "low",
                                           "PACU transfer time on the day of surgery -- NOT a recorded discharge")

    if disch_dt is None:
        fields["discharge_datetime"] = _fv(None, "low", "RN Discharge / Floor Flowsheet",
                                            "no discharge date/time recorded anywhere in this packet")
    else:
        fields["discharge_datetime"] = _fv(
            disch_dt.strftime("%Y-%m-%d %H:%M"), disch_conf, disch_src,
            None if disch_conf == "high" else f"discharge time taken from {disch_src}")

    # LOS in whole days, matching the original sheet's column. Same-day
    # discharge is 0, not 1.
    if surgery_date and disch_dt:
        days = (disch_dt.date() - surgery_date).days
        if days < 0:
            fields["los"] = _fv(None, "low", f"{disch_src} vs date of surgery",
                                 f"discharge ({disch_dt.date()}) reads as BEFORE surgery ({surgery_date}) -- "
                                 f"one of the two dates is misread; not guessing which")
        else:
            los_notes = []
            if disch_conf != "high":
                los_notes.append(f"discharge time came from {disch_src}")
            if fields["date_of_surgery"].confidence != "high":
                dos_reason = fields["date_of_surgery"].flag_reason or "date_of_surgery is not high confidence"
                los_notes.append(f"built on a date_of_surgery that is uncertain: {dos_reason}")
            fields["los"] = _fv(days, _weaker(disch_conf, fields["date_of_surgery"].confidence),
                                 f"{disch_src} minus date of surgery",
                                 "; ".join(los_notes) if los_notes else None)
    else:
        fields["los"] = _fv(None, "low", "RN Discharge vs date of surgery",
                             "cannot compute: " + ("no readable surgery date" if not surgery_date
                                                   else "no readable discharge date"))

    # LOS in hours, measured from the patient entering the OR. This is the
    # column to analyse; the whole-day count above is kept only because the
    # original sheet has it, and it cannot distinguish a 4-hour stay from a
    # 20-hour one.
    anchor = None
    anchor_name = None
    for key, label in (("in_room", "OR in-room"), ("anesthesia_start", "anesthesia start")):
        t = _parse_time(_value(primary_or, key) or _value(primary_anes, key))
        if t:
            anchor, anchor_name = _combine(surgery_date, t), label
            break
    if anchor is None and surgery_date and pacu_arrival:
        anchor, anchor_name = _combine(surgery_date, pacu_arrival), "PACU arrival"
    if anchor and disch_dt:
        hours = (disch_dt - anchor).total_seconds() / 3600
        if hours < 0:
            fields["los_hours"] = _fv(None, "low", f"{anchor_name} to {disch_src}",
                                       f"discharge reads as before {anchor_name} -- times misread")
        else:
            fields["los_hours"] = _fv(round(hours, 1), _weaker(disch_conf, "medium"),
                                       f"{anchor_name} to {disch_src}",
                                       f"measured from {anchor_name} to the discharge time recorded on the {disch_src}")
    else:
        fields["los_hours"] = _fv(None, "low", "OR in-room to discharge",
                                   "cannot compute: no readable start anchor or no readable discharge time")

    pods = [p for p in (_value(pg, "pod") for pg in _find_pages(raw, "Progress Note"))
            if isinstance(p, (int, float))]
    if pods:
        fields["max_pod_documented"] = _fv(int(max(pods)), "high", "Progress Notes")
    else:
        fields["max_pod_documented"] = _fv(None, "low", "Progress Notes",
                                            "no numbered post-operative day found on any Progress Note page")

    # Cross-check computed LOS against this packet's OWN Progress Notes
    # (added 2026-08, Ghana 2025-09 LOS follow-up). Only meaningful when
    # the discharge date came from a low-confidence source (the date-only
    # RN Discharge fallback, the last-dated-entry stand-in, or the PACU
    # stand-in) -- those are exactly the readings most likely to be a
    # misread digit, and until now nothing in the packet checked them
    # against anything else. If Progress Notes only document up through
    # POD 1 but the discharge-based LOS says 60+ days, that's real
    # evidence independent of the mission window or the blunt 0-5-day
    # plausibility ceiling -- and it catches what those two miss, because
    # a wrong discharge date can land inside both the mission window
    # (with its 45-day trailing grace) and, by coincidence, inside a
    # "plausible" LOS range too. Margin of 3 days: Progress Notes normally
    # stop a day or two before the actual discharge, not track it exactly,
    # so a small gap is expected and not itself suspicious.
    if (disch_conf == "low" and pods and isinstance(fields["los"].value, (int, float))
            and fields["los"].value > max(pods) + 3):
        gap = fields["los"].value - int(max(pods))
        note = (f"LOS reads {fields['los'].value} days, but Progress Notes in this same packet only "
                f"document up through POD {int(max(pods))} -- a {gap}-day gap with nothing in the "
                f"packet to explain it. The discharge date this LOS was built on came from {disch_src}, "
                f"a low-confidence source; check it against the paper")
        for key in ("los", "los_hours"):
            fv = fields.get(key)
            if fv is None or fv.value is None:
                continue
            fv.flag_reason = f"{fv.flag_reason}; {note}" if fv.flag_reason else note
            fv.confidence = "low"

    _carry("discharge_criteria_met", primary_disch, ("discharge_criteria_met",), "RN Discharge")

    # Post-Op Visit: the outpatient follow-up form. It has no equivalent in
    # earlier missions, so a packet without one is normal, not an error --
    # but "no follow-up recorded" and "follow-up was normal" are different
    # answers and must not collapse into the same empty cell.
    if not visit_pages:
        for col in ("postop_visit_date", "postop_visit_pod", "postop_visit_findings",
                    "postop_complication", "further_surgery_planned"):
            fields[col] = _fv(None, "low", "Post-Op Visit",
                               "no Post-Op Visit page in this packet -- follow-up not documented, "
                               "which is NOT the same as an uneventful follow-up"
                               + (f"; {dropped_visit_note}" if dropped_visit_note else ""))
    else:
        _carry("postop_visit_date", primary_visit, ("visit_date",), "Post-Op Visit",
               lambda v: (lambda d: d.isoformat() if d else None)(_parse_date(v, default_year)))
        _carry("postop_visit_pod", primary_visit, ("pod",), "Post-Op Visit")
        _carry("postop_visit_findings", primary_visit,
               ("wound_findings", "other_abnormal_findings"), "Post-Op Visit")
        _carry("postop_complication", primary_visit, ("complication",), "Post-Op Visit")
        _carry("further_surgery_planned", primary_visit, ("further_surgery_planned",), "Post-Op Visit")
        if len(visit_pages) > 1:
            fields["postop_visit_date"].flag_reason = (
                f"{len(visit_pages)} Post-Op Visit pages in this packet; only the first is "
                f"reported here -- open the scan if the later visits matter")
            fields["postop_visit_date"].confidence = "medium"

    fields["procedure_category"] = _fv(*_categorize_procedure(proc_f["value"] if proc_f else None))


    complication_notes = []
    if complication_prefix:
        complication_notes.append(complication_prefix)
    # Complications are gathered from every page that can record one. Each
    # note is prefixed with the page it came from, so the cell stays
    # auditable when several pages contribute.
    COMPLICATION_SOURCES = {
        "H&P": ("ebl_complications",),
        "Anesthesia Record": ("event_note", "complications"),
        "OR Record": ("complications",),
        "PACU Flowsheet": ("pacu_complications",),
        "Floor Flowsheet": ("floor_complications", "notes"),
        "Progress Note": ("complications",),
        "RN Discharge": ("nursing_complications",),
        "Post-Op Visit": ("complication",),
    }
    for p in raw["pages"]:
        for key in COMPLICATION_SOURCES.get(p["page_type"], ()):
            v = _value(p, key)
            if v is None:
                continue
            text = str(v).strip()
            # The extractor is told to return null rather than "none" for an
            # absent complication, but a tick in a "no complications" box
            # legitimately reads as one of these -- and pasting "none" into
            # the complications column makes an uneventful case look
            # reviewed when it was only transcribed.
            if text.lower().strip(" .-") in ("", "none", "nil", "no", "n/a", "na", "none noted", "no complications"):
                continue
            complication_notes.append(f"{p.get('page_label', p['page_type'])}: {text}")

    fields["complications_notes"] = _fv(
        " | ".join(complication_notes) if complication_notes else None,
        "medium" if complication_notes else "high",
        "aggregated across pages",
        "auto-aggregated from multiple pages -- confirm this is complete and correctly summarized" if complication_notes else None,
    )

    # Was the patient seen post-operatively? Evidence is a Progress Note
    # with an actual entry on it, or a Post-Op Visit form.
    #
    # This used to answer "Y" either way -- with a low confidence and a note
    # when there was no evidence, but still "Y" in the cell. Anyone reading
    # the column, or any formula counting it, saw 100% follow-up. A question
    # about whether something happened must not default to yes.
    filled_progress = [p for p in _find_pages(raw, "Progress Note")
                       if _raw_any(p, "pod", "disposition", "s_p")]
    if filled_progress or visit_pages:
        evidence = ", ".join(p.get("page_label", p["page_type"])
                             for p in filled_progress + visit_pages)
        fields["seen_post_op"] = _fv("Y", "high", evidence)
    else:
        fields["seen_post_op"] = _fv(
            None, "low", "Progress Note / Post-Op Visit",
            "no filled-in Progress Note and no Post-Op Visit page in this packet, so there is no "
            "evidence either way -- left blank rather than assumed Y",
        )

    postop_comment_candidates = []
    for p in raw["pages"]:
        if p["page_type"] in ("PACU Flowsheet", "Progress Note", "Post-Op Visit"):
            for key in ("notes", "disposition", "followup_plan", "activity_restrictions"):
                v = _value(p, key)
                if v is not None and str(v).strip():
                    postop_comment_candidates.append(f"{p.get('page_label', p['page_type'])}: {str(v).strip()}")
    fields["post_op_comments"] = _fv(
        " | ".join(postop_comment_candidates) if postop_comment_candidates else None,
        "medium" if postop_comment_candidates else "high",
        "PACU Flowsheet / Progress Note / Post-Op Visit",
    )

    # ----------------------------------------------------------------------
    # HERNIA DETAIL AND SEVERITY
    #
    # This used to write a literal "x" into every h_score cell, with a note
    # saying the grade could not be extracted from free text. That was true
    # of the 2019 forms. The 2025 H&P prints a "Clinical classification for
    # inguinal hernia" table -- H1-H4 size, F1-F4 skin-fold, a/b/c
    # inguinoscrotal component -- so the grade is now transcribed from a
    # marked box rather than inferred, and an unmarked table reads as null.
    #
    # Grades are validated against the printed vocabulary. Anything outside
    # it is reported as null with the raw reading preserved in the flag,
    # because a grade coerced into range is worse than no grade at all: it
    # is indistinguishable from a real one in the analysis.
    # ----------------------------------------------------------------------
    is_hernia = "hernia" in (fields["procedure_category"].value or "").lower()
    hp_label = primary_hp.get("page_label", "H&P")

    # Same primary_hp-only bug shape as preop_dx/postop_dx above, found the
    # same way (Peru 2015 vet gate, checking hernia_reducibility/
    # hernia_recurrent directly against a vetted packet's raw JSON): on
    # packets whose page 0 is the short intake card, the packet's REAL
    # clinical detail -- reducibility, recurrence, defect count, H/F/
    # inguinoscrotal grade, laterality -- lives on a later H&P page and was
    # being silently dropped because only hp_pages[0] was ever consulted.
    # _hp_page_for finds the first H&P page (in packet order) that actually
    # has a filled value for the given key(s), falling back to primary_hp
    # so the existing "no H&P value -- page missing or box blank" messaging
    # still fires correctly when the value genuinely isn't anywhere in the
    # packet.
    def _hp_page_for(*keys):
        for hp in hp_pages:
            if _raw_any(hp, *keys):
                return hp
        return primary_hp

    def _graded(col, keys, allowed, label, normalise=str.upper, flag_when_missing=True):
        page = _hp_page_for(*keys)
        page_label = page.get("page_label", "H&P")
        f = _raw_any(page, *keys)
        raw_val = f["value"] if f else None
        norm = normalise(str(raw_val).strip().replace(" ", "")) if raw_val not in (None, "") else None
        if norm in allowed:
            fields[col] = _fv(allowed[norm], f.get("confidence", "high"), page_label, f.get("flag_reason"))
        elif norm is None:
            if flag_when_missing:
                fields[col] = _fv(None, "low" if is_hernia else "high", page_label,
                                  f"{label} not marked on the H&P classification table" if is_hernia else None)
            else:
                # ISHI doesn't routinely collect this grade (questions_for_doc.md
                # #5, answered) -- an unmarked box is the expected, normal case,
                # not a documentation gap, so it goes blank at high confidence
                # with no flag. Capture the value whenever it IS marked (the
                # branch above), just don't train reviewers to ignore flags by
                # raising one on almost every row.
                fields[col] = _fv(None, "high", page_label)
        else:
            fields[col] = _fv(None, "low", page_label,
                              f"{label} read as {raw_val!r}, which is not one of "
                              f"{sorted(set(allowed.values()))} -- left blank rather than forced into range")

    _graded("h_score", ("hernia_class_h", "h_score"),
            {f"H{i}": f"H{i}" for i in range(1, 5)}, "H grade")
    _graded("hernia_f_score", ("hernia_class_f", "f_score"),
            {f"F{i}": f"F{i}" for i in range(1, 5)}, "F (skin-fold) grade",
            flag_when_missing=False)
    _graded("inguinoscrotal_component", ("inguinoscrotal_component",),
            {"A": "a", "B": "b", "C": "c"}, "inguinoscrotal component")

    # An a/b/c component only exists for H3 and H4 on the printed table.
    if fields["inguinoscrotal_component"].value and fields["h_score"].value in ("H1", "H2"):
        fields["inguinoscrotal_component"].confidence = "low"
        fields["inguinoscrotal_component"].flag_reason = (
            f"an inguinoscrotal component was marked but the H grade reads {fields['h_score'].value}; "
            f"the printed table only defines a/b/c for H3 and H4 -- one of the two is misread")

    _graded("hernia_laterality", ("hernia_laterality", "laterality"),
            {"R": "R", "RIGHT": "R", "L": "L", "LEFT": "L",
             "B/L": "Bilateral", "BL": "Bilateral", "BILATERAL": "Bilateral", "BOTH": "Bilateral"},
            "hernia side")

    # The OR Record ticks the operated side independently of the H&P. When
    # they disagree it is a wrong-side question, so it is surfaced loudly
    # rather than resolved quietly.
    or_side_raw = _value(primary_or, "procedure_laterality")
    or_side = {"R": "R", "RIGHT": "R", "L": "L", "LEFT": "L", "B/L": "Bilateral",
               "BL": "Bilateral", "BILATERAL": "Bilateral"}.get(
        str(or_side_raw).strip().upper().replace(" ", "")) if or_side_raw else None
    hp_side = fields["hernia_laterality"].value
    if or_side and hp_side and or_side != hp_side:
        fields["hernia_laterality"].confidence = "low"
        fields["hernia_laterality"].flag_reason = (
            f"SIDE MISMATCH: the H&P indicates {hp_side} but the OR Record's ticked side is "
            f"{or_side}. Reported here is the H&P's. Check the scans before using this row.")
    elif or_side and not hp_side:
        fields["hernia_laterality"] = _fv(or_side, "medium", primary_or.get("page_label", "OR Record"),
                                           "side taken from the OR Record because the H&P did not state one")

    _carry("hernia_reducibility", _hp_page_for("hernia_reducibility"), ("hernia_reducibility",), "H&P")
    _carry("hernia_recurrent", _hp_page_for("hernia_recurrent"), ("hernia_recurrent",), "H&P")
    _carry("repair_technique", _hp_page_for("repair_technique"), ("repair_technique",), "H&P")
    _carry("defect_count", _hp_page_for("defect_count"), ("defect_count",), "H&P")

    # For a non-hernia case none of these are missing data -- they do not
    # apply. Downgrading them to unflagged nulls keeps the review list
    # honest; otherwise every hysterectomy arrives with eight false flags
    # and reviewers learn to ignore the colour.
    if not is_hernia:
        for col in ("hernia_reducibility", "hernia_recurrent", "repair_technique", "defect_count"):
            if fields[col].value is None:
                fields[col] = _fv(None, "high", hp_label)

    # Cross-record mesh check. "mesh_mentioned" is asked on every extracted
    # page type, not just the H&P, because a handwritten "mesh" note
    # routinely lands somewhere other than the H&P's Procedure line that
    # "repair_technique" is scoped to -- the OR Record's Implant/Special
    # Equipment box, a Progress Note, RN Discharge, etc. This never
    # overrides repair_technique's own value; it only appends a flagged
    # pointer to where else "mesh" was seen so a reviewer can check the
    # paper. Added at the client's direction, 2026-08 (Sierra Leone 2022
    # vet gate) -- see questions_for_doc.md and GLOSSARY.md.
    if is_hernia:
        current_val = fields["repair_technique"].value
        current_text = (current_val or "").lower()
        repair_technique_source_page = _hp_page_for("repair_technique")
        mesh_hits = []
        for p in raw["pages"]:
            mm = _raw_field(p, "mesh_mentioned")
            if not (mm and mm.get("value")):
                continue
            is_hp_page = p is repair_technique_source_page
            if is_hp_page and "mesh" in current_text:
                continue  # already captured by repair_technique itself
            label = p.get("page_label") or p.get("page_type")
            where = f"{label} (elsewhere on the page)" if is_hp_page else label
            mesh_hits.append(f"{where}: {mm['value']!r}")
        if mesh_hits and "mesh" not in current_text:
            note = "mesh mentioned on " + "; ".join(mesh_hits)
            new_val = f"{current_val} ({note})" if current_val else note[0].upper() + note[1:]
            fields["repair_technique"] = _fv(
                new_val,
                _weaker(fields["repair_technique"].confidence, "medium"),
                fields["repair_technique"].source_pages[0] if fields["repair_technique"].source_pages else hp_label,
                (fields["repair_technique"].flag_reason + "; " if fields["repair_technique"].flag_reason else "")
                + "mesh mentioned elsewhere in the packet but not (or not clearly) on the H&P Procedure "
                  "line -- verify against paper",
            )

    symptom_dur_page = _hp_page_for("symptom_duration_years")
    symptom_dur = _raw_field(symptom_dur_page, "symptom_duration_years")
    fields["symptom_duration_years"] = _fv(
        symptom_dur["value"] if symptom_dur else None,
        symptom_dur.get("confidence", "high") if symptom_dur else "low",
        symptom_dur_page.get("page_label"),
        symptom_dur.get("flag_reason") if symptom_dur else None,
    )

    fields["mrn"] = _fv(packet_id, "high", "packet identifier")

    _reconcile_social_history(fields, hp_pages)
    _reconcile_eurahs(fields, raw, surgery_date)

    # Completeness guard. Adding a column to schema.py and forgetting to
    # populate it here produces a column that is empty on every row, which
    # reads exactly like "this was never recorded on the forms" -- a silent
    # wrong answer rather than a crash. Anything unpopulated arrives as a
    # flagged null that says plainly that the pipeline, not the paper, is
    # what is missing.
    for col in CASE_LOG_COLUMNS:
        if col == "fields_flagged_count":
            continue  # derived after the fact, below
        if col not in fields:
            fields[col] = _fv(None, "low", "not extracted",
                               f"'{col}' is defined in the schema but nothing in reconcile.py fills "
                               f"it in -- this cell is blank because of a pipeline gap, not because "
                               f"the form was blank")

    row = CaseLogRow(packet_id=packet_id, fields=fields)
    fields["fields_flagged_count"] = _fv(len(row.flagged_fields()), "high", "derived")
    return [row]


def load_and_reconcile(json_path: str, options: Optional[dict] = None) -> List[CaseLogRow]:
    # encoding="utf-8" is required, not decorative: run_mission.py writes
    # these files with encoding="utf-8" explicitly (see its comment on that
    # write) because a verbatim transcription can contain a character
    # outside Windows' default locale codepage. Reading back without the
    # same explicit encoding hits the mirror-image bug: Windows' default
    # read encoding can't decode a valid UTF-8 byte sequence and raises
    # UnicodeDecodeError.
    with open(json_path, encoding="utf-8") as f:
        raw = json.load(f)
    return reconcile_packet(raw, options)


# Physical / physiological bounds, as (low, high, unit, why).
#
# These are NOT quality targets and nothing is rejected for missing them.
# They are the range inside which a value is merely unusual; outside it,
# the likeliest explanation stops being "an unusual patient" and starts
# being "a misread digit", and a human should look before the number is
# analysed.
#
# The reason this check has to exist alongside the model's own confidence:
# confidence answers "could I read this clearly?", and the errors that
# survive to this point are the ones where the answer was honestly yes.
# SL25-907 is the case in point -- the H&P Age box reads 60, the model
# returned 100 at HIGH confidence, and nothing downstream had any opinion
# about whether 100 was a plausible age for an elective outreach hernia
# repair. A bound does have an opinion. It is the cheapest possible
# cross-check and it catches the errors that confidence structurally
# cannot.
#
# Chosen deliberately wide. A false flag costs a reviewer ten seconds; a
# missed one puts a wrong number in a manuscript.
_PLAUSIBLE = {
    "age":                  (1, 95, "years", "outside this, a digit is likelier than a patient"),
    "surgery_time_mins":    (10, 300, "min", "knife-to-skin under 10 min or over 5 h"),
    "or_time_mins":         (15, 360, "min", "in-room to out-of-room under 15 min or over 6 h"),
    "anesthesia_time_mins": (15, 360, "min", "anesthesia under 15 min or over 6 h"),
    "pacu_los_mins":        (10, 300, "min", "recovery under 10 min or over 5 h"),
    "los_hours":            (0.5, 120, "h", "stay under 30 min or over 5 days"),
    "los":                  (0, 5, "days", "stay under 0 (impossible) or over 5 days on a day-case "
                             "mission -- los_hours uses the same ceiling; this had no bound at all "
                             "until a date_of_surgery misread produced an unflagged 126-day LOS "
                             "(SL19-901, 2026-08)"),
    "postop_visit_pod":     (0, 60, "days", "follow-up more than 2 months out"),
}


# Each duration column and the two bound columns that must explain it.
_DURATION_BOUNDS = (
    ("or_time_mins", "or_time_start", "or_time_stop"),
    ("surgery_time_mins", "surgery_time_start", "surgery_time_stop"),
    ("operative_time_mins", "operative_time_start", "operative_time_stop"),
    ("anesthesia_time_mins", "anesthesia_time_start", "anesthesia_time_stop"),
)


def verify_duration_bounds(rows) -> list[str]:
    """Check that every duration equals the gap between its own bounds.

    This runs on every build rather than living in the test suite alone,
    because the tests exercise one hand-built fixture while this exercises
    the real packets -- and the failure it guards against is a
    reconciliation branch that sets the minutes from one source and the
    bounds from another. That kind of drift appears only on the packets
    that take the unusual branch, which is exactly what a fixture does not
    cover (RULES.md rule 22).

    Returns a list of human-readable problems; empty means clean. It
    reports rather than raises: a mismatch means one of the columns is
    wrong and a human needs to look, but it is not a reason to withhold
    the other 3,500 fields in the workbook.
    """
    problems = []
    for row in rows:
        mrn = row.fields["mrn"].value if "mrn" in row.fields else "?"
        for dur, s_key, e_key in _DURATION_BOUNDS:
            d = row.fields.get(dur)
            s = row.fields.get(s_key)
            e = row.fields.get(e_key)
            if d is None or s is None or e is None:
                problems.append(f"{mrn}: {dur} is missing one of its bound columns entirely")
                continue
            sv, ev, dv = s.value, e.value, d.value
            if bool(sv) != bool(ev):
                problems.append(f"{mrn}: {dur} has only one bound ({s_key}={sv!r}, {e_key}={ev!r}) "
                                f"-- a pair must be all-or-nothing")
                continue
            if not sv or not ev or not isinstance(dv, (int, float)) or isinstance(dv, bool):
                continue
            if _parse_time(sv) is None or _parse_time(ev) is None:
                continue  # already flagged as unreadable on the cell itself
            calc = _minutes_between(sv, ev)
            if calc != dv:
                problems.append(f"{mrn}: {dur} says {dv} min but its bounds {sv}-{ev} "
                                f"subtract to {calc} min")
    return problems


def flag_implausible_values(rows) -> int:
    """Flag numbers that are physically implausible, whatever the model said.

    Deliberately does not correct, and deliberately does not blank the
    value: the reviewer needs to see what was read in order to work out
    what it should have been. 100 is a useful clue that the paper says 60;
    an empty cell is no clue at all.

    Only WEAKENS confidence, never strengthens it -- a value already
    marked low stays low.

    Returns the number of fields flagged.
    """
    n = 0
    for row in rows:
        for key, (lo, hi, unit, why) in _PLAUSIBLE.items():
            fv = row.fields.get(key)
            if fv is None or fv.value is None:
                continue
            v = fv.value
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                continue
            if lo <= v <= hi:
                continue
            note = (f"{v} {unit} is outside the plausible range {lo}-{hi} {unit} "
                    f"({why}) -- check the source digits; the value is kept as read, "
                    f"not corrected")
            fv.flag_reason = f"{fv.flag_reason}; {note}" if fv.flag_reason else note
            fv.confidence = "low"
            n += 1
    return n


# flag_implausible_values() above downgrades any value outside its
# _PLAUSIBLE range to low confidence with a note, but deliberately LEAVES
# THE NUMBER IN THE CELL for everything -- because for a directly-read
# field (age, a duration read off a single clock) the raw value is a
# genuine clue: 100 is a useful hint the paper probably says 60.
#
# LOS and LOS (hours) are not that kind of field. Nothing on the page
# says "LOS" -- it is the SUBTRACTION of two independently-read
# dates/times (date_of_surgery and discharge_datetime, or an OR/anesthesia
# anchor and discharge_datetime). Once either input is wrong, the result
# is not "close to right" the way a misread digit is; it is an arbitrary
# difference that carries no information about the true stay length. A
# reviewer looking at "3289" learns nothing they couldn't get faster from
# the flag itself, and anyone who pulls the LOS column into a summary
# stat -- which is exactly what an LOS column is for -- gets a mean
# skewed by tens of days from a single bad packet, invisibly, because
# averaging a column doesn't see cell comments or yellow fill.
#
# First seen at scale in Ghana 2025 (September): 9 of 62 packets came back
# with LOS from 9 to 3289 days on a mission of same-day/next-day hernia
# repairs (GH25s-909, -07, -12, -24, -57, -59, -60, -73, -76) -- every one
# already correctly flagged low-confidence by flag_implausible_values,
# flag_discharge_is_followup, or the date_of_surgery-window propagation
# below, but still displayed as a literal, alarming number in the LOS
# column. SL19-901 (2026-08, referenced in the _PLAUSIBLE comment above)
# was the same failure mode with a single unflagged-at-the-time 126-day
# packet.
#
# So LOS/LOS (hours) get an exception to the "never blank a flagged
# value, only downgrade it" rule that governs the rest of this module:
# once either is outside its own _PLAUSIBLE range, the VALUE is cleared
# (set to None) in addition to the confidence/flag_reason handling
# flag_implausible_values already did. date_of_surgery, discharge_datetime,
# and every field that fed the computation are left exactly as read,
# still flagged -- a reviewer reconstructs the true LOS from those
# underlying fields, which is the only way to actually get it right
# anyway (rule 18: no guessing which of two misreadings is correct).
def suppress_implausible_los(rows) -> int:
    """Blank (not just flag) an LOS or LOS (hours) value once it falls
    outside its plausible range. See the comment above this function for
    why LOS gets an exception to the module's usual flag-don't-blank rule.

    Must run AFTER flag_implausible_values, flag_discharge_is_followup,
    and flag_dates_outside_window, so their notes are already attached to
    flag_reason before the value disappears -- the comment is the only
    place the original number survives.

    Returns the number of values blanked.
    """
    n = 0
    for row in rows:
        for key in ("los", "los_hours"):
            fv = row.fields.get(key)
            if fv is None or fv.value is None:
                continue
            v = fv.value
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                continue
            lo, hi, unit, _why = _PLAUSIBLE[key]
            if lo <= v <= hi:
                continue
            note = (f"value blanked: {v} {unit} was outside the plausible range "
                    f"{lo}-{hi} {unit} for a day-case mission. LOS is computed from "
                    f"two independently-read dates/times rather than read directly "
                    f"off the page, so an implausible result is not a useful clue to "
                    f"the true value the way a misread digit would be, and left in "
                    f"place it risks silently skewing any average taken over this "
                    f"column. Check the underlying date_of_surgery / "
                    f"discharge_datetime fields (still shown below, still flagged) "
                    f"against the paper to find the real stay length")
            fv.flag_reason = f"{fv.flag_reason}; {note}" if fv.flag_reason else note
            fv.value = None
            fv.confidence = "low"
            n += 1
    return n


# A stay this long on an outreach hernia mission is not a stay, it is a
# misread. Same-day and next-day discharge are the norm; three days would
# be a notably complicated course. Anything at or beyond this, arrived at
# via a date that also happens to be the follow-up appointment, is the
# signature of the confusion described in flag_discharge_is_followup.
_IMPLAUSIBLE_LOS_DAYS = 3


def flag_discharge_is_followup(rows) -> int:
    """Catch a discharge date that is really the follow-up appointment date.

    The "RN Notes, Orders, Discharge" form has two dates on it that mean
    opposite things, and they sit close together. Inside the Discharge
    Orders block is a printed line reading "Discharge to home [ ] Follow up
    on ____ at ____", and the date written there is a FUTURE outpatient
    clinic appointment. The real discharge date/time is on the signature
    lines at the foot of the page.

    In the Sierra Leone run the model took the follow-up line for nine
    patients. It is an understandable misread -- the line is literally
    inside a block headed "Discharge Orders" -- but the consequence is
    severe and quiet: LOS became 6-13 days for day-case hernia repairs.
    SL25-908 is the clearest example. The paper says "Follow up on 12/15/25
    at 0800" and is signed by the MD "12/9/25 1400"; surgery was 12/8.
    True LOS is about one day. The sheet said seven.

    The prompt in extract.py now explains the distinction, and that fixes
    most of it. This exists because a prompt is a request, not a
    guarantee, and the failure is invisible in the output -- a plausible
    integer in a LOS column, carrying "medium" confidence, because the
    model was quite correct about the digits it read. The tell is not the
    handwriting; it is that discharge date == follow-up date AND the
    implied stay is far too long. Both conditions together, so a genuine
    long admission that happens to be discharged on clinic day is not
    swept up.

    Flags, never corrects. The true discharge date may be on the signature
    line, or the page may be undated, and choosing between those is a
    reading of the paper rather than an inference from it (rule 18).

    Returns the number of ROWS flagged.
    """
    n = 0
    for row in rows:
        disch = row.fields.get("discharge_datetime")
        visit = row.fields.get("postop_visit_date")
        los = row.fields.get("los")
        if not (disch and visit and disch.value and visit.value):
            continue
        d_disch = _parse_date(disch.value)
        d_visit = _parse_date(visit.value)
        if d_disch is None or d_visit is None or d_disch != d_visit:
            continue
        days = los.value if los and isinstance(los.value, int) else None
        if days is None or days < _IMPLAUSIBLE_LOS_DAYS:
            continue
        note = (f"discharge date {d_disch.isoformat()} is the SAME as the post-op "
                f"follow-up date, and implies a {days}-day stay for a day-case "
                f"procedure -- this is almost certainly the 'Follow up on ___' line "
                f"of the Discharge Orders block, not the discharge itself. The real "
                f"discharge date/time is on the MD/RN signature line at the foot of "
                f"the form. Check the paper; LOS and LOS (hours) are wrong if so")
        for key in ("discharge_datetime", "los", "los_hours"):
            fv = row.fields.get(key)
            if fv is None:
                continue
            fv.flag_reason = f"{fv.flag_reason}; {note}" if fv.flag_reason else note
            fv.confidence = "low"
        n += 1
    return n


# Dates that must fall INSIDE the mission window. Surgery happened during
# the mission by definition, and the patient reached the ward the same day.
_WINDOW_STRICT = ("date_of_surgery", "floor_arrival")

# Dates that may legitimately fall AFTER the window closes: a patient can
# still be an inpatient, or come back for a follow-up visit, once the team
# has left. Anything BEFORE the window is still wrong for these.
_WINDOW_TRAILING_OK = ("discharge_datetime", "postop_visit_date")

# How long after the mission ends a discharge or follow-up is still
# credible. Six weeks covers the usual post-op visit schedule with room to
# spare; beyond that the date is far likelier to be a misread year or a
# transposed month than a real late follow-up.
_TRAILING_GRACE_DAYS = 45


def flag_dates_outside_window(rows, mission) -> int:
    """Flag any extracted date that falls outside the mission's date window.

    This exists because the window was previously used only to TELL the
    model what to expect, and never to CHECK what it returned. In the
    Sierra Leone vet run two packets came back with a floor arrival in
    October for a December mission -- and both were marked "high"
    confidence, because the model was perfectly certain it had read
    "10/8" correctly. It had. The form is simply written day-first, or the
    digits were transposed. Confidence in the READING says nothing about
    whether the resulting date is possible.

    So this is a cross-check of a different kind: not "is the handwriting
    clear" but "is the answer consistent with something we already know".
    A date outside the window is never silently accepted, and its
    confidence is forced down to low so the flagged-field count and the
    yellow highlighting in the workbook both reflect it.

    Deliberately flags rather than corrects. The obvious "fix" for 10/8 in
    a December mission is to swap it to 8/10 -- but that is a guess about
    which of two conventions the writer used, and rule 18 says ticked
    boxes are transcribed, never inferred. The same reasoning applies to
    digits. A human with the paper in hand settles it in seconds.

    Returns the number of fields it flagged, so the caller can report it.
    """
    win = (mission or {}).get("date_window") or []
    if len(win) != 2:
        return 0
    try:
        lo = date.fromisoformat(str(win[0]))
        hi = date.fromisoformat(str(win[1]))
    except (ValueError, TypeError):
        return 0  # an uncalibrated or placeholder window is not an error

    flagged = 0
    for row in rows:
        for key in _WINDOW_STRICT + _WINDOW_TRAILING_OK:
            fv = row.fields.get(key)
            if fv is None or fv.value is None:
                continue
            d = _parse_date(fv.value)
            if d is None:
                continue  # unparseable values are already flagged elsewhere
            limit = hi + timedelta(days=_TRAILING_GRACE_DAYS) if key in _WINDOW_TRAILING_OK else hi
            if lo <= d <= limit:
                continue
            side = "before" if d < lo else "after"
            note = (f"date {d.isoformat()} falls {side} the mission window "
                    f"({lo.isoformat()} to {hi.isoformat()}) -- treated as a likely "
                    f"documentation error (day/month swap or transposed digit) per "
                    f"questions_for_doc.md #6; requires verification against the paper "
                    f"before trusting it")
            fv.flag_reason = f"{fv.flag_reason}; {note}" if fv.flag_reason else note
            fv.confidence = "low"
            flagged += 1
            # date_of_surgery is downgraded above, but LOS (whole days) was
            # already computed and its confidence locked in back inside
            # reconcile_packet() -- BEFORE this mission-level window check
            # ever runs (the window is a property of the mission, not of a
            # single packet, so it can't run any earlier; see the comment
            # on this call in run_mission.py's build_case_log()). Without
            # this, a date_of_surgery that gets caught and downgraded here
            # leaves its own dependent LOS looking exactly as confident as
            # it did before the bad date was caught -- which is how SL19-901
            # got an unflagged 126-day LOS built on an August date_of_surgery
            # (an 8/12 vs 12/8 day/month misread) paired with a perfectly
            # good December discharge date. Propagate the same way
            # flag_discharge_is_followup() already does for its own case.
            if key == "date_of_surgery":
                los = row.fields.get("los")
                if los is not None and los.value is not None:
                    los_note = (f"built on a date_of_surgery that {note}")
                    los.flag_reason = f"{los.flag_reason}; {los_note}" if los.flag_reason else los_note
                    los.confidence = "low"
    return flagged


_PMH_COLS = ("pmh_htn", "pmh_dmii", "pmh_copd", "pmh_bph", "pmh_other")
_PMH_NO_ROW_REASON = "no PMH checkbox row on this form template"


def flag_pmh_row_inconsistency(rows) -> int:
    """Cross-check for the PMH checkbox row (added 2026-08, same gate that
    added PMH/PSH), same spirit as flag_dates_outside_window above: a
    single packet's own confidence says nothing about whether its answer
    is consistent with what every OTHER packet in the same batch found on
    what is supposed to be the same printed form.

    Concretely: extract.py's H&P prompt has the model report null with
    flag_reason "no PMH checkbox row on this form template" for all four
    PMH condition columns when it decides the row isn't printed on the
    page at all. That's a real case (older/different form templates
    genuinely don't have this row) -- but it is also exactly the kind of
    claim a vision call can get wrong on an otherwise-standard page,
    especially when every box in the row happens to be unticked and blank
    boxes are easy to skim past. The GH25 2025-05 vet gate caught one:
    GH25-902 came back "no checkbox row" on a page that demonstrably prints
    the row with all four boxes unticked.

    So: if ANY row in this batch has a real (non-null) PMH value -- i.e.
    at least one packet's H&P was read as having the row -- then any OTHER
    row that claims "no checkbox row" is suspect, because packets from the
    same mission overwhelmingly share the same form template. Rather than
    silently overriding the model's answer (which would be guessing in the
    other direction), this downgrades confidence to low and rewrites the
    flag into something a reviewer can act on immediately, instead of
    reading like a settled, structural fact about the form.

    Returns the number of rows it flagged, so the caller can report it.
    """
    rows_with_row = 0
    rows_claiming_no_row = []
    for row in rows:
        cols = [row.fields.get(c) for c in _PMH_COLS]
        has_value = any(fv is not None and fv.value is not None for fv in cols)
        claims_no_row = any(
            fv is not None and fv.value is None and fv.flag_reason
            and _PMH_NO_ROW_REASON in fv.flag_reason
            for fv in cols
        )
        if has_value:
            rows_with_row += 1
        elif claims_no_row:
            rows_claiming_no_row.append(row)

    if not rows_with_row or not rows_claiming_no_row:
        return 0

    flagged = 0
    for row in rows_claiming_no_row:
        packet = getattr(row, "packet_id", "this packet")
        note = (f"SUSPECT MISREAD: this page was read as having no PMH checkbox "
                f"row, but {rows_with_row} other packet(s) in this batch DO show "
                f"the row printed -- packets from the same mission normally share "
                f"one form template. Re-check the strip between 'Current Pain "
                f"level' and 'PSH:' on {packet}'s H&P before accepting 'row not "
                f"present'.")
        for c in _PMH_COLS:
            fv = row.fields.get(c)
            if fv is None:
                continue
            fv.flag_reason = f"{fv.flag_reason}; {note}" if fv.flag_reason else note
            fv.confidence = "low"
        flagged += 1
    return flagged
