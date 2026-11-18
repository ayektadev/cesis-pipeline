# START HERE — How to digitize a mission's surgical packets

You have a folder of scanned patient packets (PDFs) from a surgical
mission trip. This tool reads the handwriting in them with AI and builds
one Excel case log. Your job: run three commands and double-check a
sample against the paper logs. No coding needed.

## Part 1 — One-time setup (Mac, ~15 minutes)

1. Unzip the pipeline zip somewhere easy, like your Desktop. You'll get
   a folder with files like `run_mission.py` in it.

2. Open the **Terminal** app (press Cmd+Space, type "terminal", Enter).
   The terminal is just a way to give the computer typed commands.

3. Install Homebrew (a free installer tool) if you don't have it. Paste
   this into Terminal and press Enter, then follow its prompts:
   /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

4. Install the two helper programs (reads text from images / opens PDFs):
   brew install tesseract poppler

5. Install the Python pieces. First go INTO the pipeline folder:
   cd ~/Desktop/YOUR-FOLDER-NAME
   (Tip: type `cd ` with a space, then drag the folder from Finder onto
   the Terminal window and press Enter.) Then run:
   pip3 install -r requirements.txt

6. The API key is already in the file `api_key.txt` in this folder, so
   there is nothing to paste in. Treat that file like a credit card
   number — never email it, screenshot it, or forward this folder to
   anyone outside the team. If you were told to use a different key,
   double-click `api_key.txt` (it opens in TextEdit), replace what is
   there with the new key, and save.

7. Check the setup worked. In Terminal, still inside the pipeline folder:
   python3 check_setup.py

   This makes no AI calls, so it is free and you can run it as often as
   you like. Every line should say "ok". If any line says FAIL, it also
   prints the command that fixes it — run that, then run the check
   again. Do not go further until this comes back clean.

## Part 2 — Before the first run (with your supervisor)

8. Put the mission's packet PDFs in one folder inside the pipeline
   folder, e.g. `Ghana PDFs`. Each file should be one patient, named by
   their ID (like `GH-01.pdf`).

9. That's it — you do NOT need to fill anything in about the mission
   (dates, staff names, etc.). The test run in Part 3 figures those out
   by itself from the first packets; your job is only to CONFIRM what it
   found against the paper.

## Part 3 — The 5-packet test run (the "vet")

10. In Terminal, inside the pipeline folder, run:
   python3 run_mission.py --mission missions/ghana.json --pdf-dir "Ghana PDFs" --vet 5

   This reads only the FIRST 5 patients (a few minutes) and then stops
   on purpose. It creates a file called `VET_CHECKLIST_GH.xlsx`, and it
   automatically learns the mission's date range and staff names from
   what it read.

11. Open that checklist in Excel. Two tabs to work through with the real
    paper logs next to you:
    - "Vet Checklist" tab: go row by row — does the "Extracted value"
      match the paper? Fill in the "Paper log says", "Match?" and
      "Correction" columns. Yellow rows = the computer itself wasn't
      sure, check those extra carefully. "(absent)" means the box on the
      form was empty or unreadable and the computer deliberately did NOT
      guess — confirm the paper really is blank there. In the full Case
      Log workbook, the cell itself stays truly blank (so totals/averages
      still work) but gets a light gray shade and an "(absent)" note you
      can see by hovering, so it never looks like a spot nobody checked.
    - "Calibration" tab: shows the mission dates and staff names the
      computer detected. Confirm each against the paper. Names marked
      "seen ONCE — possible misread" deserve a close look. If anything
      is wrong here, tell your supervisor before the full run.

12. Judging the result (the "How to vet" tab in the checklist explains
    this too):
    - Everything matches, or just one or two random wrong digits →
      you're good, go to Part 4.
    - The SAME kind of mistake shows up on several patients (e.g. every
      surgery start time is wrong the same way) → STOP. Don't run the
      rest. Show the checklist to your supervisor — something needs
      fixing first, and stopping here is exactly what this step is for.

## Part 4 — The full run

13. Run the same command WITHOUT the vet part:
    python3 run_mission.py --mission missions/ghana.json --pdf-dir "Ghana PDFs"

    This does every remaining patient in one go (roughly an hour for a
    full mission — leave the laptop open and plugged in). The 5 you
    already did are reused automatically. If it gets interrupted, just
    run the exact same command again — it picks up where it stopped.

14. Done! The result is `ISHI_Case_Log_Ghana.xlsx` in the pipeline
    folder. Open it once in Excel so the Summary tab calculates.

## Things to know

- Highlighted cells in the final log = the computer flagged them for a
  human to double-check against the scans. That's normal and expected.
- Blank cells with a note = the handwriting was unreadable and the rule
  is to leave it blank rather than guess. Never fill those in unless the
  paper log actually shows a value.
- Never edit numbers directly in the Excel and consider it "fixed" — the
  log gets rebuilt from the underlying data, so hand-edits get erased.
  Report corrections to your supervisor instead.
- These are real patients' medical records. Don't share the files, the
  key, or screenshots with anyone outside the team.
- Stuck? Copy the exact error message from Terminal and send it to your
  supervisor — the message usually says what's missing.
