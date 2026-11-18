"""Offline checks for the Ghana 2026 (October) additions: reconcile_options,
the EuraHS-QoL block and the social-history block. No API calls.
Run:  python3 tests/test_ghana26o_options.py"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from reconcile import reconcile_packet  # noqa: E402

OPTS = {"chronological_or_order": True, "or_record_timing_fallback": True,
        "drop_same_day_postop_visits": True}
FAILS = []


def F(v, c="high", r=None):
    return {"value": v, "confidence": c, "flag_reason": r}


def page(ptype, i, **fields):
    return {"page_type": ptype, "page_label": f"{ptype} p{i}",
            "fields": {k: (v if isinstance(v, dict) else F(v)) for k, v in fields.items()}}


def check(label, got, want):
    ok = got == want
    print(f"  {'ok  ' if ok else 'FAIL'}  {label} = {got!r}" + ("" if ok else f" (wanted {want!r})"))
    if not ok:
        FAILS.append(label)


hp = page("H&P", 1, age=40, gender="M", procedure="R IHR w/ mesh", date="10/6/26",
          nicotine="Current", etoh=F(None, "low", "no option ticked"), occupation="Farmer",
          substance_use="Denies", travel_from="Kumasi", travel_distance="45 km")

# 1. takeback scanned first -> chronological order makes the earlier case primary
raw = {"packet_id": "T-1", "pages": [
    hp,
    page("Anesthesia Record", 2, date="10/6/26", anesthesia_start="17:20", anesthesia_end="20:05",
         procedure_start="17:25", procedure_end="19:49"),
    page("Anesthesia Record", 3, date="10/6/26", anesthesia_start="11:25", anesthesia_end="13:45",
         procedure_start="11:42", procedure_end="13:37"),
    page("OR Record", 4, date="10/6/26", in_room="17:20", out_of_room="20:00",
         procedure_start="17:25", procedure_end="19:49", surgical_procedure="Other: hematoma evacuation"),
    page("OR Record", 5, date="10/6/26", in_room="11:25", out_of_room="13:39",
         procedure_start="11:42", procedure_end="13:37", surgical_procedure="IHR"),
]}
print("chronological_or_order")
f_off = reconcile_packet(raw)[0].fields
f_on = reconcile_packet(raw, OPTS)[0].fields
check("off: primary surgery start (scan order)", f_off["surgery_time_start"].value, "17:25")
check("on: primary surgery start (earliest)", f_on["surgery_time_start"].value, "11:42")
check("on: secondary procedure = the takeback", f_on["secondary_procedure"].value,
      "Other: hematoma evacuation")
check("on: complication wording", (f_on["complications_notes"].value or "").split(" |")[0],
      "unplanned return to OR during the same admission -- see Secondary Procedure and the notes "
      "below for the reason")

# 2. or_record_timing_fallback: date vote + impossible STOP times
raw2 = {"packet_id": "T-2", "pages": [
    page("H&P", 1, age=56, gender="M", procedure="L IHR", date="10/5/26"),
    page("Anesthesia Record", 2, date="10/04/26", anesthesia_start="10:56",
         anesthesia_end="13:40", procedure_start="11:17", procedure_end="12:54"),
    page("OR Record", 3, date="10/5/26", in_room="10:56", out_of_room="12:27",
         anesthesia_start="10:56", anesthesia_end=F(None, "low", "blank"),
         procedure_start="11:17", procedure_end="12:24"),
    page("PACU Flowsheet", 4, date="10/5/26", pacu_arrival_time="12:19", pacu_transfer_time="13:30"),
]}
print("or_record_timing_fallback")
f_off = reconcile_packet(raw2)[0].fields
f_on = reconcile_packet(raw2, OPTS)[0].fields
check("off: date from Anesthesia Record", f_off["date_of_surgery"].value, "10/04/26")
check("on: date from OR Record (2 forms agree)", f_on["date_of_surgery"].value, "2026-10-05")
check("on: impossible procedure end replaced by OR Record", f_on["surgery_time_stop"].value, "12:24")
check("on: impossible anesthesia end with blank OR box -> blank", f_on["anesthesia_time_mins"].value, None)
check("on: ...and the reason says why", "impossible time" in (f_on["anesthesia_time_mins"].flag_reason or ""),
      True)

# 3. same-day 'post-op visit' dropped
raw3 = {"packet_id": "T-3", "pages": raw2["pages"] + [
    page("Post-Op Visit", 5, visit_date="10/5/26", pod=F(None, "low", "blank"))]}
print("drop_same_day_postop_visits")
check("off: counted as a visit", reconcile_packet(raw3)[0].fields["postop_visit_date"].value, "2026-10-05")
check("on: not counted", reconcile_packet(raw3, OPTS)[0].fields["postop_visit_date"].value, None)

# 4. EuraHS-QoL block + social history (not option-gated: new columns)
eu = page("EuraHS-QoL", 9, form_timepoint="pre", date_of_birth="24/9/1964", sex="M",
          pain_rest=5, pain_activity=0, pain_worst_week=8,
          restrict_daily=0, restrict_outside=0, restrict_sports="X", restrict_heavy_labour=1,
          cosmetic_abdomen_shape=0, cosmetic_hernia_site=0, unassigned_marks=F(None))
raw4 = {"packet_id": "T-4", "pages": raw["pages"] + [eu]}
print("EuraHS-QoL + social history")
f = reconcile_packet(raw4, OPTS)[0].fields
check("pain score", f["qol_pain_score"].value, 13)
check("restriction score blank (X present)", f["qol_restriction_score"].value, None)
check("cosmetic score", f["qol_cosmetic_score"].value, 0)
check("total blank (a domain is incomplete)", f["qol_total_score"].value, None)
check("restrict_sports keeps X", f["restrict_sports"].value, "X")
check("DOB 1964 vs H&P age 40 -> age flagged", "implies age 62" in (f["age"].flag_reason or ""), True)
check("nicotine", f["social_nicotine"].value, "Current")
check("unticked ETOH stays blank, flagged", (f["social_etoh"].value, f["social_etoh"].confidence), (None, "low"))
check("travel distance verbatim", f["travel_distance"].value, "45 km")
f_none = reconcile_packet(raw2)[0].fields
check("no questionnaire -> blank, unflagged",
      (f_none["qol_total_score"].value, f_none["qol_total_score"].needs_review), (None, False))
check("old H&P without social keys -> blank, unflagged",
      (f_none["social_nicotine"].value, f_none["social_nicotine"].needs_review), (None, False))

print("\nall checks passed" if not FAILS else f"\n{len(FAILS)} FAILED: {FAILS}")
sys.exit(1 if FAILS else 0)
