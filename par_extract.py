"""Parallel extraction driver (2026-10-25). Extracts every packet in
--pdf-dir that has no raw JSON yet, N at a time, then exits; run
run_mission.py afterwards to build the workbook (it reuses the JSONs).
Same per-packet code path as run_mission.extract_missing()."""
import argparse, json, sys, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import run_mission as rm
ap = argparse.ArgumentParser()
ap.add_argument("--mission", required=True); ap.add_argument("--pdf-dir", required=True)
ap.add_argument("--workers", type=int, default=6); ap.add_argument("--only", nargs="*")
a = ap.parse_args()
mission = rm.load_mission_profile(a.mission)
pdfs = rm.mission_pdfs(a.pdf_dir, mission["mrn_prefix"])
if a.only:
    pdfs = [p for p in pdfs if p.stem in a.only]
todo = [p for p in pdfs if not rm.json_path_for(p.stem).exists()]
print(f"{len(todo)} to extract of {len(pdfs)}", flush=True)
import extract
extract.ensure_api_key()
rm.RAW_JSON_DIR.mkdir(exist_ok=True)
def one(pdf):
    t = time.time()
    raw = extract.process_packet(str(pdf), pdf.stem, mission)
    rm.json_path_for(pdf.stem).write_text(json.dumps(raw, indent=2, ensure_ascii=False), encoding="utf-8")
    return pdf.stem, time.time() - t
fails = []
with ThreadPoolExecutor(a.workers) as ex:
    futs = {ex.submit(one, p): p for p in todo}
    for i, f in enumerate(as_completed(futs), 1):
        try:
            pid, dt = f.result()
            print(f"[{i}/{len(todo)}] {pid} done in {dt:.0f}s  [running ${extract.usage_cost():.2f}]", flush=True)
        except Exception as e:  # noqa: BLE001
            fails.append((futs[f].stem, repr(e)[:300])); print(f"[{i}/{len(todo)}] {futs[f].stem} FAILED {e!r}"[:400], flush=True)
print(extract.usage_report(packets=len(todo) - len(fails)))
print("FAILED:", fails)
