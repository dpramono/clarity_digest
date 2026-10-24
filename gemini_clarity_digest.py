#!/usr/bin/env python3
"""
ClarityDigest - wochentliche PubMed-Triage, fachgebietsunabhaengig.

Modulare Variante von cardio_digest.py: saemtliche fachspezifischen Inhalte
(Subgroups, Journals, Keywords, Prompts, Branding, Disclaimer) liegen in
Dateien unterhalb eines Fachgebiets-Ordners, das Script selbst ist generisch.

Ausgabe je Lauf (Ordner output/<projekt>_<timestamp>/):
  <projekt>.md      - Markdown-Digest (wie bisher)
  clarity_<projekt>.docx - Word-Dokument (Modul 2, wie cardio_digest.py --docx)
  scored_raw.json   - Rohbewertung (fuer --rebuild)

Verzeichnislayout (siehe README-Abschnitt am Dateiende):

  gemini_clarity_digest.py
  config/gemini_clarity.cfg
  <fachgebiet>/
      params/  subgroups.lst journals_spec.lst journals_gen.lst
               tiab_keywords.lst score_sys.txt score_rubric.txt
               summary_sys.txt summary_rubric.txt noise_titles.lst
               subgroup_aliases.json branding.json disclaimer.txt
      refs/            (Referenz-PDFs)
      key_findings.json
      state/           (seen_pmids.json, abstract_attempts.json, summaries.json)
      output/
"""

import os
import re
import io
import sys
import json
import time
import argparse
import configparser
import datetime as dt
import xml.etree.ElementTree as ET
from pathlib import Path

import requests
from google import genai
from google.genai import types as genai_types

# ---------------------------------------------------------------------------
# Config: config/gemini_clarity.cfg, relativ zum Skript-Speicherort
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_FILE = SCRIPT_DIR / "config" / "gemini_clarity.cfg"

if CONFIG_FILE.is_file():
    _cfg = configparser.ConfigParser()
    _cfg.read(CONFIG_FILE, encoding="utf-8")

    def _cfg2env(section, option, envname):
        if _cfg.has_section(section):
            val = _cfg.get(section, option, fallback="")
            if val:
                os.environ[envname] = val

    _cfg2env("NCBI", "email", "NCBI_EMAIL")
    _cfg2env("NCBI", "api_key", "NCBI_API_KEY")
    _cfg2env("NCBI", "tool", "NCBI_TOOL")
    _cfg2env("GEMINI", "api_key", "GEMINI_API_KEY")
    _cfg2env("GEMINI", "llm_model", "LLM_MODEL")
    _cfg2env("GEMINI", "summary_model", "SUMMARY_MODEL")
    _cfg2env("GENERAL", "dry_run", "DRY_RUN_DEFAULT")
    _cfg2env("GENERAL", "min_score", "MIN_SCORE")
    _cfg2env("GENERAL", "doc_min_score", "DOC_MIN_SCORE")
    _cfg2env("GENERAL", "max_per_subgroup", "MAX_PER_SUBGROUP")

# ---------------------------------------------------------------------------
# Globale Konstanten / API-Clients
# ---------------------------------------------------------------------------

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")


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

RELDATE_DAYS = 7


def _int_env(name, default):
    try:
        return int(str(os.environ.get(name, "")).strip())
    except (TypeError, ValueError):
        return default


MIN_SCORE = _int_env("MIN_SCORE", 6)            # Schwelle fuer den Markdown-Digest
DOC_MIN_SCORE = _int_env("DOC_MIN_SCORE", 6)    # Schwelle fuer Word-Studienseiten
MAX_PER_SUBGROUP = _int_env("MAX_PER_SUBGROUP", 5)
MAX_ABSTRACT_ATTEMPTS = 3

DRY_RUN_DEFAULT = os.environ.get("DRY_RUN_DEFAULT", "false").strip().lower() in (
    "1", "true", "yes", "on")

# Default-Pfade, alle relativ zum --specialty-dir
SUBGROUPS_FILE_DEFAULT = "params/subgroups.lst"
JOURNALS_SPEC_FILE_DEFAULT = "params/journals_spec.lst"
JOURNALS_GEN_FILE_DEFAULT = "params/journals_gen.lst"
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

SEEN_FILE = "seen_pmids.json"
ATTEMPTS_FILE = "abstract_attempts.json"
SUMM_CACHE_FILE = "summaries.json"
SCORED_FILE = "scored_raw.json"

# ---------------------------------------------------------------------------
# Generische Datei-Loader
# ---------------------------------------------------------------------------


def load_list_file(path, description="Liste"):
    """Textdatei, ein Eintrag pro Zeile. '#'-Zeilen und Leerzeilen werden ignoriert."""
    p = Path(path)
    if not p.is_file():
        print(f"  !! Konnte {path} nicht finden - {description} bleibt leer.")
        return []
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except Exception as e:
        print(f"  !! Konnte {path} nicht laden ({type(e).__name__}): {e}")
        return []
    items = [l.strip() for l in lines if l.strip() and not l.strip().startswith("#")]
    if not items:
        print(f"  !! {path} ist leer - {description} bleibt leer.")
        return []
    print(f"{description} geladen: {len(items)} aus {path}")
    return items


def load_text_file(path, fallback, description="Text"):
    """Kompletter Fliesstext aus einer Datei, sonst Fallback."""
    p = Path(path)
    if not p.is_file():
        print(f"  !! {path} fehlt - verwende eingebauten Fallback ({description}).")
        return fallback
    try:
        text = p.read_text(encoding="utf-8").strip()
    except Exception as e:
        print(f"  !! Konnte {path} nicht laden ({type(e).__name__}): {e}")
        return fallback
    if not text:
        print(f"  !! {path} ist leer - verwende eingebauten Fallback ({description}).")
        return fallback
    print(f"{description} geladen aus {path}")
    return text


def load_json_file(path, fallback, description="JSON"):
    p = Path(path)
    if not p.is_file():
        return fallback
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        print(f"  !! Konnte {path} nicht laden ({type(e).__name__}): {e}")
        return fallback
    print(f"{description} geladen aus {path}")
    return data


def load_subgroups(path=SUBGROUPS_FILE_DEFAULT):
    subgroups = load_list_file(path, description="Subgroups")
    return subgroups if subgroups else ["Other"]


def load_reference_files(refs_dir="refs"):
    ref_path = Path(refs_dir)
    if not ref_path.is_dir():
        return []
    return sorted(f.name for f in ref_path.glob("*.pdf"))


def load_key_finding_overrides(path="key_findings.json"):
    data = load_json_file(path, {}, description="Key-Finding-Overrides")
    if not isinstance(data, dict):
        print(f"  !! {path}: erwartet wird ein JSON-Objekt {{pmid: text}} - ignoriere Datei.")
        return {}
    return {str(k): v for k, v in data.items()}


# ---------------------------------------------------------------------------
# Branding / Disclaimer (fachgebietsspezifisch, aus Dateien)
# ---------------------------------------------------------------------------

BRANDING_FALLBACK = {
    "brand": "CLARITY",
    "tagline": "Real insights in seconds",
    "field_name": "Medicine",
    "palette": {
        "TEAL": "0F3D3A", "TEAL_D": "0A2C2A", "MINT": "2FB89A",
        "MINT_BG": "E8F5EF", "CREAM": "F7F4ED", "INK": "16302E",
        "MUTE": "5C7370", "PANEL": "F0F6F3", "HAIR": "DCE7E3",
    },
}

DISCLAIMER_FALLBACK = (
    "CLARITY is an independent literature curation service and is not affiliated with, "
    "endorsed by, or associated with the journals or societies named herein. This "
    "document is a factual summary of published research, provided for information and "
    "continuing-education purposes only. It does not constitute medical advice, a "
    "treatment recommendation or clinical guidance, and must not be used as a basis for "
    "patient care decisions. Summaries are AI-generated and may contain errors; always "
    "consult the original publication and the applicable clinical guidelines."
)


def load_branding(path=BRANDING_FILE_DEFAULT):
    data = load_json_file(path, {}, description="Branding")
    brand = dict(BRANDING_FALLBACK)
    if isinstance(data, dict):
        palette = dict(BRANDING_FALLBACK["palette"])
        palette.update({k: str(v).lstrip("#") for k, v in (data.get("palette") or {}).items()})
        brand.update({k: v for k, v in data.items() if k != "palette"})
        brand["palette"] = palette
    # Env-Override bleibt moeglich (Kompatibilitaet zu cardio_digest.py)
    brand["field_name"] = os.environ.get("FIELD_NAME", brand.get("field_name"))
    return brand


# ---------------------------------------------------------------------------
# PubMed Queries & E-utilities
# ---------------------------------------------------------------------------


def _or(terms):
    return " OR ".join(terms)


def build_query(journals_spec, journals_gen, tiab_keywords):
    if not journals_spec:
        print("  !! Warnung: keine Fachgebiet-Journals geladen - Suche eingeschraenkt.")
    if not journals_gen or not tiab_keywords:
        print("  !! Warnung: journals_gen oder tiab_keywords leer - allgemeiner Zweig traegt nicht bei.")
    spec = _or(f'"{j}"[Journal]' for j in journals_spec)
    general_j = _or(f'"{j}"[Journal]' for j in journals_gen)
    tiab = _or(f'{k}[tiab]' for k in tiab_keywords)
    return f"(({spec}) OR (({general_j}) AND ({tiab})))"


def _params(extra):
    p = {"tool": NCBI_TOOL, "email": NCBI_EMAIL}
    if NCBI_API_KEY:
        p["api_key"] = NCBI_API_KEY
    p.update(extra)
    return p


def _sleep():
    time.sleep(0.11 if NCBI_API_KEY else 0.34)


def esearch_recent(term, reldate=RELDATE_DAYS):
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


# ---------------------------------------------------------------------------
# XML-Parsing (netzfrei, testbar)
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
    """Vollstaendiger Datensatz je Paper - inkl. authors, pub_types, doi.
    Diese drei Felder fehlten in der bisherigen Kurzversion und werden vom
    Word-Dokument (Autorenzeile, Guideline-Erkennung, DOI-Fusszeile) gebraucht."""
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
        out.append({
            "pmid": pmid,
            "title": _text(art.find(".//ArticleTitle")),
            "journal": journal,
            "pub_date": _pubdate(art),
            "authors": authors,
            "pub_types": pubtypes,
            "abstract": _abstract(art),
            "doi": _doi(pa),
            "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
        })
    return out


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
# Abstract-Nachbeschaffung (Europe PMC / Crossref / Semantic Scholar / PubMed)
# ---------------------------------------------------------------------------


def _strip_html(s):
    return re.sub(r"<[^>]+>", "", s or "").strip()


def _clean_abstract(s):
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
    if not doi:
        return ""
    try:
        params, headers = {}, {"User-Agent": NCBI_TOOL}
        if NCBI_EMAIL and "@" in NCBI_EMAIL:
            params["mailto"] = NCBI_EMAIL
            headers["User-Agent"] = f"{NCBI_TOOL} (mailto:{NCBI_EMAIL})"
        r = requests.get(CROSSREF + doi, params=params, headers=headers, timeout=30)
        if r.status_code >= 300:
            return ""
        return _clean_abstract(r.json().get("message", {}).get("abstract", ""))
    except Exception:
        return ""


def fetch_semanticscholar_abstract(pmid, doi):
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
    """Fehlende Abstracts der Reihe nach aus vier Quellen nachladen."""
    filled = 0
    for p in papers:
        if p.get("abstract") and p.get("authors"):
            continue
        data = fetch_europepmc_core(p["pmid"])
        if data:
            if not p.get("abstract"):
                ab = _clean_abstract(data.get("abstractText"))
                if ab:
                    p["abstract"] = ab
                    p["abstract_source"] = "Europe PMC"
                    filled += 1
            if not p.get("authors"):
                astr = (data.get("authorString") or "").strip().rstrip(".")
                if astr:
                    p["authors"] = [a.strip() for a in astr.split(",") if a.strip()]
        if not p.get("abstract") and p.get("doi"):
            ab = fetch_crossref_abstract(p["doi"])
            if ab:
                p["abstract"], p["abstract_source"] = ab, "Crossref"
                filled += 1
        if not p.get("abstract"):
            ab = fetch_semanticscholar_abstract(p["pmid"], p.get("doi"))
            if ab:
                p["abstract"], p["abstract_source"] = ab, "Semantic Scholar"
                filled += 1
        if not p.get("abstract"):
            ab = fetch_pubmed_publisher_abstract(p["pmid"])
            if ab:
                p["abstract"], p["abstract_source"] = ab, "PubMed (publisher)"
                filled += 1
        time.sleep(0.2)
    return filled


# ---------------------------------------------------------------------------
# Noise-Filter (Errata, Retractions) - Muster aus params/noise_titles.lst
# ---------------------------------------------------------------------------

NOISE_PATTERNS_FALLBACK = [
    r"^correction to\b", r"^corrigendum\b", r"^erratum\b", r"^author correction\b",
    r"^retraction\b", r"^retracted:", r"^withdrawn:", r"^expression of concern\b",
    r"^reply to\b", r"^response to\b",
]


def build_noise_matcher(path=NOISE_FILE_DEFAULT):
    patterns = load_list_file(path, description="Noise-Titelmuster") or NOISE_PATTERNS_FALLBACK
    valid = []
    for pat in patterns:
        try:
            re.compile(pat)
            valid.append(pat)
        except re.error as e:
            print(f"  !! Ungueltiges Regex in {path} ignoriert: {pat!r} ({e})")
    if not valid:
        valid = NOISE_PATTERNS_FALLBACK
    rx = re.compile("|".join(valid), re.IGNORECASE)
    return lambda title: bool(rx.match((title or "").strip()))


# ---------------------------------------------------------------------------
# Gemini-Aufruf mit Modell-Fallback-Kette
# ---------------------------------------------------------------------------

FALLBACK_MODELS = ["gemini-3.6-flash", "gemini-3.1-flash-lite"]
TRANSIENT_MARKERS = ("503", "UNAVAILABLE", "429", "RESOURCE_EXHAUSTED")
RETRY_DELAY_RE = re.compile(r"retry in ([\d.]+)s", re.IGNORECASE)
MAX_BACKOFF_SECONDS = 30


def gemini_json(system_prompt, user_prompt, primary_model, max_output_tokens=2000,
                label="Gemini"):
    """Ruft Gemini auf und liefert den Antworttext, oder "" bei endgueltigem Fehler.
    Wiederholt bei temporaeren Fehlern und wechselt sonst auf das naechste Modell."""
    if client is None:
        print("  !! Kein GEMINI_API_KEY gesetzt - Aufruf uebersprungen.")
        return ""

    models_to_try, seen = [], set()
    for m in [primary_model] + FALLBACK_MODELS:
        if m and m not in seen:
            seen.add(m)
            models_to_try.append(m)

    for model_name in models_to_try:
        is_last_model = model_name == models_to_try[-1]
        last_exc = None
        for attempt in range(2):
            try:
                response = client.models.generate_content(
                    model=model_name,
                    contents=user_prompt,
                    config=genai_types.GenerateContentConfig(
                        system_instruction=system_prompt,
                        response_mime_type="application/json",
                        max_output_tokens=max_output_tokens,
                    ),
                )
                return response.text or ""
            except Exception as e:
                last_exc = e
                is_transient = any(marker in str(e) for marker in TRANSIENT_MARKERS)
                if is_transient and attempt == 0:
                    m = RETRY_DELAY_RE.search(str(e))
                    delay = min(float(m.group(1)), MAX_BACKOFF_SECONDS) + 1 if m else 3
                    print(f"  !! Modell '{model_name}' temporaer nicht verfuegbar "
                          f"({type(e).__name__}) - warte {delay:.0f}s...")
                    time.sleep(delay)
                    continue
                break
        if not is_last_model:
            print(f"  !! Modell '{model_name}' fehlgeschlagen "
                  f"({type(last_exc).__name__}: {last_exc}) - naechstes Fallback-Modell...")
            continue
        print(f"  !! {label} API-Aufruf FEHLGESCHLAGEN ({type(last_exc).__name__}): {last_exc}")
    return ""


def _extract_json(text, fallback):
    try:
        return json.loads(text)
    except Exception:
        m = re.search(r"\{.*\}", text or "", re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                pass
    return dict(fallback)


# ---------------------------------------------------------------------------
# Modul 1 - Triage / Scoring
# ---------------------------------------------------------------------------

SCORING_SYSTEM_FALLBACK = (
    "You are a senior academic specialist and evidence-based-medicine expert triaging "
    "brand-new literature for busy practising clinicians. Be rigorous and skeptical: the "
    "large majority of papers do NOT change practice. Reward strong designs (RCTs, "
    "guidelines, large trials) and genuine clinical impact; penalise small, preliminary "
    "or purely mechanistic work. Judge ONLY from the text provided. If the abstract is "
    "missing, score conservatively."
)

# Der Rubric-Text kann komplett aus params/score_rubric.txt kommen. Der Platzhalter
# {subgroups} wird durch die JSON-Liste der geladenen Subgroups ersetzt.
SCORING_RUBRIC_FALLBACK = """Return a single JSON object and nothing else:
{
  "subgroup": one of {subgroups} or "Other",
  "study_type": "RCT" | "Meta-analysis / Systematic review" | "Guideline / Consensus"
                | "Large observational / Registry" | "Small observational"
                | "Basic / Translational" | "Review / Editorial" | "Other",
  "statement_type": "Major guideline" | "Practical statement" | "Primary study or other",
  "interventional": true or false,
  "relevance_score": integer 1-10,
  "practice_changing": true or false,
  "why_it_matters": one sentence,
  "bottom_line": one or two sentences, concrete clinical takeaway
}

statement_type - how to classify:
  "Major guideline"        = ONLY a COMPLETE clinical practice guideline or a universal
                             disease DEFINITION from a major society. These are only NAMED,
                             never summarised.
  "Practical statement"    = a scientific statement, expert consensus, position paper or a
                             focused best-practice / technical document. These WILL be
                             summarised.
  "Primary study or other" = original research, narrative reviews, editorials, methods
                             papers, case reports, publishing-policy documents. Score pure
                             policy or non-clinical documents very low (1-2).

Score anchors:
  9-10  practice-changing RCT / new guideline in a major journal
  7-8   important; strong evidence likely to influence practice or debate
  4-6   incremental, confirmatory, or narrow relevance
  1-3   preliminary, niche, mechanistic, or low clinical relevance
"""

SCORE_EMPTY = {
    "subgroup": "Other", "structural_topic": None, "study_type": "Other",
    "statement_type": "Primary study or other", "interventional": False,
    "relevance_score": 0, "practice_changing": False,
    "why_it_matters": "", "bottom_line": "(scoring failed)",
}


def build_scoring_rubric(subgroups, path=SCORING_RUBRIC_FILE_DEFAULT):
    """Rubric aus Datei laden; {subgroups} wird mit der Subgroup-Liste gefuellt."""
    template = load_text_file(path, SCORING_RUBRIC_FALLBACK, description="Scoring-Rubric")
    return template.replace("{subgroups}", json.dumps(subgroups))


def load_scoring_system(path=SCORING_SYSTEM_FILE_DEFAULT):
    return load_text_file(path, SCORING_SYSTEM_FALLBACK, description="Scoring-System-Prompt")


def score_paper(paper, ref_files=None, key_overrides=None, rubric=None, scoring_system=None):
    pmid = str(paper.get("pmid", ""))
    if key_overrides and pmid in key_overrides:
        return {**SCORE_EMPTY, "relevance_score": 10, "practice_changing": True,
                "bottom_line": key_overrides[pmid], "why_it_matters": key_overrides[pmid]}

    ref_context = (f"\nLocal reference guidelines available: {', '.join(ref_files)}"
                   if ref_files else "")
    user = (
        f"Title: {paper['title']}\n"
        f"Journal: {paper['journal']} ({paper['pub_date']})\n"
        f"Publication types (may be incomplete for new papers): "
        f"{', '.join(paper.get('pub_types') or []) or 'n/a'}\n"
        f"{ref_context}\n\n"
        f"Abstract:\n{paper.get('abstract') or '(no abstract available)'}\n\n"
        f"{rubric}"
    )
    text = gemini_json(scoring_system, user, LLM_MODEL,
                       max_output_tokens=800, label="Modul 1 Scoring")
    return _extract_json(text, SCORE_EMPTY)


# ---------------------------------------------------------------------------
# Modul 2 - ausfuehrliche Zusammenfassungen (nur fuer die selektierten Top-N)
# ---------------------------------------------------------------------------

SUMMARY_SYSTEM_FALLBACK = (
    "You are an experienced clinician writing concise, strictly factual English summaries "
    "of new studies for fellow specialists."
    "\n\nRULE 1 - NO HALLUCINATIONS. Every factual statement (numbers, endpoints, "
    "population, results) must come EXCLUSIVELY from the abstract provided. If information "
    "is not in the abstract, do not invent it and do not state it. Never invent trial names, "
    "effect sizes, p-values or guideline recommendations. Keep the original terminology of "
    "the source; do not translate."
    "\n\nRULE 2 - NO CLINICAL ADVICE. You describe what a paper reports; you never advise "
    "anyone what to do. This is a legal requirement, not a style preference. Never write "
    "recommendations, suggestions or calls to action. Specifically forbidden: 'should', "
    "'should be considered', 'recommend', 'is recommended', 'clinicians should', 'consider "
    "using', 'warrants adoption', 'supports the use of', 'is indicated', 'must', 'we advise', "
    "and any imperative addressed to the reader. Attribute every conclusion to its source "
    "instead: 'the authors conclude that ...', 'the trial reported ...', 'the study found ...'."
)

SUMMARY_RUBRIC_FALLBACK = """Return a single JSON object and nothing else:
{
  "summary": "10-14 sentences, based EXCLUSIVELY on the abstract. Cover: background/
              research question, study design, population and inclusion, intervention
              and comparator, primary endpoint with numbers, key secondary endpoints
              with numbers, safety/adverse events, limitations (if stated in the
              abstract), and what the authors conclude. Purely descriptive - report
              what the paper says, never what the reader should do.",
  "relation_evidence": "1-3 sentences placing the study in the context of existing
              evidence, descriptively. ONLY broadly established knowledge. Do NOT invent
              specific trials, numbers or results. No advice. If no confident placement
              is possible, write exactly: 'Cannot be placed in context from the abstract
              alone.'",
  "relation_guidelines": "1-3 sentences DESCRIBING how the findings relate to current
              guidelines - for example that they are consistent with, challenge, or refine
              what current guidelines state. NEVER invent a specific class of recommendation
              or level of evidence, and NEVER tell the reader to follow, adopt or change
              anything. If uncertain, write exactly: 'No clear guideline link can be derived.'",
  "authors_conclusion": "1-2 sentences stating what THE AUTHORS conclude, explicitly
              attributed to them (start with 'The authors conclude that ...' or similar).
              Strictly descriptive. No advice, no recommendation, no imperative.",
  "key_numbers": {
    "design": "e.g. 'RCT, double-blind, multicentre'  (or null)",
    "n": "total participants as a string, e.g. '4321'  (or null)",
    "followup": "e.g. '12 months'  (or null)",
    "primary_endpoint": {
      "name": "name + timepoint, e.g. 'Target lesion failure, 12 mo'",
      "arm1_label": "short label", "arm1_value": "e.g. '6.1%'",
      "arm2_label": "short label", "arm2_value": "e.g. '7.0%'",
      "effect": "e.g. 'Risk difference -0.9% (95% CI -2.1 to 0.3), non-inferior'"
    },
    "secondary": [
      {"name": "e.g. 'Cardiac death'", "arm1": "1.6%", "arm2": "1.8%", "effect": "-0.2%"}
    ],
    "key_findings": [
      {"label": "e.g. 'Operative mortality'", "value": "e.g. '0.4% (n=2)'"}
    ]
  }
}
IMPORTANT for key_numbers: Only take values stated VERBATIM in the abstract. If a value
is missing, set it to null (leave 'primary_endpoint' null and 'secondary' empty []).
Never invent numbers, endpoints, arms or confidence intervals. Max 5 secondary rows.

key_findings is REQUIRED: always fill 3-6 rows whenever the abstract contains any
quantitative or concrete factual statements. This is what gets displayed for papers with
no two-arm comparison (registries, single-arm series, prevalence studies, consensus
documents). Each row is a short label plus the value exactly as stated in the abstract.
Leave it empty only if the abstract contains nothing concrete.

Write in a factual, clinical, descriptive register. No marketing language, no advice.
"""


def _empty_summary():
    return {"summary": "", "relation_evidence": "", "relation_guidelines": "",
            "authors_conclusion": "", "key_numbers": {}}


def load_summary_system(path=SUMMARY_SYSTEM_FILE_DEFAULT):
    return load_text_file(path, SUMMARY_SYSTEM_FALLBACK, description="Summary-System-Prompt")


def load_summary_rubric(path=SUMMARY_RUBRIC_FILE_DEFAULT):
    return load_text_file(path, SUMMARY_RUBRIC_FALLBACK, description="Summary-Rubric")


def summarize_paper(paper, summary_system, summary_rubric, ref_files=None):
    if not paper.get("abstract"):
        s = _empty_summary()
        s["summary"] = "No abstract available for this paper yet (selected on title/journal)."
        return s
    ref_context = (f"\nLocal reference guidelines available: {', '.join(ref_files)}"
                   if ref_files else "")
    user = (
        f"Title: {paper['title']}\n"
        f"Journal: {paper['journal']} ({paper['pub_date']})"
        f"{ref_context}\n\n"
        f"Abstract:\n{paper['abstract']}\n\n"
        f"{summary_rubric}"
    )
    text = gemini_json(summary_system, user, SUMMARY_MODEL,
                       max_output_tokens=4000, label="Modul 2 Summary")
    if not text:
        return _empty_summary()
    result = _extract_json(text, _empty_summary())
    if not isinstance(result, dict) or not str(result.get("summary") or "").strip():
        print(f"  !! Modul 2 lieferte keine brauchbare Zusammenfassung fuer: "
              f"{paper['title'][:50]}")
        return _empty_summary()
    return result


# ---------------------------------------------------------------------------
# Zustand (seen / attempts / summary cache) - im Ordner state/
# ---------------------------------------------------------------------------

_STATE_DIR = Path(STATE_DIR_DEFAULT)


def set_state_dir(path):
    global _STATE_DIR
    _STATE_DIR = Path(path)
    _STATE_DIR.mkdir(parents=True, exist_ok=True)


def _sp(name):
    return _STATE_DIR / name


def _load_state(name, default):
    p = _sp(name)
    if not p.is_file():
        return default
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"  !! Konnte {p} nicht laden ({type(e).__name__}): {e}")
        return default


def _save_state(name, data):
    with open(_sp(name), "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def load_seen():
    return set(_load_state(SEEN_FILE, []))


def save_seen(seen):
    _save_state(SEEN_FILE, sorted(seen))


def load_attempts():
    return dict(_load_state(ATTEMPTS_FILE, {}))


def save_attempts(attempts):
    _save_state(ATTEMPTS_FILE, attempts)


def load_summ_cache():
    return dict(_load_state(SUMM_CACHE_FILE, {}))


def save_summ_cache(cache):
    _save_state(SUMM_CACHE_FILE, cache)


# ---------------------------------------------------------------------------
# Dedup, Guideline-Erkennung, Hilfsfunktionen fuers Rendering
# ---------------------------------------------------------------------------


def is_guideline_doc(p):
    s = p.get("score", {}) or {}
    return (s.get("statement_type") in ("Major guideline", "Practical statement")
            or s.get("study_type") == "Guideline / Consensus")


def _norm_title(t):
    t = (t or "").lower().strip()
    return re.sub(r"\s+", " ", t).rstrip(".")


def dedup_by_title(papers):
    """Gleiche Arbeit in mehreren Journals -> ein Eintrag mit sources-Liste."""
    groups = {}
    for p in papers:
        groups.setdefault(_norm_title(p["title"]), []).append(p)
    merged = []
    for grp in groups.values():
        grp.sort(key=lambda x: (x.get("score") or {}).get("relevance_score", 0), reverse=True)
        best = dict(grp[0])
        seen_urls, sources = set(), []
        for g in grp:
            if g["url"] not in seen_urls:
                seen_urls.add(g["url"])
                sources.append({"journal": g["journal"], "url": g["url"]})
        best["sources"] = sources
        merged.append(best)
    return merged


def _cell(v):
    s = "" if v is None else str(v).strip()
    return "\u2014" if s.lower() in ("", "none", "null", "n/a", "na", "nan") else s


def _chart_values(pe):
    """(v1, v2) nur wenn beide Arme numerisch sind UND dieselbe Einheit tragen."""
    def num(s):
        m = re.search(r"-?\d+(?:[.,]\d+)?", str(s or ""))
        return float(m.group(0).replace(",", ".")) if m else None

    def unit(s):
        return re.sub(r"[-\d.,\s]", "", str(s or "")).lower()

    a, b = (pe or {}).get("arm1_value"), (pe or {}).get("arm2_value")
    v1, v2 = num(a), num(b)
    if v1 is None or v2 is None or v1 < 0 or v2 < 0:
        return None, None
    if unit(a) != unit(b):
        return None, None
    return v1, v2


_ADVISORY_RE = re.compile(
    r"\b(?:clinicians?|physicians?|operators?|readers?|we|you)\s+(?:should|must|ought)\b"
    r"|\bshould\s+(?:be\s+(?:considered|used|offered|adopted|implemented|preferred)|"
    r"undergo|receive|prefer|adopt|use|switch)\b"
    r"|\b(?:is|are)\s+recommended\b"
    r"|\bwe\s+(?:recommend|advise|suggest)\b"
    r"|\bwarrants?\s+(?:adoption|implementation)\b"
    r"|\bconsider\s+(?:using|adopting|switching|offering)\b",
    re.IGNORECASE)


def advisory_flags(summary):
    hits = []
    for k in ("summary", "relation_evidence", "relation_guidelines",
              "authors_conclusion", "key_message"):
        m = _ADVISORY_RE.search(str((summary or {}).get(k) or ""))
        if m:
            hits.append((k, m.group(0).strip()))
    return hits


def resolve_subgroup(name, subgroups, aliases):
    n = (name or "").strip().lower()
    if not n:
        return None
    if n in aliases:
        return aliases[n]
    for g in subgroups:
        if g.lower() == n:
            return g
    for g in subgroups:
        if n in g.lower():
            return g
    return None


# ---------------------------------------------------------------------------
# Markdown-Digest
# ---------------------------------------------------------------------------


def _render_paper(p, include_abstract=True):
    s = p.get("score") or {}
    flag = "  \u2b50 practice-changing" if s.get("practice_changing") else ""
    iv = "  \u00b7 interventional" if s.get("interventional") else ""
    authors = p.get("authors") or []
    auth = ", ".join(authors[:3]) + (" et al." if len(authors) > 3 else "")
    sources = p.get("sources") or [{"journal": p["journal"], "url": p["url"]}]
    src_line = " \u00b7 ".join(f"[{x['journal']}]({x['url']})" for x in sources)
    out = [
        f"### [{p['title']}]({sources[0]['url']})",
        f"*{src_line} \u00b7 {p['pub_date']} \u00b7 {auth}*  ",
        f"**{s.get('study_type', '?')} \u00b7 relevance "
        f"{s.get('relevance_score', '?')}/10{flag}{iv}**",
        "",
        f"**Why it matters:** {s.get('why_it_matters', '')}  ",
        f"**Bottom line:** {s.get('bottom_line', '')}",
        "",
    ]
    if include_abstract:
        out += [
            "<details><summary>Original abstract (internal QC only)</summary>",
            "",
            p.get("abstract") or "(no abstract available)",
            "",
            f"DOI: {p.get('doi') or 'n/a'} \u00b7 PMID: {p['pmid']}",
            "</details>",
            "",
        ]
    else:
        out += [f"DOI: {p.get('doi') or 'n/a'} \u00b7 PMID: {p['pmid']}", ""]
    return out


def build_digest(scored, subgroups, title, ref_files=None, include_abstract=True):
    today = dt.date.today().isoformat()
    lines = [f"# {title} - week of {today}", ""]
    if ref_files:
        lines += [f"_Verwendete Referenzleitlinien: {', '.join(ref_files)}_", ""]

    guidelines = [p for p in scored if is_guideline_doc(p)]
    rest = [p for p in scored if not is_guideline_doc(p)]

    g_merged = sorted(dedup_by_title(guidelines),
                      key=lambda x: (x.get("score") or {}).get("relevance_score", 0),
                      reverse=True)
    if g_merged:
        lines += ["## Guidelines, consensus & position papers this week",
                  "*For awareness - please read the original (deliberately not summarised).*",
                  ""]
        for p in g_merged:
            srcs = " \u00b7 ".join(f"[{x['journal']}]({x['url']})" for x in p["sources"])
            lines.append(f"- **{p['title']}**  ")
            lines.append(f"  {srcs}")
        lines.append("")

    gated = dedup_by_title([p for p in rest
                            if (p.get("score") or {}).get("relevance_score", 0) >= MIN_SCORE])

    interv = sorted([p for p in gated if (p.get("score") or {}).get("interventional")],
                    key=lambda x: x["score"]["relevance_score"], reverse=True)
    if interv:
        lines += ["## Interventional - cross-cutting view", ""]
        for p in interv[:MAX_PER_SUBGROUP + 3]:
            lines += _render_paper(p, include_abstract)

    by_group = {g: [] for g in subgroups}
    for p in gated:
        g = (p.get("score") or {}).get("subgroup", "Other")
        if g in by_group:
            by_group[g].append(p)
    for g in subgroups:
        items = sorted(by_group[g], key=lambda x: x["score"]["relevance_score"],
                       reverse=True)[:MAX_PER_SUBGROUP]
        if not items:
            continue
        lines += [f"## {g}", ""]
        for p in items:
            lines += _render_paper(p, include_abstract)

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Word-Dokument (das bisher fehlende Stueck)
# ---------------------------------------------------------------------------


def build_docx(items, date_str, days, out_path, subgroups, branding, disclaimer,
               guidelines=None, pending=None):
    """Teilbares CLARITY-Word-Dokument: eine Seite pro Studie, Kennzahlen-Panel,
    Primaerendpunkt-Chart, Sekundaerendpunkt-Tabelle, englische Zusammenfassungen.
    Keine Abstracts im Volltext, keine erfundenen Zahlen."""
    from docx import Document
    from docx.shared import Pt, Inches, RGBColor
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.enum.table import WD_ROW_HEIGHT_RULE
    from docx.oxml.ns import qn
    from docx.oxml import OxmlElement
    from docx.opc.constants import RELATIONSHIP_TYPE as RT

    pal = branding["palette"]
    TEAL, TEAL_D, MINT = pal["TEAL"], pal["TEAL_D"], pal["MINT"]
    MINT_BG, CREAM, INK = pal["MINT_BG"], pal["CREAM"], pal["INK"]
    MUTE, PANEL, HAIR = pal["MUTE"], pal["PANEL"], pal["HAIR"]
    BRAND, FIELD_NAME = branding["brand"], branding["field_name"]

    groups = [(g, [p for p in items if (p.get("score") or {}).get("subgroup") == g])
              for g in subgroups]
    groups = [(g, ps) for g, ps in groups if ps]
    total = sum(len(ps) for _, ps in groups)

    doc = Document()
    sec = doc.sections[0]
    sec.top_margin = sec.bottom_margin = Inches(0.85)
    sec.left_margin = sec.right_margin = Inches(0.8)
    CONTENT_W = sec.page_width - sec.left_margin - sec.right_margin

    base = doc.styles["Normal"]
    base.font.name = "Calibri"
    base.font.size = Pt(10)
    base.font.color.rgb = RGBColor.from_string(INK)

    # ---- Low-Level-Helfer -------------------------------------------------
    def shade(cell, fill):
        tcPr = cell._tc.get_or_add_tcPr()
        shd = OxmlElement("w:shd")
        shd.set(qn("w:val"), "clear")
        shd.set(qn("w:color"), "auto")
        shd.set(qn("w:fill"), fill)
        tcPr.append(shd)

    def borderless(table):
        borders = OxmlElement("w:tblBorders")
        for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
            e = OxmlElement(f"w:{edge}")
            e.set(qn("w:val"), "none")
            e.set(qn("w:sz"), "0")
            borders.append(e)
        table._tbl.tblPr.append(borders)

    def keep_together(table):
        for row in table.rows:
            trPr = row._tr.get_or_add_trPr()
            trPr.append(OxmlElement("w:cantSplit"))

    def write(p, text="", size=10, bold=False, color=INK, italic=False,
              before=0, after=3, align=None):
        p.paragraph_format.space_before = Pt(before)
        p.paragraph_format.space_after = Pt(after)
        if align is not None:
            p.alignment = align
        if text:
            r = p.add_run(text)
            r.font.size = Pt(size)
            r.bold = bold
            r.italic = italic
            r.font.color.rgb = RGBColor.from_string(color)
        return p

    def para(text="", **kw):
        return write(doc.add_paragraph(), text, **kw)

    def label(text):
        return para(text, size=8, bold=True, color=MINT, before=9, after=1)

    def hyperlink(p, url, text, color=MINT, size=8):
        r_id = p.part.relate_to(url, RT.HYPERLINK, is_external=True)
        h = OxmlElement("w:hyperlink")
        h.set(qn("r:id"), r_id)
        r = OxmlElement("w:r")
        rPr = OxmlElement("w:rPr")
        c = OxmlElement("w:color")
        c.set(qn("w:val"), color)
        rPr.append(c)
        u = OxmlElement("w:u")
        u.set(qn("w:val"), "single")
        rPr.append(u)
        sz = OxmlElement("w:sz")
        sz.set(qn("w:val"), str(int(size * 2)))
        rPr.append(sz)
        r.append(rPr)
        t = OxmlElement("w:t")
        t.text = text
        r.append(t)
        h.append(r)
        p._p.append(h)

    def box(fill=MINT_BG):
        t = doc.add_table(rows=1, cols=1)
        borderless(t)
        keep_together(t)
        c = t.cell(0, 0)
        shade(c, fill)
        c.width = CONTENT_W
        return c

    def endpoint_chart(v1, d1, l1, v2, d2, l2):
        """Kleines Balkendiagramm als PNG. None, wenn matplotlib fehlt/scheitert."""
        try:
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
            fig, ax = plt.subplots(figsize=(3.4, 1.85), dpi=220)
            bars = ax.bar([l1 or "Arm 1", l2 or "Arm 2"], [v1, v2],
                          color=[f"#{TEAL}", f"#{MINT}"], width=0.5)
            for b, disp in zip(bars, [d1, d2]):
                ax.text(b.get_x() + b.get_width() / 2, b.get_height(), disp,
                        ha="center", va="bottom", fontsize=8.5,
                        fontweight="bold", color=f"#{INK}")
            ax.set_ylim(0, (max(v1, v2) or 1) * 1.3)
            for s in ("top", "right", "left"):
                ax.spines[s].set_visible(False)
            ax.spines["bottom"].set_color(f"#{HAIR}")
            ax.get_yaxis().set_visible(False)
            ax.tick_params(axis="x", length=0, labelsize=8.5, colors=f"#{MUTE}")
            fig.patch.set_alpha(0)
            fig.tight_layout(pad=0.25)
            buf = io.BytesIO()
            fig.savefig(buf, format="png", transparent=True)
            plt.close(fig)
            buf.seek(0)
            return buf
        except Exception:
            return None

    # ---- Fusszeile --------------------------------------------------------
    fp = sec.footer.paragraphs[0]
    fp.alignment = WD_ALIGN_PARAGRAPH.LEFT
    fr = fp.add_run(f"{BRAND} \u00b7 {FIELD_NAME} \u00b7 {date_str}\t\t")
    fr.font.size = Pt(7)
    fr.font.color.rgb = RGBColor.from_string(MUTE)
    pr = fp.add_run()
    pr.font.size = Pt(7)
    pr.font.color.rgb = RGBColor.from_string(MUTE)
    for tag, attrs, text in (("w:fldChar", {"w:fldCharType": "begin"}, None),
                             ("w:instrText", {"xml:space": "preserve"}, "PAGE"),
                             ("w:fldChar", {"w:fldCharType": "end"}, None)):
        el = OxmlElement(tag)
        for k, v in attrs.items():
            el.set(qn(k), v)
        if text:
            el.text = text
        pr._r.append(el)

    # ---- Titelseite -------------------------------------------------------
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
    write(bc.add_paragraph(), branding.get("tagline", ""), size=11, color=MINT,
          align=WD_ALIGN_PARAGRAPH.CENTER, after=0)

    para("", after=26)
    para("The most relevant publications of the week", size=15, bold=True,
         color=TEAL, align=WD_ALIGN_PARAGRAPH.CENTER, after=4)
    para(f"{FIELD_NAME}   \u00b7   {date_str}   \u00b7   {total} "
         f"{'study' if total == 1 else 'studies'} across {len(groups)} "
         f"{'subspecialty' if len(groups) == 1 else 'subspecialties'}",
         size=10.5, color=MUTE, align=WD_ALIGN_PARAGRAPH.CENTER, after=22)

    para("Contents", size=14, bold=True, color=TEAL, after=2)
    para(f"AI-curated from the leading {FIELD_NAME.lower()}-relevant journals "
         f"of the last {days} days.", size=9, color=MUTE, after=8)
    for g, ps in groups:
        p = doc.add_paragraph()
        p.paragraph_format.space_after = Pt(3)
        r1 = p.add_run("\u25cf ")
        r1.font.size = Pt(10)
        r1.font.color.rgb = RGBColor.from_string(MINT)
        r2 = p.add_run(g)
        r2.font.size = Pt(10.5)
        r2.font.color.rgb = RGBColor.from_string(INK)
        r3 = p.add_run(f"  \u2014 {len(ps)} {'study' if len(ps) == 1 else 'studies'}")
        r3.font.size = Pt(9.5)
        r3.font.color.rgb = RGBColor.from_string(MUTE)

    # ---- Studienseiten ----------------------------------------------------
    for g, ps in groups:
        for idx, p_ in enumerate(ps, 1):
            doc.add_page_break()
            s = p_.get("summary", {}) or {}
            kn = s.get("key_numbers") or {}
            sources = p_.get("sources") or [{"journal": p_["journal"], "url": p_["url"]}]
            authors = p_.get("authors") or []
            auth = ", ".join(authors[:3]) + (" et al." if len(authors) > 3 else "")

            para(g.upper(), size=8, bold=True, color=MINT, after=2)

            tp = doc.add_paragraph()
            tp.paragraph_format.space_after = Pt(1)
            rn = tp.add_run(f"{idx}  ")
            rn.font.size = Pt(15)
            rn.bold = True
            rn.font.color.rgb = RGBColor.from_string(MINT)
            rt = tp.add_run(p_["title"])
            rt.font.size = Pt(15)
            rt.bold = True
            rt.font.color.rgb = RGBColor.from_string(INK)

            meta = " \u00b7 ".join(x["journal"] for x in sources) + " \u00b7 " + str(p_["pub_date"])
            if auth:
                meta += " \u00b7 " + auth
            para(meta, size=8.5, color=MUTE, after=2)

            kick = [(p_.get("score") or {}).get("study_type", "")]
            if (p_.get("score") or {}).get("interventional"):
                kick.append("interventional")
            para("\u25cf " + "  \u00b7  ".join(x for x in kick if x).upper(),
                 size=8, bold=True, color=TEAL, after=6)

            # Kennzahlen-Panel
            cells = [(kn.get(k), lab) for k, lab in
                     (("design", "DESIGN"), ("n", "PARTICIPANTS"), ("followup", "FOLLOW-UP"))
                     if kn.get(k)]
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

            # Primaerendpunkt
            pe = kn.get("primary_endpoint")
            if pe and pe.get("name"):
                c = box(MINT_BG)
                write(c.paragraphs[0], "PRIMARY ENDPOINT \u2014 " + str(pe["name"]),
                      size=8, bold=True, color=TEAL_D, after=3)
                v1, v2 = _chart_values(pe)
                png = (endpoint_chart(v1, str(pe.get("arm1_value")), str(pe.get("arm1_label", "")),
                                      v2, str(pe.get("arm2_value")), str(pe.get("arm2_label", "")))
                       if v1 is not None else None)
                if png is not None:
                    ip = c.add_paragraph()
                    ip.alignment = WD_ALIGN_PARAGRAPH.CENTER
                    ip.paragraph_format.space_after = Pt(2)
                    ip.add_run().add_picture(png, width=Inches(3.2))
                else:
                    vp = c.add_paragraph()
                    write(vp, "", after=2)
                    for lab_, val_, col in ((pe.get("arm1_label", ""), pe.get("arm1_value", ""), TEAL),
                                            (pe.get("arm2_label", ""), pe.get("arm2_value", ""), MINT)):
                        if val_:
                            r = vp.add_run(f"{lab_}: ")
                            r.font.size = Pt(9)
                            r.bold = True
                            r.font.color.rgb = RGBColor.from_string(MUTE)
                            r2 = vp.add_run(f"{val_}    ")
                            r2.font.size = Pt(11)
                            r2.bold = True
                            r2.font.color.rgb = RGBColor.from_string(col)
                if pe.get("effect"):
                    write(c.add_paragraph(), str(pe["effect"]), size=9, color=INK, after=0)
                para("", after=4)

            # Sekundaerendpunkte
            rows = [r for r in (kn.get("secondary") or []) if r.get("name") and
                    any(_cell(r.get(k)) != "\u2014" for k in ("arm1", "arm2", "effect"))][:5]
            if rows:
                label("OTHER ENDPOINTS")
                head = ["Endpoint",
                        (pe.get("arm1_label") if pe else None) or "Arm 1",
                        (pe.get("arm2_label") if pe else None) or "Arm 2",
                        "Effect"]
                t = doc.add_table(rows=len(rows) + 1, cols=4)
                t.style = "Table Grid"
                keep_together(t)
                widths = [int(CONTENT_W * f) for f in (0.40, 0.16, 0.16, 0.28)]
                for i, h in enumerate(head):
                    c = t.cell(0, i)
                    shade(c, TEAL)
                    c.width = widths[i]
                    write(c.paragraphs[0], str(h), size=7.6, bold=True, color="FFFFFF", after=0)
                for ri, r in enumerate(rows, 1):
                    vals = [_cell(r.get("name")), _cell(r.get("arm1")),
                            _cell(r.get("arm2")), _cell(r.get("effect"))]
                    for ci, v in enumerate(vals):
                        c = t.cell(ri, ci)
                        c.width = widths[ci]
                        if ri % 2 == 0:
                            shade(c, "F5F9F7")
                        write(c.paragraphs[0], str(v), size=8,
                              bold=(ci in (1, 2)), color=INK, after=0)
                para("", after=4)

            # Fallback-Visual: Key Findings als Tabelle
            findings = [f for f in (kn.get("key_findings") or [])
                        if f.get("label") and f.get("value")][:6]
            if not rows and findings:
                label("KEY FIGURES REPORTED")
                t = doc.add_table(rows=len(findings), cols=2)
                t.style = "Table Grid"
                keep_together(t)
                w = [int(CONTENT_W * 0.55), int(CONTENT_W * 0.45)]
                for ri, f in enumerate(findings):
                    for ci, v in enumerate((_cell(f.get("label")), _cell(f.get("value")))):
                        c = t.cell(ri, ci)
                        c.width = w[ci]
                        if ri % 2 == 1:
                            shade(c, "F5F9F7")
                        write(c.paragraphs[0], str(v), size=8.5,
                              bold=(ci == 1), color=INK if ci == 0 else TEAL, after=0)
                para("", after=4)

            for lab_, key in (("SUMMARY", "summary"),
                              ("RELATION TO EXISTING EVIDENCE", "relation_evidence"),
                              ("RELATION TO GUIDELINES", "relation_guidelines")):
                if s.get(key):
                    label(lab_)
                    para(str(s[key]), size=9.5, after=2)

            if s.get("authors_conclusion") or s.get("key_message"):
                para("", after=4)
                c = box(MINT_BG)
                write(c.paragraphs[0], "AUTHORS' CONCLUSION (AS REPORTED)",
                      size=8, bold=True, color=TEAL_D, after=2)
                write(c.add_paragraph(),
                      str(s.get("authors_conclusion") or s.get("key_message")),
                      size=9.8, bold=True, color=TEAL_D, after=0)
                para("", after=4)

            lp = doc.add_paragraph()
            lp.paragraph_format.space_before = Pt(6)
            hyperlink(lp, sources[0]["url"], "View on PubMed")
            rd = lp.add_run(f"  \u00b7  DOI: {p_.get('doi') or 'n/a'}")
            rd.font.size = Pt(8)
            rd.font.color.rgb = RGBColor.from_string(MUTE)

    # ---- Anhaenge ---------------------------------------------------------
    pend = dedup_by_title(pending or [])
    pend.sort(key=lambda x: (x.get("score") or {}).get("relevance_score", 0), reverse=True)
    gl = dedup_by_title([x for x in (guidelines or []) if is_guideline_doc(x)])
    gl.sort(key=lambda x: (x.get("score") or {}).get("relevance_score", 0), reverse=True)

    if pend or gl:
        doc.add_page_break()
    if pend:
        para("New this week \u2014 abstract pending", size=14, bold=True, color=TEAL, after=2)
        para("Highly relevant, very recent publications whose abstract is not yet "
             "available. They will be summarised automatically once it appears.",
             size=9, color=MUTE, after=8)
        for x in pend:
            para(x["title"], size=9.5, bold=True, after=1)
            srcs = " \u00b7 ".join(y["journal"] for y in (x.get("sources")
                                                          or [{"journal": x["journal"]}]))
            lp = doc.add_paragraph()
            lp.paragraph_format.space_after = Pt(7)
            r = lp.add_run(srcs + "  \u00b7  ")
            r.font.size = Pt(8.5)
            r.font.color.rgb = RGBColor.from_string(MUTE)
            hyperlink(lp, (x.get("sources") or [{"url": x["url"]}])[0]["url"], "PubMed")

    if gl:
        para("Guidelines, consensus & position papers this week", size=14, bold=True,
             color=TEAL, before=10, after=2)
        para("Named for awareness \u2014 deliberately not summarised; please read the "
             "original.", size=9, color=MUTE, after=8)
        for x in gl:
            para(x["title"], size=9.5, bold=True, after=1)
            para(" \u00b7 ".join(y["journal"] for y in (x.get("sources")
                                                        or [{"journal": x["journal"]}])),
                 size=8.5, color=MUTE, after=7)

    hr = doc.add_paragraph()
    hr.paragraph_format.space_before = Pt(10)
    pbdr = OxmlElement("w:pBdr")
    top = OxmlElement("w:top")
    top.set(qn("w:val"), "single")
    top.set(qn("w:sz"), "4")
    top.set(qn("w:space"), "1")
    top.set(qn("w:color"), HAIR)
    pbdr.append(top)
    hr._p.get_or_add_pPr().append(pbdr)
    para(disclaimer, size=7.5, italic=True, color=MUTE, after=0)

    doc.save(str(out_path))


# ---------------------------------------------------------------------------
# Auswahl der Studien fuer das Word-Dokument
# ---------------------------------------------------------------------------


def select_for_document(scored, target_subs, top):
    """Liefert (selected, pending). Guidelines bekommen nie eine Studienseite;
    Papers ohne Abstract wandern in 'pending' und werden nachrueckend ersetzt."""
    pool = dedup_by_title([p for p in scored
                           if not is_guideline_doc(p)
                           and (p.get("score") or {}).get("relevance_score", 0) >= DOC_MIN_SCORE
                           and (p.get("score") or {}).get("subgroup") in target_subs])
    eligible = [p for p in pool if p.get("abstract")]
    no_abstract = [p for p in pool if not p.get("abstract")]

    by_sub = {g: [] for g in target_subs}
    for p in eligible:
        by_sub[p["score"]["subgroup"]].append(p)
    selected = []
    for g in target_subs:
        selected.extend(sorted(by_sub[g], key=lambda x: x["score"]["relevance_score"],
                               reverse=True)[:top])
    pending = sorted(no_abstract, key=lambda x: x["score"]["relevance_score"], reverse=True)
    return selected, pending


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description="ClarityDigest - PubMed Triage")
    parser.add_argument("--days", type=int, default=RELDATE_DAYS, help="Lookback in Tagen")
    parser.add_argument("--limit", type=int, default=0, help="Anzahl bewerteter Papers begrenzen")
    parser.add_argument("--specialty-dir", type=str, default=None,
                        help="Fachgebiets-Ordner (params/, refs/, state/, output/). "
                             "Das Script wechselt vor der Verarbeitung dorthin.")
    parser.add_argument("--state-dir", type=str, default=STATE_DIR_DEFAULT,
                        help="Ordner fuer seen_pmids.json, abstract_attempts.json, "
                             "summaries.json, scored_raw.json.")
    parser.add_argument("--key-findings", type=str, default="key_findings.json")
    parser.add_argument("--refs-dir", type=str, default="refs")
    parser.add_argument("--subgroups-file", type=str, default=SUBGROUPS_FILE_DEFAULT)
    parser.add_argument("--journals-spec-file", type=str, default=JOURNALS_SPEC_FILE_DEFAULT)
    parser.add_argument("--journals-gen-file", type=str, default=JOURNALS_GEN_FILE_DEFAULT)
    parser.add_argument("--tiab-keywords-file", type=str, default=TIAB_KEYWORDS_FILE_DEFAULT)
    parser.add_argument("--scoring-system-file", type=str, default=SCORING_SYSTEM_FILE_DEFAULT)
    parser.add_argument("--scoring-rubric-file", type=str, default=SCORING_RUBRIC_FILE_DEFAULT)
    parser.add_argument("--summary-system-file", type=str, default=SUMMARY_SYSTEM_FILE_DEFAULT)
    parser.add_argument("--summary-rubric-file", type=str, default=SUMMARY_RUBRIC_FILE_DEFAULT)
    parser.add_argument("--noise-file", type=str, default=NOISE_FILE_DEFAULT)
    parser.add_argument("--aliases-file", type=str, default=ALIASES_FILE_DEFAULT)
    parser.add_argument("--branding-file", type=str, default=BRANDING_FILE_DEFAULT)
    parser.add_argument("--disclaimer-file", type=str, default=DISCLAIMER_FILE_DEFAULT)

    parser.add_argument("--docx", dest="docx", action="store_true", default=True,
                        help="Word-Dokument bauen (Modul 2 auf den Top-N). Default: an.")
    parser.add_argument("--no-docx", dest="docx", action="store_false",
                        help="Nur den Markdown-Digest erzeugen, kein Word-Dokument.")
    parser.add_argument("--top", type=int, default=7,
                        help="Anzahl Studien je Subgroup im Word-Dokument.")
    parser.add_argument("--only", default="",
                        help="Word-Dokument auf EINE Subgroup beschraenken (spart Modul-2-Kosten).")
    parser.add_argument("--no-dedup", action="store_true",
                        help="seen_pmids.json ignorieren (alles neu bewerten).")
    parser.add_argument("--rebuild", action="store_true",
                        help="Kein PubMed, kein Modul 1: scored_raw.json neu laden und "
                             "Dokumente aus dem Cache neu bauen (kostenfrei).")
    parser.add_argument("--dry-run", dest="dry_run", action="store_true", default=DRY_RUN_DEFAULT,
                        help="Testmodus: nur Parameter-/Referenzdateien pruefen, kein "
                             f"Netzwerk- oder Gemini-Zugriff. Default: {DRY_RUN_DEFAULT}.")
    parser.add_argument("--live", dest="dry_run", action="store_false",
                        help="Erzwingt einen echten Lauf trotz dry_run=true in der Config.")
    args = parser.parse_args()

    if args.specialty_dir:
        target = Path(args.specialty_dir)
        if not target.is_dir():
            print(f"FEHLER: --specialty-dir '{args.specialty_dir}' existiert nicht.")
            sys.exit(1)
        resolved_target = target.resolve()      # vor dem chdir aufloesen
        os.chdir(resolved_target)
        print(f"Fachgebiets-Verzeichnis gewechselt zu: {resolved_target}")

    set_state_dir(args.state_dir)

    # ---- Parameter laden --------------------------------------------------
    ref_files = load_reference_files(args.refs_dir)
    print(f"Gefundene Referenzen in {args.refs_dir}: {ref_files if ref_files else 'Keine'}")

    key_overrides = load_key_finding_overrides(args.key_findings)
    subgroups = load_subgroups(args.subgroups_file)
    rubric = build_scoring_rubric(subgroups, args.scoring_rubric_file)
    scoring_system = load_scoring_system(args.scoring_system_file)
    summary_system = load_summary_system(args.summary_system_file)
    summary_rubric = load_summary_rubric(args.summary_rubric_file)
    is_noise_title = build_noise_matcher(args.noise_file)
    aliases_raw = load_json_file(args.aliases_file, {}, description="Subgroup-Aliase")
    aliases = {str(k).lower(): v for k, v in (aliases_raw or {}).items()}
    branding = load_branding(args.branding_file)
    disclaimer = load_text_file(args.disclaimer_file, DISCLAIMER_FALLBACK,
                                description="Disclaimer")

    journals_spec = load_list_file(args.journals_spec_file, description="Fachgebiet-Journals")
    journals_gen = load_list_file(args.journals_gen_file, description="Allgemeine Top-Journals")
    tiab_keywords = load_list_file(args.tiab_keywords_file, description="TIAB-Keywords")

    if args.dry_run:
        print("\n=== DRY-RUN: nur Datei-Einlesen, kein PubMed-/Gemini-Aufruf ===")
        print(f"Subgroups        ({len(subgroups)}): {subgroups}")
        print(f"Journals spec    ({len(journals_spec)}): {journals_spec}")
        print(f"Journals gen     ({len(journals_gen)}): {journals_gen}")
        print(f"TIAB Keywords    ({len(tiab_keywords)}): {tiab_keywords}")
        print(f"Referenz-PDFs    ({len(ref_files)}): {ref_files}")
        print(f"Key-Findings     ({len(key_overrides)} Overrides)")
        print(f"Branding         : {branding['brand']} / {branding['field_name']}")
        print(f"Subgroup-Aliase  ({len(aliases)}): {sorted(aliases)}")
        print(f"Schwellen        : MIN_SCORE={MIN_SCORE}, DOC_MIN_SCORE={DOC_MIN_SCORE}, "
              f"MAX_PER_SUBGROUP={MAX_PER_SUBGROUP}, top={args.top}")
        print(f"Modelle          : Triage={LLM_MODEL}, Summary={SUMMARY_MODEL}")
        print(f"\nScoring-System-Prompt:\n{scoring_system}")
        print(f"\nScoring-Rubric:\n{rubric}")
        print(f"\nSummary-System-Prompt:\n{summary_system}")
        print(f"\nPubMed-Query (Vorschau, wird NICHT ausgefuehrt):\n"
              f"{build_query(journals_spec, journals_gen, tiab_keywords)}")
        print("\nDry-Run abgeschlossen.")
        return

    if client is None:
        sys.exit("FEHLER: kein GEMINI_API_KEY gesetzt (config/gemini_clarity.cfg, [GEMINI]).")

    # ---- Ausgabeverzeichnis ----------------------------------------------
    project_name = Path.cwd().name
    digest_title = project_name.replace("_", " ").replace("-", " ").title()
    timestamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = Path(f"output/{project_name}_{timestamp}")
    out_dir.mkdir(parents=True, exist_ok=True)
    digest_file = out_dir / f"{project_name}.md"
    docx_file = out_dir / f"clarity_{project_name}.docx"

    # ---- Modul 1 ----------------------------------------------------------
    seen = set()
    papers = []
    if args.rebuild:
        scored = _load_state(SCORED_FILE, None)
        if scored is None:
            sys.exit(f"--rebuild benoetigt {_sp(SCORED_FILE)}. Bitte erst einmal ohne "
                     f"--rebuild laufen lassen.")
        print(f"Rebuild-Modus: {len(scored)} bewertete Papers geladen (kein Fetch, kein Modul 1).")
    else:
        query = build_query(journals_spec, journals_gen, tiab_keywords)
        print(f"Query:\n{query}\n")
        pmids = esearch_recent(query, reldate=args.days)
        print(f"esearch: {len(pmids)} PMIDs der letzten {args.days} Tage")

        seen = set() if args.no_dedup else load_seen()
        fresh = [p for p in pmids if p not in seen]
        print(f"{len(fresh)} neu nach Dedup")
        if args.limit > 0:
            fresh = fresh[:args.limit]

        papers = efetch_details(fresh)
        print(f"efetch: {len(papers)} Datensaetze")

        before = len(papers)
        papers = [p for p in papers if not is_noise_title(p["title"])]
        if before - len(papers):
            print(f"{before - len(papers)} Errata/Retractions gefiltert")

        filled = enrich_missing_abstracts(papers)
        if filled:
            print(f"{filled} Abstracts nachtraeglich beschafft")

        print(f"\nModul 1 - Scoring von {len(papers)} Papers mit {LLM_MODEL}...")
        scored = []
        for i, p in enumerate(papers, 1):
            p["score"] = score_paper(p, ref_files=ref_files, key_overrides=key_overrides,
                                     rubric=rubric, scoring_system=scoring_system)
            scored.append(p)
            print(f"  [{i}/{len(papers)}] {p['score'].get('relevance_score', '?')}/10  "
                  f"{p['title'][:70]}")
            time.sleep(1)

    # ---- Markdown-Digest + Rohdaten sichern -------------------------------
    digest = build_digest(scored, subgroups, digest_title, ref_files=ref_files)
    digest_file.write_text(digest, encoding="utf-8")
    _save_state(SCORED_FILE, scored)
    print(f"\nGeschrieben: {digest_file} und {_sp(SCORED_FILE)}")

    # ---- Modul 2 + Word-Dokument ------------------------------------------
    if args.docx:
        target_subs = subgroups
        if args.only:
            resolved = resolve_subgroup(args.only, subgroups, aliases)
            if not resolved:
                print(f"\n--only '{args.only}' nicht erkannt. Gueltige Subgroups:")
                for g in subgroups:
                    print(f"   - {g}")
                target_subs = []
            else:
                target_subs = [resolved]
                print(f"\nDokument-Fokus: {resolved} (bis zu {args.top} Studien).")

        if target_subs:
            selected, pending = select_for_document(scored, target_subs, args.top)
            if pending:
                print(f"{len(pending)} relevante(s) Paper ohne Abstract -> Anhang "
                      f"'abstract pending'; Top-{args.top} wurde nachgefuellt")

            n_guide = len([p for p in scored if is_guideline_doc(p)
                           and (p.get("score") or {}).get("subgroup") in target_subs])
            if n_guide:
                print(f"{n_guide} Guideline-/Konsensus-Dokument(e) ohne Studienseite "
                      f"- nur im Anhang genannt")

            cache = load_summ_cache()
            reused = new = 0
            print(f"Modul 2 - {len(selected)} Papers ausgewaehlt...")
            for i, p in enumerate(selected, 1):
                pmid = p["pmid"]
                if pmid in cache:
                    p["summary"] = cache[pmid]
                    reused += 1
                    print(f"  [{i}/{len(selected)}] cached   {p['title'][:52]}")
                else:
                    p["summary"] = summarize_paper(p, summary_system, summary_rubric,
                                                   ref_files=ref_files)
                    cache[pmid] = p["summary"]
                    save_summ_cache(cache)      # inkrementell, absturzsicher
                    new += 1
                    print(f"  [{i}/{len(selected)}] NEU      {p['title'][:52]}")
                    time.sleep(1)
            print(f"Modul 2: {new} neu zusammengefasst, {reused} aus Cache (kostenfrei).")

            # Qualitaetskontrollen
            no_visual, flagged = [], []
            for p in selected:
                kn = (p.get("summary") or {}).get("key_numbers") or {}
                has_chart = _chart_values(kn.get("primary_endpoint"))[0] is not None
                has_table = bool([r for r in (kn.get("secondary") or []) if r.get("name")]) \
                    or bool([f for f in (kn.get("key_findings") or []) if f.get("value")])
                if not (has_chart or has_table):
                    no_visual.append(p["title"][:60])
                hits = advisory_flags(p.get("summary") or {})
                if hits:
                    flagged.append((p["title"][:50], hits[0]))
            print(f"Visuals: {len(selected) - len(no_visual)}/{len(selected)} Studien "
                  f"mit Chart oder Tabelle.")
            for t in no_visual:
                print(f"   ohne Visual: {t}")
            if flagged:
                print("Advisory-Sprache gefunden - bitte vor dem Teilen pruefen:")
                for t, (field, phrase) in flagged:
                    print(f"   {t} [{field}]: '{phrase}'")

            # Papers ohne brauchbare Zusammenfassung nicht publizieren
            def _has_summary(p):
                return bool(str((p.get("summary") or {}).get("summary") or "").strip())

            incomplete = [p for p in selected if not _has_summary(p)]
            if incomplete:
                for p in incomplete:
                    print(f"  ! keine brauchbare Zusammenfassung, verworfen: {p['title'][:60]}")
                selected = [p for p in selected if _has_summary(p)]
                print(f"{len(incomplete)} Paper verworfen; {len(selected)} verbleiben.")

            if selected or pending:
                appendix_src = [p for p in scored
                                if (p.get("score") or {}).get("subgroup") in target_subs]
                build_docx(selected, dt.date.today().isoformat(), args.days, docx_file,
                           subgroups=target_subs, branding=branding, disclaimer=disclaimer,
                           guidelines=appendix_src, pending=pending)
                print(f"Geschrieben: {docx_file}")
            else:
                print("Keine Studie erfuellte die Kriterien - kein Word-Dokument erzeugt.")

    # ---- Zustand fortschreiben --------------------------------------------
    if not args.rebuild and not args.no_dedup:
        attempts = load_attempts()
        retry = 0
        for p in papers:
            pmid = p["pmid"]
            if p.get("abstract"):
                seen.add(pmid)
                attempts.pop(pmid, None)
            else:
                attempts[pmid] = attempts.get(pmid, 0) + 1
                if attempts[pmid] >= MAX_ABSTRACT_ATTEMPTS:
                    seen.add(pmid)
                    attempts.pop(pmid, None)
                else:
                    retry += 1
        save_seen(seen)
        save_attempts(attempts)
        if retry:
            print(f"{retry} Paper weiterhin ohne Abstract - Wiederholung beim naechsten Lauf")

    print(f"\nFertig. Ordner: {out_dir}")


if __name__ == "__main__":
    main()