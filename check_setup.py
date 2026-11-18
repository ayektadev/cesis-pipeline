"""
check_setup.py

Run this FIRST, before any mission:

    python3 check_setup.py

It confirms the machine can actually run the pipeline, and it does so
without making a single API call, so it costs nothing and can be run as
many times as you like.

Why this exists. The pipeline's real failures on a new machine are boring
and all the same shape: poppler isn't installed, so pdf2image raises three
minutes into a run; tesseract isn't on PATH, so every page classifies as
"Unknown" and the API gets asked to read pages nobody selected; the key in
api_key.txt is a placeholder somebody forgot to replace. Every one of those
is cheap to detect up front and expensive to discover halfway through a
sixty-packet run that has already been billed.

So the order of checks below is deliberate: cheapest and most likely to be
wrong first, and nothing here touches the network.

Exit code is 0 if the machine is ready and 1 if it is not, so this can also
be dropped into a script.
"""

import importlib
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

# Terminal colour, but only when the output is going to a terminal. Piped
# into a file or a supervisor's email, escape codes are just noise.
_TTY = sys.stdout.isatty()
_OK = "\033[32mok\033[0m  " if _TTY else "ok   "
_BAD = "\033[31mFAIL\033[0m" if _TTY else "FAIL"
_WARN = "\033[33mwarn\033[0m" if _TTY else "warn"

failures: list[str] = []
warnings: list[str] = []


def report(passed, label, detail="", fix="", fatal=True):
    """One line per check. A failed check always prints how to fix it.

    An error message that says what is wrong but not what to do about it
    just relocates the problem to whoever the user forwards it to.
    """
    if passed:
        print(f"  {_OK} {label}" + (f" ({detail})" if detail else ""))
        return
    mark = _BAD if fatal else _WARN
    print(f"  {mark} {label}" + (f" -- {detail}" if detail else ""))
    if fix:
        for line in fix.strip().splitlines():
            print(f"         {line}")
    (failures if fatal else warnings).append(label)


print("\nISHI digitization pipeline -- setup check")
print("no API calls are made, so this is free to run\n")

# --------------------------------------------------------------------------
print("Python")
ver = sys.version_info
report(ver >= (3, 10), "Python 3.10 or newer",
       f"found {ver.major}.{ver.minor}.{ver.micro}",
       "Install a newer Python from python.org, or on a Mac: brew install python")

# --------------------------------------------------------------------------
print("\nPython packages")
for mod, why in [("anthropic", "talks to the model"),
                 ("pdf2image", "turns packet PDFs into page images"),
                 ("pytesseract", "free local OCR used to classify pages"),
                 ("PIL", "image handling"),
                 ("openpyxl", "writes the Excel case log")]:
    try:
        importlib.import_module(mod)
        report(True, mod, why)
    except ImportError:
        report(False, mod, f"not installed -- {why}",
               "cd into this folder and run:  pip3 install -r requirements.txt")

# --------------------------------------------------------------------------
print("\nCommand-line helpers")
# These two are the ones people miss, because pip cannot install them --
# they are system programs that the Python packages merely wrap.
for exe, pkg, why in [("pdftoppm", "poppler", "pdf2image needs it to open PDFs"),
                      ("tesseract", "tesseract", "pytesseract needs it to read text")]:
    found = shutil.which(exe)
    report(bool(found), exe, found or f"not on PATH -- {why}",
           f"Mac:    brew install {pkg}\n"
           f"Linux:  sudo apt install {pkg}")

# --------------------------------------------------------------------------
print("\nAPI key")
key_file = HERE / "api_key.txt"
if not key_file.exists():
    report(False, "api_key.txt exists", "file is missing",
           "Create a file called api_key.txt in this folder and paste the key into it.")
else:
    key = key_file.read_text().strip()
    # Deliberately never printed in full. A key echoed to a terminal ends up
    # in scrollback, in screenshots, and in pasted error reports.
    looks_real = key.startswith("sk-ant") and len(key) > 40
    report(looks_real, "api_key.txt holds a key",
           f"{key[:10]}...{key[-4:]}" if looks_real else "placeholder or empty",
           "Open api_key.txt, delete what is there, paste the key (starts with sk-ant), save.")

# --------------------------------------------------------------------------
print("\nPipeline files")
for name in ["extract.py", "reconcile.py", "run_mission.py", "schema.py",
             "xlsx_writer.py", "RULES.md", "GLOSSARY.md"]:
    report((HERE / name).exists(), name,
           "" if (HERE / name).exists() else "missing from this folder",
           "Re-unzip the package; a file did not survive the copy.")

profiles = sorted((HERE / "missions").glob("*.json")) if (HERE / "missions").is_dir() else []
report(bool(profiles), "mission profiles",
       ", ".join(p.stem for p in profiles) or "missions/ is empty or missing",
       "Re-unzip the package.")

# --------------------------------------------------------------------------
# Only worth attempting once the imports above have passed; importing the
# pipeline with openpyxl missing would raise here and bury the real cause.
print("\nPipeline self-test")
if failures:
    print("  skipped -- fix the failures above first")
else:
    try:
        sys.path.insert(0, str(HERE))
        import schema  # noqa: E402

        n_cols = len(schema.CASE_LOG_COLUMNS)
        report(n_cols > 0, "schema loads", f"{n_cols} columns")
        undefined = [c for c in schema.CASE_LOG_COLUMNS
                     if not schema.COLUMN_DEFINITIONS.get(c)]
        # Rule 21: a column with no definition is a column nobody can check.
        report(not undefined, "every column defines itself",
               "" if not undefined else f"missing: {', '.join(undefined)}",
               "This is a code bug, not a setup problem -- report it.")

        res = subprocess.run([sys.executable, str(HERE / "tests" / "test_reconcile.py")],
                             capture_output=True, text=True, timeout=120)
        passed = res.returncode == 0 and "all checks passed" in res.stdout
        report(passed, "offline reconciliation tests",
               "all checks passed" if passed else "see output below",
               "This is a code bug, not a setup problem -- report it.")
        if not passed:
            print((res.stdout or "")[-2000:])
            print((res.stderr or "")[-2000:])
    except Exception as exc:  # noqa: BLE001
        report(False, "pipeline self-test", f"{type(exc).__name__}: {exc}",
               "This is a code bug, not a setup problem -- report it.")

# --------------------------------------------------------------------------
print()
if failures:
    print(f"NOT READY -- {len(failures)} problem(s): {', '.join(failures)}")
    print("Fix the lines marked FAIL above, then run this again.")
    sys.exit(1)

if warnings:
    print(f"Ready, with {len(warnings)} warning(s): {', '.join(warnings)}")
else:
    print("Ready. Next step: the 5-packet vet run, described in docs/START_HERE.md")
print("  python3 run_mission.py --mission missions/<name>.json "
      "--pdf-dir \"<your PDF folder>\" --vet 5\n")
sys.exit(0)
