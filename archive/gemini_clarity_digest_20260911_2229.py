#!/usr/bin/env python3
"""
ClarityDigest - weekly PubMed triage for cardiologists.
"""

import os
import re
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
# Config: Einlesen von config/gemini_car_dig.cfg
# ---------------------------------------------------------------------------

config_file = Path("config/gemini_car_dig.cfg")

if config_file.is_file():
    config = configparser.ConfigParser()
    config.read(config_file)

    if config.has_section("NCBI"):
        if config.get("NCBI", "email", fallback=""):
            os.environ["NCBI_EMAIL"] = config.get("NCBI", "email")
        if config.get("NCBI", "api_key", fallback=""):
            os.environ["NCBI_API_KEY"] = config.get("NCBI", "api_key")
        if config.get("NCBI", "tool", fallback=""):
            os.environ["NCBI_TOOL"] = config.get("NCBI", "tool")
        
    if config.has_section("GEMINI"):
        if config.get("GEMINI", "api_key", fallback=""):
            os.environ["GEMINI_API_KEY"] = config.get("GEMINI", "api_key")
        if config.get("GEMINI", "llm_model", fallback=""):
            os.environ["LLM_MODEL"] = config.get("GEMINI", "llm_model")

# Fallback & API-Key Initialisierung
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

# Aktuell gültiger Standard: gemini-3.7-flash (Stand September 2026).
raw_model = os.environ.get("LLM_MODEL", "gemini-3.7-flash").strip()
raw_model = raw_model.replace("–", "-").replace("—", "-")
if raw_model.startswith("models/"):
    raw_model = raw_model.replace("models/", "")
LLM_MODEL = raw_model if raw_model else "gemini-3.7-flash"

client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

NCBI_EMAIL = os.environ.get("NCBI_EMAIL", "you@example.com")
NCBI_TOOL = os.environ.get("NCBI_TOOL", "ClarityDigest")
NCBI_API_KEY = os.environ.get("NCBI_API_KEY", "")

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
RELDATE_DAYS = 7
MIN_SCORE = 6

SUBGROUPS_FILE_DEFAULT = "params/subgroups.lst"
JOURNALS_SPEC_FILE_DEFAULT = "params/journals_spec.lst"
JOURNALS_GEN_FILE_DEFAULT = "params/journals_gen.lst"
TIAB_KEYWORDS_FILE_DEFAULT = "params/tiab_keywords.lst"
SCORING_SYSTEM_FILE_DEFAULT = "params/score_sys.txt"

# ---------------------------------------------------------------------------
# References Scraper (Verzeichnis ./refs)
# ---------------------------------------------------------------------------

def load_reference_files(refs_dir="refs"):
    """Liest Dateinamen aus dem refs-Verzeichnis zur Verwendung im Prompt."""
    ref_path = Path(refs_dir)
    if not ref_path.exists() or not ref_path.is_dir():
        return []
    
    files = [f.name for f in ref_path.glob("*.pdf")]
    return files

def load_list_file(path, description="Liste"):
    """
    Generischer Zeilen-Loader: liest eine Textdatei ein, eine Eintrag pro Zeile.
    Leere Zeilen und Zeilen, die mit '#' beginnen, werden ignoriert.
    Fehlt die Datei oder ist sie leer/fehlerhaft, wird eine leere Liste zurückgegeben
    (der Aufrufer entscheidet, ob/wie ein Fallback nötig ist).
    """
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

def load_subgroups(path=SUBGROUPS_FILE_DEFAULT):
    """
    Liest die Liste der Fachgebiets-Subgroups aus einer Textdatei (eine Zeile pro Subgroup).
    Damit lässt sich dasselbe Script per --subgroups-file auch für andere Fachgebiete
    (z.B. Dermatologie) verwenden, ohne den Code anzupassen.
    Fehlt die Datei oder ist sie leer, wird als sicherer Fallback ["Other"] zurückgegeben,
    damit das Programm nicht abbricht.
    """
    subgroups = load_list_file(path, description="Subgroups")
    return subgroups if subgroups else ["Other"]

def load_key_finding_overrides(path="key_findings.json"):
    """
    Lädt manuell kuratierte Bottom-Line-Zusammenfassungen für einzelne PMIDs.
    Erwartetes Format (JSON-Objekt): {"<pmid>": "<kuratierter Text>", ...}
    Fehlt die Datei oder ist sie fehlerhaft, wird ein leeres Dict zurückgegeben
    und der reguläre Gemini-Scoring-Ablauf läuft unverändert weiter.
    """
    p = Path(path)
    if not p.is_file():
        return {}
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        print(f"  !! Konnte {path} nicht laden ({type(e).__name__}): {e}")
        return {}

    if not isinstance(data, dict):
        print(f"  !! {path}: erwartetes Format ist ein JSON-Objekt {{pmid: text}}, ignoriere Datei.")
        return {}

    overrides = {str(k): v for k, v in data.items()}
    print(f"Key-Finding-Overrides geladen: {len(overrides)} PMIDs aus {path}")
    return overrides

# ---------------------------------------------------------------------------
# PubMed Queries & E-utilities
# ---------------------------------------------------------------------------

def _or(terms):
    return " OR ".join(terms)

def build_query(journals_spec, journals_gen, tiab_keywords):
    if not journals_spec:
        print("  !! Warnung: keine Fachgebiet-Journals (journals_spec) geladen - Suche eingeschränkt.")
    if not journals_gen or not tiab_keywords:
        print("  !! Warnung: journals_gen oder tiab_keywords leer - allgemeiner Journal-Zweig traegt nicht bei.")
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

def parse_articles(xml_text):
    root = ET.fromstring(xml_text)
    out = []
    for pa in root.findall(".//PubmedArticle"):
        art = pa.find(".//Article")
        if art is None:
            continue
        pmid = _text(pa.find(".//PMID"))
        journal = (_text(art.find(".//Journal/ISOAbbreviation")) or _text(art.find(".//Journal/Title")))
        out.append({
            "pmid": pmid,
            "title": _text(art.find(".//ArticleTitle")),
            "journal": journal,
            "pub_date": _text(art.find(".//JournalIssue/PubDate/Year")),
            "abstract": _abstract(art),
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
# Scoring mit Gemini
# ---------------------------------------------------------------------------

SCORING_SYSTEM_FALLBACK = (
    "You are a senior academic cardiologist triaging brand-new literature. "
    "Reward strong designs (RCTs, guidelines, large trials) and genuine clinical impact. "
    "Evaluate relevance to clinical practice."
)

def load_scoring_system(path=SCORING_SYSTEM_FILE_DEFAULT):
    """
    Liest den System-Prompt fuers Scoring aus einer Textdatei (kompletter Fliesstext,
    keine Zeilenliste). Damit laesst sich die "Fachrolle" des Bewerters (z.B. Kardiologe
    vs. Dermatologe) pro Fachgebiet austauschen, ohne den Code anzufassen.
    Fehlt die Datei oder ist sie leer/fehlerhaft, wird SCORING_SYSTEM_FALLBACK verwendet,
    damit das Programm nicht abbricht.
    """
    p = Path(path)
    if not p.is_file():
        print(f"  !! Konnte {path} nicht finden - verwende eingebauten Fallback-Scoring-Prompt.")
        return SCORING_SYSTEM_FALLBACK
    try:
        text = p.read_text(encoding="utf-8").strip()
    except Exception as e:
        print(f"  !! Konnte {path} nicht laden ({type(e).__name__}): {e} - verwende Fallback-Scoring-Prompt.")
        return SCORING_SYSTEM_FALLBACK
    if not text:
        print(f"  !! {path} ist leer - verwende eingebauten Fallback-Scoring-Prompt.")
        return SCORING_SYSTEM_FALLBACK

    print(f"Scoring-System-Prompt geladen aus {path}")
    return text

def build_scoring_rubric(subgroups):
    return """Return a single JSON object:
{
  "subgroup": one of %s or "Other",
  "relevance_score": integer 1-10,
  "practice_changing": true or false,
  "bottom_line": one or two sentences takeaway
}
""" % json.dumps(subgroups)

def _parse_json(text):
    try:
        return json.loads(text)
    except Exception:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                pass
    return {"subgroup": "Other", "relevance_score": 0, "practice_changing": False, "bottom_line": "(scoring failed)"}

def score_paper(paper, ref_files=None, key_overrides=None, rubric=None, scoring_system=None):
    pmid = str(paper.get("pmid", ""))
    if key_overrides and pmid in key_overrides:
        return {
            "subgroup": "Other",
            "relevance_score": 10,
            "practice_changing": True,
            "bottom_line": key_overrides[pmid],
        }

    if rubric is None:
        # Fallback, falls score_paper ohne explizites rubric aufgerufen wird
        rubric = build_scoring_rubric(load_subgroups())
    if scoring_system is None:
        # Fallback, falls score_paper ohne explizites scoring_system aufgerufen wird
        scoring_system = load_scoring_system()

    ref_context = f"\nLocal Reference Guidelines available: {', '.join(ref_files)}" if ref_files else ""
    user = (
        f"Title: {paper['title']}\n"
        f"Journal: {paper['journal']} ({paper['pub_date']})\n"
        f"Abstract:\n{paper['abstract'] or '(no abstract available)'}\n"
        f"{ref_context}\n\n"
        f"{rubric}"
    )
    
    if client is None:
        print("  !! Kein GEMINI_API_KEY gesetzt - Scoring uebersprungen.")
        return _parse_json("{}")

    # Fallback-Liste für aktuell unterstützte Modellnamen
    # (gemini-1.5-* ist vollständig abgeschaltet und liefert immer 404 - nicht mehr verwenden)
    models_to_try = [LLM_MODEL, "gemini-2.5-flash", "gemini-2.5-flash-lite"]
    # Duplikate entfernen, Reihenfolge beibehalten
    seen = set()
    models_to_try = [m for m in models_to_try if not (m in seen or seen.add(m))]

    for model_name in models_to_try:
        try:
            response = client.models.generate_content(
                model=model_name,
                contents=user,
                config=genai_types.GenerateContentConfig(
                    system_instruction=scoring_system,
                    response_mime_type="application/json",
                ),
            )
            return _parse_json(response.text)
        except Exception as e:
            if "404" in str(e) and model_name != models_to_try[-1]:
                continue
            print(f"  !! Module 1 Scoring API call FAILED ({type(e).__name__}): {e}")
            break

    return _parse_json("{}")

# ---------------------------------------------------------------------------
# Main Execution
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="ClarityDigest - PubMed Triage")
    parser.add_argument("--days", type=int, default=7, help="Lookback in days")
    parser.add_argument("--limit", type=int, default=0, help="Limit papers to score")
    parser.add_argument("--key-findings", type=str, default="key_findings.json",
                         help="Pfad zur JSON-Datei mit manuellen Bottom-Line-Overrides je PMID")
    parser.add_argument("--subgroups-file", type=str, default=SUBGROUPS_FILE_DEFAULT,
                         help="Pfad zur Textdatei mit den Subgroups (eine pro Zeile). "
                              "Erlaubt die Wiederverwendung des Scripts fuer andere Fachgebiete.")
    parser.add_argument("--journals-spec-file", type=str, default=JOURNALS_SPEC_FILE_DEFAULT,
                         help="Pfad zur Textdatei mit den Fachgebiet-Journals (eine pro Zeile).")
    parser.add_argument("--journals-gen-file", type=str, default=JOURNALS_GEN_FILE_DEFAULT,
                         help="Pfad zur Textdatei mit den allgemeinmedizinischen Top-Journals (eine pro Zeile).")
    parser.add_argument("--tiab-keywords-file", type=str, default=TIAB_KEYWORDS_FILE_DEFAULT,
                         help="Pfad zur Textdatei mit den Titel/Abstract-Suchbegriffen (eine pro Zeile).")
    parser.add_argument("--scoring-system-file", type=str, default=SCORING_SYSTEM_FILE_DEFAULT,
                         help="Pfad zur Textdatei mit dem Scoring-System-Prompt (Fliesstext, "
                              "definiert die Bewerter-Rolle je Fachgebiet).")
    parser.add_argument("--dry-run", action="store_true",
                         help="Testmodus: laedt und prueft nur alle Parameter-/Referenzdateien "
                              "(Subgroups, Journals, Keywords, Scoring-Prompt, Refs, Key-Findings) "
                              "und beendet sich danach sofort. Es wird weder PubMed abgefragt noch "
                              "Gemini aufgerufen - kein KI-Tokenverbrauch, kein Netzwerkzugriff auf "
                              "E-Utilities/Gemini.")
    args = parser.parse_args()

    # Referenz-Dateien aus /refs scannen
    ref_files = load_reference_files("refs")
    print(f"Gefundene Referenzen in /refs: {ref_files if ref_files else 'Keine'}")

    # Manuelle Kurzfassungen (Key Findings) für bekannte PMIDs laden
    key_overrides = load_key_finding_overrides(args.key_findings)

    # Subgroups aus Datei laden und Scoring-Rubric daraus bauen
    subgroups = load_subgroups(args.subgroups_file)
    rubric = build_scoring_rubric(subgroups)

    # Scoring-System-Prompt (Bewerter-Rolle) laden
    scoring_system = load_scoring_system(args.scoring_system_file)

    # Fachgebiet-spezifische und allgemeine Journal-/Keyword-Listen laden
    journals_spec = load_list_file(args.journals_spec_file, description="Fachgebiet-Journals")
    journals_gen = load_list_file(args.journals_gen_file, description="Allgemeine Top-Journals")
    tiab_keywords = load_list_file(args.tiab_keywords_file, description="TIAB-Keywords")

    if args.dry_run:
        print("\n=== DRY-RUN: Nur Datei-Einlesen wird geprueft, kein PubMed-/Gemini-Aufruf ===")
        print(f"Subgroups        ({len(subgroups)}): {subgroups}")
        print(f"Journals spec    ({len(journals_spec)}): {journals_spec}")
        print(f"Journals gen     ({len(journals_gen)}): {journals_gen}")
        print(f"TIAB Keywords    ({len(tiab_keywords)}): {tiab_keywords}")
        print(f"Referenz-PDFs    ({len(ref_files)}): {ref_files}")
        print(f"Key-Findings     ({len(key_overrides)} Overrides geladen)")
        print(f"\nScoring-System-Prompt:\n{scoring_system}")
        preview_query = build_query(journals_spec, journals_gen, tiab_keywords)
        print(f"\nPubMed-Query (Vorschau, wird NICHT ausgefuehrt):\n{preview_query}")
        print("\nDry-Run abgeschlossen. Kein Netzwerkzugriff auf PubMed oder Gemini erfolgt.")
        return

    # Digest-Dateiname und -Titel aus dem Verzeichnisnamen ableiten (z.B. "clarity_digest" ->
    # Datei "clarity_digest.md", Titel "Clarity Digest"). So passt sich der Name automatisch
    # an, sobald das Projektverzeichnis umbenannt wird - ohne Codeaenderung.
    project_name = Path.cwd().name
    digest_basename = f"{project_name}.md"
    digest_title = project_name.replace("_", " ").replace("-", " ").title()

    # Datumsbasierter Ordner für Multi-File Output erstellen
    timestamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = Path(f"output/digest_{timestamp}")
    out_dir.mkdir(parents=True, exist_ok=True)
    digest_file = out_dir / digest_basename

    print(f"Fetching PubMed papers from last {args.days} days...")
    query = build_query(journals_spec, journals_gen, tiab_keywords)
    pmids = esearch_recent(query, reldate=args.days)
    print(f"Found {len(pmids)} PMIDs.")

    if args.limit > 0:
        pmids = pmids[:args.limit]

    papers = efetch_details(pmids)

    print(f"Scoring {len(papers)} papers using Gemini ({LLM_MODEL})...")
    scored = []
    for p in papers:
        res = score_paper(p, ref_files=ref_files, key_overrides=key_overrides, rubric=rubric, scoring_system=scoring_system)
        p.update(res)
        if p.get("relevance_score", 0) >= MIN_SCORE:
            scored.append(p)
        time.sleep(1)  # <--- HIER 1 Sekunde Pause einfügen! 
        
    print(f"Writing digest to {digest_file} ({len(scored)} papers passed score threshold)...")
    with open(digest_file, "w", encoding="utf-8") as f:
        f.write(f"# {digest_title} - {dt.date.today().isoformat()}\n")
        f.write(f"_Verwendete Referenzleitlinien: {', '.join(ref_files) if ref_files else 'Keine'}_\n\n")
        for p in scored:
            f.write(f"### {p['title']}\n")
            f.write(f"- **Journal**: {p['journal']} ({p['pub_date']})\n")
            f.write(f"- **Score**: {p.get('relevance_score')}/10 | **Subgroup**: {p.get('subgroup')}\n")
            f.write(f"- **Bottom Line**: {p.get('bottom_line')}\n")
            f.write(f"- **URL**: {p['url']}\n\n")

    # Kopie im Hauptverzeichnis aktualisieren für schnellen Zugriff (aktuell deaktiviert -
    # wuerde eine zweite, inhaltsgleiche Datei im Hauptverzeichnis anlegen; Dateiname passt
    # sich ebenfalls an digest_basename an, falls wieder aktiviert)
    # with open(digest_basename, "w", encoding="utf-8") as f:
    #    f.write(digest_file.read_text(encoding="utf-8"))

    print(f"Done! Ordner erstellt: {out_dir}")

if __name__ == "__main__":
    main()