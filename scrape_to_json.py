"""
scrape_to_json.py
─────────────────
Scrapa il portale EU Funding & Tenders con Playwright e produce calls.json
direttamente, senza passare per Excel.
Incorpora tutta la logica di classificazione di make_calls_json.py.

Uso:
    python scrape_to_json.py              # scrive calls.json nella cartella corrente
    python scrape_to_json.py --out /path  # percorso custom
"""

import re
import math
import time
import json
import argparse
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright
import playwright_stealth

# ── Parametri ─────────────────────────────────────────────────────────────────

PAGE_SIZE = 50

LIST_URL = (
    "https://ec.europa.eu/info/funding-tenders/opportunities/portal/screen"
    "/opportunities/calls-for-proposals"
    "?order=DESC&pageNumber={page}&pageSize={ps}&sortBy=startDate"
    "&isExactMatch=true&status=31094501,31094502&programmePeriod=2021%20-%202027"
)

SEARCH_API  = "apiKey=SEDIA"
COOKIE_TEXT = "This site uses cookies"

LINK_SELECTOR = (
    'a[href*="/topic-details/"], '
    'a[href*="/competitive-calls-cs/"], '
    'a[href*="/prospect-details/"]'
)

# URL base per i link delle call, costruiti dal campo 'reference' dell'API
TOPIC_BASE_URL = "https://ec.europa.eu/info/funding-tenders/opportunities/portal/screen/opportunities/topic-details/"
COMPETITIVE_BASE_URL = "https://ec.europa.eu/info/funding-tenders/opportunities/portal/screen/opportunities/competitive-calls-cs/"
PROSPECT_BASE_URL = "https://ec.europa.eu/info/funding-tenders/opportunities/portal/screen/opportunities/prospect-details/"

RE_TOTAL = re.compile(r"(\d[\d,\.]*)\s*(?:results?|items?|found)", re.IGNORECASE)
RE_OPEN      = re.compile(r"Opening date:\s*([^\|\n\r]+)",          re.IGNORECASE)
RE_DEAD      = re.compile(r"Deadline date:\s*([^\|\n\r]+)",         re.IGNORECASE)
RE_NEXT_DEAD = re.compile(r"Next deadline:\s*([^\|\n\r]+)",         re.IGNORECASE)
RE_PROG      = re.compile(r"Programme:\s*([^\|\n\r]+)",             re.IGNORECASE)
RE_ACTION    = re.compile(r"Type of action:\s*([^\|\n\r]+)",        re.IGNORECASE)
RE_CLUSTER   = re.compile(r"HORIZON-CL([1-6])",                     re.IGNORECASE)
RE_CALL_ID   = re.compile(r"callIdentifier[=:\s]+([^\s&\|\n\r]+)",  re.IGNORECASE)

# Budget patterns — ordered from most to least reliable
RE_BUDGET_LABEL = re.compile(
    r"(?:total\s+)?budget[:\s]+(?:of\s+)?(?:EUR|€|euro)?\s*([\d][0-9 .,]+)",
    re.IGNORECASE,
)
RE_BUDGET_SUFFIX = re.compile(
    r"([\d][0-9 .,]+)\s*(?:EUR|€|euro)",
    re.IGNORECASE,
)
RE_BUDGET_INDICATIVE = re.compile(
    r"indicative\s+(?:total\s+)?budget[:\s]+(?:EUR|€|euro)?\s*([\d][0-9 .,]+)",
    re.IGNORECASE,
)
RE_BUDGET_EXPECTED = re.compile(
    r"(?:total\s+)?(?:estimated|expected|available|allocated)\s+budget[:\s]+(?:EUR|€|euro)?\s*([\d][0-9 .,]+)",
    re.IGNORECASE,
)

MONTHS = {
    "january":1,"february":2,"march":3,"april":4,"may":5,"june":6,
    "july":7,"august":8,"september":9,"october":10,"november":11,"december":12,
}

# ── Tabelle di classificazione ────────────────────────────────────────────────

PROGRAMME_MAP = {
    "43108390":"Horizon Europe","43108391":"Horizon Europe",
    "43152860":"Digital Europe Programme","111111":"EU External Action-Prospect",
    "44181033":"European Defence Fund","43353764":"Erasmus+",
    "43251589":"CERV","43251814":"Creative Europe (CREA)",
    "43252476":"Single Market Programme (SMP)","43298664":"AGRIP",
    "43251842":"EUAF","43298916":"Euratom",
    "43089234":"Innovation Fund (INNOVFUND)","43637601":"PPPA",
    "44416173":"I3","45532249":"EUBA",
    "43252368":"Internal Security Fund (ISF)","43252449":"RFCS",
    "43298203":"UCPM","43254037":"European Solidarity Corps (ESC)",
    "44773066":"Just Transition Mechanism (JTM)",
    "43251567":"Connecting Europe Facility (CEF)",
    "43252386":"JUST","43252433":"Pericles IV","43252517":"SOCPL",
    "43253967":"RENEWFM","43254019":"European Social Fund+ (ESF+)",
    "43392145":"EMFAF",
}

# ── Horizon Europe: mappatura strutturale per cluster CL1-6 ───────────────────
# Fonte: struttura ufficiale Horizon Europe Work Programme
HE_CLUSTER_MAP = {
    "1": "Health & Life Sciences",
    "2": "Culture, Creativity & Inclusion",
    "3": "Security & Resilience",
    "4": "Digital, Industry & Space",
    "5": "Climate, Energy & Mobility",
    "6": "Food, Bioeconomy & Environment",
}

# Horizon Europe: mappatura per sottoprogrammi NON-CL (prefisso del topic ID)
# Ordine: dal più specifico al più generico.
# (prefisso_nell_url_o_callid, cluster_num, cluster_label, thematic_area)
HE_SUBPROGRAMME_MAP = [
    # ── Missions ──────────────────────────────────────────────────────────────
    ("MISS-CIT",    "M-CIT",  "Climate-neutral & Smart Cities",                "Climate-neutral & Smart Cities"),
    ("MISS-OCEAN",  "M-OCEAN","Healthy Oceans, Seas, Coastal & Inland Waters", "Healthy Oceans, Seas, Coastal & Inland Waters"),
    ("MISS-CLIMA",  "5",      "Climate, Energy and Mobility",                  "Climate, Energy & Mobility"),
    ("MISS-CANCER", "1",      "Health",                                        "Health & Life Sciences"),
    ("MISS-SOIL",   "6",      "Food, Bioeconomy, Natural Resources, Agriculture and Environment","Food, Bioeconomy & Environment"),
    ("MISS-CROSS",  "",       "",                                              "Climate, Energy & Mobility"),   # Adaptation Mission cross-cutting → CL5
    ("MISS",        "",       "",                                              "Climate, Energy & Mobility"),   # generic MISS fallback

    # ── Health cluster (explicit prefix) ──────────────────────────────────────
    ("HLTH",        "1",      "Health",                                        "Health & Life Sciences"),

    # ── EIC / EIT / EIE ───────────────────────────────────────────────────────
    ("EITUM-BP",    "M-CIT",  "Climate-neutral & Smart Cities",               "Climate-neutral & Smart Cities"),
    ("EITUM",       "M-CIT",  "Climate-neutral & Smart Cities",               "Climate-neutral & Smart Cities"),
    ("EIC",         "",       "European Innovation Council",                  "SME, Entrepreneurship & Market Uptake"),
    ("EIE",         "",       "European Innovation Ecosystems",               "SME, Entrepreneurship & Market Uptake"),
    ("EIT",         "",       "European Institute of Innovation & Technology","SME, Entrepreneurship & Market Uptake"),

    # ── JU – Joint Undertakings ───────────────────────────────────────────────
    ("JU-CLEAN-AVIATION", "", "Clean Aviation",                               "Clean Aviation"),
    ("JU-CBIO",     "6",      "Food, Bioeconomy, Natural Resources, Agriculture and Environment","Food, Bioeconomy & Environment"),
    ("JU-GH",       "1",      "Health",                                       "Health & Life Sciences"),
    ("JU-IHI",      "1",      "Health",                                       "Health & Life Sciences"),
    ("JU-EDCTP",    "1",      "Health",                                       "Health & Life Sciences"),
    ("JU-H2",       "5",      "Climate, Energy and Mobility",                 "Climate, Energy & Mobility"),
    ("JU-SESAR",    "5",      "Climate, Energy and Mobility",                 "Climate, Energy & Mobility"),
    ("JU-S2R",      "5",      "Climate, Energy and Mobility",                 "Climate, Energy & Mobility"),
    ("JU-ECS",      "4",      "Digital, Industry and Space",                  "Digital, Industry & Space"),
    ("JU-KDT",      "4",      "Digital, Industry and Space",                  "Digital, Industry & Space"),
    ("JU-SNS",      "4",      "Digital, Industry and Space",                  "Digital, Industry & Space"),
    ("JU-CHIPS",    "4",      "Digital, Industry and Space",                  "Digital, Industry & Space"),
    ("JU-",         "5",      "Climate, Energy and Mobility",                 "Climate, Energy & Mobility"),   # generic JU fallback → CL5 (most JUs are clean energy/transport)

    # ── ERC ───────────────────────────────────────────────────────────────────
    # ERC is frontier research — classified by scientific domain from title/text;
    # without domain info we use "Internships, fellowships & scholarships"
    ("ERC",         "",       "European Research Council",                    "Internships, fellowships & scholarships"),

    # ── MSCA ──────────────────────────────────────────────────────────────────
    ("MSCA",        "",       "Marie Skłodowska-Curie Actions",               "Internships, fellowships & scholarships"),

    # ── Research Infrastructures ──────────────────────────────────────────────
    ("CL3-INFRA",   "3",      "Civil Security for Society",                   "Security & Resilience"),
    ("INFRA-TECH",  "4",      "Digital, Industry and Space",                  "Digital, Industry & Space"),
    ("INFRA-SERV",  "4",      "Digital, Industry and Space",                  "Digital, Industry & Space"),
    ("INFRA-EOSC",  "4",      "Digital, Industry and Space",                  "Digital, Industry & Space"),   # EOSC is digital infrastructure
    ("INFRA-DEV",   "4",      "Digital, Industry and Space",                  "Digital, Industry & Space"),
    ("INFRA",       "4",      "Research Infrastructures",                     "Digital, Industry & Space"),   # generic INFRA → CL4 (INFRA is mainly digital/data)

    # ── Euratom / Nuclear ─────────────────────────────────────────────────────
    ("EURATOM",     "5",      "Climate, Energy and Mobility",                 "Climate, Energy & Mobility"),
    ("CID",         "5",      "Climate, Energy and Mobility",                 "Climate, Energy & Mobility"),

    # ── EUROHPC ───────────────────────────────────────────────────────────────
    ("EUROHPC",     "4",      "Digital, Industry and Space",                  "Digital, Industry & Space"),

    # ── Widening / NEB ────────────────────────────────────────────────────────
    ("WIDERA",      "",       "Widening Participation & ERA",                 "Cross-cutting / Other"),   # genuine cross-cutting support programme
    ("NEB",         "M-CIT",  "New European Bauhaus",                         "Climate-neutral & Smart Cities"),

    # ── RAISE ─────────────────────────────────────────────────────────────────
    ("RAISE",       "4",      "Digital, Industry and Space",                  "Digital, Industry & Space"),
]

# ── Non-Horizon: mappatura per programma (stringa nome o ID numerico) ─────────
# Ordine: dal più specifico al più generico.
# (sottostringa_nel_nome_programma, thematic_area)
PROGRAMME_THEMATIC_MAP = [
    # Defence
    ("European Defence Fund",           "Defence"),
    ("EDF",                             "Defence"),
    # External Action
    ("EU External Action",              "External Action & International Cooperation"),
    ("EU External Action-Prospect",     "External Action & International Cooperation"),
    ("EUBA",                            "External Action & International Cooperation"),
    # Digital
    ("Digital Europe",                  "Digital, Industry & Space"),
    ("EUAF",                            "Digital, Industry & Space"),
    ("PPPA",                            "Digital, Industry & Space"),   # PPPA default → digital (CHIPS)
    # Climate / Energy / Transport
    ("Just Transition",                 "Climate, Energy & Mobility"),
    ("Innovation Fund",                 "Climate, Energy & Mobility"),
    ("Euratom",                         "Climate, Energy & Mobility"),
    ("Connecting Europe",               "Climate, Energy & Mobility"),
    ("RENEWFM",                         "Climate, Energy & Mobility"),
    ("RFCS",                            "Climate, Energy & Mobility"),
    # Food / Environment
    ("EMFAF",                           "Food, Bioeconomy & Environment"),
    ("LIFE",                            "Food, Bioeconomy & Environment"),
    ("AGRIP",                           "Food, Bioeconomy & Environment"),
    # Security
    ("Internal Security Fund",          "Security & Resilience"),
    ("ISF",                             "Security & Resilience"),
    ("UCPM",                            "Security & Resilience"),   # Union Civil Protection Mechanism
    # Culture / Social
    ("CERV",                            "Culture, Creativity & Inclusion"),
    ("Creative Europe",                 "Culture, Creativity & Inclusion"),
    ("Erasmus+",                        "Culture, Creativity & Inclusion"),
    ("European Social Fund+",           "Culture, Creativity & Inclusion"),
    ("European Solidarity Corps",       "Culture, Creativity & Inclusion"),
    ("SOCPL",                           "Culture, Creativity & Inclusion"),
    ("JUST",                            "Culture, Creativity & Inclusion"),
    ("Pericles IV",                     "Culture, Creativity & Inclusion"),
    # SME / Market
    ("Single Market Programme",         "SME, Entrepreneurship & Market Uptake"),
    ("I3",                              "SME, Entrepreneurship & Market Uptake"),
    # Horizon Europe last (uses HE-specific logic, not this map)
    ("Horizon Europe",                  None),   # handled by HE logic; None = skip
]

# URL-level overrides for non-HE programmes that embed the programme code in the URL path
# e.g. /opportunities/topic-details/AGRIP-2026-01  → use prefix matching
# (prefix_in_topic_id, thematic)
NON_HE_URL_PREFIX_MAP = [
    ("AGRIP",       "Food, Bioeconomy & Environment"),
    ("EUAF",        "Digital, Industry & Space"),
    ("DIGITAL",     "Digital, Industry & Space"),
    ("UCPM",        "Security & Resilience"),
    ("RFCS",        "Climate, Energy & Mobility"),
    ("EUBA",        "External Action & International Cooperation"),
    ("PPPA-CHIPS",  "Digital, Industry & Space"),
    ("PPPA-MEDIA",  "Culture, Creativity & Inclusion"),
    ("PPPA",        "Digital, Industry & Space"),
    ("RENEWFM",     "Climate, Energy & Mobility"),
    ("SOCPL",       "Culture, Creativity & Inclusion"),
    ("JUST",        "Culture, Creativity & Inclusion"),
    ("I3",          "SME, Entrepreneurship & Market Uptake"),
    ("EMFAF",       "Food, Bioeconomy & Environment"),
    ("EDF",         "Defence"),
    ("ISF",         "Security & Resilience"),
    ("CEF",         "Climate, Energy & Mobility"),
    ("INEA",        "Climate, Energy & Mobility"),
    ("CINEA",       "Climate, Energy & Mobility"),
    ("INNOVFUND",   "Climate, Energy & Mobility"),
    ("LIFE",        "Food, Bioeconomy & Environment"),
    ("ERASMUS",     "Culture, Creativity & Inclusion"),
    ("CERV",        "Culture, Creativity & Inclusion"),
    ("CREA",        "Culture, Creativity & Inclusion"),
    ("ESC",         "Culture, Creativity & Inclusion"),
    ("ESF",         "Culture, Creativity & Inclusion"),
    ("JTM",         "Climate, Energy & Mobility"),
    ("SMP",         "SME, Entrepreneurship & Market Uptake"),
]

# Legacy alias kept for THEMATIC_MAP references elsewhere in the code
THEMATIC_MAP = {
    "1":"Health & Life Sciences","2":"Culture, Creativity & Inclusion",
    "3":"Security & Resilience","4":"Digital, Industry & Space",
    "5":"Climate, Energy & Mobility","6":"Food, Bioeconomy & Environment",
    "M-CIT":"Climate-neutral & Smart Cities",
    "M-OCEAN":"Healthy Oceans, Seas, Coastal & Inland Waters",
}

# (prefix, subcode_or_None, cluster_num, cluster_label, thematic)
# NOTE: This table is now used ONLY as a last-resort fallback.
# Primary logic is in classify_horizon_europe() and classify_non_he_by_url().
URL_RULES = [
    ("MISS","CIT",      "M-CIT", "Climate-neutral & Smart Cities",               "Climate-neutral & Smart Cities"),
    ("MISS","OCEAN",    "M-OCEAN","Healthy Oceans, Seas, Coastal & Inland Waters","Healthy Oceans, Seas, Coastal & Inland Waters"),
    ("MISS","CLIMA",    "5",     "Climate, Energy and Mobility",                  "Climate, Energy & Mobility"),
    ("MISS","CANCER",   "1",     "Health",                                        "Health & Life Sciences"),
    ("MISS","SOIL",     "6",     "Food, Bioeconomy, Natural Resources, Agriculture and Environment","Food, Bioeconomy & Environment"),
    ("MISS","CROSS",    "",      "",                                              "Climate, Energy & Mobility"),
    ("HLTH",     None,  "1",     "Health",                                        "Health & Life Sciences"),
    ("EIC",      None,  "",      "",                                              "SME, Entrepreneurship & Market Uptake"),
    ("EIE",      None,  "",      "",                                              "SME, Entrepreneurship & Market Uptake"),
    ("EITUM-BP", None,  "M-CIT", "Climate-neutral & Smart Cities",               "Climate-neutral & Smart Cities"),
    ("EIT",      None,  "",      "",                                              "SME, Entrepreneurship & Market Uptake"),
    ("CID",      None,  "5",     "Climate, Energy and Mobility",                  "Climate, Energy & Mobility"),
    ("EURATOM",  None,  "5",     "Climate, Energy and Mobility",                  "Climate, Energy & Mobility"),
    ("EUROHPC",  None,  "4",     "Digital, Industry and Space",                   "Digital, Industry & Space"),
    ("JU-CLEAN-AVIATION",None,"","",                                              "Clean Aviation"),
    ("JU-",      None,  "",      "",                                              "Climate, Energy & Mobility"),
    ("MSCA",     None,  "",      "",                                              "Internships, fellowships & scholarships"),
    ("NEB",      None,  "",      "",                                              "Climate-neutral & Smart Cities"),
    ("RAISE",    None,  "4",     "Digital, Industry and Space",                   "Digital, Industry & Space"),
    ("WIDERA",   None,  "",      "",                                              "Cross-cutting / Other"),
    ("CL3","INFRA",     "3",     "Civil Security for Society",                    "Security & Resilience"),
    ("INFRA","TECH",    "4",     "Digital, Industry and Space",                   "Digital, Industry & Space"),
    ("INFRA","SERV",    "4",     "Digital, Industry and Space",                   "Digital, Industry & Space"),
    ("INFRA","DEV",     "4",     "Digital, Industry and Space",                   "Digital, Industry & Space"),
    ("INFRA","EOSC",    "4",     "Digital, Industry and Space",                   "Digital, Industry & Space"),
    ("INFRA",    None,  "4",     "Research Infrastructures",                      "Digital, Industry & Space"),
    ("AGRIP",    None,  "6",     "Food, Bioeconomy, Natural Resources, Agriculture and Environment","Food, Bioeconomy & Environment"),
    ("EUAF",     None,  "4",     "Digital, Industry and Space",                   "Digital, Industry & Space"),
    ("DIGITAL",  None,  "4",     "Digital, Industry and Space",                   "Digital, Industry & Space"),
    ("UCPM",     None,  "3",     "Civil Security for Society",                    "Security & Resilience"),
    ("RFCS",     None,  "5",     "Climate, Energy and Mobility",                  "Climate, Energy & Mobility"),
    ("EUBA",     None,  "",      "",                                              "External Action & International Cooperation"),
    ("PPPA","CHIPS",    "4",     "Digital, Industry and Space",                   "Digital, Industry & Space"),
    ("PPPA","MEDIA",    "",      "",                                              "Culture, Creativity & Inclusion"),
    ("PPPA",     None,  "4",     "Digital, Industry and Space",                   "Digital, Industry & Space"),
    ("RENEWFM",  None,  "5",     "Climate, Energy and Mobility",                  "Climate, Energy & Mobility"),
    ("SOCPL",    None,  "",      "",                                              "Culture, Creativity & Inclusion"),
    ("ERC",      None,  "",      "",                                              "Internships, fellowships & scholarships"),
    ("EMFAF",    None,  "6",     "Food, Bioeconomy, Natural Resources, Agriculture and Environment","Food, Bioeconomy & Environment"),
    ("JUST",     None,  "",      "",                                              "Culture, Creativity & Inclusion"),
    ("I3",       None,  "",      "",                                              "SME, Entrepreneurship & Market Uptake"),
]

NUMERIC_ID_NAME_RULES = [
    ("OHAMR",       "Health & Life Sciences"),
    ("ERA4HEALTH",  "Health & Life Sciences"),
    ("ERA4 HEALTH", "Health & Life Sciences"),
    ("BRAINHEALTH", "Health & Life Sciences"),
    ("EP BRAINHEALTH","Health & Life Sciences"),
    ("ERDERA",      "Health & Life Sciences"),
    ("BE READY",    "Health & Life Sciences"),
    ("OVERWEIGHT",  "Health & Life Sciences"),
    ("OBESITY",     "Health & Life Sciences"),
    ("CARDIOVASC",  "Health & Life Sciences"),
    ("CLINICAL TRIAL","Health & Life Sciences"),
    ("NEUROSCI",    "Health & Life Sciences"),
    ("RARE DISEASE","Health & Life Sciences"),
    ("EITUM",       "Climate-neutral & Smart Cities"),
    ("URBAN MOBILITY","Climate-neutral & Smart Cities"),
    ("DRIVING URBAN","Climate-neutral & Smart Cities"),
    ("EIC AWARDEE", "SME, Entrepreneurship & Market Uptake"),
    ("INNOMATCH",   "SME, Entrepreneurship & Market Uptake"),
    ("STARTUP",     "SME, Entrepreneurship & Market Uptake"),
    ("FOOD SUSTAINABILITY","Food, Bioeconomy & Environment"),
    ("MARINE BIODIVERSITY","Food, Bioeconomy & Environment"),
    ("BLUEACTION",  "Food, Bioeconomy & Environment"),
    ("TASC-RESTOREMED","Food, Bioeconomy & Environment"),
    ("RESTORE",     "Food, Bioeconomy & Environment"),
    ("FERMENTED",   "Food, Bioeconomy & Environment"),
]

URL_BENEFICIARY_OVERRIDE = {
    "MSCA":  ["Research organisation"],
    "INFRA": ["Research organisation"],
    "EUBA":  ["Public body"],
}

SPECIAL_BASIC_RESEARCH_CATEGORY = "Internships, fellowships & scholarships"
SPECIAL_TITLE_KEYWORDS = ["internship","internships","fellowship","fellowships","msca","scholarship","scholarships"]
TOPIC_KEYWORDS = {
    "Health & Life Sciences": ["health","biotech","biotechnology","pharma","pharmaceutical","therapeutic","medical","diagnostic","genomic","genomics","public health","clinical"],
    "Culture, Creativity & Inclusion": ["culture","creative","heritage","museum","archive","inclusion","social inclusion","democracy","education","skills"],
    "Security & Resilience": ["security","cybersecurity","cyber security","disaster resilience","emergency","critical infrastructure","civil protection","border security"],
    "Digital, Industry & Space": ["digital","artificial intelligence","machine learning","generative ai","data space","data sharing","cloud","edge","software","semiconductor","microelectronics","quantum","robotics","space","satellite"],
    "Climate, Energy & Mobility": ["climate","adaptation","mitigation","energy","electricity","power system","grid","hydrogen","battery","batteries","mobility","transport","renewable","solar","photovoltaic","wind","storage","smart grid","building renovation","built environment","city","cities"],
    "Food, Bioeconomy & Environment": ["agriculture","farming","crop","food system","bioeconomy","biodiversity","forestry","soil","water resources","environment","ecosystem","marine"],
    "Defence": ["defence","defense","dual-use","dual use","military"],
    "SME, Entrepreneurship & Market Uptake": ["sme","startup","entrepreneurship","venture","scale-up","market uptake","innovation uptake"],
    "External Action & International Cooperation": ["international cooperation","development cooperation","global south","partner countries","external action"],
    "Climate-neutral & Smart Cities": ["smart city","smart cities","climate-neutral city","urban transition","city mission"],
    "Healthy Oceans, Seas, Coastal & Inland Waters": ["ocean","oceans","sea","seas","coastal","inland waters","marine","blue economy"],
    "Clean Aviation": ["aviation","aircraft","aeronautics","sustainable aviation"],
    "Cross-cutting / Other": ["interdisciplinary","cross-cutting","widening","research infrastructure","eosc"],
}

# ── Helpers di classificazione ────────────────────────────────────────────────

def escape_rx(s: str) -> str:
    return re.escape(s or "")

def text_has_keyword(text: str, keyword: str) -> bool:
    return bool(re.search(rf"(?<![A-Za-z]){escape_rx(keyword.lower())}(?![A-Za-z])", (text or "").lower()))

def keyword_hits_for_thematic(text: str, thematic: str):
    hits = []
    for kw in TOPIC_KEYWORDS.get(thematic, []):
        if text_has_keyword(text, kw):
            hits.append(kw)
    return list(dict.fromkeys(hits))

def title_is_special_basic_research(title: str) -> bool:
    tl = (title or "").lower()
    return any(text_has_keyword(tl, kw) for kw in SPECIAL_TITLE_KEYWORDS)

def classify_multitopic(name: str, full_text: str, thematic: str):
    text = re.sub(r"\s+", " ", (full_text or "")).strip().lower()
    keyword_hits = {}
    multi_thematic = []
    for area in TOPIC_KEYWORDS:
        hits = keyword_hits_for_thematic(text, area)
        if hits:
            keyword_hits[area] = hits
            multi_thematic.append(area)

    special = title_is_special_basic_research(name)
    if special:
        keyword_hits[SPECIAL_BASIC_RESEARCH_CATEGORY] = [kw for kw in SPECIAL_TITLE_KEYWORDS if text_has_keyword((name or "").lower(), kw)]
        if SPECIAL_BASIC_RESEARCH_CATEGORY not in multi_thematic:
            multi_thematic.append(SPECIAL_BASIC_RESEARCH_CATEGORY)

    return {
        "full_text": text,
        "keyword_hits": keyword_hits,
        "multi_thematic": multi_thematic,
        "is_special_basic_research": special,
    }

# ── Classificazione strutturale ───────────────────────────────────────────────

def _topic_id(url: str) -> str:
    """Estrae il topic-ID dall'URL (tutto dopo /topic-details/ o /competitive-calls-cs/)."""
    s = (url or "").upper().split("?")[0]
    for m in ["/TOPIC-DETAILS/", "/COMPETITIVE-CALLS-CS/", "/PROSPECT-DETAILS/"]:
        i = s.find(m)
        if i >= 0:
            return s[i + len(m):]
    return s


def _is_horizon_europe(prog: str, url: str, call_id: str) -> bool:
    """True se la call appartiene a Horizon Europe (inclusi JU, ERC, MSCA, ecc.)."""
    prog_l = (prog or "").lower()
    if "horizon" in prog_l:
        return True
    tid = _topic_id(url)
    he_prefixes = (
        "HORIZON-", "HLTH-", "CL1-", "CL2-", "CL3-", "CL4-", "CL5-", "CL6-",
        "ERC-", "MSCA-", "EIC-", "EIT-", "EIE-", "EITUM-", "EUROHPC-",
        "MISS-", "NEB-", "WIDERA-", "RAISE-", "INFRA-", "CID-", "EURATOM-",
        "JU-",
    )
    for p in he_prefixes:
        if tid.startswith(p) or (call_id or "").upper().startswith(p):
            return True
    return False


def classify_horizon_europe(url: str, call_id: str) -> tuple:
    """
    Classificazione strutturale per Horizon Europe.

    Logica a cascata:
      1. Cluster CL1-6 esplicito nel call_id o URL  → usa HE_CLUSTER_MAP
      2. Sottoprogramma noto (HE_SUBPROGRAMME_MAP)  → usa quella entry
      3. Fallback URL_RULES legacy                   → usa quella entry
      4. Non classificato                            → ("", "", "")

    Ritorna (cluster_num, cluster_label, thematic)
    """
    tid = _topic_id(url)
    cid_up = (call_id or "").upper()

    # ── 1. CL1-6 strutturale: cerca HORIZON-CL<N> o CL<N>- nel call_id/URL ──
    m = RE_CLUSTER.search(cid_up) or RE_CLUSTER.search(tid)
    if m:
        cnum = m.group(1)
        thematic = HE_CLUSTER_MAP.get(cnum, "")
        # Affina il cluster_label dal nome completo THEMATIC_MAP
        clabel_map = {
            "1":"Health","2":"Culture, Creativity and Inclusion",
            "3":"Civil Security for Society","4":"Digital, Industry and Space",
            "5":"Climate, Energy and Mobility",
            "6":"Food, Bioeconomy, Natural Resources, Agriculture and Environment",
        }
        return cnum, clabel_map.get(cnum, ""), thematic

    # ── 2. Sottoprogramma HE_SUBPROGRAMME_MAP ──────────────────────────────────
    # Ordine importante: voci più specifiche (MISS-CIT, MISS-OCEAN, …) stanno
    # prima di quelle generiche (MISS) nella tabella.
    # Problema: per le MISS il formato reale è HORIZON-MISS-{YEAR}-{SUBCODE}-…
    # quindi "MISS-CIT" non appare mai come sottostringa continua.
    # Soluzione: normalizziamo il tid rimuovendo segmenti puramente numerici
    # (anni come 2026, 2027) prima del matching.
    import re as _re
    tid_norm = _re.sub(r"-20\d\d(?=-)", "", tid)          # HORIZON-MISS-2026-CIT → HORIZON-MISS-CIT
    cid_norm = _re.sub(r"-20\d\d(?=-)", "", cid_up)
    for prefix, cnum, clabel, thematic in HE_SUBPROGRAMME_MAP:
        # a) Corrispondenza diretta sul tid originale
        if tid.startswith(prefix) or cid_up.startswith(prefix):
            return cnum, clabel, thematic
        # b) Corrispondenza con HORIZON- davanti
        if tid.startswith("HORIZON-" + prefix) or cid_up.startswith("HORIZON-" + prefix):
            return cnum, clabel, thematic
        # c) Corrispondenza sul tid normalizzato (anno rimosso)
        if tid_norm.startswith(prefix) or cid_norm.startswith(prefix):
            return cnum, clabel, thematic
        if tid_norm.startswith("HORIZON-" + prefix) or cid_norm.startswith("HORIZON-" + prefix):
            return cnum, clabel, thematic
        # d) Prefisso presente come segmento interno (es. -MSCA- dentro HORIZON-MSCA-2026-)
        if ("-" + prefix + "-") in tid or ("-" + prefix + "-") in cid_up:
            return cnum, clabel, thematic

    # ── 3. Fallback: URL_RULES legacy ─────────────────────────────────────────
    for prefix, subcode, c_num, c_label, thematic in URL_RULES:
        if prefix not in tid:
            continue
        if subcode is not None and subcode not in tid:
            continue
        return c_num, c_label, thematic

    return "", "", ""


def classify_non_he_by_programme(prog: str) -> str:
    """
    Per programmi non-HE: ricava il thematic_cluster dal nome del programma
    usando PROGRAMME_THEMATIC_MAP (ordinato dal più specifico al più generico).
    """
    pl = (prog or "").lower()
    for key, label in PROGRAMME_THEMATIC_MAP:
        if label is None:
            continue
        if key.lower() in pl:
            return label
    return ""


def classify_non_he_by_url(url: str) -> str:
    """
    Per programmi non-HE: ricava il thematic_cluster dal prefisso del topic ID
    nell'URL, usando NON_HE_URL_PREFIX_MAP.
    """
    tid = _topic_id(url)
    for prefix, thematic in NON_HE_URL_PREFIX_MAP:
        if tid.startswith(prefix):
            return thematic
    return ""


def url_classify(url: str):
    """
    Wrapper legacy: usa la nuova logica strutturale per Horizon Europe,
    e la nuova logica per non-HE; cade sul vecchio URL_RULES come safety-net.
    Ritorna (cluster_num, cluster_label, thematic, beneficiary_hint_or_None).
    """
    tid = _topic_id(url)

    # Beneficiary hint basato sul prefisso (invariato)
    benef = None
    for key, hint in URL_BENEFICIARY_OVERRIDE.items():
        if key in tid:
            benef = hint
            break

    # Prova la classificazione HE strutturale
    cnum, clabel, thematic = classify_horizon_europe(url, "")
    if thematic:
        return cnum, clabel, thematic, benef

    # Prova NON-HE by URL prefix
    thematic_non_he = classify_non_he_by_url(url)
    if thematic_non_he:
        return "", "", thematic_non_he, benef

    return "", "", "", benef


def name_classify(name: str) -> str:
    name_up = (name or "").upper()
    for keyword, thematic in NUMERIC_ID_NAME_RULES:
        if keyword.upper() in name_up:
            return thematic
    return ""


def prog_thematic(prog: str) -> str:
    return classify_non_he_by_programme(prog)


def resolve_thematic(cluster_num: str, prog: str) -> str:
    if cluster_num and THEMATIC_MAP.get(cluster_num):
        return THEMATIC_MAP[cluster_num]
    return prog_thematic(prog)

def normalize_action(v: str) -> str:
    s = (v or "").lower()
    if "research and innovation action" in s: return "RIA"
    if "innovation action" in s:              return "IA"
    if "coordination and support" in s:       return "CSA"
    if "cofund" in s:                         return "COFUND"
    # Abbreviazioni dirette (già normalizzate)
    u = (v or "").strip().upper()
    if u in ("RIA", "HORIZON-RIA"):  return "RIA"
    if u in ("IA",  "HORIZON-IA"):   return "IA"
    if u in ("CSA", "HORIZON-CSA"):  return "CSA"
    if u in ("COFUND", "HORIZON-COFUND"): return "COFUND"
    return v or ""

def beneficiary_hint(action: str, prog: str, url_benef):
    if url_benef is not None:
        return url_benef
    a = (action or "").upper()
    p = (prog or "").lower()
    hints = []
    if a == "IA":   hints.extend(["SME","Large enterprise","Research organisation"])
    if a == "RIA":  hints.extend(["Research organisation","SME","Large enterprise"])
    if a == "CSA":  hints.extend(["Research organisation","Public body","NGO","SME"])
    if "external action" in p: hints.extend(["NGO","Public body","Research organisation"])
    return list(dict.fromkeys(hints))

# ── Parsing date e budget ─────────────────────────────────────────────────────

def parse_date_iso(s: str) -> str:
    s = re.sub(r"\s+", " ", str(s or "")).strip()
    if not s:
        return ""
    m = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", s)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    m = re.search(r"\b(\d{1,2})[\/\-.](\d{1,2})[\/\-.](\d{4})\b", s)
    if m:
        try:
            return datetime(int(m.group(3)), int(m.group(2)), int(m.group(1))).strftime("%Y-%m-%d")
        except ValueError:
            pass
    m = re.search(r"\b(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})\b", s)
    if m:
        mo = MONTHS.get(m.group(2).lower())
        if mo:
            try:
                return datetime(int(m.group(3)), mo, int(m.group(1))).strftime("%Y-%m-%d")
            except ValueError:
                pass
    return ""

def parse_budget(s: str) -> int:
    """Normalize a raw budget string to an integer euro amount.
    Returns 0 if unparseable. Handles formats like:
      1,500,000 / 1.500.000 / 1 500 000 / 1.5M / 2,3M
    """
    if not s:
        return 0
    s = s.strip()
    # Millions shorthand: 1.5M or 2,3M
    m = re.match(r"^([\d]+[.,][\d]+)\s*[Mm]$", s)
    if m:
        try:
            return int(float(m.group(1).replace(",", ".")) * 1_000_000)
        except ValueError:
            pass
    m2 = re.match(r"^([\d]+)\s*[Mm]$", s)
    if m2:
        try:
            return int(m2.group(1)) * 1_000_000
        except ValueError:
            pass
    # Strip anything that isn't a digit, comma, dot, or space
    cleaned = re.sub(r"[^\d,. ]", "", s).strip()
    # Detect European format: 1.500.000 or 1.500.000,00
    if re.match(r"^\d{1,3}(\.\d{3})+(,\d+)?$", cleaned):
        cleaned = cleaned.replace(".", "").replace(",", ".")
    # American format: 1,500,000 or 1,500,000.00
    elif re.match(r"^\d{1,3}(,\d{3})+(\.\d+)?$", cleaned):
        cleaned = cleaned.replace(",", "")
    else:
        # Space-separated: 1 500 000
        cleaned = cleaned.replace(" ", "").replace(",", ".")
    try:
        return int(float(cleaned))
    except ValueError:
        return 0

def extract_budget_from_text(text: str) -> int:
    """Try multiple regex patterns against page body text.
    Returns the best (largest plausible) match in euros, or 0.
    """
    candidates = []
    for rx in (RE_BUDGET_INDICATIVE, RE_BUDGET_EXPECTED, RE_BUDGET_LABEL, RE_BUDGET_SUFFIX):
        for m in rx.finditer(text or ""):
            val = parse_budget(m.group(1))
            # Sanity: between €10,000 and €10 billion
            if 10_000 <= val <= 10_000_000_000:
                candidates.append(val)
    if not candidates:
        return 0
    # Prefer the largest single figure (total budget > per-project budget)
    return max(candidates)

# ── Utilità Playwright ────────────────────────────────────────────────────────

def clean(s):
    if not s:
        return None
    s = re.sub(r"\s+", " ", str(s)).strip()
    return s or None

def pick(rx, text):
    m = rx.search(text or "")
    return clean(m.group(1)) if m else None

def accept_cookies(page):
    for label in ["Accept all","Accept All","Accept","I accept","Agree","OK"]:
        for scope in [page] + list(page.frames):
            try:
                btn = scope.get_by_role("button", name=re.compile(label, re.IGNORECASE))
                if btn.count():
                    btn.first.click(timeout=2000)
                    page.wait_for_timeout(800)
                    return
            except Exception:
                pass

def wait_cookie_gone(page, max_ms=12000):
    t0 = time.time()
    while (time.time() - t0) * 1000 < max_ms:
        try:
            body = page.locator("body").inner_text()
        except Exception:
            body = ""
        if COOKIE_TEXT.lower() not in (body or "").lower():
            return
        page.wait_for_timeout(600)

def count_links(page):
    return page.locator(LINK_SELECTOR).count()

def read_total(page, timeout_ms=30000):
    print(" ⏳ In attesa della risposta dall'API SEDIA...")
    try:
        # Aspettiamo specificamente che l'API risponda
        with page.expect_response(lambda r: "apiKey=SEDIA" in r.url and r.status == 200, timeout=timeout_ms) as response_info:
            data = response_info.value.json()
            # Il campo esatto nel nuovo sistema è 'totalResults'
            count = data.get("totalResults")
            if count is not None:
                print(f" ✅ Totale rilevato dall'API: {count}")
                return int(count)
    except Exception as e:
        print(f" ⚠️ L'API non ha risposto in tempo o ha bloccato la richiesta.")
        
    # FALLBACK: Se l'API fallisce, proviamo a leggere il nuovo selettore CSS
    try:
        # Nella v1.0.15 il numero è spesso dentro una classe 'wt-count' o simile
        page.wait_for_selector(".ecl-u-type-bold", timeout=5000) 
        txt = page.locator("body").inner_text()
        m = re.search(r"(\d[\d,\.]*)\s*(?:results?|items?|found)", txt, re.I)
        if m:
            return int(m.group(1).replace(",", "").replace(".", ""))
    except:
        pass
    return None
        
        
def scroll_until(page, expected, max_ms=50000):
    start = time.time()
    last = -1
    stable_since = time.time()
    while count_links(page) == 0 and (time.time()-start)*1000 < 10000:
        accept_cookies(page)
        wait_cookie_gone(page, 3000)
        page.wait_for_timeout(700)
    container = page.evaluate_handle(f"""() => {{
        const sel = `{LINK_SELECTOR}`;
        const links = document.querySelectorAll(sel);
        if (!links.length) return null;
        let el = links[0];
        for (let i=0; i<20; i++) {{
            if (!el) break;
            const st = window.getComputedStyle(el);
            const oy = st.overflowY;
            if ((oy==='auto'||oy==='scroll') && el.scrollHeight>el.clientHeight+5) return el;
            el = el.parentElement;
        }}
        return null;
    }}""")
    while (time.time()-start)*1000 < max_ms:
        accept_cookies(page)
        wait_cookie_gone(page, 3000)
        c = count_links(page)
        if c >= expected:
            return c
        if c != last:
            last = c
            stable_since = time.time()
        try:
            if container:
                page.evaluate("(el)=>{ el.scrollTop = el.scrollTop + el.clientHeight*0.9; }", container)
            else:
                page.mouse.wheel(0, 1800)
        except Exception:
            pass
        page.wait_for_timeout(600)
        if time.time()-stable_since > 5:
            try:
                if container:
                    page.evaluate("(el)=>{ el.scrollTop = el.scrollHeight; }", container)
                else:
                    page.mouse.wheel(0, 5000)
            except Exception:
                pass
            page.wait_for_timeout(600)
    return count_links(page)

def extract_links(page):
    hrefs = page.evaluate(f"""
        () => Array.from(document.querySelectorAll('{LINK_SELECTOR}'))
                  .map(a => a.getAttribute('href'))
    """)
    out, seen = [], set()
    for h in hrefs or []:
        if not h:
            continue
        full = "https://ec.europa.eu" + h if h.startswith("/") else h
        if full not in seen:
            seen.add(full)
            out.append(full)
    return out

# ── Parsing card dalla lista ──────────────────────────────────────────────────

def parse_card(page, full_url: str) -> dict:
    path = full_url.replace("https://ec.europa.eu","").split("?")[0]
    a = page.locator(f'a[href*="{path}"]').first
    title = clean(a.inner_text()) if a.count() else path.split("/")[-1]

    card = a.locator(
        "xpath=ancestor::*[contains(.,'Programme:') or contains(.,'Opening date:') or "
        "contains(.,'Deadline date:') or contains(.,'Type of action:')][1]"
    ).first
    text = (card.inner_text() if card.count()
            else (a.locator("xpath=ancestor::*[1]").inner_text() if a.count() else ""))

    dead = pick(RE_DEAD, text) or pick(RE_NEXT_DEAD, text)
    call_id = pick(RE_CALL_ID, full_url) or pick(RE_CALL_ID, text)
    cluster_raw = pick(RE_CLUSTER, text) or pick(RE_CLUSTER, full_url) or pick(RE_CLUSTER, call_id or "")

    return {
        "name":           title,
        "call_id":        call_id,
        "programme_raw":  pick(RE_PROG, text),
        "action_raw":     pick(RE_ACTION, text),
        "cluster_raw":    cluster_raw,
        "opening_raw":    pick(RE_OPEN, text),
        "deadline_raw":   dead,
        "url":            full_url,
        "_needs_enrich":  False,
    }

# ── Arricchimento via XHR ─────────────────────────────────────────────────────

def _first(meta, *keys):
    for k in keys:
        v = meta.get(k)
        if isinstance(v, list) and v:
            return re.sub(r"\s+", " ", str(v[0])).strip()
        if v and isinstance(v, str):
            return v.strip()
    return ""

def extract_budget_per_project_dom(page, topic_id):
    """Logica Cacciatore: espande la tabella e preleva il budget semantico"""
    parts = topic_id.split('?')[0].split('-')
    target_match = "-".join(parts[-2:]) if len(parts) > 1 else parts[-1]
    try:
        # Espande la sezione 'Topic conditions'
        btn = page.locator("button:has-text('Topic conditions and documents')").first
        if btn.count() > 0:
            btn.scroll_into_view_if_needed()
            if btn.get_attribute("aria-expanded") == "false":
                btn.click(force=True)
                page.wait_for_timeout(3500)
        
        # Scrolla sulla riga specifica dell'ID
        row_locator = page.locator(f"tr:has-text('{target_match}')").first
        if row_locator.count() > 0:
            row_locator.scroll_into_view_if_needed()
            page.wait_for_timeout(1000)
            
        # Estrae il valore tramite JavaScript
        return page.evaluate(f"""
            (shortId) => {{
                const allRows = Array.from(document.querySelectorAll('tr, .wt-table-row'));
                const targetRow = allRows.find(el => el.innerText.includes(shortId));
                if (targetRow) {{
                    const cells = Array.from(targetRow.querySelectorAll('td, .wt-table-cell')).map(c => c.innerText.trim());
                    const candidates = cells.filter(txt => {{
                        const hasMoney = txt.includes('€') || txt.toLowerCase().includes('eur');
                        const isDate = /202[0-9]/.test(txt) && txt.length < 15;
                        return hasMoney && !isDate;
                    }});
                    if (candidates.length > 0) {{
                        const specific = candidates.find(b => /around|to|between/i.test(b));
                        return specific || candidates[candidates.length - 1];
                    }}
                }}
                return null;
            }}
        """, target_match)
    except: return None
        
def _enrich_one(page, row: dict) -> bool:
    """
    Apre una pagina di dettaglio, cattura i campi mancanti via XHR (handle)
    e integra la nuova logica DOM per l'estrazione precisa del budget.
    """
    url      = row["url"]
    captured = {}
    # Estraiamo il topic_id dall'URL per il Cacciatore di Righe
    topic_id = url.split('/')[-1].split('?')[0]

    def handle(response, _c=captured):
        if SEARCH_API in response.url and response.status == 200:
            try:
                body = response.json()
                for item in body.get("results", [body]):
                    meta    = item.get("metadata", {}) or {}
                    prog_id = _first(meta, "frameworkProgramme", "programme")
                    cid     = _first(meta, "callIdentifier","identifier")

                    if prog_id and not _c.get("prog"):
                        _c["prog"] = PROGRAMME_MAP.get(prog_id, prog_id)
                    if cid and not _c.get("call_id"):
                        _c["call_id"] = cid

                    # ── Parsing campo `actions` (fonte più affidabile per action type e deadline) ──
                    # actions è una stringa JSON: [{"types":[{"typeOfAction":"..."}], "deadlineDates":[...], "plannedOpeningDate":"..."}]
                    raw_actions = meta.get("actions")
                    if isinstance(raw_actions, list): raw_actions = raw_actions[0] if raw_actions else None
                    if raw_actions and not _c.get("action_parsed"):
                        try:
                            acts = json.loads(raw_actions) if isinstance(raw_actions, str) else raw_actions
                            if isinstance(acts, list) and acts:
                                act0 = acts[0]
                                # Action type: dentro types[0].typeOfAction
                                types = act0.get("types", [])
                                if types and not _c.get("action"):
                                    toa = types[0].get("typeOfAction", "")
                                    if toa:
                                        _c["action"] = toa
                                # Deadline: deadlineDates è lista di stringhe "YYYY-MM-DD"
                                dl_dates = act0.get("deadlineDates", [])
                                if dl_dates and not _c.get("deadline"):
                                    # Prendi la più lontana (ultima scadenza)
                                    _c["deadline"] = sorted(dl_dates)[-1]
                                # Opening date
                                pod = act0.get("plannedOpeningDate", "")
                                if pod and not _c.get("opening"):
                                    _c["opening"] = pod
                            _c["action_parsed"] = True
                        except Exception:
                            pass

                    # Fallback action da typesOfAction se actions non l'ha fornita
                    if not _c.get("action"):
                        action_fb = _first(meta, "typesOfAction", "typeOfAction", "fundingScheme")
                        if action_fb:
                            _c["action"] = action_fb

                    # ── Deadline da campi diretti (fallback se actions non disponibile) ──
                    if not _c.get("deadline"):
                        deadline_detail = _first(meta, "deadlineDate", "nextDeadline", "closingDate")
                        if deadline_detail:
                            _c["deadline"] = deadline_detail
                    # Opening da campi diretti
                    if not _c.get("opening"):
                        opening_detail = _first(meta, "startDate", "openingDate", "publicationDate")
                        if opening_detail:
                            _c["opening"] = opening_detail

                    # ── Budget da budgetOverview (fonte primaria e più precisa) ──
                    if not _c.get("budget"):
                        raw_overview = meta.get("budgetOverview")
                        if isinstance(raw_overview, list):
                            raw_overview = raw_overview[0] if raw_overview else None
                        if raw_overview:
                            try:
                                overview = json.loads(raw_overview) if isinstance(raw_overview, str) else raw_overview
                                # Identifier del topic dall'URL (es. HORIZON-CL6-2027-02-FARM2FORK-06)
                                # NON il callIdentifier (es. HORIZON-CL6-2027-02) che è troppo generico
                                tid_local = row.get("url","").split("/")[-1].split("?")[0].upper()
                                topic_map = overview.get("budgetTopicActionMap", {})
                                for entry_list in topic_map.values():
                                    for entry in (entry_list if isinstance(entry_list, list) else [entry_list]):
                                        action_str = entry.get("action", "").upper()
                                        if tid_local and tid_local in action_str:
                                            min_c = entry.get("minContribution")
                                            if min_c and int(min_c) > 0:
                                                _c["budget"] = int(min_c)
                                                _c["budget_total"] = sum(
                                                    int(v) for v in entry.get("budgetYearMap", {}).values()
                                                    if str(v).isdigit()
                                                ) or int(min_c)
                                                _c["expected_grants"] = entry.get("expectedGrants")
                                                break
                                    if _c.get("budget"):
                                        break
                            except Exception:
                                pass

                    # ── Budget da altri campi XHR (fallback) ──
                    if not _c.get("budget"):
                        for key in (
                            "budgetOverviewTotal", "totalBudget", "budget",
                            "budgetTopicActions", "indicativeBudget",
                            "availableBudget", "estimatedTotalContribution"
                        ):
                            raw = meta.get(key)
                            if isinstance(raw, list): raw = raw[0] if raw else None
                            if raw is not None:
                                val = parse_budget(str(raw))
                                if val > 0:
                                    _c["budget"] = val
                                    break

                    # ── full_text dall'XHR (fonte primaria, sempre completo) ──
                    # descriptionByte  → scope + expected outcomes (HTML)
                    # destinationDetails → contesto destination (HTML)
                    # topicConditions  → ammissibilità (HTML)
                    if not _c.get("full_text"):
                        from html.parser import HTMLParser

                        class _StripHTML(HTMLParser):
                            def __init__(self):
                                super().__init__()
                                self._parts = []
                            def handle_data(self, data):
                                self._parts.append(data)
                            def get_text(self):
                                return " ".join(self._parts)

                        def strip_html(html_str):
                            if not html_str:
                                return ""
                            p = _StripHTML()
                            try:
                                p.feed(html_str)
                            except Exception:
                                pass
                            return re.sub(r"\s+", " ", p.get_text()).strip()

                        parts = []
                        title_xhr = _first(meta, "title")
                        if title_xhr:
                            parts.append(title_xhr)
                        desc = _first(meta, "descriptionByte")
                        if desc:
                            parts.append(strip_html(desc))
                        dest = _first(meta, "destinationDetails")
                        if dest:
                            parts.append(strip_html(dest))
                        conds = _first(meta, "topicConditions")
                        if conds:
                            parts.append(strip_html(conds))

                        combined = " ".join(parts)
                        if combined:
                            _c["full_text"] = combined

            except Exception:
                pass

    page.on("response", handle)
    try:
        # 1. Navigazione
        page.goto(url, wait_until="domcontentloaded", timeout=40000)
        page.wait_for_timeout(2500)
        
        # 2. Testo dal DOM — solo come fallback se l'XHR non ha già fornito il testo
        try:
            body_text = page.locator("body").inner_text(timeout=5000)
        except Exception:
            body_text = ""

        # 3. LOGICA "CACCIATORE DI RIGHE" (Espansione Tabella DOM)
        # Questa funzione deve essere definita sopra _enrich_one
        budget_val_dom = extract_budget_per_project_dom(page, topic_id)

        # ── full_text: priorità XHR (sempre completo e senza cookie banner),
        #               fallback DOM solo se non contiene il testo del cookie.
        if captured.get("full_text"):
            row["full_text"] = captured["full_text"]
        elif body_text and COOKIE_TEXT.lower() not in body_text.lower():
            row["full_text"] = clean(body_text) or ""
        else:
            row["full_text"] = ""

        # Priorità budget: 1) budgetOverview XHR (preciso per topic)
        #                   2) DOM tabella  3) regex testo libero
        if captured.get("budget"):
            row["budget_raw"]       = captured["budget"]
            row["budget_total_raw"] = captured.get("budget_total", captured["budget"])
            row["expected_grants"]  = captured.get("expected_grants")
        if not row.get("budget_raw") and budget_val_dom:
            val_dom = parse_budget(str(budget_val_dom)) if isinstance(budget_val_dom, str) else int(budget_val_dom)
            if val_dom > 0:
                row["budget_raw"] = val_dom
        if not row.get("budget_raw") and body_text and COOKIE_TEXT.lower() not in body_text.lower():
            val_reg = extract_budget_from_text(body_text)
            if val_reg > 0:
                row["budget_raw"] = int(val_reg)

    except Exception as e:
        print(f"    [ERR goto] {e}", flush=True)
    finally:
        page.remove_listener("response", handle)

    # 4. Assegnazione finale metadati
    # full_text: se l'XHR è arrivato dopo il blocco try (raro ma possibile), aggiorna
    if captured.get("full_text") and not row.get("full_text"):
        row["full_text"] = captured["full_text"]

    if captured.get("prog") and not row.get("programme_raw"):
        row["programme_raw"] = captured["prog"]
    if captured.get("action"):
        # Sovrascrive sempre: la pagina di dettaglio ha l'action type preciso
        row["action_raw"] = captured["action"]
    if captured.get("call_id") and not row.get("call_id"):
        row["call_id"] = captured["call_id"]
    # Deadline dalla pagina di dettaglio (fonte più affidabile: campo actions.deadlineDates)
    if captured.get("deadline"):
        row["deadline_raw"] = captured["deadline"]
    # Opening dalla pagina di dettaglio
    if captured.get("opening") and not row.get("opening_raw"):
        row["opening_raw"] = captured["opening"]

    return bool(captured) or bool(row.get("full_text"))


def enrich(ctx, rows: list):
    # Arricchiamo TUTTE le call con URL valido per ottenere full_text e budget
    to_fix = [r for r in rows if r.get("url")]
    if not to_fix:
        print("  Nessuna call con URL da arricchire.", flush=True)
        return

    print(f"  {len(to_fix)} call da arricchire (full_text + budget)…", flush=True)
    page = ctx.new_page()
    skipped = 0

    for idx, row in enumerate(to_fix, 1):
        print(f"  [{idx:>4}/{len(to_fix)}] {(row['name'] or '')[:60]}", flush=True)

        ok = False
        for attempt in range(1, 3):
            try:
                ok = _enrich_one(page, row)
                break
            except Exception as e:
                print(f"    [tentativo {attempt} fallito] {e}", flush=True)
                try:
                    page.close()
                except Exception:
                    pass
                page = ctx.new_page()
                time.sleep(2)

        if not ok:
            skipped += 1
            print(f"    [SKIP] nessun dato recuperato", flush=True)

        if idx % 100 == 0:
            print(f"  [checkpoint] salvate {idx} call finora…", flush=True)

        time.sleep(0.3)

    try:
        page.close()
    except Exception:
        pass
    print(f"  Arricchimento completato. Saltate: {skipped}/{len(to_fix)}", flush=True)

# ── Trasforma riga grezza → oggetto call classificato ─────────────────────────

def to_call(row: dict) -> dict:
    url        = row.get("url", "")
    prog_raw   = row.get("programme_raw") or ""
    call_id    = row.get("call_id") or ""
    action_raw = row.get("action_raw") or ""

    cluster_num = ""
    for src in [call_id, row.get("cluster_raw",""), url]:
        m = RE_CLUSTER.search(src or "")
        if m:
            cluster_num = m.group(1)
            break

    # ── Classificazione tematica strutturale a due livelli ────────────────────
    #
    # LIVELLO 1 – Horizon Europe: classificazione strutturale per CL1-6 + sottoprogrammi
    # LIVELLO 2 – Altri programmi: codice programma dall'URL o dal campo programme
    #
    he_flag = _is_horizon_europe(prog_raw, url, call_id)
    cluster_label = ""
    thematic = ""

    if he_flag:
        # ── L1: Horizon Europe ────────────────────────────────────────────────
        cnum, clabel, th = classify_horizon_europe(url, call_id)
        if cnum:
            cluster_num = cnum
        if clabel:
            cluster_label = clabel
        if th:
            thematic = th
        # Fallback: se CL trovato ma non mappato
        if not thematic and cluster_num:
            thematic = HE_CLUSTER_MAP.get(cluster_num, "")
    else:
        # ── L2: Altri programmi ───────────────────────────────────────────────
        # 2a. Prefisso topic ID nell'URL (più preciso: cattura sottoprogrammi come PPPA-CHIPS, CEF-T, ecc.)
        thematic = classify_non_he_by_url(url)
        # 2b. Nome del programma (campo programme_raw)
        if not thematic:
            thematic = classify_non_he_by_programme(prog_raw)
        # 2c. cluster_label da THEMATIC_MAP se abbiamo un cluster numerico
        if not cluster_label and cluster_num:
            cluster_label = THEMATIC_MAP.get(cluster_num, "")

    # ── Fallback comune: name_classify (NUMERIC_ID_NAME_RULES) ───────────────
    if not thematic:
        thematic = name_classify(row.get("name", ""))

    # Allinea cluster_num con il thematic se ricavato dal THEMATIC_MAP inverso
    if not cluster_num and cluster_label:
        _inv = {v: k for k, v in THEMATIC_MAP.items()}
        cluster_num = _inv.get(cluster_label, "")

    # ── Beneficiary hint (invariato) ──────────────────────────────────────────
    u_cnum, u_clabel, u_thematic, u_benef = url_classify(url)
    # url_classify può raffinare cluster_label se non ancora impostato
    if not cluster_label and u_clabel:
        cluster_label = u_clabel

    action     = normalize_action(action_raw)
    is_mission = bool("/HORIZON-MISS" in url.upper())

    opening_raw  = row.get("opening_raw") or ""
    deadline_raw = row.get("deadline_raw") or ""

    full_text = row.get("full_text") or ""
    multi = classify_multitopic(row.get("name") or "", full_text, thematic)

    # ── Promozione tematica: se ancora generico, usa keyword dal full_text ────
    # "Cross-cutting / Other" è accettabile SOLO per WIDERA e call genuinamente
    # interdisciplinari; tutto il resto deve avere un'area specifica.
    GENERIC_THEMATICS = {"Cross-cutting / Other", ""}
    # Aree che NON devono essere promosse tramite keyword (sono già specifiche)
    PROMOTION_EXEMPT = {
        "Internships, fellowships & scholarships",  # ERC/MSCA: dipende dal dominio, non promuovere casualmente
    }
    effective_thematic = thematic
    if effective_thematic in GENERIC_THEMATICS and multi["multi_thematic"]:
        for candidate in multi["multi_thematic"]:
            if candidate not in GENERIC_THEMATICS and candidate not in PROMOTION_EXEMPT:
                effective_thematic = candidate
                break

    # Assicura che il thematic primario sia in multi_thematic
    all_thematics = list(multi["multi_thematic"])
    if effective_thematic and effective_thematic not in all_thematics:
        all_thematics.insert(0, effective_thematic)

    deadline_raw_final = row.get("deadline_raw") or ""
    opening_raw_final  = row.get("opening_raw") or ""

    return {
        "name":             row.get("name") or "",
        "call_id":          call_id,
        "programme":        prog_raw,
        "cluster_num":      cluster_num,
        "cluster_label":    cluster_label,
        "thematic_cluster": effective_thematic,
        "action":           action,
        "opening":          parse_date_iso(opening_raw_final),
        "deadline":         parse_date_iso(deadline_raw_final),
        "url":              url,
        "is_mission":       is_mission,
        "beneficiary_hint": beneficiary_hint(action, prog_raw, u_benef),
        "budget":           int(row.get("budget_raw") or 0),
        "budget_total":     int(row.get("budget_total_raw") or row.get("budget_raw") or 0),
        "expected_grants":  row.get("expected_grants"),
        "full_text":        multi["full_text"],
        "keyword_hits":     multi["keyword_hits"],
        "multi_thematic":   all_thematics,
        "is_special_basic_research": multi["is_special_basic_research"],
    }

# ── Changelog ─────────────────────────────────────────────────────────────────

def write_changelog(old_calls: list, new_calls: list, changelog_path: Path, generated: str):
    old_by_url = {c["url"]: c for c in old_calls}
    new_by_url = {c["url"]: c for c in new_calls}

    old_urls = set(old_by_url)
    new_urls = set(new_by_url)

    added   = [new_by_url[u] for u in sorted(new_urls - old_urls)]
    removed = [old_by_url[u] for u in sorted(old_urls - new_urls)]

    def thematic_counts(calls):
        tc = {}
        for c in calls:
            k = c.get("thematic_cluster") or "(non classificato)"
            tc[k] = tc.get(k, 0) + 1
        return tc

    date_str = generated[:10]

    lines = []
    lines.append(f"# Changelog calls.json")
    lines.append(f"")
    lines.append(f"**Ultimo aggiornamento:** {generated.replace('T',' ').replace('+00:00',' UTC')[:22]}")
    lines.append(f"")
    lines.append(f"## Riepilogo")
    lines.append(f"")
    lines.append(f"| | Numero |")
    lines.append(f"|---|---|")
    lines.append(f"| Call totali (nuovo) | {len(new_calls)} |")
    lines.append(f"| Call totali (precedente) | {len(old_calls)} |")
    lines.append(f"| **Nuove call aggiunte** | **{len(added)}** |")
    lines.append(f"| Call rimosse (scadute/chiuse) | {len(removed)} |")
    lines.append(f"")

    if added:
        lines.append(f"## Call aggiunte ({len(added)})")
        lines.append(f"")
        by_thematic = {}
        for c in added:
            t = c.get("thematic_cluster") or "(non classificato)"
            by_thematic.setdefault(t, []).append(c)
        for thematic, calls in sorted(by_thematic.items()):
            lines.append(f"### {thematic} ({len(calls)})")
            lines.append(f"")
            for c in calls:
                name    = c.get("name") or "(senza nome)"
                prog    = c.get("programme") or ""
                action  = c.get("action") or ""
                dead    = c.get("deadline") or ""
                url     = c.get("url") or ""
                meta = " · ".join(filter(None, [prog, action, f"Scadenza: {dead}" if dead else ""]))
                lines.append(f"- **{name}**")
                if meta:
                    lines.append(f"  {meta}")
                if url:
                    lines.append(f"  {url}")
                lines.append(f"")
    else:
        lines.append(f"## Call aggiunte")
        lines.append(f"")
        lines.append(f"Nessuna nuova call rispetto alla rilevazione precedente.")
        lines.append(f"")

    if removed:
        lines.append(f"## Call rimosse ({len(removed)})")
        lines.append(f"")
        for c in removed:
            name = c.get("name") or "(senza nome)"
            prog = c.get("programme") or ""
            dead = c.get("deadline") or ""
            meta = " · ".join(filter(None, [prog, f"Scadenza: {dead}" if dead else ""]))
            lines.append(f"- **{name}**{(' — ' + meta) if meta else ''}")
        lines.append(f"")

    lines.append(f"## Distribuzione per area tematica (nuovo dataset)")
    lines.append(f"")
    lines.append(f"| Area tematica | Call |")
    lines.append(f"|---|---|")
    for k, v in sorted(thematic_counts(new_calls).items(), key=lambda x: -x[1]):
        lines.append(f"| {k} | {v} |")
    lines.append(f"")

    changelog_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n📋 Changelog scritto: {changelog_path} (+{len(added)} aggiunte, -{len(removed)} rimosse)")

    history_path = changelog_path.parent / "changelog_history.md"
    history_line = (
        f"| {date_str} | {len(new_calls)} | +{len(added)} | -{len(removed)} |"
    )
    if history_path.exists():
        hist = history_path.read_text(encoding="utf-8")
        if history_line not in hist:
            hist = hist.rstrip() + "\n" + history_line + "\n"
            history_path.write_text(hist, encoding="utf-8")
    else:
        header = (
            "# Storico aggiornamenti calls.json\n\n"
            "| Data | Call totali | Aggiunte | Rimosse |\n"
            "|---|---|---|---|\n"
            + history_line + "\n"
        )
        history_path.write_text(header, encoding="utf-8")
    print(f"📋 History aggiornata: {history_path}")

# ── Main ──────────────────────────────────────────────────────────────────────

def main(out_path: Path):
    rows      = []
    seen_urls = set()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
        ctx = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
        page = ctx.new_page()
        try:
            if hasattr(playwright_stealth, 'stealth_sync'):
                playwright_stealth.stealth_sync(page)
            elif hasattr(playwright_stealth, 'stealth'):
                from playwright_stealth.stealth import stealth as _st
                _st(page)
            else:
                print("⚠️ Impossibile applicare stealth, procedo comunque...")
        except Exception as e:
            print(f"⚠️ Errore stealth ignorato: {e}")

        # ── Passo 1: lista ────────────────────────────────────────────────────
        # Intercettiamo la risposta API PRIMA del goto per catturare totalResults
        total_captured = {}
        api_url_captured = {}   # cattura l'URL reale dell'API SEDIA per uso diretto

        def handle_first_response(response, _tc=total_captured, _au=api_url_captured):
            if SEARCH_API in response.url and response.status == 200 and "total" not in _tc:
                try:
                    body = response.json()
                    t = body.get("totalResults")
                    if t is not None:
                        _tc["total"] = int(t)
                        _au["url"] = response.url   # salva URL completo per requests
                        print(f" ✅ Totale rilevato dall'API: {t}")
                except Exception:
                    pass

        page.on("response", handle_first_response)
        page.goto(LIST_URL.format(page=1, ps=PAGE_SIZE),
                  wait_until="domcontentloaded", timeout=90000)

        # Forza l'accettazione dei cookie
        try:
            cookie_button = page.get_by_role("button", name="Accept all cookies")
            if cookie_button.is_visible():
                cookie_button.click()
                print("✅ Cookie accettati")
                page.wait_for_timeout(4000)
        except:
            pass

        # Aspetta che l'API risponda (max 30s)
        print(" ⏳ In attesa della risposta dall'API SEDIA...")
        deadline_init = time.time() + 30
        while "total" not in total_captured and time.time() < deadline_init:
            page.wait_for_timeout(500)

        page.remove_listener("response", handle_first_response)

        total = total_captured.get("total") or read_total(page)

        if total is None:
            print("❌ Non riesco a leggere il contatore delle call.")
            browser.close()
            return
        max_pages = math.ceil(total / PAGE_SIZE)
        print(f"✅ Totale: {total} call | pagine: {max_pages}")

        for pnum in range(1, max_pages + 1):
            remaining = total - (pnum - 1) * PAGE_SIZE
            expected  = min(PAGE_SIZE, remaining)
            url = LIST_URL.format(page=pnum, ps=PAGE_SIZE)
            print(f"\n[p{pnum}/{max_pages}] attese ~{expected}", end="", flush=True)

            page_results = []

            def handle_list_response(response, _pr=page_results):
                if SEARCH_API in response.url and response.status == 200:
                    try:
                        body = response.json()
                        api_count = len(body.get("results", []))
                        print(f" [API p{body.get('pageNumber','?')}: {api_count}]", end="", flush=True)
                        for item in body.get("results", []):
                            ref = item.get("reference", "")
                            if not ref:
                                continue

                            # URL: usa quello già presente nell'API (sempre corretto)
                            meta = item.get("metadata", {}) or {}
                            full_url = (
                                item.get("url")
                                or _first(meta, "url", "esST_URL")
                                or ""
                            )
                            # Fallback euristico solo se proprio manca tutto
                            if not full_url:
                                cid_tmp = _first(meta, "identifier", "callIdentifier") or ref
                                full_url = TOPIC_BASE_URL + cid_tmp

                            prog_id      = _first(meta, "frameworkProgramme", "programme")
                            action       = _first(meta, "typesOfAction", "typeOfAction", "fundingScheme")
                            cid          = _first(meta, "identifier", "callIdentifier")
                            # title è null a livello root, ma presente in metadata["title"][0]
                            title        = _first(meta, "title", "name") or item.get("summary") or ref
                            opening_raw  = _first(meta, "startDate", "openingDate", "publicationDate")
                            deadline_raw = _first(meta, "deadlineDate", "nextDeadline", "closingDate")
                            cluster_raw  = pick(RE_CLUSTER, full_url) or pick(RE_CLUSTER, cid or "")

                            _pr.append({
                                "name":          clean(title) or ref,
                                "call_id":       cid,
                                "programme_raw": PROGRAMME_MAP.get(prog_id, prog_id) if prog_id else None,
                                "action_raw":    action or None,
                                "cluster_raw":   cluster_raw,
                                "opening_raw":   opening_raw or None,
                                "deadline_raw":  deadline_raw or None,
                                "url":           full_url,
                                "_ref":          ref,
                                "_needs_enrich": False,
                            })
                    except Exception as e:
                        print(f"\n    [WARN parse API p{pnum}] {e}", flush=True)

            page.on("response", handle_list_response)
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=90000)
                # Aspetta che l'API risponda (max 25s)
                deadline_t = time.time() + 25
                while len(page_results) == 0 and time.time() < deadline_t:
                    page.wait_for_timeout(500)
                    accept_cookies(page)
                # Se ancora vuoto, forza reload
                if len(page_results) == 0:
                    page.reload(wait_until="domcontentloaded", timeout=60000)
                    deadline_t2 = time.time() + 20
                    while len(page_results) == 0 and time.time() < deadline_t2:
                        page.wait_for_timeout(500)
                if len(page_results) == 0:
                    print(f" ⚠️ Nessuna risposta API per p{pnum}", flush=True)
            finally:
                page.remove_listener("response", handle_list_response)

            new_items = [r for r in page_results if r.get("_ref") not in seen_urls]
            print(f" → trovati {len(new_items)} nuovi (API totale: {len(page_results)})", flush=True)

            for r in new_items:
                seen_urls.add(r["_ref"])   # _ref è sempre univoco lato SEDIA
                rows.append(r)
            time.sleep(0.3)

        # ── Passo 1b: recupero call mancanti con seconda passata Playwright ──
        missing = total - len(rows)
        if missing > 0:
            print(f"\n⚠️  {missing} call mancanti (duplicati cross-pagina SEDIA). Seconda passata...", flush=True)
            for pg in range(1, max_pages + 1):
                if len(rows) >= total:
                    break
                url2 = LIST_URL.format(page=pg, ps=PAGE_SIZE)
                page_results2 = []

                def handle_recovery(response, _pr=page_results2):
                    if SEARCH_API in response.url and response.status == 200:
                        try:
                            body = response.json()
                            if not body.get("results"):
                                return
                            for item in body.get("results", []):
                                ref = item.get("reference", "")
                                if not ref:
                                    continue
                                meta = item.get("metadata", {}) or {}
                                full_url = item.get("url") or _first(meta, "url", "esST_URL") or ""
                                if not full_url:
                                    continue
                                _pr.append((ref, full_url, item))
                        except Exception:
                            pass

                page.on("response", handle_recovery)
                try:
                    page.goto(url2, wait_until="domcontentloaded", timeout=90000)
                    t0 = time.time()
                    while len(page_results2) == 0 and time.time() - t0 < 20:
                        page.wait_for_timeout(500)
                finally:
                    page.remove_listener("response", handle_recovery)

                for ref, full_url, item in page_results2:
                    if ref in seen_urls:
                        continue
                    seen_urls.add(ref)
                    meta         = item.get("metadata", {}) or {}
                    prog_id      = _first(meta, "frameworkProgramme", "programme")
                    action       = _first(meta, "typesOfAction", "typeOfAction", "fundingScheme")
                    cid          = _first(meta, "identifier", "callIdentifier")
                    title        = _first(meta, "title", "name") or item.get("summary") or ref
                    opening_raw  = _first(meta, "startDate", "openingDate", "publicationDate")
                    deadline_raw = _first(meta, "deadlineDate", "nextDeadline", "closingDate")
                    cluster_raw  = pick(RE_CLUSTER, full_url) or pick(RE_CLUSTER, cid or "")
                    rows.append({
                        "name":          clean(title) or ref,
                        "call_id":       cid,
                        "programme_raw": PROGRAMME_MAP.get(prog_id, prog_id) if prog_id else None,
                        "action_raw":    action or None,
                        "cluster_raw":   cluster_raw,
                        "opening_raw":   opening_raw or None,
                        "deadline_raw":  deadline_raw or None,
                        "url":           full_url,
                        "_ref":          ref,
                        "_needs_enrich": False,
                    })
                    print(f"  ✚ {clean(title) or ref}", flush=True)
            still_missing = total - len(rows)
            print(f"  Dopo recupero: {len(rows)}/{total} call (ancora mancanti: {still_missing})", flush=True)
            if still_missing > 0:
                print(f"  ⚠️  {still_missing} call irreperibili dopo doppia passata — potrebbero essere duplicati lato server SEDIA o call ritirate nel frattempo.", flush=True)

        # ── Passo 2: arricchimento ────────────────────────────────────────────
        print(f"\n═══ Passo 2: arricchimento {len(rows)} call totali ═══", flush=True)
        enrich(ctx, rows)
        browser.close()

    # ── Classificazione e output ──────────────────────────────────────────────
    calls = []
    seen_refs = set()
    seen_urls = set()
    for row in rows:
        call = to_call(row)
        # Chiave primaria = _ref (reference API, sempre univoco); fallback url
        ref_key = row.get("_ref") or ""
        url_key = call.get("url") or ""
        # Salta solo se ENTRAMBE le chiavi sono già viste (evita falsi duplicati
        # causati da URL euristici diversi per lo stesso topic)
        if ref_key and ref_key in seen_refs:
            continue
        if not ref_key and url_key and url_key in seen_urls:
            continue
        if ref_key:
            seen_refs.add(ref_key)
        if url_key:
            seen_urls.add(url_key)
        calls.append(call)

    tc = {}
    for c in calls:
        k = c["thematic_cluster"] or "(non classificato)"
        tc[k] = tc.get(k, 0) + 1
    print(f"\nClassificazione ({len(calls)} call totali):")
    for k, v in sorted(tc.items(), key=lambda x: -x[1]):
        print(f"  {v:5d}  {k}")
    print(f"\nNon classificati: {tc.get('(non classificato)', 0)}")

    generated = datetime.now(timezone.utc).isoformat()

    # ── Changelog ────────────────────────────────────────────────────────────
    old_calls = []
    if out_path.exists():
        try:
            old_data = json.loads(out_path.read_text(encoding="utf-8"))
            old_calls = old_data.get("calls", [])
            print(f"\nDataset precedente: {len(old_calls)} call")
        except Exception:
            print("\nNessun dataset precedente trovato.")

    changelog_path = out_path.parent / "changelog.md"
    write_changelog(old_calls, calls, changelog_path, generated)

    # ── Salva ─────────────────────────────────────────────────────────────────
    payload = {
        "generated": generated,
        "calls": calls,
    }
    out_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"\n✅ Scritto {out_path} con {len(calls)} call")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="calls.json", help="Percorso output JSON")
    args = parser.parse_args()
    main(Path(args.out))


































































































