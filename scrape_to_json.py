"""
scrape_to_json.py  —  v2.2 (fix perdita call)
──────────────────────────────────────────────────────────────────────────────
Scrapa il portale EU Funding & Tenders con Playwright e produce calls.json.

Modifiche v2.2 rispetto a v2.1:
  • _harvest_xhr_links: usa un cursore (_cursor) sulla lista cumulativa XHR
    invece di ri-scansionare tutta la lista ad ogni pagina → elimina il bug
    per cui p3/p4/p8 sembravano avere meno link del dovuto (erano presenti,
    ma già marcati come "seen" perché la lista veniva riletta dall'inizio)
  • _normalize_url(): normalizza URL per dedup coerente (lowercase path,
    no trailing slash, no fragment) → elimina falsi duplicati per variazioni
    di case nell'identificatore (es. HORIZON-cl5 vs HORIZON-CL5)
  • seen_urls ora contiene URL normalizzati, usati sia da XHR che da DOM
    fallback in modo coerente
  • _wait_for_xhr_page(): attesa attiva (polling 300ms) che i link della
    pagina corrente siano arrivati via XHR prima di procedere → elimina race
    condition con wait_for_timeout(2500) fixed
  • Output: mostra "⚠ MANCANTI:N" se una pagina ha portato meno call del
    previsto, per facilitare il debug
  • Tutte le logiche di classificazione e arricchimento invariate
"""

import re
import math
import time
import json
import argparse
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ── Parametri ──────────────────────────────────────────────────────────────────

PAGE_SIZE = 50
DEBUG     = False

LIST_URL = (
    "https://ec.europa.eu/info/funding-tenders/opportunities/portal/screen"
    "/opportunities/calls-for-proposals"
    "?order=DESC&pageNumber={page}&pageSize={ps}&sortBy=startDate"
    "&isExactMatch=true&status=31094501,31094502&programmePeriod=2021%20-%202027"
)

# Sarà aggiornato dinamicamente da _sniff_api_path() prima dello scraping.
# I pattern qui sono tutti i path noti, dal più recente al più vecchio.
SEARCH_API_CANDIDATES = [
    "search-api/prod/rest/search",
    "search-api/rest/search",
    "rest/search",
    "/api/search",
    "/api/v1/search",
    "/api/v2/search",
    "funding-tenders/opportunities/rest",
    "opportunities/rest",
    "portal/rest",
]
SEARCH_API = SEARCH_API_CANDIDATES[0]   # aggiornato a runtime

COOKIE_TEXT = "This site uses cookies"

# Selettori candidati — provati in ordine sulla prima pagina.
# Aggiunti selettori Angular/Material/EUI moderni (post-update maggio 2026).
LINK_SELECTORS_CANDIDATES = [
    # ── Post-update maggio 2026 (Angular Material / EUI v3+) ──
    'eui-card a[href]',
    'mat-card a[href]',
    '[class*="opportunity-card"] a[href]',
    '[class*="call-card"] a[href]',
    '[class*="result-card"] a[href]',
    'a[routerlink*="/calls/"]',
    'a[routerlink*="/topic-details/"]',
    '[data-testid*="call"] a[href]',
    '[data-cy*="call"] a[href]',
    # ── Pattern href noti ──
    'a[href*="/calls/"]',
    'a[href*="callIdentifier"]',
    'a[href*="/opportunities/"]',
    'a[href*="/topic-details/"]',
    'a[href*="/competitive-calls-cs/"]',
    'a[href*="/prospect-details/"]',
    'a[href*="ec.europa.eu/info/funding"]',
    'a[href*="cftIdentifier"]',
    # ── Fallback ultra-generico: qualsiasi link nella sezione risultati ──
    '[class*="results"] a[href]',
    '[class*="list"] a[href]',
    'main a[href]',
]

LINK_SELECTOR: str = ""

HREF_CALL_PATTERNS = [
    re.compile(r"/topic-details/",        re.IGNORECASE),
    re.compile(r"/competitive-calls-cs/", re.IGNORECASE),
    re.compile(r"/prospect-details/",     re.IGNORECASE),
    re.compile(r"/calls/[A-Z0-9\-]+",     re.IGNORECASE),
    re.compile(r"callIdentifier=",        re.IGNORECASE),
    re.compile(r"cftIdentifier=",         re.IGNORECASE),
    re.compile(r"topicId=",               re.IGNORECASE),
    re.compile(r"/opportunities/portal",  re.IGNORECASE),
    re.compile(r"portal/screen/",         re.IGNORECASE),
]

RE_TOTAL     = re.compile(r"(\d+)\s*item\s*\(?s\)?\s*found", re.IGNORECASE)
RE_OPEN      = re.compile(r"Opening date:\s*([^\|\n\r]+)",          re.IGNORECASE)
RE_DEAD      = re.compile(r"Deadline date:\s*([^\|\n\r]+)",         re.IGNORECASE)
RE_NEXT_DEAD = re.compile(r"Next deadline:\s*([^\|\n\r]+)",         re.IGNORECASE)
RE_PROG      = re.compile(r"Programme:\s*([^\|\n\r]+)",             re.IGNORECASE)
RE_ACTION    = re.compile(r"Type of action:\s*([^\|\n\r]+)",        re.IGNORECASE)
RE_CLUSTER   = re.compile(r"HORIZON-CL([1-6])",                     re.IGNORECASE)
RE_CALL_ID   = re.compile(r"callIdentifier[=:\s]+([^\s&\|\n\r]+)",  re.IGNORECASE)

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

# ── Tabelle di classificazione (invariate) ─────────────────────────────────────

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

THEMATIC_MAP = {
    "1":"Health & Life Sciences","2":"Culture, Creativity & Inclusion",
    "3":"Security & Resilience","4":"Digital, Industry & Space",
    "5":"Climate, Energy & Mobility","6":"Food, Bioeconomy & Environment",
    "M-CIT":"Climate-neutral & Smart Cities",
    "M-OCEAN":"Healthy Oceans, Seas, Coastal & Inland Waters",
}

PROGRAMME_THEMATIC_MAP = [
    ("European Defence Fund",           "Defence"),
    ("EDF",                             "Defence"),
    ("EU External Action",              "External Action & International Cooperation"),
    ("EU External Action-Prospect",     "External Action & International Cooperation"),
    ("Single Market Programme",         "SME, Entrepreneurship & Market Uptake"),
    ("CERV",                            "Culture, Creativity & Inclusion"),
    ("Creative Europe",                 "Culture, Creativity & Inclusion"),
    ("Erasmus+",                        "Culture, Creativity & Inclusion"),
    ("European Social Fund+",           "Culture, Creativity & Inclusion"),
    ("Just Transition",                 "Climate, Energy & Mobility"),
    ("Innovation Fund",                 "Climate, Energy & Mobility"),
    ("EMFAF",                           "Food, Bioeconomy & Environment"),
    ("LIFE",                            "Food, Bioeconomy & Environment"),
    ("Euratom",                         "Climate, Energy & Mobility"),
    ("Connecting Europe",               "Climate, Energy & Mobility"),
    ("Internal Security Fund",          "Security & Resilience"),
    ("European Solidarity Corps",       "Culture, Creativity & Inclusion"),
    ("Digital Europe",                  "Digital, Industry & Space"),
    ("RENEWFM",                         "Climate, Energy & Mobility"),
    ("SOCPL",                           "Culture, Creativity & Inclusion"),
    ("JUST",                            "Culture, Creativity & Inclusion"),
    ("Pericles IV",                     "Culture, Creativity & Inclusion"),
    ("I3",                              "SME, Entrepreneurship & Market Uptake"),
    ("ERC",                             "Cross-cutting / Other"),
    ("43392145",                        "Food, Bioeconomy & Environment"),
    ("Horizon Europe",                  "Cross-cutting / Other"),
]

URL_RULES = [
    ("MISS","CIT",      "M-CIT", "Climate-neutral & Smart Cities",               "Climate-neutral & Smart Cities"),
    ("MISS","OCEAN",    "M-OCEAN","Healthy Oceans, Seas, Coastal & Inland Waters","Healthy Oceans, Seas, Coastal & Inland Waters"),
    ("MISS","CLIMA",    "5",     "Climate, Energy and Mobility",                  "Climate, Energy & Mobility"),
    ("MISS","CANCER",   "1",     "Health",                                        "Health & Life Sciences"),
    ("MISS","SOIL",     "6",     "Food, Bioeconomy, Natural Resources, Agriculture and Environment","Food, Bioeconomy & Environment"),
    ("MISS","CROSS",    "",      "",                                              "Cross-cutting / Other"),
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
    ("MSCA",     None,  "",      "",                                              "Cross-cutting / Other"),
    ("NEB",      None,  "",      "",                                              "Climate-neutral & Smart Cities"),
    ("RAISE",    None,  "4",     "Digital, Industry and Space",                   "Digital, Industry & Space"),
    ("WIDERA",   None,  "",      "",                                              "Cross-cutting / Other"),
    ("CL3","INFRA",     "3",     "Civil Security for Society",                    "Security & Resilience"),
    ("INFRA","TECH",    "4",     "Digital, Industry and Space",                   "Digital, Industry & Space"),
    ("INFRA","SERV",    "4",     "Digital, Industry and Space",                   "Digital, Industry & Space"),
    ("INFRA","DEV",     "",      "",                                              "Cross-cutting / Other"),
    ("INFRA","EOSC",    "",      "",                                              "Cross-cutting / Other"),
    ("INFRA",    None,  "",      "",                                              "Cross-cutting / Other"),
    ("AGRIP",    None,  "6",     "Food, Bioeconomy, Natural Resources, Agriculture and Environment","Food, Bioeconomy & Environment"),
    ("EUAF",     None,  "4",     "Digital, Industry and Space",                   "Digital, Industry & Space"),
    ("DIGITAL",  None,  "4",     "Digital, Industry and Space",                   "Digital, Industry & Space"),
    ("UCPM",     None,  "",      "",                                              "Cross-cutting / Other"),
    ("RFCS",     None,  "5",     "Climate, Energy and Mobility",                  "Climate, Energy & Mobility"),
    ("EUBA",     None,  "",      "",                                              "External Action & International Cooperation"),
    ("PPPA","CHIPS",    "4",     "Digital, Industry and Space",                   "Digital, Industry & Space"),
    ("PPPA","MEDIA",    "",      "",                                              "Culture, Creativity & Inclusion"),
    ("PPPA",     None,  "4",     "Digital, Industry and Space",                   "Digital, Industry & Space"),
    ("RENEWFM",  None,  "5",     "Climate, Energy and Mobility",                  "Climate, Energy & Mobility"),
    ("SOCPL",    None,  "",      "",                                              "Culture, Creativity & Inclusion"),
    ("ERC",      None,  "",      "",                                              "Cross-cutting / Other"),
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

# ── Helpers classificazione (invariati) ───────────────────────────────────────

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

def _topic_id(url: str) -> str:
    s = (url or "").upper().split("?")[0]
    for m in ["/TOPIC-DETAILS/", "/COMPETITIVE-CALLS-CS/"]:
        i = s.find(m)
        if i >= 0:
            return s[i + len(m):]
    return s

def url_classify(url: str):
    tid = _topic_id(url)
    for prefix, subcode, c_num, c_label, thematic in URL_RULES:
        if prefix not in tid:
            continue
        if subcode is not None:
            if subcode not in tid:
                continue
        benef = URL_BENEFICIARY_OVERRIDE.get(prefix, None)
        return c_num, c_label, thematic, benef
    return "", "", "", None

def name_classify(name: str):
    name_up = (name or "").upper()
    for keyword, thematic in NUMERIC_ID_NAME_RULES:
        if keyword.upper() in name_up:
            return thematic
    return ""

def prog_thematic(prog: str) -> str:
    pl = (prog or "").lower()
    for key, label in PROGRAMME_THEMATIC_MAP:
        if key.lower() in pl:
            return label
    return ""

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
    if not s:
        return 0
    s = s.strip()
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
    cleaned = re.sub(r"[^\d,. ]", "", s).strip()
    if re.match(r"^\d{1,3}(\.\d{3})+(,\d+)?$", cleaned):
        cleaned = cleaned.replace(".", "").replace(",", ".")
    elif re.match(r"^\d{1,3}(,\d{3})+(\.\d+)?$", cleaned):
        cleaned = cleaned.replace(",", "")
    else:
        cleaned = cleaned.replace(" ", "").replace(",", ".")
    try:
        return int(float(cleaned))
    except ValueError:
        return 0

def extract_budget_from_text(text: str) -> int:
    candidates = []
    for rx in (RE_BUDGET_INDICATIVE, RE_BUDGET_EXPECTED, RE_BUDGET_LABEL, RE_BUDGET_SUFFIX):
        for m in rx.finditer(text or ""):
            val = parse_budget(m.group(1))
            if 10_000 <= val <= 10_000_000_000:
                candidates.append(val)
    if not candidates:
        return 0
    return max(candidates)

def clean(s):
    if not s:
        return None
    s = re.sub(r"\s+", " ", str(s)).strip()
    return s or None

def pick(rx, text):
    m = rx.search(text or "")
    return clean(m.group(1)) if m else None

# ── NEW v2.1: Sniffing dinamico del path API ───────────────────────────────────

def _sniff_api_path(page) -> str:
    """
    Naviga la home del portale e osserva le richieste XHR per trovare il path
    effettivo dell'API di ricerca. Aggiorna SEARCH_API globalmente.

    Strategia:
      1. Registra un handler su tutte le response del dominio EC
      2. Naviga la pagina lista (prima pagina) con wait=commit (veloce)
      3. Aspetta max 15s osservando quale URL XHR restituisce JSON con "results"
      4. Usa il path trovato; fallback al primo candidato se nessuno trovato
    """
    global SEARCH_API

    detected: dict = {}

    def _detect(response, _d=detected):
        if _d.get("found"):
            return
        url = response.url
        # Filtra solo chiamate EC con JSON
        if "europa.eu" not in url:
            return
        ct = response.headers.get("content-type", "")
        if "json" not in ct:
            return
        # Deve avere un path API-like
        if not any(kw in url for kw in ["/rest/", "/api/", "/search", "/query"]):
            return
        try:
            body = response.json()
            # Controlla se è una risposta di ricerca
            has_results = (
                isinstance(body.get("results"), list) or
                isinstance(body.get("hits"), list) or
                isinstance(body.get("items"), list) or
                isinstance(body.get("data"), list) or
                isinstance(body.get("content"), list)
            )
            has_total = any(
                isinstance(body.get(k), int)
                for k in ("total", "totalCount", "totalResults", "count", "numFound", "totalElements")
            )
            if has_results or has_total:
                _d["found"] = True
                _d["url"]   = url
                # Estrai la parte path dopo il dominio
                m = re.search(r"europa\.eu(/[^?]+)", url)
                if m:
                    _d["path"] = m.group(1).lstrip("/")
                if DEBUG:
                    print(f"  [sniff] API rilevata: {url}")
        except Exception:
            pass

    page.on("response", _detect)

    # Carica la prima pagina lista (commit = appena il server risponde, non aspetta JS)
    try:
        page.goto(
            LIST_URL.format(page=1, ps=PAGE_SIZE),
            wait_until="commit",
            timeout=30000,
        )
    except Exception:
        pass

    # Aspetta max 15s che arrivi la risposta API
    t0 = time.time()
    while time.time() - t0 < 15:
        if detected.get("found"):
            break
        page.wait_for_timeout(500)

    try:
        page.remove_listener("response", _detect)
    except Exception:
        pass

    if detected.get("path"):
        SEARCH_API = detected["path"]
        print(f"✅ API path rilevato: /{SEARCH_API}", flush=True)
    elif detected.get("url"):
        # Usa l'URL completo come stringa di match
        SEARCH_API = detected["url"].split("?")[0].split("europa.eu/")[-1]
        print(f"✅ API path rilevato (URL completo): /{SEARCH_API}", flush=True)
    else:
        print(f"⚠ API path non rilevato automaticamente, uso default: {SEARCH_API}", flush=True)
        if DEBUG:
            print(f"  Candidati testati: {SEARCH_API_CANDIDATES}")

    return SEARCH_API


def _is_search_api_url(url: str) -> bool:
    """Controlla se un URL corrisponde all'API di ricerca attiva."""
    return SEARCH_API in url


# ── Cookie handling (invariato) ───────────────────────────────────────────────

def accept_cookies(page):
    labels = ["Accept all", "Accept All", "Accept", "I accept", "Agree", "OK",
              "Accetta", "Accetto", "Accepter"]
    for label in labels:
        for scope in [page] + list(page.frames):
            try:
                btn = scope.get_by_role("button", name=re.compile(label, re.IGNORECASE))
                if btn.count():
                    btn.first.click(timeout=2000)
                    page.wait_for_timeout(800)
                    return True
            except Exception:
                pass
    try:
        clicked = page.evaluate(r"""() => {
            const labels = /accept|agree|ok|accetta/i;
            function findInShadow(root) {
                const btns = root.querySelectorAll('button, [role="button"], a');
                for (const b of btns) {
                    if (labels.test(b.innerText || b.textContent || '')) {
                        b.click();
                        return true;
                    }
                }
                for (const el of root.querySelectorAll('*')) {
                    if (el.shadowRoot) {
                        if (findInShadow(el.shadowRoot)) return true;
                    }
                }
                return false;
            }
            return findInShadow(document);
        }""")
        if clicked:
            page.wait_for_timeout(800)
            return True
    except Exception:
        pass
    return False

def wait_cookie_gone(page, max_ms=12000):
    t0 = time.time()
    while (time.time() - t0) * 1000 < max_ms:
        try:
            body = page.locator("body").inner_text(timeout=3000)
        except Exception:
            body = ""
        if COOKIE_TEXT.lower() not in (body or "").lower():
            return True
        accept_cookies(page)
        page.wait_for_timeout(600)
    return False

# ── Lettura contatore (invariata) ─────────────────────────────────────────────

_TOTAL_PATTERNS = [
    re.compile(r"(\d[\d,\.]*)\s*items?\s*\(?s?\)?\s*found",          re.IGNORECASE),
    re.compile(r"(\d[\d,\.]*)\s*results?\s*found",                    re.IGNORECASE),
    re.compile(r"(\d[\d,\.]*)\s*opportunit\w+\s*found",               re.IGNORECASE),
    re.compile(r"(\d[\d,\.]*)\s*calls?\s*found",                      re.IGNORECASE),
    re.compile(r"found\s+(\d[\d,\.]*)\s*results?",                    re.IGNORECASE),
    re.compile(r"Total[:\s]+(\d[\d,\.]*)",                            re.IGNORECASE),
    re.compile(r"Showing\s+\d+\s*[–\-]\s*\d+\s+of\s+(\d[\d,\.]*)",  re.IGNORECASE),
    re.compile(r"of\s+(\d[\d,\.]*)\s+results?",                       re.IGNORECASE),
    re.compile(r"(\d[\d,\.]*)\s*open\s*calls?",                       re.IGNORECASE),
    re.compile(r"(\d[\d,\.]*)\s*proposals?\s*found",                  re.IGNORECASE),
    re.compile(r"(\d+)\s*result",                                     re.IGNORECASE),
]

def _parse_count(raw: str) -> int:
    return int(raw.replace(",", "").replace(".", "").replace(" ", ""))

def _try_read_total_from_text(txt: str):
    for pat in _TOTAL_PATTERNS:
        m = pat.search(txt or "")
        if m:
            try:
                val = _parse_count(m.group(1))
                if val > 0:
                    return val, pat.pattern
            except Exception:
                pass
    return None, None

def _read_total_shadow_dom(page) -> int | None:
    try:
        result = page.evaluate(r"""() => {
            const patterns = [
                /(\d[\d,\.]*)\s*items?\s*found/i,
                /(\d[\d,\.]*)\s*results?\s*found/i,
                /found\s+(\d[\d,\.]*)\s*results?/i,
                /(\d[\d,\.]*)\s*calls?\s*found/i,
                /of\s+(\d[\d,\.]*)\s+results?/i,
                /(\d+)\s*result/i,
                /Total[:\s]+(\d[\d,\.]*)/i,
            ];
            function tryText(t) {
                if (!t) return null;
                for (const p of patterns) {
                    const m = p.exec(t);
                    if (m) {
                        const v = parseInt(m[1].replace(/[,. ]/g, ''));
                        if (v > 0) return v;
                    }
                }
                return null;
            }
            function walkShadow(root, depth) {
                if (depth > 12) return null;
                const priority = root.querySelectorAll(
                    '[class*="count"], [class*="total"], [class*="result"], [class*="found"], ' +
                    '[aria-label*="result"], [aria-label*="item"], [aria-label*="found"], ' +
                    'eui-count, eui-total, eui-results, ' +
                    '.results-count, .search-count, .item-count, .total-count, ' +
                    'span[data-testid], p[data-testid]'
                );
                for (const el of priority) {
                    const v = tryText(el.innerText || el.textContent);
                    if (v) return v;
                    if (el.shadowRoot) {
                        const sv = walkShadow(el.shadowRoot, depth + 1);
                        if (sv) return sv;
                    }
                }
                for (const el of root.querySelectorAll('*')) {
                    if (el.shadowRoot) {
                        const sv = walkShadow(el.shadowRoot, depth + 1);
                        if (sv) return sv;
                    }
                }
                return null;
            }
            const bodyText = document.body?.innerText || '';
            const v1 = tryText(bodyText);
            if (v1) return { value: v1, source: 'body_text' };
            const v2 = walkShadow(document, 0);
            if (v2) return { value: v2, source: 'shadow_dom' };
            const allEls = document.querySelectorAll('span, p, div, li, td, h1, h2, h3, h4, label');
            for (const el of allEls) {
                const t = el.innerText || el.textContent || '';
                if (t.length > 5 && t.length < 200) {
                    const v = tryText(t);
                    if (v) return { value: v, source: 'element:' + (el.className || el.tagName) };
                }
            }
            return null;
        }""")
        if result and result.get("value"):
            if DEBUG:
                print(f"  [shadow_dom] trovato {result['value']} via {result['source']}")
            return result["value"]
    except Exception as e:
        if DEBUG:
            print(f"  [shadow_dom] errore JS: {e}")
    return None

def _wait_for_results_to_load(page, timeout_ms=45000):
    selectors_to_try = [
        "eui-search-results",
        "eui-result-item",
        "[class*='result-item']",
        "[class*='search-result']",
        "[class*='call-item']",
        "[class*='opportunity']",
        'a[href*="/topic-details/"]',
        'a[href*="/competitive-calls-cs/"]',
        ".results-list",
        ".search-results",
        "[role='list'] [role='listitem']",
        "text=/\\d+\\s*item/i",
        "text=/\\d+\\s*result/i",
        "text=/\\d+\\s*call/i",
    ]
    for sel in selectors_to_try:
        try:
            page.wait_for_selector(sel, timeout=8000, state="visible")
            if DEBUG:
                print(f"  [load] selettore trovato: {sel}")
            return True
        except Exception:
            pass
    page.wait_for_timeout(3000)
    return False

def read_total(page, timeout_ms=60000) -> int | None:
    print(f"  URL corrente: {page.url}", flush=True)
    _wait_for_results_to_load(page, timeout_ms=min(timeout_ms, 30000))
    start   = time.time()
    attempt = 0

    while (time.time() - start) * 1000 < timeout_ms:
        attempt += 1
        try:
            txt = page.locator("body").inner_text(timeout=5000)
            val, pat = _try_read_total_from_text(txt)
            if val:
                print(f"  ✓ Contatore trovato (body text, pattern '{pat}'): {val}", flush=True)
                return val
        except Exception:
            txt = ""

        val = _read_total_shadow_dom(page)
        if val:
            print(f"  ✓ Contatore trovato (shadow DOM): {val}", flush=True)
            return val

        try:
            html = page.content()
            for attr_pat in [
                re.compile(r'data-(?:total|count|results?)["\s]*[=:]["\s]*(\d+)', re.IGNORECASE),
                re.compile(r'(?:total|count|results?)["\s]*:["\s]*(\d+)', re.IGNORECASE),
            ]:
                for m in attr_pat.finditer(html):
                    try:
                        v = int(m.group(1))
                        if 10 < v < 100000:
                            print(f"  ✓ Contatore trovato (HTML attr): {v}", flush=True)
                            return v
                    except Exception:
                        pass
        except Exception:
            pass

        if attempt % 3 == 0:
            print(f"  [tentativo {attempt}] in attesa del contatore…", flush=True)
            if DEBUG and txt:
                number_contexts = re.findall(r'.{0,40}\d+.{0,40}', txt)
                print(f"  Contesti numerici trovati ({len(number_contexts)}):")
                for ctx in number_contexts[:15]:
                    print(f"    › {ctx.strip()}")

        try:
            page.mouse.wheel(0, 300)
        except Exception:
            pass
        page.wait_for_timeout(1500)

    link_count = page.locator(LINK_SELECTOR).count() if LINK_SELECTOR else 0
    if link_count > 0:
        print(f"  ⚠ Contatore non trovato, uso {link_count} link come stima minima", flush=True)
        return link_count

    print("  ✗ Impossibile trovare il contatore. Diagnostica:", flush=True)
    try:
        txt = page.locator("body").inner_text(timeout=5000)
        print(f"  URL: {page.url}")
        print(f"  Testo body (primi 3000 char):\n{txt[:3000]}", flush=True)
    except Exception as e:
        print(f"  Errore lettura body: {e}", flush=True)
    return None

# ── NEW v2.1: Intercettatore XHR migliorato ───────────────────────────────────

def attach_link_interceptor(page) -> dict:
    """
    Intercetta TUTTE le risposte JSON del dominio EC (non solo SEARCH_API),
    le filtra per struttura, ed estrae link + totale.

    Fonte primaria per la raccolta link — completamente indipendente dal DOM.
    """
    # links       : lista cumulativa di tutti gli URL raccolti
    # total       : numero totale di call secondo l'API
    # seen        : set URL normalizzati già aggiunti a links (dedup interno XHR)
    # api_urls    : path API effettivamente usati (diagnostica)
    # _cursor     : prossimo indice da leggere in links (usato da _harvest_xhr_links)
    # _page_sizes : quante call sono arrivate per ogni risposta XHR (diagnostica race)
    captured: dict = {
        "links": [], "total": None, "seen": set(),
        "api_urls": set(), "_cursor": 0, "_page_sizes": [],
    }

    def handle(response, _c=captured):
        url = response.url
        # Filtra: solo dominio EC, solo JSON
        if "europa.eu" not in url:
            return
        if response.status != 200:
            return
        ct = response.headers.get("content-type", "")
        if "json" not in ct and "javascript" not in ct:
            return
        # Deve sembrare una chiamata API (path con /rest/, /api/, /search, ecc.)
        if not any(kw in url for kw in ["/rest/", "/api/", "/search", "/query", "search-api"]):
            return

        try:
            body = response.json()
        except Exception:
            return

        # ── Leggi totale ──
        if _c["total"] is None:
            for key in ("total", "totalCount", "totalResults", "count", "numFound",
                        "hits", "totalElements", "resultCount"):
                v = body.get(key)
                if isinstance(v, int) and v > 0:
                    _c["total"] = v
                    _c["api_urls"].add(url.split("?")[0])
                    if DEBUG:
                        print(f"  [XHR total] {v} da key '{key}' in {url}")
                    break

        # ── Estrai link dai risultati ──
        results = (
            body.get("results") or
            body.get("hits") or
            body.get("items") or
            body.get("data") or
            body.get("content") or
            []
        )
        if not isinstance(results, list):
            return

        added_this_response = 0
        for item in results:
            if not isinstance(item, dict):
                continue

            meta = item.get("metadata") or item.get("_source") or item.get("fields") or item

            call_url = _extract_url_from_meta(meta)
            if not call_url:
                continue
            # Normalizza URL per dedup: lowercase path, no trailing slash, no fragment
            norm = _normalize_url(call_url)
            if norm not in _c["seen"]:
                _c["seen"].add(norm)
                _c["links"].append(call_url)   # conserva l'URL originale per navigazione
                added_this_response += 1
                if DEBUG:
                    print(f"  [XHR link] {call_url}")

        if added_this_response > 0:
            _c["_page_sizes"].append(added_this_response)

    page.on("response", handle)
    return captured


def _normalize_url(url: str) -> str:
    """
    Normalizza un URL per il confronto di deduplicazione:
    - lowercase del path (gli ID delle call sono case-insensitive sul portale EU)
    - rimuove trailing slash
    - rimuove fragment (#...)
    - preserva query string (pageNumber ecc. non presenti nei link di dettaglio)
    """
    url = (url or "").strip().rstrip("/").split("#")[0]
    # Separa schema+host dal path: lowercasa solo il path
    if "://" in url:
        proto, rest = url.split("://", 1)
        if "/" in rest:
            host, path = rest.split("/", 1)
            return f"{proto}://{host.lower()}/{path.lower()}"
        return f"{proto}://{rest.lower()}"
    return url.lower()


def _extract_url_from_meta(meta: dict) -> str | None:
    """
    Estrae un URL di dettaglio da un oggetto metadati della search API.
    Prova una lista estesa di campi, sia URL diretti che identificatori
    da cui ricostruire l'URL.
    """
    if not isinstance(meta, dict):
        return None

    # ── 1. Campi che contengono URL diretti ──
    direct_url_keys = [
        "url", "link", "detailUrl", "href", "landingPageUrl",
        "canonicalUrl", "detailsUrl", "pageUrl", "callUrl",
        "topicUrl", "opportunityUrl", "viewUrl",
        # Struttura _links HAL-style
        "_links",
    ]
    for key in direct_url_keys:
        v = meta.get(key)
        if isinstance(v, dict):
            # HAL _links: { self: { href: "..." }, detail: { href: "..." } }
            for sub_key in ("detail", "self", "canonical", "alternate"):
                sub = v.get(sub_key)
                if isinstance(sub, dict):
                    href = sub.get("href")
                    if href and isinstance(href, str) and href.startswith("http"):
                        return href
                if isinstance(sub, str) and sub.startswith("http"):
                    return sub
        if isinstance(v, list) and v:
            v = v[0]
        if v and isinstance(v, str):
            if v.startswith("http"):
                return v
            # Path relativo EC
            if v.startswith("/info/funding") or v.startswith("/opportunities"):
                return "https://ec.europa.eu" + v

    # ── 2. Campi che contengono identificatori ──
    id_keys = [
        "identifier", "callIdentifier", "topicIdentifier",
        "cftIdentifier", "topicId", "id", "callId",
        "topicProgrammeMap", "name", "reference",
    ]
    for key in id_keys:
        v = meta.get(key)
        if isinstance(v, list):
            v = v[0] if v else None
        if v and isinstance(v, str):
            built = _build_url_from_id(v.strip())
            if built:
                return built

    return None


def _build_url_from_id(identifier: str) -> str | None:
    """
    Dato un identificatore, restituisce l'URL di dettaglio sul portale EU.
    Copre tutti i pattern storici + nuovi pattern post-update.
    """
    i = identifier.upper()
    base = "https://ec.europa.eu/info/funding-tenders/opportunities/portal/screen/opportunities"

    horizon_prefixes = (
        "HORIZON-", "HLTH-", "MSCA-", "ERC-", "EIC-", "EIT-", "MISS-",
        "DIGITAL-", "EURATOM-", "EUROHPC-", "NEB-", "WIDERA-", "RAISE-",
        "RFCS-", "AGRIP-", "UCPM-", "PPPA-", "RENEWFM-", "SOCPL-", "JUST-",
        "EMFAF-", "EUBA-", "I3-", "JU-", "CID-","CREA-", "ERASMUS-", "EDF-", "LIFE-", "SMP-", "CERV-", "CEF-",
    "ISF-", "ESF-", "EUAF-", "PERICLES-", "INNOVFUND-", "JTM-",
    )
    if any(i.startswith(p) for p in horizon_prefixes):
        return f"{base}/topic-details/{identifier}"

    if i.startswith("CFT-") or i.startswith("COMP-"):
        return f"{base}/competitive-calls-cs/{identifier}"

    if i.startswith("PROSPECT-") or i.startswith("EOI-"):
        return f"{base}/prospect-details/{identifier}"

    # Numerico puro — non abbastanza informazioni
    if re.match(r"^\d+$", identifier):
        return None

    # Fallback generico: prova topic-details
    if len(identifier) > 5 and "-" in identifier:
        return f"{base}/topic-details/{identifier}"

    return None

# ── Selettore DOM (migliorato v2.1) ───────────────────────────────────────────

def _discover_link_selector(page) -> str:
    """
    Discovery automatico del selettore CSS per i link alle call.
    v2.1: analizza anche attributi routerLink, data-href, e link relativi EC.
    """
    global LINK_SELECTOR

    try:
        all_hrefs: list[str] = page.evaluate("""
            () => {
                const links = Array.from(document.querySelectorAll('a'));
                return links.map(a => ({
                    href: a.getAttribute('href') || '',
                    rl:   a.getAttribute('routerLink') || a.getAttribute('routerlink') || '',
                    text: (a.innerText || a.textContent || '').trim().substring(0, 80),
                })).filter(x => x.href.length > 5 || x.rl.length > 5);
            }
        """)
    except Exception:
        return LINK_SELECTOR or ""

    if not all_hrefs:
        if DEBUG:
            print("  [discovery] nessun link trovato nel DOM")
        return LINK_SELECTOR or ""

    hrefs = [x.get("href", "") for x in all_hrefs]
    rls   = [x.get("rl", "")   for x in all_hrefs]

    # ── Conta match per ogni pattern ──
    pattern_counts: dict[int, int] = {}
    for idx, pat in enumerate(HREF_CALL_PATTERNS):
        pattern_counts[idx] = sum(
            1 for h in hrefs + rls if h and pat.search(h)
        )

    best_idx   = max(pattern_counts, key=lambda k: pattern_counts[k])
    best_count = pattern_counts[best_idx]

    if best_count == 0:
        print("  ⚠ Discovery DOM: nessun pattern di call trovato.", flush=True)
        if DEBUG:
            print(f"  Campione href: {hrefs[:20]}")
            print(f"  Campione routerLink: {[r for r in rls if r][:20]}")
        # Ultimo tentativo: link con /portal/screen/ nel path
        screen_links = [h for h in hrefs if "/portal/screen/" in h and len(h) > 30]
        if screen_links:
            LINK_SELECTOR = 'a[href*="/portal/screen/"]'
            print(f"  ✓ Selettore generico fallback: '{LINK_SELECTOR}' ({len(screen_links)} link)", flush=True)
            return LINK_SELECTOR
        return LINK_SELECTOR or ""

    matching = [h for h in hrefs + rls if h and HREF_CALL_PATTERNS[best_idx].search(h)]
    m        = HREF_CALL_PATTERNS[best_idx].search(matching[0])
    if m:
        fragment = m.group(0).rstrip("/").rstrip("=")
        if "=" in fragment:
            sel = f'a[href*="{fragment}"]'
        else:
            sel = f'a[href*="{fragment}/"], a[href*="{fragment}?"]'

        print(f"  🔍 Discovery DOM selettore: '{sel}' ({best_count} link trovati)", flush=True)
        LINK_SELECTOR = sel
        return sel

    return LINK_SELECTOR or ""


def _set_link_selector_from_candidates(page) -> str:
    """Prova i selettori candidati in ordine e usa il primo che trova link."""
    global LINK_SELECTOR

    for sel in LINK_SELECTORS_CANDIDATES:
        try:
            count = page.locator(sel).count()
            if count > 0:
                print(f"  ✓ Selettore DOM attivo: '{sel}' ({count} link)", flush=True)
                LINK_SELECTOR = sel
                return sel
        except Exception:
            pass

    print("  ⚠ Nessun selettore candidato funziona, avvio discovery…", flush=True)
    return _discover_link_selector(page)

# ── Scroll e link (invariati) ─────────────────────────────────────────────────

def count_links(page) -> int:
    if not LINK_SELECTOR:
        return 0
    try:
        return page.locator(LINK_SELECTOR).count()
    except Exception:
        return 0


def scroll_until(page, expected: int, max_ms: int = 60000) -> int:
    start        = time.time()
    last         = -1
    stable_since = time.time()
    selector_checked = False

    while count_links(page) == 0 and (time.time() - start) * 1000 < 12000:
        accept_cookies(page)
        wait_cookie_gone(page, 3000)
        page.wait_for_timeout(700)

    if count_links(page) == 0 and not selector_checked:
        _set_link_selector_from_candidates(page)
        selector_checked = True

    container = None
    if LINK_SELECTOR:
        try:
            container = page.evaluate_handle(f"""() => {{
                const sel = `{LINK_SELECTOR}`;
                const links = document.querySelectorAll(sel);
                if (!links.length) return null;
                let el = links[0];
                for (let i = 0; i < 20; i++) {{
                    if (!el) break;
                    const st = window.getComputedStyle(el);
                    const oy = st.overflowY;
                    if ((oy === 'auto' || oy === 'scroll') && el.scrollHeight > el.clientHeight + 5) return el;
                    el = el.parentElement;
                }}
                return null;
            }}""")
        except Exception:
            container = None

    while (time.time() - start) * 1000 < max_ms:
        accept_cookies(page)
        c = count_links(page)

        if c >= expected:
            return c

        if c != last:
            last = c
            stable_since = time.time()

        try:
            if container:
                page.evaluate("(el) => { el.scrollTop += el.clientHeight * 0.85; }", container)
            else:
                page.mouse.wheel(0, 1800)
        except Exception:
            try:
                page.mouse.wheel(0, 1800)
            except Exception:
                pass

        page.wait_for_timeout(700)

        if time.time() - stable_since > 6:
            try:
                if container:
                    page.evaluate("(el) => { el.scrollTop = el.scrollHeight; }", container)
                else:
                    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            except Exception:
                pass
            if c == 0 and not selector_checked:
                _set_link_selector_from_candidates(page)
                selector_checked = True
            page.wait_for_timeout(1000)
            stable_since = time.time()

    return count_links(page)


def extract_links(page) -> list[str]:
    """Estrae link dal DOM usando il selettore attivo."""
    if not LINK_SELECTOR:
        return []
    try:
        hrefs = page.evaluate(f"""
            () => Array.from(document.querySelectorAll('{LINK_SELECTOR}'))
                      .map(a => a.getAttribute('href'))
        """)
    except Exception:
        return []

    out, seen = [], set()
    for h in hrefs or []:
        if not h:
            continue
        full = "https://ec.europa.eu" + h if h.startswith("/") else h
        if full not in seen:
            seen.add(full)
            out.append(full)
    return out

# ── Parsing card (invariato) ──────────────────────────────────────────────────

def parse_card(page, full_url: str) -> dict:
    path = full_url.replace("https://ec.europa.eu", "").split("?")[0]
    try:
        a = page.locator(f'a[href*="{path}"]').first
    except Exception:
        a = None
    title = clean(a.inner_text()) if (a and a.count()) else path.split("/")[-1]

    try:
        card = a.locator(
            "xpath=ancestor::*[contains(.,'Programme:') or contains(.,'Opening date:') or "
            "contains(.,'Deadline date:') or contains(.,'Type of action:')][1]"
        ).first if a and a.count() else None
        text = (card.inner_text() if (card and card.count())
                else (a.locator("xpath=ancestor::*[1]").inner_text() if (a and a.count()) else ""))
    except Exception:
        text = ""

    dead     = pick(RE_DEAD, text) or pick(RE_NEXT_DEAD, text)
    call_id  = pick(RE_CALL_ID, full_url) or pick(RE_CALL_ID, text)
    cluster_raw = pick(RE_CLUSTER, text) or pick(RE_CLUSTER, full_url) or pick(RE_CLUSTER, call_id or "")

    return {
        "name":          title,
        "call_id":       call_id,
        "programme_raw": pick(RE_PROG, text),
        "action_raw":    pick(RE_ACTION, text),
        "cluster_raw":   cluster_raw,
        "opening_raw":   pick(RE_OPEN, text),
        "deadline_raw":  dead,
        "url":           full_url,
    }

# ── Arricchimento (invariato) ─────────────────────────────────────────────────

def _first(meta, *keys):
    for k in keys:
        v = meta.get(k)
        if isinstance(v, list) and v:
            return re.sub(r"\s+", " ", str(v[0])).strip()
        if v and isinstance(v, str):
            return v.strip()
    return ""

def extract_budget_per_project_dom(page, topic_id: str):
    parts = topic_id.split("?")[0].split("-")
    target_match = "-".join(parts[-2:]) if len(parts) > 1 else parts[-1]
    try:
        btn = page.locator("button:has-text('Topic conditions and documents')").first
        if btn.count() > 0:
            btn.scroll_into_view_if_needed()
            if btn.get_attribute("aria-expanded") == "false":
                btn.click(force=True)
                page.wait_for_timeout(3500)

        row_locator = page.locator(f"tr:has-text('{target_match}')").first
        if row_locator.count() > 0:
            row_locator.scroll_into_view_if_needed()
            page.wait_for_timeout(1000)

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
    except Exception:
        return None

def _enrich_one(page, row: dict) -> bool:
    url      = row["url"]
    topic_id = url.split("/")[-1].split("?")[0]
    captured = {}

    def handle(response, _c=captured):
        if not _is_search_api_url(response.url) or response.status != 200:
            return
        try:
            body = response.json()
            for item in body.get("results", [body]):
                meta    = item.get("metadata", {}) or {}
                prog_id = _first(meta, "frameworkProgramme", "programme")
                action  = _first(meta, "typesOfAction", "typeOfAction", "fundingScheme")
                cid     = _first(meta, "callIdentifier", "identifier")

                if prog_id and not _c.get("prog"):
                    _c["prog"] = PROGRAMME_MAP.get(prog_id, prog_id)
                if action and not _c.get("action"):
                    _c["action"] = action
                if cid and not _c.get("call_id"):
                    _c["call_id"] = cid

                if not _c.get("budget"):
                    for key in (
                        "budgetOverviewTotal", "totalBudget", "budget",
                        "budgetTopicActions", "indicativeBudget",
                        "availableBudget", "estimatedTotalContribution",
                    ):
                        raw = meta.get(key)
                        if isinstance(raw, list):
                            raw = raw[0] if raw else None
                        if raw is not None:
                            val = parse_budget(str(raw))
                            if val > 0:
                                _c["budget"] = val
                                break
        except Exception:
            pass

    page.on("response", handle)
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=40000)
        try:
            page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass
        page.wait_for_timeout(2000)

        try:
            body_text = page.locator("body").inner_text(timeout=5000)
        except Exception:
            body_text = ""
        row["full_text"] = clean(body_text) or ""

        budget_val_dom = extract_budget_per_project_dom(page, topic_id)
        if budget_val_dom:
            row["budget_raw"] = budget_val_dom
        elif captured.get("budget"):
            row["budget_raw"] = captured["budget"]
        elif body_text:
            val_reg = extract_budget_from_text(body_text)
            if val_reg > 0:
                row["budget_raw"] = val_reg

    except Exception as e:
        print(f"    [ERR goto] {e}", flush=True)
    finally:
        try:
            page.remove_listener("response", handle)
        except Exception:
            pass

    if captured.get("prog") and not row.get("programme_raw"):
        row["programme_raw"] = captured["prog"]
    if captured.get("action") and not row.get("action_raw"):
        row["action_raw"] = captured["action"]
    if captured.get("call_id") and not row.get("call_id"):
        row["call_id"] = captured["call_id"]

    return bool(captured) or bool(row.get("full_text"))

def enrich(ctx, rows: list):
    to_fix = [
        r for r in rows
        if (not r.get("programme_raw") or not r.get("action_raw") or not r.get("call_id"))
        and r.get("url")
    ]
    if not to_fix:
        print("  Tutti i campi già presenti ✓", flush=True)
        return

    print(f"  {len(to_fix)} call da arricchire…", flush=True)
    page    = ctx.new_page()
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
            print(f"  [checkpoint] {idx} call elaborate…", flush=True)

        time.sleep(0.3)

    try:
        page.close()
    except Exception:
        pass
    print(f"  Arricchimento completato. Saltate: {skipped}/{len(to_fix)}", flush=True)

# ── to_call (invariato) ───────────────────────────────────────────────────────

def to_call(row: dict) -> dict:
    url        = row.get("url", "")
    prog_raw   = row.get("programme_raw") or ""
    call_id    = row.get("call_id") or ""
    action_raw = row.get("action_raw") or ""

    cluster_num = ""
    for src in [call_id, row.get("cluster_raw", ""), url]:
        m = RE_CLUSTER.search(src or "")
        if m:
            cluster_num = m.group(1)
            break

    u_cnum, u_clabel, u_thematic, u_benef = url_classify(url)
    if u_cnum:
        cluster_num = u_cnum

    cluster_label = u_clabel or THEMATIC_MAP.get(cluster_num, "")
    thematic      = u_thematic or resolve_thematic(cluster_num, prog_raw) or name_classify(row.get("name", ""))
    action        = normalize_action(action_raw)
    is_mission    = bool("/HORIZON-MISS" in url.upper())

    full_text = row.get("full_text") or ""
    multi = classify_multitopic(row.get("name") or "", full_text, thematic)

    return {
        "name":             row.get("name") or "",
        "call_id":          call_id,
        "programme":        prog_raw,
        "cluster_num":      cluster_num,
        "cluster_label":    cluster_label,
        "thematic_cluster": thematic,
        "action":           action,
        "opening":          row.get("opening_raw") or "",
        "opening_iso":      parse_date_iso(row.get("opening_raw") or ""),
        "deadline":         row.get("deadline_raw") or "",
        "deadline_iso":     parse_date_iso(row.get("deadline_raw") or ""),
        "url":              url,
        "is_mission":       is_mission,
        "beneficiary_hint": beneficiary_hint(action, prog_raw, u_benef),
        "budget":           row.get("budget_raw") or 0,
        "full_text":        multi["full_text"],
        "keyword_hits":     multi["keyword_hits"],
        "multi_thematic":   multi["multi_thematic"],
        "is_special_basic_research": multi["is_special_basic_research"],
    }

# ── Changelog (invariato) ─────────────────────────────────────────────────────

def write_changelog(old_calls: list, new_calls: list, changelog_path: Path, generated: str):
    old_by_url = {c["url"]: c for c in old_calls}
    new_by_url = {c["url"]: c for c in new_calls}
    old_urls   = set(old_by_url)
    new_urls   = set(new_by_url)
    added      = [new_by_url[u] for u in sorted(new_urls - old_urls)]
    removed    = [old_by_url[u] for u in sorted(old_urls - new_urls)]

    def thematic_counts(calls):
        tc = {}
        for c in calls:
            k = c.get("thematic_cluster") or "(non classificato)"
            tc[k] = tc.get(k, 0) + 1
        return tc

    date_str = generated[:10]
    lines    = []
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
        by_thematic: dict[str, list] = {}
        for c in added:
            t = c.get("thematic_cluster") or "(non classificato)"
            by_thematic.setdefault(t, []).append(c)
        for thematic, calls in sorted(by_thematic.items()):
            lines.append(f"### {thematic} ({len(calls)})")
            lines.append(f"")
            for c in calls:
                name   = c.get("name") or "(senza nome)"
                prog   = c.get("programme") or ""
                action = c.get("action") or ""
                dead   = c.get("deadline") or ""
                url    = c.get("url") or ""
                meta   = " · ".join(filter(None, [prog, action, f"Scadenza: {dead}" if dead else ""]))
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
    history_line = f"| {date_str} | {len(new_calls)} | +{len(added)} | -{len(removed)} |"
    if history_path.exists():
        hist = history_path.read_text(encoding="utf-8")
        if history_line not in hist:
            history_path.write_text(hist.rstrip() + "\n" + history_line + "\n", encoding="utf-8")
    else:
        header = (
            "# Storico aggiornamenti calls.json\n\n"
            "| Data | Call totali | Aggiunte | Rimosse |\n"
            "|---|---|---|---|\n"
            + history_line + "\n"
        )
        history_path.write_text(header, encoding="utf-8")
    print(f"📋 History aggiornata: {history_path}")

# ── Navigazione robusta (aggiornata v2.1) ─────────────────────────────────────

def navigate_to_list(page, url: str, max_attempts: int = 3) -> bool:
    for attempt in range(1, max_attempts + 1):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=90000)
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass
            page.wait_for_timeout(2000)

            accept_cookies(page)
            wait_cookie_gone(page, max_ms=8000)

            current = page.url
            if "calls-for-proposals" in current or "opportunities" in current:
                return True
            if "login" in current.lower() or "error" in current.lower():
                print(f"  [navigate] redirect inatteso: {current}", flush=True)
                if attempt < max_attempts:
                    page.go_back(timeout=10000)
                    page.wait_for_timeout(2000)
                    continue

            try:
                txt = page.locator("body").inner_text(timeout=5000)
                if len(txt) > 500:
                    return True
            except Exception:
                pass

        except PWTimeout:
            print(f"  [navigate] timeout (tentativo {attempt}/{max_attempts})", flush=True)
        except Exception as e:
            print(f"  [navigate] errore: {e} (tentativo {attempt}/{max_attempts})", flush=True)

        if attempt < max_attempts:
            page.wait_for_timeout(3000 * attempt)

    return False

# ── Main (aggiornato v2.1) ────────────────────────────────────────────────────

def main(out_path: Path, debug: bool = False):
    global DEBUG
    DEBUG = debug

    rows: list       = []
    # seen_urls contiene URL *normalizzati* (_normalize_url) per dedup coerente
    # tra XHR e DOM fallback, indipendente da maiuscole/minuscole e trailing slash
    seen_urls: set   = set()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=[
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled",
        ])
        ctx = browser.new_context(
            locale="en-US",
            viewport={"width": 1920, "height": 1080},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
        )
        ctx.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        """)

        page = ctx.new_page()

        # ══ FASE 0: sniffing API path (NUOVO v2.1) ══════════════════════════
        print(f"\n══ Fase 0: sniffing API path ══", flush=True)
        _sniff_api_path(page)

        # ══ FASE 1: navigazione prima pagina ════════════════════════════════
        # Attacca l'interceptor XHR — rimane attivo per tutta la sessione
        xhr = attach_link_interceptor(page)

        first_url = LIST_URL.format(page=1, ps=PAGE_SIZE)
        print(f"\n══ Fase 1: navigazione prima pagina ══", flush=True)
        ok = navigate_to_list(page, first_url)
        if not ok:
            print("⚠ navigate_to_list ha restituito False, provo comunque…", flush=True)

        # Attendi risposte XHR
        page.wait_for_timeout(3000)

        # Totale: prima dall'XHR, poi dal DOM
        total: int | None = None
        if xhr.get("total"):
            total = xhr["total"]
            print(f"✅ Totale da XHR: {total}", flush=True)
        else:
            total = read_total(page)

        if total is None:
            if xhr["links"]:
                total = len(xhr["links"])
                print(f"⚠ Totale stimato da link XHR: {total}", flush=True)
            else:
                print("❌ Impossibile determinare il numero di call. Uscita.", flush=True)
                browser.close()
                return

        max_pages = math.ceil(total / PAGE_SIZE)
        print(f"✅ Totale: {total} call | pagine attese: {max_pages}", flush=True)

        # Scopri selettore DOM sulla prima pagina (usato come fallback)
        _set_link_selector_from_candidates(page)

        def _harvest_xhr_links(xhr_captured: dict, row_list: list, pg) -> int:
            """
            Legge i link in xhr_captured["links"] a partire dal cursore,
            li aggiunge a row_list e avanza il cursore.
            Usa seen_urls (closure) per dedup globale normalizzata.
            """
            cursor    = xhr_captured["_cursor"]
            new_links = xhr_captured["links"][cursor:]
            xhr_captured["_cursor"] = len(xhr_captured["links"])

            added = 0
            for u in new_links:
                norm = _normalize_url(u)
                if norm not in seen_urls:
                    seen_urls.add(norm)
                    row_list.append(parse_card(pg, u))
                    added += 1
            return added

        def _wait_for_xhr_page(xhr_captured: dict, expected: int,
                               timeout_s: float = 12.0) -> int:
            """
            Attende attivamente che l'XHR abbia portato almeno `expected` link
            NUOVI rispetto al cursore corrente. Restituisce quanti ne sono arrivati.
            Evita race condition tra navigate_to_list e la risposta API.
            """
            cursor = xhr_captured["_cursor"]
            t0     = time.time()
            while time.time() - t0 < timeout_s:
                available = len(xhr_captured["links"]) - cursor
                if available >= expected:
                    return available
                page.wait_for_timeout(300)
            return len(xhr_captured["links"]) - cursor

        # ── Prima pagina ──────────────────────────────────────────────────────
        # Attesa attiva: vogliamo almeno min(PAGE_SIZE, total) link XHR
        expected_p1 = min(PAGE_SIZE, total)
        arrived     = _wait_for_xhr_page(xhr, expected_p1, timeout_s=12.0)
        new_from_xhr = _harvest_xhr_links(xhr, rows, page)
        print(f"  Prima pagina XHR: {new_from_xhr} link (attesi {expected_p1}, arrivati {arrived})"
              f" | DOM: {count_links(page)} link", flush=True)

        if new_from_xhr < expected_p1:
            scroll_until(page, expected=expected_p1)
            dom_links = extract_links(page)
            new_links = [u for u in dom_links if _normalize_url(u) not in seen_urls]
            for u in new_links:
                seen_urls.add(_normalize_url(u))
                rows.append(parse_card(page, u))
            print(f"  Prima pagina DOM fallback: {len(new_links)} link", flush=True)

        # ══ FASE 2: scraping pagine 2..N ════════════════════════════════════
        for pnum in range(2, max_pages + 1):
            remaining = total - (pnum - 1) * PAGE_SIZE
            expected  = min(PAGE_SIZE, max(remaining, 0))
            url       = LIST_URL.format(page=pnum, ps=PAGE_SIZE)

            print(f"\n[p{pnum}/{max_pages}] attese ~{expected} call", end="", flush=True)

            ok = navigate_to_list(page, url)
            if not ok:
                print(f" ⚠ navigazione incerta, continuo…", flush=True)

            # Attesa attiva XHR: aspetta che arrivino i link di questa pagina
            arrived = _wait_for_xhr_page(xhr, expected, timeout_s=15.0)

            new_from_xhr = _harvest_xhr_links(xhr, rows, page)

            # Fallback DOM solo se XHR non ha portato NULLA per questa pagina
            new_from_dom = 0
            if new_from_xhr < expected:
                scroll_until(page, expected=expected)
                dom_links = extract_links(page)
                new_dom = [u for u in dom_links if _normalize_url(u) not in seen_urls]
                
                for u in new_dom:
                    seen_urls.add(_normalize_url(u))
                    rows.append(parse_card(page, u))

                new_from_dom = len(new_dom)

            total_new = new_from_xhr + new_from_dom
            src_label = f"XHR:{new_from_xhr}" + (f" DOM:{new_from_dom}" if new_from_dom else "")
            # Avvisa se siamo short rispetto all'atteso (utile per debug)
            gap = expected - total_new
            gap_str = f" ⚠ MANCANTI:{gap}" if gap > 2 else ""
            print(f" → {total_new} nuovi ({src_label}) | tot seen: {len(seen_urls)}{gap_str}", flush=True)

            time.sleep(0.15)

        # ══ FASE 3: arricchimento ════════════════════════════════════════════
        print(f"\n══ Fase 3: arricchimento {len(rows)} call ══", flush=True)
        enrich(ctx, rows)
        browser.close()

    # ── Classificazione ────────────────────────────────────────────────────────
    calls: list[dict] = []
    seen_final: set = set()
    for row in rows:
        call = to_call(row)
        norm = _normalize_url(call["url"])
        if call["url"] and norm not in seen_final:
            seen_final.add(norm)
            calls.append(call)

    tc: dict = {}
    for c in calls:
        k = c["thematic_cluster"] or "(non classificato)"
        tc[k] = tc.get(k, 0) + 1
    print(f"\nClassificazione ({len(calls)} call totali):")
    for k, v in sorted(tc.items(), key=lambda x: -x[1]):
        print(f"  {v:5d}  {k}")
    print(f"\nNon classificati: {tc.get('(non classificato)', 0)}")

    generated = datetime.now(timezone.utc).isoformat()

    old_calls: list = []
    if out_path.exists():
        try:
            old_data  = json.loads(out_path.read_text(encoding="utf-8"))
            old_calls = old_data.get("calls", [])
            print(f"\nDataset precedente: {len(old_calls)} call")
        except Exception:
            print("\nNessun dataset precedente trovato.")

    changelog_path = out_path.parent / "changelog.md"
    write_changelog(old_calls, calls, changelog_path, generated)

    payload = {"generated": generated, "calls": calls}
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n✅ Scritto {out_path} con {len(calls)} call")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="EU Funding & Tenders scraper → calls.json")
    parser.add_argument("--out",   default="calls.json", help="Percorso output JSON")
    parser.add_argument("--debug", action="store_true",  help="Diagnostica estesa")
    args = parser.parse_args()
    main(Path(args.out), debug=args.debug)


































