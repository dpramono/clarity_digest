#!/usr/bin/env python3
"""
CardioDigest v2.1 - weekly PubMed triage for cardiologists (CLARITY).

Pipeline:
  1. Pull everything ADDED to PubMed in the last N days from a whitelist of
     core cardiology journals (take all) + top general journals (cardio filter),
     PLUS every paper still waiting in the retry queue (fetched by PMID, so it is
     found again no matter how old it is).
  2. Fetch metadata + abstracts (E-utilities efetch, XML); fill missing abstracts
     from Europe PMC / Crossref / Semantic Scholar.
  3. Score each paper with an LLM acting as a senior cardiologist
     (cached, retried, never silently turned into "score 0").
  4. Emit a Markdown digest plus the CLARITY documents (PDF / Word) as a
     long edition (full study pages) and a short edition (2-3 sentences per study).

What is new in v2 - "nothing disappears silently":
  * A missing abstract is NOT a quality verdict. Such papers get a provisional,
    title-based triage, enter a persistent retry queue (retry_queue.json) and are
    re-checked by PMID on every run until the abstract exists (max. 28 days).
  * Letters, replies, comments and editorials are filtered BEFORE scoring and
    logged (also by PubMed publication type, not only by title).
  * Every paper that does not get a study page has a recorded reason:
    dropped.csv (this run), dropped_history.csv (all runs), funnel_report.md.
  * Documents carry three appendices instead of silent cut-offs:
    "abstract pending", "further relevant studies" (cut by --top) and
    "for manual review" (top-journal papers, trial names from the reference base,
    RCT/meta-analyses below the threshold, failed scoring or summaries).
  * Papers can belong to several subgroups (secondary_subgroups + a deterministic
    structural-heart title rule), so a valve study in an imaging journal is found.
  * Scoring failures (API error, unparsable JSON) are retried and queued instead
    of becoming "relevance 0". Scores, summaries and grounded relation texts are
    cached, so re-runs cost (almost) nothing.
  * English is the master language (--lang de for German, Swiss spelling). Each language
    is written straight from the English abstract, never translated from another edition.
  * New "short" field: short and long edition come from ONE model call.
  * Reader-facing texts contain no pipeline jargon; retrieval remarks go to
    qc_report.md (qc_notes), never into the document.
  * --recall-check compares PubMed publication types (RCT, meta-analysis) against
    what the pipeline actually saw, to measure what was missed.

What is new in v2.1 - made from the first real output:
  * Reader text is cleaned when the documents are built (display_paper): German terms
    in tables, one number format (Swiss apostrophe from 5 digits), no pipeline jargon
    ("Landmark (...)", "im vorliegenden Material", "in den Passagen"), no trailing
    period in titles, a single number for "Teilnehmende".
  * Title-only commentaries / editorials ("What does FFR measure after TAVI?") are
    recognised, are not announced as "abstract follows" and are not listed for review.
  * The guideline appendix holds only real guidelines / consensus / position papers
    (reviews and editorials go to "further studies" instead).
  * New structural topic "Infective endocarditis" (+ optional ESC 2023 endocarditis PDF
    in the References folder); new topics are merged into older reference files.
  * "interventional" means: the paper evaluates a catheter-based / surgical intervention.
  * Fewer false alarms in the editorial-review list (trial name only counted in the
    title or in primary studies; case-report titles and trivial scores skipped).

Tune these (the actual product IP):
  - JOURNALS_SPEC / JOURNALS_GENERAL / JOURNALS_ADJACENT   (coverage vs. noise)
  - TIAB_KEYWORDS                        (recall in general journals)
  - SCORING_SYSTEM / SCORING_RUBRIC      (what "relevant" means)

Run:
  pip install requests anthropic pypdf python-docx reportlab matplotlib markdown
  export NCBI_EMAIL="you@example.com"      # NCBI asks callers to identify
  export NCBI_API_KEY="..."                # optional, raises rate limit 3->10/s
  export ANTHROPIC_API_KEY="..."
  export STATE_DIR=/content/drive/MyDrive/clarity
  export SUMMARY_MODEL=claude-sonnet-5-5   # summaries / relation text (triage: LLM_MODEL)

  # weekly production run: both editions as PDF and Word
  python cardio_digest.py --days 7 --pdf --docx --only "Strukturelle Kardiologie"
  # test run (does not touch seen_pmids / retry queue / history):
  python cardio_digest.py --days 30 --no-dedup --pdf --docx --only "Strukturelle Kardiologie" --top 8
  # only the short edition, German:
  python cardio_digest.py --pdf --edition short --lang de
  # what did we miss?  (no model calls)
  python cardio_digest.py --days 60 --recall-check

Files written to STATE_DIR (read these after every run):
  clarity_cardio_long.pdf/.docx   long edition        clarity_cardio_short.pdf/.docx   short edition
                     (German edition: clarity_cardio_lang / clarity_cardio_kurz)
  funnel_report.md   what happened to every paper (counts per status + manual-review list)
  dropped.csv        every paper WITHOUT a study page, with status and reason (this run)
  dropped_history.csv  the same, all runs      qc_report.md   editor notes per featured paper
  retry_queue.json   papers waiting for an abstract / retry   recall_report.md  from --recall-check
Flags: --lang en|de  --edition long|short|both  --top N (max, default 10)  --min N (default 0 = off)  --only X  --workers N  --no-manual
       --no-dedup  --rebuild  --limit N  --no-grounding  --email  --build-refs  --refs-docx
"""

import os
import re
import sys
import copy
import csv
import json
import time
import hashlib
import argparse
import configparser
import datetime as dt
import xml.etree.ElementTree as ET
from pathlib import Path
import requests

from google import genai
from google.genai import types as genai_types

VERSION = "2.2"

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_FILE = SCRIPT_DIR / "config" / "gemini_clarity.cfg"
ANTHROPIC_CONFIG_FILE = SCRIPT_DIR / "config" / "anthropic_clarity.cfg"

def read_config(path):
    """Eine .cfg-Datei lesen; None, wenn sie fehlt."""
    if not Path(path).is_file():
        return None
    cfg = configparser.ConfigParser()
    cfg.read(path, encoding="utf-8")
    return cfg

def _cfg2env(cfg, section, option, envname):
    """Wert aus der Config in die Umgebung übernehmen (leere Werte werden übersprungen)."""
    if cfg is not None and cfg.has_section(section):
        val = cfg.get(section, option, fallback="").strip()
        if val:
            os.environ[envname] = val

def apply_config(cfg, llm_section):
    """Gemeinsame Abschnitte plus den Abschnitt des LLM-Anbieters (GEMINI / ANTHROPIC)."""
    _cfg2env(cfg, "NCBI", "email", "NCBI_EMAIL")
    _cfg2env(cfg, "NCBI", "api_key", "NCBI_API_KEY")
    _cfg2env(cfg, "NCBI", "tool", "NCBI_TOOL")
    _cfg2env(cfg, llm_section, "api_key", f"{llm_section}_API_KEY")
    _cfg2env(cfg, llm_section, "llm_model", "LLM_MODEL")
    _cfg2env(cfg, llm_section, "summary_model", "SUMMARY_MODEL")
    _cfg2env(cfg, "GENERAL", "dry_run", "DRY_RUN_DEFAULT")
    _cfg2env(cfg, "GENERAL", "min_score", "MIN_SCORE")
    _cfg2env(cfg, "GENERAL", "doc_min_score", "DOC_MIN_SCORE")
    _cfg2env(cfg, "GENERAL", "max_per_subgroup", "MAX_PER_SUBGROUP")

# Anthropic zuletzt: die Pipeline ruft die Anthropic-API auf, deshalb gewinnen deren Modellnamen.
apply_config(read_config(CONFIG_FILE), "GEMINI")
apply_config(read_config(ANTHROPIC_CONFIG_FILE), "ANTHROPIC")

# ---------------------------------------------------------------------------
# Globale Konstanten / API-Clients
# ---------------------------------------------------------------------------

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

# was tut die Funktion _clean_model?
 
def _clean_model(name, default):
    n = (name or "").strip().replace("\u2013", "-").replace("\u2014", "-")
    if n.startswith("models/"):
        n = n[len("models/"):]
    return n or default

LLM_MODEL = _clean_model(os.environ.get("LLM_MODEL"), "gemini-3.7-flash")
# Modul 2 laeuft nur auf wenigen Papers - ein staerkeres Modell lohnt sich hier.
SUMMARY_MODEL = _clean_model(os.environ.get("SUMMARY_MODEL"), LLM_MODEL)

client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

NCBI_EMAIL = os.environ.get("NCBI_EMAIL", "you@example.com")
NCBI_TOOL = os.environ.get("NCBI_TOOL", "ClarityDigest")
NCBI_API_KEY = os.environ.get("NCBI_API_KEY", "")

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
EUROPEPMC = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
CROSSREF = "https://api.crossref.org/works/"
SEMANTIC_SCHOLAR = "https://api.semanticscholar.org/graph/v1/paper/"

# Default-Pfade, alle relativ zum --specialty-dir
SUBGROUPS_FILE_DEFAULT = "params/subgroups.lst"
JOURNALS_SPEC_FILE_DEFAULT = "params/journals_spec.lst"
JOURNALS_GEN_FILE_DEFAULT = SCRIPT_DIR / "journals_general.lst"   # Ausnahme: im Programmverzeichnis
JOURNALS_ADJ_FILE_DEFAULT = "params/journals_adjacent.lst"
TIAB_KEYWORDS_FILE_DEFAULT = "params/tiab_keywords.lst"
SCORING_SYSTEM_FILE_DEFAULT = "params/score_sys.txt"
SCORING_RUBRIC_FILE_DEFAULT = "params/score_rubric.txt"
SUMMARY_SYSTEM_FILE_DEFAULT = "params/summary_sys.txt"
SUMMARY_RUBRIC_FILE_DEFAULT = "params/summary_rubric.txt"
NOISE_FILE_DEFAULT = "params/noise_titles.lst"
ALIASES_FILE_DEFAULT = "params/subgroup_aliases.json"
BRANDING_FILE_DEFAULT = "params/branding.json"
DISCLAIMER_FILE_DEFAULT = "params/disclaimer.txt"
STATE_DIR_DEFAULT = "state"

SPECIALTY_DIR = Path(os.environ.get("SPECIALTY_DIR",
                                    SCRIPT_DIR / "specialties" / "cardiology_digest"))

PARAM_SOURCES = {}          # Pfad -> Anzahl Einträge (für --check-params)

def load_list_param(relpath, allow_empty=False):
    """Eine .lst-Parameterdatei relativ zu SPECIALTY_DIR (ein absoluter Pfad gilt direkt):
    ein Eintrag pro Zeile, ein Komma am Zeilenende wird entfernt, Leerzeilen und
    Zeilen mit '#' werden ignoriert. allow_empty=True: eine leere Datei ergibt []."""
    path = SPECIALTY_DIR / relpath
    try:
        with open(path, encoding="utf-8-sig") as f:
            items = [ln.strip().rstrip(",").strip() for ln in f
                     if ln.strip() and not ln.lstrip().startswith("#")]
    except FileNotFoundError:
        sys.exit(f"Parameterdatei fehlt: {path}")
    if not items and not allow_empty:
        sys.exit(f"Parameterdatei ist leer: {path}")
    PARAM_SOURCES[str(path)] = len(items)
    return items

# !---------------------------------------------------------------------------!
# Ab hier original code, not config file 

# An API key raises the E-utilities limit from 3 to 10 requests/sec.
# Get one at https://www.ncbi.nlm.nih.gov/account/ -> Settings -> API Key.
NCBI_EMAIL = os.environ.get("NCBI_EMAIL", "you@example.com")
NCBI_TOOL = "CardioDigest"
NCBI_API_KEY = os.environ.get("NCBI_API_KEY", "")

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
# Haiku is cheap and good enough for triage; switch to a Sonnet/Opus-class model
# for sharper judgement. Check current model names at https://docs.claude.com
LLM_MODEL = os.environ.get("LLM_MODEL", "claude-haiku-4-5")

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"

RELDATE_DAYS = 7          # "added to PubMed in the last N days"
MIN_SCORE = 6             # drop anything below this (quality > quantity)
PDF_MIN_SCORE = 6         # relevance floor for the CLARITY PDF (avoid padding weak subgroups)
MAX_PER_SUBGROUP = 5      # cap for the Markdown digest only
MAX_STUDIES = 10          # study pages per subgroup (--top)
MIN_STUDIES = 0           # off by default; >0 fills up to this number ... (--min)
FALLBACK_SCORE = 5        # ... with the best studies scoring at least this

# Output language of the reader-facing texts ("en" is the master edition, "de" the German
# one); --lang overrides it. Every language is written by the model straight from the
# English abstract - nothing is machine-translated afterwards.
OUTPUT_LANG = os.environ.get("OUTPUT_LANG", "en").strip().lower() or "en"

# Bump a version to invalidate the matching cache after you change a prompt.
SCORING_VERSION = "2.1"
SUMMARY_VERSION = "2.3"
RELATION_VERSION = "2.1"

MAX_QUEUE_DAYS = 28       # a paper without abstract is re-checked for this long
LLM_RETRIES = 3           # attempts per model call (API error or unparsable JSON)


SUBGROUPS = load_list_param(SUBGROUPS_FILE_DEFAULT)
JOURNALS_SPEC = load_list_param(JOURNALS_SPEC_FILE_DEFAULT)
JOURNALS_GENERAL = load_list_param(JOURNALS_GEN_FILE_DEFAULT)
JOURNALS_ADJACENT = load_list_param(JOURNALS_ADJ_FILE_DEFAULT, allow_empty=True)
TIAB_KEYWORDS = load_list_param(TIAB_KEYWORDS_FILE_DEFAULT)

# ---------------------------------------------------------------------------
# 0b. Deterministic rules that run BEFORE (and next to) the language model
# ---------------------------------------------------------------------------
# Everything in this block is plain code: testable, free, and not subject to model
# mood. It decides what is noise, which journals must never lose a paper silently,
# and which papers belong to the structural-heart area regardless of the model's
# single-label routing.

def _jnorm(j):
    return re.sub(r"[\.\s]+", " ", (j or "").lower()).strip()

# Journals whose new original articles must never vanish without a trace.
TOP_JOURNALS_GENERAL = {_jnorm(j) for j in JOURNALS_GENERAL}
TOP_JOURNALS_ALL = TOP_JOURNALS_GENERAL | {
    _jnorm(j) for j in ("Eur Heart J", "Circulation", "J Am Coll Cardiol", "JAMA Cardiol")}

def is_top_general(journal):
    return _jnorm(journal) in TOP_JOURNALS_GENERAL

def is_top_any(journal):
    return _jnorm(journal) in TOP_JOURNALS_ALL

# Errata, retractions, letters, replies, comments, journal-scan columns.
NOISE_TITLE_PATTERNS = [
    r"^correction to\b", r"^corrigendum\b", r"^erratum\b", r"^author correction\b",
    r"^retraction\b", r"^retracted:", r"^withdrawn:", r"^expression of concern\b",
    r"^reply to\b", r"^response to\b", r"^reply\b", r"^response by\b",
    r"^letters?\b", r"^comments? on\b", r"\bregarding article\b",
    r"\bto the editor\b",
    r"(?:^|[\s\.\-–:])reply\.?$",            # '... Atrial Fibrillation. Reply.'
    r"^weekly journal scan\b", r"^journal scan\b", r"^in this issue\b",
    r"^this week in\b", r"^editor'?s? (?:note|choice|comment)\b",
]
_NOISE_RE = re.compile("|".join(NOISE_TITLE_PATTERNS), re.IGNORECASE)

# PubMed publication types that are never research. (A paper that ALSO carries a
# research type such as RCT/meta-analysis is not treated as noise on this basis.)
NOISE_PUBTYPES = {
    "letter", "comment", "editorial", "published erratum", "erratum",
    "retraction of publication", "retracted publication", "expression of concern",
    "news", "interview", "biography", "portrait", "newspaper article",
    "personal narrative", "introductory journal article", "duplicate publication",
    "historical article", "autobiography", "lecture", "address", "congress",
}
_HARD_NOISE_PUBTYPES = {"published erratum", "erratum", "retraction of publication",
                        "retracted publication", "expression of concern"}
_RESEARCH_PUBTYPES = {
    "randomized controlled trial", "clinical trial", "controlled clinical trial",
    "meta-analysis", "systematic review", "multicenter study", "observational study",
    "comparative study", "validation study", "pragmatic clinical trial",
}

def is_noise_title(title):
    return bool(_NOISE_RE.search((title or "").strip()))

def noise_reason(p):
    """'' if the record may be a real paper, otherwise a short reason string."""
    pts = {(t or "").strip().lower() for t in (p.get("pub_types") or [])}
    if pts & _HARD_NOISE_PUBTYPES:
        return "pubtype:" + sorted(pts & _HARD_NOISE_PUBTYPES)[0]
    if is_noise_title(p.get("title")):
        return "title:letter/reply/erratum"
    hit = pts & NOISE_PUBTYPES
    if hit and not (pts & _RESEARCH_PUBTYPES):
        return "pubtype:" + sorted(hit)[0]
    return ""

# Structural heart disease keywords in the TITLE. Used as a second, deterministic
# vote: a valve study in an imaging or EP journal must still reach the structural
# section even if the model routed it elsewhere.
STRUCTURAL_RE = re.compile(
    r"\b(TAVR|TAVI|TMVR|TTVR|TEER|M-TEER|T-TEER|MitraClip|TriClip|PASCAL|"
    r"transcatheter\s+(?:aortic|mitral|tricuspid|pulmonary|valve|edge)|"
    r"aortic\s+(?:stenosis|valve|regurgitation|bioprosthe\w*)|"
    r"mitral\s+(?:regurgitation|valve|stenosis|annul\w*|leaflet\w*)|"
    r"tricuspid|paravalvular|valve[\s\-]in[\s\-](?:valve|ring)|TAV[\s\-]in[\s\-]\w+|"
    r"bioprosthe\w*|valvular|valve\s+(?:replacement|repair|disease|durability)|"
    r"left\s+atrial\s+appendage|LAAO|LAAC|appendage\s+(?:closure|occlusion)|"
    r"patent\s+foramen|PFO|atrial\s+septal|endocarditis)\b", re.IGNORECASE)

def looks_structural(title):
    return bool(STRUCTURAL_RE.search(title or ""))

# A title WITHOUT any design / outcome word that is a question or opens like a commentary
# is an editorial, not a study. Used only for records that have no abstract (an abstract,
# once it exists, always wins). Deliberately narrow: a wrong "editorial" verdict would hide
# a real study, so anything unclear stays a normal title-only candidate.
_RESEARCH_WORDS = re.compile(
    r"\b(?:trials?|stud(?:y|ies)|registr(?:y|ies)|cohorts?|analys[ie]s|meta-?analys[ie]s|"
    r"randomi[sz]ed|outcomes?|comparison|versus|vs\.?|results|experience|series|evaluation|"
    r"validation|predict\w*|association|incidence|prevalence|safety|efficacy|feasibility|"
    r"data|systematic|review)\b", re.IGNORECASE)
_COMMENT_LEAD = re.compile(
    r"^(?:interpret\w*|reflections?|rethink\w*|reimagin\w*|revisit\w*|perspectives?|commentary|"
    r"editorial|viewpoint|opinion|lessons?|the case (?:for|against)|towards?|time (?:to|for)|"
    r"a call (?:for|to)|beyond|why|what|how(?:\s+to)?|when|where|who|should|must|does|do|did|"
    r"is|are|was|were|can|could|will|would)\b", re.IGNORECASE)

def commentary_like_title(title):
    t = (title or "").strip()
    if not t or _RESEARCH_WORDS.search(t):
        return False
    return "?" in t or bool(_COMMENT_LEAD.match(t))

# Case reports / "images in cardiology" never get an abstract and are not a study.
CASE_REPORT_RE = re.compile(
    r"\bcase (?:report|series|of)\b|\bin an? (?:patient|woman|man|child|young|\d+-year)|"
    r"\brescu\w*|\bsalvag\w*|\bbail-?out\b|\baccidental\w*|\bunusual\b|\bunexpected\b|"
    r"\bimages? in\b", re.IGNORECASE)

def looks_like_case_report(title):
    return bool(CASE_REPORT_RE.search(title or ""))

# Trial names that must always reach a human if they show up. Matched case-sensitively
# (OPTION, CLOSE, REDUCE ... are only trial names when written in capitals). The list
# is extended at run time with the all-caps trial names found in the reference base.
_CANARY_STATIC = [
    "CHAMPION-AF", "CLOSURE-AF", "PARTNER", "EVOLUT", "SURTAVI", "NOTION", "DEDICATE",
    "RHEIA", "COAPT", "RESHAPE-HF2", "MATTERHORN", "CLASP", "TRILUMINATE", "TRISCEND",
    "Tri.Fr", "PRAGUE-17", "OPTION", "SWISS-APERO", "CHOICE-MI", "SUMMIT", "LAMPOON",
    "PLUGinTAVI", "CLOSE", "REDUCE", "RESPECT", "MITRA-FR", "MITRA-PRO",
]
_canary_cache = {}

def build_canary_regex():
    if "re" in _canary_cache:
        return _canary_cache["re"]
    names = set(_CANARY_STATIC)
    try:
        for tiers in _EMBEDDED_STRUCTURAL_REFERENCES["topics"].values():
            for tier in ("landmark_trials", "consensus", "additional_guidelines"):
                for e in tiers.get(tier, []):
                    m = re.search(r"\(([A-Za-z0-9\.\- ]{3,30})\)\s*$", e["title"])
                    if m and m.group(1).upper() == m.group(1) and re.search(r"[A-Z]{3}", m.group(1)):
                        names.add(m.group(1).strip())
    except Exception:
        pass
    pat = "|".join(r"(?<![A-Za-z0-9])" + re.escape(n) + r"(?![A-Za-z0-9])"
                   for n in sorted(names, key=len, reverse=True))
    _canary_cache["re"] = re.compile(pat)       # case-sensitive on purpose
    return _canary_cache["re"]

def canary_hit(p):
    """First trial name from the reference base found in the TITLE, else ''. The abstract
    counts only for primary research (RCT / meta-analysis / large registry) or when the
    score is missing: a small series that merely cites a landmark trial is not a lead."""
    rx = build_canary_regex()
    m = rx.search(p.get("title", "") or "")
    if m:
        return m.group(0)
    sc = p.get("score") or {}
    if p.get("abstract") and (not sc or sc.get("failed") or sc.get("study_type") in (
            "RCT", "Meta-analysis / Systematic review", "Large observational / Registry")):
        m = rx.search(p["abstract"])
        return m.group(0) if m else ""
    return ""

# ---------------------------------------------------------------------------
# 1. Query
# ---------------------------------------------------------------------------

def _or(terms):
    return " OR ".join(terms)

def build_query():
    """All new articles from cardiology journals, PLUS cardiology-flavoured
    articles from top general journals and adjacent (surgery / echo / structural)
    journals. We deliberately do NOT filter on Publication Type at this stage:
    PubMed assigns pub types during indexing, which lags days-to-weeks, so a
    pub-type filter silently drops the newest papers. We rely on the journal
    whitelist to keep volume sane and let the LLM classify study type from the
    abstract instead."""
    cardio = _or(f'"{j}"[Journal]' for j in JOURNALS_SPEC)
    filtered = list(JOURNALS_GENERAL) + list(JOURNALS_ADJACENT)
    general_j = _or(f'"{j}"[Journal]' for j in filtered)
    tiab = _or(f'{k}[tiab]' for k in TIAB_KEYWORDS)
    return f"(({cardio}) OR (({general_j}) AND ({tiab})))"

# ---------------------------------------------------------------------------
# 2. E-utilities
# ---------------------------------------------------------------------------

def _params(extra):
    p = {"tool": NCBI_TOOL, "email": NCBI_EMAIL}
    if NCBI_API_KEY:
        p["api_key"] = NCBI_API_KEY
    p.update(extra)
    return p

def _sleep():
    time.sleep(0.11 if NCBI_API_KEY else 0.34)

def esearch_recent(term, reldate=RELDATE_DAYS):
    """Return PMIDs added to PubMed (datetype=edat) in the last `reldate` days."""
    pmids, retstart, retmax = [], 0, 200
    while True:
        r = requests.get(f"{EUTILS}/esearch.fcgi", params=_params({
            "db": "pubmed", "term": term, "reldate": reldate,
            "datetype": "edat", "retmode": "json",
            "retstart": retstart, "retmax": retmax,
        }), timeout=30)
        r.raise_for_status()
        res = r.json()["esearchresult"]
        pmids.extend(res.get("idlist", []))
        total = int(res.get("count", 0))
        retstart += retmax
        _sleep()
        if retstart >= total or not res.get("idlist"):
            break
    return pmids

def efetch_details(pmids):
    papers = []
    for i in range(0, len(pmids), 200):
        chunk = pmids[i:i + 200]
        r = requests.get(f"{EUTILS}/efetch.fcgi", params=_params({
            "db": "pubmed", "id": ",".join(chunk), "retmode": "xml",
        }), timeout=60)
        r.raise_for_status()
        papers.extend(parse_articles(r.text))
        _sleep()
    return papers

# ---------------------------------------------------------------------------
# XML parsing (kept pure/testable - takes a string, no network)
# ---------------------------------------------------------------------------

def _text(el):
    return "".join(el.itertext()).strip() if el is not None else ""

def _abstract(article):
    node = article.find(".//Abstract")
    if node is None:
        return ""
    parts = []
    for at in node.findall("AbstractText"):
        label = at.get("Label")
        txt = "".join(at.itertext()).strip()
        if txt:
            parts.append(f"{label}: {txt}" if label else txt)
    return "\n".join(parts).strip()

def _pubdate(article):
    pd = article.find(".//JournalIssue/PubDate")
    if pd is None:
        return ""
    ml = pd.find("MedlineDate")
    if ml is not None:
        return _text(ml)
    y, m, d = _text(pd.find("Year")), _text(pd.find("Month")), _text(pd.find("Day"))
    return " ".join(x for x in (y, m, d) if x)

def _doi(pubmed_article):
    for aid in pubmed_article.findall(".//ArticleIdList/ArticleId"):
        if aid.get("IdType") == "doi":
            return _text(aid)
    for el in pubmed_article.findall(".//ELocationID"):
        if el.get("EIdType") == "doi":
            return _text(el)
    return ""

def parse_articles(xml_text):
    root = ET.fromstring(xml_text)
    out = []
    for pa in root.findall(".//PubmedArticle"):
        art = pa.find(".//Article")
        if art is None:
            continue
        pmid = _text(pa.find(".//PMID"))
        journal = (_text(art.find(".//Journal/ISOAbbreviation"))
                   or _text(art.find(".//Journal/Title")))
        authors = []
        for a in art.findall(".//AuthorList/Author"):
            ln, fn = _text(a.find("LastName")), _text(a.find("ForeName"))
            if ln:
                authors.append(f"{ln} {fn[:1]}." if fn else ln)
        pubtypes = [_text(pt) for pt in art.findall(".//PublicationTypeList/PublicationType")]
        ad = art.find(".//ArticleDate")
        epub = ""
        if ad is not None and _text(ad.find("Year")):
            epub = "-".join(x.zfill(2) for x in (_text(ad.find("Year")), _text(ad.find("Month")),
                                                  _text(ad.find("Day"))) if x)
        out.append({
            "pmid": pmid,
            "title": _text(art.find(".//ArticleTitle")),
            "journal": journal,
            "journal_full": _text(art.find(".//Journal/Title")),
            "pub_date": _pubdate(art),
            "epub_date": epub,
            "authors": authors,
            "pub_types": pubtypes,
            "abstract": _abstract(art),
            "doi": _doi(pa),
            "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
        })
    return out

# ---------------------------------------------------------------------------
# 2b. Abstract fallback (Europe PMC) - for papers PubMed hasn't fully indexed
# ---------------------------------------------------------------------------
# Brand-new papers (esp. NEJM) often arrive in PubMed as a bare stub: title +
# journal + date, no abstract and no authors. Europe PMC is a free second source
# (no key) that sometimes has the abstract earlier. Best-effort: if it still has
# nothing, the retry logic in main() re-checks the paper next week.

EUROPEPMC = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
CROSSREF = "https://api.crossref.org/works/"
SEMANTIC_SCHOLAR = "https://api.semanticscholar.org/graph/v1/paper/"

def _strip_html(s):
    return re.sub(r"<[^>]+>", "", s or "").strip()

def _clean_abstract(s):
    """Strip markup and a leading 'Abstract' heading some sources include."""
    s = _strip_html(s)
    s = re.sub(r"^\s*abstract[:\s]*", "", s, flags=re.IGNORECASE)
    return re.sub(r"\s+\n", "\n", s).strip()

def fetch_europepmc_core(pmid):
    try:
        r = requests.get(EUROPEPMC, params={
            "query": f"EXT_ID:{pmid} AND SRC:MED",
            "resultType": "core", "format": "json",
        }, timeout=30)
        r.raise_for_status()
        results = r.json().get("resultList", {}).get("result", [])
        return results[0] if results else None
    except Exception:
        return None

def fetch_crossref_abstract(doi):
    """Some publishers (incl. EuroIntervention) deposit the abstract with Crossref
    earlier than PubMed passes it through. Best-effort, via the DOI."""
    if not doi:
        return ""
    try:
        params, headers = {}, {"User-Agent": "CardioDigest"}
        if NCBI_EMAIL and "@" in NCBI_EMAIL:      # Crossref "polite pool"
            params["mailto"] = NCBI_EMAIL
            headers["User-Agent"] = f"CardioDigest (mailto:{NCBI_EMAIL})"
        r = requests.get(CROSSREF + doi, params=params, headers=headers, timeout=30)
        if r.status_code >= 300:
            return ""
        return _clean_abstract(r.json().get("message", {}).get("abstract", ""))
    except Exception:
        return ""

def fetch_semanticscholar_abstract(pmid, doi):
    """Semantic Scholar often has abstracts that the other sources lack.
    Free, no key needed. Try PMID first, then DOI."""
    for ident in ([f"PMID:{pmid}"] if pmid else []) + ([f"DOI:{doi}"] if doi else []):
        try:
            r = requests.get(SEMANTIC_SCHOLAR + ident,
                             params={"fields": "abstract"}, timeout=30)
            if r.status_code >= 300:
                continue
            ab = _clean_abstract(r.json().get("abstract") or "")
            if ab:
                return ab
        except Exception:
            continue
        finally:
            time.sleep(0.2)
    return ""

def fetch_pubmed_publisher_abstract(pmid):
    """Re-fetch the single record from PubMed. Publisher-supplied (ahead-of-print)
    records sometimes gain their abstract a few days before full MEDLINE indexing,
    so a targeted re-fetch can succeed where the bulk fetch came back empty."""
    try:
        r = requests.get(f"{EUTILS}/efetch.fcgi", params=_params({
            "db": "pubmed", "id": pmid, "retmode": "xml",
        }), timeout=30)
        r.raise_for_status()
        recs = parse_articles(r.text)
        return _clean_abstract(recs[0]["abstract"]) if recs else ""
    except Exception:
        return ""
    finally:
        _sleep()

def enrich_missing_abstracts(papers):
    """Fill missing abstracts from, in order: Europe PMC, Crossref (DOI),
    Semantic Scholar, and a targeted PubMed re-fetch. Best-effort - if none of
    them has it yet, the paper is retried on the next run."""
    filled = 0
    for p in papers:
        if p["abstract"] and p["authors"]:
            continue
        # source 1: Europe PMC (also supplies authors)
        data = fetch_europepmc_core(p["pmid"])
        if data:
            if not p["abstract"]:
                ab = _clean_abstract(data.get("abstractText"))
                if ab:
                    p["abstract"] = ab
                    p["abstract_source"] = "Europe PMC"
                    filled += 1
            if not p["authors"]:
                astr = (data.get("authorString") or "").strip().rstrip(".")
                if astr:
                    p["authors"] = [a.strip() for a in astr.split(",") if a.strip()]
        # source 2: Crossref via DOI
        if not p["abstract"] and p.get("doi"):
            ab = fetch_crossref_abstract(p["doi"])
            if ab:
                p["abstract"] = ab
                p["abstract_source"] = "Crossref"
                filled += 1
        # source 3: Semantic Scholar (PMID, then DOI)
        if not p["abstract"]:
            ab = fetch_semanticscholar_abstract(p["pmid"], p.get("doi"))
            if ab:
                p["abstract"] = ab
                p["abstract_source"] = "Semantic Scholar"
                filled += 1
        # source 4: targeted PubMed re-fetch (publisher/ahead-of-print record)
        if not p["abstract"]:
            ab = fetch_pubmed_publisher_abstract(p["pmid"])
            if ab:
                p["abstract"] = ab
                p["abstract_source"] = "PubMed (publisher)"
                filled += 1
        time.sleep(0.2)
    return filled

# ---------------------------------------------------------------------------
# 2c. Plumbing shared by every model call: JSON parsing, retries, safe state files
# ---------------------------------------------------------------------------

def _try_json(text):
    """First JSON object in a model reply (tolerates code fences and extra prose)."""
    t = (text or "").strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t)
    candidates = [t]
    m = re.search(r"\{.*\}", t, re.DOTALL)
    if m:
        candidates.append(m.group(0))
    for candidate in candidates:
        if not candidate:
            continue
        try:
            obj = json.loads(candidate)
            if isinstance(obj, dict):
                return obj
        except Exception:
            continue
    return None


_FATAL_HTTP = {400, 401, 402, 403, 404, 413, 422}      # retrying cannot help

def llm_json(client, model, system, user, max_tokens, retries=None, tag=""):
    """Call the model and return (dict, "") or (None, reason). Retries transient API
    errors and unparsable / truncated replies. Never raises, never invents a result."""
    retries = retries or LLM_RETRIES
    last = ""
    for attempt in range(1, retries + 1):
        try:
            msg = client.messages.create(model=model, max_tokens=max_tokens, system=system,
                                         messages=[{"role": "user", "content": user}])
        except Exception as e:
            last = f"{type(e).__name__}: {str(e)[:160]}"
            if getattr(e, "status_code", None) in _FATAL_HTTP:
                break
            if attempt < retries:
                time.sleep(min(2 ** attempt, 15))
            continue
        text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        obj = _try_json(text)
        if obj is not None:
            return obj, ""
        stop = getattr(msg, "stop_reason", None)
        last = f"unparsable reply (stop_reason={stop!r}): {text[:100]!r}"
        if stop == "max_tokens":
            max_tokens = int(max_tokens * 1.5)          # truncated: give it more room
        if attempt < retries:
            time.sleep(1.5)
    if tag:
        print(f"  !! {tag}: {last}")
    return None, last


def load_json(name, default):
    try:
        with open(_sp(name), encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except Exception as e:
        # a corrupt state file must never be overwritten silently: keep a copy
        try:
            os.replace(_sp(name), _sp(name + ".corrupt"))
            print(f"  !! {name} was unreadable ({e}); moved to {name}.corrupt")
        except Exception:
            pass
        return default


def save_json(name, obj, **kw):
    """Atomic write (temp file + rename) so a crash or Drive hiccup cannot leave a
    half-written state file."""
    path = _sp(name)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, **kw)
        os.replace(tmp, path)
    except Exception:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, **kw)


# ---------------------------------------------------------------------------
# 2d. Triage (Module 1)
# ---------------------------------------------------------------------------

STRUCT = "Structural Heart Disease"

STRUCT_TOPICS = [
    "TAVR - low risk", "TAVR - intermediate/high risk",
    "TEER - secondary (functional) MR", "TEER - primary (degenerative) MR",
    "Transcatheter mitral valve replacement (TMVR)",
    "LAAO / left atrial appendage occlusion",
    "Transcatheter tricuspid valve (TEER/replacement)", "PFO / ASD closure",
    "Paravalvular leak", "Infective endocarditis", "Multivalvular disease", "Other",
]

STUDY_TYPES = ["RCT", "Meta-analysis / Systematic review", "Guideline / Consensus",
               "Large observational / Registry", "Small observational",
               "Diagnostic / Validation study", "Basic / Translational",
               "Review / Editorial", "Other"]
STATEMENT_TYPES = ["Major guideline", "Practical statement", "Primary study or other"]

SCORING_SYSTEM = (
    "You are a senior academic cardiologist and evidence-based-medicine expert "
    "triaging brand-new literature for busy practicing cardiologists. Be rigorous "
    "and skeptical: the large majority of papers do NOT change practice. Reward "
    "strong designs (RCTs, guidelines, large trials) and genuine clinical impact; "
    "penalise small, preliminary, or purely mechanistic work. Judge ONLY from the "
    "text provided. A missing abstract says NOTHING about the quality of a paper - it "
    "only means the PubMed record is brand new."
)

SCORING_NO_ABSTRACT_NOTE = (
    "\n\nTHIS RECORD HAS NO ABSTRACT YET. Triage from title, journal and publication "
    "type only, and do NOT let the missing abstract lower the score. If the title "
    "indicates original research (randomised trial, large registry, meta-analysis) on a "
    "clinically important question, score it at least 6. Commentaries, editorials, "
    "letters, replies, news items and 'what this trial means' pieces are "
    "'Review / Editorial' and score 1-3. Typical commentary titles: a question ('What does "
    "FFR measure after TAVI?'), 'Interpreting ...', 'Reflections on ...', 'How to ...', "
    "'The case for ...', or a trial name plus a topic without any design word. If the title "
    "is genuinely ambiguous, score 5-6 and say so in why_it_matters - never 2."
)

SCORING_RUBRIC = """Return a single JSON object and nothing else:
{
  "subgroup": one of __SUBGROUPS__ or "Other",
     Routing note for "Structural Heart Disease": this category is for ADULT
     structural/valvular heart disease and its catheter-based or surgical treatment -
     aortic, mitral, tricuspid and pulmonary valve disease, TAVR/TAVI, TEER, TMVR/TTVR,
     LAAO, PFO/ASD closure in adults, paravalvular leak, prosthetic valve dysfunction,
     endocarditis of a valve or prosthesis.
     ROUTE BY CLINICAL SUBJECT, NEVER BY JOURNAL. A study of a valve or a structural
     intervention stays in this category even when it appears in an imaging or
     electrophysiology journal, even when the outcome measure is echocardiographic, and
     even when the data come from a registry. Examples that DO belong here:
     echocardiographic outcomes after transcatheter tricuspid valve replacement; a meta-analysis of
     left atrial appendage closure versus anticoagulation; registry outcomes of
     antithrombotic strategy after valve-in-valve TAVR; natural history of quantified
     mitral regurgitation.
     It is NOT for: paediatric or congenital heart disease and its risk factors or
     epidemiology (the word "structural" in "structural congenital heart disease" does NOT
     make it this category), pregnancy/obstetric risk studies, health-policy or
     reimbursement studies, and papers whose SUBJECT is database linkage or surveillance
     methodology rather than a clinical result. Route those to "General Cardiology" or
     "Other".
  "secondary_subgroups": a list (usually empty, at most 2) of OTHER subgroups from the same
                list that the paper ALSO clearly belongs to, e.g. an AI echocardiography
                model for aortic stenosis: subgroup "Cardiac Imaging", secondary
                ["Structural Heart Disease"]. Use [] when there is no genuine second home.
  "structural_topic": if subgroup is "Structural Heart Disease" OR it is listed in
                secondary_subgroups, pick the single best match from __STRUCT_TOPICS__ ;
                otherwise null.
                Choose by the clinical subject, not by wording of the title. A tricuspid
                device study belongs to the tricuspid topic; aortic prosthesis outcomes to
                the matching TAVR topic; paravalvular leak and prosthetic valve dysfunction
                to "Paravalvular leak"; infective endocarditis (native OR prosthetic valve,
                including risk scores and imaging of endocarditis) to "Infective
                endocarditis"; anything else to "Other".
  "structural_topics_extra": a list (usually empty) of further topics from the same list
                when the study clearly covers more than one, e.g. a registry of degenerative
                AND functional mitral regurgitation: structural_topic "TEER - primary
                (degenerative) MR" and extra ["TEER - secondary (functional) MR"].
  "study_type": "RCT" | "Meta-analysis / Systematic review" | "Guideline / Consensus"
                | "Large observational / Registry" | "Small observational"
                | "Diagnostic / Validation study" | "Basic / Translational"
                | "Review / Editorial" | "Other",
  "statement_type": "Major guideline" | "Practical statement" | "Primary study or other",
  "interventional": true or false,
  "relevance_score": integer 1-10,
  "practice_changing": true or false,
  "why_it_matters": one sentence,
  "bottom_line": one or two sentences, concrete clinical takeaway
}

statement_type - how to classify:
  "Major guideline"      = ONLY a COMPLETE clinical practice guideline or a universal
                           disease DEFINITION from a major society (ESC/ACC/AHA/etc.),
                           e.g. "2026 ACC/AHA Guideline on ..." or "Universal Definition
                           of ...". These are broadly disseminated, long, and hard to
                           summarise faithfully, so we only NAME them.
                           NOT a Major guideline: a Scientific Statement, an expert
                           consensus / position paper, a best-practices document, an
                           erratum/correction, or a journal editorial/authorship policy
                           (e.g. ICMJE recommendations). Do not use this label for those.
  "Practical statement"  = a document issued by a society, task force or named expert
                           panel: Scientific Statement, expert consensus, position paper,
                           or a focused best-practices / step-by-step technical document
                           (e.g. an AHA Scientific Statement, an EACTS Expert Consensus
                           Document, a standardised-definitions paper). It is NOT a narrative
                           review, state-of-the-art review, editorial, commentary or opinion
                           piece written by individual authors - those are "Primary study or
                           other" with study_type "Review / Editorial", even when they discuss
                           guidelines or recommendations.
  "Primary study or other" = original research (RCT, registry, cohort, meta-analysis),
                           narrative reviews, editorials, epidemiology papers, methods
                           papers, case reports, and any journal editorial/publishing
                           policy document. Anything that is not a guideline or a society
                           statement. Score pure publishing-policy or non-clinical
                           documents very low (1-2).

interventional - set true ONLY if the paper itself evaluates, compares or reports the
  outcomes, technique or complications of a CATHETER-BASED / percutaneous cardiac
  intervention (PCI, stents, FFR/OCT/IVUS-guided PCI, TAVR/TAVI, valve-in-valve,
  transcatheter mitral or tricuspid repair/replacement incl. TEER, left atrial appendage
  occlusion, PFO/ASD closure, paravalvular leak closure, mechanical circulatory support in
  the cath lab) - also in comparison with surgery or medical therapy.
  Set FALSE for: imaging or diagnostic studies, AI models, risk scores, natural history
  and epidemiology, medical therapy, studies of patients explicitly WITHOUT an
  intervention, pure surgery, and electrophysiology ablation/device procedures (own category).

Score anchors:
  9-10  practice-changing RCT / new guideline in a major journal
  7-8   important; strong evidence likely to influence practice or debate
  4-6   incremental, confirmatory, or narrow relevance
  1-3   preliminary, niche, mechanistic, or low clinical relevance
""".replace("__SUBGROUPS__", json.dumps(SUBGROUPS)).replace(
    "__STRUCT_TOPICS__", json.dumps(STRUCT_TOPICS))


def _as_list(x):
    return [str(i).strip() for i in x if i] if isinstance(x, list) else []


def guess_structural_topics(title):
    """Keyword fallback when the model named no structural topic. Returns
    (topic, [extra topics]); ambiguous areas load both neighbouring topics."""
    t = (title or "").lower()
    if re.search(r"tricuspid|triscend|triluminate", t):
        return "Transcatheter tricuspid valve (TEER/replacement)", []
    if re.search(r"appendage|\blaao\b|\blaac\b", t):
        return "LAAO / left atrial appendage occlusion", []
    if re.search(r"foramen|\bpfo\b|atrial septal", t):
        return "PFO / ASD closure", []
    if re.search(r"endocarditis", t):
        return "Infective endocarditis", []
    if re.search(r"paravalvular|valve[\s\-]in[\s\-]valve|prosthe", t):
        return "Paravalvular leak", []
    if re.search(r"\btmvr\b|mitral valve replacement|mitral annular calc", t):
        return "Transcatheter mitral valve replacement (TMVR)", []
    if re.search(r"mitral|\bteer\b|mitraclip", t):
        return ("TEER - primary (degenerative) MR",
                ["TEER - secondary (functional) MR"])
    if re.search(r"\btavr\b|\btavi\b|aortic", t):
        return "TAVR - intermediate/high risk", ["TAVR - low risk"]
    return None, []


def normalize_score(sc, paper=None):
    """Make a model answer safe for everything downstream (idempotent): valid labels,
    integer score, secondary subgroups, and the deterministic structural vote."""
    sc = dict(sc or {})
    title = (paper or {}).get("title", "") or sc.get("_title", "")
    sg = sc.get("subgroup")
    sc["subgroup"] = sg = sg if sg in SUBGROUPS else "Other"
    sec = []
    for g in _as_list(sc.get("secondary_subgroups")):
        if g in SUBGROUPS and g != sg and g not in sec:
            sec.append(g)
    topic = sc.get("structural_topic")
    topic = topic if topic in STRUCT_TOPICS else None
    extra = [t for t in _as_list(sc.get("structural_topics_extra"))
             if t in STRUCT_TOPICS and t != topic]

    in_struct = (sg == STRUCT) or (STRUCT in sec)
    if not in_struct and (looks_structural(title) or (topic and topic != "Other")):
        sec.append(STRUCT)                       # deterministic second vote
        in_struct = True
    if in_struct and not topic:
        topic, guessed = guess_structural_topics(title)
        if topic:
            sc["topic_guessed"] = True
            extra = [t for t in (extra + guessed) if t != topic]
    sc["secondary_subgroups"] = sec[:3]
    sc["structural_topic"] = topic if in_struct else None
    sc["structural_topics_extra"] = extra[:2] if in_struct else []

    try:
        rs = int(round(float(sc.get("relevance_score"))))
    except (TypeError, ValueError):
        rs = 0
    sc["relevance_score"] = max(0, min(10, rs))
    if sc.get("study_type") not in STUDY_TYPES:
        sc["study_type"] = "Other"
    if sc.get("statement_type") not in STATEMENT_TYPES:
        sc["statement_type"] = "Primary study or other"
    # title-only record whose title reads like a commentary: treat as editorial (a guess,
    # kept in the retry queue so that an abstract, if one ever appears, is scored properly)
    if paper is not None and not paper.get("abstract") and not sc.get("failed"):
        pts = {(t or "").strip().lower() for t in (paper.get("pub_types") or [])}
        if (commentary_like_title(title) and not (pts & _RESEARCH_PUBTYPES)
                and sc.get("statement_type") == "Primary study or other"):
            sc["study_type"] = "Review / Editorial"
            sc["commentary_guess"] = True
    sc["interventional"] = bool(sc.get("interventional"))
    sc["practice_changing"] = bool(sc.get("practice_changing"))
    sc.setdefault("why_it_matters", "")
    sc.setdefault("bottom_line", "")
    sc.setdefault("failed", False)
    return sc


def failed_score(err=""):
    sc = normalize_score({"relevance_score": 0, "bottom_line": "(scoring failed)"})
    sc["failed"] = True
    sc["error"] = err
    return sc


def in_group(p, g):
    """Primary OR secondary membership of a subgroup."""
    sc = p.get("score") or {}
    return sc.get("subgroup") == g or g in (sc.get("secondary_subgroups") or [])


def score_paper(paper, client):
    """Triage one paper. Never raises and never turns a failure into 'score 0':
    on failure the result carries failed=True so the caller can queue a retry."""
    has_abs = bool(paper.get("abstract"))
    system = SCORING_SYSTEM + ("" if has_abs else SCORING_NO_ABSTRACT_NOTE)
    user = (
        f"Title: {paper['title']}\n"
        f"Journal: {paper['journal']} ({paper['pub_date']})\n"
        f"Publication types (may be incomplete for new papers): "
        f"{', '.join(paper['pub_types']) or 'n/a'}\n\n"
        f"Abstract:\n{paper['abstract'] if has_abs else '(no abstract available yet)'}\n\n"
        f"{SCORING_RUBRIC}"
    )
    obj, err = llm_json(client, LLM_MODEL, system, user, 900,
                        tag=f"triage failed for PMID {paper.get('pmid')}")
    if obj is None:
        return failed_score(err)
    sc = normalize_score(obj, paper)
    sc["provisional"] = not has_abs          # title-only verdict, re-done once the abstract exists
    return sc


SCORE_CACHE_FILE = "score_cache.json"

def score_cached(paper, client, cache):
    """(score, from_cache). A cached title-only verdict is replaced as soon as the
    abstract is available; changing LLM_MODEL or SCORING_VERSION invalidates the cache."""
    pmid, has_abs = paper["pmid"], bool(paper.get("abstract"))
    c = cache.get(pmid)
    if (c and c.get("v") == SCORING_VERSION and c.get("model") == LLM_MODEL
            and (c.get("has_abstract") or not has_abs)):
        return normalize_score(c["score"], paper), True
    sc = score_paper(paper, client)
    if not sc.get("failed"):
        cache[pmid] = {"v": SCORING_VERSION, "model": LLM_MODEL,
                       "has_abstract": has_abs, "score": sc}
    return sc, False

# ---------------------------------------------------------------------------
# 3b. Module 2 - structured summaries (ONLY for the selected papers)
# ---------------------------------------------------------------------------
# A stronger model is worth it here: it runs on a handful of papers, not hundreds.
# Set SUMMARY_MODEL to a Sonnet/Opus-class model for better summaries.
# One call produces BOTH editions: "short" (2-3 sentences) and "summary" (long).
SUMMARY_MODEL = os.environ.get("SUMMARY_MODEL", LLM_MODEL)

_SUMMARY_SYSTEM = {
"de": (
    "Du bist ein erfahrener Kardiologe und schreibst präzise, streng faktentreue "
    "deutschsprachige Zusammenfassungen neuer Studien für Fachkolleginnen und "
    "Fachkollegen. Schweizer Rechtschreibung (ss statt ß). "
    "\n\nREGEL 1 - KEINE HALLUZINATIONEN. Jede Tatsachenaussage (Zahlen, Endpunkte, "
    "Population, Ergebnisse) stammt AUSSCHLIESSLICH aus dem vorliegenden Abstract. Was "
    "nicht im Abstract steht, wird weder erfunden noch erwähnt. Erfinde nie Studiennamen, "
    "Effektgrössen, p-Werte oder Leitlinienempfehlungen. Fachbegriffe, Studiennamen, "
    "Abkürzungen und Zahlen werden unverändert übernommen (z. B. TAVI, TEER, MitraScore, "
    "'non-inferior'); englische Fachbegriffe ohne gebräuchliche deutsche Entsprechung "
    "bleiben englisch. "
    "\n\nREGEL 2 - KEINE KLINISCHEN RATSCHLÄGE. Du beschreibst, was eine Arbeit berichtet; "
    "du rätst niemandem, was zu tun ist. Das ist eine rechtliche Vorgabe, keine "
    "Stilfrage. Verboten sind Empfehlungen, Vorschläge und Handlungsaufforderungen, "
    "insbesondere: 'sollte', 'sollten', 'sollen', 'empfohlen', 'wir empfehlen', 'ist zu "
    "erwägen', 'ist indiziert', 'muss', 'müssen', 'spricht für den Einsatz', 'rechtfertigt "
    "die Anwendung' und jede Aufforderung an die Leserschaft. Schreibe stattdessen in der "
    "dritten Person und ordne jede Schlussfolgerung ihrer Quelle zu: 'Die Autoren "
    "schliessen, dass ...', 'Die Studie fand ...', 'Gemäss den Autoren ...'. "
    "\n\nREGEL 3 - KEINE EIGENEN BEWERTUNGEN. Bewerte weder Qualität noch Aussagekraft der "
    "Studie. Gib nur Einschränkungen wieder, die der Abstract selbst nennt, oder reine "
    "Designfakten ('beobachtend', 'ohne Kontrollgruppe'). Schreibe keine Sätze darüber, was "
    "im Abstract FEHLT (z. B. 'Der Abstract nennt keine Sicherheitsdaten'); solche Hinweise "
    "gehören ausschliesslich in 'qc_notes'."
),
"en": (
    "You are an experienced cardiologist writing concise, strictly factual English "
    "summaries of new studies for fellow specialists. "
    "\n\nRULE 1 - NO HALLUCINATIONS. Every factual statement (numbers, endpoints, "
    "population, results) must come EXCLUSIVELY from the abstract provided. If "
    "information is not in the abstract, do not invent it and do not state it. Never "
    "invent trial names, effect sizes, p-values or guideline recommendations. Keep the "
    "original terminology of the source; do not translate. "
    "\n\nRULE 2 - NO CLINICAL ADVICE. You describe what a paper reports; you never "
    "advise anyone what to do. This is a legal requirement, not a style preference. "
    "Never write recommendations, suggestions or calls to action. Specifically forbidden: "
    "'should', 'should be considered', 'recommend', 'is recommended', 'clinicians should', "
    "'consider using', 'warrants adoption', 'supports the use of', 'is indicated', 'must', "
    "'we advise', and any imperative addressed to the reader. Attribute every conclusion "
    "to its source instead: 'the authors conclude that ...', 'the trial reported ...', "
    "'the study found ...', 'according to the authors ...'. Describe findings and their "
    "stated implications in the third person, as reported. Do not tell the reader what "
    "to do with the information. "
    "\n\nRULE 3 - NO OWN JUDGEMENTS. Do not evaluate the quality or strength of the study. "
    "Report only limitations the abstract itself states, or plain design facts "
    "('observational', 'no control group'). Do not write sentences about what the abstract "
    "LACKS (e.g. 'the abstract reports no safety data'); such remarks belong exclusively "
    "in 'qc_notes'."
),
}

_KEYNUM_SCHEMA = """  "key_numbers": {
    "design": "keyword(s), at most 6 words, e.g. 'RCT, double-blind, multicentre' or 'Registry' (or null)",
    "n": "total participants as a string, e.g. '4321'  (or null)",
    "followup": "e.g. '12 months'  (or null)",
    "primary_endpoint": {
      "name": "name + timepoint, e.g. 'Target lesion failure, 12 mo'",
      "arm1_label": "short label, e.g. 'DCB'", "arm1_value": "e.g. '6.1%'",
      "arm2_label": "short label, e.g. 'DES'", "arm2_value": "e.g. '7.0%'",
      "effect": "e.g. 'Risk difference -0.9% (95% CI -2.1 to 0.3), non-inferior'  (or null)"
    },
    "secondary": [
      {"name": "e.g. 'Cardiac death'", "arm1": "1.6%", "arm2": "1.8%", "effect": "-0.2%"}
    ],
    "key_findings": [
      {"label": "e.g. 'Operative mortality'", "value": "e.g. '0.4% (n=2)'"}
    ]
  }"""

_KEYNUM_SCHEMA_DE = """  "key_numbers": {
    "design": "Stichwörter auf Deutsch, höchstens 6 Wörter, z. B. 'RCT, doppelblind, multizentrisch' oder 'Register, prospektiv' (oder null)",
    "n": "EINE einzelne Zahl als String, z. B. '4321' (oder null)",
    "followup": "z. B. '12 Monate' oder 'Median 3.2 Jahre'  (oder null)",
    "primary_endpoint": {
      "name": "Name + Zeitpunkt auf Deutsch, z. B. 'Versagen der Zielläsion, 12 Monate'",
      "arm1_label": "Kurzlabel, z. B. 'DCB'", "arm1_value": "z. B. '6.1%'",
      "arm2_label": "Kurzlabel, z. B. 'DES'", "arm2_value": "z. B. '7.0%'",
      "effect": "nur Masszahlen, z. B. 'Risikodifferenz -0.9% (95% CI -2.1 bis 0.3), nicht unterlegen'  (oder null)"
    },
    "secondary": [
      {"name": "z. B. 'Kardialer Tod'", "arm1": "1.6%", "arm2": "1.8%", "effect": "-0.2%"}
    ],
    "key_findings": [
      {"label": "z. B. 'Operative Mortalität'", "value": "z. B. '0.4% (n=2)'"}
    ]
  }"""

_SUMMARY_RUBRIC = {
"de": """Antworte mit genau einem JSON-Objekt und sonst nichts:
{
  "short": "Kurzfassung in 2-3 Sätzen, höchstens 60 Wörter, ausschliesslich aus dem Abstract:
              Design und Population (mit n, falls genannt), das zentrale Ergebnis mit der
              wichtigsten Zahl und zuletzt, was die Autoren daraus schliessen. Rein
              beschreibend, ohne Rat. Muss ohne den Rest der Zusammenfassung verständlich sein.",
  "headline": "EINE Zeile, höchstens 20 Wörter: das zentrale Ergebnis mit der wichtigsten
              Zahl, rein beschreibend (z. B. 'TAVI nach 5 Jahren nicht unterlegen: Tod/Schlaganfall
              20.1% vs 22.3%'). Kein Rat.",
  "design_tag": "Designbezeichnung plus Grösse für den Studienkopf, höchstens 10 Wörter, z. B.
              'Internationales Register · N = 5605', 'Systematische Übersicht & Meta-Analyse · 57
              Studien · N = 73588'. Nur Fakten aus dem Abstract.",
  "why_it_matters": "2 Sätze (35-60 Wörter): (1) was die Studie macht - Design, Population und
              Fragestellung, mit Studien- oder Registername; (2) der klinische Kontext aus dem
              Hintergrund des Abstracts: welche Lücke oder Unsicherheit sie adressiert.
              Beschreibend, ohne Lob, ohne Rat, ohne Qualitätsurteil.",
  "key_finding": "3-4 Sätze (60-110 Wörter) mit den Zahlen genau wie im Abstract: (1) Grösse und
              Aufbau der Population; (2) primäres Ergebnis mit beiden Gruppenwerten und
              Effektmass mit CI oder p-Wert; (3) wichtigstes sekundäres, Subgruppen- oder
              Validierungsergebnis mit Zahlen; (4) falls genannt, ein weiteres Kernergebnis. Nie
              nur ein Satz. Rein beschreibend; die Schlussfolgerung der Autoren wird hier NICHT
              wiederholt.",
  "tiles": [{"value": "kurze Zahl genau wie im Abstract, z. B. '55.1% vs. 67.7%' oder 'aHR
              2.01'", "label": "2-4 Wörter, z. B. '5-Jahres-Mortalität'", "context": "kurze
              Präzisierung, z. B. 'MitraScore <=3 vs. >=4' oder '95% CI 1.35-3.00'"}],
              GENAU 3 Kacheln mit den drei aussagekräftigsten Zahlen des Abstracts. Jede Zahl in
              value und context muss WÖRTLICH im Abstract stehen - nie rechnen, runden oder
              kombinieren. Weniger als 3 nur, wenn der Abstract weniger Zahlen enthält.
  "summary": "8-12 Sätze, ausschliesslich aus dem Abstract. Abdecken: Hintergrund/
              Fragestellung, Studiendesign, Population und Einschluss, Intervention und
              Vergleich, primärer Endpunkt mit Zahlen, wichtige sekundäre Endpunkte mit
              Zahlen, Sicherheit/unerwünschte Ereignisse (nur wenn im Abstract genannt),
              vom Abstract genannte Limitationen und die Schlussfolgerung der Autoren. Rein
              beschreibend - berichte, was die Arbeit sagt, nie, was die Leserschaft tun
              soll. Keine Sätze darüber, was im Abstract fehlt.",
  "relation_evidence": "1-3 Sätze, die die Studie beschreibend in die bestehende Evidenz
              einordnen. NUR allgemein gesichertes Wissen. Erfinde keine konkreten Studien,
              Zahlen oder Ergebnisse. Kein Rat. Ist keine sichere Einordnung möglich,
              schreibe genau: 'Aus dem Abstract allein nicht einzuordnen.'",
  "relation_guidelines": "1-3 Sätze, die BESCHREIBEN, wie die Befunde zu aktuellen Leitlinien
              (z. B. ESC/ACC/AHA) stehen - etwa dass sie mit deren Aussagen vereinbar sind,
              sie in Frage stellen oder verfeinern. Erfinde NIE eine konkrete Empfehlungs-
              klasse oder ein Evidenzlevel und fordere NIE auf, etwas zu befolgen,
              zu übernehmen oder zu ändern. Bei Unsicherheit schreibe genau:
              'Kein klarer Leitlinienbezug ableitbar.'",
  "authors_conclusion": "1-2 Sätze, was DIE AUTOREN schliessen, ausdrücklich ihnen zugeordnet
              (beginne mit 'Die Autoren schliessen, dass ...' o. ä.). Rein beschreibend.
              Kein Rat, keine Empfehlung, kein Imperativ.",
""" + _KEYNUM_SCHEMA_DE + """,
  "qc_notes": "Interne Hinweise für die Redaktion (erscheinen NICHT im Dokument), z. B.
              fehlende Angaben im Abstract, unklare Zahlen. Leer lassen, wenn nichts auffällt."
}
WICHTIG zu key_numbers:
- Nimm nur Werte, die WÖRTLICH im Abstract stehen. Fehlt ein Wert, setze null. Erfinde nie
  Zahlen, Endpunkte, Gruppen oder Konfidenzintervalle. Höchstens 5 Zeilen unter 'secondary'.
- Nennt der Abstract für den primären Endpunkt Werte für zwei Gruppen, MUSS
  'primary_endpoint' vollständig befüllt sein (name, arm1/arm2 mit Label und Wert, effect).
- Jede Endpunkt-Zahl, die du in 'summary' nennst, gehört - sofern sie in eine der Strukturen
  passt - auch in key_numbers (primary_endpoint, secondary oder key_findings).
- Gibt es nur EIN Kollektiv (gepoolte Metaanalyse, Einzelarm, Register): arm2_label und
  arm2_value = null und den Gesamtwert in arm1 eintragen. Keine leeren Strings und keine
  Platzhalter wie '-', 'n/a' oder 'nicht angegeben' - nutze immer null.
- 'design': höchstens 6 Wörter, Stichwort statt Satz ('Register', 'RCT, doppelblind,
  multizentrisch', 'Narrative Übersichtsarbeit').
- SPRACHE: Alle Textfelder von key_numbers (design, followup, Endpunkt-Namen, Gruppen- und
  Zeilenbezeichnungen, effect, Labels) stehen auf DEUTSCH, z. B. 'Gesamtmortalität, 30 Tage',
  'Median 24 Monate', 'Gruppe ohne Operationsindikation', 'Systematische Übersicht,
  Meta-Analyse'. Unverändert bleiben nur Studien- und Registernamen, etablierte Abkürzungen
  (TAVI, HR, CI, AUC, ERO, SMD) und Zahlen. Keine englischen Wörter wie 'months', 'Group',
  'All-cause', 'Significantly', 'Pooled'.
- 'n': EINE Zahl (Gesamtzahl der ausgewerteten Teilnehmenden bzw. Patienten; bei mehreren
  Kohorten die Hauptkohorte), ohne Text und ohne Klammern.
- 'primary_endpoint' ist der im Abstract genannte primäre Endpunkt bzw. das Hauptergebnis -
  KEIN Baseline-Merkmal. Bei deskriptiven Studien ohne definierten Endpunkt das zentrale
  Ergebnis, auf das sich die Schlussfolgerung der Autoren stützt; gibt es kein Ergebnis mit
  Zahl: null.
- 'effect' nur, wenn der Abstract eine Masszahl nennt (HR, OR, RR, Differenz, SMD, p-Wert,
  Konfidenzintervall); nie ein Satz in Worten wie 'signifikant niedriger'. Nennt der Abstract
  nur die Gruppenwerte, trage diese in arm1/arm2 ein und setze effect = null.
- 'effect' nur, wenn der Abstract ihn nennt, sonst null.
- key_findings ist PFLICHT: 3-6 Zeilen, sobald der Abstract irgendwelche quantitativen oder
  konkreten Tatsachen enthält (Raten, Zahlen, Gradienten, Dauern, Prävalenzen, Schwellen,
  Umfang). Jede Zeile: kurzes Label plus Wert genau wie im Abstract. Für Dokumente ganz ohne
  Zahlen (z. B. Technik-Konsensus) konkrete Sachangaben verwenden, z. B.
  {"label": "Adressierte Komplikation", "value": "LVOT-Obstruktion nach TMVR"}.
  Leer lassen nur, wenn der Abstract gar nichts Konkretes enthält.

Schreibe sachlich, klinisch, beschreibend. Keine Werbesprache, kein Rat.
""",
"en": """Return a single JSON object and nothing else:
{
  "short": "Short edition in 2-3 sentences, at most 60 words, based EXCLUSIVELY on the
              abstract: design and population (with n if stated), the central result with
              its most important number, and finally what the authors conclude. Purely
              descriptive, no advice. Must be understandable without the rest of the summary.",
  "headline": "ONE line, at most 20 words: the central result with its most important
              number, purely descriptive (e.g. 'TAVR non-inferior at 5 years: death/stroke 20.1%
              vs 22.3%'). No advice.",
  "design_tag": "Design label plus size for the study header, at most 10 words, e.g.
              'International registry · N = 5,605', 'Systematic review & meta-analysis · 57
              studies · N = 73,588', 'Multicenter AI'. Only facts from the abstract.",
  "why_it_matters": "2 sentences (35-60 words): (1) what the study does - design, population
              and the question it answers, naming the trial or registry; (2) the clinical
              context from the abstract's background: which gap or uncertainty it addresses.
              Descriptive; no praise, no advice, no judgement of quality.",
  "key_finding": "3-4 sentences (60-110 words), dense with the numbers exactly as in the
              abstract: (1) population size and structure (cohorts, groups); (2) the primary
              result with both group values and the effect measure with its CI or p-value;
              (3) the most important secondary, subgroup or validation result with numbers;
              (4) if stated, a further key result. Never only a single sentence. Purely
              descriptive; the authors' conclusion is NOT repeated here.",
  "tiles": [{"value": "short figure exactly as in the abstract, e.g. '55.1% vs. 67.7%' or
              'aHR 2.01'", "label": "2-4 words, e.g. '5-year mortality'", "context": "short
              qualifier, e.g. 'MitraScore <=3 vs. >=4' or '95% CI 1.35-3.00'"}],
              EXACTLY 3 tiles with the three most informative figures of the abstract. Every
              number in value and context must appear VERBATIM in the abstract - never compute,
              round or combine numbers. Fewer than 3 only if the abstract has fewer figures.
  "summary": "8-12 sentences, based EXCLUSIVELY on the abstract. Cover: background/
              research question, study design, population and inclusion, intervention
              and comparator, primary endpoint with numbers, key secondary endpoints
              with numbers, safety/adverse events (only if stated), limitations stated in
              the abstract, and what the authors conclude. Purely descriptive - report
              what the paper says, never what the reader should do. No sentences about
              what the abstract lacks.",
  "relation_evidence": "1-3 sentences placing the study in the context of existing
              evidence, descriptively. ONLY broadly established knowledge. Do NOT invent
              specific trials, numbers or results. No advice. If no confident placement
              is possible, write exactly: 'Cannot be placed in context from the abstract
              alone.'",
  "relation_guidelines": "1-3 sentences DESCRIBING how the findings relate to current
              guidelines (e.g. ESC/ACC/AHA) - for example that they are consistent with,
              challenge, or refine what current guidelines state. NEVER invent a specific
              class of recommendation or level of evidence, and NEVER tell the reader to
              follow, adopt or change anything. If uncertain, write exactly: 'No clear
              guideline link can be derived.'",
  "authors_conclusion": "1-2 sentences stating what THE AUTHORS conclude, explicitly
              attributed to them (start with 'The authors conclude that ...' or similar).
              Strictly descriptive. No advice, no recommendation, no imperative.",
""" + _KEYNUM_SCHEMA + """,
  "qc_notes": "Internal notes for the editor (NOT shown in the document), e.g. information
              missing from the abstract, unclear numbers. Leave empty if nothing stands out."
}
IMPORTANT for key_numbers:
- Only take values stated VERBATIM in the abstract. If a value is missing, set it to null.
  Never invent numbers, endpoints, arms or confidence intervals. Max 5 secondary rows.
- If the abstract gives values for two groups for the primary endpoint, 'primary_endpoint'
  MUST be filled completely (name, arm1/arm2 with label and value, effect).
- Every endpoint number you cite in 'summary' must also appear in key_numbers
  (primary_endpoint, secondary or key_findings) whenever it fits one of those structures.
- With only ONE cohort (pooled meta-analysis, single arm, registry): arm2_label and
  arm2_value = null and put the overall value in arm1. No empty strings and no
  placeholders such as '-', 'n/a' or 'not stated' - always use null.
- 'design': at most 6 words, a keyword rather than a sentence.
- 'n': ONE number (total participants analysed; with several cohorts the main cohort),
  no text and no brackets.
- 'primary_endpoint' is the primary endpoint or main result named in the abstract - NOT a
  baseline characteristic. For descriptive studies without a defined endpoint use the
  central result the authors' conclusion rests on; if there is none with a number: null.
- 'effect' only if the abstract states a measure (HR, OR, RR, difference, SMD, p-value,
  confidence interval); never a sentence in words such as 'significantly lower'. If the
  abstract gives only the group values, fill arm1/arm2 and set effect = null.
- 'effect' only if the abstract states it, otherwise null.
- key_findings is REQUIRED: 3-6 rows whenever the abstract contains any quantitative or
  concrete factual statements (rates, counts, gradients, durations, prevalences, thresholds,
  scope items). Each row is a short label plus the value exactly as stated in the abstract.
  For a document with no numbers at all (e.g. a technique consensus) use concrete factual
  descriptors, such as {"label": "Complication addressed", "value": "LVOT obstruction after
  TMVR"}. Leave it empty only if the abstract contains nothing concrete.

Write in a factual, clinical, descriptive register. No marketing language, no advice.

STYLE EXAMPLES for why_it_matters / key_finding (from the approved CLARITY short edition -
match this length and density, never copy their content):
  why_it_matters: "The 5-year MITRA-PRO follow-up quantifies the association between residual
    mitral regurgitation after M-TEER and long-term mortality, including how that association
    changes over time. Earlier analyses of the registry covered only 1- and 3-year outcomes."
  key_finding: "Five-year mortality was 55.1% with MitraScore <=3 versus 67.7% with MitraScore
    >=4. In degenerative MR, the corresponding mortality rates were 53.1% and 73.2%; in
    functional MR, they were 55.0% and 64.6%. Residual MR became prognostic after 30 days and
    remained associated with excess long-term mortality."
  key_finding: "The registry enrolled 2,171 patients; 2,140 entered the final analysis and 254
    (11.9%) died within 30 days. The model achieved an AUROC of 0.763 with good calibration. In
    the external cohort of 377 patients, 48 (12.7%) died within 30 days and the AUROC was 0.799
    (95% CI 0.730-0.868)."
""",
}


_PLACEHOLDER_WORDS = {"", "none", "null", "n/a", "na", "nan", "-", "—", "–",
                      "unknown", "unbekannt", "k. a", "k.a", "tbd"}
_PLACEHOLDER_RE = re.compile(
    r"^\(?\s*(?:not|nicht|no|kein\w*)\b.{0,40}(?:stated|reported|given|available|"
    r"angegeben|berichtet|genannt|verfügbar|vorhanden)\b", re.IGNORECASE)


def _clean_val(v):
    """None for anything that is really 'nothing' (empty, 'n/a', 'not stated ...')."""
    if v is None:
        return None
    if isinstance(v, float) and v == int(v):
        v = int(v)
    s = str(v).strip()
    if s.lower().rstrip(".") in _PLACEHOLDER_WORDS or _PLACEHOLDER_RE.match(s):
        return None
    return s


def _short_design(s):
    """A design is a keyword, not a sentence: cut at the first bracket / semicolon."""
    s = _clean_val(s)
    if not s or len(s) <= 48:
        return s
    cut = re.split(r"\s\(|;|:", s, maxsplit=1)[0].strip()
    if 3 <= len(cut) <= 60:
        return cut
    return s[:48].rsplit(" ", 1)[0].rstrip(",;") + "..."


def clean_key_numbers(kn):
    """Normalise the model's key_numbers: placeholders to None, single-arm studies get
    one value (no empty 'Arm 2' column later), empty rows removed, design shortened."""
    kn = kn if isinstance(kn, dict) else {}
    out = {"design": _short_design(kn.get("design")),
           "n": _clean_val(kn.get("n")), "followup": _clean_val(kn.get("followup"))}
    pe = kn.get("primary_endpoint")
    if isinstance(pe, dict):
        pe = {k: _clean_val(pe.get(k)) for k in
              ("name", "arm1_label", "arm1_value", "arm2_label", "arm2_value", "effect")}
        if pe["arm1_value"] is None and pe["arm2_value"] is not None:     # lone value -> arm1
            pe["arm1_label"], pe["arm1_value"] = pe["arm2_label"], pe["arm2_value"]
            pe["arm2_label"] = pe["arm2_value"] = None
        if pe["arm2_value"] is None:
            pe["arm2_label"] = None
        if not pe["name"] or (pe["arm1_value"] is None and pe["effect"] is None):
            pe = None
    else:
        pe = None
    out["primary_endpoint"] = pe
    sec = []
    for r in (kn.get("secondary") or []):
        if not isinstance(r, dict):
            continue
        row = {k: _clean_val(r.get(k)) for k in ("name", "arm1", "arm2", "effect")}
        if row["name"] and (row["arm1"] or row["arm2"] or row["effect"]):
            sec.append(row)
    out["secondary"] = sec[:5]
    kf = []
    for r in (kn.get("key_findings") or []):
        if not isinstance(r, dict):
            continue
        row = {"label": _clean_val(r.get("label")), "value": _clean_val(r.get("value"))}
        if row["label"] and row["value"]:
            kf.append(row)
    out["key_findings"] = kf[:6]
    return out


def _first_sentences(text, n=2):
    parts = re.split(r"(?<=[.!?])\s+(?=[A-ZÄÖÜ])", (text or "").strip())
    return " ".join(parts[:n]).strip()


def _empty_summary():
    return {"short": "", "headline": "", "design_tag": "", "why_it_matters": "", "key_finding": "",
            "tiles": [], "summary": "", "relation_evidence": "", "relation_guidelines": "",
            "authors_conclusion": "", "key_numbers": {}, "qc_notes": ""}


def finalize_summary(obj, lang):
    """Fill gaps, clean numbers, stamp the cache metadata."""
    s = _empty_summary()
    for k in ("short", "headline", "design_tag", "why_it_matters", "key_finding", "summary",
              "relation_evidence", "relation_guidelines", "authors_conclusion", "qc_notes"):
        v = obj.get(k)
        s[k] = v.strip() if isinstance(v, str) else ""
    tiles = []
    for t in (obj.get("tiles") or []) if isinstance(obj.get("tiles"), list) else []:
        if isinstance(t, dict):
            row = {k: _clean_val(t.get(k)) for k in ("value", "label", "context")}
            if row["value"] and row["label"]:
                tiles.append(row)
    s["tiles"] = tiles[:3]
    s["key_numbers"] = clean_key_numbers(obj.get("key_numbers"))
    notes = [s["qc_notes"]] if s["qc_notes"] else []
    if not s["short"] and s["summary"]:
        s["short"] = _first_sentences(s["summary"], 2)
        notes.append("short edition built from the first sentences of the summary (model gave none)")
    s["qc_notes"] = " | ".join(notes)
    s["_meta"] = {"lang": lang, "v": SUMMARY_VERSION, "model": SUMMARY_MODEL}
    return s


def summary_cache_ok(entry, lang):
    """A cached summary is reusable only if it is complete, in the right language and
    made by the current prompt version and model (so old English v1 entries are redone)."""
    m = (entry or {}).get("_meta") or {}
    return bool(entry and (entry.get("summary") or "").strip() and not entry.get("failed")
                and m.get("lang") == lang and m.get("v") == SUMMARY_VERSION
                and m.get("model") == SUMMARY_MODEL)


def summarize_paper(paper, client, lang=None):
    lang = lang or OUTPUT_LANG
    if not paper.get("abstract"):
        s = _empty_summary()
        s["failed"] = True
        s["error"] = "no abstract"
        return s
    user = (
        f"Title: {paper['title']}\n"
        f"Journal: {paper['journal']} ({paper['pub_date']})\n\n"
        f"Abstract:\n{paper['abstract']}\n\n"
        f"{_SUMMARY_RUBRIC[lang]}"
    )
    obj, err = llm_json(client, SUMMARY_MODEL, _SUMMARY_SYSTEM[lang], user, 4500,
                        tag=f"Module 2 failed for: {paper['title'][:50]}")
    if obj is None:
        if SUMMARY_MODEL != LLM_MODEL:
            print(f"  !! model requested: {SUMMARY_MODEL!r} - if this name is wrong, unset "
                  f"SUMMARY_MODEL to fall back to {LLM_MODEL!r}")
        s = _empty_summary()
        s["failed"] = True
        s["error"] = err
        return s
    s = finalize_summary(obj, lang)
    short_fields = _too_short(s)
    if short_fields:                                       # one expansion attempt
        fix = (user + "\n\nYour previous answer was too short in: " + ", ".join(short_fields) +
               ". why_it_matters needs 2 sentences, key_finding 3-4 sentences with the numbers from "
               "the abstract. Return the complete JSON again.")
        obj2, _ = llm_json(client, SUMMARY_MODEL, _SUMMARY_SYSTEM[lang], fix, 4500)
        if obj2 is not None:
            s2 = finalize_summary(obj2, lang)
            if len(_too_short(s2)) < len(short_fields):
                s = s2
        left = _too_short(s)
        if left:
            s["qc_notes"] = (s["qc_notes"] + " | short edition text shorter than the standard: "
                             + ", ".join(left)).strip(" |")
    return s


def _too_short(s):
    """Fields of the short edition below the agreed length (why: 2 sentences, key: 3)."""
    out = []
    if len(split_sentences(s.get("why_it_matters") or "")) < 2:
        out.append("why_it_matters")
    if len(split_sentences(s.get("key_finding") or "")) < 3:
        out.append("key_finding")
    return out


# ---------------------------------------------------------------------------
# 3c. Structural reference base (grounding for the two "relation" fields)
# ---------------------------------------------------------------------------
# Cyril's curated list: per topic, ESC guideline -> consensus -> landmark trials,
# each with its PMID. --build-refs fetches every abstract once and extracts a
# one-sentence key finding, cached to STATE_DIR so it is never re-paid. The
# enriched base is what later grounds "Relation to existing evidence / guidelines".

def _R(pmid, year, title):
    return {"pmid": str(pmid), "year": year, "title": title}

# shared references (cited under several topics - defined once)
_ESC_VHD = _R(40878295, 2025, "2025 ESC/EACTS Guidelines for the management of valvular heart disease")
_EAPCI_CORE = _R(39207816, 2024, "Percutaneous Valvular and Structural Heart Disease Interventions: 2024 EAPCI Core Curriculum")
_VARC3 = _R(33871579, 2021, "Valve Academic Research Consortium 3: Updated Endpoint Definitions for Aortic Valve Clinical Research")
_EAPCI_SVD = _R(29020344, 2017, "Standardized definitions of structural deterioration and valve failure in transcatheter and surgical aortic bioprosthetic valves: EAPCI consensus statement")
_MMI_STRUCT = _R(37283275, 2023, "Multimodality imaging for patient selection, procedural guidance and follow-up of transcatheter interventions for structural heart disease - Part 1")
_MMI_REGURG = _R(35292799, 2022, "Multi-modality imaging assessment of native valvular regurgitation: EACVI/ESC Council of Valvular Heart Disease position paper")
_TMV_DESIGN = _R(26184622, 2015, "Clinical trial design principles and endpoint definitions for transcatheter mitral valve repair and replacement - Part 1")
_TMV_ENDPT = _R(26184623, 2015, "Clinical trial design principles and endpoint definitions for transcatheter mitral valve repair and replacement - Part 2: Endpoint definitions")

_EMBEDDED_STRUCTURAL_REFERENCES = {
    "topic_order": [
        "TAVR - low risk", "TAVR - intermediate/high risk",
        "TEER - secondary (functional) MR", "TEER - primary (degenerative) MR",
        "Transcatheter mitral valve replacement (TMVR)",
        "LAAO / left atrial appendage occlusion",
        "Transcatheter tricuspid valve (TEER/replacement)",
        "PFO / ASD closure", "Paravalvular leak", "Infective endocarditis",
        "Multivalvular disease", "Other",
    ],
    "topics": {
        "TAVR - low risk": {
            "esc_guideline": [_ESC_VHD],
            "consensus": [_EAPCI_CORE, _VARC3, _EAPCI_SVD, _MMI_STRUCT],
            "landmark_trials": [
                _R(37874020, 2023, "Transcatheter Aortic-Valve Replacement in Low-Risk Patients at Five Years (PARTNER 3)"),
                _R(41144631, 2025, "Transcatheter or Surgical Aortic-Valve Replacement in Low-Risk Patients at 7 Years (PARTNER 3)"),
                _R(40158212, 2025, "5-Year Outcomes After TAVR or SAVR in Low-Risk Patients With Aortic Stenosis (Evolut Low Risk)"),
                _R(38588025, 2024, "Transcatheter or Surgical Treatment of Aortic-Valve Stenosis (DEDICATE-DZHK6)"),
                _R(38747246, 2024, "TAVI in low-risk tricuspid or bicuspid aortic stenosis: the NOTION-2 trial"),
                _R(40884768, 2025, "Three-Year Follow-Up of the NOTION-2 Trial: TAVR Versus SAVR in Younger Low-Risk Patients"),
                _R(38321820, 2024, "Transcatheter or surgical aortic valve implantation: 10-year outcomes of the NOTION trial"),
                _R(40171878, 2025, "Transcatheter vs. surgical aortic valve replacement in women: the RHEIA trial"),
            ],
        },
        "TAVR - intermediate/high risk": {
            "esc_guideline": [_ESC_VHD],
            "consensus": [_EAPCI_CORE, _VARC3, _EAPCI_SVD, _MMI_STRUCT],
            "landmark_trials": [
                _R(36001335, 2022, "5-Year Outcomes of the SURTAVI Randomized Clinical Trial"),
                _R(35579641, 2022, "TAVI vs SAVR on All-Cause Mortality in Patients Aged 70+ With Severe Aortic Stenosis: The UK TAVI Trial"),
                _R(38588025, 2024, "Transcatheter or Surgical Treatment of Aortic-Valve Stenosis (DEDICATE-DZHK6)"),
            ],
        },
        "TEER - secondary (functional) MR": {
            "esc_guideline": [_ESC_VHD],
            "additional_guidelines": [
                _R(34447992, 2021, "2021 ESC Guidelines for the diagnosis and treatment of acute and chronic heart failure"),
                _R(37622666, 2023, "2023 Focused Update of the 2021 ESC Guidelines for acute and chronic heart failure"),
            ],
            "consensus": [
                _R(33734354, 2021, "Management of secondary mitral regurgitation in heart failure: HFA/EACVI/EHRA/EAPCI joint position statement"),
                _MMI_REGURG, _MMI_STRUCT, _TMV_DESIGN, _TMV_ENDPT,
            ],
            "landmark_trials": [
                _R(36876756, 2023, "Five-Year Follow-up after Transcatheter Repair of Secondary Mitral Regurgitation (COAPT)"),
                _R(39216092, 2024, "Transcatheter Valve Repair in Heart Failure with Moderate to Severe Mitral Regurgitation (RESHAPE-HF2)"),
                _R(39216093, 2024, "Transcatheter Repair versus Mitral-Valve Surgery for Secondary Mitral Regurgitation (MATTERHORN)"),
            ],
        },
        "TEER - primary (degenerative) MR": {
            "esc_guideline": [_ESC_VHD],
            "consensus": [_MMI_REGURG, _MMI_STRUCT, _TMV_DESIGN, _TMV_ENDPT],
            "landmark_trials": [
                _R(36121247, 2022, "Randomized Comparison of TEER for Degenerative MR in Prohibitive Surgical Risk Patients (CLASP IID)"),
                _R(37962288, 2023, "One-Year Outcomes from the CLASP IID Randomized Trial for Degenerative MR"),
                _R(41093456, 2025, "CLASP IID Trial and Registry: 2-Year Outcomes of TEER for Degenerative MR"),
            ],
        },
        "Transcatheter mitral valve replacement (TMVR)": {
            "esc_guideline": [_ESC_VHD],
            "consensus": [_EAPCI_CORE, _MMI_STRUCT, _TMV_DESIGN, _TMV_ENDPT,
                          _R(26481845, 2015, "Multimodality Imaging in Transcatheter Mitral Valve Replacement")],
            "landmark_trials": [
                _R(34736561, 2021, "2-Year Outcomes of TMVR in Patients With Severe Symptomatic MR (Tendyne)"),
                _R(35338542, 2022, "Clinical outcomes of TMVR: 1-year results from the CHOICE-MI registry"),
                _R(37235388, 2023, "Clinical outcomes of TMVR: 2-year results of the CHOICE-MI registry"),
                _R(37758379, 2023, "Five-Year Evaluation of Mitral Valve-in-Valve, Valve-in-Ring, and Valve-in-MAC: MITRAL Trial"),
                _R(39243268, 2024, "5-Year Outcomes of Anterior Mitral Leaflet Laceration to Prevent Outflow Obstruction (LAMPOON) During TMVR"),
                _R(41194751, 2025, "TMVR for Severe Mitral Annular Calcification: Primary Outcomes From SUMMIT-MAC"),
            ],
        },
        "LAAO / left atrial appendage occlusion": {
            "esc_guideline": [_ESC_VHD],
            "additional_guidelines": [_R(39210723, 2024, "2024 ESC Guidelines for the management of atrial fibrillation")],
            "consensus": [
                _R(31504441, 2020, "EHRA/EAPCI expert consensus statement on catheter-based left atrial appendage occlusion - an update"),
                _R(38291925, 2024, "Practical guide on left atrial appendage closure for the non-implanting physician: an international consensus paper"),
                _R(39812172, 2025, "The role of multi-modality imaging for the assessment of left atrium and left atrial appendage: EACVI clinical consensus statement"),
            ],
            "landmark_trials": [
                _R(34459659, 2021, "Amplatzer Amulet LAA Occluder Versus Watchman Device for Stroke Prophylaxis (Amulet IDE)"),
                _R(34748929, 2022, "4-Year Outcomes After LAA Closure Versus Nonwarfarin Oral Anticoagulation for AF (PRAGUE-17)"),
                _R(34747186, 2022, "Amulet or Watchman Device for Percutaneous LAA Closure: SWISS-APERO Randomized Trial"),
                _R(39555822, 2025, "Left Atrial Appendage Closure after Ablation for Atrial Fibrillation (OPTION)"),
                _R(41910347, 2026, "Left Atrial Appendage Closure or Anticoagulation for Atrial Fibrillation (CHAMPION-AF)"),
                _R(41849741, 2026, "Left Atrial Appendage Closure or Medical Therapy in Atrial Fibrillation (CLOSURE-AF)"),
            ],
        },
        "Transcatheter tricuspid valve (TEER/replacement)": {
            "esc_guideline": [_ESC_VHD],
            "additional_guidelines": [_R(34455430, 2021, "2021 ESC Guidelines on cardiac pacing and cardiac resynchronization therapy")],
            "consensus": [
                _R(38131233, 2024, "Chronic right-sided heart failure and tricuspid regurgitation: HFA/EAPCI clinical consensus statement"),
                _R(40163675, 2025, "Management of patients with transvalvular RV leads undergoing transcatheter tricuspid valve interventions: EHRA/EAPCI scientific statement"),
                _R(37793121, 2023, "Tricuspid Valve Academic Research Consortium Definitions for Tricuspid Regurgitation and Trial Endpoints"),
                _MMI_REGURG,
            ],
            "landmark_trials": [
                _R(36876753, 2023, "Transcatheter Repair for Patients with Tricuspid Regurgitation (TRILUMINATE Pivotal)"),
                _R(39471883, 2024, "One-Year Outcomes From the TRILUMINATE Randomized Cohort"),
                _R(40159089, 2025, "Two-Year Outcomes From the TRILUMINATE Pivotal Trial"),
                _R(39602173, 2024, "TEER for Severe Isolated Tricuspid Regurgitation: The Tri.Fr Randomized Clinical Trial"),
                _R(39475399, 2024, "Transcatheter Valve Replacement in Severe Tricuspid Regurgitation (TRISCEND II)"),
                _R(39480380, 2024, "One-Year Outcomes After Transcatheter Tricuspid Valve Replacement (TRISCEND II)"),
            ],
        },
        "PFO / ASD closure": {
            "esc_guideline": [_ESC_VHD],
            "additional_guidelines": [
                _R(32860028, 2020, "2020 ESC Guidelines for the management of adult congenital heart disease"),
                _R(38752755, 2024, "European Stroke Organisation Guidelines on the diagnosis and management of patent foramen ovale after stroke"),
            ],
            "consensus": [
                _R(30141306, 2019, "European position paper on the management of patients with patent foramen ovale: Part I"),
                _R(33507260, 2021, "European position paper on the management of patients with patent foramen ovale - Part II"),
                _EAPCI_CORE,
            ],
            "landmark_trials": [
                _R(28902593, 2017, "Patent Foramen Ovale Closure or Anticoagulation versus Antiplatelets after Stroke (CLOSE)"),
                _R(28902580, 2017, "Patent Foramen Ovale Closure or Antiplatelet Therapy for Cryptogenic Stroke (REDUCE)"),
                _R(28902590, 2017, "Long-Term Outcomes of Patent Foramen Ovale Closure or Medical Therapy after Stroke (RESPECT)"),
            ],
            "notes": "ASD: no new landmark RCT primary publication in the period 31.07.2021-31.07.2026.",
        },
        "Paravalvular leak": {
            "esc_guideline": [_ESC_VHD],
            "additional_guidelines": [_R(37622656, 2023, "2023 ESC Guidelines for the management of endocarditis")],
            "consensus": [_EAPCI_CORE, _MMI_STRUCT, _VARC3],
            "landmark_trials": [
                _R(38156599, 2024, "Transcatheter Closure or Surgery for Symptomatic Paravalvular Leaks: The Multicenter KISS Registry"),
                _R(36256693, 2022, "Predictors of Clinical Success After Transcatheter Paravalvular Leak Closure: An International Prospective Registry"),
                _R(37128805, 2023, "Percutaneous Paravalvular Leak Closure After TAVI: The International PLUGinTAVI Registry"),
            ],
        },
        "Infective endocarditis": {
            "esc_guideline": [_R(37622656, 2023, "2023 ESC Guidelines for the management of endocarditis")],
            "additional_guidelines": [_ESC_VHD],
            "notes": "The dedicated guideline for this topic is the 2023 ESC Guidelines for the "
                     "management of endocarditis. If no excerpt of it appears above, its content "
                     "is NOT available: do not describe or guess it, and state in "
                     "relation_guidelines only that this guideline exists for the topic.",
        },
        "Multivalvular disease": {
            "esc_guideline": [_ESC_VHD],
            "consensus": [
                _R(39874243, 2025, "The role of multi-modality imaging in multiple valvular heart disease: EACVI expert consensus document"),
                _MMI_REGURG, _EAPCI_CORE,
            ],
            "notes": "No standalone landmark RCT primary publication in the period 31.07.2021-31.07.2026.",
        },
        "Other": {
            "esc_guideline": [_ESC_VHD],
            "notes": "No entries yet.",
        },
    },
}

_REF_TIERS = ("esc_guideline", "additional_guidelines", "consensus", "landmark_trials")

def merge_new_topics(base):
    """Topics that a newer script version knows but an older reference file lacks are
    added (existing entries are never touched, so manual edits survive)."""
    import copy
    base = base if isinstance(base, dict) else {}
    topics = base.setdefault("topics", {})
    order = base.setdefault("topic_order", [])
    for t, tiers in _EMBEDDED_STRUCTURAL_REFERENCES["topics"].items():
        if t not in topics:
            topics[t] = copy.deepcopy(tiers)
        if t not in order:
            order.insert(order.index("Other") if "Other" in order else len(order), t)
    return base


def load_structural_references():
    """Prefer an editable JSON in STATE_DIR (so the base can be maintained without
    touching code); fall back to the embedded copy and write it out once."""
    path = _sp("structural_references.json")
    try:
        with open(path) as f:
            return merge_new_topics(json.load(f))
    except FileNotFoundError:
        try:
            with open(path, "w") as f:
                json.dump(_EMBEDDED_STRUCTURAL_REFERENCES, f, indent=2, ensure_ascii=False)
        except Exception:
            pass
        return _EMBEDDED_STRUCTURAL_REFERENCES

REF_KEYFINDING_SYSTEM = (
    "You extract ONE factual English sentence from the provided abstract. For a trial: "
    "state the population, the comparison, and the main result (with the key number if "
    "stated). For a guideline / consensus / definitions document: state in one line what "
    "it covers. Strictly from the abstract - no invented numbers, no advice, no "
    "recommendations, no imperative. Output only the sentence, nothing else."
)

def extract_key_finding(title, abstract, client):
    if not abstract:
        return ""
    msg = client.messages.create(
        model=LLM_MODEL, max_tokens=200, system=REF_KEYFINDING_SYSTEM,
        messages=[{"role": "user", "content":
                   f"Title: {title}\n\nAbstract:\n{abstract}\n\nOne-sentence key finding:"}],
    )
    return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text").strip()

def build_reference_base(client, refresh=False):
    """Fetch every referenced abstract once, extract a one-line key finding, cache to
    STATE_DIR/reference_base.json, and write a human-readable review file."""
    refs = load_structural_references()
    cache_path = _sp("reference_base.json")
    findings = {}
    if not refresh:
        try:
            with open(cache_path) as f:
                findings = json.load(f).get("key_findings", {})
        except FileNotFoundError:
            pass

    pmid_title = {}
    for topic, tiers in refs["topics"].items():
        for tier in _REF_TIERS:
            for e in tiers.get(tier, []):
                pmid_title[e["pmid"]] = e["title"]

    todo = [p for p in pmid_title if p not in findings]
    print(f"Reference base: {len(pmid_title)} unique references · "
          f"{len(findings)} cached · {len(todo)} to fetch")
    if todo:
        papers = efetch_details(todo)
        got = enrich_missing_abstracts(papers)
        if got:
            print(f"  recovered {got} abstracts via fallback sources")
        by_pmid = {p["pmid"]: p for p in papers}
        for i, pid in enumerate(todo, 1):
            p = by_pmid.get(pid)
            abstract = (p or {}).get("abstract", "")
            kf = extract_key_finding(pmid_title[pid], abstract, client) if abstract else ""
            findings[pid] = {"title": pmid_title[pid], "key_finding": kf,
                             "has_abstract": bool(abstract)}
            print(f"  [{i}/{len(todo)}] {'ok    ' if kf else 'NO-ABS'} {pmid_title[pid][:52]}")

    out = {"topic_order": refs["topic_order"], "topics": refs["topics"],
           "key_findings": findings}
    with open(cache_path, "w") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)

    # human-readable review file
    review_path = _sp("reference_base_review.md")
    lines = ["# Structural reference base - key findings for review", ""]
    missing = 0
    for topic in refs["topic_order"]:
        tiers = refs["topics"].get(topic, {})
        lines.append(f"## {topic}")
        tier_names = {"esc_guideline": "ESC guideline",
                      "additional_guidelines": "Additional guidelines",
                      "consensus": "Consensus / position / definitions",
                      "landmark_trials": "Landmark trials"}
        for tier in _REF_TIERS:
            entries = tiers.get(tier, [])
            if not entries:
                continue
            lines.append(f"\n**{tier_names[tier]}**\n")
            for e in entries:
                fi = findings.get(e["pmid"], {})
                kf = fi.get("key_finding") or "_(no abstract available yet - will retry)_"
                if not fi.get("key_finding"):
                    missing += 1
                lines.append(f"- **{e['title']}** ({e['year']}, PMID {e['pmid']}) — "
                             f"[PubMed](https://pubmed.ncbi.nlm.nih.gov/{e['pmid']}/)  ")
                lines.append(f"  → {kf}")
        if tiers.get("notes"):
            lines.append(f"\n_Note: {tiers['notes']}_")
        lines.append("")
    with open(review_path, "w") as f:
        f.write("\n".join(lines))

    print(f"\nWrote {cache_path}")
    print(f"Wrote {review_path}")
    try:
        print(f"Wrote {build_reference_docx()}  <- open in Word to review")
    except Exception as e:
        print(f"(Word export skipped: {e})")
    if missing:
        print(f"{missing} reference(s) have no abstract yet - re-run --build-refs later "
              f"to fill them in.")
    return out

# ---------------------------------------------------------------------------
# 3d. Module 3 - grounded "relation to evidence / guidelines"
# ---------------------------------------------------------------------------
# Reads the full-text guideline PDFs from STATE_DIR/References/, retrieves the
# passages relevant to each paper's topic, and writes the two relation fields
# against real text instead of model memory.
#
# Hierarchy, in order of authority:
#   1. ESC guideline (full text, passages retrieved)
#   2. Consensus / position papers (curated key findings)
#   3. Landmark trials (curated key findings)
#
# Tensions between levels ARE surfaced - e.g. new evidence going beyond what the
# current guideline states - but strictly as an observation, never as advice.

REFERENCES_DIRNAME = "References"

# Which guideline PDF applies to which topics. Filenames are matched
# case-insensitively and partially, so minor renaming is tolerated.
# Matched on filename KEYWORDS, not exact names, so typos and renaming are
# tolerated (e.g. "ESC_Valvur_Guidelines.pdf" still registers as the VHD guideline).
GUIDELINE_DOCS = [
    {
        "match": ["valv", "vhd", "klappen"],
        "label": "ESC/EACTS 2025 Valvular Heart Disease Guidelines",
        "topics": "ALL",          # anchor document for every structural topic
    },
    {
        "match": ["_hf", "hf_", "heartfailure", "heart_failure", "herzinsuffizienz"],
        "label": "ESC Heart Failure Guidelines",
        "topics": [
            "TEER - secondary (functional) MR",
            "Transcatheter mitral valve replacement (TMVR)",
            "Transcatheter tricuspid valve (TEER/replacement)",
            "Multivalvular disease",
        ],
    },
    {
        "match": ["_af", "af_", "atrial", "vorhofflimmern"],
        "label": "ESC Atrial Fibrillation Guidelines",
        "topics": ["LAAO / left atrial appendage occlusion"],
    },
    {
        # optional: drop the ESC 2023 endocarditis guideline PDF into the References folder
        "match": ["endocard", "endokard"],
        "label": "ESC 2023 Endocarditis Guidelines",
        "topics": ["Infective endocarditis", "Paravalvular leak"],
    },
]

# Search terms per topic, used to pull the relevant passages out of a long PDF.
TOPIC_SEARCH_TERMS = {
    "TAVR - low risk": ["transcatheter aortic", "TAVI", "TAVR", "aortic stenosis",
                        "surgical aortic valve", "SAVR", "low risk", "low surgical risk"],
    "TAVR - intermediate/high risk": ["transcatheter aortic", "TAVI", "TAVR",
                                      "aortic stenosis", "surgical risk", "high risk",
                                      "intermediate risk", "heart team"],
    "TEER - secondary (functional) MR": ["secondary mitral", "functional mitral",
                                         "transcatheter edge-to-edge", "TEER",
                                         "mitral regurgitation", "heart failure"],
    "TEER - primary (degenerative) MR": ["primary mitral", "degenerative mitral",
                                         "transcatheter edge-to-edge", "TEER",
                                         "mitral regurgitation", "mitral valve repair"],
    "Transcatheter mitral valve replacement (TMVR)": [
        "transcatheter mitral valve", "mitral valve replacement", "mitral prosthesis",
        "mitral annular calcification"],
    "LAAO / left atrial appendage occlusion": [
        "left atrial appendage", "appendage occlusion", "appendage closure",
        "anticoagulation", "stroke prevention"],
    "Transcatheter tricuspid valve (TEER/replacement)": [
        "tricuspid regurgitation", "tricuspid valve", "transcatheter tricuspid",
        "right heart"],
    "PFO / ASD closure": ["patent foramen ovale", "atrial septal", "shunt closure",
                          "cryptogenic stroke"],
    "Paravalvular leak": ["paravalvular", "prosthetic valve", "regurgitation",
                          "valve dehiscence"],
    "Infective endocarditis": ["endocarditis", "infective endocarditis", "vegetation",
                               "prosthetic valve endocarditis", "antibiotic", "blood culture",
                               "surgery for endocarditis", "embolic"],
    "Multivalvular disease": ["multiple valve", "multivalvular", "combined valve",
                              "mixed valve disease"],
    "Other": ["valvular heart disease", "valve intervention"],
}

PDF_CACHE_FILE = "guideline_text_cache.json"
CHUNK_WORDS = 220          # small enough to separate guidance from bibliography
CHUNKS_PER_DOC = 6         # passages handed to the model per guideline


def _references_dir():
    """Find the references folder, tolerating case and a couple of plausible spots."""
    candidates = [STATE_DIR, os.path.dirname(STATE_DIR) or ".",
                  os.path.join(os.path.dirname(STATE_DIR) or ".", "MyDrive")]
    names = (REFERENCES_DIRNAME, "references", "Referenzen", "referenzen", "refs")
    for base in candidates:
        if not os.path.isdir(base):
            continue
        try:
            existing = os.listdir(base)
        except OSError:
            continue
        for n in names:
            hit = next((e for e in existing if e.lower() == n.lower()), None)
            if hit and os.path.isdir(os.path.join(base, hit)):
                return os.path.join(base, hit)
    return os.path.join(STATE_DIR, REFERENCES_DIRNAME)


def extract_pdf_text(path):
    """Plain text of a PDF. Tries pypdf, falls back to the pdftotext binary."""
    try:
        from pypdf import PdfReader
        reader = PdfReader(path)
        return "\n".join((pg.extract_text() or "") for pg in reader.pages)
    except Exception:
        pass
    try:
        import subprocess
        out = subprocess.run(["pdftotext", "-layout", path, "-"],
                             capture_output=True, text=True, timeout=180)
        return out.stdout
    except Exception as e:
        print(f"  ! could not read {os.path.basename(path)}: {e}")
        return ""


def load_guideline_texts(refresh=False):
    """Extract (once) and cache the full text of every guideline PDF found in
    STATE_DIR/References/. Parsing a 150-page PDF takes seconds, so we cache."""
    cache_path = _sp(PDF_CACHE_FILE)
    cache = {}
    if not refresh:
        try:
            with open(cache_path) as f:
                cache = json.load(f)
        except FileNotFoundError:
            pass

    refdir = _references_dir()
    if not os.path.isdir(refdir):
        print(f"!! No references folder found. Looked for '{REFERENCES_DIRNAME}/' "
              f"under {STATE_DIR}.")
        print(f"!! Guideline grounding is DISABLED for this run - the two relation "
              f"fields will be weak. Put the guideline PDFs and key_findings.json in "
              f"{os.path.join(STATE_DIR, REFERENCES_DIRNAME)}")
        return {}
    print(f"References folder: {refdir}")

    pdfs = [f for f in sorted(os.listdir(refdir)) if f.lower().endswith(".pdf")]
    found = {}
    used = set()
    for entry in GUIDELINE_DOCS:
        match = next((f for f in pdfs
                      if f not in used
                      and any(k in os.path.splitext(f)[0].lower().replace("-", "_")
                              for k in entry["match"])), None)
        if not match:
            continue
        used.add(match)
        if match not in cache:
            print(f"  extracting {match} ...")
            txt = extract_pdf_text(os.path.join(refdir, match))
            if not txt.strip():
                continue
            cache[match] = txt
        found[entry["label"]] = {"text": cache[match], "topics": entry["topics"],
                                 "file": match}

    # any additional PDFs the user dropped in, applied to all topics
    for f in pdfs:
        if f in used:
            continue
        if f not in cache:
            print(f"  extracting {f} (unregistered - applied to all topics) ...")
            txt = extract_pdf_text(os.path.join(refdir, f))
            if not txt.strip():
                continue
            cache[f] = txt
        found[os.path.splitext(f)[0].replace("_", " ")] = {
            "text": cache[f], "topics": "ALL", "file": f}

    try:
        with open(cache_path, "w") as fh:
            json.dump(cache, fh)
    except Exception:
        pass
    if found:
        print(f"Guideline full texts loaded: {', '.join(sorted(found))}")
    return found


def _chunks(text, size=CHUNK_WORDS):
    words = text.split()
    return [" ".join(words[i:i + size]) for i in range(0, len(words), size)]


# Guideline PDFs are dominated by their reference list, which is far denser in topic
# keywords than the recommendation text itself. Naive keyword scoring therefore returns
# bibliography instead of guidance. These filters fix that.
_REF_LIST_RE = re.compile(
    r"(et al\.|doi:|https?://|PMID|;\s*\d{4};\s*\d+|\(\d{4}\)\s*\d+[-–]\d+)",
    re.IGNORECASE)
_RECO_RE = re.compile(
    r"(is recommended|are recommended|should be considered|may be considered|"
    r"is indicated|is not recommended|class of recommendation|level of evidence|"
    r"\bClass I\b|\bClass IIa\b|\bClass IIb\b|\bClass III\b|"
    r"recommendation table|it is recommended)", re.IGNORECASE)


def _looks_like_bibliography(chunk):
    """Reference lists and figure/table indexes are keyword-dense but useless here."""
    refs = len(_REF_LIST_RE.findall(chunk))
    years = len(re.findall(r"\b(19|20)\d{2}\b", chunk))
    words = max(len(chunk.split()), 1)
    # a paragraph of prose has few of these; a reference list is full of them
    return (refs >= 4) or (refs / words > 0.012) or (years / words > 0.030)


_CITATION_RE = re.compile(
    r"(\b\d{1,3}[a-z]?\.\s+[A-Z][a-zA-Z\-]+\s+[A-Z]{1,3},|"
    r"\bet al\.|doi:\s*10\.|\bPM\s?ID\b)", re.IGNORECASE)


def _trim_bibliography(chunk):
    """A retrieved passage often starts as guidance and runs into the reference list.
    Cut it where the citations begin, so only the prose reaches the model."""
    m = _CITATION_RE.search(chunk)
    if not m:
        return chunk
    head = chunk[:m.start()].strip()
    # only trim if a meaningful amount of prose remains
    return head if len(head.split()) >= 12 else chunk


def relevant_passages(doc_text, topic, paper_title, n=CHUNKS_PER_DOC):
    """Retrieve the guideline passages that actually contain guidance on the topic.

    Scoring: topic-term density, with a strong bonus for recommendation language and
    an outright exclusion of bibliography-like chunks."""
    terms = [t.lower() for t in TOPIC_SEARCH_TERMS.get(topic, [])]
    terms += [w.lower() for w in re.findall(r"[A-Za-z]{5,}", paper_title or "")][:8]
    if not terms:
        return []
    scored = []
    for ch in _chunks(doc_text):
        low = ch.lower()
        hits = sum(low.count(t) for t in terms)
        if not hits:
            continue
        words = max(len(ch.split()), 1)
        density = hits / words
        reco = len(_RECO_RE.findall(ch))
        # Bibliography is penalised rather than dropped, so a passage that mixes
        # guidance with a few citations still competes; pure reference lists sink.
        is_bib = _looks_like_bibliography(ch)
        # A chunk that is bibliography-like AND carries no guidance is useless: drop it.
        # One that mixes citations with guidance is kept, but penalised.
        if is_bib and reco == 0:
            continue
        score = density * (1.0 + 2.5 * min(reco, 4)) * (0.15 if is_bib else 1.0)
        scored.append((score, reco, ch))
    if not scored:
        return []
    # keep at least a few chunks that carry actual recommendation language
    with_reco = [c for c in scored if c[1] > 0]
    without = [c for c in scored if c[1] == 0]
    with_reco.sort(key=lambda x: x[0], reverse=True)
    without.sort(key=lambda x: x[0], reverse=True)
    # Fewer, better passages beat padding with weak ones.
    picked = with_reco[:n] + without[:max(0, n - len(with_reco[:n]))]
    return [_trim_bibliography(c[2]) for c in picked[:n]]


def load_key_finding_overrides():
    """Manually improved key findings win over the auto-extracted ones.
    Place a JSON file mapping PMID -> sentence at
    STATE_DIR/References/key_findings.json"""
    for name in ("key_findings.json", "key_findings_override.json"):
        p = os.path.join(_references_dir(), name)
        try:
            with open(p) as f:
                data = json.load(f)
            print(f"Loaded {len(data)} manually curated key findings from {name}")
            return {str(k): v for k, v in data.items()}
        except FileNotFoundError:
            continue
        except Exception as e:
            print(f"  ! {name} could not be read: {e}")
    return {}


def build_reference_context(topic, paper_title, guideline_texts, ref_base, overrides,
                            n_passages=CHUNKS_PER_DOC):
    """Assemble the hierarchical context block for one paper."""
    topics = (ref_base or {}).get("topics", {})
    findings = (ref_base or {}).get("key_findings", {})
    tiers = topics.get(topic, {})

    def kf(entry):
        pid = entry["pmid"]
        return overrides.get(pid) or (findings.get(pid, {}) or {}).get("key_finding") or ""

    parts = []

    # --- tier 1: guideline full-text passages ---
    gl_blocks = []
    for label, doc in (guideline_texts or {}).items():
        if doc["topics"] != "ALL" and topic not in doc["topics"]:
            continue
        passages = relevant_passages(doc["text"], topic, paper_title, n=n_passages)
        if passages:
            joined = "\n\n[...]\n\n".join(passages)
            gl_blocks.append(f"### {label}\n{joined}")
    if gl_blocks:
        parts.append("## TIER 1 - CURRENT GUIDELINES (verbatim excerpts)\n"
                     "Cite these as e.g. 'ESC 2025: ...'\n\n" + "\n\n".join(gl_blocks))
    else:
        named = [e["title"] for e in
                 (tiers.get("esc_guideline", []) + tiers.get("additional_guidelines", []))]
        if named:
            parts.append("## TIER 1 - CURRENT GUIDELINES\n"
                         "Full text not available; only these documents are known to apply:\n- "
                         + "\n- ".join(named))

    # --- tier 2: consensus ---
    cons = [f"- {e['title']} ({e['year']}): {kf(e)}".rstrip(": ")
            for e in tiers.get("consensus", []) if kf(e) or e]
    if cons:
        parts.append("## TIER 2 - CONSENSUS / POSITION PAPERS\n"
                     "Cite as e.g. 'Consensus (VARC-3, 2021): ...'\n" + "\n".join(cons))

    # --- tier 3: landmark trials ---
    land = [f"- {e['title']} ({e['year']}): {kf(e)}".rstrip(": ")
            for e in tiers.get("landmark_trials", []) if kf(e) or e]
    if land:
        parts.append("## TIER 3 - REFERENCE TRIALS\n"
                     "Cite by trial name and year, e.g. 'PARTNER 3 (2023): ...' - never with a "
                     "category label such as 'Landmark'.\n" + "\n".join(land))

    if tiers.get("notes"):
        parts.append(f"## NOTE ON THIS TOPIC\n{tiers['notes']}")

    return "\n\n".join(parts)


_RELATION_SYSTEM = {
"de": (
    "Du bist ein erfahrener Kardiologe und ordnest eine NEUE Studie anhand des "
    "mitgelieferten Referenzmaterials ein. Antworte auf Deutsch (Schweizer "
    "Rechtschreibung); Studiennamen, Fachbegriffe und Zahlen bleiben unverändert. "
    "\n\nHINWEIS ZU DEN LEITLINIENAUSZÜGEN: Es sind automatisch abgerufene Passagen, die "
    "unvollständig sein können. Enthalten sie relevante Aussagen, nutze und benenne sie. "
    "Enthalten sie zur Frage nichts, schreibe im Lesertext nur die vorgegebene "
    "Standardformulierung und halte die Beobachtung zusätzlich in 'qc_notes' fest; "
    "spekuliere nicht über den restlichen Inhalt der Leitlinie. "
    "\n\nREGEL 1 - NUR VERWENDEN, WAS GELIEFERT WIRD. Stütze jede Aussage auf das "
    "Referenzmaterial im Prompt und auf den Abstract der neuen Studie. Verlasse dich nie "
    "auf dein Gedächtnis für konkrete Leitlinienempfehlungen, Empfehlungsklassen, "
    "Evidenzlevel, Studiennamen, Zahlen oder Ergebnisse. Deckt das Material die Frage nicht "
    "ab, sage das schlicht. Erfinde nie eine Empfehlungsklasse. "
    "\n\nREGEL 2 - HIERARCHIE BEACHTEN. Gehe die Stufen der Reihe nach durch: aktuelle "
    "Leitlinien zuerst, dann Konsensus-/Positionspapiere, dann einzelne Studien. Nenne die "
    "Quelle jeder Aussage, z. B. 'ESC 2025: ...', 'Konsensus (VARC-3, 2021): ...', "
    "'COAPT (2023): ...' (Studien mit Namen und Jahr, nie mit dem Wort 'Landmark'). "
    "\n\nREGEL 3 - SPANNUNG SICHTBAR MACHEN, NICHT AUFLÖSEN. Erweitert, verfeinert oder "
    "widerspricht die neue Studie dem, was die Leitlinie sagt, benenne das ausdrücklich und "
    "sachlich - etwa dass der Befund über das hinausgeht, was die aktuelle Leitlinie "
    "behandelt, oder in eine andere Richtung zeigt. Diese Beobachtung ist das wertvollste "
    "Ergebnis; sie bleibt aber eine BEOBACHTUNG. "
    "\n\nREGEL 4 - KEINE KLINISCHEN RATSCHLÄGE. Du beschreibst Beziehungen zwischen "
    "Dokumenten und rätst niemandem, was zu tun ist. Verboten: 'sollte', 'sollten', "
    "'empfohlen' (ausser als Wiedergabe dessen, was ein genanntes Dokument ausdrücklich "
    "festhält), 'ist zu erwägen', 'muss', 'rechtfertigt' und jeder Imperativ an die "
    "Leserschaft. Schreibe in der dritten Person darüber, was die Dokumente festhalten und "
    "wie sie zueinander stehen. "
    "\n\nREGEL 5 - LESERTEXT OHNE INTERNE BEGRIFFE. 'relation_evidence', "
    "'relation_guidelines' und 'tension' werden von der Leserschaft gelesen. Verwende dort "
    "NIE Begriffe der Verarbeitung: 'Auszüge', 'Auszug', 'excerpts', 'Tier', 'Referenzbasis', "
    "'Referenzmaterial', 'Referenzstudie', 'kuratiert', 'hier gehalten', 'im Prompt', "
    "'abgerufen', 'Passagen', 'Leitlinientext', 'Landmark', 'vorliegendes Material', "
    "'vorliegende Angaben'. Sprich von 'der Leitlinie' bzw. 'den Leitlinien'. Hinweise zur "
    "Datengrundlage (z. B. 'Die Auszüge enthalten nichts zu X') gehören ausschliesslich in "
    "'qc_notes'. "
    "\n\nREGEL 5b - NIE ÜBER DIE LÜCKEN DES MATERIALS SCHREIBEN. Behaupte im Lesertext nie, "
    "etwas sei 'im Material', 'in den Passagen', 'in den Angaben' oder 'im Text' nicht "
    "enthalten, weder bei einer Leitlinie noch bei einer Studie. Wenn ein Vergleich nicht "
    "möglich ist, beschreibe nur den sachlichen Unterschied (andere Population, anderes "
    "Design, anderer Endpunkt) oder verwende die vorgegebene Standardformulierung. "
    "\n\nREGEL 6 - KEINE EIGENEN BEWERTUNGEN DER STUDIE ('hypothesengenerierend', "
    "'wegweisend', 'schwach' o. ä.). Zulässig sind reine Designfakten ('beobachtend, nicht "
    "randomisiert') und der Vergleich berichteter Ergebnisse. "
    "\n\nREGEL 7 - ZAHLEN EINER QUELLE ZUORDNEN. Jede Zahl gehört zu genau einer genannten "
    "Studie oder Leitlinie. Fasse nie die Ergebnisse zweier Studien in einem Satz mit einem "
    "gemeinsamen Zahlenpaar zusammen. Verwende nur Zahlen, die im gelieferten Material "
    "stehen."
),
"en": (
    "You are an experienced cardiologist placing a NEW study in the context of the "
    "reference material provided. "
    "\n\nNOTE ON THE GUIDELINE EXCERPTS: these are automatically retrieved passages and may "
    "be incomplete. If they contain relevant guidance, use it and name it. If they contain "
    "nothing on the question, write only the prescribed standard sentence in the reader "
    "text and record the observation in 'qc_notes'; do not speculate about the wider "
    "content of the guideline. "
    "\n\nRULE 1 - USE ONLY WHAT IS PROVIDED. Base every statement on the reference "
    "material in the prompt and on the new study's abstract. Never rely on memory for "
    "specific guideline recommendations, classes of recommendation, levels of evidence, "
    "trial names, numbers or results. If the reference material does not cover the "
    "question, say so plainly. Never invent a class of recommendation. "
    "\n\nRULE 2 - RESPECT THE HIERARCHY. Work through the tiers in order: current "
    "guidelines first, then consensus/position papers, then landmark trials. Name the "
    "source of every statement, e.g. 'ESC 2025: ...', 'Consensus (VARC-3, 2021): ...', "
    "'COAPT (2023): ...' (trials by name and year, never with the word 'Landmark'). "
    "\n\nRULE 3 - SURFACE TENSION, DO NOT RESOLVE IT. If the new study extends, refines "
    "or conflicts with what the guideline states, say so explicitly and factually - for "
    "example that the finding goes beyond what the current guideline addresses, or points "
    "in a different direction. This observation is the most valuable output. But it stays "
    "an OBSERVATION. "
    "\n\nRULE 4 - NO CLINICAL ADVICE. You describe relationships between documents; you "
    "never advise anyone what to do. Forbidden: 'should', 'is recommended' (except to "
    "report what a named document explicitly states), 'clinicians should', 'consider', "
    "'warrants', and any imperative addressed to the reader. Write in the third person "
    "about what the documents state and how they relate. "
    "\n\nRULE 5 - READER TEXT WITHOUT INTERNAL TERMS. 'relation_evidence', "
    "'relation_guidelines' and 'tension' are read by the audience. NEVER use processing "
    "terms there: 'excerpts', 'tier', 'reference base', 'reference material', 'curated', "
    "'held here', 'in the prompt', 'retrieved', 'passages', 'landmark', 'provided material'. "
    "Say 'the guideline' / 'the guidelines'. Remarks about the data basis (e.g. 'the "
    "excerpts contain nothing on X') belong exclusively in 'qc_notes'. "
    "\n\nRULE 5b - NEVER WRITE ABOUT GAPS IN THE MATERIAL. In the reader text never claim "
    "that something is 'not contained in the material / the passages / the provided "
    "information' - neither for a guideline nor for a study. If a comparison is not "
    "possible, describe only the factual difference (other population, design, endpoint) "
    "or use the prescribed standard sentence. "
    "\n\nRULE 6 - NO OWN JUDGEMENTS OF THE STUDY ('hypothesis-generating', 'landmark', "
    "'weak' and the like). Plain design facts ('observational, not randomised') and the "
    "comparison of reported results are fine. "
    "\n\nRULE 7 - ATTRIBUTE NUMBERS TO ONE SOURCE. Every number belongs to exactly one named "
    "study or guideline. Never merge the results of two studies into one sentence with a "
    "shared pair of numbers. Use only numbers that appear in the material provided."
),
}

_RELATION_RUBRIC = {
"de": """Antworte mit genau einem JSON-Objekt und sonst nichts:
{
  "relation_evidence": "2-4 Sätze: Einordnung der neuen Studie gegenüber den oben genannten
        Studien und Konsensuspapieren. Nenne die Studien beim Namen (Name und Jahr, ohne das
        Wort 'Landmark'). Sage, ob der Befund sie bestätigt, erweitert, verfeinert oder ihnen
        widerspricht; andernfalls beschreibe nur, worin sich Population, Design oder Endpunkt
        unterscheiden. Ist nichts Vergleichbares aufgeführt, schreibe genau:
        'Für dieses Thema wurden keine direkt vergleichbaren Studien herangezogen.'",
  "relation_guidelines": "2-4 Sätze: Bezug des Befunds zu den oben aufgeführten Leitlinien.
        Gib den Inhalt in eigenen Worten wieder und nenne die Quelle ('ESC 2025: ...').
        Behandelt keine der aufgeführten Textstellen die Frage, schreibe genau:
        'Zu dieser Frage wurde in den berücksichtigten Leitlinien keine direkte Aussage gefunden.'
        Steht oben nur der TITEL einer Leitlinie ohne Text (\"Full text not available\" bzw.
        Hinweis auf eine fehlende Leitlinie), schreibe genau: 'Zu diesem Thema besteht die
        Leitlinie <Titel>; ein inhaltlicher Abgleich erfolgt in dieser Ausgabe nicht.'
        Erfinde nie eine Empfehlungsklasse.",
  "tension": "Ein Satz, WENN die neue Evidenz über die aktuelle Leitlinie hinausgeht, sie
        verfeinert oder ihr widerspricht - als sachliche Beobachtung, z. B. 'Der Befund
        betrifft eine Population, die die aktuelle Leitlinie noch nicht abdeckt.'
        Sonst null.",
  "sources_used": ["Kurzlabels der tatsächlich verwendeten Quellen, z. B. 'ESC 2025',
        'PARTNER 3 2023', 'VARC-3'"],
  "qc_notes": "Interne Hinweise für die Redaktion (erscheinen NICHT im Dokument), z. B.
        'Leitlinienpassagen enthalten nichts zu ...', 'Referenzmaterial deckt das Thema nur
        teilweise ab'. Leer lassen, wenn nichts auffällt."
}
Beschreibend, ohne Empfehlungen, ohne Imperative.""",
"en": """Return a single JSON object and nothing else:
{
  "relation_evidence": "2-4 sentences placing the new study against the trials and
        consensus papers above. Name the trials you refer to (name and year, without the word
        'landmark'). State whether the finding confirms, extends, refines or contradicts them;
        otherwise describe only how population, design or endpoint differ. If nothing
        comparable is listed, write exactly:
        'No directly comparable studies were considered for this topic.'",
  "relation_guidelines": "2-4 sentences on how the finding relates to the guidelines listed
        above. Give the substance in your own words and name the source ('ESC 2025: ...').
        If none of the listed passages addresses the question, write exactly:
        'No direct statement on this question was found in the guidelines considered.'
        If only the TITLE of a guideline is listed without text ('Full text not available' or a
        note about a missing guideline), write exactly: 'For this topic the guideline <title>
        exists; no comparison of content is made in this edition.'
        Never invent a class of recommendation.",
  "tension": "One sentence IF the new evidence goes beyond, refines or contradicts the
        current guideline - stated as a factual observation, e.g. 'The finding addresses a
        population the current guideline does not yet cover.' Otherwise null.",
  "sources_used": ["short labels of what you actually relied on, e.g. 'ESC 2025',
        'PARTNER 3 2023', 'VARC-3'"],
  "qc_notes": "Internal notes for the editor (NOT shown in the document), e.g. 'guideline
        passages contain nothing on ...', 'reference material covers the topic only in
        part'. Leave empty if nothing stands out."
}
Descriptive register throughout. No recommendations, no imperatives.""",
}

# Words that betray the pipeline instead of describing the science. They must never
# reach the reader; they belong in qc_notes.
_JARGON_RE = re.compile(
    r"\b(?:excerpts?|Auszüge?|Auszug|Tier\s?\d|reference base|reference material|Referenzbasis|"
    r"Referenzmaterial|Referenzstudien?|curated|kuratiert\w*|held here|hier gehalten|"
    r"im Prompt|in the prompt|retrieved|abgerufen\w*|Leitlinientext\w*|Passagen?|passages?|"
    r"(?:vorliegend\w*|gelieferte\w*|bereitgestellte\w*|mitgelieferte\w*|provided|supplied)\s+"
    r"(?:Material|Passagen?|Angaben|Auszüg\w+|Texte?s?|Unterlagen|Informationen|Beschreibung\w*|"
    r"Quellen|material|passages?|excerpts?|text|information)|"
    r"unvollständig wiedergegeben|Beschreibung des Papiers)\b",
    re.IGNORECASE)

_REL_READER_FIELDS = ("relation_evidence", "relation_guidelines", "tension")

# The prescribed standard sentences (they must appear in the rubric word for word) -
# shown when the grounded text is empty or had to be scrubbed completely.
REL_DEFAULTS = {
    "de": {"relation_evidence": "Für dieses Thema wurden keine direkt vergleichbaren Studien herangezogen.",
           "relation_guidelines": "Zu dieser Frage wurde in den berücksichtigten Leitlinien keine direkte Aussage gefunden."},
    "en": {"relation_evidence": "No directly comparable studies were considered for this topic.",
           "relation_guidelines": "No direct statement on this question was found in the guidelines considered."},
}
for _l, _d in REL_DEFAULTS.items():
    for _k, _v in _d.items():
        assert _v in _RELATION_RUBRIC[_l], f"default sentence missing from the {_l} relation rubric"

_ABBREVIATIONS = ("z. B.", "z.B.", "d. h.", "d.h.", "u. a.", "u.a.", "i. d. R.", "bzw.", "ca.",
                  "vs.", "Abb.", "Nr.", "inkl.", "evtl.", "ggf.", "et al.", "e. g.", "i. e.",
                  "approx.", "Dr.", "Prof.")


def split_sentences(text):
    t = text or ""
    for a in _ABBREVIATIONS:
        t = t.replace(a, a.replace(".", "\u2024"))
    parts = re.split(r"(?<=[.!?])\s+(?=[A-ZÄÖÜ0-9\"\u201c(])", t.strip())
    return [x.replace("\u2024", ".") for x in parts if x.strip()]


_LANDMARK_NAME_YEAR = re.compile(r"\bLandmark\s*\(\s*([^,()]+?)\s*,\s*((?:19|20)\d{2}[^)]*)\)", re.IGNORECASE)
_LANDMARK_FIX = {
    "de": [(re.compile(r"\bLandmark\s*\((?=[^)]*\d{4})", re.IGNORECASE), "Studie ("),
           (re.compile(r"\b(randomisierte[nr]?|prospektive[nr]?)\s+Landmark-Studien?\b", re.IGNORECASE),
            lambda m: m.group(1) + " Studien"),
           (re.compile(r"\bLandmark-Studien?\b", re.IGNORECASE), "Studien")],
    "en": [(re.compile(r"\bLandmark\s*\((?=[^)]*\d{4})", re.IGNORECASE), "Study ("),
           (re.compile(r"\b(?:randomi[sz]ed|prospective)\s+landmark (trials?|studies)\b", re.IGNORECASE),
            lambda m: "randomised " + m.group(1)),
           (re.compile(r"\blandmark (trials?|studies)\b", re.IGNORECASE), lambda m: m.group(1))],
}


def scrub_reader_text(text, lang="de"):
    """Reader text without pipeline vocabulary: 'Landmark (COAPT, 2023)' becomes 'COAPT
    (2023)', and sentences that talk about the supplied material instead of the science
    are removed. Returns '' if nothing is left."""
    t = (text or "").strip()
    if not t:
        return ""
    t = _LANDMARK_NAME_YEAR.sub(lambda m: f"{m.group(1).strip()} ({m.group(2).strip()})", t)
    for rx, sub in _LANDMARK_FIX.get(lang, _LANDMARK_FIX["de"]):
        t = rx.sub(sub, t)
    kept = [x for x in split_sentences(t) if not _JARGON_RE.search(x)]
    return " ".join(kept).strip()

REL_CACHE_FILE = "relation_cache.json"


def _jargon_in(rel):
    hits = []
    for k in _REL_READER_FIELDS:
        m = _JARGON_RE.search(str(rel.get(k) or ""))
        if m:
            hits.append(m.group(0))
    return hits


def build_relation_fields(paper, client, guideline_texts, ref_base, overrides,
                          model=None, lang=None, cache=None):
    """Module 3: rewrite the relation fields, grounded in the reference base.
    Handles studies that cover several structural topics (mixed cohorts), caches the
    result per (paper, language, grounding material) and keeps pipeline jargon out of
    the reader text (one automatic repair attempt; leftovers are flagged for QC)."""
    lang = lang or OUTPUT_LANG
    model = model or SUMMARY_MODEL
    sc = paper.get("score") or {}
    topic = sc.get("structural_topic") or "Other"
    extra = [t for t in (sc.get("structural_topics_extra") or []) if t != topic]
    topics = [topic] + extra

    blocks = []
    for i, t in enumerate(topics):
        ctx = build_reference_context(t, paper.get("title", ""), guideline_texts, ref_base,
                                      overrides, n_passages=CHUNKS_PER_DOC if i == 0 else 3)
        if ctx.strip():
            blocks.append((t, ctx))
    if len(blocks) <= 1:
        context = blocks[0][1] if blocks else ""
    else:
        context = "\n\n".join(f"########## REFERENCE SET FOR TOPIC: {t} ##########\n{c}"
                              for t, c in blocks)

    # diagnostics so a weak result can be traced to its cause
    tmap = ((ref_base or {}).get("topics", {}) or {})
    paper["_grounding"] = {
        "topic": topic, "topics_all": topics,
        "topic_was_set": bool(sc.get("structural_topic")) and not sc.get("topic_guessed"),
        "topic_guessed": bool(sc.get("topic_guessed")),
        "guideline_words": sum(len(c.split("## TIER 2")[0].split())
                               for _, c in blocks if "TIER 1" in c),
        "n_landmarks": sum(len((tmap.get(t, {}) or {}).get("landmark_trials", [])) for t in topics),
        "n_consensus": sum(len((tmap.get(t, {}) or {}).get("consensus", [])) for t in topics),
        "context_words": len(context.split()),
    }
    if not context.strip():
        return None

    key = "|".join([str(paper.get("pmid")), lang, RELATION_VERSION, model,
                    hashlib.sha1((context + (paper.get("abstract") or "")).encode("utf-8"))
                    .hexdigest()[:12]])
    if cache is not None and key in cache:
        paper["_grounding"]["cached"] = True
        return cache[key]

    user = (
        f"# NEW STUDY TO PLACE IN CONTEXT\n"
        f"Title: {paper['title']}\n"
        f"Journal: {paper['journal']} ({paper['pub_date']})\n"
        f"Topic: {', '.join(topics)}\n\n"
        f"Abstract:\n{paper.get('abstract','')}\n\n"
        f"# REFERENCE MATERIAL (hierarchical)\n{context}\n\n"
        f"{_RELATION_RUBRIC[lang]}"
    )
    rel, err = llm_json(client, model, _RELATION_SYSTEM[lang], user, 2000,
                        tag=f"Module 3 failed for: {paper['title'][:50]}")
    if rel is None:
        return None

    bad = _jargon_in(rel)
    if bad:                                   # one repair attempt
        fix = (user + "\n\nYour previous answer used internal processing terms in the "
               f"reader text ({', '.join(sorted(set(bad)))}). Rewrite the answer without "
               "them; move any remark about the data basis into qc_notes.")
        rel2, _ = llm_json(client, model, _RELATION_SYSTEM[lang], fix, 2000)
        if rel2 is not None and not _jargon_in(rel2):
            rel = rel2
        else:
            rel["qc_notes"] = ((rel.get("qc_notes") or "") +
                               f" | JARGON LEFT IN READER TEXT: {', '.join(sorted(set(bad)))}").strip(" |")
    for k in _REL_READER_FIELDS:                 # whatever the repair left behind is scrubbed
        v = rel.get(k)
        if isinstance(v, str) and v.strip():
            cleaned = scrub_reader_text(v, lang)
            if cleaned != v.strip():
                rel["qc_notes"] = ((rel.get("qc_notes") or "") +
                                   (f" | {k}: Sätze über das Material bzw. interne Begriffe entfernt"
                                    if lang == "de" else
                                    f" | {k}: sentences about the material / internal terms removed")
                                   ).strip(" |")
            rel[k] = cleaned
    for k in _REL_READER_FIELDS + ("qc_notes",):
        v = rel.get(k)
        rel[k] = v.strip() if isinstance(v, str) and v.strip() else None
    if not isinstance(rel.get("sources_used"), list):
        rel["sources_used"] = []
    if cache is not None:
        cache[key] = rel
    return rel

def build_reference_docx(out_path=None):
    """Render the curated reference base as a branded, readable Word document.
    Reads reference_base.json from STATE_DIR (written by --build-refs)."""
    from docx import Document
    from docx.shared import Pt, Inches, RGBColor
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.enum.table import WD_ROW_HEIGHT_RULE
    from docx.oxml.ns import qn
    from docx.oxml import OxmlElement
    from docx.opc.constants import RELATIONSHIP_TYPE as RT

    TEAL, TEAL_D, MINT = "0F3D3A", "0A2C2A", "2FB89A"
    MINT_BG, CREAM, INK, MUTE, PANEL = "E8F5EF", "F7F4ED", "16302E", "5C7370", "F0F6F3"

    out_path = out_path or _sp("reference_base_review.docx")
    with open(_sp("reference_base.json")) as f:
        data = json.load(f)
    findings = data.get("key_findings", {})
    topics = data.get("topics", {})
    order = data.get("topic_order", list(topics))

    doc = Document()
    sec = doc.sections[0]
    sec.top_margin = sec.bottom_margin = Inches(0.8)
    sec.left_margin = sec.right_margin = Inches(0.8)
    CW = sec.page_width - sec.left_margin - sec.right_margin
    base = doc.styles["Normal"]
    base.font.name = "Calibri"; base.font.size = Pt(10)
    base.font.color.rgb = RGBColor.from_string(INK)

    def shade(cell, fill):
        tcPr = cell._tc.get_or_add_tcPr()
        shd = OxmlElement("w:shd")
        shd.set(qn("w:val"), "clear"); shd.set(qn("w:color"), "auto")
        shd.set(qn("w:fill"), fill)
        tcPr.append(shd)

    def borderless(table):
        b = OxmlElement("w:tblBorders")
        for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
            e = OxmlElement(f"w:{edge}")
            e.set(qn("w:val"), "none"); e.set(qn("w:sz"), "0")
            b.append(e)
        table._tbl.tblPr.append(b)
        for row in table.rows:
            row._tr.get_or_add_trPr().append(OxmlElement("w:cantSplit"))

    def write(p, text="", size=10, bold=False, color=INK, italic=False,
              before=0, after=3, align=None):
        p.paragraph_format.space_before = Pt(before)
        p.paragraph_format.space_after = Pt(after)
        if align is not None:
            p.alignment = align
        if text:
            r = p.add_run(text)
            r.font.size = Pt(size); r.bold = bold; r.italic = italic
            r.font.color.rgb = RGBColor.from_string(color)
        return p

    def para(text="", **kw):
        return write(doc.add_paragraph(), text, **kw)

    def link(p, url, text, color=MINT, size=8):
        rid = p.part.relate_to(url, RT.HYPERLINK, is_external=True)
        h = OxmlElement("w:hyperlink"); h.set(qn("r:id"), rid)
        r = OxmlElement("w:r"); rPr = OxmlElement("w:rPr")
        c = OxmlElement("w:color"); c.set(qn("w:val"), color); rPr.append(c)
        u = OxmlElement("w:u"); u.set(qn("w:val"), "single"); rPr.append(u)
        sz = OxmlElement("w:sz"); sz.set(qn("w:val"), str(int(size * 2))); rPr.append(sz)
        r.append(rPr)
        t = OxmlElement("w:t"); t.text = text; r.append(t)
        h.append(r); p._p.append(h)

    # counts
    seen, n_ok, n_missing = set(), 0, 0
    for t in topics.values():
        for tier in _REF_TIERS:
            for e in t.get(tier, []):
                if e["pmid"] in seen:
                    continue
                seen.add(e["pmid"])
                if findings.get(e["pmid"], {}).get("key_finding"):
                    n_ok += 1
                else:
                    n_missing += 1

    # ---- cover banner ----
    banner = doc.add_table(rows=1, cols=1); borderless(banner)
    bc = banner.cell(0, 0); shade(bc, TEAL); bc.width = CW
    banner.rows[0].height = Inches(1.9)
    banner.rows[0].height_rule = WD_ROW_HEIGHT_RULE.EXACTLY
    write(bc.paragraphs[0], "", after=22)
    write(bc.add_paragraph(), "CLARITY", size=30, bold=True, color=CREAM,
          align=WD_ALIGN_PARAGRAPH.CENTER, after=2)
    write(bc.add_paragraph(), "Reference base — structural cardiology", size=11,
          color=MINT, align=WD_ALIGN_PARAGRAPH.CENTER, after=0)

    para("", after=14)
    para("Curated evidence base for review", size=15, bold=True, color=TEAL,
         align=WD_ALIGN_PARAGRAPH.CENTER, after=3)
    para(f"{len(order)} topics · {len(seen)} unique references · "
         f"{n_ok} with key finding · {n_missing} pending · {dt.date.today().isoformat()}",
         size=9.5, color=MUTE, align=WD_ALIGN_PARAGRAPH.CENTER, after=12)

    note = doc.add_table(rows=1, cols=1); borderless(note)
    nc = note.cell(0, 0); shade(nc, MINT_BG); nc.width = CW
    write(nc.paragraphs[0], "HOW TO USE THIS DOCUMENT", size=8, bold=True,
          color=TEAL_D, after=2)
    write(nc.add_paragraph(),
          "Each reference below carries a one-sentence key finding, extracted "
          "automatically from its abstract. Please review these for accuracy — they are "
          "the basis against which new studies are compared. Corrections can be made "
          "directly in structural_references.json. References marked as pending have no "
          "abstract available in the public sources yet; they are retried automatically.",
          size=9, color=INK, after=0)
    para("", after=8)

    tier_labels = {"esc_guideline": "ESC guideline (primary reference)",
                   "additional_guidelines": "Additional guidelines",
                   "consensus": "Consensus, position & definitions papers",
                   "landmark_trials": "Landmark trials"}

    for topic in order:
        tiers = topics.get(topic, {})
        doc.add_page_break()
        # topic header bar
        hb = doc.add_table(rows=1, cols=1); borderless(hb)
        hc = hb.cell(0, 0); shade(hc, PANEL); hc.width = CW
        write(hc.paragraphs[0], topic, size=14, bold=True, color=TEAL, after=0)
        para("", after=6)

        for tier in _REF_TIERS:
            entries = tiers.get(tier, [])
            if not entries:
                continue
            para(tier_labels[tier].upper(), size=8, bold=True, color=MINT,
                 before=8, after=4)
            for e in entries:
                fi = findings.get(e["pmid"], {})
                kf = fi.get("key_finding")
                tp = doc.add_paragraph()
                tp.paragraph_format.space_after = Pt(1)
                rt = tp.add_run(e["title"])
                rt.font.size = Pt(10); rt.bold = True
                rt.font.color.rgb = RGBColor.from_string(INK)
                mp = doc.add_paragraph()
                mp.paragraph_format.space_after = Pt(2)
                rm = mp.add_run(f"{e['year']} · PMID {e['pmid']} · ")
                rm.font.size = Pt(8); rm.font.color.rgb = RGBColor.from_string(MUTE)
                link(mp, f"https://pubmed.ncbi.nlm.nih.gov/{e['pmid']}/", "PubMed")

                kb = doc.add_table(rows=1, cols=1); borderless(kb)
                kc = kb.cell(0, 0); kc.width = CW
                shade(kc, MINT_BG if kf else "FBF3E8")
                write(kc.paragraphs[0], kf or
                      "Key finding pending — no abstract available in public sources yet; "
                      "will be retried automatically.",
                      size=9, color=INK if kf else MUTE, italic=not kf, after=0)
                para("", after=5)

        if tiers.get("notes"):
            para(f"Note: {tiers['notes']}", size=8.5, italic=True, color=MUTE,
                 before=4, after=0)

    # footer
    fp = sec.footer.paragraphs[0]
    fr = fp.add_run(f"CLARITY · Reference base · {dt.date.today().isoformat()}\t\t")
    fr.font.size = Pt(7); fr.font.color.rgb = RGBColor.from_string(MUTE)
    pr = fp.add_run()
    pr.font.size = Pt(7); pr.font.color.rgb = RGBColor.from_string(MUTE)
    for tag, attrs, txt in (("w:fldChar", {"w:fldCharType": "begin"}, None),
                            ("w:instrText", {"xml:space": "preserve"}, "PAGE"),
                            ("w:fldChar", {"w:fldCharType": "end"}, None)):
        el = OxmlElement(tag)
        for k, v in attrs.items():
            el.set(qn(k), v)
        if txt:
            el.text = txt
        pr._r.append(el)

    doc.save(out_path)
    return out_path

# ---------------------------------------------------------------------------
# Dedup across weeks + persistence
# ---------------------------------------------------------------------------
# All state and outputs live under STATE_DIR. Point it at Google Drive
# (STATE_DIR=/content/drive/MyDrive/clarity) so results survive Colab restarts
# and expensive results are never recomputed. Every state file is written
# atomically (see save_json) and a corrupt file is moved aside, never overwritten.
STATE_DIR = os.environ.get("STATE_DIR", ".")

def _sp(name):
    return os.path.join(STATE_DIR, name)

SEEN_FILE = "seen_pmids.json"              # papers that are fully dealt with
SUMM_CACHE_FILE = "summaries.json"         # pmid -> Module-2 summary (never recompute)
SCORED_FILE = "scored_raw.json"
QUEUE_FILE = "retry_queue.json"            # pmid -> why it is waiting (no abstract, failed call)
DROPPED_FILE = "dropped.csv"               # this run: every paper without a study page + reason
DROPPED_HISTORY_FILE = "dropped_history.csv"   # all runs (append only)
FUNNEL_FILE = "funnel_report.md"
QC_FILE = "qc_report.md"

def load_seen():
    return set(load_json(SEEN_FILE, []))

def save_seen(seen):
    save_json(SEEN_FILE, sorted(seen))

def summ_key(pmid, lang):
    """German keeps the plain PMID (older caches stay valid); every other language gets its own."""
    return str(pmid) if lang == "de" else f"{pmid}|{lang}"


def load_summ_cache():
    return load_json(SUMM_CACHE_FILE, {})

def save_summ_cache(cache):
    save_json(SUMM_CACHE_FILE, cache)

def load_queue():
    q = load_json(QUEUE_FILE, {})
    return q if isinstance(q, dict) else {}

def save_queue(q):
    save_json(QUEUE_FILE, q, indent=1)

def _days_between(d1, d2):
    try:
        return (dt.date.fromisoformat(d2) - dt.date.fromisoformat(d1)).days
    except Exception:
        return 0

def queue_age(entry, today):
    return _days_between(entry.get("first_seen") or today, today)

def queue_put(q, p, reason, today):
    """Register (or refresh) a paper in the retry queue."""
    e = q.get(p["pmid"]) or {"first_seen": today, "attempts": 0}
    e.update({"reason": reason, "title": (p.get("title") or "")[:200],
              "journal": p.get("journal", ""), "last_checked": today})
    e["attempts"] = int(e.get("attempts", 0)) + 1
    q[p["pmid"]] = e

DROP_COLUMNS = ["run_date", "pmid", "title", "journal", "pub_date", "status", "reason",
                "manual_review", "subgroup", "secondary_subgroups", "study_type", "score",
                "origin", "url"]

def drop_row(p, status, reason, today):
    sc = p.get("score") or {}
    return {"run_date": today, "pmid": p.get("pmid", ""), "title": p.get("title", ""),
            "journal": p.get("journal", ""), "pub_date": p.get("pub_date", ""),
            "status": status, "reason": reason,
            "manual_review": "; ".join(manual_reason_text(r, "en") for r in (p.get("_manual") or [])),
            "subgroup": sc.get("subgroup", ""),
            "secondary_subgroups": "; ".join(sc.get("secondary_subgroups") or []),
            "study_type": sc.get("study_type", ""),
            "score": ("" if sc.get("failed") else sc.get("relevance_score", "")),
            "origin": p.get("_origin", ""), "url": p.get("url", "")}

def write_dropped(rows, history=True):
    """dropped.csv (this run, overwritten) and dropped_history.csv (append only).
    UTF-8 with BOM so Excel opens umlauts correctly."""
    with open(_sp(DROPPED_FILE), "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=DROP_COLUMNS)
        w.writeheader()
        w.writerows(rows)
    if history and rows:
        new = not os.path.exists(_sp(DROPPED_HISTORY_FILE))
        with open(_sp(DROPPED_HISTORY_FILE), "a", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=DROP_COLUMNS)
            if new:
                w.writeheader()
            w.writerows(rows)

# ---------------------------------------------------------------------------
# 4. Digest
# ---------------------------------------------------------------------------

def is_guideline_doc(p):
    """True for ANY guideline, consensus, position or best-practice document.
    These are listed by name at the end of the digest and never summarised - they
    are long, broadly disseminated and cannot be compressed faithfully, and they
    would otherwise crowd out primary research in the top-N."""
    s = p.get("score", {}) or {}
    if s.get("statement_type") == "Major guideline" or s.get("study_type") == "Guideline / Consensus":
        return True
    # a review or editorial about guidelines is not itself a guideline, whatever the label says
    return s.get("statement_type") == "Practical statement" and s.get("study_type") != "Review / Editorial"


def _norm_title(t):
    """Normalise a title so the same document published in several journals
    collapses to one key."""
    t = (t or "").lower().strip()
    t = re.sub(r"\s+", " ", t)
    return t.rstrip(".")

def dedup_by_title(papers):
    """Collapse papers that share a title (same document in multiple journals).
    Keeps the highest-scored record and merges all (journal, url) sources into a
    `sources` list."""
    groups = {}
    for p in papers:
        groups.setdefault(_norm_title(p["title"]), []).append(p)
    merged = []
    for grp in groups.values():
        grp.sort(key=lambda x: (x.get("score") or {}).get("relevance_score", 0), reverse=True)
        best = dict(grp[0])
        # de-duplicate sources by url while preserving order (keeps sources merged earlier)
        seen_urls, sources = set(), []
        for g in grp:
            for s_ in (g.get("sources") or [{"journal": g.get("journal", ""), "url": g.get("url", "")}]):
                if s_.get("url") not in seen_urls:
                    seen_urls.add(s_.get("url"))
                    sources.append({"journal": s_.get("journal", ""), "url": s_.get("url", "")})
        best["sources"] = sources
        merged.append(best)
    return merged

def _render_paper(p, include_abstract=True):
    """Card for a paper: metadata, LLM verdict, and (optionally) the abstract.

    include_abstract=True  -> for your internal QC file (keeps the abstract).
    include_abstract=False -> for anything you send to users: summary + link only,
                              no abstract (see the copyright discussion)."""
    s = p.get("score") or {}
    flag = "  ⭐ practice-changing" if s.get("practice_changing") else ""
    iv = "  · 🫀 interventional" if s.get("interventional") else ""
    prov = ("  · ⏳ abstract pending - provisional, title-based rating"
            if (s.get("provisional") or not p.get("abstract")) else "")
    also = [g for g in (s.get("secondary_subgroups") or []) if g]
    authors = p.get("authors") or []
    auth = ", ".join(authors[:3]) + (" et al." if len(authors) > 3 else "")
    sources = p.get("sources") or [{"journal": p.get("journal", ""), "url": p.get("url", "")}]
    src_line = " · ".join(f"[{x['journal']}]({x['url']})" for x in sources)
    out = [
        f"### [{p.get('title', '')}]({sources[0]['url']})",
        f"*{src_line} · {p.get('pub_date', '')} · {auth}*  ",
        f"**{s.get('study_type', '?')} · relevance {s.get('relevance_score', '?')}/10{flag}{iv}{prov}**",
    ]
    if also:
        out.append(f"*also relevant for: {', '.join(also)}*")
    out += [
        "",
        f"**Why it matters:** {s.get('why_it_matters', '')}  ",
        f"**Bottom line:** {s.get('bottom_line', '')}",
        "",
    ]
    if include_abstract:
        out += [
            "<details><summary>Original abstract (internal QC only - remove in product)</summary>",
            "",
            p.get("abstract") or "(no abstract available)",
            "",
            f"DOI: {p.get('doi') or 'n/a'} · PMID: {p.get('pmid')}",
            "</details>",
            "",
        ]
    else:
        out += [f"DOI: {p.get('doi') or 'n/a'} · PMID: {p.get('pmid')}", ""]
    return out


def build_digest(scored, include_abstract=True):
    """Render three zones from the same scored data:
       A. New guidelines  - name + link only, deduplicated, NOT summarised.
       B. Interventional  - cross-cutting lens over coronary/structural cath work.
       C. Subgroups       - studies + practical statements, summarised.
       D. Needs attention - papers whose scoring failed (retried next run) and
                            papers flagged for manual review.
    A paper can appear in both B and C (that is intentional: the interventionalist
    finds it in the lens, the ACS reader finds it in their subgroup).

    include_abstract=False produces the email-safe version (summaries + links,
    no abstracts; zone D is internal and left out)."""
    today = dt.date.today().isoformat()
    lines = [f"# CardioDigest - week of {today}", ""]
    ok = [p for p in scored if not (p.get("score") or {}).get("failed")]

    guidelines = [p for p in ok if is_guideline_doc(p)]
    rest = [p for p in ok if not is_guideline_doc(p)]

    # ---- Zone A: major guidelines, name + link only, deduplicated ----
    g_merged = sorted(dedup_by_title(guidelines),
                      key=lambda x: x["score"].get("relevance_score", 0),
                      reverse=True)
    if g_merged:
        lines += [
            "## 📋 Guidelines, consensus & position papers this week",
            "*For awareness - please read the original "
            "(deliberately not summarised).*",
            "",
        ]
        for p in g_merged:
            srcs = " · ".join(f"[{x['journal']}]({x['url']})" for x in p["sources"])
            lines.append(f"- **{p['title']}**  ")
            lines.append(f"  {srcs}")
        lines.append("")

    # gate + dedup the rest once, reuse for both remaining zones
    gated = dedup_by_title(
        [p for p in rest if p["score"].get("relevance_score", 0) >= MIN_SCORE])

    # ---- Zone B: interventional lens (cross-cutting) ----
    interv = sorted([p for p in gated if p["score"].get("interventional")],
                    key=rank_score, reverse=True)
    if interv:
        lines += [
            "## 🫀 Interventional Cardiology - cross-cutting view",
            "*All coronary & structural cath-lab topics of the week, "
            "across all sections.*",
            "",
        ]
        for p in interv[:MAX_PER_SUBGROUP + 3]:
            lines += _render_paper(p, include_abstract)

    # ---- Zone C: disease/topic subgroups ----
    by_group = {g: [] for g in SUBGROUPS}
    for p in gated:
        g = p["score"].get("subgroup", "Other")
        if g in by_group:
            by_group[g].append(p)
    for g in SUBGROUPS:
        items = sorted(by_group[g], key=rank_score, reverse=True)[:MAX_PER_SUBGROUP]
        if not items:
            continue
        lines += [f"## {g}", ""]
        for p in items:
            lines += _render_paper(p, include_abstract)

    # ---- Zone D: needs attention (internal) ----
    if include_abstract:
        attn = [p for p in scored if p.get("_status") == "scoring_failed" or p.get("_manual")]
        if attn:
            lines += ["## ⚠ Needs attention (internal)", "",
                      "*Not scored or flagged by the safety rules - see funnel_report.md / "
                      "dropped.csv.*", ""]
            for p in attn:
                why = "; ".join(manual_reason_text(r, "en") for r in (p.get("_manual") or [])) \
                    or p.get("_reason", "")
                lines.append(f"- [{p.get('title', '')}]({p.get('url', '')}) - {p.get('journal', '')}: {why}")
            lines.append("")

    return "\n".join(lines)

# ---------------------------------------------------------------------------
# 4b. Accountability: every paper gets a status and a reason - nothing vanishes
# ---------------------------------------------------------------------------
# Status values (p["_status"]) and what they mean:
#   selected          full study page
#   further_listed    qualified, but beyond --top: named in the appendix
#   pending           very recent, abstract not available yet: named in the appendix
#   guideline_listed  guideline / consensus / position paper: named in the appendix
#   duplicate         same title as a featured record (letters, repeated listings)
#   below_threshold   scored under the relevance floor
#   no_abstract_low   no abstract and the title-based triage found it unremarkable
#   outside_focus     belongs to another subgroup than the requested one
#   scoring_failed    the triage call failed (queued for retry)
#   summary_failed    the summary call failed (queued for retry)
#   noise_*           letter / reply / erratum etc., removed before scoring
#   queue_expired     waited MAX_QUEUE_DAYS without ever getting an abstract
# p["_manual"] additionally lists reasons for a human look ("for manual review").

STATUS_ORDER = ["selected", "further_listed", "pending", "guideline_listed", "duplicate",
                "below_threshold", "no_abstract_low", "outside_focus", "scoring_failed",
                "summary_failed", "queue_expired"]


def rank_score(p):
    """Sort key: primary research outranks reviews/commentary at equal relevance."""
    sc = p.get("score") or {}
    r = float(sc.get("relevance_score", 0) or 0)
    if sc.get("study_type") == "Review / Editorial":
        r -= 1.5
    return r


def is_editorial_like(p):
    return (p.get("score") or {}).get("study_type") == "Review / Editorial"


def _set(p, status, reason=""):
    p["_status"] = status
    p["_reason"] = reason


def area_of(p, target_subs):
    """The target subgroup a paper is filed under: its primary subgroup if requested,
    otherwise its first requested secondary subgroup, otherwise None."""
    sc = p.get("score") or {}
    if sc.get("subgroup") in target_subs:
        return sc["subgroup"]
    for g in sc.get("secondary_subgroups") or []:
        if g in target_subs:
            return g
    return None


def classify_papers(scored, target_subs, min_score=PDF_MIN_SCORE,
                    min_count=MIN_STUDIES, fallback_score=FALLBACK_SCORE):
    """Give EVERY scored paper a status. Returns {area: [candidates, best first]}.
    Candidates still have status 'candidate'; the caller turns them into 'selected',
    'further_listed' or 'summary_failed' after the summaries have been made."""
    ranked = {g: [] for g in target_subs}
    for p in scored:
        sc = p.get("score") or {}
        p["_manual"] = []
        p["_display_group"] = None
        p["_filled"] = False
        if sc.get("failed"):
            _set(p, "scoring_failed", str(sc.get("error", ""))[:120])
            continue
        area = area_of(p, target_subs)
        p["_display_group"] = area
        if area is None:
            _set(p, "outside_focus", sc.get("subgroup", ""))
        elif is_guideline_doc(p):
            _set(p, "guideline_listed", sc.get("statement_type", ""))
        elif not p.get("abstract"):
            if sc.get("relevance_score", 0) >= min_score and not is_editorial_like(p):
                _set(p, "pending", "no abstract yet (title-based triage)")
            elif sc.get("commentary_guess"):
                _set(p, "no_abstract_low", "no abstract; the title reads like a commentary "
                                           "(kept in the retry queue, re-checked each run)")
            else:
                _set(p, "no_abstract_low", f"no abstract, title-based score {sc.get('relevance_score', 0)}")
        elif sc.get("relevance_score", 0) < min_score:
            _set(p, "below_threshold", f"score {sc.get('relevance_score', 0)} < {min_score}")
        else:
            _set(p, "candidate")
            ranked[area].append(p)

    # merge candidates that share a title (same document in several journals)
    cand_titles = {}
    for g in target_subs:
        groups = {}
        for p in ranked[g]:
            groups.setdefault(_norm_title(p["title"]), []).append(p)
        merged = []
        for key, grp in groups.items():
            grp.sort(key=rank_score, reverse=True)
            best, urls, sources = grp[0], set(), []
            for x in grp:
                if x["url"] not in urls:
                    urls.add(x["url"])
                    sources.append({"journal": x["journal"], "url": x["url"]})
            best["sources"] = sources
            for dup in grp[1:]:
                _set(dup, "duplicate", f"same title as PMID {best['pmid']}")
            merged.append(best)
            cand_titles[key] = best["pmid"]
        merged.sort(key=rank_score, reverse=True)
        ranked[g] = merged

    # minimum per area: too few studies at the normal threshold -> fill up with the best
    # ones at the fallback score (abstract required; guidelines, reviews never used)
    for g in target_subs:
        need = min_count - len(ranked[g])
        if need <= 0:
            continue
        pool = [p for p in scored
                if p.get("_status") == "below_threshold" and p.get("_display_group") == g
                and p.get("abstract") and not is_editorial_like(p)
                and (p.get("score") or {}).get("relevance_score", 0) >= fallback_score
                and _norm_title(p["title"]) not in cand_titles]
        pool.sort(key=rank_score, reverse=True)
        for p in pool:
            if need <= 0:
                break
            k = _norm_title(p["title"])
            if k in cand_titles:
                continue
            _set(p, "candidate", f"filled up to the minimum of {min_count} "
                                 f"(score {(p.get('score') or {}).get('relevance_score', 0)})")
            p["_filled"] = True
            ranked[g].append(p)
            cand_titles[k] = p["pmid"]
            need -= 1

    # a no-abstract / low record that repeats a featured title (letters!) is a duplicate
    for p in scored:
        if p.get("_status") in ("pending", "no_abstract_low", "below_threshold"):
            k = _norm_title(p["title"])
            if k in cand_titles and cand_titles[k] != p["pmid"]:
                _set(p, "duplicate", f"same title as PMID {cand_titles[k]}")
    return ranked


def manual_flags(p, min_score=PDF_MIN_SCORE):
    """Reasons for a human look at a paper the automation did not feature.
    Codes: ('canary', name) ('top_journal', journal, score|None) ('no_abstract_top', journal)
           ('trial_below', score) ('scoring_failed',) ('summary_failed',)"""
    st = p.get("_status")
    if st not in ("below_threshold", "no_abstract_low", "scoring_failed", "summary_failed",
                  "queue_expired"):
        return []
    sc = p.get("score") or {}
    reasons = []
    hit = "" if is_editorial_like(p) else canary_hit(p)      # a commentary on a trial is not the trial
    if hit:
        reasons.append(("canary", hit))
    if st == "scoring_failed":
        if is_top_any(p.get("journal")) or looks_structural(p.get("title")) or hit:
            reasons.append(("scoring_failed",))
    elif st == "summary_failed":
        reasons.append(("summary_failed",))
    elif not is_editorial_like(p):
        has_abs = bool(p.get("abstract"))
        if is_top_general(p.get("journal")):
            reasons.append(("top_journal", p.get("journal"), sc.get("relevance_score") if has_abs else None))
        elif (not has_abs and is_top_any(p.get("journal"))
              and sc.get("relevance_score", 0) >= max(1, min_score - 1)
              and not looks_like_case_report(p.get("title"))):
            reasons.append(("no_abstract_top", p.get("journal")))
        elif (has_abs and sc.get("study_type") in ("RCT", "Meta-analysis / Systematic review")
              and sc.get("relevance_score", 0) >= min_score - 2):
            reasons.append(("trial_below", sc.get("relevance_score", 0)))
    return reasons


def numbers_gap(summary):
    """True if the long summary is full of numbers but key_numbers stayed (nearly) empty
    - the chart/table would then be missing although the data exist."""
    s = summary or {}
    kn = s.get("key_numbers") or {}
    n_pct = len(re.findall(r"\d+(?:[.,]\d+)?\s?%", s.get("summary") or ""))
    have = (1 if kn.get("primary_endpoint") else 0) + len(kn.get("secondary") or []) \
        + len(kn.get("key_findings") or [])
    return n_pct >= 3 and have < 2


def status_counts(scored):
    c = {}
    for p in scored:
        c[p.get("_status", "?")] = c.get(p.get("_status", "?"), 0) + 1
    return c


_FUNNEL_T = {
    "de": {
        "title": "Funnel-Bericht", "window": "Zeitfenster", "days": "Tage", "focus": "Fokus",
        "stage": "Stufe", "count": "Anzahl",
        "found": "In PubMed gefunden (Journal-Whitelist, Zeitfenster)",
        "seen": "davon bereits früher verarbeitet",
        "fresh": "neu geholt", "queue": "aus der Warteschlange nachgeholt",
        "noise": "Briefe, Antworten, Editorials, Errata vor der Bewertung entfernt",
        "expired": "Warteschlange abgelaufen (nie ein Abstract)",
        "abs_new": "Abstracts über Zweitquellen nachgeladen",
        "abs_missing": "Weiterhin ohne Abstract (nach allen Zweitquellen)",
        "missing": "Von PubMed nicht geliefert (in der Warteschlange)",
        "scored": "bewertet", "cached": "davon aus dem Zwischenspeicher",
        "result": "Ergebnis pro Status",
        "manual": "Zur manuellen Prüfung markiert (zusätzlich zu den Status)",
        "queued_now": "In der Warteschlange für den nächsten Lauf",
        "status": {
            "selected": "Studienseite (ausgewählt)",
            "further_listed": "Weitere relevante Studien (nur namentlich, über --top hinaus)",
            "pending": "Abstract folgt (namentlich im Anhang)",
            "guideline_listed": "Leitlinie / Konsensus (namentlich im Anhang)",
            "duplicate": "Duplikat (gleicher Titel)",
            "below_threshold": "Unter der Relevanzschwelle",
            "no_abstract_low": "Ohne Abstract und unauffällig",
            "outside_focus": "Anderes Fachgebiet als angefragt",
            "scoring_failed": "Bewertung fehlgeschlagen (in Warteschlange)",
            "summary_failed": "Zusammenfassung fehlgeschlagen (in Warteschlange)",
            "queue_expired": "Warteschlange abgelaufen (nie ein Abstract)",
        },
    },
    "en": {
        "title": "Funnel report", "window": "Window", "days": "days", "focus": "Focus",
        "stage": "Stage", "count": "Count",
        "found": "Found in PubMed (journal whitelist, window)",
        "seen": "of which already processed earlier",
        "fresh": "newly fetched", "queue": "re-fetched from the retry queue",
        "noise": "letters, replies, editorials, errata removed before scoring",
        "expired": "queue expired (never got an abstract)",
        "abs_new": "abstracts recovered from secondary sources",
        "abs_missing": "still without abstract (after all secondary sources)",
        "missing": "not delivered by PubMed (kept in the retry queue)",
        "scored": "scored", "cached": "of which from cache",
        "result": "Outcome per status",
        "manual": "Flagged for manual review (in addition to the statuses)",
        "queued_now": "In the retry queue for the next run",
        "status": {
            "selected": "Study page (selected)",
            "further_listed": "Further relevant studies (named only, beyond --top)",
            "pending": "Abstract pending (named in appendix)",
            "guideline_listed": "Guideline / consensus (named in appendix)",
            "duplicate": "Duplicate (same title)",
            "below_threshold": "Below the relevance threshold",
            "no_abstract_low": "No abstract and unremarkable",
            "outside_focus": "Other subgroup than requested",
            "scoring_failed": "Scoring failed (queued)",
            "summary_failed": "Summary failed (queued)",
            "queue_expired": "Retry queue expired (never got an abstract)",
        },
    },
}


def build_funnel_report(stats, scored, focus, days, lang, today):
    T = _FUNNEL_T[lang]
    counts = status_counts(scored)
    rows = [(T["found"], stats.get("found")), (T["seen"], stats.get("seen")),
            (T["fresh"], stats.get("fresh")), (T["queue"], stats.get("from_queue")),
            (T["noise"], stats.get("noise")), (T["expired"], stats.get("expired")),
            (T["abs_new"], stats.get("abs_recovered")), (T["abs_missing"], stats.get("abs_missing")),
            (T["missing"], stats.get("missing")),
            (T["scored"], len(scored)), (T["cached"], stats.get("score_cached"))]
    out = [f"# {T['title']} - {today}", "",
           f"{T['window']}: {days} {T['days']} · {T['focus']}: {focus} · v{VERSION}", "",
           f"| {T['stage']} | {T['count']} |", "|---|---:|"]
    out += [f"| {k} | {v if v is not None else '-'} |" for k, v in rows]
    out += ["", f"## {T['result']}", "", f"| {T['stage']} | {T['count']} |", "|---|---:|"]
    for st in STATUS_ORDER:
        if counts.get(st):
            out.append(f"| {T['status'][st]} | {counts[st]} |")
    flagged = [p for p in scored if p.get("_manual")]
    out += ["", f"{T['manual']}: **{len(flagged)}**",
            f"{T['queued_now']}: **{stats.get('queued_now', 0)}**", ""]
    if flagged:
        out += [f"## {T['manual']}", ""]
        for p in sorted(flagged, key=rank_score, reverse=True):
            why = "; ".join(manual_reason_text(r, lang) for r in p["_manual"])
            out.append(f"- [{p.get('title', '')}]({p.get('url', '')}) - {p.get('journal', '')} "
                       f"(PMID {p.get('pmid')}): {why}")
        out.append("")
    return "\n".join(out)


def build_qc_report(papers, lang, today):
    """Internal editorial notes per featured paper - never part of the documents."""
    de = lang == "de"
    out = [f"# QC-Bericht - {today}" if de else f"# QC report - {today}", "",
           ("Nur für die Redaktion. Enthält Hinweise, die bewusst NICHT im Dokument stehen."
            if de else "For the editor only. Contains remarks deliberately kept OUT of the document."), ""]
    for p in papers:
        s = p.get("summary") or {}
        g = p.get("_grounding") or {}
        flags = []
        for field, phrase in advisory_flags(s):
            flags.append(f"{'Ratschlag-Formulierung' if de else 'advisory wording'} [{field}]: '{phrase}'")
        _k, _d = verify_tiles(s.get("tiles") or [], p.get("abstract") or "")
        if _d:
            flags.append(("Kachel(n) verworfen, Zahl nicht im Abstract: " if de else
                          "tile(s) dropped, number not in the abstract: ") +
                         "; ".join(str(t.get("value")) for t in _d))
        if p.get("_filled"):
            flags.append(f"als Auffüller aufgenommen (Score {(p.get('score') or {}).get('relevance_score')}, "
                         f"unter der normalen Schwelle {PDF_MIN_SCORE})" if de else
                         f"filled up to the minimum (score {(p.get('score') or {}).get('relevance_score')}, "
                         f"below the normal threshold {PDF_MIN_SCORE})")
        if numbers_gap(s):
            flags.append("key_numbers unvollständig (Zahlen im Text, keine Tabelle/Grafik)" if de
                         else "key_numbers incomplete (numbers in text, no table/chart)")
        if g and not g.get("topic_was_set") and (g.get("topic") or "Other") != "Other" and g.get("topic_guessed"):
            flags.append("Thema per Stichwortregel zugeordnet (nicht vom Modell)" if de
                         else "topic assigned by keyword rule (not by the model)")
        if g and (g.get("topic") in (None, "Other")):
            flags.append("kein spezifisches Struktur-Thema: Referenzstudien fehlen" if de
                         else "no specific structural topic: reference studies missing")
        if g and not g.get("guideline_words"):
            flags.append("kein Leitlinientext erhalten" if de else "no guideline text received")
        notes = [n for n in (s.get("qc_notes"), (s.get("relation_qc_notes"))) if n]
        out.append(f"## {p.get('title','')}")
        out.append(f"PMID {p.get('pmid')} · {p.get('journal')} · "
                   f"{'Score' if de else 'score'} {(p.get('score') or {}).get('relevance_score')}")
        if g:
            out.append(("Grounding: " if not de else "Grundlage: ") +
                       f"topics={g.get('topics_all') or g.get('topic')}, "
                       f"guideline_words={g.get('guideline_words')}, landmarks={g.get('n_landmarks')}, "
                       f"consensus={g.get('n_consensus')}")
        for f_ in flags:
            out.append(f"- ⚠ {f_}")
        for n in notes:
            out.append(f"- {n}")
        if not flags and not notes:
            out.append("- " + ("keine Auffälligkeiten" if de else "nothing flagged"))
        out.append("")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# 5. Email delivery (Resend)
# ---------------------------------------------------------------------------
# Resend (https://resend.com) has a free tier and a simple API. In TEST MODE you
# can send FROM onboarding@resend.dev TO the email you signed up with, with no
# setup. To send to colleagues you verify your own domain (then set EMAIL_FROM to
# e.g. "CardioDigest <digest@yourdomain.com>").

RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
EMAIL_FROM = os.environ.get("EMAIL_FROM", "CardioDigest <onboarding@resend.dev>")
EMAIL_TO = [a.strip() for a in os.environ.get("EMAIL_TO", "").split(",") if a.strip()]

def markdown_to_html(md_text):
    """Convert the digest Markdown to HTML for email. Uses the `markdown` package
    if present; otherwise falls back to a readable preformatted block."""
    try:
        import markdown as _md
        body = _md.markdown(md_text, extensions=["extra", "sane_lists", "nl2br"])
    except Exception:
        safe = md_text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        body = f"<pre style='white-space:pre-wrap'>{safe}</pre>"
    return (
        "<!doctype html><html><body style=\"font-family:-apple-system,Segoe UI,"
        "Roboto,Helvetica,Arial,sans-serif;max-width:720px;margin:0 auto;"
        "padding:16px;color:#1a1a1a;line-height:1.55\">" + body + "</body></html>"
    )

def send_email(subject, html_body):
    if not RESEND_API_KEY:
        print("No RESEND_API_KEY set - skipping email.")
        return
    if not EMAIL_TO:
        print("No EMAIL_TO set - skipping email.")
        return
    try:
        r = requests.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {RESEND_API_KEY}",
                     "Content-Type": "application/json"},
            json={"from": EMAIL_FROM, "to": EMAIL_TO,
                  "subject": subject, "html": html_body},
            timeout=30,
        )
        if r.status_code >= 300:
            print(f"Email send failed ({r.status_code}): {r.text[:300]}")
        else:
            print(f"Email sent to {', '.join(EMAIL_TO)}")
    except Exception as e:
        print(f"Email send error: {e}")

# ---------------------------------------------------------------------------
# 6. CLARITY documents - one content model, rendered to PDF and Word
# ---------------------------------------------------------------------------
# Two editions come out of ONE set of model calls:
#   lang  - one full page per study (key figures, endpoint chart, tables, relation to
#           evidence and guidelines, authors' conclusion)           -> premium
#   kurz  - compact block per study (2-3 sentences from the `short` field)  -> free
# Both carry the same appendices, so a paper never vanishes without a trace:
#   "abstract pending", "further relevant studies", "guidelines & consensus" and
#   (long edition) "for editorial review".

BRAND = "CLARITY"
_FIELD_NAME_ENV = os.environ.get("FIELD_NAME", "").strip()


def field_name(lang, focus=None):
    if _FIELD_NAME_ENV:
        return _FIELD_NAME_ENV
    if focus:
        return group_label(focus, lang)
    return "Kardiologie" if lang == "de" else "Cardiology"


GROUP_DE = {
    "General Cardiology": "Allgemeine Kardiologie",
    "Heart Failure / Cardiomyopathies": "Herzinsuffizienz / Kardiomyopathien",
    "Electrophysiology & Devices": "Elektrophysiologie & Devices",
    "Interventional Cardiology": "Interventionelle Kardiologie",
    "Structural Heart Disease": "Strukturelle Kardiologie",
    "Coronary Artery Disease / ACS": "Koronare Herzkrankheit / ACS",
    "Cardiac Imaging": "Kardiale Bildgebung",
    "Prevention, Lipids & Sports Cardiology": "Prävention, Lipide & Sportkardiologie",
}

TYPE_DE = {
    "RCT": "Randomisierte kontrollierte Studie",
    "Meta-analysis / Systematic review": "Meta-Analyse / Systematische Übersicht",
    "Guideline / Consensus": "Leitlinie / Konsensus",
    "Large observational / Registry": "Grosse Beobachtungsstudie / Register",
    "Small observational": "Kleine Beobachtungsstudie",
    "Diagnostic / Validation study": "Diagnostik- / Validierungsstudie",
    "Basic / Translational": "Grundlagen- / translationale Forschung",
    "Review / Editorial": "Übersichtsarbeit / Editorial",
    "Other": "Sonstige",
}


GROUP_EN_DISPLAY = {"Structural Heart Disease": "Structural Cardiology"}


def group_label(g, lang):
    return GROUP_DE.get(g, g) if lang == "de" else GROUP_EN_DISPLAY.get(g, g)


def type_label(t, lang):
    return TYPE_DE.get(t, t or "") if lang == "de" else (t or "")


LABELS = {
    "de": {
        "tagline": "Echte Erkenntnisse in Sekunden",
        "cover_lang": "Die relevantesten Publikationen der Woche",
        "cover_kurz": "Die Woche in Kürze",
        "edition_lang": "LANGVERSION", "edition_kurz": "KURZVERSION",
        "study": "Studie", "studies": "Studien", "area": "Fachgebiet", "areas": "Fachgebieten",
        "of_areas": "aus", "contents": "Inhalt",
        "contents_sub": "KI-kuratiert aus den führenden kardiologisch relevanten Fachzeitschriften "
                        "der letzten {days} Tage.",
        "design": "DESIGN", "participants": "TEILNEHMENDE", "followup": "NACHBEOBACHTUNG",
        "primary": "PRIMÄRER ENDPUNKT", "primary_short": "Primärer Endpunkt",
        "other_ep": "WEITERE ENDPUNKTE", "endpoint": "Endpunkt", "effect": "Effekt",
        "arm1": "Gruppe 1", "arm2": "Gruppe 2",
        "key_figures": "BERICHTETE KERNZAHLEN",
        "summary": "ZUSAMMENFASSUNG",
        "rel_ev": "EINORDNUNG IN DIE BESTEHENDE EVIDENZ",
        "rel_gl": "EINORDNUNG IN DIE LEITLINIEN",
        "concl": "SCHLUSSFOLGERUNG DER AUTOREN (WIEDERGEGEBEN)",
        "view": "In PubMed ansehen", "pubmed": "PubMed", "doi": "DOI", "page": "Seite",
        "interventional": "interventionell", "n_prefix": "n = ",
        "picks": "DIE WICHTIGSTEN DREI",
        "quiet": "Ruhige {period} im Fachgebiet {field}: {n} {studies} {verb} unsere Relevanzschwelle.",
        "quiet_week": "Woche", "quiet_days": "Periode", "quiet_verb1": "erreichte", "quiet_verbN": "erreichten",
        "none_title": "Diese Woche keine Studie mit ausreichender Relevanz",
        "none_text": "In diesem Zeitfenster hat keine Publikation die Relevanzschwelle "
                     "erreicht. Weitere Titel stehen in den Anhängen.",
        "app": {
            "pending": ("Neu diese Woche – Abstract folgt",
                        "Hochrelevante, sehr neue Publikationen, deren Abstract noch nicht "
                        "vorliegt. Sie werden automatisch zusammengefasst, sobald er erscheint."),
            "further": ("Weitere relevante Studien",
                        "Diese Studien erfüllen die Relevanzkriterien, wurden aber nicht "
                        "ausführlich zusammengefasst (Begrenzung auf die Top {top} je Fachgebiet)."),
            "guidelines": ("Leitlinien, Konsensus- und Positionspapiere dieser Woche",
                           "Zur Kenntnis genannt – bewusst nicht zusammengefasst; bitte das "
                           "Original lesen."),
            "manual": ("Zur redaktionellen Prüfung",
                       "Automatisch nicht ausgewählt, aber auffällig (z. B. Top-Journal, bekannter "
                       "Studienname, fehlgeschlagene Verarbeitung). Wird vor dem Versand "
                       "redaktionell geprüft."),
        },
        "m_canary": "Studienname aus der Referenzbasis erkannt: {name}",
        "m_top": "Top-Journal ({journal}), automatisch nicht ausgewählt",
        "m_top_score": "Top-Journal ({journal}), Relevanzwert {score}",
        "m_noabs_top": "Kardiologisches Journal ({journal}), noch kein Abstract",
        "m_trial_below": "RCT/Meta-Analyse unter der Relevanzschwelle (Wert {score})",
        "m_scoring_failed": "Bewertung fehlgeschlagen - wird im nächsten Lauf wiederholt",
        "m_summary_failed": "Zusammenfassung fehlgeschlagen - wird im nächsten Lauf wiederholt",
    },
    "en": {
        "tagline": "Real insights in seconds",
        "cover_lang": "The most relevant publications of the week",
        "cover_kurz": "The week in brief",
        "edition_lang": "LONG EDITION", "edition_kurz": "SHORT EDITION",
        "study": "study", "studies": "studies", "area": "subspecialty", "areas": "subspecialties",
        "of_areas": "across", "contents": "Contents",
        "contents_sub": "AI-curated from the leading cardiology-relevant journals of the last "
                        "{days} days.",
        "design": "DESIGN", "participants": "PARTICIPANTS", "followup": "FOLLOW-UP",
        "primary": "PRIMARY ENDPOINT", "primary_short": "Primary endpoint",
        "other_ep": "OTHER ENDPOINTS", "endpoint": "Endpoint", "effect": "Effect",
        "arm1": "Arm 1", "arm2": "Arm 2",
        "key_figures": "KEY FIGURES REPORTED",
        "summary": "SUMMARY",
        "rel_ev": "RELATION TO EXISTING EVIDENCE",
        "rel_gl": "RELATION TO GUIDELINES",
        "concl": "AUTHORS' CONCLUSION (AS REPORTED)",
        "view": "View on PubMed", "pubmed": "PubMed", "doi": "DOI", "page": "Page",
        "interventional": "interventional", "n_prefix": "n = ",
        "picks": "EDITOR'S PICKS",
        "quiet": "A quiet {period} in {field}: {n} {studies} {verb} our relevance threshold.",
        "quiet_week": "week", "quiet_days": "period", "quiet_verb1": "met", "quiet_verbN": "met",
        "none_title": "No study of sufficient relevance this week",
        "none_text": "No publication reached the relevance threshold in this window. "
                     "Further titles are listed in the appendices.",
        "app": {
            "pending": ("New this week - abstract pending",
                        "Highly relevant, very recent publications whose abstract is not yet "
                        "available. They will be summarised automatically once it appears."),
            "further": ("Further relevant studies",
                        "These studies meet the relevance criteria but were not summarised in "
                        "full (limit: top {top} per subspecialty)."),
            "guidelines": ("Guidelines, consensus & position papers this week",
                           "Named for awareness - deliberately not summarised; please read the "
                           "original."),
            "manual": ("For editorial review",
                       "Not selected automatically but conspicuous (e.g. top journal, known trial "
                       "name, failed processing). Reviewed by the editor before distribution."),
        },
        "m_canary": "Trial name from the reference base detected: {name}",
        "m_top": "Top journal ({journal}), not selected automatically",
        "m_top_score": "Top journal ({journal}), relevance score {score}",
        "m_noabs_top": "Cardiology journal ({journal}), no abstract yet",
        "m_trial_below": "RCT/meta-analysis below the relevance threshold (score {score})",
        "m_scoring_failed": "Scoring failed - will be retried on the next run",
        "m_summary_failed": "Summary failed - will be retried on the next run",
    },
}


def manual_reason_text(reason, lang):
    """One human-readable line for a manual_flags() tuple."""
    L = LABELS[lang]
    kind = reason[0]
    if kind == "canary":
        return L["m_canary"].format(name=reason[1])
    if kind == "top_journal":
        if reason[2] is None:
            return L["m_top"].format(journal=reason[1])
        return L["m_top_score"].format(journal=reason[1], score=reason[2])
    if kind == "no_abstract_top":
        return L["m_noabs_top"].format(journal=reason[1])
    if kind == "trial_below":
        return L["m_trial_below"].format(score=reason[1])
    if kind == "scoring_failed":
        return L["m_scoring_failed"]
    if kind == "summary_failed":
        return L["m_summary_failed"]
    return str(reason)


# Accept German aliases / partial names for --only and map to an internal subgroup.
_SUBGROUP_ALIASES = {
    "strukturelle kardiologie": "Structural Heart Disease",
    "strukturell": "Structural Heart Disease",
    "structural": "Structural Heart Disease",
    "structural cardiology": "Structural Heart Disease",
    "interventionelle kardiologie": "Interventional Cardiology",
    "interventionell": "Interventional Cardiology",
    "elektrophysiologie": "Electrophysiology & Devices",
    "rhythmologie": "Electrophysiology & Devices",
    "herzinsuffizienz": "Heart Failure / Cardiomyopathies",
    "kardiomyopathie": "Heart Failure / Cardiomyopathies",
    "bildgebung": "Cardiac Imaging",
    "prävention": "Prevention, Lipids & Sports Cardiology",
    "praevention": "Prevention, Lipids & Sports Cardiology",
    "sportkardiologie": "Prevention, Lipids & Sports Cardiology",
    "lipide": "Prevention, Lipids & Sports Cardiology",
    "koronar": "Coronary Artery Disease / ACS",
    "khk": "Coronary Artery Disease / ACS",
    "acs": "Coronary Artery Disease / ACS",
    "allgemein": "General Cardiology",
}


def resolve_subgroup(name):
    """Return the canonical subgroup for a user-typed name, or None."""
    n = (name or "").strip().lower()
    if not n:
        return None
    if n in _SUBGROUP_ALIASES:
        return _SUBGROUP_ALIASES[n]
    for g in SUBGROUPS:
        if g.lower() == n or GROUP_DE.get(g, "").lower() == n:
            return g
    for g in SUBGROUPS:  # partial match, e.g. "structural heart"
        if n in g.lower() or n in GROUP_DE.get(g, "").lower():
            return g
    return None


DISCLAIMER = (
    "CLARITY is an independent literature curation service and is not affiliated with, "
    "endorsed by, or associated with the journals or societies named herein. This "
    "document is a factual summary of published research, provided for information and "
    "continuing-education purposes only. It does not constitute medical advice, a "
    "treatment recommendation or clinical guidance, and must not be used as a basis for "
    "patient care decisions. Summaries are AI-generated and may contain errors; always "
    "consult the original publication and the applicable clinical guidelines."
)
DISCLAIMER_DE = (
    "CLARITY ist ein unabhängiger Dienst zur Kuratierung von Fachliteratur und steht in keiner "
    "Verbindung zu den genannten Fachzeitschriften oder Fachgesellschaften; diese unterstützen oder "
    "empfehlen CLARITY nicht. Dieses Dokument ist eine sachliche Zusammenfassung veröffentlichter "
    "Forschung und dient ausschliesslich der Information und Fortbildung. Es stellt keine "
    "medizinische Beratung, Behandlungsempfehlung oder klinische Handlungsanleitung dar und darf "
    "nicht als Grundlage für Entscheidungen in der Patientenversorgung verwendet werden. Die "
    "Zusammenfassungen werden von einer KI erstellt und können Fehler enthalten; massgeblich sind "
    "stets die Originalpublikation und die geltenden klinischen Leitlinien."
)


def disclaimer(lang):
    return DISCLAIMER_DE if lang == "de" else DISCLAIMER


# ---------------------------------------------------------------------------
# Legal safety net: flag reader-directed advice that slipped past the prompt.
# Purely advisory to the operator - nothing is rewritten automatically.
# ---------------------------------------------------------------------------
_ADVISORY_RE = re.compile(
    r"\b(?:clinicians?|physicians?|operators?|readers?|we|you)\s+(?:should|must|ought)\b"
    r"|\bshould\s+(?:be\s+(?:considered|used|offered|adopted|implemented|preferred)|"
    r"undergo|receive|prefer|adopt|use|switch)\b"
    r"|\b(?:is|are)\s+recommended\b"
    r"|\bwe\s+(?:recommend|advise|suggest)\b"
    r"|\bwarrants?\s+(?:adoption|implementation)\b"
    r"|\bconsider\s+(?:using|adopting|switching|offering)\b"
    # German
    r"|\b(?:Kliniker\w*|Ärzt\w+|Leser\w*|Anwender\w*|Behandler\w*|Operateure?|wir|Sie)\s+"
    r"(?:sollte[n]?|müsste[n]?|muss(?:ten)?|müssen)\b"
    r"|\bsollte[n]?\s+(?:\w+\s+){0,3}(?:erwogen|eingesetzt|angeboten|bevorzugt|angewendet|"
    r"verwendet|umgestellt|durchgeführt|eingeführt)\b"
    r"|\b(?:wird|werden)\s+empfohlen\b"
    r"|\bwir\s+(?:empfehlen|raten|schlagen)\b"
    r"|\bzu\s+erwägen\b|\bin\s+Betracht\s+(?:ziehen|zu\s+ziehen)\b",
    re.IGNORECASE)


def advisory_flags(summary):
    """Return [(field, matched phrase)] for any reader-directed advice found."""
    hits = []
    for k in ("short", "headline", "why_it_matters", "key_finding", "summary", "relation_evidence",
              "relation_guidelines", "tension", "authors_conclusion", "key_message"):
        m = _ADVISORY_RE.search(str((summary or {}).get(k) or ""))
        if m:
            hits.append((k, m.group(0).strip()))
    return hits


# palette (from the CLARITY deck: deep petrol-teal, mint, cream)
_TEAL   = "#0F3D3A"
_TEAL_D = "#0A2C2A"
_MINT   = "#2FB89A"
_MINT_BG = "#E8F5EF"
_CREAM  = "#F7F4ED"
_INK    = "#16302E"
_MUTE   = "#5C7370"
_HAIR   = "#DCE7E3"

# Characters the PDF base fonts can show: everything in cp1252 plus the Greek / math signs
# reportlab takes from its Symbol fallback. Anything else would print as an empty box.
_PDF_OK = set("≤≥≈≠→←↑↓↔∼−±×∞αβγδεκλμπσφωΔΣΩ∆")
_PDF_MAP = {"●": "•", "‑": "-", "‐": "-", "‒": "–", " ": " ", " ": " ",
            " ": " ", " ": " ", " ": " ", " ": " ", "​": "",
            "⁠": "", "﻿": "", "✓": "", "✔": "", "▲": "", "▼": ""}


def _pdf_clean(s):
    import unicodedata
    out = []
    for ch in str(s):
        if ch in _PDF_MAP:
            out.append(_PDF_MAP[ch])
            continue
        if ord(ch) < 128 or ch in _PDF_OK:
            out.append(ch)
            continue
        try:
            ch.encode("cp1252")
            out.append(ch)
        except UnicodeEncodeError:
            asc = unicodedata.normalize("NFKD", ch).encode("ascii", "ignore").decode()
            out.append(asc)
    return "".join(out)


def _esc(s):
    from xml.sax.saxutils import escape
    return escape(_pdf_clean(s or ""))


def _cell(v):
    """Render a table value: missing/None becomes an em dash, never the word 'None'."""
    s = "" if v is None else str(v).strip()
    return "—" if s.lower() in ("", "none", "null", "n/a", "na", "nan") else s


def _chart_values(pe):
    """Return (v1, v2) if both arms are numeric AND carry the same unit, else
    (None, None). Allows bar charts for %, mmHg, cm2, years - not just percentages."""
    def num(s):
        m = re.search(r"-?\d+(?:[.,]\d+)?", str(s or "").replace("'", "").replace("\u2019", ""))
        return float(m.group(0).replace(",", ".")) if m else None

    def unit(s):
        return re.sub(r"[-\d.,\s]", "", str(s or "")).lower()

    a, b = (pe or {}).get("arm1_value"), (pe or {}).get("arm2_value")
    v1, v2 = num(a), num(b)
    if v1 is None or v2 is None or v1 < 0 or v2 < 0:
        return None, None
    if unit(a) != unit(b):          # e.g. "6.1%" vs "n=30" - not comparable
        return None, None
    return v1, v2


# ---------------------------------------------------------------------------
# Reader-text hygiene. The stored summaries stay exactly as the model wrote them (cache,
# scored_raw.json); only the copies handed to PDF / Word are cleaned, so every fix here is
# retroactive. Everything is deterministic - no model call.
# ---------------------------------------------------------------------------

def clean_title(t):
    """PubMed titles end with a full stop; a headline does not."""
    t = re.sub(r"\s+", " ", t or "").strip()
    if t.endswith(".") and not t.endswith("..") and not re.search(r"(?:\b[A-Z]|\bvs|\bet al)\.$", t):
        t = t[:-1].rstrip()
    return t


def _ch_int(n):
    """Swiss style: plain up to 4 digits, apostrophe from 5 digits (73'588)."""
    return f"{n:,}".replace(",", "'") if n >= 10000 else str(n)


_RX_GROUPED = re.compile(r"(?<![\w.,'’])([1-9]\d{0,2}(?:[,   '’ ]\d{3})+)(?![\d]|[.,]\d)")
_RX_PLAIN_BIG = re.compile(r"(?<![\w.,'’])(\d{5,})(?![\d]|[.,]\d)")


def fmt_numbers(text, lang="de"):
    """One thousands format for all reader text: Swiss apostrophe from 5 digits in the
    German edition (73'588), comma in the English one (73,588)."""
    if text is None:
        return ""
    t = str(text)
    one = _ch_int if lang == "de" else (lambda n: f"{n:,}")
    t = _RX_GROUPED.sub(lambda m: one(int(re.sub(r"\D", "", m.group(1)))), t)
    return _RX_PLAIN_BIG.sub(lambda m: one(int(m.group(1))), t)


def clean_n(n, lang="de"):
    """'Teilnehmende' is ONE number: '2171 (REGISTER); 377 (EXTERNE VALIDIERUNG)' -> '2171'."""
    if n is None:
        return None
    t = str(n).strip()
    m = re.search(r"\d[\d,   '’ ]*(?:\.\d+)?", t)
    if not m:
        return t if 0 < len(t) <= 24 else None
    tok = m.group(0).strip()
    if "." in tok:
        return tok
    digits = re.sub(r"\D", "", tok)
    if not digits:
        return None
    v = int(digits)
    return _ch_int(v) if lang == "de" else f"{v:,}"


# English leftovers in the short table texts -> German. Only whole words; the prompt already
# asks for German, this is the safety net for the cases where the model slipped.
_DE_TERMS = [
    (r"\b(\d+)-years?[\s-]+(?=\w)", r"\1-Jahres-"), (r"\b(\d+)-months?[\s-]+(?=\w)", r"\1-Monats-"),
    (r"\b(\d+)-days?[\s-]+(?=\w)", r"\1-Tage-"),
    (r"\b1 months?\b", "1 Monat"), (r"\b1 years?\b", "1 Jahr"), (r"\b1 weeks?\b", "1 Woche"),
    (r"\b1 days?\b", "1 Tag"),
    (r"\bmonths?\b|\bmo\b\.?", "Monate"), (r"\byears?\b|\byrs?\b\.?", "Jahre"),
    (r"\bweeks?\b", "Wochen"), (r"\bdays\b", "Tage"),
    (r"\bsystematic reviews?\b", "Systematische Übersicht"),
    (r"\bmeta-?analys[ie]s\b", "Meta-Analyse"), (r"\bnarrative review\b", "Narrative Übersichtsarbeit"),
    (r"\bregistry\b", "Register"), (r"\bprospective(?:ly)?\b", "prospektiv"),
    (r"\bretrospective(?:ly)?\b", "retrospektiv"), (r"\bmulti-?cent(?:er|re)\b", "multizentrisch"),
    (r"\bsingle-?cent(?:er|re)\b", "monozentrisch"), (r"\bdouble-blind\b", "doppelblind"),
    (r"\bsingle-arm\b", "einarmig"), (r"\brandomi[sz]ed\b", "randomisiert"),
    (r"\bobservational\b", "beobachtend"), (r"\bcross-sectional\b", "Querschnitt"),
    (r"\bcohorts?\b", "Kohorte"), (r"\bpooled\b", "gepoolt"),
    (r"\ball-cause (?:mortality|death)\b", "Gesamtmortalität"),
    (r"\bcardiovascular (?:mortality|death)\b", "kardiovaskuläre Mortalität"),
    (r"\bcardiac (?:mortality|death)\b", "kardialer Tod"),
    (r"\bevent-free survival\b", "ereignisfreies Überleben"), (r"\bsurvival\b", "Überleben"),
    (r"\bmortality\b", "Mortalität"),
    (r"\bheart failure hospitali[sz]ations?\b", "Herzinsuffizienz-Hospitalisierung"),
    (r"\bhospitali[sz]ations?\b", "Hospitalisierung"),
    (r"\bpermanent pacemaker implantation\b", "permanente Schrittmacherimplantation"),
    (r"\blatest follow-up\b", "letztes Follow-up"), (r"\bat discharge\b", "bei Entlassung"),
    (r"\bmedian\b", "Median"), (r"\baverage\b|\bavg\b\.?", "im Mittel"),
    (r"\bGroup\s*(\d)\b", r"Gruppe \1"), (r"\bArm\s*(\d)\b", r"Gruppe \1"),
    (r"\bstandardi[sz]ed mean difference\b", "standardisierte Mittelwertdifferenz"),
    (r"\bmean difference\b", "Mittelwertdifferenz"), (r"\brisk difference\b", "Risikodifferenz"),
    (r"\bhazard ratio\b", "Hazard Ratio"), (r"\bodds ratio\b", "Odds Ratio"),
    (r"\brelative risk\b", "relatives Risiko"), (r"\bnon-?inferior(?:ity)?\b", "nicht unterlegen"),
    (r"\bsignificantly\b", "signifikant"), (r"\bnot significant\b", "nicht signifikant"),
]
_DE_TERMS_RX = [(re.compile(a, re.IGNORECASE), b) for a, b in _DE_TERMS]
_EN_HINT = re.compile(r"\b(?:the|of|with|than|group|lower|higher|between|was|were|and|"
                      r"significantly|compared|reduced|increased|improved)\b", re.IGNORECASE)


def de_terms(text):
    t = str(text)
    first_upper = t[:1].isupper()
    for rx, sub in _DE_TERMS_RX:
        t = rx.sub(sub, t)
    return (t[:1].upper() + t[1:]) if first_upper and t else t


_HAS_MEASURE = re.compile(r"\b(?:a?HR|OR|RR|IRR|RD|SMD|CI|KI)\b|\b[Pp]\s*[<=>≤≥]|\d+[.,]\d+|\d\s*%")


def _is_prose_without_number(v):
    """A sentence in words where a figure belongs ('Significantly lower mortality in
    MitraScore <=3 group'): English function words, no measure (HR, p-value, decimal, %)."""
    v = v or ""
    return len(v.split()) >= 4 and not _HAS_MEASURE.search(v) and bool(_EN_HINT.search(v))


def localize_kn(kn, lang):
    """key_numbers for display: German table texts, one number format, one participant
    number, no sentence where a measure belongs. Returns the same structure."""
    kn = copy.deepcopy(kn or {})
    de = lang == "de"

    def tx(v, prose_ok=True):
        if v is None:
            return None
        t = str(v).strip()
        if not t:
            return None
        if not prose_ok and _is_prose_without_number(t):          # judge BEFORE translating
            return None
        return fmt_numbers(de_terms(t) if de else t, lang)

    out = {"design": tx(kn.get("design")), "followup": tx(kn.get("followup")),
           "n": clean_n(kn.get("n"), lang)}
    pe = kn.get("primary_endpoint")
    if isinstance(pe, dict):
        pe2 = {"name": tx(pe.get("name")),
               "arm1_label": tx(pe.get("arm1_label")), "arm1_value": tx(pe.get("arm1_value"), False),
               "arm2_label": tx(pe.get("arm2_label")), "arm2_value": tx(pe.get("arm2_value"), False),
               "effect": tx(pe.get("effect"), False)}
        if pe2["arm2_value"] is None:
            pe2["arm2_label"] = None
        pe = pe2 if (pe2["name"] and (pe2["arm1_value"] or pe2["effect"])) else None
    else:
        pe = None
    out["primary_endpoint"] = pe
    sec = []
    for r in kn.get("secondary") or []:
        row = {"name": tx(r.get("name")), "arm1": tx(r.get("arm1"), False),
               "arm2": tx(r.get("arm2"), False), "effect": tx(r.get("effect"), False)}
        if row["name"] and (row["arm1"] or row["arm2"] or row["effect"]):
            sec.append(row)
    out["secondary"] = sec[:5]
    kf = []
    for r in kn.get("key_findings") or []:
        row = {"label": tx(r.get("label")), "value": tx(r.get("value"))}
        if row["label"] and row["value"]:
            kf.append(row)
    out["key_findings"] = kf[:6]
    return out


def display_paper(p, lang):
    """A cleaned COPY of a featured paper for the documents (the stored one is untouched)."""
    q = dict(p)
    q["title"] = clean_title(p.get("title"))
    s = copy.deepcopy(p.get("summary") or {})
    for k in ("short", "headline", "why_it_matters", "key_finding", "design_tag", "summary",
              "authors_conclusion"):
        if s.get(k):
            s[k] = fmt_numbers(s[k], lang)
    tiles = s.get("tiles") or tiles_fallback(s.get("key_numbers") or {})
    tiles, _dropped = verify_tiles(tiles, p.get("abstract") or "")
    s["tiles"] = [{k: fmt_numbers(t.get(k) or "", lang) for k in ("value", "label", "context")}
                  for t in tiles]
    for k in ("relation_evidence", "relation_guidelines", "tension"):
        if s.get(k):
            cleaned = scrub_reader_text(s[k], lang)
            if cleaned:
                s[k] = fmt_numbers(cleaned, lang)
            elif k in REL_DEFAULTS[lang]:
                s[k] = REL_DEFAULTS[lang][k]
            else:
                s.pop(k, None)
    s["key_numbers"] = localize_kn(s.get("key_numbers"), lang)
    q["summary"] = s
    return q


_LABELS_BASE = copy.deepcopy(LABELS)


def adapt_labels_to_window(days):
    """'... der Woche' is right for a weekly run only; for --days 30 the headings say so."""
    for lg in LABELS:
        L, B = LABELS[lg], _LABELS_BASE[lg]
        for k in ("cover_lang", "cover_kurz"):
            L[k] = B[k]
        L["app"] = dict(B["app"])
        if days <= 8:
            continue
        if lg == "de":
            L["cover_lang"] = f"Die relevantesten Publikationen der letzten {days} Tage"
            L["cover_kurz"] = f"Die letzten {days} Tage in Kürze"
            L["app"]["pending"] = ("Neu erschienen – Abstract folgt", B["app"]["pending"][1])
            L["app"]["guidelines"] = (f"Leitlinien, Konsensus- und Positionspapiere der letzten {days} Tage",
                                      B["app"]["guidelines"][1])
        else:
            L["cover_lang"] = f"The most relevant publications of the last {days} days"
            L["cover_kurz"] = f"The last {days} days in brief"
            L["app"]["pending"] = ("Recently published - abstract pending", B["app"]["pending"][1])
            L["app"]["guidelines"] = (f"Guidelines, consensus & position papers of the last {days} days",
                                      B["app"]["guidelines"][1])


# ---- content model shared by PDF and Word ---------------------------------

def _sources(p):
    return p.get("sources") or [{"journal": p.get("journal", ""), "url": p.get("url", "")}]


def journals_text(p):
    return " · ".join(dict.fromkeys(x["journal"] for x in _sources(p) if x.get("journal")))


def paper_url(p):
    return (_sources(p)[0].get("url") or p.get("url") or "")


def authors_text(p):
    a = p.get("authors") or []
    return ", ".join(a[:3]) + (" et al." if len(a) > 3 else "")


def display_group(p):
    return p.get("_display_group") or (p.get("score") or {}).get("subgroup") or "Other"


def group_items(items):
    """[(subgroup, [papers])] in the canonical SUBGROUPS order, empty groups left out."""
    out = []
    for g in SUBGROUPS:
        ps = [p for p in items if display_group(p) == g]
        if ps:
            out.append((g, ps))
    rest = [p for p in items if display_group(p) not in SUBGROUPS]
    if rest:
        out.append(("Other", rest))
    return out


def count_text(n, ng, lang):
    L = LABELS[lang]
    return (f"{n} {L['study'] if n == 1 else L['studies']} {L['of_areas']} "
            f"{ng} {L['area'] if ng == 1 else L['areas']}")


def kicker_text(p, lang, with_n=False):
    L = LABELS[lang]
    sc = p.get("score") or {}
    parts = [type_label(sc.get("study_type"), lang)]
    if sc.get("interventional"):
        parts.append(L["interventional"])
    n = ((p.get("summary") or {}).get("key_numbers") or {}).get("n")
    if with_n and n:
        parts.append(f"{L['n_prefix']}{n}" if re.fullmatch(r"[\d'’.,\s]+", str(n)) else str(n))
    return "  ·  ".join(x for x in parts if x)


def stat_cells(kn, lang):
    L = LABELS[lang]
    return [(str(kn[k]), L[lab]) for k, lab in
            (("design", "design"), ("n", "participants"), ("followup", "followup")) if kn.get(k)]


def primary_view(pe, lang):
    """None, or {name, arms:[(label, value)], effect, chart:(v1, v2)|None, labels:(l1, l2)}."""
    if not pe or not pe.get("name"):
        return None
    L = LABELS[lang]
    arms = []
    for i in (1, 2):
        if pe.get(f"arm{i}_value"):
            arms.append((pe.get(f"arm{i}_label") or "", str(pe[f"arm{i}_value"])))
    v1, v2 = _chart_values(pe)
    return {"name": str(pe["name"]), "arms": arms, "effect": str(pe.get("effect") or ""),
            "chart": (v1, v2) if v1 is not None else None,
            "labels": (pe.get("arm1_label") or L["arm1"], pe.get("arm2_label") or L["arm2"])}


def secondary_view(kn, lang):
    """None, or (columns[(key, header, width_fraction)], rows). Empty arm columns are
    dropped (single-arm studies), so there is never a column of dashes."""
    L = LABELS[lang]
    rows = [r for r in (kn.get("secondary") or []) if r.get("name")][:5]
    if not rows:
        return None
    pe = kn.get("primary_endpoint") or {}
    cols = [("name", L["endpoint"])]
    if any(r.get("arm1") for r in rows):
        cols.append(("arm1", pe.get("arm1_label") or L["arm1"]))
    if any(r.get("arm2") for r in rows):
        cols.append(("arm2", pe.get("arm2_label") or L["arm2"]))
    if any(r.get("effect") for r in rows):
        cols.append(("effect", L["effect"]))
    fixed = {"arm1": 0.16, "arm2": 0.16, "effect": 0.30}
    used = sum(fixed[k] for k, _ in cols if k != "name")
    return [(k, h, (1 - used) if k == "name" else fixed[k]) for k, h in cols], rows


def findings_view(kn):
    return [(str(f["label"]), str(f["value"])) for f in (kn.get("key_findings") or [])
            if f.get("label") and f.get("value")][:6]


def text_sections(p, lang):
    L = LABELS[lang]
    s = p.get("summary") or {}
    return [(L[lab], s[key]) for lab, key in
            (("summary", "summary"), ("rel_ev", "relation_evidence"), ("rel_gl", "relation_guidelines"))
            if s.get(key)]


def conclusion_text(p):
    s = p.get("summary") or {}
    return s.get("authors_conclusion") or s.get("key_message") or ""


def short_text(p):
    s = p.get("summary") or {}
    return (s.get("short") or "").strip() or _first_sentences(s.get("summary") or "", 2)


PICKS_FROM = 4        # editor's picks only when an edition has at least this many studies
PICKS_N = 3
QUIET_BELOW = 3       # "a quiet week" line when fewer studies than this (but at least one)


def headline_text(p):
    s = p.get("summary") or {}
    return (s.get("headline") or "").strip() or _first_sentences(s.get("short") or s.get("summary") or "", 1)


def front_matter(items, lang, days, focus=None):
    """{'quiet': str|None, 'picks': [(number, title, headline)]} - the reader's way in."""
    L = LABELS[lang]
    n = len(items)
    quiet = None
    if 0 < n < QUIET_BELOW:
        quiet = L["quiet"].format(period=L["quiet_week"] if days <= 8 else L["quiet_days"],
                                  field=field_name(lang, focus), n=n,
                                  studies=L["study"] if n == 1 else L["studies"],
                                  verb=L["quiet_verb1"] if n == 1 else L["quiet_verbN"])
    picks = []
    if n >= PICKS_FROM:
        number = {}
        for _g, ps in group_items(items):
            for i, p in enumerate(ps, 1):
                number[id(p)] = i
        best = sorted(items, key=rank_score, reverse=True)[:PICKS_N]
        best.sort(key=lambda p: number.get(id(p), 99))
        picks = [(number.get(id(p)), p.get("title", ""), headline_text(p)) for p in best]
    return {"quiet": quiet, "picks": picks}


def appendix_model(scored, lang, top=None, with_manual=True):
    """[(key, title, intro, [entry])] for the appendices. entry = {title, meta, url, note}.
    Built from the per-paper _status / _manual that classify_papers and manual_flags set."""
    L = LABELS[lang]

    def entries(pred, note_fn=None):
        ps = dedup_by_title([p for p in scored if pred(p)])
        ps.sort(key=rank_score, reverse=True)
        out = []
        for p in ps:
            sc = p.get("score") or {}
            t = sc.get("study_type")
            known_type = bool(t and t != "Other" and p.get("abstract") and not sc.get("provisional"))
            meta = " · ".join(x for x in (journals_text(p), p.get("pub_date", ""),
                                          type_label(t, lang) if known_type else "") if x)
            out.append({"title": clean_title(p.get("title", "")), "meta": meta, "url": paper_url(p),
                        "jdate": " · ".join(x for x in (journal_display(p), display_date(p, lang)) if x),
                        "note": note_fn(p) if note_fn else ""})
        return out

    spec = [
        ("pending", lambda p: p.get("_status") == "pending", None),
        ("further", lambda p: p.get("_status") == "further_listed", None),
        ("guidelines", lambda p: p.get("_status") == "guideline_listed", None),
    ]
    if with_manual:
        spec.append(("manual", lambda p: bool(p.get("_manual")),
                     lambda p: "; ".join(manual_reason_text(r, lang) for r in p["_manual"])))
    out = []
    for key, pred, note_fn in spec:
        es = entries(pred, note_fn)
        if es:
            title, intro = L["app"][key]
            out.append((key, title, intro.format(top=top if top else "N"), es))
    return out


# ---------------------------------------------------------------------------
# Short edition ("The week at a glance") - Cyril's format, October 2026:
# number bar + design tag, title, journal + date, WHY IT MATTERS, KEY FINDING,
# three figure tiles (every figure checked against the abstract), PMID link;
# "Also on our radar" (max 5) and a short source-standards box at the end. A4.
# ---------------------------------------------------------------------------

JOURNAL_DISPLAY = {
    "circulation": "Circulation", "j am coll cardiol": "Journal of the American College of Cardiology",
    "eur heart j": "European Heart Journal", "jama cardiol": "JAMA Cardiology",
    "nat rev cardiol": "Nature Reviews Cardiology", "eur j heart fail": "European Journal of Heart Failure",
    "circ heart fail": "Circulation: Heart Failure", "jacc heart fail": "JACC: Heart Failure",
    "circ arrhythm electrophysiol": "Circulation: Arrhythmia and Electrophysiology",
    "europace": "EP Europace", "heart rhythm": "Heart Rhythm",
    "jacc clin electrophysiol": "JACC: Clinical Electrophysiology", "eurointervention": "EuroIntervention",
    "circ cardiovasc interv": "Circulation: Cardiovascular Interventions",
    "jacc cardiovasc interv": "JACC: Cardiovascular Interventions",
    "circ cardiovasc imaging": "Circulation: Cardiovascular Imaging",
    "eur heart j cardiovasc imaging": "European Heart Journal - Cardiovascular Imaging",
    "jacc cardiovasc imaging": "JACC: Cardiovascular Imaging",
    "j am heart assoc": "Journal of the American Heart Association", "heart": "Heart",
    "n engl j med": "New England Journal of Medicine", "lancet": "The Lancet", "jama": "JAMA",
    "bmj": "BMJ", "ann intern med": "Annals of Internal Medicine", "nat med": "Nature Medicine",
    "eur j cardiothorac surg": "European Journal of Cardio-Thoracic Surgery",
    "j thorac cardiovasc surg": "Journal of Thoracic and Cardiovascular Surgery",
    "ann thorac surg": "The Annals of Thoracic Surgery",
    "interact cardiovasc thorac surg": "Interactive CardioVascular and Thoracic Surgery",
    "j am soc echocardiogr": "Journal of the American Society of Echocardiography",
    "catheter cardiovasc interv": "Catheterization and Cardiovascular Interventions",
    "cardiovasc revasc med": "Cardiovascular Revascularization Medicine",
    "struct heart": "Structural Heart", "j cardiovasc magn reson": "Journal of Cardiovascular Magnetic Resonance",
    "eur j prev cardiol": "European Journal of Preventive Cardiology",
}

_MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August",
           "September", "October", "November", "December"]
_MONTHS_DE = ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August",
              "September", "Oktober", "November", "Dezember"]


def journal_display(p):
    """Full journal name for the reader ('JACC: Cardiovascular Imaging'), never the
    PubMed abbreviation if we know better."""
    j = p.get("journal") or ""
    return JOURNAL_DISPLAY.get(_jnorm(j)) or (p.get("journal_full") or "").strip() or j


def display_date(p, lang="en"):
    """Online date if PubMed has it (that is when readers first saw the paper), else the
    issue date. 'Oct 5, 2026' / '5. Okt. 2026'; month-only dates stay month-only."""
    e = p.get("epub_date") or ""
    m = re.match(r"(\d{4})-(\d{1,2})-(\d{1,2})$", e)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    else:
        parts = (p.get("pub_date") or "").split()
        if not parts or not parts[0].isdigit():
            return p.get("pub_date") or ""
        y = int(parts[0])
        mo = next((i + 1 for i, n in enumerate(_MONTHS) if len(parts) > 1 and n[:3].lower() == parts[1][:3].lower()), 0)
        d = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
        if not mo:
            return p.get("pub_date") or ""
    if lang == "de":
        mon = _MONTHS_DE[mo - 1][:3] + ("." if len(_MONTHS_DE[mo - 1]) > 3 else "")
        return f"{d}. {mon} {y}" if d else f"{mon} {y}"
    return f"{_MONTHS[mo - 1][:3]} {d}, {y}" if d else f"{_MONTHS[mo - 1][:3]} {y}"


def long_date(date_str, lang="en"):
    try:
        d = dt.date.fromisoformat(date_str)
    except Exception:
        return date_str
    return f"{d.day}. {_MONTHS_DE[d.month - 1]} {d.year}" if lang == "de" else f"{_MONTHS[d.month - 1]} {d.day}, {d.year}"


def _numbers_in(text):
    t = re.sub(r"(?<=\d)[,'’   ](?=\d{3}(?!\d))", "", str(text or ""))
    out = set()
    for tok in re.findall(r"\d+(?:\.\d+)?", t):
        try:
            out.add(float(tok))
        except ValueError:
            pass
    return out


def verify_tiles(tiles, abstract):
    """Keep only tiles whose every number (value and context) appears in the abstract.
    Big numbers are what readers remember - they must never be invented or rounded."""
    have = _numbers_in(abstract)
    kept, dropped = [], []
    for t in tiles or []:
        nums = _numbers_in(t.get("value")) | _numbers_in(t.get("context"))
        (kept if nums and nums <= have else dropped).append(t)
    return kept, dropped


def tiles_fallback(kn):
    """Three tiles from key_numbers when the model gave none (older cache entries)."""
    out = []
    pe = (kn or {}).get("primary_endpoint") or {}
    if pe.get("arm1_value"):
        v = pe["arm1_value"] + (f" vs. {pe['arm2_value']}" if pe.get("arm2_value") else "")
        ctx = f"{pe.get('arm1_label') or ''} vs. {pe.get('arm2_label') or ''}" if pe.get("arm2_value") else (pe.get("arm1_label") or "")
        out.append({"value": v, "label": pe.get("name") or "", "context": ctx.strip(" vs.")})
    if pe.get("effect"):
        out.append({"value": pe["effect"].split("(")[0].strip(), "label": "Effect", "context": pe["effect"]})
    for f in (kn or {}).get("key_findings") or []:
        out.append({"value": f.get("value"), "label": f.get("label"), "context": ""})
    return [t for t in out if t.get("value") and t.get("label")][:3]


SHORT_DISCLAIMER = {
    "en": ("CLARITY is an independent medical literature curation service and is not affiliated with "
           "or endorsed by the journals or societies named. This document is provided solely for "
           "information and continuing education and does not constitute medical advice, a treatment "
           "recommendation, or clinical guidance. AI-generated summaries may contain errors; the "
           "original publication remains the authoritative source."),
    "de": ("CLARITY ist ein unabhängiger Dienst zur Kuratierung medizinischer Fachliteratur und steht in "
           "keiner Verbindung zu den genannten Fachzeitschriften oder Fachgesellschaften. Dieses Dokument "
           "dient ausschliesslich der Information und Fortbildung und stellt keine medizinische Beratung, "
           "Behandlungsempfehlung oder klinische Handlungsanleitung dar. KI-erstellte Zusammenfassungen "
           "können Fehler enthalten; massgeblich ist die Originalpublikation."),
}

SHORT_L = {
    "en": {"tagline": "REAL INSIGHTS IN SECONDS", "glance": "THE WEEK AT A GLANCE",
           "glance_days": "THE LAST {days} DAYS AT A GLANCE",
           "count": "{n} selected publication", "count_n": "{n} selected publications",
           "curated": "CURATED", "essentials": "Only the essentials", "why": "WHY IT MATTERS",
           "key": "KEY FINDING", "view": "View on PubMed", "radar": "ALSO ON OUR RADAR",
           "radar_sub": "Named, not summarised - see the long edition for the full list.",
           "source": "SOURCE STANDARDS", "edition": "Short Edition",
           "tag_guideline": "Guideline / consensus", "tag_pending": "Abstract pending",
           "tag_further": "Further study"},
    "de": {"tagline": "ECHTE ERKENNTNISSE IN SEKUNDEN", "glance": "DIE WOCHE AUF EINEN BLICK",
           "glance_days": "DIE LETZTEN {days} TAGE AUF EINEN BLICK",
           "count": "{n} ausgewählte Publikation", "count_n": "{n} ausgewählte Publikationen",
           "curated": "KURATIERT", "essentials": "Nur das Wesentliche", "why": "WARUM ES ZÄHLT",
           "key": "KERNBEFUND", "view": "In PubMed ansehen", "radar": "AUSSERDEM AUF UNSEREM RADAR",
           "radar_sub": "Genannt, nicht zusammengefasst - die vollständige Liste steht in der Langversion.",
           "source": "QUELLENSTANDARDS", "edition": "Kurzversion",
           "tag_guideline": "Leitlinie / Konsensus", "tag_pending": "Abstract folgt",
           "tag_further": "Weitere Studie"},
}
RADAR_MAX = 5


def short_view(p, lang):
    """Everything one study block of the short edition shows (p = display copy)."""
    s = p.get("summary") or {}
    sc = p.get("score") or {}
    kn = s.get("key_numbers") or {}
    tag = (s.get("design_tag") or "").strip()
    if not tag:
        n = kn.get("n")
        tag = type_label(sc.get("study_type"), lang) + (f" · N = {n}" if n else "")
    why = (s.get("why_it_matters") or "").strip() or _first_sentences(s.get("short") or "", 1)
    key = (s.get("key_finding") or "").strip()
    if not key:
        sents = split_sentences(s.get("short") or s.get("summary") or "")
        key = " ".join(sents[1:3] if len(sents) > 1 else sents[:1])
    return {"tag": tag.upper(), "title": p.get("title", ""),
            "meta": "  ·  ".join(x for x in (journal_display(p), display_date(p, lang)) if x),
            "why": why, "key": key, "tiles": s.get("tiles") or [],
            "pmid": p.get("pmid", ""), "url": paper_url(p)}


def radar_entries(appendices, lang, n=RADAR_MAX):
    """Up to n titles for 'Also on our radar': guidelines first, then further studies,
    then papers whose abstract is pending (manual-review items never appear here)."""
    S = SHORT_L[lang]
    by = {k: es for k, _t, _i, es in (appendices or [])}
    out = []
    for key, tag in (("guidelines", S["tag_guideline"]), ("further", S["tag_further"]),
                     ("pending", S["tag_pending"])):
        for e in by.get(key, []):
            if len(out) >= n:
                return out
            out.append(dict(e, tag=tag))
    return out


def build_short_pdf(items, date_str, days, out_path, lang="en", appendices=None, focus=None):
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.lib import colors
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.enums import TA_CENTER
    from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
                                    KeepTogether)
    L, S = LABELS[lang], SHORT_L[lang]
    C = colors.HexColor
    W, H = A4
    M = 15 * mm
    CW = W - 2 * M
    fname = field_name(lang, focus)
    fm = front_matter(items, lang, days, focus)
    groups = group_items(items)
    ordered = [p for _g, ps in groups for p in ps]

    def ps(name, **kw):
        base = dict(fontName="Helvetica", fontSize=9, leading=12.5, textColor=C(_INK))
        base.update(kw)
        return ParagraphStyle(name, **base)
    st = {
        "brand": ps("br", fontName="Times-Bold", fontSize=30, leading=34, textColor=C(_CREAM), alignment=TA_CENTER),
        "tag": ps("tg", fontName="Helvetica", fontSize=8, leading=10, textColor=C(_MINT), alignment=TA_CENTER),
        "glance": ps("gl", fontName="Helvetica-Bold", fontSize=8, leading=11, textColor=C(_MINT), alignment=TA_CENTER),
        "field": ps("fi", fontName="Times-Bold", fontSize=18, leading=22, textColor=C(_TEAL_D), alignment=TA_CENTER),
        "date": ps("da", fontName="Helvetica", fontSize=13, leading=17, textColor=C(_INK), alignment=TA_CENTER),
        "cur": ps("cu", fontName="Helvetica-Bold", fontSize=7, leading=9, textColor=C(_TEAL_D), alignment=TA_CENTER),
        "cur2": ps("c2", fontName="Helvetica", fontSize=7, leading=9, textColor=C(_MUTE), alignment=TA_CENTER),
        "quiet": ps("qu", fontName="Helvetica-Oblique", fontSize=9, textColor=C(_MUTE), alignment=TA_CENTER),
        "num": ps("nu", fontName="Times-Bold", fontSize=13, leading=15, textColor=C(_CREAM), alignment=TA_CENTER),
        "kick": ps("ki", fontName="Helvetica-Bold", fontSize=7, leading=9, textColor=C(_MINT)),
        "title": ps("ti", fontName="Times-Bold", fontSize=11.5, leading=14.5, textColor=C(_INK), spaceBefore=5),
        "meta": ps("me", fontName="Helvetica", fontSize=7.5, leading=10, textColor=C(_MUTE), spaceAfter=5),
        "lab": ps("la", fontName="Helvetica-Bold", fontSize=7, leading=9, textColor=C(_MINT)),
        "body": ps("bo", fontSize=9, leading=12.5, spaceAfter=4),
        "tv": ps("tv", fontName="Helvetica-Bold", fontSize=10.5, leading=13, textColor=C(_TEAL_D), alignment=TA_CENTER),
        "tl": ps("tl", fontName="Helvetica-Bold", fontSize=6.3, leading=8, textColor=C(_MINT), alignment=TA_CENTER),
        "tc": ps("tc", fontName="Helvetica", fontSize=6.3, leading=8, textColor=C(_MUTE), alignment=TA_CENTER),
        "pmid": ps("pm", fontName="Helvetica", fontSize=7.5, leading=10, textColor=C(_MUTE), spaceBefore=4),
        "sec": ps("se", fontName="Helvetica-Bold", fontSize=8, leading=11, textColor=C(_MINT)),
        "rt": ps("rt", fontName="Helvetica-Bold", fontSize=8.8, leading=11.5),
        "rm": ps("rm", fontName="Helvetica", fontSize=7.3, leading=9.5, textColor=C(_MUTE), spaceAfter=4),
        "src": ps("sr", fontName="Helvetica-Bold", fontSize=7, leading=9, textColor=C(_TEAL_D)),
        "disc": ps("di", fontName="Helvetica-Oblique", fontSize=6.8, leading=9, textColor=C(_MUTE)),
    }

    def footer(canv, doc_):
        canv.saveState()
        canv.setFont("Helvetica", 6.5)
        canv.setFillColor(C(_MUTE))
        canv.drawCentredString(W / 2, 9 * mm, _pdf_clean(
            f"{BRAND}  ·  {fname}  ·  {S['edition']}  ·  {display_date({'epub_date': date_str}, lang)}"))
        canv.restoreState()

    def box(rows, bg, pad=8, widths=None, style_extra=()):
        t = Table(rows, colWidths=widths or [CW])
        t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), C(bg)),
                               ("LEFTPADDING", (0, 0), (-1, -1), pad), ("RIGHTPADDING", (0, 0), (-1, -1), pad),
                               ("TOPPADDING", (0, 0), (-1, -1), pad * 0.8), ("BOTTOMPADDING", (0, 0), (-1, -1), pad * 0.8),
                               ("VALIGN", (0, 0), (-1, -1), "MIDDLE")] + list(style_extra)))
        return t

    story = [box([[[Paragraph(BRAND, st["brand"]), Spacer(1, 2), Paragraph(_esc(S["tagline"]), st["tag"])]]],
                 _TEAL, pad=14),
             Spacer(1, 12),
             Paragraph(_esc(S["glance"] if days <= 8 else S["glance_days"].format(days=days)), st["glance"]),
             Spacer(1, 3), Paragraph(_esc(fname), st["field"]), Spacer(1, 2),
             Paragraph(_esc(f"{long_date(date_str, lang)} · " +
                            (S["count"] if len(ordered) == 1 else S["count_n"]).format(n=len(ordered))),
                       st["date"]),
             Spacer(1, 8)]
    cur = Table([["", [Paragraph(_esc(S["curated"]), st["cur"]), Paragraph(_esc(S["essentials"]), st["cur2"])], ""]],
                colWidths=[CW / 3] * 3)
    cur.setStyle(TableStyle([("BACKGROUND", (1, 0), (1, 0), C(_MINT_BG)),
                             ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5)]))
    story += [cur, Spacer(1, 10)]
    if fm["quiet"]:
        story += [Paragraph(_esc(fm["quiet"]), st["quiet"]), Spacer(1, 8)]
    if not ordered:
        story += [Paragraph(_esc(L["none_title"]), st["rt"]), Paragraph(_esc(L["none_text"]), st["body"])]

    for i, p in enumerate(ordered, 1):
        v = short_view(p, lang)
        bar = Table([[Paragraph(f"{i:02d}", st["num"]), Paragraph(_esc(v["tag"]), st["kick"])]],
                    colWidths=[CW / 2, CW / 2])
        bar.setStyle(TableStyle([("BACKGROUND", (0, 0), (0, 0), C(_TEAL)), ("BACKGROUND", (1, 0), (1, 0), C("#F1F6F4")),
                                 ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                                 ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                                 ("LEFTPADDING", (1, 0), (1, 0), 8)]))
        block = [bar, Paragraph(_esc(v["title"]), st["title"]), Paragraph(_esc(v["meta"]), st["meta"])]
        if v["why"]:
            block += [Paragraph(_esc(S["why"]), st["lab"]), Paragraph(_esc(v["why"]), st["body"])]
        if v["key"]:
            block += [Paragraph(_esc(S["key"]), st["lab"]), Paragraph(_esc(v["key"]), st["body"])]
        if v["tiles"]:
            cells = [[Paragraph(_esc(t["value"]), st["tv"]), Paragraph(_esc(str(t["label"]).upper()), st["tl"]),
                      Paragraph(_esc(t.get("context") or ""), st["tc"])] for t in v["tiles"]]
            n = len(cells)
            tt = Table([cells], colWidths=[CW / n] * n)
            tt.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), C(_MINT_BG)),
                                    ("LINEAFTER", (0, 0), (-2, -1), 2, C(_CREAM)),
                                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                                    ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6)]))
            block += [Spacer(1, 3), tt]
        block.append(Paragraph(f'PMID {_esc(v["pmid"])}   ·   <link href="{_esc(v["url"])}">'
                               f'<font color="{_MINT}"><u>{_esc(S["view"])}</u></font></link>', st["pmid"]))
        story += [KeepTogether(block), Spacer(1, 12)]

    radar = radar_entries(appendices, lang)
    if radar:
        rb = [Paragraph(_esc(S["radar"]), st["sec"]), Paragraph(_esc(S["radar_sub"]), st["rm"])]
        for e in radar:
            rb += [Paragraph(_esc(e["title"]), st["rt"]),
                   Paragraph(f'{_esc(e["tag"])}  ·  {_esc(e.get("jdate") or e["meta"])}  ·  <link href="{_esc(e["url"])}">'
                             f'<font color="{_MINT}"><u>PubMed</u></font></link>', st["rm"])]
        story += [KeepTogether(rb), Spacer(1, 10)]
    story.append(box([[[Paragraph(_esc(S["source"]), st["src"]), Spacer(1, 3),
                        Paragraph(_esc(SHORT_DISCLAIMER[lang]), st["disc"])]]], "#FBF4E8", pad=9))

    doc = SimpleDocTemplate(out_path, pagesize=A4, leftMargin=M, rightMargin=M, topMargin=13 * mm,
                            bottomMargin=16 * mm, title=f"{BRAND} - {fname} - {S['edition']} - {date_str}",
                            author=BRAND)
    doc.build(story, onFirstPage=footer, onLaterPages=footer)


def build_short_docx(items, date_str, days, out_path, lang="en", appendices=None, focus=None):
    from docx import Document
    from docx.shared import Pt, Mm, RGBColor
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.enum.table import WD_TABLE_ALIGNMENT
    from docx.oxml.ns import qn
    from docx.oxml import OxmlElement
    from docx.opc.constants import RELATIONSHIP_TYPE as RT

    L, S = LABELS[lang], SHORT_L[lang]
    TEAL, TEAL_D, MINT, MINT_BG, CREAM, INK, MUTE = "0F3D3A", "0A2C2A", "2FB89A", "E8F5EF", "F7F4ED", "16302E", "5C7370"
    fname = field_name(lang, focus)
    fm = front_matter(items, lang, days, focus)
    ordered = [p for _g, ps_ in group_items(items) for p in ps_]

    doc = Document()
    sec = doc.sections[0]
    sec.page_width, sec.page_height = Mm(210), Mm(297)
    sec.left_margin = sec.right_margin = Mm(15)
    sec.top_margin, sec.bottom_margin = Mm(14), Mm(14)
    CW = sec.page_width - sec.left_margin - sec.right_margin
    base = doc.styles["Normal"]
    base.font.name = "Arial"
    base.font.size = Pt(9)
    base.font.color.rgb = RGBColor.from_string(INK)
    base.paragraph_format.space_after = Pt(0)
    try:
        lg = OxmlElement("w:lang"); lg.set(qn("w:val"), "de-CH" if lang == "de" else "en-GB")
        base.element.get_or_add_rPr().append(lg)
    except Exception:
        pass

    def shade(cell, fill):
        tcPr = cell._tc.get_or_add_tcPr()
        shd = OxmlElement("w:shd")
        shd.set(qn("w:val"), "clear"); shd.set(qn("w:color"), "auto"); shd.set(qn("w:fill"), fill)
        tcPr.append(shd)

    def margins(cell, top=90, side=120):
        tcPr = cell._tc.get_or_add_tcPr()
        m = OxmlElement("w:tcMar")
        for k, v in (("top", top), ("bottom", top), ("left", side), ("right", side)):
            e = OxmlElement(f"w:{k}"); e.set(qn("w:w"), str(v)); e.set(qn("w:type"), "dxa"); m.append(e)
        tcPr.append(m)

    def table(cols, widths, fills):
        t = doc.add_table(rows=1, cols=cols)
        t.alignment = WD_TABLE_ALIGNMENT.CENTER
        t.autofit = False
        b = OxmlElement("w:tblBorders")
        for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
            e = OxmlElement(f"w:{edge}"); e.set(qn("w:val"), "nil"); b.append(e)
        t._tbl.tblPr.append(b)
        tr = t.rows[0]._tr.get_or_add_trPr(); tr.append(OxmlElement("w:cantSplit"))
        for c, w, f in zip(t.rows[0].cells, widths, fills):
            c.width = w
            if f:
                shade(c, f)
            margins(c)
        return t

    def run(par, text, size=9, bold=False, color=None, italic=False, font=None):
        r = par.add_run(text)
        r.font.size = Pt(size); r.bold = bold; r.italic = italic
        if color:
            r.font.color.rgb = RGBColor.from_string(color)
        if font:
            r.font.name = font
            r._element.get_or_add_rPr().get_or_add_rFonts().set(qn("w:hAnsi"), font)
        return r

    def para(text="", size=9, bold=False, color=None, italic=False, font=None, align=None,
             before=0, after=0, keep=False, container=None):
        par = (container or doc).add_paragraph()
        pf = par.paragraph_format
        pf.space_before, pf.space_after = Pt(before), Pt(after)
        if keep:
            pf.keep_with_next = True
        if align is not None:
            par.alignment = align
        if text:
            run(par, text, size, bold, color, italic, font)
        return par

    def cell_para(cell, text, first=False, **kw):
        if first:
            par = cell.paragraphs[0]
            pf = par.paragraph_format
            pf.space_before, pf.space_after = Pt(kw.pop("before", 0)), Pt(kw.pop("after", 0))
            if kw.get("align") is not None:
                par.alignment = kw.pop("align")
            else:
                kw.pop("align", None)
            run(par, text, **kw)
            return par
        return para(text, container=cell, **kw)

    def link(par, url, text, size=7.5):
        rid = par.part.relate_to(url, RT.HYPERLINK, is_external=True)
        h = OxmlElement("w:hyperlink"); h.set(qn("r:id"), rid)
        r = OxmlElement("w:r"); rpr = OxmlElement("w:rPr")
        c = OxmlElement("w:color"); c.set(qn("w:val"), MINT); rpr.append(c)
        u = OxmlElement("w:u"); u.set(qn("w:val"), "single"); rpr.append(u)
        sz = OxmlElement("w:sz"); sz.set(qn("w:val"), str(int(size * 2))); rpr.append(sz)
        bd = OxmlElement("w:b"); rpr.append(bd)
        r.append(rpr); t = OxmlElement("w:t"); t.text = text; r.append(t); h.append(r)
        par._p.append(h)

    C_ = WD_ALIGN_PARAGRAPH.CENTER
    # footer
    fp = sec.footer.paragraphs[0]; fp.alignment = C_
    run(fp, f"{BRAND}  ·  {fname}  ·  {S['edition']}  ·  {display_date({'epub_date': date_str}, lang)}", 6.5, color=MUTE)

    # header block
    t = table(1, [CW], [TEAL])
    c = t.rows[0].cells[0]; margins(c, 260, 200)
    cell_para(c, BRAND, first=True, size=29, bold=True, color=CREAM, font="Georgia", align=C_)
    cell_para(c, S["tagline"], size=8, color=MINT, align=C_)
    para(S["glance"] if days <= 8 else S["glance_days"].format(days=days), 8, True, MINT, align=C_, before=12, after=2)
    para(fname, 18, True, TEAL_D, font="Georgia", align=C_, after=2)
    para(f"{long_date(date_str, lang)} · " + (S["count"] if len(ordered) == 1 else S["count_n"]).format(n=len(ordered)),
         13, align=C_, after=8)
    t = table(3, [CW // 3] * 3, [None, MINT_BG, None])
    mc = t.rows[0].cells[1]
    cell_para(mc, S["curated"], first=True, size=7, bold=True, color=TEAL_D, align=C_)
    cell_para(mc, S["essentials"], size=7, color=MUTE, align=C_)
    para("", after=6)
    if fm["quiet"]:
        para(fm["quiet"], 9, italic=True, color=MUTE, align=C_, after=8)
    if not ordered:
        para(L["none_title"], 10, True, after=2)
        para(L["none_text"], 9, after=8)

    for i, p in enumerate(ordered, 1):
        v = short_view(p, lang)
        t = table(2, [CW // 2, CW // 2], [TEAL, "F1F6F4"])
        cell_para(t.rows[0].cells[0], f"{i:02d}", first=True, size=13, bold=True, color=CREAM, font="Georgia", align=C_)
        cell_para(t.rows[0].cells[1], v["tag"], first=True, size=7, bold=True, color=MINT)
        para(v["title"], 11.5, True, font="Georgia", before=5, after=1, keep=True)
        para(v["meta"], 7.5, color=MUTE, after=5, keep=True)
        if v["why"]:
            para(S["why"], 7, True, MINT, after=1, keep=True)
            para(v["why"], 9, after=4, keep=True)
        if v["key"]:
            para(S["key"], 7, True, MINT, after=1, keep=True)
            para(v["key"], 9, after=4, keep=bool(v["tiles"]))
        if v["tiles"]:
            n = len(v["tiles"])
            t = table(n, [CW // n] * n, [MINT_BG] * n)
            for cc, tile in zip(t.rows[0].cells, v["tiles"]):
                cell_para(cc, tile["value"], first=True, size=10.5, bold=True, color=TEAL_D, align=C_)
                cell_para(cc, str(tile["label"]).upper(), size=6.5, bold=True, color=MINT, align=C_)
                if tile.get("context"):
                    cell_para(cc, tile["context"], size=6.5, color=MUTE, align=C_)
        lp = para(f"PMID {v['pmid']}   ·   ", 7.5, color=MUTE, before=4, after=12)
        if v["url"]:
            link(lp, v["url"], S["view"])

    radar = radar_entries(appendices, lang)
    if radar:
        para(S["radar"], 8, True, MINT, before=4, after=1, keep=True)
        para(S["radar_sub"], 7.3, color=MUTE, after=5, keep=True)
        for e in radar:
            para(e["title"], 8.8, True, after=1, keep=True)
            lp = para(f"{e['tag']}  ·  {e.get('jdate') or e['meta']}  ·  ", 7.3, color=MUTE, after=5)
            if e.get("url"):
                link(lp, e["url"], "PubMed", 7.3)
    t = table(1, [CW], ["FBF4E8"])
    c = t.rows[0].cells[0]
    cell_para(c, S["source"], first=True, size=7, bold=True, color=TEAL_D, after=2)
    cell_para(c, SHORT_DISCLAIMER[lang], size=6.8, italic=True, color=MUTE)
    doc.core_properties.title = f"{BRAND} - {fname} - {S['edition']} - {date_str}"
    doc.core_properties.author = BRAND
    doc.save(out_path)


def build_pdf(items, date_str, days, out_path, lang=None, edition="lang", appendices=None,
              focus=None, top=None):
    if edition == "kurz":
        return build_short_pdf(items, date_str, days, out_path, lang or OUTPUT_LANG, appendices, focus)
    """Shareable CLARITY PDF.
      edition="lang": dark cover, contents, one full page per study (key-figures panel,
                      endpoint bar chart, secondary-outcomes table, relation to evidence and
                      guidelines, authors' conclusion).
      edition="kurz": dark cover, then a compact block per study (2-3 sentences).
    Appendices (pending / further / guidelines / editorial review) come from
    appendix_model(). No abstracts are reproduced and no figures are made up."""
    from functools import partial
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.lib import colors
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.enums import TA_LEFT
    from reportlab.platypus import (BaseDocTemplate, PageTemplate, Frame, Paragraph,
                                    Spacer, PageBreak, Table, TableStyle, Flowable,
                                    NextPageTemplate, KeepTogether)

    lang = lang or OUTPUT_LANG
    L = LABELS[lang]
    fname = field_name(lang, focus)
    appendices = appendices or []
    W, H = A4
    C = colors.HexColor

    groups = group_items(items)
    total = sum(len(ps) for _, ps in groups)
    meta = {"date": date_str, "days": days, "n": total, "ng": len(groups)}

    # ---- custom flowables ----------------------------------------------------
    class RankBadge(Flowable):
        def __init__(self, n, d=11 * mm): super().__init__(); self.n = n; self.d = d
        def wrap(self, aw, ah): return (self.d, self.d)
        def draw(self):
            c = self.canv; c.setFillColor(C(_MINT)); c.circle(self.d / 2, self.d / 2, self.d / 2, fill=1, stroke=0)
            c.setFillColor(colors.white); c.setFont("Times-Bold", 12 if self.d >= 10 * mm else 10.5)
            c.drawCentredString(self.d / 2, self.d / 2 - 4.2, str(self.n))

    class HR(Flowable):
        def __init__(self, col=_HAIR, lw=0.6): super().__init__(); self.col = col; self.lw = lw
        def wrap(self, aw, ah): self._w = aw; return (aw, self.lw)
        def draw(self):
            self.canv.setStrokeColor(C(self.col)); self.canv.setLineWidth(self.lw); self.canv.line(0, 0, self._w, 0)

    class EndpointBars(Flowable):
        def __init__(self, d1, v1, l1, d2, v2, l2, width=74 * mm, height=42 * mm):
            super().__init__()
            self.d1, self.v1, self.l1 = _pdf_clean(d1), v1, _pdf_clean(l1)
            self.d2, self.v2, self.l2 = _pdf_clean(d2), v2, _pdf_clean(l2)
            self.width, self.height = width, height
        def wrap(self, aw, ah): return (self.width, self.height)
        def draw(self):
            c = self.canv; maxv = max(self.v1, self.v2, 1e-6)
            base = 18; top_ = self.height - 15; bh = top_ - base; bw = self.width * 0.24
            for x, v, disp, lab, col in [(self.width * 0.22, self.v1, self.d1, self.l1, C(_TEAL)),
                                         (self.width * 0.56, self.v2, self.d2, self.l2, C(_MINT))]:
                h = max(bh * (v / maxv), 1.5)
                c.setFillColor(col); c.roundRect(x, base, bw, h, 2.5, fill=1, stroke=0)
                c.setFillColor(C(_INK)); c.setFont("Helvetica-Bold", 9.5)
                c.drawCentredString(x + bw / 2, base + h + 4, disp)
                c.setFillColor(C(_MUTE)); c.setFont("Helvetica", 7.5)
                c.drawCentredString(x + bw / 2, base - 10, lab[:22])
            c.setStrokeColor(C(_HAIR)); c.setLineWidth(0.6); c.line(6, base, self.width - 6, base)

    st = {
        "sublabel": ParagraphStyle("sl", fontName="Helvetica-Bold", fontSize=8.5, textColor=C(_MINT),
                                   leading=11, spaceAfter=3, keepWithNext=1),
        "title": ParagraphStyle("t", fontName="Times-Bold", fontSize=15, textColor=C(_INK),
                                leading=18, spaceAfter=1),
        "title_s": ParagraphStyle("ts", fontName="Times-Bold", fontSize=12.5, textColor=C(_INK),
                                  leading=15, spaceAfter=1),
        "meta": ParagraphStyle("m", fontName="Helvetica", fontSize=8.5, textColor=C(_MUTE), leading=11),
        "kicker": ParagraphStyle("k", fontName="Helvetica-Bold", fontSize=8, textColor=C(_TEAL),
                                 leading=11, spaceBefore=2, spaceAfter=2),
        "label": ParagraphStyle("l", fontName="Helvetica-Bold", fontSize=8, textColor=C(_MINT),
                                leading=11, spaceBefore=7, spaceAfter=1, keepWithNext=1),
        "body": ParagraphStyle("b", fontName="Helvetica", fontSize=9.5, textColor=C(_INK),
                               leading=13.6, alignment=TA_LEFT),
        "short": ParagraphStyle("sh", fontName="Helvetica", fontSize=10, textColor=C(_INK),
                                leading=14.4, alignment=TA_LEFT, spaceBefore=3),
        "statval": ParagraphStyle("sv", fontName="Helvetica-Bold", fontSize=11.5, textColor=C(_TEAL),
                                  leading=13),
        "statlab": ParagraphStyle("sk", fontName="Helvetica", fontSize=7, textColor=C(_MUTE), leading=8.5),
        "pelabel": ParagraphStyle("pl", fontName="Helvetica-Bold", fontSize=8, textColor=C(_TEAL_D),
                                  leading=11, spaceAfter=3),
        "effect": ParagraphStyle("ef", fontName="Helvetica", fontSize=8.8, textColor=C(_INK), leading=12.5),
        "peline": ParagraphStyle("pe", fontName="Helvetica", fontSize=8.8, textColor=C(_TEAL_D),
                                 leading=12.3, spaceBefore=4),
        "th": ParagraphStyle("th", fontName="Helvetica-Bold", fontSize=7.6, textColor=colors.white, leading=9.5),
        "td": ParagraphStyle("td", fontName="Helvetica", fontSize=8, textColor=C(_INK), leading=10.5),
        "tdb": ParagraphStyle("tb", fontName="Helvetica-Bold", fontSize=8, textColor=C(_INK), leading=10.5),
        "keylabel": ParagraphStyle("kl", fontName="Helvetica-Bold", fontSize=8, textColor=C(_TEAL_D),
                                   leading=11, spaceAfter=2),
        "keybody": ParagraphStyle("kb", fontName="Helvetica-Bold", fontSize=9.8, textColor=C(_TEAL_D), leading=13.8),
        "link": ParagraphStyle("ln", fontName="Helvetica", fontSize=8, textColor=C(_MINT), leading=11, spaceBefore=4),
        "section": ParagraphStyle("s", fontName="Times-Bold", fontSize=15, textColor=C(_TEAL), leading=18,
                                  spaceBefore=2, spaceAfter=2, keepWithNext=1),
        "ov": ParagraphStyle("ov", fontName="Helvetica", fontSize=10, textColor=C(_INK), leading=16),
        "gtitle": ParagraphStyle("g", fontName="Helvetica-Bold", fontSize=9.5, textColor=C(_INK), leading=13,
                                 keepWithNext=1),
        "note": ParagraphStyle("nt", fontName="Helvetica-Oblique", fontSize=8, textColor=C(_MUTE), leading=10.5),
        "disc": ParagraphStyle("d", fontName="Helvetica-Oblique", fontSize=7.3, textColor=C(_MUTE),
                               leading=10, spaceBefore=4),
    }
    FULLW = W - 36 * mm  # content frame width

    def P(txt, style):
        return Paragraph(_esc(txt), st[style])

    def stat_row(kn):
        cells = [[P(v, "statval"), Paragraph(_esc(lab), st["statlab"])] for v, lab in stat_cells(kn, lang)]
        if not cells:
            return []
        t = Table([cells], colWidths=[FULLW / len(cells)] * len(cells))
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), C("#F0F6F3")),
            ("LEFTPADDING", (0, 0), (-1, -1), 9), ("RIGHTPADDING", (0, 0), (-1, -1), 9),
            ("TOPPADDING", (0, 0), (-1, -1), 7), ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("ROUNDEDCORNERS", [5, 5, 5, 5]),
            ("LINEAFTER", (0, 0), (-2, -1), 0.5, C("#DCE7E3")),
        ]))
        return [Spacer(1, 6), t]

    def arms_line(arms):
        bits = []
        for lab, val in arms:
            bits.append(f"<b>{_esc(lab)}:</b> {_esc(val)}" if lab else f"<b>{_esc(val)}</b>")
        return "   ·   ".join(bits)

    def primary_block(pv):
        if not pv:
            return []
        head = Paragraph(_esc(L["primary"] + " — " + pv["name"]), st["pelabel"])
        eff = P(pv["effect"], "effect") if pv["effect"] else Spacer(1, 1)
        if pv["chart"]:
            v1, v2 = pv["chart"]
            arm_vals = [a[1] for a in pv["arms"]]
            bars = EndpointBars(arm_vals[0], v1, pv["labels"][0], arm_vals[1], v2, pv["labels"][1],
                                width=76 * mm, height=42 * mm)
            row = Table([[bars, eff]], colWidths=[80 * mm, FULLW - 80 * mm])
            row.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                                     ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (0, 0), 6)]))
            inner = [head, row]
        else:
            inner = [head]
            if pv["arms"]:
                inner.append(Paragraph(arms_line(pv["arms"]), st["effect"]))
                inner.append(Spacer(1, 3))
            inner.append(eff)
        box = Table([[inner]], colWidths=[FULLW])
        box.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), C(_MINT_BG)),
            ("LEFTPADDING", (0, 0), (-1, -1), 10), ("RIGHTPADDING", (0, 0), (-1, -1), 10),
            ("TOPPADDING", (0, 0), (-1, -1), 8), ("BOTTOMPADDING", (0, 0), (-1, -1), 9),
            ("ROUNDEDCORNERS", [5, 5, 5, 5]),
        ]))
        return [Spacer(1, 6), box]

    def secondary_table(sv):
        if not sv:
            return []
        cols, rows = sv
        data = [[Paragraph(_esc(h), st["th"]) for _, h, _w in cols]]
        for r in rows:
            data.append([Paragraph(_esc(_cell(r.get(k))), st["tdb"] if k in ("arm1", "arm2") else st["td"])
                         for k, _h, _w in cols])
        t = Table(data, colWidths=[FULLW * w for _k, _h, w in cols])
        style = [("BACKGROUND", (0, 0), (-1, 0), C(_TEAL)),
                 ("LEFTPADDING", (0, 0), (-1, -1), 6), ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                 ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                 ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                 ("LINEBELOW", (0, 0), (-1, -1), 0.4, C("#E1EAE7"))]
        for i in range(1, len(data)):
            if i % 2 == 0:
                style.append(("BACKGROUND", (0, i), (-1, i), C("#F5F9F7")))
        t.setStyle(TableStyle(style))
        return [Spacer(1, 7), Paragraph(_esc(L["other_ep"]), st["label"]), Spacer(1, 2), t]

    def findings_table(rows):
        """Fallback visual for papers without a two-arm comparison."""
        if not rows:
            return []
        data = [[P(a, "td"), P(b, "tdb")] for a, b in rows]
        t = Table(data, colWidths=[FULLW * 0.55, FULLW * 0.45])
        style = [("LEFTPADDING", (0, 0), (-1, -1), 6), ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                 ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                 ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                 ("LINEBELOW", (0, 0), (-1, -1), 0.4, C("#E1EAE7"))]
        for i in range(len(data)):
            if i % 2 == 1:
                style.append(("BACKGROUND", (0, i), (-1, i), C("#F5F9F7")))
        t.setStyle(TableStyle(style))
        return [Spacer(1, 7), Paragraph(_esc(L["key_figures"]), st["label"]), Spacer(1, 2), t]

    def header_block(p, i, small=False):
        metaline = _esc(journals_text(p)) + " · " + _esc(p.get("pub_date", "")) \
            + (f" · {_esc(authors_text(p))}" if authors_text(p) else "")
        d = 9 * mm if small else 11 * mm
        header = Table([[RankBadge(i, d), [Paragraph(_esc(p["title"]), st["title_s" if small else "title"]),
                                           Paragraph(metaline, st["meta"])]]],
                       colWidths=[d + 2 * mm, None])
        header.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (0, 0), 0), ("RIGHTPADDING", (0, 0), (0, 0), 0),
            ("LEFTPADDING", (1, 0), (1, 0), 4), ("RIGHTPADDING", (1, 0), (1, 0), 0),
            ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0)]))
        return header

    def link_line(p):
        return Paragraph(f'<link href="{_esc(paper_url(p))}"><u>{_esc(L["view"])}</u></link>'
                         f'  ·  {L["doi"]}: {_esc(p.get("doi") or "n/a")}', st["link"])

    def study_page(p, i, subgroup):
        s = p.get("summary", {}) or {}
        kn = s.get("key_numbers") or {}
        kick = Paragraph(f'<font color="{_MINT}">•</font>  ' + _esc(kicker_text(p, lang)).upper(), st["kicker"])
        flows = [Paragraph(_esc(group_label(subgroup, lang)).upper(), st["sublabel"]),
                 header_block(p, i), kick]
        flows += stat_row(kn)
        flows += primary_block(primary_view(kn.get("primary_endpoint"), lang))
        sv = secondary_view(kn, lang)
        flows += secondary_table(sv)
        if not sv:
            flows += findings_table(findings_view(kn))
        for lab, txt in text_sections(p, lang):
            flows += [Paragraph(_esc(lab), st["label"]), P(txt, "body")]
        concl = conclusion_text(p)
        if concl:
            kb = Table([[[Paragraph(_esc(L["concl"]), st["keylabel"]), P(concl, "keybody")]]], colWidths=[FULLW])
            kb.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, -1), C(_MINT_BG)),
                ("LEFTPADDING", (0, 0), (-1, -1), 9), ("RIGHTPADDING", (0, 0), (-1, -1), 9),
                ("TOPPADDING", (0, 0), (-1, -1), 7), ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
                ("ROUNDEDCORNERS", [5, 5, 5, 5])]))
            flows += [Spacer(1, 7), kb]
        flows += [link_line(p), PageBreak()]
        return flows

    def short_block(p, i):
        s = p.get("summary", {}) or {}
        kn = s.get("key_numbers") or {}
        kick = Paragraph(f'<font color="{_MINT}">•</font>  ' + _esc(kicker_text(p, lang, with_n=True)).upper(),
                         st["kicker"])
        flows = [header_block(p, i, small=True), kick, P(short_text(p), "short")]
        pv = primary_view(kn.get("primary_endpoint"), lang)
        if pv and pv["effect"]:
            flows.append(Paragraph(f'<b>{_esc(L["primary_short"])}</b> ({_esc(pv["name"])}): {_esc(pv["effect"])}',
                                   st["peline"]))
        flows += [link_line(p), Spacer(1, 5), HR(), Spacer(1, 9)]
        return [KeepTogether(flows)]

    # ---- page backgrounds ----------------------------------------------------
    def draw_cover(canvas, doc, meta):
        canvas.saveState()
        canvas.setFillColor(C(_TEAL)); canvas.rect(0, 0, W, H, fill=1, stroke=0)
        for k, base in enumerate((0.20, 0.145, 0.09)):
            canvas.setStrokeColor(C(_MINT)); canvas.setLineWidth(1.4 - 0.35 * k)
            path = canvas.beginPath(); y0 = H * base; path.moveTo(-10, y0)
            path.curveTo(W * 0.30, y0 + 26 + 10 * k, W * 0.62, y0 - 26 - 10 * k, W + 10, y0 + 8)
            canvas.drawPath(path, stroke=1, fill=0)
        canvas.setFillColor(C(_CREAM)); canvas.setFont("Times-Bold", 52)
        canvas.drawCentredString(W / 2 + 4, H * 0.63, BRAND)
        canvas.setFillColor(C(_MINT)); canvas.setFont("Helvetica", 12.5)
        canvas.drawCentredString(W / 2, H * 0.63 - 22, _pdf_clean(L["tagline"]))
        canvas.setFillColor(C(_CREAM)); canvas.setFont("Helvetica-Bold", 16)
        canvas.drawCentredString(W / 2, H * 0.40, _pdf_clean(L["cover_" + edition]))
        canvas.setFillColor(C("#9FC7BF")); canvas.setFont("Helvetica", 12)
        canvas.drawCentredString(W / 2, H * 0.40 - 20, _pdf_clean(
            f"{fname}   ·   {meta['date']}   ·   {count_text(meta['n'], meta['ng'], lang)}"))
        canvas.setFillColor(C(_MINT)); canvas.setFont("Helvetica-Bold", 9)
        canvas.drawCentredString(W / 2, H * 0.40 - 42, _pdf_clean(L["edition_" + edition]))
        canvas.restoreState()

    def draw_content(canvas, doc, meta):
        canvas.saveState()
        canvas.setFillColor(C(_CREAM)); canvas.rect(0, 0, W, H, fill=1, stroke=0)
        canvas.setFillColor(C(_MINT)); canvas.circle(18 * mm + 1.4, H - 15 * mm + 3, 2.0, fill=1, stroke=0)
        canvas.setFillColor(C(_TEAL)); canvas.setFont("Times-Bold", 10.5)
        canvas.drawString(18 * mm + 7, H - 15 * mm, BRAND)
        canvas.setFillColor(C(_MUTE)); canvas.setFont("Helvetica", 7)
        canvas.drawString(18 * mm, 10 * mm, _pdf_clean(f"{BRAND} · {fname} · {meta['date']}"))
        canvas.drawRightString(W - 18 * mm, 10 * mm, _pdf_clean(f"{L['page']} {doc.page - 1}"))
        canvas.restoreState()

    # ---- assemble ------------------------------------------------------------
    doc = BaseDocTemplate(out_path, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm,
                          topMargin=22 * mm, bottomMargin=16 * mm,
                          title=f"{BRAND} - {fname} - {date_str}", author=BRAND)
    cover_frame = Frame(0, 0, W, H, id="cov", leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)
    content_frame = Frame(doc.leftMargin, doc.bottomMargin, W - doc.leftMargin - doc.rightMargin,
                          H - doc.topMargin - doc.bottomMargin, id="con")
    doc.addPageTemplates([
        PageTemplate(id="cover", frames=[cover_frame], onPage=partial(draw_cover, meta=meta)),
        PageTemplate(id="content", frames=[content_frame], onPage=partial(draw_content, meta=meta)),
    ])

    fm = front_matter(items, lang, days, focus)

    def front():
        out = []
        if fm["quiet"]:
            out += [Paragraph(_esc(fm["quiet"]), st["body"]), Spacer(1, 8)]
        if fm["picks"]:
            box = [Paragraph(_esc(L["picks"]), st["label"]), Spacer(1, 3)]
            for num, title, head in fm["picks"]:
                box.append(Paragraph(f'<font color="{_MINT}">{num}</font>  {_esc(title)}', st["gtitle"]))
                if head:
                    box.append(Paragraph(_esc(head), st["body"]))
                box.append(Spacer(1, 4))
            t = Table([[box]], colWidths=[W - 36 * mm])
            t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), C(_MINT_BG)),
                                   ("LEFTPADDING", (0, 0), (-1, -1), 10), ("RIGHTPADDING", (0, 0), (-1, -1), 10),
                                   ("TOPPADDING", (0, 0), (-1, -1), 8), ("BOTTOMPADDING", (0, 0), (-1, -1), 6)]))
            out += [t, Spacer(1, 10)]
        return out

    def overview():
        out = [Paragraph(_esc(L["contents"]), st["section"]),
               Paragraph(_esc(L["contents_sub"].format(days=days)), st["meta"]), Spacer(1, 8)]
        out += front()
        for g, ps in groups:
            out.append(Paragraph(f'<font color="{_MINT}">•</font>  {_esc(group_label(g, lang))}'
                                 f'  <font color="{_MUTE}">— {len(ps)} '
                                 f'{L["study"] if len(ps) == 1 else L["studies"]}</font>', st["ov"]))
        if not groups:
            out.append(Paragraph(_esc(L["none_text"]), st["body"]))
        for key, title, _intro, es in appendices:
            out.append(Paragraph(f'<font color="{_MINT}">•</font>  {_esc(title)}'
                                 f'  <font color="{_MUTE}">— {len(es)}</font>', st["ov"]))
        return out

    story = [NextPageTemplate("content"), PageBreak()]
    if edition == "lang":
        story += overview() + [PageBreak()]
        for g, ps in groups:
            for i, p in enumerate(ps, 1):
                story += study_page(p, i, g)
    else:
        story += [Paragraph(_esc(L["cover_kurz"]), st["section"]),
                  Paragraph(_esc(L["contents_sub"].format(days=days)), st["meta"]), Spacer(1, 10)]
        story += front()
        if not groups:
            story += [Paragraph(_esc(L["none_title"]), st["gtitle"]), Paragraph(_esc(L["none_text"]), st["body"])]
        for g, ps in groups:
            story += [Paragraph(_esc(group_label(g, lang)), st["section"]), HR(), Spacer(1, 6)]
            for i, p in enumerate(ps, 1):
                story += short_block(p, i)
        if appendices:
            story.append(Spacer(1, 10))

    first = True
    for key, title, intro, es in appendices:
        if not first:
            story.append(Spacer(1, 12))
        first = False
        story += [Paragraph(_esc(title), st["section"]), Paragraph(_esc(intro), st["meta"]), Spacer(1, 6)]
        for e in es:
            bits = [Paragraph(_esc(e["title"]), st["gtitle"]),
                    Paragraph(f'{_esc(e["meta"])} · <link href="{_esc(e["url"])}">'
                              f'<font color="{_MINT}"><u>{_esc(L["pubmed"])}</u></font></link>', st["meta"])]
            if e.get("note"):
                bits.append(Paragraph(_esc(e["note"]), st["note"]))
            bits.append(Spacer(1, 6))
            story.append(KeepTogether(bits))

    story += [Spacer(1, 8), HR(), Paragraph(_esc(disclaimer(lang)), st["disc"])]
    doc.build(story)


# ---------------------------------------------------------------------------
# 7. CLARITY Word document (.docx)
# ---------------------------------------------------------------------------

def build_docx(items, date_str, days, out_path, lang=None, edition="lang", appendices=None,
               focus=None, top=None):
    if edition == "kurz":
        return build_short_docx(items, date_str, days, out_path, lang or OUTPUT_LANG, appendices, focus)
    """Shareable CLARITY Word document (.docx); same content model and rules as the PDF
    (long edition = one page per study, short edition = compact blocks). Editable by the
    recipient, which a PDF is not."""
    import io
    from docx import Document
    from docx.shared import Pt, Inches, RGBColor
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.enum.table import WD_ROW_HEIGHT_RULE
    from docx.oxml.ns import qn
    from docx.oxml import OxmlElement
    from docx.opc.constants import RELATIONSHIP_TYPE as RT

    lang = lang or OUTPUT_LANG
    L = LABELS[lang]
    fname = field_name(lang, focus)
    appendices = appendices or []

    # brand palette without the leading '#'
    TEAL, TEAL_D, MINT = "0F3D3A", "0A2C2A", "2FB89A"
    MINT_BG, CREAM, INK, MUTE, PANEL = "E8F5EF", "F7F4ED", "16302E", "5C7370", "F0F6F3"

    groups = group_items(items)
    total = sum(len(ps) for _, ps in groups)

    doc = Document()
    sec = doc.sections[0]
    sec.page_width, sec.page_height = Inches(8.27), Inches(11.69)        # A4
    sec.top_margin = sec.bottom_margin = Inches(0.85)
    sec.left_margin = sec.right_margin = Inches(0.8)
    CONTENT_W = sec.page_width - sec.left_margin - sec.right_margin

    base = doc.styles["Normal"]
    base.font.name = "Calibri"
    base.font.size = Pt(10)
    base.font.color.rgb = RGBColor.from_string(INK)
    try:                                           # German proofing language
        rpr = base.element.get_or_add_rPr()
        lg = OxmlElement("w:lang")
        lg.set(qn("w:val"), "de-CH" if lang == "de" else "en-GB")
        rpr.append(lg)
    except Exception:
        pass

    # ---- low-level helpers ------------------------------------------------
    def shade(cell, fill):
        tcPr = cell._tc.get_or_add_tcPr()
        shd = OxmlElement("w:shd")
        shd.set(qn("w:val"), "clear"); shd.set(qn("w:color"), "auto")
        shd.set(qn("w:fill"), fill)
        tcPr.append(shd)

    def borderless(table):
        borders = OxmlElement("w:tblBorders")
        for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
            e = OxmlElement(f"w:{edge}")
            e.set(qn("w:val"), "none"); e.set(qn("w:sz"), "0")
            borders.append(e)
        table._tbl.tblPr.append(borders)

    def keep_together(table):
        """Stop a row splitting across a page break (ugly mid-sentence breaks)."""
        for row in table.rows:
            trPr = row._tr.get_or_add_trPr()
            trPr.append(OxmlElement("w:cantSplit"))

    def write(p, text="", size=10, bold=False, color=INK, italic=False,
              before=0, after=3, align=None, keep_next=False):
        p.paragraph_format.space_before = Pt(before)
        p.paragraph_format.space_after = Pt(after)
        if keep_next:
            p.paragraph_format.keep_with_next = True
        if align is not None:
            p.alignment = align
        if text:
            r = p.add_run(text)
            r.font.size = Pt(size); r.bold = bold; r.italic = italic
            r.font.color.rgb = RGBColor.from_string(color)
        return p

    def para(text="", **kw):
        return write(doc.add_paragraph(), text, **kw)

    def label(text):
        return para(text, size=8, bold=True, color=MINT, before=9, after=1, keep_next=True)

    def hyperlink(p, url, text, color=MINT, size=8):
        r_id = p.part.relate_to(url, RT.HYPERLINK, is_external=True)
        h = OxmlElement("w:hyperlink"); h.set(qn("r:id"), r_id)
        r = OxmlElement("w:r"); rPr = OxmlElement("w:rPr")
        c = OxmlElement("w:color"); c.set(qn("w:val"), color); rPr.append(c)
        u = OxmlElement("w:u"); u.set(qn("w:val"), "single"); rPr.append(u)
        sz = OxmlElement("w:sz"); sz.set(qn("w:val"), str(int(size * 2))); rPr.append(sz)
        r.append(rPr)
        t = OxmlElement("w:t"); t.text = text; r.append(t)
        h.append(r); p._p.append(h)

    def box(fill=MINT_BG):
        """One-cell shaded, borderless container spanning the text width."""
        t = doc.add_table(rows=1, cols=1)
        borderless(t)
        keep_together(t)
        c = t.cell(0, 0)
        shade(c, fill)
        c.width = CONTENT_W
        return c

    def hrule(before=6):
        hr = doc.add_paragraph()
        hr.paragraph_format.space_before = Pt(before)
        hr.paragraph_format.space_after = Pt(4)
        pbdr = OxmlElement("w:pBdr")
        top_ = OxmlElement("w:top")
        top_.set(qn("w:val"), "single"); top_.set(qn("w:sz"), "4")
        top_.set(qn("w:space"), "1"); top_.set(qn("w:color"), "DCE7E3")
        pbdr.append(top_)
        hr._p.get_or_add_pPr().append(pbdr)

    def endpoint_chart(v1, d1, l1, v2, d2, l2):
        """Small bar chart as PNG. Returns a BytesIO or None if plotting fails."""
        try:
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
            fig, ax = plt.subplots(figsize=(3.4, 1.85), dpi=220)
            bars = ax.bar([l1, l2], [v1, v2], color=["#0F3D3A", "#2FB89A"], width=0.5)
            for b, disp in zip(bars, [d1, d2]):
                ax.text(b.get_x() + b.get_width() / 2, b.get_height(), disp,
                        ha="center", va="bottom", fontsize=8.5,
                        fontweight="bold", color="#16302E")
            ax.set_ylim(0, (max(v1, v2) or 1) * 1.3)
            for s_ in ("top", "right", "left"):
                ax.spines[s_].set_visible(False)
            ax.spines["bottom"].set_color("#DCE7E3")
            ax.get_yaxis().set_visible(False)
            ax.tick_params(axis="x", length=0, labelsize=8.5, colors="#5C7370")
            fig.patch.set_alpha(0)
            fig.tight_layout(pad=0.25)
            buf = io.BytesIO()
            fig.savefig(buf, format="png", transparent=True)
            plt.close(fig)
            buf.seek(0)
            return buf
        except Exception:
            return None

    def link_para(p_, before=6):
        lp = doc.add_paragraph()
        lp.paragraph_format.space_before = Pt(before)
        lp.paragraph_format.space_after = Pt(2)
        hyperlink(lp, paper_url(p_) or "https://pubmed.ncbi.nlm.nih.gov/", L["view"])
        rd = lp.add_run(f"  ·  {L['doi']}: {p_.get('doi') or 'n/a'}")
        rd.font.size = Pt(8); rd.font.color.rgb = RGBColor.from_string(MUTE)

    def meta_text(p_):
        m = journals_text(p_) + " · " + str(p_.get("pub_date", ""))
        a = authors_text(p_)
        return m + (" · " + a if a else "")

    def title_para(idx, p_, size):
        tp = doc.add_paragraph()
        tp.paragraph_format.space_after = Pt(1)
        tp.paragraph_format.keep_with_next = True
        rn = tp.add_run(f"{idx}  ")
        rn.font.size = Pt(size); rn.bold = True
        rn.font.color.rgb = RGBColor.from_string(MINT)
        rt = tp.add_run(p_["title"])
        rt.font.size = Pt(size); rt.bold = True
        rt.font.color.rgb = RGBColor.from_string(INK)

    # ---- footer -----------------------------------------------------------
    fp = sec.footer.paragraphs[0]
    fp.alignment = WD_ALIGN_PARAGRAPH.LEFT
    fr = fp.add_run(f"{BRAND} · {fname} · {date_str}\t\t{L['page']} ")
    fr.font.size = Pt(7); fr.font.color.rgb = RGBColor.from_string(MUTE)
    pr = fp.add_run()
    pr.font.size = Pt(7); pr.font.color.rgb = RGBColor.from_string(MUTE)
    for tag, attrs, text in (("w:fldChar", {"w:fldCharType": "begin"}, None),
                             ("w:instrText", {"xml:space": "preserve"}, "PAGE"),
                             ("w:fldChar", {"w:fldCharType": "end"}, None)):
        el = OxmlElement(tag)
        for k, v in attrs.items():
            el.set(qn(k), v)
        if text:
            el.text = text
        pr._r.append(el)

    # ---- cover ------------------------------------------------------------
    banner = doc.add_table(rows=1, cols=1)
    borderless(banner)
    bc = banner.cell(0, 0)
    shade(bc, TEAL)
    bc.width = CONTENT_W
    banner.rows[0].height = Inches(2.9)
    banner.rows[0].height_rule = WD_ROW_HEIGHT_RULE.EXACTLY
    write(bc.paragraphs[0], "", after=40)
    write(bc.add_paragraph(), BRAND, size=40, bold=True, color=CREAM,
          align=WD_ALIGN_PARAGRAPH.CENTER, after=2)
    write(bc.add_paragraph(), L["tagline"], size=11, color=MINT,
          align=WD_ALIGN_PARAGRAPH.CENTER, after=0)

    para("", after=26)
    para(L["cover_" + edition], size=15, bold=True, color=TEAL, align=WD_ALIGN_PARAGRAPH.CENTER, after=4)
    para(f"{fname}   ·   {date_str}   ·   {count_text(total, len(groups), lang)}",
         size=10.5, color=MUTE, align=WD_ALIGN_PARAGRAPH.CENTER, after=4)
    para(L["edition_" + edition], size=8.5, bold=True, color=MINT, align=WD_ALIGN_PARAGRAPH.CENTER, after=22)

    para(L["contents"], size=14, bold=True, color=TEAL, after=2)
    para(L["contents_sub"].format(days=days), size=9, color=MUTE, after=8)
    fm = front_matter(items, lang, days, focus)
    if fm["quiet"]:
        para(fm["quiet"], size=10, color=INK, after=8)
    if fm["picks"]:
        para(L["picks"], size=8.5, bold=True, color=MINT, after=3, keep_next=True)
        for num, title, head in fm["picks"]:
            para(f"{num}  {title}", size=10, bold=True, color=INK, after=1, keep_next=True)
            if head:
                para(head, size=9.5, color=INK, after=5)
        para("", after=4)
    for g, ps in groups:
        p = doc.add_paragraph()
        p.paragraph_format.space_after = Pt(3)
        r1 = p.add_run("● "); r1.font.size = Pt(10)
        r1.font.color.rgb = RGBColor.from_string(MINT)
        r2 = p.add_run(group_label(g, lang)); r2.font.size = Pt(10.5)
        r2.font.color.rgb = RGBColor.from_string(INK)
        r3 = p.add_run(f"  — {len(ps)} {L['study'] if len(ps) == 1 else L['studies']}")
        r3.font.size = Pt(9.5); r3.font.color.rgb = RGBColor.from_string(MUTE)
    if not groups:
        para(L["none_text"], size=9.5, color=INK, after=4)
    for key, title, _intro, es in appendices:
        p = doc.add_paragraph()
        p.paragraph_format.space_after = Pt(3)
        r1 = p.add_run("● "); r1.font.size = Pt(10)
        r1.font.color.rgb = RGBColor.from_string(MINT)
        r2 = p.add_run(title); r2.font.size = Pt(10.5)
        r2.font.color.rgb = RGBColor.from_string(INK)
        r3 = p.add_run(f"  — {len(es)}"); r3.font.size = Pt(9.5)
        r3.font.color.rgb = RGBColor.from_string(MUTE)

    # ---- long edition: one page per study ----------------------------------
    def study_page(g, idx, p_):
        doc.add_page_break()
        s = p_.get("summary", {}) or {}
        kn = s.get("key_numbers") or {}
        para(group_label(g, lang).upper(), size=8, bold=True, color=MINT, after=2, keep_next=True)
        title_para(idx, p_, 15)
        para(meta_text(p_), size=8.5, color=MUTE, after=2)
        para("● " + kicker_text(p_, lang).upper(), size=8, bold=True, color=TEAL, after=6)

        cells = stat_cells(kn, lang)
        if cells:
            t = doc.add_table(rows=1, cols=len(cells))
            borderless(t)
            keep_together(t)
            for i, (val, lab) in enumerate(cells):
                c = t.cell(0, i)
                shade(c, PANEL)
                c.width = int(CONTENT_W / len(cells))
                write(c.paragraphs[0], str(val), size=11, bold=True, color=TEAL, after=0)
                write(c.add_paragraph(), lab, size=7, color=MUTE, after=0)
            para("", after=4)

        pv = primary_view(kn.get("primary_endpoint"), lang)
        if pv:
            c = box(MINT_BG)
            write(c.paragraphs[0], L["primary"] + " — " + pv["name"], size=8, bold=True, color=TEAL_D, after=3)
            png = None
            if pv["chart"]:
                v1, v2 = pv["chart"]
                a1, a2 = pv["arms"][0][1], pv["arms"][1][1]
                png = endpoint_chart(v1, a1, pv["labels"][0], v2, a2, pv["labels"][1])
            if png is not None:
                ip = c.add_paragraph()
                ip.alignment = WD_ALIGN_PARAGRAPH.CENTER
                ip.paragraph_format.space_after = Pt(2)
                ip.add_run().add_picture(png, width=Inches(3.2))
            elif pv["arms"]:
                vp = c.add_paragraph()
                write(vp, "", after=2)
                for (lab_, val_), col in zip(pv["arms"], (TEAL, MINT)):
                    if lab_:
                        r = vp.add_run(f"{lab_}: ")
                        r.font.size = Pt(9); r.bold = True
                        r.font.color.rgb = RGBColor.from_string(MUTE)
                    r2 = vp.add_run(f"{val_}    ")
                    r2.font.size = Pt(11); r2.bold = True
                    r2.font.color.rgb = RGBColor.from_string(col)
            if pv["effect"]:
                write(c.add_paragraph(), pv["effect"], size=9, color=INK, after=0)
            para("", after=4)

        sv = secondary_view(kn, lang)
        if sv:
            cols, rows = sv
            label(L["other_ep"])
            t = doc.add_table(rows=len(rows) + 1, cols=len(cols))
            t.style = "Table Grid"
            keep_together(t)
            widths = [int(CONTENT_W * w) for _k, _h, w in cols]
            for i, (_k, h, _w) in enumerate(cols):
                c = t.cell(0, i)
                shade(c, TEAL)
                c.width = widths[i]
                write(c.paragraphs[0], str(h), size=7.6, bold=True, color="FFFFFF", after=0)
            for ri, r in enumerate(rows, 1):
                for ci, (k, _h, _w) in enumerate(cols):
                    c = t.cell(ri, ci)
                    c.width = widths[ci]
                    if ri % 2 == 0:
                        shade(c, "F5F9F7")
                    write(c.paragraphs[0], _cell(r.get(k)), size=8,
                          bold=(k in ("arm1", "arm2")), color=INK, after=0)
            para("", after=4)
        else:
            findings = findings_view(kn)
            if findings:
                label(L["key_figures"])
                t = doc.add_table(rows=len(findings), cols=2)
                t.style = "Table Grid"
                keep_together(t)
                w = [int(CONTENT_W * 0.55), int(CONTENT_W * 0.45)]
                for ri, (a, b) in enumerate(findings):
                    for ci, v in enumerate((_cell(a), _cell(b))):
                        c = t.cell(ri, ci)
                        c.width = w[ci]
                        if ri % 2 == 1:
                            shade(c, "F5F9F7")
                        write(c.paragraphs[0], str(v), size=8.5,
                              bold=(ci == 1), color=INK if ci == 0 else TEAL, after=0)
                para("", after=4)

        for lab_, txt in text_sections(p_, lang):
            label(lab_)
            para(str(txt), size=9.5, after=2)

        concl = conclusion_text(p_)
        if concl:
            para("", after=4)
            c = box(MINT_BG)
            write(c.paragraphs[0], L["concl"], size=8, bold=True, color=TEAL_D, after=2)
            write(c.add_paragraph(), str(concl), size=9.8, bold=True, color=TEAL_D, after=0)
            para("", after=4)
        link_para(p_)

    # ---- short edition: compact blocks -------------------------------------
    def short_block(idx, p_):
        s = p_.get("summary", {}) or {}
        kn = s.get("key_numbers") or {}
        title_para(idx, p_, 12.5)
        para(meta_text(p_), size=8.5, color=MUTE, after=1, keep_next=True)
        para("● " + kicker_text(p_, lang, with_n=True).upper(), size=8, bold=True, color=TEAL, after=3,
             keep_next=True)
        para(short_text(p_), size=10, after=3, keep_next=True)
        pv = primary_view(kn.get("primary_endpoint"), lang)
        if pv and pv["effect"]:
            lp = doc.add_paragraph()
            lp.paragraph_format.space_after = Pt(2)
            lp.paragraph_format.keep_with_next = True
            r = lp.add_run(L["primary_short"] + " "); r.bold = True; r.font.size = Pt(8.8)
            r.font.color.rgb = RGBColor.from_string(TEAL_D)
            r2 = lp.add_run(f"({pv['name']}): {pv['effect']}"); r2.font.size = Pt(8.8)
            r2.font.color.rgb = RGBColor.from_string(TEAL_D)
        link_para(p_, before=2)
        hrule(before=2)

    if edition == "lang":
        for g, ps in groups:
            for idx, p_ in enumerate(ps, 1):
                study_page(g, idx, p_)
    else:
        doc.add_page_break()
        para(L["cover_kurz"], size=15, bold=True, color=TEAL, after=2)
        para(L["contents_sub"].format(days=days), size=9, color=MUTE, after=8)
        for g, ps in groups:
            para(group_label(g, lang), size=14, bold=True, color=TEAL, before=8, after=4, keep_next=True)
            for idx, p_ in enumerate(ps, 1):
                short_block(idx, p_)

    # ---- appendices -------------------------------------------------------
    if appendices:
        if edition == "lang":
            doc.add_page_break()
    for n_, (key, title, intro, es) in enumerate(appendices):
        para(title, size=14, bold=True, color=TEAL, before=12 if n_ else 4, after=2, keep_next=True)
        para(intro, size=9, color=MUTE, after=8, keep_next=True)
        for e in es:
            para(e["title"], size=9.5, bold=True, after=1, keep_next=True)
            lp = doc.add_paragraph()
            lp.paragraph_format.space_after = Pt(2 if e.get("note") else 7)
            r = lp.add_run(e["meta"] + "  ·  ")
            r.font.size = Pt(8.5); r.font.color.rgb = RGBColor.from_string(MUTE)
            if e.get("url"):
                hyperlink(lp, e["url"], L["pubmed"])
            if e.get("note"):
                para(e["note"], size=8, italic=True, color=MUTE, after=7)

    hrule(before=10)
    para(disclaimer(lang), size=7.5, italic=True, color=MUTE, after=0)

    doc.save(out_path)

# ---------------------------------------------------------------------------
# 8. Recall check - how much of the relevant literature does the pipeline see?
# ---------------------------------------------------------------------------
RECALL_FILE = "recall_report.md"
_RECALL_TIAB = [
    "TAVR", "TAVI", "TEER", "TMVR", "transcatheter aortic", "transcatheter mitral",
    "transcatheter tricuspid", "aortic stenosis", "aortic valve", "mitral valve",
    "tricuspid valve", "mitral regurgitation", "tricuspid regurgitation", "edge-to-edge",
    "left atrial appendage", "patent foramen ovale", "paravalvular", "valve-in-valve",
]


def _whitelist_norm():
    return {_jnorm(j) for j in (list(JOURNALS_SPEC) + list(JOURNALS_GENERAL)
                                + list(JOURNALS_ADJACENT))}


def recall_check(days, lang="de", focus=None):
    """Independent ground truth: every randomised trial, meta-analysis and systematic
    review on the topic that PubMed knows, in ANY journal. Compared with what the
    pipeline has seen (scored_raw, retry queue, dropped history, summaries). Shows
    (1) journals the query never looks at, (2) whitelisted papers that never arrived,
    (3) papers that arrived but were not featured. Writes recall_report.md.
    Note: PubMed assigns publication types with a delay, so use a window of 30+ days."""
    terms = _RECALL_TIAB if focus in (None, STRUCT) else TIAB_KEYWORDS
    term = (f"(({_or(f'{k}[tiab]' for k in terms)}) AND "
            f"(randomized controlled trial[pt] OR meta-analysis[pt] OR systematic review[pt]))")
    print(f"Recall check: PubMed ground truth for the last {days} days ...")
    pmids = esearch_recent(term, days)
    papers = efetch_details(pmids)
    print(f"  {len(papers)} randomised trials / meta-analyses / systematic reviews found")

    known = {}
    for p in load_json(SCORED_FILE, []):
        known[str(p.get("pmid"))] = p.get("_status") or "scored"
    for pmid in load_queue():
        known.setdefault(str(pmid), "queue")
    try:
        with open(_sp(DROPPED_HISTORY_FILE), encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                known.setdefault(str(row.get("pmid")), row.get("status") or "dropped")
    except FileNotFoundError:
        pass
    for pmid in load_summ_cache():
        known[str(pmid).split("|")[0]] = "featured"
    seen = set(load_seen())
    wl = _whitelist_norm()

    not_wl, unseen, not_featured, featured = [], [], [], 0
    for p in papers:
        pt = {(t or "").lower() for t in (p.get("pub_types") or [])}
        if pt & _HARD_NOISE_PUBTYPES or noise_reason(p):
            continue
        st = known.get(p["pmid"])
        if _jnorm(p["journal"]) not in wl:
            not_wl.append(p)
        elif st is None and p["pmid"] not in seen:
            unseen.append(p)
        elif st in (None, "seen"):
            not_featured.append((p, "seen earlier"))
        elif st == "featured" or st == "selected":
            featured += 1
        else:
            not_featured.append((p, st))

    def row(p, extra=""):
        t = (p.get("title") or "").replace("|", "/")[:110]
        return (f"| [{p['pmid']}]({p['url']}) | {p['journal']} | {p.get('pub_date', '')} | {t} |"
                + (f" {extra} |" if extra != "" else ""))

    out = [f"# Recall-Check - {dt.date.today().isoformat()}", "",
           f"Fenster: {days} Tage · Thema: {'Strukturelle Kardiologie' if terms is _RECALL_TIAB else 'Kardiologie allgemein'} · "
           f"Typen: RCT, Meta-Analyse, Systematische Übersicht", "",
           "| Gruppe | Anzahl |", "|---|---:|",
           f"| Gefunden (Ground Truth) | {len(papers)} |",
           f"| davon ausgewählt/zusammengefasst | {featured} |",
           f"| gesehen, aber nicht ausgewählt | {len(not_featured)} |",
           f"| Journal auf der Whitelist, aber nie angekommen | {len(unseen)} |",
           f"| Journal nicht auf der Whitelist | {len(not_wl)} |", ""]
    if not_wl:
        by_j = {}
        for p in not_wl:
            by_j[p["journal"]] = by_j.get(p["journal"], 0) + 1
        out += ["## Journals ausserhalb der Whitelist (Kandidaten für JOURNALS_ADJACENT)", "",
                "| Journal | Treffer |", "|---|---:|"]
        out += [f"| {j} | {n} |" for j, n in sorted(by_j.items(), key=lambda x: -x[1])]
        out += ["", "| PMID | Journal | Datum | Titel |", "|---|---|---|---|"]
        out += [row(p) for p in not_wl[:60]]
        out.append("")
    if unseen:
        out += ["## Whitelist-Journal, aber nie gesehen", "",
                "Mögliche Ursachen: bei Allgemein-/Randjournals greift der Stichwortfilter nicht; "
                "oder die Arbeit wurde vor Einführung der Protokolle verarbeitet.", "",
                "| PMID | Journal | Datum | Titel |", "|---|---|---|---|"]
        out += [row(p) for p in unseen[:80]]
        out.append("")
    if not_featured:
        out += ["## Gesehen, aber nicht ausgewählt", "",
                "| PMID | Journal | Datum | Titel | Status |", "|---|---|---|---|---|"]
        out += [row(p, st) for p, st in not_featured[:120]]
        out.append("")
    with open(_sp(RECALL_FILE), "w", encoding="utf-8") as f:
        f.write("\n".join(out))
    print(f"  featured {featured} · seen-not-featured {len(not_featured)} · "
          f"whitelisted-but-unseen {len(unseen)} · outside whitelist {len(not_wl)}")
    print(f"Wrote {_sp(RECALL_FILE)}")


# ---------------------------------------------------------------------------
# 9. Pipeline stages used by main()
# ---------------------------------------------------------------------------
_RESOLVED = {"selected", "further_listed", "guideline_listed", "duplicate", "below_threshold",
             "queue_expired"}


def _run_pool(workers, fn, items):
    """Results in input order; workers=1 runs inline (no threads)."""
    if workers <= 1 or len(items) <= 1:
        return [fn(x) for x in items]
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=workers) as ex:
        return list(ex.map(fn, items))


def stage_collect(args, today):
    """PubMed + retry queue -> abstracts filled -> noise removed. No model calls.
    Returns (papers, stats, noise_rows, ctx)."""
    stats = {}
    term = build_query()
    print("Query:\n", term, "\n")
    pmids = esearch_recent(term, args.days)
    stats["found"] = len(pmids)
    print(f"esearch: {len(pmids)} PMIDs in last {args.days} days")

    seen = set() if args.no_dedup else load_seen()
    queue = load_queue()
    fresh = [p for p in pmids if p not in seen]
    stats["seen"] = len(pmids) - len(fresh)
    if args.limit:
        fresh = fresh[:args.limit]
    stats["fresh"] = len(fresh)
    fresh_set = set(fresh)

    from_queue, expired_ids = [], set()
    for pmid, e in queue.items():
        if pmid in fresh_set:
            continue
        if queue_age(e, today) > MAX_QUEUE_DAYS:
            expired_ids.add(pmid)
        from_queue.append(pmid)          # expired ones get one last attempt
    stats["from_queue"] = len(from_queue)
    print(f"{len(fresh)} new after dedup, {len(from_queue)} waiting in the retry queue")

    ids = fresh + [x for x in from_queue if x not in fresh_set]
    papers = efetch_details(ids)
    got = {p["pmid"] for p in papers}
    missing = [x for x in ids if x not in got]
    stats["missing"] = len([x for x in missing if x in fresh_set])
    if missing:
        print(f"  !! {len(missing)} PMID(s) requested but not delivered by PubMed "
              f"(kept in the retry queue): {', '.join(missing[:8])}{' ...' if len(missing) > 8 else ''}")
    print(f"efetch: {len(papers)} records")
    for p in papers:
        p["_origin"] = "new" if p["pmid"] in fresh_set else "queue"
        p["_expired"] = p["pmid"] in expired_ids

    kept, noise_rows = [], []
    for p in papers:
        r = noise_reason(p)
        if r:
            p["_status"], p["_reason"], p["_manual"] = "noise", r, []
            noise_rows.append(drop_row(p, "noise", r, today))
        else:
            kept.append(p)
    stats["noise"] = len(noise_rows)
    if noise_rows:
        print(f"removed {len(noise_rows)} letters / replies / errata / editorials (logged in dropped.csv)")

    stats["abs_recovered"] = enrich_missing_abstracts(kept)
    stats["abs_missing"] = sum(1 for p in kept if not p.get("abstract"))
    print(f"abstracts: {stats['abs_recovered']} recovered from secondary sources, "
          f"{stats['abs_missing']} record(s) still without abstract")
    ctx = {"seen": seen, "queue": queue, "missing": missing, "fresh_set": fresh_set,
           "noise_pmids": [p["pmid"] for p in papers if p.get("_status") == "noise"],
           "noise_papers": [p for p in papers if p.get("_status") == "noise"]}
    return kept, stats, noise_rows, ctx


def stage_score(papers, client, workers, stats):
    """Module 1 with a persistent cache. Aborts (without touching any state) when the
    API is clearly down, instead of turning a whole week into 'score 0'."""
    cache = load_json(SCORE_CACHE_FILE, {})
    print(f"\nModule 1 - scoring {len(papers)} papers ({workers} parallel) ...")
    import threading
    lock, gate = threading.Lock(), {"streak": 0, "abort": False}

    def one(p):
        if gate["abort"]:                       # API clearly down: stop spending time on it
            return failed_score("skipped after repeated failures"), False
        try:
            sc, cached = score_cached(p, client, cache)
        except Exception as e:                  # never let one paper stop the run
            sc, cached = failed_score(f"{type(e).__name__}: {str(e)[:120]}"), False
        with lock:
            gate["streak"] = gate["streak"] + 1 if sc.get("failed") else 0
            if gate["streak"] >= 8:
                gate["abort"] = True
        return sc, cached

    results = _run_pool(workers, one, papers)
    n_cached = n_fail = 0
    first_err = ""
    for i, (p, (sc, cached)) in enumerate(zip(papers, results), 1):
        p["score"] = sc
        n_cached += 1 if cached else 0
        if sc.get("failed"):
            n_fail += 1
            first_err = first_err or str(sc.get("error", ""))
        mark = "!! FAILED" if sc.get("failed") else f"{sc.get('relevance_score', '?')}/10"
        print(f"  [{i}/{len(papers)}] {mark}{' (cache)' if cached else ''}  {p['title'][:66]}")
    save_json(SCORE_CACHE_FILE, cache)
    stats["score_cached"] = n_cached
    if gate["abort"]:
        sys.exit(f"\nABORTED: the triage kept failing ({n_fail}/{len(papers)} papers without a score).\n"
                 f"  first error: {first_err}\n"
                 f"State (seen / retry queue) was NOT changed; scores that worked are cached, so a "
                 f"re-run only repeats the failed papers.")
    if n_fail:
        print(f"!! {n_fail} paper(s) could not be scored -> status 'scoring_failed', queued for the next run")
    return papers


def stage_summaries(ranked, target_subs, top, client, lang, workers):
    """Module 2 for the best `top` candidates per area. A failed summary is replaced by the
    next candidate (backfill) and the failed paper goes to the retry queue."""
    import copy
    cache = load_summ_cache()
    selected = []
    stats = {"new": 0, "reused": 0, "failed": 0}

    def one(p):
        e = cache.get(summ_key(p["pmid"], lang))
        if summary_cache_ok(e, lang):
            return p, e, True
        return p, summarize_paper(p, client, lang), False

    for g in target_subs:
        cands, pos, got = ranked.get(g, []), 0, 0
        while got < top and pos < len(cands):
            batch = cands[pos:pos + (top - got)]
            pos += len(batch)
            dirty = False
            for p, s, was_cached in _run_pool(workers, one, batch):
                if s.get("failed"):
                    _set(p, "summary_failed", str(s.get("error", ""))[:120])
                    stats["failed"] += 1
                    print(f"  !! summary failed -> next candidate: {p['title'][:60]}")
                    continue
                if was_cached:
                    stats["reused"] += 1
                else:
                    cache[summ_key(p["pmid"], lang)] = s
                    stats["new"] += 1
                    dirty = True
                p["summary"] = copy.deepcopy(s)
                _set(p, "selected")
                selected.append(p)
                got += 1
                print(f"  [{group_label(g, 'en')[:22]:<22}] {'cached' if was_cached else 'NEW   '}  {p['title'][:56]}")
            if dirty:
                save_summ_cache(cache)                 # incremental: crash-safe
        for k, p in enumerate(cands[pos:], start=pos + 1):
            _set(p, "further_listed", f"rank {k} in {g} (--top {top})")
    print(f"Module 2: {stats['new']} newly summarised, {stats['reused']} reused from cache, "
          f"{stats['failed']} failed (backfilled / queued).")
    return selected


def stage_grounding(selected, client, lang, workers):
    """Module 3: ground the relation fields in the reference base."""
    guideline_texts = load_guideline_texts()
    overrides = load_key_finding_overrides()
    ref_base = load_json("reference_base.json", None)
    if not ref_base:
        ref_base = dict(load_structural_references())
        ref_base["key_findings"] = {}
    else:
        ref_base = merge_new_topics(ref_base)
    rel_cache = load_json(REL_CACHE_FILE, {})

    def one(p):
        try:
            return p, build_relation_fields(p, client, guideline_texts, ref_base, overrides,
                                            lang=lang, cache=rel_cache)
        except Exception as e:
            print(f"  !! Module 3 error for {p['title'][:50]}: {type(e).__name__}: {str(e)[:100]}")
            return p, None

    print(f"\nModule 3 - grounding {len(selected)} papers ...")
    n_ok = 0
    for p, rel in _run_pool(workers, one, selected):
        s = p.setdefault("summary", {})
        g = p.get("_grounding", {})
        if rel:
            for k in ("relation_evidence", "relation_guidelines"):
                # a grounded answer that ended up empty gets the standard sentence - never the
                # ungrounded Module-2 text, which knows nothing about the guideline
                s[k] = rel.get(k) or REL_DEFAULTS[lang][k]
            if rel.get("tension"):
                s["tension"] = rel["tension"]
            else:
                s.pop("tension", None)
            s["sources_used"] = rel.get("sources_used") or []
            s["relation_qc_notes"] = rel.get("qc_notes") or ""
            n_ok += 1
        else:
            s["relation_qc_notes"] = ("Modul 3 lieferte nichts: Einordnung stammt aus Modul 2, "
                                      "ohne Referenzbasis" if lang == "de" else
                                      "Module 3 returned nothing: relation text comes from Module 2, "
                                      "without the reference base")
        print(f"  {'grounded' if rel else 'NOT grounded':<12} {str(g.get('topic', '-'))[:30]:<30} "
              f"guideline={g.get('guideline_words', 0):>5}w landmarks={g.get('n_landmarks', 0)} "
              f"consensus={g.get('n_consensus', 0)}{'  (cached)' if g.get('cached') else ''}")
    save_json(REL_CACHE_FILE, rel_cache)
    print(f"Module 3: {n_ok}/{len(selected)} papers grounded.")
    try:                                   # what the model actually saw, for inspection
        dump = {}
        for p in selected:
            t = (p.get("score") or {}).get("structural_topic") or "Other"
            dump[p["pmid"]] = {"title": p["title"], "topic": t,
                               "diagnostics": p.get("_grounding", {}),
                               "context_sent_to_model": build_reference_context(
                                   t, p["title"], guideline_texts, ref_base, overrides)}
        save_json("grounding_debug.json", dump, indent=2)
    except Exception as e:
        print(f"(debug dump skipped: {e})")


def apply_state(scored, ctx, stats, today, focused):
    """Persist what has been dealt with. A paper is 'seen' only when it was resolved
    (featured, listed, rejected on evidence); anything that could still turn out to be
    relevant (no abstract yet, failed call) goes into the retry queue instead."""
    seen, queue = ctx["seen"], ctx["queue"]
    for p in ctx.get("noise_papers", []):
        seen.add(p["pmid"])
        queue.pop(p["pmid"], None)
    for p in scored:
        st, pmid = p.get("_status"), p["pmid"]
        if st == "queue_expired" or st in _RESOLVED:
            seen.add(pmid)
            queue.pop(pmid, None)
        elif st in ("pending", "no_abstract_low"):
            # Even a record that looks like an editorial stays in the queue: the verdict was
            # made from the title alone, and if an abstract ever appears it is scored properly.
            queue_put(queue, p, "no_abstract", today)
        elif st in ("scoring_failed", "summary_failed"):
            queue_put(queue, p, st, today)
        elif st == "outside_focus":
            if not focused:                          # nobody will ever ask for it
                seen.add(pmid)
                queue.pop(pmid, None)
        # 'candidate' (no document was built): left untouched so the next run still sees it
    for pmid in ctx.get("missing", []):
        e = queue.get(pmid)
        if e is None:
            queue[pmid] = {"first_seen": today, "attempts": 1, "reason": "efetch_missing",
                           "title": "", "journal": "", "last_checked": today}
        elif queue_age(e, today) > MAX_QUEUE_DAYS:       # PubMed never delivered it: stop asking
            seen.add(pmid)
            queue.pop(pmid, None)
        else:
            e["attempts"] = int(e.get("attempts", 0)) + 1
            e["last_checked"] = today
    save_seen(seen)
    save_queue(queue)
    stats["queued_now"] = len(queue)
    print(f"State saved: {len(seen)} PMIDs seen, {len(queue)} waiting in the retry queue.")


def dump_scored(scored):
    out = [{k: v for k, v in p.items() if k not in ("_grounding", "_expired")} for p in scored]
    save_json(SCORED_FILE, out, indent=1)


def stage_documents(args, lang, selected, scored, focus, today):
    ed_key = {"long": "lang", "short": "kurz", "both": "beide"}.get(args.edition, args.edition)
    editions = ["lang", "kurz"] if ed_key == "beide" else [ed_key]     # internal keys stay lang / kurz
    failures = []
    adapt_labels_to_window(args.days)
    shown = [display_paper(p, lang) for p in selected]       # cleaned copies; stored data untouched
    for ed in editions:
        apps = appendix_model(scored, lang, top=args.top,
                              with_manual=(ed == "lang" and not args.no_manual))
        base = "clarity_cardio_" + (ed if lang == "de" else {"lang": "long", "kurz": "short"}[ed])
        for ext, fn in (("docx", build_docx), ("pdf", build_pdf)):
            if not getattr(args, ext):
                continue
            path = _sp(f"{base}.{ext}")
            try:
                fn(shown, today, args.days, path, lang=lang, edition=ed, appendices=apps,
                   focus=focus, top=args.top)
                print(f"Wrote {path}")
            except Exception as e:
                import traceback
                traceback.print_exc()
                failures.append(f"{base}.{ext}: {type(e).__name__}: {e}")
    return failures


def _pubmed_count(term, reldate):
    """Trefferzahl einer PubMed-Suche (nur Anzahl, keine PMIDs); None bei Netzwerkfehler."""
    try:
        r = requests.get(f"{EUTILS}/esearch.fcgi", params=_params({
            "db": "pubmed", "term": term, "reldate": reldate,
            "datetype": "edat", "retmode": "json", "retmax": 0,
        }), timeout=30)
        r.raise_for_status()
        return int(r.json()["esearchresult"].get("count", 0))
    except Exception as e:
        print(f"  ! PubMed-Abfrage fehlgeschlagen: {type(e).__name__}: {str(e)[:100]}")
        return None
    finally:
        _sleep()


def check_params(days):
    """Testmodus --check-params: zeigt die geladenen Parameterlisten, warnt bei
    verdächtigen Einträgen und prüft jeden Journalnamen bei PubMed.
    Keine Modellaufrufe, kein State, keine Dateien."""
    print(f"SPECIALTY_DIR: {SPECIALTY_DIR}")
    print(f"STATE_DIR:     {STATE_DIR}\n")
    print("Gelesene Parameterdateien:")
    for path, n in PARAM_SOURCES.items():
        print(f"  {n:>4}  {path}")

    lists = {"SUBGROUPS": SUBGROUPS, "JOURNALS_SPEC": JOURNALS_SPEC,
             "JOURNALS_GENERAL": JOURNALS_GENERAL, "JOURNALS_ADJACENT": JOURNALS_ADJACENT,
             "TIAB_KEYWORDS": TIAB_KEYWORDS}
    warnings = []
    for name, items in lists.items():
        print(f"\n{name} ({len(items)})")
        for i, x in enumerate(items, 1):
            print(f"  {i:>3}. {x!r}")
        dups = sorted({x for x in items if items.count(x) > 1})
        if dups:
            warnings.append(f"{name}: doppelte Einträge {dups}")
        odd = [x for x in items if x.endswith((",", ";")) or "\t" in x]
        if odd:
            warnings.append(f"{name}: verdächtige Zeichen in {odd}")

    journal_lists = ("JOURNALS_SPEC", "JOURNALS_GENERAL", "JOURNALS_ADJACENT")
    where = {}
    for name in journal_lists:
        for j in lists[name]:
            where.setdefault(_jnorm(j), []).append(name)
    for j, names in where.items():
        if len(names) > 1:
            warnings.append(f"Journal {j!r} steht in mehreren Listen: {', '.join(names)}")

    print(f"\nPubMed-Abfrage:\n{build_query()}\n")
    print("Journalnamen bei PubMed prüfen (Treffer der letzten 365 Tage) ...")
    for name in journal_lists:
        for j in lists[name]:
            n = _pubmed_count(f'"{j}"[Journal]', 365)
            mark = "   <-- 0 Treffer: Name falsch?" if n == 0 else ""
            print(f"  {'?' if n is None else n:>6}  {j}  ({name}){mark}")
            if n == 0:
                warnings.append(f"{name}: {j!r} liefert bei PubMed 0 Treffer")
    total = _pubmed_count(build_query(), days)
    print(f"\nGesamte Abfrage, letzte {days} Tage: {'?' if total is None else total} Treffer")

    print(f"\n{len(warnings)} Warnung(en)" + (":" if warnings else "."))
    for w in warnings:
        print(f"  !! {w}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=f"CardioDigest v{VERSION} - weekly PubMed triage (CLARITY)")
    ap.add_argument("--days", type=int, default=RELDATE_DAYS)
    ap.add_argument("--out", default="cardio_digest.md")
    ap.add_argument("--no-dedup", action="store_true",
                    help="test mode: ignore seen_pmids.json and do NOT change seen / retry queue / history")
    ap.add_argument("--limit", type=int, default=0, help="cap papers fetched (testing)")
    ap.add_argument("--email", action="store_true",
                    help="also send the digest by email (needs RESEND_API_KEY, EMAIL_TO)")
    ap.add_argument("--pdf", action="store_true", help="build the CLARITY PDF(s)")
    ap.add_argument("--docx", action="store_true", help="build the CLARITY Word document(s)")
    ap.add_argument("--lang", choices=["de", "en"], default=OUTPUT_LANG if OUTPUT_LANG in ("de", "en") else "en",
                    help="language of the documents (default: OUTPUT_LANG, else en)")
    ap.add_argument("--edition", choices=["long", "short", "both", "lang", "kurz", "beide"], default="both",
                    help="long (lang) = one page per study, short (kurz) = 2-3 sentences per study, "
                         "both (beide) = both")
    ap.add_argument("--no-manual", action="store_true",
                    help="leave the 'for editorial review' appendix out of the documents "
                         "(it stays in funnel_report.md and dropped.csv)")
    ap.add_argument("--top", type=int, default=MAX_STUDIES,
                    help=f"maximum study pages per subgroup (default {MAX_STUDIES})")
    ap.add_argument("--min", type=int, default=MIN_STUDIES, dest="min_studies",
                    help=f"minimum study pages per subgroup: if fewer reach score {PDF_MIN_SCORE}, the best "
                         f"studies with score >= {FALLBACK_SCORE} fill up (default {MIN_STUDIES}; 0 = off)")
    ap.add_argument("--only", default="",
                    help="restrict the documents to ONE subgroup, e.g. --only 'Strukturelle Kardiologie'")
    ap.add_argument("--workers", type=int, default=int(os.environ.get("LLM_WORKERS", "3")),
                    help="parallel model calls (default 3; 1 = sequential)")
    ap.add_argument("--build-refs", action="store_true",
                    help="fetch all curated reference abstracts once, extract key findings, then exit")
    ap.add_argument("--refs-docx", action="store_true",
                    help="rebuild only the reference-base Word document (free)")
    ap.add_argument("--no-grounding", action="store_true",
                    help="skip Module 3 (no guideline grounding of the relation fields)")
    ap.add_argument("--refresh-refs", action="store_true", help="force a full rebuild of the reference base")
    ap.add_argument("--rebuild", action="store_true",
                    help="skip PubMed + Module 1: reload scored_raw.json and rebuild the documents "
                         "(cached summaries are reused; state is not touched)")
    ap.add_argument("--recall-check", action="store_true",
                    help="compare PubMed's RCTs / meta-analyses on the topic with what the pipeline "
                         "saw; writes recall_report.md (no model calls)")
    ap.add_argument("--check-params", action="store_true",
                    help="Parameterdateien laden und anzeigen, Journalnamen bei PubMed prüfen; "
                         "keine Modellaufrufe, kein State")
    args = ap.parse_args()
    if args.check_params:
        check_params(args.days)
        return
    lang = args.lang
    print(f"CardioDigest v{VERSION} - short edition in the CLARITY 'week at a glance' format "
          f"(file: {os.path.abspath(__file__)})")
    today = dt.date.today().isoformat()

    os.makedirs(STATE_DIR, exist_ok=True)
    if STATE_DIR != ".":
        print(f"State + outputs directory: {STATE_DIR}")

    # --- focus: fail early on a typo, not after an hour of scoring ---
    target_subs, focus = list(SUBGROUPS), None
    if args.only:
        focus = resolve_subgroup(args.only)
        if not focus:
            print(f"--only '{args.only}' not recognised. Valid subgroups:")
            for g in SUBGROUPS:
                print(f"   - {g}   ({GROUP_DE.get(g, '')})")
            sys.exit(2)
        target_subs = [focus]
        print(f"Focus: {focus} only (up to {args.top} studies).")

    if args.refs_docx:
        try:
            print(f"Wrote {build_reference_docx()}")
        except FileNotFoundError:
            sys.exit(f"{_sp('reference_base.json')} not found - run --build-refs once first.")
        return
    if args.recall_check:
        recall_check(args.days, lang, focus)
        return

    if not ANTHROPIC_API_KEY:
        sys.exit("Set ANTHROPIC_API_KEY (and ideally NCBI_API_KEY / NCBI_EMAIL).")
    import anthropic
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    # Verify both models answer before doing any work - a wrong model name or a wrong key
    # would otherwise fail on every paper.
    for label, name in (("triage", LLM_MODEL), ("summary", SUMMARY_MODEL)):
        try:
            client.messages.create(model=name, max_tokens=8,
                                   messages=[{"role": "user", "content": "ok"}])
            print(f"model check: {label} model {name!r} OK")
        except Exception as e:
            sys.exit(f"\nMODEL CHECK FAILED for the {label} model {name!r}:\n  {e}\n"
                     f"If the message says 'invalid x-api-key' the KEY is wrong, not the model. "
                     f"Otherwise fix the model name or unset the environment variable.")

    if args.build_refs or args.refresh_refs:
        build_reference_base(client, refresh=args.refresh_refs)
        return

    stats, ctx, noise_rows = {}, None, []
    if args.rebuild:
        scored = load_json(SCORED_FILE, None)
        if scored is None:
            sys.exit(f"--rebuild needs {_sp(SCORED_FILE)} but it doesn't exist yet. "
                     f"Run once without --rebuild first.")
        old_fail = 0
        for p in scored:
            sc = p.get("score") or {}
            if sc.get("bottom_line") == "(scoring failed)" and not sc.get("failed"):
                sc["failed"], old_fail = True, old_fail + 1
            p["score"] = normalize_score(sc, p)
        print(f"Rebuild mode: loaded {len(scored)} scored papers (no fetch, no Module 1).")
        if old_fail:
            print(f"!! {old_fail} of them are old 'scoring failed' entries (score 0, not a verdict). "
                  f"Run once without --rebuild to score them again.")
        stats = {"found": len(scored)}
    else:
        papers, stats, noise_rows, ctx = stage_collect(args, today)
        scored = stage_score(papers, client, max(1, args.workers), stats)
        dump_scored(scored)                               # crash safety

    # ---- every paper gets a status -------------------------------------------------------
    ranked = classify_papers(scored, target_subs, PDF_MIN_SCORE,
                             min_count=min(args.min_studies, args.top))
    for p in scored:                                      # expired queue entries end here
        if p.get("_expired") and p.get("_status") in ("pending", "no_abstract_low"):
            _set(p, "queue_expired",
                 f"no abstract after more than {MAX_QUEUE_DAYS} days in the retry queue")

    build_docs = args.pdf or args.docx
    selected = []
    if build_docs:
        n_guide = sum(1 for p in scored if p.get("_status") == "guideline_listed")
        if n_guide:
            print(f"{n_guide} guideline/consensus paper(s): named in the appendix, not summarised")
        print(f"\nModule 2 - summaries in {'German' if lang == 'de' else 'English'} "
              f"(top {args.top} per area) ...")
        selected = stage_summaries(ranked, target_subs, args.top, client, lang, max(1, args.workers))
        if selected and not args.no_grounding:
            stage_grounding(selected, client, lang, max(1, args.workers))

    for p in scored:                                      # flags for a human look
        p["_manual"] = manual_flags(p)

    # ---- quality checks on the featured set ----------------------------------------------
    if selected:
        no_visual = [p["title"][:60] for p in selected
                     if not ((p["summary"].get("key_numbers") or {}).get("primary_endpoint")
                             or (p["summary"].get("key_numbers") or {}).get("secondary")
                             or (p["summary"].get("key_numbers") or {}).get("key_findings"))]
        print(f"Visuals: {len(selected) - len(no_visual)}/{len(selected)} studies have a chart or table.")
        flagged = [(p["title"][:50], advisory_flags(p["summary"])[0]) for p in selected
                   if advisory_flags(p["summary"])]
        if flagged:
            print("Advisory-language check - please review before sharing (details in qc_report.md):")
            for t, (field, phrase) in flagged:
                print(f"   {t} [{field}]: '{phrase}'")

    # ---- reports: nothing may vanish without a line in one of these files -----------------
    drop_rows = list(noise_rows)
    if ctx is not None:
        for pmid in ctx.get("missing", []):
            e = ctx["queue"].get(pmid)
            if e is not None and queue_age(e, today) > MAX_QUEUE_DAYS:
                stats["expired"] = stats.get("expired", 0) + 1
                drop_rows.append(drop_row({"pmid": pmid, "title": e.get("title", ""), "journal": e.get("journal", ""),
                                           "_origin": "queue", "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"},
                                          "queue_expired", "PubMed never delivered the record", today))
    for p in scored:
        st = p.get("_status", "?")
        if st != "selected":
            drop_rows.append(drop_row(p, st, p.get("_reason", ""), today))
    if not args.rebuild:
        write_dropped(drop_rows, history=not args.no_dedup)
    counts = status_counts(scored)
    stats["expired"] = counts.get("queue_expired", 0) + stats.get("expired", 0)
    if ctx is not None and not args.no_dedup:
        apply_state(scored, ctx, stats, today, focused=bool(args.only))
    else:
        stats["queued_now"] = len(load_queue())
    focus_txt = group_label(focus, lang) if focus else ("alle" if lang == "de" else "all")
    if not args.rebuild:                       # a rebuild has no fetch stats - keep the real report
        with open(_sp(FUNNEL_FILE), "w", encoding="utf-8") as f:
            f.write(build_funnel_report(stats, scored, focus_txt, args.days, lang, today))
    if selected:
        with open(_sp(QC_FILE), "w", encoding="utf-8") as f:
            f.write(build_qc_report(selected, lang, today))
    dump_scored(scored)

    digest = build_digest(scored)
    with open(_sp(args.out), "w", encoding="utf-8") as f:
        f.write(digest)
    print(f"\nWrote {_sp(args.out)}, {_sp(SCORED_FILE)}"
          f"{', ' + _sp(FUNNEL_FILE) + ', ' + _sp(DROPPED_FILE) if not args.rebuild else ''}")

    if args.email:
        send_email(f"CardioDigest - {today}", markdown_to_html(build_digest(scored, include_abstract=False)))

    failures = []
    if build_docs:
        failures = stage_documents(args, lang, selected, scored, focus, today)

    # ---- the one-screen answer to "what happened to the papers?" --------------------------
    print("\nWhat happened to the papers:")
    for st in STATUS_ORDER:
        if counts.get(st):
            print(f"  {counts[st]:>4}  {_FUNNEL_T[lang]['status'].get(st, st)}")
    if stats.get("noise"):
        print(f"  {stats['noise']:>4}  {_FUNNEL_T[lang]['noise']}")
    n_manual = sum(1 for p in scored if p.get("_manual"))
    if n_manual:
        print(f"  {n_manual:>4}  {_FUNNEL_T[lang]['manual']}  -> see {FUNNEL_FILE}")
    if failures:
        sys.exit("\nSome documents could not be built:\n  " + "\n  ".join(failures))


if __name__ == "__main__":
    main()
