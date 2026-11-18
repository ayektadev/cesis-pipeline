# Concordance analysis

`concordance_build.py` is the analysis code for the paper. It compares the pipeline's case logs with the manually compiled ISHI case logs on 15 variables for 7 Sierra Leone and 11 Ghana trips.

It does the following:
- links pipeline records to hand-log rows by chart number, plus a secondary linkage as a sensitivity analysis
- standardizes each variable with the same coding rules for both sources
- scores agreement
- writes the results workbook, the cleaned 15-variable dataset and a pair-level audit file

```bash
pip install pandas openpyxl scipy statsmodels
python3 concordance_build.py <hand_log_dir> <pipeline_output_dir> <out_dir>
```

The script needs the hand logs and pipeline outputs, which contain patient information and are **not** included. They are held by ISHI under data use agreements with the partner hospitals. Requests for de-identified data go to the corresponding author and require approval from the host hospitals.

The outputs it writes include:
- **Data dictionary:** the definition, coding, comparability rule and agreement rule for every variable (Supplementary Table S2 in the paper).
- **Trend tests:** the mission-year, run-date and every-trip-variable models (Table 3 and Supplementary Table S7).
- **Complications:** McNemar tests (Table 4).
