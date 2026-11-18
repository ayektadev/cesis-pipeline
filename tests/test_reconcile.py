"""Offline checks on reconcile.py's new logic. No API calls, no packets --
just hand-built extraction JSON so each rule can be exercised on its own,
including the ones that are supposed to REFUSE to produce a value.

The main fixture is synthetic. It follows the layout of a real Sierra Leone
2025 packet, but its identifiers, staff initials and clinical values were
altered for the public release; each rule is checked against these values.
"""
import sys
from pathlib import Path

# The pipeline modules live one directory up. Resolving that from __file__
# rather than hard-coding a path is what lets this suite travel with the
# zip and still run from wherever the recipient unpacked it.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from reconcile import reconcile_packet, _parse_time, _split_date_time, _parse_date
from schema import CASE_LOG_COLUMNS


def F(v, c="high", flag=None):
    return {"value": v, "confidence": c, "flag_reason": flag}


def page(ptype, label, **fields):
    return {"page_type": ptype, "page_label": label, "fields": fields}


SL25_1 = {
    "packet_id": "SL25-909",
    "pages": [
        page("Post-Op Visit", "Post-Op Visit p1",
             visit_date=F("12/8/25"), pod=F(7), s_p_procedure=F("(R) IHR"),
             wound_findings=F("CDI"),
             complication=F("possible seroma, expected to resolve"),
             further_surgery_planned=F("L IHR repair planned"),
             other_abnormal_findings=F(None, "high", "line blank")),
        page("H&P", "H&P p2",
             age=F(71), gender=F("F"), preop_dx=F("Right inguinal hernia"),
             postop_dx=F("RIH"), procedure=F("RIH plug/patch"),
             case_duration_mins=F(60), symptom_duration_years=F(2),
             surgeon_assistant=F("SMITH/JONES"), anesthesiologist=F("ANNE/BEN"),
             attending_surgeon=F("ABC"), date=F("12/1/25"),
             hernia_class_h=F("H4"), hernia_class_f=F(None, "low", "no F box circled"),
             inguinoscrotal_component=F(None), hernia_laterality=F("R"),
             hernia_reducibility=F("b/l reducible R>L"), hernia_recurrent=F(None, "low", "not stated"),
             repair_technique=F("plug/patch"), defect_count=F(2),
             ebl_complications=F(None, "high", "checkmark only, no text")),
        page("Consent", "Consent p3"),
        page("Pre-Anesthesia Evaluation", "Pre-Anesthesia Evaluation p4",
             asa_class=F("II"), age_gender_hpi=F("71 y/o F"), date=F("7-12-25", "medium", "written 7-12-25")),
        page("Anesthesia Record", "Anesthesia Record p5",
             date=F("12/1/25"), anesthesia_start=F("0937"), anesthesia_end=F("1057"),
             procedure_start=F("0957"), procedure_end=F("1059"),
             anesthetist=F("XY"), anesthetic_technique=F("Spinal")),
        page("OR Record", "OR Record p6",
             date=F("12/1/25"), in_room=F("09:37"), out_of_room=F(None, "high", "box blank"),
             anesthesia_start=F("09:40"), anesthesia_end=F("10:54"),
             procedure_start=F("09:57"), procedure_end=F("10:59"), time_out=F("09:55"),
             surgeon=F("ABC"), assistant=F("DE"), anesthesiologist=F("FG"),
             surgical_procedure=F("IHR"), procedure_laterality=F("R"), count_correct=F("Y")),
        page("PACU Orders", "PACU Orders p7"),
        page("PACU Flowsheet", "PACU Flowsheet p8",
             date=F("12/1/25"), pacu_arrival_time=F("11:05"), pacu_transfer_time=F("17:33"),
             pacu_complications=F(None)),
        page("Floor Flowsheet", "Floor Flowsheet p9",
             floor_arrival=F("12/1 12:35"), floor_discharge=F(None, "low", "slashes blank")),
        page("Progress Note", "Progress Note p10",
             pod=F(0), s_p=F("s/p RIH"), disposition=F("stable"), complications=F(None)),
        page("RN Discharge", "RN Discharge p11",
             discharge_date=F("12/2/25"), discharge_time=F("10:00"),
             discharge_criteria_met=F("Y"), nursing_complications=F(None)),
    ],
}

fails = []


def check(name, got, want):
    if got != want:
        fails.append(f"{name}: got {got!r}, expected {want!r}")
    print(f"  {'ok ' if got == want else 'FAIL'} {name} = {got!r}")


print("parsing helpers")
check("bare 4-digit time", _parse_time("0937"), "09:37")
check("colon time", _parse_time("09:37"), "09:37")
check("pm time", _parse_time("9:40 pm"), "21:40")
check("date digits not read as a time", _split_date_time("12/8/25 09:40", 2025)[1], "09:40")
check("date half", _split_date_time("12/8/25 09:40", 2025)[0].isoformat(), "2025-12-08")
check("month-first", _parse_date("12/8/25").isoformat(), "2025-12-08")
check("impossible date refused", _parse_date("13/45/25"), None)
check("no date in text", _parse_date("stable"), None)

print("\nSL25-1 fixture")
row = reconcile_packet(SL25_1)[0]
v = {c: fv.value for c, fv in row.fields.items()}

check("every schema column present", sorted(row.fields) == sorted(CASE_LOG_COLUMNS), True)
check("Case Duration retired -- no case_duration column remains (questions_for_doc.md #2)",
      "case_duration" in row.fields, False)
check("  ... nor its bounds", ("case_duration_start" in row.fields or "case_duration_stop" in row.fields), False)
check("  ... nor the secondary-procedure counterpart", "secondary_case_duration_mins" in row.fields, False)
check("h_score", v["h_score"], "H4")
check("F score absent, not invented", v["hernia_f_score"], None)
check("  ... unmarked F box is NOT flagged (questions_for_doc.md #5, answered)",
      row.fields["hernia_f_score"].needs_review, False)
check("  ... and stays at high confidence", row.fields["hernia_f_score"].confidence, "high")
check("laterality", v["hernia_laterality"], "R")
check("repair technique", v["repair_technique"], "plug/patch")
check("defect count", v["defect_count"], 2)
check("surgery time from bare digits", v["surgery_time_mins"], 62)
check("anesthesia time", v["anesthesia_time_mins"], 80)
# OR Time Out is blank on both the fixture's Anesthesia Record (no OR Time
# row at all) and its OR Record (out_of_room explicitly blank). Client
# direction, 2026-08 (SL19 vet gate): when this happens, substitute the
# Anesthesia End time as the Out rather than leaving OR Time null -- see
# reconcile.py's _compute_times_for_procedure and questions_for_doc.md #2's
# second amendment. The fixture's Anesthesia End is 10:57 (used above for
# Anesthesia Time), so 09:37 (in-room) to 10:57 is 80 min.
check("OR time falls back to Anesthesia End when Out is blank everywhere", v["or_time_mins"], 80)
check("  ... forced to low confidence, never treated as a clean reading",
      row.fields["or_time_mins"].confidence, "low")
check("  ... and the substitution is named in the flag",
      "substituted the Anesthesia End" in (row.fields["or_time_mins"].flag_reason or ""), True)
check("PACU arrival", v["pacu_arrival_time"], "11:05")
check("PACU minutes", v["pacu_los_mins"], 388)
check("floor arrival normalised", v["floor_arrival"], "2025-12-01 12:35")
check("discharge datetime", v["discharge_datetime"], "2025-12-02 10:00")
check("LOS days", v["los"], 1)
check("LOS hours from in-room", v["los_hours"], 24.4)
check("max POD", v["max_pod_documented"], 0)
check("discharge criteria", v["discharge_criteria_met"], "Y")
check("post-op visit POD", v["postop_visit_pod"], 7)
check("post-op complication", v["postop_complication"], "possible seroma, expected to resolve")
check("further surgery", v["further_surgery_planned"], "L IHR repair planned")
check("seen post op", v["seen_post_op"], "Y")
check("no phantom secondary procedure", v["secondary_procedure"], None)
check("complications exclude the blank/'none' fields",
      v["complications_notes"], "Post-Op Visit p1: possible seroma, expected to resolve")

print("\nrefusals: a packet with almost nothing filled in")
BARE = {"packet_id": "SL25-99", "pages": [
    page("H&P", "H&P p1", age=F(50), gender=F("M"), procedure=F("Hysterectomy"),
         date=F("12/8/25")),
    page("Unclassified", "Unclassified p2"),
]}
b = reconcile_packet(BARE)[0]
bv = {c: fv.value for c, fv in b.fields.items()}
check("no discharge invented", bv["discharge_datetime"], None)
check("LOS not defaulted to 1", bv["los"], None)
check("seen_post_op not defaulted to Y", bv["seen_post_op"], None)
check("h_score null for a non-hernia case", bv["h_score"], None)
check("non-hernia detail unflagged", b.fields["repair_technique"].needs_review, False)
check("missing discharge IS flagged", b.fields["discharge_datetime"].needs_review, True)

print("\nrefusals: contradictory and out-of-range readings")
BAD = {"packet_id": "SL25-98", "pages": [
    page("H&P", "H&P p1", age=F(40), procedure=F("Left inguinal hernia repair"),
         date=F("12/8/25"), hernia_class_h=F("H7"), hernia_laterality=F("L"),
         inguinoscrotal_component=F("b")),
    page("OR Record", "OR Record p2", date=F("12/8/25"), procedure_laterality=F("R"),
         in_room=F("09:00"), out_of_room=F("10:00"),
         procedure_start=F("09:10"), procedure_end=F("09:50")),
    page("RN Discharge", "RN Discharge p3", discharge_date=F("12/5/25"), discharge_time=F("10:00")),
]}
x = reconcile_packet(BAD)[0]
check("out-of-range H grade refused", x.fields["h_score"].value, None)
check("out-of-range H grade explains itself", "H7" in (x.fields["h_score"].flag_reason or ""), True)
check("side mismatch flagged", "SIDE MISMATCH" in (x.fields["hernia_laterality"].flag_reason or ""), True)
check("discharge before surgery refused", x.fields["los"].value, None)

print("\nevery column defines itself (RULES.md rule 21)")
import schema as _schema
_missing = [c for c in _schema.CASE_LOG_COLUMNS if not _schema.COLUMN_DEFINITIONS.get(c)]
check("no column lacks a definition", _missing, [])
_orphans = [c for c in _schema.COLUMN_DEFINITIONS if c not in _schema.CASE_LOG_COLUMNS]
check("no definition for a column that does not exist", _orphans, [])
# A definition that just restates the header teaches nobody anything. The
# derived columns are the ones people misread, so they must say what they
# are computed FROM.
_derived = ["los", "los_hours", "pacu_los_mins", "or_time_mins", "surgery_time_mins",
            "anesthesia_time_mins", "operative_time_mins", "procedure_category"]
_thin = [c for c in _derived
         if len(_schema.COLUMN_DEFINITIONS.get(c, "")) < 60
         or not any(w in _schema.COLUMN_DEFINITIONS[c].lower()
                    for w in ("minus", "from", "derived", "computed", "between"))]
check("derived columns say what they are computed from", _thin, [])
# The two LOS columns are the classic confusion: same name, different
# anchor and different unit. Each must name its own anchor.
check("LOS (days) names its anchor", "date of surgery" in _schema.COLUMN_DEFINITIONS["los"], True)
check("LOS (hours) names its anchor", "in-room" in _schema.COLUMN_DEFINITIONS["los_hours"], True)

print("\nfollow-up date is not mistaken for the discharge date")
import reconcile as _rec
from schema import CaseLogRow as _Row, FieldValue as _FV


def _mk(disch, visit, los_days):
    f = {"mrn": _FV(value="SL25-TEST"),
         "discharge_datetime": _FV(value=disch, confidence="medium"),
         "postop_visit_date": _FV(value=visit),
         "los": _FV(value=los_days, confidence="medium"),
         "los_hours": _FV(value=(los_days or 0) * 24.0, confidence="medium")}
    return _Row(packet_id="SL25-TEST", fields=f)

# The real SL25-908 shape: "discharged" on the follow-up clinic day, seven
# days after a day-case hernia repair. Must be caught.
bad = _mk("2025-12-15 08:00", "2025-12-15", 7)
check("catches discharge == follow-up with an absurd LOS", _rec.flag_discharge_is_followup([bad]), 1)
check("  ... and drops confidence to low", bad.fields["los"].confidence, "low")
check("  ... and flags LOS (hours) too", bad.fields["los_hours"].confidence, "low")
check("  ... and says which line to re-read", "signature" in bad.fields["los"].flag_reason, True)

# Same-day discharge that happens to land on clinic day is NOT the bug.
# Both conditions are required precisely so this case survives untouched.
ok = _mk("2025-12-15 08:00", "2025-12-15", 0)
check("leaves a same-day discharge alone", _rec.flag_discharge_is_followup([ok]), 0)
check("  ... confidence untouched", ok.fields["los"].confidence, "medium")

# A genuinely long stay discharged on a DIFFERENT day is a real long stay.
check("leaves a real long stay alone",
      _rec.flag_discharge_is_followup([_mk("2025-12-14 08:00", "2025-12-20", 6)]), 0)

# Missing pieces must not raise.
check("tolerates a missing discharge date", _rec.flag_discharge_is_followup([_mk(None, "2025-12-15", 7)]), 0)
check("tolerates a missing follow-up date", _rec.flag_discharge_is_followup([_mk("2025-12-15 08:00", None, 7)]), 0)
check("tolerates a null LOS", _rec.flag_discharge_is_followup([_mk("2025-12-15 08:00", "2025-12-15", None)]), 0)

print("\nevery duration ships with the clock times it was computed from (rule 22)")
# THE invariant. A bound that does not subtract to the number beside it is
# worse than no bound, because it looks like corroboration while actually
# contradicting. Checked for every duration on the fixture, and again
# across all 66 real packets by verify_bounds.py.
from reconcile import _minutes_between as _mb
for _dur, _s, _e in [("or_time_mins", "or_time_start", "or_time_stop"),
                     ("surgery_time_mins", "surgery_time_start", "surgery_time_stop"),
                     ("operative_time_mins", "operative_time_start", "operative_time_stop"),
                     ("anesthesia_time_mins", "anesthesia_time_start", "anesthesia_time_stop")]:
    _sv, _ev, _dv = row.fields[_s].value, row.fields[_e].value, row.fields[_dur].value
    if _sv and _ev and _dv is not None:
        check(f"{_dur}: bounds subtract to the stated minutes", _mb(_sv, _ev), _dv)

# The fixture's Anesthesia Record says 0937-1057; the OR Record independently
# says 09:40-10:54. The used value is shown, and the disagreement is named.
check("anesthesia bounds normalised to HH:MM", row.fields["anesthesia_time_start"].value, "09:37")
check("anesthesia stop from the page that was used", row.fields["anesthesia_time_stop"].value, "10:57")
check("conflicting second source is flagged",
      "OR Record says 09:40" in (row.fields["anesthesia_time_start"].flag_reason or ""), True)
check("  ... and drops confidence", row.fields["anesthesia_time_start"].confidence, "medium")

# Both pages agree on the procedure times, so no conflict note.
check("agreeing sources are not flagged", row.fields["surgery_time_start"].flag_reason, None)
check("surgery start normalised", row.fields["surgery_time_start"].value, "09:57")

# out_of_room is blank on the fixture, but the Anesthesia End substitution
# (see above) means OR Time now DOES have a value and a matching bound pair
# -- rule 22 requires the bounds to come from the same branch that produced
# the minutes, so the stop bound must be the substituted Anesthesia End,
# not a blank.
check("OR start still the real in-room reading", row.fields["or_time_start"].value, "09:37")
check("OR stop is the substituted Anesthesia End, not invented separately",
      row.fields["or_time_stop"].value, "10:57")
check("bounds subtract to the same 80 min reported above",
      row.fields["or_time_mins"].value, 80)

# operative time is the surgery measurement under another name; the two
# pairs must not be allowed to drift apart.
check("operative bounds match surgery bounds",
      (row.fields["operative_time_start"].value, row.fields["operative_time_stop"].value),
      (row.fields["surgery_time_start"].value, row.fields["surgery_time_stop"].value))

# Rule 21 applies to the new columns as much as the old ones.
_bound_cols = [c for c in _schema.CASE_LOG_COLUMNS if c.endswith(("_start", "_stop"))]
check("all 8 bound columns exist", len(_bound_cols), 8)
check("every bound column defines itself",
      [c for c in _bound_cols if not _schema.COLUMN_DEFINITIONS.get(c)], [])
check("every bound column says it is transcribed, not derived",
      [c for c in _bound_cols if "TRANSCRIBED" not in _schema.COLUMN_DEFINITIONS[c]], [])
# Each pair must sit immediately before the duration it explains.
for _dur, _s, _e in [("operative_time_mins", "operative_time_start", "operative_time_stop"),
                     ("or_time_mins", "or_time_start", "or_time_stop"),
                     ("surgery_time_mins", "surgery_time_start", "surgery_time_stop"),
                     ("anesthesia_time_mins", "anesthesia_time_start", "anesthesia_time_stop")]:
    _i = _schema.CASE_LOG_COLUMNS.index(_dur)
    check(f"{_dur} is preceded by its bounds",
          _schema.CASE_LOG_COLUMNS[_i - 2:_i], [_s, _e])

print("\nphysically implausible numbers are flagged whatever the model claimed")
# The real SL25-907: H&P Age box reads 60, model returned 100 at HIGH
# confidence. Confidence cannot catch this; a bound can.
r = _Row(packet_id="T", fields={"age": _FV(value=100, confidence="high")})
check("catches an implausible age at high confidence", _rec.flag_implausible_values([r]), 1)
check("  ... forces it to low", r.fields["age"].confidence, "low")
check("  ... keeps the value visible as a clue", r.fields["age"].value, 100)
check("  ... names the range", "1-95" in r.fields["age"].flag_reason, True)

ok = _Row(packet_id="T", fields={"age": _FV(value=60, confidence="high")})
check("leaves a plausible age alone", _rec.flag_implausible_values([ok]), 0)
check("  ... confidence untouched", ok.fields["age"].confidence, "high")

# Boundary values are inside the range, not outside it.
edge = _Row(packet_id="T", fields={"age": _FV(value=95), "los_hours": _FV(value=0.5)})
check("bounds are inclusive", _rec.flag_implausible_values([edge]), 0)

# Non-numbers and blanks must not raise or be flagged.
odd = _Row(packet_id="T", fields={"age": _FV(value=None), "los_hours": _FV(value="N/A"),
                                  "postop_visit_pod": _FV(value=True)})
check("ignores blanks, text and booleans", _rec.flag_implausible_values([odd]), 0)

# The absurd durations found in the real run.
dur = _Row(packet_id="T", fields={"anesthesia_time_mins": _FV(value=1411, confidence="high"),
                                  "or_time_mins": _FV(value=212, confidence="high")})
check("catches a 23-hour anesthesia time", _rec.flag_implausible_values([dur]), 1)
check("  ... but not a long-but-possible OR time", dur.fields["or_time_mins"].confidence, "high")

print("\nF score IS captured, at normal confidence, whenever the box is marked (Q5)")
FSCORE = {"packet_id": "SL25-97", "pages": [
    page("H&P", "H&P p1", age=F(30), procedure=F("Right inguinal hernia repair"),
         date=F("12/8/25"), hernia_class_h=F("H2"), hernia_class_f=F("F3"),
         hernia_laterality=F("R")),
]}
fsc = reconcile_packet(FSCORE)[0]
check("F score captured when the box IS marked", fsc.fields["hernia_f_score"].value, "F3")
check("  ... at normal (high) confidence, not flagged as a gap",
      fsc.fields["hernia_f_score"].confidence, "high")

print("\nundated discharge signature block falls back to the last dated entry (Q4)")
UNDATED = {"packet_id": "SL25-96", "pages": [
    page("H&P", "H&P p1", age=F(40), procedure=F("Right inguinal hernia repair"), date=F("12/8/25")),
    page("OR Record", "OR Record p2", date=F("12/8/25"), in_room=F("09:00"), out_of_room=F("10:00")),
    page("Floor Flowsheet", "Floor Flowsheet p3",
         floor_arrival=F("12/8 11:00"), floor_discharge=F(None, "low", "slashes blank")),
    page("RN Discharge", "RN Discharge p4",
         discharge_date=F(None, "low", "signature block undated"),
         discharge_time=F(None, "low", "signature block undated"),
         last_entry_date=F("12/9/25")),
]}
u = reconcile_packet(UNDATED)[0]
check("last_entry_date captured for transparency", u.fields["last_entry_date"].value, "2025-12-09")
check("discharge falls back to the last dated entry", u.fields["discharge_datetime"].value, "2025-12-09 00:00")
check("  ... names last_entry_date as the source",
      "last dated entry" in (u.fields["discharge_datetime"].flag_reason or ""), True)
check("  ... at low confidence (a date, not a recorded discharge time)",
      u.fields["discharge_datetime"].confidence, "low")

# A form that names its discharge date directly must never be overridden by
# last_entry_date -- the fallback is strictly a last resort.
DATED = {"packet_id": "SL25-95", "pages": [
    page("H&P", "H&P p1", age=F(40), procedure=F("Right inguinal hernia repair"), date=F("12/8/25")),
    page("RN Discharge", "RN Discharge p2", discharge_date=F("12/9/25"), discharge_time=F("09:00"),
         last_entry_date=F("12/10/25")),
]}
dtd = reconcile_packet(DATED)[0]
check("a real discharge date/time is never overridden by last_entry_date",
      dtd.fields["discharge_datetime"].value, "2025-12-09 09:00")

print("\nparse_date_day_first -- the self-calibration date-order helper")
from reconcile import parse_date_day_first as _pdf
check("day-first reading of an ambiguous date", _pdf("09/12/24", 2024).isoformat(), "2024-12-09")
check("month-first reading of the same digits", _parse_date("09/12/24", 2024).isoformat(), "2024-09-12")
check("ISO dates are not ambiguous -- no day-first reading offered", _pdf("2024-12-09"), None)
check("unparseable text", _pdf("stable"), None)

print("\nself-calibration auto-detects a mixed day/month convention on the Floor Flowsheet")
import json as _json, tempfile
import run_mission as _rm


def _write_raw(tmpdir, pid, floor_arrival, hp_date):
    doc = {"packet_id": pid, "pages": [
        {"page_type": "H&P", "fields": {"date": {"value": hp_date}}},
        {"page_type": "Floor Flowsheet", "fields": {"floor_arrival": {"value": floor_arrival}}},
    ]}
    p = Path(tmpdir) / f"{pid}.json"
    p.write_text(_json.dumps(doc))
    return p


with tempfile.TemporaryDirectory() as _td:
    _paths = [
        _write_raw(_td, "SL24-902", "12/9/24 13:00", "12/9/24"),   # month-first, agrees w/ window
        _write_raw(_td, "SL24-903", "09/12/24 13:02", "12/11/24"),  # only makes sense day-first
        _write_raw(_td, "SL24-904", "12/10/24 09:00", "12/10/24"),  # month-first, agrees w/ window
    ]
    _findings = _rm.calibrate_from_jsons(_paths, {"year": 2024})
    check("date window detected from the (trusted) H&P dates", _findings.get("date_window"),
          ["2024-12-09", "2024-12-11"])
    check("exactly the one day-first-only Floor Flowsheet value is caught",
          _findings.get("floor_flowsheet_day_first_count"), 1)

    _mission = {"extraction_hints": []}
    _profile_path = str(Path(_td) / "profile.json")
    _changed = _rm.apply_calibration(_findings, _mission, _profile_path, 3)
    check("extraction_hints gains a Floor Flowsheet day/month warning",
          any("Floor Flowsheet" in h and "day-first" in h for h in _mission["extraction_hints"]), True)
    check("apply_calibration reports the change in its return value",
          any("day/month-order warning" in c for c in _changed), True)
    check("the hint never claims to have corrected anything (rule 18)",
          any("TRANSCRIBE" in h for h in _mission["extraction_hints"]), True)

    # Re-calibrating (e.g. a later vet run against more packets) must not
    # duplicate the hint.
    _rm.apply_calibration(_findings, _mission, _profile_path, 3)
    check("re-running calibration does not add a second copy of the hint",
          len(_mission["extraction_hints"]), 1)

print("\nanesthetist (name) vs anesthesia_type (technique) are kept separate")
check("anesthetist comes from the Anesthesia Record's own name field", v["anesthetist"], "XY")
check("  ... not the H&P's 'ANNE/BEN' (that page is pre-op, not the source of truth)",
      v["anesthetist"] != "ANNE/BEN", True)
check("anesthesia_type comes from the Anesthesia Record's technique field", v["anesthesia_type"], "Spinal")
check("  ... anesthetist is never contaminated with the technique word",
      "spinal" in v["anesthetist"].lower(), False)

# A packet with NO Anesthesia Record (common when that form is missing from
# the scan) should fall back to the OR Record's own name/technique fields --
# and, crucially, NOT the H&P's, since the H&P's "anesthesiologist" line has
# been seen with the technique bundled into the name on this mission
# (e.g. "Spinal/[name removed]" transcribed verbatim off a single handwritten line).
SL24_OR_FALLBACK = {
    "packet_id": "SL24-FALLBACK",
    "pages": [
        page("H&P", "H&P p2",
             age=F(60), gender=F("M"), preop_dx=F("Left inguinal hernia"),
             postop_dx=F("LIH"), procedure=F("LIH repair"),
             anesthesiologist=F("Spinal/[name removed]"), anesthesia_planned=F("Spinal"),
             date=F("12/9/24")),
        page("OR Record", "OR Record p6",
             date=F("12/9/24"), in_room=F("09:00"), out_of_room=F("10:00"),
             surgeon=F("ABC"), anesthesiologist=F("FG"), anesthesia_type=F("Spinal")),
    ],
}
row2 = reconcile_packet(SL24_OR_FALLBACK)[0]
v2 = {c: fv.value for c, fv in row2.fields.items()}
check("no Anesthesia Record -> anesthetist falls back to the OR Record's clean name",
      v2["anesthetist"], "FG")
check("  ... NOT the H&P's conflated 'Spinal/[name removed]'", v2["anesthetist"] != "Spinal/[name removed]", True)
check("no Anesthesia Record -> anesthesia_type falls back to the OR Record's technique",
      v2["anesthesia_type"], "Spinal")

# When NEITHER the Anesthesia Record NOR the OR Record has a name, the H&P
# is used as a last resort -- but flagged, since that's the line known to
# sometimes mix in the technique.
SL24_HP_ONLY = {
    "packet_id": "SL24-HPONLY",
    "pages": [
        page("H&P", "H&P p2",
             age=F(45), gender=F("F"), preop_dx=F("Umbilical hernia"),
             postop_dx=F("Umbilical hernia"), procedure=F("Umbilical hernia repair"),
             anesthesiologist=F("Spinal/[name removed]"), anesthesia_planned=F("Spinal"),
             date=F("12/9/24")),
    ],
}
row3 = reconcile_packet(SL24_HP_ONLY)[0]
check("H&P-only last resort still surfaces a value", row3.fields["anesthetist"].value, "Spinal/[name removed]")
check("  ... but flagged as needing verification, not treated as trustworthy",
      row3.fields["anesthetist"].needs_review, True)
check("H&P-only anesthesia_type is flagged as the PLAN, not confirmed actual",
      row3.fields["anesthesia_type"].needs_review, True)

print("\nOR Time falls back to Anesthesia End only when Out is truly missing everywhere (SL19 vet gate)")
# In found, Out blank on both forms, but an Anesthesia End IS available --
# already exercised on the main SL25_1 fixture above. Here: the case where
# NOTHING is available to substitute either, which must still come back
# null rather than inventing a number.
OR_NO_FALLBACK = {
    "packet_id": "T-NOFALLBACK",
    "pages": [
        page("H&P", "H&P p1", age=F(40), procedure=F("Right inguinal hernia repair"), date=F("12/8/25")),
        page("Anesthesia Record", "Anesthesia Record p1",
             date=F("12/8/25"), anesthesia_start=F("0900")),  # no anesthesia_end at all
        page("OR Record", "OR Record p1",
             date=F("12/8/25"), in_room=F("09:00"), out_of_room=F(None)),
    ],
}
row_nf = reconcile_packet(OR_NO_FALLBACK)[0]
check("no Anesthesia End anywhere -- OR Time stays null, not guessed",
      row_nf.fields["or_time_mins"].value, None)
check("  ... flag says so explicitly",
      "no Anesthesia End was available to substitute" in (row_nf.fields["or_time_mins"].flag_reason or ""), True)

# Confirm the substitution never fires when the Anesthesia Record's OWN OR
# Time row is present and complete -- that's the SAME measurement recorded
# in its usual place, and must keep winning outright over any fallback.
OR_ROW_WINS = {
    "packet_id": "T-ORROWWINS",
    "pages": [
        page("H&P", "H&P p1", age=F(40), procedure=F("Right inguinal hernia repair"), date=F("12/8/25")),
        page("Anesthesia Record", "Anesthesia Record p1",
             date=F("12/8/25"), anesthesia_start=F("0900"), anesthesia_end=F("1100"),
             or_time_in=F("0905"), or_time_out=F("1015")),
        page("OR Record", "OR Record p1",
             date=F("12/8/25"), in_room=F("09:05"), out_of_room=F(None, "high", "box blank")),
    ],
}
row_win = reconcile_packet(OR_ROW_WINS)[0]
check("Anesthesia Record's own complete OR Time row still wins outright",
      row_win.fields["or_time_mins"].value, 70)
check("  ... at its own (high) confidence, not downgraded by the unrelated fallback logic",
      row_win.fields["or_time_mins"].confidence, "high")

print()
if fails:
    print(f"{len(fails)} FAILURES:")
    for f in fails:
        print("  -", f)
    sys.exit(1)
print("all checks passed")
