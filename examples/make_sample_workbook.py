"""
make_sample_workbook.py

Builds examples/SAMPLE_case_log.xlsx so someone can see exactly what this
pipeline produces before running it on real packets:

    python3 examples/make_sample_workbook.py

The patients in it are INVENTED. Not anonymised, not scrubbed, not
shifted -- invented, here in this file, from nothing.

That distinction is the whole point. The obvious way to make a shareable
sample is to take a real case log and strip the identifiers, and the
obvious way is wrong, because de-identification is a promise you can only
keep if you enumerated every field that could betray a patient. This
schema has 55 columns, several of them free text where a nurse may have
written anything at all, and a mission is a small enough cohort that a
date plus an age plus a laterality can be enough on its own. Getting that
list exhaustively right, and keeping it right every time a column is
added, is a standing obligation. Generating fictional patients instead
costs about the same effort and carries no such obligation: there is no
real person behind these rows to fail to protect.

The clinical shapes are realistic -- plausible times, a spinal, a
recurrent hernia, a seroma at follow-up -- because a sample where every
value is "TEST" teaches a reviewer nothing about whether the columns make
sense. The names are deliberately not name-shaped.

Three of the eight cases are deliberately imperfect, because a sample
showing only clean rows would misrepresent the tool:

  DEMO-03  a blank out-of-room box, so OR Time is empty rather than
           guessed, and its bound columns are empty with it
  DEMO-05  the OR and Anesthesia Records disagree on anesthesia start,
           so the bound cell shows the value used and names the other
  DEMO-07  an age of 100, high confidence, caught by the plausibility
           guard rather than by the model doubting itself

Those are the three behaviours worth understanding before trusting any
output, and they are much easier to understand seen than described.
"""

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from reconcile import (  # noqa: E402
    flag_discharge_is_followup,
    flag_implausible_values,
    mrn_sort_key,
    reconcile_packet,
    verify_duration_bounds,
)
from xlsx_writer import write_case_log  # noqa: E402


def F(v, c="high", flag=None):
    return {"value": v, "confidence": c, "flag_reason": flag}


def page(ptype, label, **fields):
    return {"page_type": ptype, "page_label": label, "fields": fields}


def packet(pid, *, age, gender, side, technique, h_class, sx_years, defects,
           recurrent, surg_date, in_room, out_room, proc_start, proc_end,
           anes_start, anes_end, pacu_in, pacu_out, disch_date, disch_time,
           visit_date, pod, wound, complication=None,
           or_anes_start=None, asa="II", anesthetic="Spinal"):
    """One invented packet in the shape extract.py emits.

    Every argument is a value a human would have written on the paper, so
    a reader comparing this file to a real scan can see which box each one
    came out of. `or_anes_start` exists only to let a caller introduce a
    disagreement between the two records on purpose.
    """
    laterality = {"R": "Right", "L": "Left"}[side]
    return {
        "packet_id": pid,
        "pages": [
            page("H&P", "H&P p1",
                 age=F(age), gender=F(gender),
                 preop_dx=F(f"{laterality} inguinal hernia"),
                 postop_dx=F(f"{side}IH"),
                 procedure=F(f"{side}IHR {technique}"),
                 symptom_duration_years=F(sx_years),
                 surgeon_assistant=F("SURG-A/ASSIST-B"),
                 anesthesiologist=F("ANES-C"),
                 attending_surgeon=F("SURG-A"),
                 date=F(surg_date),
                 hernia_class_h=F(h_class),
                 hernia_laterality=F(side),
                 hernia_reducibility=F("reducible"),
                 hernia_recurrent=F(recurrent),
                 repair_technique=F(technique),
                 defect_count=F(defects)),
            page("Pre-Anesthesia Evaluation", "Pre-Anesthesia Evaluation p2",
                 asa_class=F(asa), age_gender_hpi=F(f"{age} y/o {gender}"),
                 date=F(surg_date)),
            page("Anesthesia Record", "Anesthesia Record p3",
                 date=F(surg_date),
                 anesthesia_start=F(anes_start), anesthesia_end=F(anes_end),
                 procedure_start=F(proc_start), procedure_end=F(proc_end),
                 anesthetist=F("ANES-C"), anesthetic_technique=F(anesthetic)),
            page("OR Record", "OR Record p4",
                 date=F(surg_date),
                 in_room=F(in_room),
                 out_of_room=F(out_room) if out_room else F(None, "high", "box blank"),
                 anesthesia_start=F(or_anes_start or anes_start),
                 anesthesia_end=F(anes_end),
                 procedure_start=F(proc_start), procedure_end=F(proc_end),
                 surgeon=F("SURG-A"), assistant=F("ASSIST-B"),
                 surgical_procedure=F("IHR"), procedure_laterality=F(side),
                 count_correct=F("Y")),
            page("PACU Flowsheet", "PACU Flowsheet p5",
                 date=F(surg_date),
                 pacu_arrival_time=F(pacu_in), pacu_transfer_time=F(pacu_out),
                 pacu_complications=F(None)),
            page("RN Discharge", "RN Discharge p6",
                 discharge_date=F(disch_date), discharge_time=F(disch_time),
                 discharge_criteria_met=F("Y"), nursing_complications=F(None)),
            page("Post-Op Visit", "Post-Op Visit p7",
                 visit_date=F(visit_date), pod=F(pod),
                 s_p_procedure=F(f"({side}) IHR"),
                 wound_findings=F(wound),
                 complication=F(complication),
                 further_surgery_planned=F(None)),
        ],
    }


# A fictional mission: eight patients, three operating days in March 2024,
# a site and a year that correspond to no ISHI trip.
PACKETS = [
    packet("DEMO-01", age=54, gender="M", side="R", technique="Lichtenstein",
           h_class="H2", sx_years=3, defects=1, recurrent="N",
           surg_date="3/4/24", in_room="08:12", out_room="09:20",
           proc_start="08:31", proc_end="09:12",
           anes_start="08:15", anes_end="09:18",
           pacu_in="09:25", pacu_out="11:40",
           disch_date="3/4/24", disch_time="14:30",
           visit_date="3/11/24", pod=7, wound="CDI"),

    packet("DEMO-02", age=38, gender="M", side="L", technique="plug/patch",
           h_class="H1", sx_years=1, defects=1, recurrent="N",
           surg_date="3/4/24", in_room="09:35", out_room="10:28",
           proc_start="09:52", proc_end="10:21",
           anes_start="09:38", anes_end="10:25",
           pacu_in="10:33", pacu_out="12:05",
           disch_date="3/4/24", disch_time="15:10",
           visit_date="3/11/24", pod=7, wound="CDI"),

    # Out-of-room box left blank. OR Time and both its bounds stay empty --
    # the pipeline will not infer a discharge from the room it cannot see.
    packet("DEMO-03", age=67, gender="F", side="R", technique="Lichtenstein",
           h_class="H3", sx_years=6, defects=2, recurrent="N",
           surg_date="3/4/24", in_room="10:45", out_room=None,
           proc_start="11:02", proc_end="11:58",
           anes_start="10:48", anes_end="12:04",
           pacu_in="12:10", pacu_out="14:50",
           disch_date="3/5/24", disch_time="09:15",
           visit_date="3/11/24", pod=7, wound="CDI"),

    packet("DEMO-04", age=45, gender="M", side="R", technique="Lichtenstein",
           h_class="H2", sx_years=2, defects=1, recurrent="N",
           surg_date="3/5/24", in_room="08:05", out_room="09:02",
           proc_start="08:22", proc_end="08:55",
           anes_start="08:08", anes_end="08:59",
           pacu_in="09:08", pacu_out="11:20",
           disch_date="3/5/24", disch_time="13:45",
           visit_date="3/12/24", pod=7, wound="CDI"),

    # The two records disagree about when anesthesia started: 09:41 on the
    # Anesthesia Record, 09:45 on the OR Record. The bound cell shows the
    # one that was used and says what the other page claimed.
    packet("DEMO-05", age=72, gender="M", side="L", technique="plug/patch",
           h_class="H4", sx_years=10, defects=2, recurrent="Y",
           surg_date="3/5/24", in_room="09:38", out_room="11:05",
           proc_start="10:01", proc_end="10:52",
           anes_start="09:41", anes_end="11:00", or_anes_start="09:45",
           pacu_in="11:12", pacu_out="14:35",
           disch_date="3/6/24", disch_time="10:20",
           visit_date="3/12/24", pod=7, wound="CDI",
           complication="small seroma, expected to resorb"),

    packet("DEMO-06", age=29, gender="F", side="R", technique="Lichtenstein",
           h_class="H1", sx_years=1, defects=1, recurrent="N",
           surg_date="3/5/24", in_room="11:20", out_room="12:14",
           proc_start="11:37", proc_end="12:06",
           anes_start="11:23", anes_end="12:11",
           pacu_in="12:20", pacu_out="14:05",
           disch_date="3/5/24", disch_time="16:40",
           visit_date="3/12/24", pod=7, wound="CDI"),

    # Age recorded as 100 at high confidence -- the model read the digits
    # cleanly, it just read the wrong ones. Legibility cannot catch this;
    # the plausibility bound can.
    packet("DEMO-07", age=100, gender="M", side="R", technique="Lichtenstein",
           h_class="H3", sx_years=4, defects=1, recurrent="N",
           surg_date="3/6/24", in_room="08:15", out_room="09:30",
           proc_start="08:34", proc_end="09:21",
           anes_start="08:18", anes_end="09:27",
           pacu_in="09:38", pacu_out="12:10",
           disch_date="3/6/24", disch_time="15:00",
           visit_date="3/13/24", pod=7, wound="CDI", asa="III"),

    packet("DEMO-08", age=61, gender="M", side="L", technique="plug/patch",
           h_class="H2", sx_years=5, defects=1, recurrent="N",
           surg_date="3/6/24", in_room="09:50", out_room="10:47",
           proc_start="10:08", proc_end="10:40",
           anes_start="09:53", anes_end="10:44",
           pacu_in="10:55", pacu_out="13:15",
           disch_date="3/6/24", disch_time="16:25",
           visit_date="3/13/24", pod=7, wound="CDI",
           anesthetic="General"),
]


def main():
    rows = []
    for raw in PACKETS:
        rows.extend(reconcile_packet(raw))
    rows.sort(key=lambda r: mrn_sort_key(r.fields["mrn"].value or r.packet_id))

    # The same cross-checks the real build runs. Running them here is not
    # decoration: it is what makes DEMO-05 and DEMO-07 show up in the
    # workbook as flagged cells rather than as quietly wrong numbers.
    n = flag_discharge_is_followup(rows)
    n += flag_implausible_values(rows)

    problems = verify_duration_bounds(rows)
    if problems:
        print("bounds disagree with their durations -- this is a bug:")
        for p in problems:
            print("   ", p)
        return 1

    out = HERE / "SAMPLE_case_log.xlsx"
    write_case_log(rows, str(out), sheet_title="Case Log")
    print(f"wrote {out.name}: {len(rows)} invented patients, {n} cell(s) flagged")
    print("every duration agrees with its start/stop bounds")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
