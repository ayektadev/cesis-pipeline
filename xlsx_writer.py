"""
xlsx_writer.py

Writes CaseLogRow objects to an .xlsx file as a proper Excel Table (so future
runs can append rows without disturbing anything below it), highlights any
field that needs human review with a yellow fill + a cell comment, and adds
a Summary sheet built entirely from formulas referencing the Case Log table
-- so it recalculates automatically as more patients are added, rather than
needing to be regenerated from Python each time.
"""

from datetime import date, datetime, time

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment
from openpyxl.comments import Comment
from openpyxl.worksheet.table import Table, TableStyleInfo
from openpyxl.utils import get_column_letter

from schema import (CASE_LOG_COLUMNS, CASE_LOG_HEADERS, COLUMN_DEFINITIONS,
                    PROCEDURE_CATEGORIES, COLUMN_GROUPS)

FLAG_FILL = PatternFill(start_color="FFF2CC", end_color="FFF2CC", fill_type="solid")  # soft yellow
ABSENT_FILL = PatternFill(start_color="F2F2F2", end_color="F2F2F2", fill_type="solid")  # light gray
HEADER_FILL = PatternFill(start_color="4472C4", end_color="4472C4", fill_type="solid")
SUMMARY_HEADER_FILL = PatternFill(start_color="70AD47", end_color="70AD47", fill_type="solid")
HEADER_FONT = Font(name="Arial", bold=True, color="FFFFFF")
# One header colour per visual block (schema.COLUMN_GROUPS) so a survey or
# social-history section reads as one unit inside the patient row.
GROUP_HEADER_FILLS = {
    "Social history": PatternFill(start_color="7F6000", end_color="7F6000", fill_type="solid"),
    "EuraHS-QoL": PatternFill(start_color="7030A0", end_color="7030A0", fill_type="solid"),
}
BODY_FONT = Font(name="Arial")
BOLD_FONT = Font(name="Arial", bold=True)
TABLE_NAME = "CaseLog"


def _excel_safe(value):
    """Coerce an extracted value into something a cell can actually hold.

    Excel has no cell type for a list. openpyxl raises rather than guessing,
    which is right -- but the raise came at the very end of a 66-packet run,
    from one field on four patients (postop_visit_findings, which the model
    sometimes returns as ["CDI"] instead of "CDI"). Losing the whole workbook
    over that is not a good trade.

    The coercion happens HERE, at the write boundary, and deliberately not in
    reconcile.py. reconcile's job is to say what the chart said; a list of
    two findings is a truthful answer and the JSON should keep it as a list.
    It is only Excel that cannot store it, so it is only on the way into
    Excel that it should be flattened.

    Joining rather than taking the first element matters: a patient with two
    documented findings has two, and silently keeping one would be a wrong
    answer rather than an awkward one. Anything unrecognised falls through to
    str() so a novel shape degrades to a readable cell instead of a crash --
    the value is still visible and still checkable against the paper.
    """
    if value is None or isinstance(value, (str, int, float, bool, datetime, date, time)):
        return value
    if isinstance(value, (list, tuple, set)):
        # Recurse: a list of dicts is rare but not impossible, and the
        # elements deserve the same treatment the top-level value gets.
        parts = [str(_excel_safe(v)) for v in value if v is not None and v != ""]
        return "; ".join(parts) if parts else None
    if isinstance(value, dict):
        parts = [f"{k}: {_excel_safe(v)}" for k, v in value.items() if v is not None and v != ""]
        return "; ".join(parts) if parts else None
    return str(value)


def write_case_log(rows, out_path: str, sheet_title: str = "Case Log"):
    wb = Workbook()
    ws = wb.active
    ws.title = sheet_title

    for col_idx, col_name in enumerate(CASE_LOG_COLUMNS, start=1):
        cell = ws.cell(row=1, column=col_idx, value=CASE_LOG_HEADERS[col_name])
        cell.font = HEADER_FONT
        cell.fill = GROUP_HEADER_FILLS.get(COLUMN_GROUPS.get(col_name), HEADER_FILL)
        cell.alignment = Alignment(wrap_text=True, vertical="center")
        # Every header carries its own definition as a hover note, so the
        # workbook explains itself to someone who has only the workbook.
        # KeyError is deliberately NOT caught: a column without a definition
        # should stop the build, not ship silently (RULES.md rule 21).
        definition = COLUMN_DEFINITIONS[col_name]
        # Sized generously -- openpyxl's default note box is small enough to
        # clip these, and a definition you have to drag a box open to finish
        # reading is barely better than no definition.
        note = Comment(definition, "ISHI extraction pipeline")
        note.width, note.height = 460, 30 + 13 * (len(definition) // 58 + 1)
        cell.comment = note

    flagged_count = 0
    total_fields = 0
    for r_idx, row in enumerate(rows, start=2):
        # "Fields Flagged (count)" is a meta-count over this row's OTHER
        # fields (how many needed review) -- it's not itself an extracted
        # FieldValue, so it's computed here directly rather than looked
        # up from row.fields.
        row_flag_count = len(row.flagged_fields())

        for c_idx, col_name in enumerate(CASE_LOG_COLUMNS, start=1):
            if col_name == "fields_flagged_count":
                cell = ws.cell(row=r_idx, column=c_idx, value=row_flag_count)
                cell.font = BODY_FONT
                continue

            fv = row.fields.get(col_name)
            total_fields += 1
            value = _excel_safe(fv.value if fv else None)
            cell = ws.cell(row=r_idx, column=c_idx, value=value)
            cell.font = BODY_FONT
            if fv and fv.needs_review:
                flagged_count += 1
                cell.fill = FLAG_FILL
                comment_text = f"Confidence: {fv.confidence}."
                if fv.flag_reason:
                    comment_text += f" {fv.flag_reason}"
                if fv.source_pages:
                    comment_text += f" [source: {', '.join(fv.source_pages)}]"
                if value is None:
                    comment_text = "(absent) " + comment_text
                cell.comment = Comment(comment_text, "ISHI extraction pipeline")
            elif value is None:
                # Confidently absent, not merely unreviewed -- e.g. no
                # secondary procedure, or a hernia-only field on a
                # non-hernia case. These don't go through the needs_review
                # branch above because the pipeline isn't unsure about
                # them: it looked and there was genuinely nothing there.
                # The cell itself has to stay a real, empty Excel blank --
                # the Summary sheet's COUNTIF("<>") / AVERAGE formulas rely
                # on that -- but a silent blank cell is indistinguishable
                # from a field the pipeline never even looked at, and a
                # reviewer needs to be able to tell those apart. A comment
                # carries that distinction without touching the value
                # Excel computes over.
                comment_text = "(absent) -- not documented on the source form."
                if fv and fv.source_pages:
                    comment_text += f" [checked: {', '.join(fv.source_pages)}]"
                cell.fill = ABSENT_FILL
                cell.comment = Comment(comment_text, "ISHI extraction pipeline")

    for c_idx, col_name in enumerate(CASE_LOG_COLUMNS, start=1):
        header_len = len(CASE_LOG_HEADERS[col_name])
        ws.column_dimensions[get_column_letter(c_idx)].width = max(14, min(40, header_len + 4))

    last_col_letter = get_column_letter(len(CASE_LOG_COLUMNS))
    last_row = len(rows) + 1
    table_ref = f"A1:{last_col_letter}{last_row}"
    table = Table(displayName=TABLE_NAME, ref=table_ref)
    table.tableStyleInfo = TableStyleInfo(
        name="TableStyleMedium2", showRowStripes=True, showFirstColumn=False
    )
    ws.add_table(table)
    ws.freeze_panes = "A2"

    _write_summary_sheet(wb, n_rows=len(rows))

    wb.save(out_path)
    return {"rows_written": len(rows), "fields_flagged": flagged_count, "total_fields": total_fields}


def _col(col_name: str) -> str:
    """Structured-reference-safe column label, e.g. CaseLog[Age]."""
    return f"{TABLE_NAME}[{CASE_LOG_HEADERS[col_name]}]"


def _write_summary_sheet(wb, n_rows: int):
    ws = wb.create_sheet("Summary")
    ws.column_dimensions["A"].width = 34
    ws.column_dimensions["B"].width = 14
    ws.column_dimensions["C"].width = 14
    ws.column_dimensions["D"].width = 50

    def section(row, title):
        c = ws.cell(row=row, column=1, value=title)
        c.font = HEADER_FONT
        c.fill = SUMMARY_HEADER_FILL
        ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=4)
        return row + 1

    def stat(row, label, formula, note=""):
        ws.cell(row=row, column=1, value=label).font = BODY_FONT
        cell = ws.cell(row=row, column=2, value=formula)
        cell.font = BOLD_FONT
        if note:
            ws.cell(row=row, column=4, value=note).font = Font(name="Arial", italic=True, size=9)
        return row + 1

    r = 1
    ws.cell(row=r, column=1, value="ISHI Case Log -- Summary").font = Font(name="Arial", bold=True, size=14)
    r += 2

    r = section(r, "Overview")
    total_cases_row = r
    r = stat(r, "Total cases", f"=COUNTA({_col('mrn')})")
    r = stat(r, "Unique patients (packets)", f"=SUMPRODUCT(1/COUNTIF({_col('mrn')},{_col('mrn')}))",
             "differs from Total cases only if a packet ever contributes >1 row")
    r = stat(r, "Average age", f"=IFERROR(ROUND(AVERAGE({_col('age')}),1),\"N/A\")")
    r = stat(r, "Gender: Female", f"=COUNTIF({_col('gender')},\"F\")")
    r = stat(r, "Gender: Male", f"=COUNTIF({_col('gender')},\"M\")")
    r += 1

    r = section(r, "Procedure mix (count and % of total cases)")
    cat_start_row = r
    for cat in PROCEDURE_CATEGORIES:
        ws.cell(row=r, column=1, value=cat).font = BODY_FONT
        count_cell = ws.cell(row=r, column=2, value=f'=COUNTIF({_col("procedure_category")},"{cat}")')
        count_cell.font = BODY_FONT
        pct_cell = ws.cell(row=r, column=3,
                            value=f"=IFERROR(B{r}/B{total_cases_row},0)")
        pct_cell.font = BODY_FONT
        pct_cell.number_format = "0.0%"
        r += 1
    r += 1

    r = section(r, "Duration metrics (minutes, averaged across cases with a value)")
    r = stat(r, "Avg Surgery Time (operative duration)", f"=IFERROR(ROUND(AVERAGE({_col('surgery_time_mins')}),0),\"N/A\")",
             "first incision to closing -- see GLOSSARY.md")
    r = stat(r, "Avg OR Time", f"=IFERROR(ROUND(AVERAGE({_col('or_time_mins')}),0),\"N/A\")",
             "in-room to out-of-room")
    r = stat(r, "Avg Anesthesia Time", f"=IFERROR(ROUND(AVERAGE({_col('anesthesia_time_mins')}),0),\"N/A -- see note\")",
             "blank whenever Anesthesia End was not filled in; N/A means every case so far is missing it")
    r = stat(r, "Avg LOS (days)", f"=IFERROR(ROUND(AVERAGE({_col('los')}),1),\"N/A\")",
             "whole days, kept to match the original sheet -- LOS (hours) below is the one to analyse")
    r += 1

    r = section(r, "Hernia detail and severity (inguinal cases)")
    for grade in ("H1", "H2", "H3", "H4"):
        ws.cell(row=r, column=1, value=f"Hernia size {grade}").font = BODY_FONT
        ws.cell(row=r, column=2, value=f'=COUNTIF({_col("h_score")},"{grade}")').font = BODY_FONT
        r += 1
    r = stat(r, "Graded H1-H4 (any grade recorded)", f'=COUNTIF({_col("h_score")},"H*")',
             "cases below this line had no grade marked on the form -- they are NOT low-grade cases")
    r = stat(r, "F score recorded", f'=COUNTIF({_col("hernia_f_score")},"F*")')
    r = stat(r, "Right / Left / Bilateral",
             f'=COUNTIF({_col("hernia_laterality")},"R")&" / "&'
             f'COUNTIF({_col("hernia_laterality")},"L")&" / "&'
             f'COUNTIF({_col("hernia_laterality")},"Bilateral")')
    r = stat(r, "Recorded as recurrent", f'=COUNTIF({_col("hernia_recurrent")},"*recurrent*")',
             "a blank here means the form did not say, not that the hernia was primary")
    r = stat(r, "Mesh or plug/patch technique recorded",
             f'=COUNTIF({_col("repair_technique")},"*mesh*")+COUNTIF({_col("repair_technique")},"*plug*")')
    r += 1

    r = section(r, "Post-op course and outcomes")
    r = stat(r, "Avg LOS (hours)", f"=IFERROR(ROUND(AVERAGE({_col('los_hours')}),1),\"N/A\")",
             "OR in-room to recorded discharge -- see GLOSSARY.md")
    r = stat(r, "Avg PACU time (mins)", f"=IFERROR(ROUND(AVERAGE({_col('pacu_los_mins')}),0),\"N/A\")")
    r = stat(r, "Discharge date/time recorded", f'=COUNTIF({_col("discharge_datetime")},"<>")')
    r = stat(r, "Discharge criteria marked met", f'=COUNTIF({_col("discharge_criteria_met")},"Y")')
    r = stat(r, "Seen post-op (documented)", f'=COUNTIF({_col("seen_post_op")},"Y")',
             "counts only cases with an actual Progress Note or Post-Op Visit page -- "
             "a blank means no evidence either way, never an assumed yes")
    r = stat(r, "Post-op follow-up visit documented", f'=COUNTIF({_col("postop_visit_date")},"<>")')
    r = stat(r, "Avg POD of follow-up visit", f"=IFERROR(ROUND(AVERAGE({_col('postop_visit_pod')}),1),\"N/A\")")
    r = stat(r, "Post-op complication documented", f'=COUNTIF({_col("postop_complication")},"<>")',
             "the denominator is the follow-up count above, NOT total cases -- "
             "a case with no follow-up visit cannot show a complication here")
    r = stat(r, "Further surgery planned", f'=COUNTIF({_col("further_surgery_planned")},"<>")')
    r += 1

    r = section(r, "Data quality / review snapshot")
    r = stat(r, "Cases with >=1 flagged field", f'=COUNTIF({_col("fields_flagged_count")},">0")')
    r = stat(r, "Cases with zero flagged fields", f'=COUNTIF({_col("fields_flagged_count")},0)')
    r = stat(r, "Avg fields flagged per case", f"=IFERROR(ROUND(AVERAGE({_col('fields_flagged_count')}),1),\"N/A\")")
    r = stat(r, "Cases with any Complications/Notes", f'=COUNTIF({_col("complications_notes")},"<>")',
             "auto-aggregated notes -- always a judgment call, see RULES.md rule 5")
    r += 1

    ws.cell(row=r, column=1,
            value="This sheet is formulas, not hardcoded numbers -- it updates automatically the next "
                  "time main.py rewrites this file with more patients in the Case Log table.").font = \
        Font(name="Arial", italic=True, size=9)
