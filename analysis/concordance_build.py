"""ISHI pipeline vs. historical hand log concordance — Sierra Leone and Ghana.
Reads hand logs and pipeline case logs, normalises 15 variables, matches by trip+MRN,
scores agreement, and writes de-identified Excel outputs. Run: python3 build.py <hand_dir> <pipeline_dir> <out_dir>
"""
import sys, re, glob, os, math, random, datetime as dt, warnings, json
import openpyxl
import pandas as pd
from scipy.stats import binomtest
warnings.filterwarnings("ignore")

HAND, PIPE, OUT = sys.argv[1], sys.argv[2], sys.argv[3]

# ---------- trips: hand file, sheets (main first), pipeline file, mrn prefix, run date ----------
TRIPS = [
 # site, trip, year, hand file, pipeline glob, mrn regex prefix
 ("Sierra Leone","SL13",2013,"SL13.xlsx","Legacy SL 2013/ISHI_Case_Log_Sierra_Leone_2013.xlsx"),
 ("Sierra Leone","SL17",2017,"SL17.xlsx","Legacy SL 2017/ISHI_Case_Log_Sierra_Leone_2017.xlsx"),
 ("Sierra Leone","SL18",2018,"SL18.xlsx","Legacy SL 2018/ISHI_Case_Log_Sierra_Leone_2018.xlsx"),
 ("Sierra Leone","SL19",2019,"SL19.xlsx","Legacy SL 2019/ISHI_Case_Log_Sierra_Leone_2019.xlsx"),
 ("Sierra Leone","SL22",2022,"SL22.xlsx","Legacy SL 2022/ISHI_Case_Log_Sierra_Leone_2022.xlsx"),
 ("Sierra Leone","SL23",2023,"SL23.xlsx","Legacy SL 2023/ISHI_Case_Log_Sierra_Leone_2023.xlsx"),
 ("Sierra Leone","SL24",2024,"SL24.xlsx","Legacy SL 2024/ISHI_Case_Log_Sierra_Leone_2024.xlsx"),
 ("Ghana","GH14",2014,"GH14.xlsx","Legacy GH 2014/ISHI_Case_Log_Ghana_2014.xlsx"),
 ("Ghana","GH15",2015,"GH15.xlsx","Legacy GH 2015/ISHI_Case_Log_Ghana_2015.xlsx"),
 ("Ghana","GH16",2016,"GH16.xlsx","Legacy GH 2016/ISHI_Case_Log_Ghana_2016.xlsx"),
 ("Ghana","GH17",2017,"GH17.xlsx","Legacy GH 2017/ISHI_Case_Log_Ghana_2017.xlsx"),
 ("Ghana","GH18",2018,"GH18.xlsx","Legacy GH 2018/ISHI_Case_Log_Ghana_2018.xlsx"),
 ("Ghana","GH19",2019,"GH19.xlsx","Legacy GH 2019/ISHI_Case_Log_Ghana_2019.xlsx"),
 ("Ghana","GH23",2023,"GH23.xlsx","Legacy GH 2023/ISHI_Case_Log_Ghana_2023.xlsx"),
 ("Ghana","GH24",2024,"GH24.xlsx","Legacy GH 2024/ISHI_Case_Log_Ghana_2024_September.xlsx"),
 ("Ghana","GH25M",2025,"GH25M.xlsx","Legacy GH MAY 2025/ISHI_Case_Log_Ghana_2025_May.xlsx"),
 ("Ghana","GH25S",2025,"GH25S.xlsx","Legacy GH SEPTEMBER 2025/ISHI_Case_Log_Ghana_2025_September.xlsx"),
 ("Ghana","GH26M",2026,"GH26M.xlsx","Legacy GH MAY 2026/ISHI_Case_Log_Ghana_2026.xlsx"),
]
# hand sheets that hold analysis tables, not patients
SKIP_SHEETS = {"Sheet2","Sheet3","Sheet4","SL22 OR Team"}

VARS = ["surgery_date","age","sex","preop_dx","postop_dx","procedure","secondary_procedure",
        "mesh","anesthesia_type","operative_minutes","los_days","complication","seen_post_op",
        "h_score","symptom_years"]
LABEL = {"surgery_date":"Date of surgery","age":"Age","sex":"Sex","preop_dx":"Pre-op diagnosis",
 "postop_dx":"Post-op diagnosis","procedure":"Procedure","secondary_procedure":"Secondary procedure",
 "mesh":"Mesh use","anesthesia_type":"Anesthesia type","operative_minutes":"Operative duration",
 "los_days":"Length of stay","complication":"Complication","seen_post_op":"Follow-up attendance",
 "h_score":"Hernia H score","symptom_years":"Symptom duration"}
RULE = {"surgery_date":"same calendar day","age":"within 1 year","sex":"exact",
 "preop_dx":"same condition and side","postop_dx":"same condition and side",
 "procedure":"same operation and side","secondary_procedure":"both none, or same operation",
 "mesh":"exact (Y/N)","anesthesia_type":"same category (spinal/general/local/sedation)",
 "operative_minutes":"within 5 minutes","los_days":"exact days","complication":"exact Y/N under the treated-complication definition (see Data dictionary); blank hand-log cell = none",
 "seen_post_op":"exact (Y/N)","h_score":"same H1-H4 grade","symptom_years":"within 1 year"}

# ---------- small parsers ----------
NEG = re.compile(r"^\s*(none|no|n|nil|0|0\.0|na|n/a|none noted|not noted|no complications?|negative|-|—|x|nothing)\s*\.?\s*$", re.I)
def blank(v): return v is None or (isinstance(v,float) and math.isnan(v)) or str(v).strip() in ("","None","nan","?","-","n/a","N/A","NA","na")
def s(v): return "" if blank(v) else str(v).strip()

def to_date(v, year=None):
    if blank(v): return None
    if isinstance(v,(dt.datetime,dt.date)): d = v if isinstance(v,dt.date) and not isinstance(v,dt.datetime) else v.date()
    elif isinstance(v,(int,float)) and 30000 < v < 60000: d = (dt.datetime(1899,12,30)+dt.timedelta(days=float(v))).date()
    else:
        t=str(v).strip()
        m=re.match(r"(\d{4})-(\d{1,2})-(\d{1,2})",t)
        if m:
            try: d=dt.date(int(m[1]),int(m[2]),int(m[3]))
            except ValueError: return None
        else:
            m=re.match(r"(\d{1,2})/(\d{1,2})/(\d{2,4})",t)
            if not m: return None
            y=int(m[3]); y = y+2000 if y<100 else y
            try: d=dt.date(y,int(m[1]),int(m[2]))
            except ValueError: return None
    if year and d.year!=year:   # hand logs with wrong year typed (e.g. 2024 for 2014): keep month/day
        try: d=d.replace(year=year)
        except ValueError: return None
    return d

def to_num(v):
    if blank(v): return None
    if isinstance(v,(int,float)): return float(v)
    m=re.search(r"\d+(\.\d+)?",str(v)); return float(m.group()) if m else None

def to_age(v):
    if blank(v): return None
    if isinstance(v,(int,float)): return float(v)
    t=str(v)
    if re.search(r"\d+\s*-\s*\d+",t): return None          # "50-60" is a range, not an age
    if re.search(r"mo|month",t,re.I):
        n=to_num(t); return None if n is None else round(n/12,1)
    return to_num(t)

def to_years(v):
    if blank(v): return None
    if isinstance(v,(int,float)): return float(v) if v<120 else None
    t=str(v).lower()
    n=to_num(t)
    if n is None: return None
    if "month" in t or re.search(r"\bmo\b",t): return round(n/12,2)
    if "week" in t or "wk" in t: return round(n/52,2)
    if "day" in t: return round(n/365,2)
    return n

def to_minutes(v):
    if blank(v): return None
    if isinstance(v,(int,float)): return float(v) if 0<v<1000 else None
    t=str(v)
    if re.search(r"\d+\s*-\s*\d+[^0-9]*/",t) or "/" in t: return None   # "1- 90/ 2- 60": multiple cases
    h=re.search(r"(\d+(\.\d+)?)\s*h",t,re.I); m=re.search(r"(\d+)\s*m",t,re.I)
    if h: return float(h[1])*60+(float(m[1]) if m else 0)
    return to_num(t)

def to_sex(v):
    t=s(v).upper()
    if not t: return None
    if t.startswith("M"): return "M"
    if t.startswith("F"): return "F"
    m=re.search(r"\d+\s*([MF])",t)
    return m[1] if m else None

def to_yn(v):
    t=s(v).lower()
    if not t: return None
    if t in ("y","yes","1","1.0","true") or t.startswith("y"): return "Y"
    if t in ("n","no","0","0.0","false") or t.startswith("n"): return "N"
    return None

def to_h(v):
    t=s(v).upper().replace(" ","")
    if not t: return None
    m=re.search(r"H?([1-4])",t)
    return f"H{m[1]}" if m else None

def anes_cat(v):
    t=s(v).lower()
    if not t: return None
    if re.search(r"\bga\b|general|geta|ett|lma|intubat",t): return "general"
    if re.search(r"spinal|epidural|regional|saddle|neuraxial|caudal|\bsab\b",t): return "spinal"
    if re.search(r"sedat|\bmac\b|ketamine|iv sed|local|block|lidocaine|infiltrat",t): return "local/sedation"
    return "other"

# --- conditions and operations: category + side ---
CATS = [
 ("femoral hernia", r"femoral|\bfh\b|\bfhr\b"),
 ("inguinal hernia", r"inguin|\bing\.?\b|\bith\b|\bb?[rl]?\s?ih\b|\bb?[rl]?\s?ihr\b|ingino|inguino|scrotal hernia|groin hernia|groin|herniorr|\bbih|\bbihr"),
 ("umbilical hernia", r"umbil|\buh\b|\buhr\b|para-?umbil|\bpuh"),
 ("epigastric hernia", r"epigast|\beh\b|\behr\b"),
 ("ventral/incisional hernia", r"ventral|incisional|\bvh\b|\bvhr\b|abdominal wall hernia|spigel"),
 ("hydrocele", r"hydro|\bhyd\b|hydrocelectomy|jaboulay"),
 ("uterine/fibroid", r"fibroid|\buf\b|uter|hyster|\btah\b|\bsch\b|myom|leiomyo"),
 ("ovarian/adnexal", r"ovar|adnex|salping|oophor|tubal"),
 ("prostate/bladder", r"prostat|\bbph\b|turp|cystolith|bladder stone|urolith|supra-?pubic|\bspt\b|urethr"),
 ("burn/contracture", r"burn|contractur|skin graft|\bftsg\b|\bstsg\b"),
 ("thyroid", r"thyro|goit"),
 ("breast", r"breast|mastect|fibroaden"),
 ("soft-tissue mass", r"\bme\b|lipoma|mass|cyst|lump|nodule|keloid|skin tag|sebaceous|ganglion|excis|polyp|wart|tumou?r|neuroma|abscess"),
 ("undescended testis", r"undescend|orchidopexy|orchiopexy|cryptorch"),
 ("other", r".")]
def categories(text):
    t=s(text).lower()
    if not t: return None
    hit=[name for name,rx in CATS[:-1] if re.search(rx,t)]
    # femoral overrides inguinal only when "femoral" appears and inguinal does not
    if "femoral hernia" in hit and "inguinal hernia" in hit and not re.search(r"inguin",t): hit.remove("inguinal hernia")
    return frozenset(hit or ["other"])
def side(text):
    t=" "+s(text).lower()+" "
    if re.search(r"bilat|\bb/l\b|\bbil\b|\bbih|\bbihr|both|\bb\s?ih",t): return "B"
    r=bool(re.search(r"\bright\b|\brt\b|\br\s?ih|\br\s|\(r\)|\brihr?\b|\br\.",t))
    l=bool(re.search(r"\bleft\b|\blt\b|\bl\s?ih|\bl\s|\(l\)|\blihr?\b|\bl\.",t))
    if r and l: return "B"
    return "R" if r else ("L" if l else None)

def mesh_flag(*texts):
    t=" ".join(s(x).lower() for x in texts)
    if not t.strip(): return None
    if re.search(r"no mesh|without mesh|non-?mesh|tissue repair|bassini|shouldice|desarda|mcvay|marcy|herniotomy|high ligation|primary repair",t): return "N"
    if re.search(r"mesh|prolene|plug|lichtenstein|patch|tep|tapp",t): return "Y"
    return None

COMP_NONEVENT = re.compile(r"error in d|unnecessary|wrong d|dx change|diagnosis change",re.I)
def comp_hand(*vals):
    """Hand log: Y if any complication field describes an event; N if explicitly none OR blank."""
    any_val=False
    for v in vals:
        t=s(v)
        if not t: continue
        any_val=True
        if NEG.match(t) or t.lower().startswith("none") or t.lower().startswith("no ") : continue
        if re.fullmatch(r"[0-9.]+",t):
            n=float(t)
            if n in (1,2): return "Y"     # coded 1=surgical, 2=anesthesia
            continue                      # 0 none; 3 = coded non-event in GH17 key
        if COMP_NONEVENT.search(t): continue
        if t.upper() in ("Y","YES"): return "Y"
        if t.upper() in ("N",): continue
        return "Y"
    return "N"
# complication terms. ANY = any documented adverse event (how most hand logs record it);
# SURG = surgical complications only (sensitivity analysis).
SURG_TERMS = r"hematoma|haematoma|seroma|infect|\bssi\b|abscess|purulent|pus\b|dehisc|wound (?:breakdown|discharge|drainage|separation)|bleed|hemorrhag|haemorrhag|take ?back|return(?:ed)? to (?:the )?or|re-?operat|re-?explor|recurren|crepit|transfus|death|died|expired|bloody drainage"
ANY_TERMS = SURG_TERMS + r"|retention|could ?n.?t void|unable to void|not voiding|straight cath|foley (?:placed|inserted|re-?inserted)|catheter placed|spinal headache|post-?dural|headache|nausea|vomit|emesis|ponv|desaturat|hypox|pneumon|\bpna\b|\buti\b|fever|febrile|ileus|numb|unable to move (?:leg|lower)|scrotal swelling|swelling|edema|oedema|syncop|hypotens"
NEG_CTX = re.compile(r"\b(no|not|denies|without|negative for|none|nil|resolved|absence of)\b[^|.;,]{0,25}$", re.I)
def _hits(text, terms):
    t=s(text)
    if not t: return False
    for seg in re.split(r"\s\|\s", t):
        body=seg.split(": ",1)[-1]
        if re.match(r"^\s*(none|no complications?|nil|0|minimal)\b", body, re.I) and not re.search(terms, body[12:], re.I):
            continue
        for m in re.finditer(terms, body, re.I):
            if NEG_CTX.search(body[:m.start()]): continue
            if re.search(r"chronic|pre-?op|preexist|pre-existing|at baseline", body[max(0,m.start()-40):m.end()+40], re.I): continue
            return True
    return False
TREATED_TERMS = SURG_TERMS + r"|retention|could ?n.?t void|unable to void|not voiding|straight cath|foley (?:placed|inserted|re-?inserted)|catheter placed|spinal headache|post-?dural|blood patch|pneumon|\bpna\b|\buti\b|ileus|desaturat|hypox"
def comp_pipe(postop, notes=None, visit=None, terms=None):
    terms = terms or ANY_TERMS
    t=s(postop)
    if t and not NEG.match(t) and not re.match(r"^(none|no\b|nil|not)",t,re.I): return "Y"
    return "Y" if (_hits(notes,terms) or _hits(visit,terms)) else "N"
def comp_hand_terms(vals, terms, codes):
    """Hand log Y if a numeric code in `codes` or text matching `terms` (not negated) is present; blank = N."""
    for v in vals:
        t=s(v)
        if not t: continue
        if re.fullmatch(r"[0-9., ]+",t):
            if any(re.search(rf"(?<![0-9]){c}(?:\.0)?(?![0-9])",t) for c in codes): return "Y"
            continue
        if re.search(terms,t,re.I) and not NEG_CTX.search(t[:5]): return "Y"
    return "N"
def comp_hand_strict(*vals):
    for v in vals:
        t=s(v)
        if t and re.fullmatch(r"[0-9., ]+",t) and re.search(r"\b1\b|1\.0",t): return "Y"
        if t and re.search(SURG_TERMS,t,re.I) and not NEG_CTX.search(t[:5]): return "Y"
    return "N"

# ---------- MRN normalisation ----------
def mrn_key(v, trip):
    """Return trip-local patient number as a string like '12' or '20A'."""
    if blank(v): return None
    if isinstance(v,(int,float)): return str(int(v))
    t=str(v).strip().upper().replace(" ","")
    yy=re.sub(r"\D","",trip)          # '13','17','25'...
    m=re.match(r"^(SL|GH)-?(\d{2})(\d{2})$",t)              # gh-1401
    if m and m[2]==yy: return str(int(m[3]))
    m=re.match(r"^(SL|GH)(\d{1,2})[MS]?-?0*(\d+)([A-Z]?)$",t) # GH15-04, SL3-001, GH25S-94, GH26M-1
    if m: return str(int(m[3]))+m[4]
    m=re.match(r"^0*(\d+)([A-Z]?)$",t)
    if m: return str(int(m[1]))+m[2]
    return None

# ---------- read hand log ----------
HMAP = {
 "mrn":[r"^mrn$"],
 "date":[r"^date$",r"^mission date$",r"^date of surgery"],
 "age":[r"^age$",r"^age/gender$"],
 "sex":[r"^sex$",r"^gender$",r"^age/gender$"],
 "preop":[r"^diagnosis$",r"^pre-?op(erative)? diagnosis",r"^pre-op diagnosis"],
 "postop":[r"^post-?op(erative)? diagnosis",r"^findings$"],
 "proc":[r"^procedure$"],
 "sec":[r"^secondary procedure"],
 "dur":[r"^procedure duration"],
 "comp":[r"^complications?\b",r"^complication$",r"^description of complication"],
 "anes":[r"^anesthesia type$",r"^anesthesia$",r"^anest\.?$"],
 "los":[r"^los",r"^length of stay"],
 "fu":[r"^seen for f/?u",r"^seen post op",r"^post-op follow-up"],
 "h":[r"^h-? ?score",r"^h score",r"^classification$",r"^hernia classification"],
 "sx":[r"duration \(years\)",r"^duration of",r"^sx duration",r"^symptom duration"],
}
def header_row(rows):
    for i,r in enumerate(rows[:6]):
        if r and any(isinstance(c,str) and c.strip().upper()=="MRN" for c in r): return i
    return None
def colmap(hdr):
    out={}
    for k,pats in HMAP.items():
        for p in pats:
            idx=[i for i,c in enumerate(hdr) if isinstance(c,str) and re.search(p,c.strip().lower())]
            if idx:
                out.setdefault(k,[]); out[k]+= [i for i in idx if i not in out[k]]
                if k not in ("comp",): break
    return out
HAND_FIELDS={}; HAND_HEADERS={}
def read_hand(path, trip, year):
    wb=openpyxl.load_workbook(path,read_only=True,data_only=True)
    recs={}; order=[]; HAND_FIELDS[trip]=set(); HAND_HEADERS[trip]={}; sheet_no=0; nomrn=0
    for ws in wb.worksheets:
        if ws.title in SKIP_SHEETS: continue
        rows=list(ws.iter_rows(values_only=True)); hi=header_row(rows)
        if hi is None: continue
        cm=colmap(rows[hi]); HAND_FIELDS[trip]|=set(cm); sheet_no+=1
        for k_,ix_ in cm.items():
            HAND_HEADERS[trip].setdefault(k_,[])
            for i in ix_:
                h_=f"{ws.title}: {str(rows[hi][i]).strip()}"
                if h_ not in HAND_HEADERS[trip][k_]: HAND_HEADERS[trip][k_].append(h_)
        for r in rows[hi+1:]:
            r=list(r)+[None]*40
            g=lambda k,j=0: r[cm[k][j]] if k in cm and len(cm[k])>j else None
            key=mrn_key(g("mrn"),trip)
            if not key:
                # rows without a chart number: keep from the main sheet only, if they describe a patient
                if sheet_no>1 or not (s(g("proc")) and (s(g("age")) or s(g("sex")))): continue
                nomrn+=1; key=f"nomrn-{nomrn}"
            ag=g("age"); 
            rec={"surgery_date":to_date(g("date"),year),
                 "age":to_age(ag),"sex":to_sex(g("sex")) or (to_sex(ag) if isinstance(ag,str) else None),
                 "preop_raw":s(g("preop")),"postop_raw":s(g("postop")),"proc_raw":s(g("proc")),"sec_raw":s(g("sec")),
                 "operative_minutes":to_minutes(g("dur")),
                 "anes_raw":s(g("anes")),"los_days":to_num(g("los")),
                 "seen_post_op":to_yn(g("fu")),"h_score":to_h(g("h")),
                 "symptom_years":to_years(g("sx")),
                 "comp_vals":[r[i] for i in cm.get("comp",[])]}
            if key in recs:      # second sheet (hernia subset): fill only gaps
                for k,v in rec.items():
                    if k=="comp_vals": recs[key][k]+=v
                    elif recs[key].get(k) in (None,"") and v not in (None,""): recs[key][k]=v
            else:
                recs[key]=rec; order.append(key)
    out=[]
    for k in order:
        r=recs[k]
        post = r["preop_raw"] if re.match(r"^\s*same\b",r["postop_raw"],re.I) else r["postop_raw"]
        out.append({"key":k,"surgery_date":r["surgery_date"],"age":r["age"],"sex":r["sex"],
            "preop_dx":r["preop_raw"] or None,"postop_dx":post or None,"procedure":r["proc_raw"] or None,
            "secondary_procedure":r["sec_raw"] or None,"mesh":mesh_flag(r["proc_raw"],r["sec_raw"]),
            "anesthesia_type":anes_cat(r["anes_raw"]),"operative_minutes":r["operative_minutes"],
            "los_days":r["los_days"],"complication":comp_hand_terms(r["comp_vals"],TREATED_TERMS,("1","2")),
            "complication_surgical":comp_hand_terms(r["comp_vals"],SURG_TERMS,("1",)),
            "complication_any":comp_hand(*r["comp_vals"]),
            "seen_post_op":r["seen_post_op"],"h_score":r["h_score"],"symptom_years":r["symptom_years"]})
    return out

# ---------- read pipeline log ----------
def pipe_comp(r,g,terms):
    pc=s(g(r,"Post-Op Complication"))
    if pc and re.search(terms,pc,re.I) and not NEG_CTX.search(pc[:5]): return "Y"
    return "Y" if (_hits(g(r,"Complications/Notes"),terms) or _hits(g(r,"Post-Op Visit Findings"),terms)) else "N"
def read_pipe(path, trip, year):
    ws=openpyxl.load_workbook(path,read_only=True,data_only=True)["Case Log"]
    rows=list(ws.iter_rows(values_only=True)); h=list(rows[0]); ix={c:i for i,c in enumerate(h)}
    g=lambda r,c: r[ix[c]] if c in ix else None
    out=[]
    for r in rows[1:]:
        key=mrn_key(g(r,"MRN"),trip)
        if not key: continue
        post=s(g(r,"Postop Dx")); pre=s(g(r,"Preop DX"))
        if re.match(r"^\s*same\b",post,re.I): post=pre
        proc=s(g(r,"Procedure")); sec=s(g(r,"Secondary Procedure"))
        out.append({"key":key,"surgery_date":to_date(g(r,"Date of surgery")),
            "age":to_age(g(r,"Age")),"sex":to_sex(g(r,"Gender")),
            "preop_dx":pre or None,"postop_dx":post or None,"procedure":proc or None,"secondary_procedure":sec or None,
            "mesh":mesh_flag(proc,sec,g(r,"Repair Technique")),
            "anesthesia_type":anes_cat(g(r,"Anesthesia Type")),
            "operative_minutes":to_minutes(g(r,"operative time (mins)")),
            "los_days":to_num(g(r,"LOS")),
            "complication":pipe_comp(r,g,TREATED_TERMS),
            "complication_surgical":pipe_comp(r,g,SURG_TERMS),
            "complication_any":comp_pipe(g(r,"Post-Op Complication"),g(r,"Complications/Notes"),g(r,"Post-Op Visit Findings")),
            "seen_post_op":to_yn(g(r,"Seen Post Op")) or "N",
            "h_score":to_h(g(r,"inguinal hernia score (H score)")),
            "symptom_years":to_years(g(r,"symptom duration (years)")),
            "flags":to_num(g(r,"Fields Flagged (count)"))})
    return out


# ---------- data dictionary (operational definitions) ----------
HKEY = {"surgery_date":"date","age":"age","sex":"sex","preop_dx":"preop","postop_dx":"postop","procedure":"proc",
        "secondary_procedure":"sec","mesh":"proc","anesthesia_type":"anes","operative_minutes":"dur","los_days":"los",
        "complication":"comp","seen_post_op":"fu","h_score":"h","symptom_years":"sx"}
DICTIONARY = [
 # variable, definition, pipeline source column(s), coding in both sources, comparable when, agreement rule, known differences
 ("surgery_date","Calendar date of the index operation.",
  "Date of surgery (Anesthesia Record date, OR Record fallback)",
  "Parsed to a date. Excel serials and m/d/y text accepted; impossible dates set missing. Hand-log year forced to the trip year when mistyped (GH14 rows read 2024).",
  "Both dates present.","Same calendar day.",
  "Some hand logs record the clinic or mission date rather than the operating day."),
 ("age","Age in years at operation.","Age",
  "Numeric years. 'N months' converted to years. Ranges such as '50-60' set missing.",
  "Both present.","Within 1 year.",
  "Hand logs sometimes record age at registration; charts may give age or date of birth."),
 ("sex","Patient sex.","Gender",
  "M or F from first letter, or from combined 'Age/Gender' cells such as '45M'.",
  "Both present.","Exact.",""),
 ("preop_dx","Pre-operative diagnosis.","Preop DX",
  "Free text mapped to condition categories (inguinal, femoral, umbilical, epigastric, ventral/incisional hernia; hydrocele; uterine/fibroid; ovarian/adnexal; prostate/bladder; thyroid; breast; burn/contracture; soft-tissue mass; undescended testis; other) and side (right, left, bilateral, unstated) by keyword rules, including common abbreviations (RIH, LIHR, UHR, UF, ME).",
  "Both present.","Share at least one condition category and the stated sides do not conflict (an unstated side does not count against agreement).",
  "Coarse: subtypes such as direct vs indirect inguinal hernia are not distinguished."),
 ("postop_dx","Post-operative diagnosis.","Postop Dx",
  "As pre-op diagnosis. 'Same' (or text starting 'same') replaced by the same source's pre-op diagnosis.",
  "Both present and both name a condition. Findings-only text (e.g. 'small direct defect', 'H1', '5 cm') is not compared.",
  "As pre-op diagnosis.",
  "Charts often record operative findings rather than a diagnosis in this box."),
 ("procedure","Index operation.","Procedure",
  "As diagnosis: condition category plus side. Identical wording (letters and digits only, case-insensitive) reported separately.",
  "Both present.","Same category and non-conflicting side.",""),
 ("secondary_procedure","Any second operation in the same sitting.","Secondary Procedure",
  "Blank, 'none' or equivalent = none; otherwise categorised as procedure.",
  "Always (absence is informative).","Both none, or both present and the same category and side.",
  "Hand logs leave this blank both when there was none and when it was not recorded."),
 ("mesh","Prosthetic mesh used (hernia repairs).","Procedure, Secondary Procedure, Repair Technique",
  "Y if text mentions mesh, Prolene, plug, patch, Lichtenstein, TEP or TAPP. N if text says no mesh, tissue repair, Bassini, Shouldice, Desarda, McVay, Marcy, herniotomy or high ligation. Otherwise missing.",
  "Both sources state Y or N.","Exact.",
  "No hand log has a mesh column; mesh is inferred from procedure text, which early logs rarely mention."),
 ("anesthesia_type","Primary anesthetic technique.","Anesthesia Type",
  "Mapped to general (GA, GETA, ETT, LMA, intubation), spinal/neuraxial (spinal, SAB, epidural, regional, saddle), local/sedation (local, block, MAC, sedation, ketamine) or other. When general and spinal are both mentioned, general is taken.",
  "Both present.","Same category.",
  "The pipeline often records general where the hand log says spinal; not adjudicated."),
 ("operative_minutes","Operative duration in minutes.","operative time (mins) (incision to closure)",
  "Numeric minutes; 'h' and 'min' strings converted. Values covering two operations (e.g. '90/60') set missing.",
  "Both present.","Within 5 minutes (within 15 minutes as sensitivity analysis).",
  "Hand logs often round (60, 90, 120). The hand-log field may mean OR time rather than incision-to-closure."),
 ("los_days","Length of stay in days.","LOS (calendar days, surgery date to discharge date)",
  "Numeric days.","Both present.","Same whole number of days.",
  "Hand logs appear to count nights; the pipeline counts calendar days (same-day discharge = 0). Pipeline discharge dates include known misreads."),
 ("complication","Post-operative complication that needed treatment.","Post-Op Complication, Complications/Notes, Post-Op Visit Findings",
  "Primary (treated-complication) definition: surgical complications (hematoma, seroma, infection or abscess, wound breakdown or discharge, bleeding or haemorrhage, transfusion, return to theatre or re-operation, recurrence, crepitus, death) plus events that needed treatment (urinary retention or catheter placement, spinal or post-dural headache, pneumonia, UTI, ileus, desaturation/hypoxia). Hand log: numeric codes 1 (surgical) or 2 (anaesthetic) = Y, or text naming one of these events; blank, 0, 'none', 'no' = N; 'error in diagnosis' and 'unnecessary procedure' = N. Pipeline: the same terms in any of the three columns, ignoring negated mentions ('no hematoma') and pre-existing findings. Sensitivity: surgical complications only; any adverse event (adds nausea, vomiting, headache, swelling, fever, hypotension, numbness and similar).",
  "Always (blank hand-log cell = no complication).","Exact.",
  "Hand logs were not designed to capture nursing-note events; keyword rules involve judgment."),
 ("seen_post_op","Patient attended a post-operative visit.","Seen Post Op",
  "Y or N from the first letter or 1/0. Pipeline blank = N.","Hand log present.","Exact.",
  "Pipeline blank means no post-op visit form was found, which may also mean it was not scanned."),
 ("h_score","Inguinal hernia H score (EHS/ISHI H1-H4).","inguinal hernia score (H score)",
  "Grade 1-4 extracted from text such as 'H3a', '3b', '2'. Sub-letters (a/b/c) ignored.",
  "Both present.","Same grade.","Recorded only on later forms and only for hernias."),
 ("symptom_years","Duration of symptoms in years.","symptom duration (years)",
  "Numeric years; months, weeks and days converted.","Both present.","Within 1 year.",
  "Recorded only in some years."),
]

# ---------- comparison ----------
def same_condition(a,b):
    ca,cb=categories(a),categories(b)
    if ca is None or cb is None: return None
    if not (ca & cb) or (ca=={"other"} and cb=={"other"} and s(a).lower()!=s(b).lower()): return False
    sa,sb=side(a),side(b)
    return True if (sa is None or sb is None or sa==sb) else False
def compare(var,h,p):
    if var=="secondary_procedure":
        hn,pn = (h is None or NEG.match(s(h)) is not None),(p is None or NEG.match(s(p)) is not None)
        if hn and pn: return True
        if hn!=pn: return False
        return same_condition(h,p)
    if h is None or p is None: return None
    if var=="surgery_date": return h==p
    if var=="age": return abs(h-p)<=1
    if var in ("symptom_years",): return abs(h-p)<=1
    if var=="operative_minutes": return abs(h-p)<=5
    if var=="los_days": return round(h)==round(p)
    if var=="postop_dx" and (categories(h)=={"other"} or categories(p)=={"other"}): return None
    if var in ("preop_dx","postop_dx","procedure"): return same_condition(h,p)
    return h==p
def exact_words(h,p):
    if h is None or p is None: return None
    n=lambda x: re.sub(r"[^a-z0-9]","",s(x).lower())
    return n(h)==n(p)

def boot_ci(df, B=2000, seed=7):
    """Percentile CI for pooled agreement, resampling patients (cluster bootstrap)."""
    if df.empty: return (None,None)
    g=df.groupby("patient")["agree"].agg(["sum","count"]).values
    rnd=random.Random(seed); n=len(g); est=[]
    for _ in range(B):
        sm=ct=0
        for _ in range(n):
            a,b=g[rnd.randrange(n)]; sm+=a; ct+=b
        est.append(sm/ct)
    est.sort(); return (est[int(.025*B)],est[int(.975*B)-1])

def summarise(cmp, by):
    rows=[]
    for keys,grp in cmp.groupby(by,sort=False):
        keys=keys if isinstance(keys,tuple) else (keys,)
        lo,hi=boot_ci(grp)
        rows.append(dict(zip(by,keys),**{"Comparable pairs":len(grp),"Agree":int(grp.agree.sum()),
            "Agreement %":round(100*grp.agree.mean(),1),"95% CI low":round(100*lo,1),"95% CI high":round(100*hi,1)}))
    return pd.DataFrame(rows)


# extraction dates (median raw-JSON date per trip, read from the pipeline folder) and logged API cost
EXTRACTED = {"SL13":"2026-08-12","SL17":"2026-08-12","SL18":"2026-08-11","SL19":"2026-08-11","SL22":"2026-09-08",
 "SL23":"2026-08-10","SL24":"2026-08-10","GH14":"2026-08-30","GH15":"2026-08-30","GH16":"2026-08-29","GH17":"2026-08-29",
 "GH18":"2026-08-29","GH19":"2026-08-29","GH23":"2026-08-27","GH24":"2026-08-22","GH25M":"2026-08-21","GH25S":"2026-08-23",
 "GH26M":"2026-09-08"}
def logged_cost(trip_dir):
    tot=0.0; logs=[]; n=0
    for f in sorted(glob.glob(os.path.join(trip_dir,"*.log"))):
        if " copy" in f: continue
        txt=open(f,errors="ignore").read()
        m=[float(x) for x in re.findall(r"Estimated cost: \$([0-9.]+)",txt)]
        e=[int(x) for x in re.findall(r"Extracted (\d+) packet",txt)]
        if m: tot+=sum(m); n+=sum(e); logs.append(os.path.basename(f))
    return round(tot,2), logs, n

def availability(H, P, keys):
    """% of matched patients with a usable value, per variable and source."""
    out={}
    for v in VARS:
        if v in ("complication","secondary_procedure"):  # absence is informative for these two
            continue
        hv=sum(H[k][v] is not None for k in keys); pv=sum(P[k][v] is not None for k in keys)
        out[v]=(hv,pv)
    return out

SECONDARY={}
def main():
    os.makedirs(OUT,exist_ok=True)
    clean=[]; pairs=[]; linkage=[]; avail=[]; costs=[]
    for site,trip,year,hf,pf in TRIPS:
        hp=os.path.join(HAND,hf); pp=os.path.join(PIPE,pf)
        H={r["key"]:r for r in read_hand(hp,trip,year)}
        P={r["key"]:r for r in read_pipe(pp,trip,year)}
        both=[k for k in P if k in H]
        # secondary linkage: unmatched records with a unique counterpart on sex, age (+-1 y) and operation (category + side)
        uP=[k for k in P if k not in H]; uH=[k for k in H if k not in P]
        def fits(p_,h_):
            a,b=P[p_],H[h_]
            return bool(a["sex"] and a["sex"]==b["sex"] and (a["age"] is None or b["age"] is None or abs(a["age"]-b["age"])<=1)
                    and bool(same_condition(a["procedure"],b["procedure"])))
        sec={}
        for p_ in uP:
            c=[h_ for h_ in uH if fits(p_,h_)]
            if len(c)==1 and sum(fits(q,c[0]) for q in uP)==1: sec[p_]=c[0]
        SECONDARY[trip]=sec
        linkage.append({"Site":site,"Trip":trip,"Year":year,"Extracted":EXTRACTED[trip],
            "Pipeline records":len(P),"Hand-log records":len(H),"Matched":len(both),
            "Linked by sex/operation/age (sensitivity only)":len(sec),
            "Pipeline only":len([k for k in P if k not in H])-len(sec),"Hand-log only":len([k for k in H if k not in P])-len(sec),
            "Hand-log rows without chart number":sum(1 for k in H if k.startswith("nomrn"))})
        c,logs,n=logged_cost(os.path.dirname(pp))
        costs.append({"Site":site,"Trip":trip,"Patients in case log":len(P),"Packets covered by logs":n,
            "Logged API cost (USD)":c,"Cost per logged packet (USD)":round(c/n,3) if n else None,
            "Log complete?":"yes" if n>=len(P)-5 else "no (earlier runs not logged)","Run logs read":", ".join(logs)})
        a=availability(H,P,both)
        for v,(hv,pv) in a.items():
            avail.append({"Site":site,"Trip":trip,"Year":year,"Variable":LABEL[v],
                "Hand log recorded (%)":round(100*hv/len(both),0) if both else None,
                "Pipeline recovered (%)":round(100*pv/len(both),0) if both else None})
        for k in both+list(sec):
            hk = sec.get(k,k); link="secondary" if k in sec else "chart number"
            pid=f"{trip}-{k}"
            for src,R in (("hand log",H[hk]),("pipeline",P[k])):
                clean.append({"Site":site,"Trip":trip,"Year":year,"Patient ID":pid,"Linkage":link,"Source":src,**{LABEL[v]:R[v] for v in VARS}})
            for v in VARS:
                a_=compare(v,H[hk][v],P[k][v])
                if a_ is None: continue
                d={"site":site,"trip":trip,"year":year,"extracted":EXTRACTED[trip],"patient":pid,"variable":LABEL[v],
                   "agree":bool(a_),"hand":H[hk][v],"pipeline":P[k][v],"analysis":"primary","linkage":link}
                pairs.append(d)
                if v=="operative_minutes":
                    pairs.append({**d,"variable":"Operative duration (within 15 min)","agree":abs(H[hk][v]-P[k][v])<=15,"analysis":"sensitivity"})
                if v=="complication":
                    for lab,key in (("Complication (surgical only)","complication_surgical"),("Complication (any adverse event)","complication_any")):
                        hs,ps=H[hk][key],P[k][key]
                        pairs.append({**d,"variable":lab,"agree":hs==ps,"hand":hs,"pipeline":ps,"analysis":"sensitivity"})
            ew=exact_words(H[hk]["procedure"],P[k]["procedure"])
            if ew is not None:
                pairs.append({"site":site,"trip":trip,"year":year,"extracted":EXTRACTED[trip],"patient":pid,
                    "variable":"Procedure (identical wording)","agree":ew,"hand":H[hk]["procedure"],
                    "pipeline":P[k]["procedure"],"analysis":"sensitivity","linkage":link})
    cmp=pd.DataFrame(pairs)
    allp=cmp[cmp.analysis=="primary"]
    T_link=pd.concat([summarise(allp[allp.linkage=="chart number"].assign(set="Chart-number links only (primary)"),["site","set"]),
                      summarise(allp.assign(set="Plus age/sex/operation links"),["site","set"])])
    cmp=cmp[cmp.linkage=="chart number"]; prim=cmp[cmp.analysis=="primary"]
    var_order=[LABEL[v] for v in VARS]
    def vsort(df):
        df["o"]=df.variable.map(lambda x: var_order.index(x) if x in var_order else 99); return df.sort_values(["o"]).drop(columns="o")
    # pooled agreement excluding complication (its disagreements are one-directional recovery, reported separately)
    core=prim[prim.variable!="Complication"]
    T={}
    T["Overall"]=pd.concat([summarise(prim.assign(scope="All 15 variables"),["scope"]),
                           summarise(core.assign(scope="14 variables (complication reported separately)"),["scope"])])
    T["By site"]=pd.concat([summarise(prim.assign(set="All 15"),["site","set"]),summarise(core.assign(set="14 (no complication)"),["site","set"])])
    T["By trip"]=summarise(core,["site","trip","year","extracted"])
    T["By extraction order"]=summarise(core,["extracted","site","trip"]).sort_values(["extracted","trip"])
    T["By variable"]=vsort(summarise(prim,["variable"]))
    T["By site x variable"]=vsort(summarise(prim,["site","variable"]))
    T["By trip x variable"]=summarise(prim,["site","trip","variable"])
    T["Sensitivity"]=summarise(cmp[cmp.analysis=="sensitivity"],["site","variable"])
    # complications: paired 2x2 and McNemar exact test
    cp=prim[prim.variable=="Complication"]; mc=[]
    for name,g in [("All trips",cp)]+list(cp.groupby("site",sort=False))+list(cp.groupby("trip",sort=False)):
        H_,P_=g["hand"],g["pipeline"]
        both=int(((P_=="Y")&(H_=="Y")).sum()); b=int(((P_=="Y")&(H_=="N")).sum())
        c=int(((P_=="N")&(H_=="Y")).sum()); none=int(((P_=="N")&(H_=="N")).sum())
        mc.append({"Scope":name,"Patients":len(g),"Both report":both,"Pipeline only":b,"Hand log only":c,"Neither":none,
                   "McNemar exact p":round(binomtest(b,b+c,0.5).pvalue,4) if b+c else None})
    T["Complications"]=pd.DataFrame(mc)
    # trend tests: logistic regression with patient-clustered robust SEs on the 14 non-complication variables,
    # with variable fixed effects so a change in which fields are recorded does not masquerade as a trend
    try:
        import statsmodels.api as sm, statsmodels.formula.api as smf
        tr=[]
        d=core.copy(); d["agree_i"]=d.agree.astype(int)
        d["ext_week"]=(pd.to_datetime(d.extracted)-pd.Timestamp("2026-08-10")).dt.days/7
        for scope,g in [("All trips",d)]+list(d.groupby("site",sort=False)):
            for term,form,unit in [("year","agree_i ~ year + C(variable)","per calendar year of the trip"),
                                   ("ext_week","agree_i ~ ext_week + C(variable)","per week later the trip was run"),
                                   ]:
                if scope=="All trips": form=form+" + C(site)"
                rates=g.groupby("variable").agree_i.mean(); g=g[g.variable.isin(rates[(rates>0)&(rates<1)].index)]  # avoid separation
                m=smf.glm(form,g,family=sm.families.Binomial()).fit(cov_type="cluster",cov_kwds={"groups":pd.factorize(g.patient)[0]})
                b,se=m.params[term],m.bse[term]
                tr.append({"Scope":scope,"Time axis":unit,"Odds ratio":round(math.exp(b),3),
                           "95% CI low":round(math.exp(b-1.96*se),3),"95% CI high":round(math.exp(b+1.96*se),3),"p":round(m.pvalues[term],4)})
        # sensitivity: refit the mission-year model on only the non-complication variables recorded on every trip
        # (age, sex, primary procedure, secondary procedure, anesthesia type), so a changing variable mix cannot drive the trend
        every=sorted(v for v,gv in d.groupby("variable") if gv.trip.nunique()==d.trip.nunique())
        d5=d[d.variable.isin(every)]
        for scope,g in [("All trips",d5)]+list(d5.groupby("site",sort=False)):
            form="agree_i ~ year + C(variable)"+(" + C(site)" if scope=="All trips" else "")
            m=smf.glm(form,g,family=sm.families.Binomial()).fit(cov_type="cluster",cov_kwds={"groups":pd.factorize(g.patient)[0]})
            b,se=m.params["year"],m.bse["year"]
            tr.append({"Scope":scope,"Time axis":f"per calendar year, {len(every)} variables recorded on every trip ({', '.join(every)})",
                       "Odds ratio":round(math.exp(b),3),"95% CI low":round(math.exp(b-1.96*se),3),
                       "95% CI high":round(math.exp(b+1.96*se),3),"p":round(m.pvalues["year"],4)})
        T["Trend tests"]=pd.DataFrame(tr)
    except Exception as e:
        T["Trend tests"]=pd.DataFrame([{"error":str(e)}])
    T["Linkage"]=pd.DataFrame(linkage)
    T["Linkage sensitivity"]=T_link
    T["Cost"]=pd.DataFrame(costs)
    T["Recording by trip"]=pd.DataFrame(avail)
    F={"date":"Date of surgery","age":"Age","sex":"Sex","preop":"Pre-op diagnosis","postop":"Post-op diagnosis",
       "proc":"Procedure","sec":"Secondary procedure","dur":"Operative duration","anes":"Anesthesia type","los":"Length of stay",
       "comp":"Complication","fu":"Follow-up attendance","h":"Hernia H score","sx":"Symptom duration"}
    T["_unused_cols"]=pd.DataFrame([{"Trip":tr,**{lab:("yes" if k in HAND_FIELDS[tr] else "no column") for k,lab in F.items()},
        "Mesh use":"inferred from procedure text"} for _,tr,_,_,_ in TRIPS])
    T["_headers"]=HAND_HEADERS
    return T, pd.DataFrame(clean), cmp

LIMITATIONS = [
 ("Design","The hand log is not a reference standard. Agreement shows that two methods match, not that either is correct. No chart was re-read by a human for this analysis."),
 ("Hand-log structure","Hand-log columns change across trips (see 'Hand log columns by trip'). Several variables have no column in some years, so the number of comparable pairs and the mix of variables differ by trip."),
 ("Not recorded early","H score, symptom duration and follow-up were added to the hand logs in later years. Mesh never had its own column and is inferred from procedure text. Early trips therefore contribute few pairs for these variables."),
 ("Chart forms","The paper chart forms themselves changed across missions (for example a merged H&P/op-note page in early Ghana trips, post-op visit forms only on some trips, and a new 2025 form set). What the pipeline can recover differs by trip for this reason."),
 ("Complication coding","Hand logs coded complications as numbers (2017-2019), free text, or Y/N (2022 onward). A blank hand-log cell is treated as no complication. Pipeline complications come from keyword rules on chart free text, which involves judgment. A broader 'any adverse event' definition is reported as a sensitivity analysis."),
 ("Equivalence rules","Diagnoses and procedures count as agreeing when they name the same condition category and side. This is coarse: for example, direct and indirect inguinal hernia count as the same."),
 ("Rounding","Hand-log operative durations are often rounded (60, 90, 120 min), which lowers agreement within 5 minutes. A within-15-minute sensitivity analysis is reported."),
 ("Length of stay","Hand logs appear to count nights, while the pipeline counts calendar days from surgery to discharge (same-day = 0). The pipeline's discharge date also has known misreads. Length-of-stay agreement mixes these causes."),
 ("Dates","Some hand logs record the clinic or mission date rather than the operation date, and some have the wrong year typed (GH14 rows read 2024; corrected to the trip year)."),
 ("Independence","Drive shows the Ghana 2024, May 2025 and May 2026 hand logs modified on 2026-10-25, after their pipeline runs. If pipeline values were copied in, agreement for those trips is inflated. Confirm with the log owner."),
 ("Linkage","Records were linked by trip and chart number only. Hand-log rows without a chart number (for example 12 in GH15) cannot be linked. The GH25S hand log has 22 rows with no matching scanned packet."),
 ("Sample","Sierra Leone 2025 is excluded: the only pipeline copy available had been recoded by hand, so it is not raw pipeline output."),
 ("Pipeline version","Extraction dates span 10 Aug to 8 Sep 2026 only, and later code fixes were applied by rebuilding from cached outputs. Extraction date is a weak proxy for pipeline maturity."),
 ("Cost","Run logs are complete for only 8 of 18 trips, and the pipeline's logged estimates use hard-coded prices that run 30-60% above billed spend. Cost is therefore reported from Claude Console billing (organization 'Global Surgery'), grouped by billing day. Billing days are UTC and cannot separate Ghana from Peru on Aug 30-31; the all-in figure includes development and repeated runs."),
 ("Anesthesia","The pipeline often reads 'General' where the hand log says spinal (for example 24 of 60 SL23 cases). The cause is unadjudicated and may be a checkbox read on the anesthesia record."),
]
ABSTRACT = [("Trips (Sierra Leone)","8 (2013-2025)","7 (2013-2024; 2025 excluded)"),
 ("Patients matched","494","428"),("Comparable data points","5,151","4,635"),
 ("Overall agreement","86.3% (85.3-87.3)",None),("Sex","98.1%",None),("Mesh use","95.8%",None),
 ("Follow-up attendance","95.1%",None),("Procedure (same operation)","92.4%",None),("Procedure (identical wording)","14.2%",None),
 ("Anesthesia type","72.6%",None),("Operative duration (within 5 min)","63.4%",None),("Complication","45.3%",None),
 ("Complications pipeline-only vs hand-only","99 vs 0",None),("Cost per patient","$0.24 (pipeline's own estimate)","$0.15 billed (Sierra Leone 2013-2024, Aug 9-12 Console spend); $0.21 billed all-in, all sites and development")]

if __name__=="__main__":
    T, clean, cmp = main()
    sl=T["By site x variable"]; sl=sl[sl.site=="Sierra Leone"].set_index("variable")["Agreement %"]
    slo=T["Overall"]; sls=T["By site"]; slc=T["Complications"].set_index("Scope")
    sens=T["Sensitivity"]; sens=sens[sens.site=="Sierra Leone"].set_index("variable")["Agreement %"]
    cst=T["Cost"]; done=cst[cst["Log complete?"]=="yes"]
    fill={"Overall agreement":f"{sls.iloc[0]['Agreement %']}% ({sls.iloc[0]['95% CI low']}-{sls.iloc[0]['95% CI high']})",
      "Sex":f"{sl.get('Sex')}%","Mesh use":f"{sl.get('Mesh use')}%","Follow-up attendance":f"{sl.get('Follow-up attendance')}%",
      "Procedure (same operation)":f"{sl.get('Procedure')}%","Procedure (identical wording)":f"{sens.get('Procedure (identical wording)')}%",
      "Anesthesia type":f"{sl.get('Anesthesia type')}%","Operative duration (within 5 min)":f"{sl.get('Operative duration')}%",
      "Complication":f"{sl.get('Complication')}% (treated-complication definition)",
      "Complications pipeline-only vs hand-only":f"{slc.loc['Sierra Leone','Pipeline only']} vs {slc.loc['Sierra Leone','Hand log only']} (p={slc.loc['Sierra Leone','McNemar exact p']})",
      }
    T["Rebuild vs SAGES abstract"]=pd.DataFrame([{"Measure":a,"SAGES abstract (Sierra Leone)":b,"Rebuilt (Sierra Leone)":c or fill.get(a)} for a,b,c in ABSTRACT])
    T["Limitations"]=pd.DataFrame(LIMITATIONS,columns=["Area","Limitation"])
    order=["Rebuild vs SAGES abstract","Overall","By site","By trip","By extraction order","Trend tests","By variable",
           "By site x variable","By trip x variable","Sensitivity","Complications","Recording by trip",
           "Linkage","Linkage sensitivity","Cost","Limitations"]
    dd=pd.DataFrame([{"Variable":LABEL[v],"Definition":d,"Pipeline source column(s)":ps,
        "Hand-log source columns":"Varies by trip; see 'Hand log source columns'","Coding (both sources)":c,
        "Comparable when":cw,"Agreement rule":ar,"Known definitional differences":kd} for v,d,ps,c,cw,ar,kd in DICTIONARY])
    hh=T.pop("_headers")
    hs=pd.DataFrame([{"Trip":tr,**{LABEL[v]:("; ".join(hh.get(tr,{}).get(HKEY[v],[])) or "not recorded")+(" (inferred from procedure text)" if v=="mesh" else "")
                      for v in VARS}} for _,tr,_,_,_ in TRIPS])
    T["Data dictionary"]=dd; T["Hand log source columns"]=hs
    order=["Data dictionary","Hand log source columns"]+order
    with pd.ExcelWriter(os.path.join(OUT,"concordance_results.xlsx"),engine="openpyxl") as xw:
        for n in order: T[n].to_excel(xw,sheet_name=n[:31],index=False)
        pd.DataFrame([{"Variable":LABEL[v],"Agreement rule":RULE[v]} for v in VARS]).to_excel(xw,sheet_name="Rules",index=False)
    with pd.ExcelWriter(os.path.join(OUT,"clean_15_variable_dataset.xlsx")) as xw:
        clean.to_excel(xw,sheet_name="All trips",index=False)
        for site,g in clean.groupby("Site",sort=False): g.to_excel(xw,sheet_name=site,index=False)
        for trip,g in clean.groupby("Trip",sort=False): g.to_excel(xw,sheet_name=trip,index=False)
    cmp.to_csv(os.path.join(OUT,"pair_level_comparisons.csv"),index=False)
    for n in ("Rebuild vs SAGES abstract","Overall","By site","By variable","Complications","Linkage","Linkage sensitivity","Trend tests"):
        print("\n##",n); print(T[n].to_string(index=False))
