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

# ── Parametri ─────────────────────────────────────────────────────────────────

PAGE_SIZE = 50

LIST_URL = (
    "https://ec.europa.eu/info/funding-tenders/opportunities/portal/screen"
    "/opportunities/calls-for-proposals"
    "?order=DESC&pageNumber={page}&pageSize={ps}&sortBy=startDate"
    "&isExactMatch=true&status=31094501,31094502&programmePeriod=2021%20-%202027"
)

SEARCH_API  = "search-api/prod/rest/search"
COOKIE_TEXT = "This site uses cookies"

LINK_SELECTOR = (
    'a[href*="/topic-details/"], '
    'a[href*="/competitive-calls-cs/"], '
    'a[href*="/prospect-details/"]'
)

RE_TOTAL     = re.compile(r"(\d+)\s*item\s*\(s\)\s*found", re.IGNORECASE)
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

# (prefix, subcode_or_None, cluster_num, cluster_label, thematic)
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

# ── Classificazione ───────────────────────────────────────────────────────────

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
    """
    Attende e legge il numero totale di risultati dalla pagina.
    Prova più pattern per coprire eventuali variazioni del portale EU.
    """
    PATTERNS = [
        re.compile(r"(\d[\d,\.]*)\s*item\s*\(?s\)?\s*found",      re.IGNORECASE),
        re.compile(r"(\d[\d,\.]*)\s*results?\s*found",             re.IGNORECASE),
        re.compile(r"(\d[\d,\.]*)\s*opportunit\w+\s*found",        re.IGNORECASE),
        re.compile(r"(\d[\d,\.]*)\s*calls?\s*found",               re.IGNORECASE),
        re.compile(r"found\s+(\d[\d,\.]*)\s*results?",             re.IGNORECASE),
        re.compile(r"Total[:\s]+(\d[\d,\.]*)",                     re.IGNORECASE),
        re.compile(r"(\d[\d,\.]*)\s*result",                       re.IGNORECASE),  # fallback largo
    ]

    start = time.time()
    while (time.time() - start) * 1000 < timeout_ms:
        try:
            txt = page.locator("body").inner_text()
        except Exception:
            txt = ""

        for pat in PATTERNS:
            m = pat.search(txt or "")
            if m:
                raw = m.group(1).replace(",", "").replace(".", "")
                print(f" Contatore trovato con pattern '{pat.pattern}': {raw}")
                return int(raw)

        page.wait_for_timeout(1000)

    # Debug: stampa le prime 2000 caratteri del body
    try:
        snippet = page.locator("body").inner_text()[:2000]
        print(f" Testo body (primi 2000 char):\n{snippet}")
    except Exception as e:
        print(f"Impossibile leggere il body: {e}")

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
                    action  = _first(meta, "typesOfAction","typeOfAction","fundingScheme")
                    cid     = _first(meta, "callIdentifier","identifier")
                    
                    if prog_id and not _c.get("prog"):
                        _c["prog"] = PROGRAMME_MAP.get(prog_id, prog_id)
                    if action and not _c.get("action"):
                        _c["action"] = action
                    if cid and not _c.get("call_id"):
                        _c["call_id"] = cid

                    # Budget da XHR (mantenuto come primo tentativo/backup)
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
            except Exception:
                pass

    page.on("response", handle)
    try:
        # 1. Navigazione
        page.goto(url, wait_until="domcontentloaded", timeout=40000)
        page.wait_for_timeout(2500)
        
        # 2. Estrazione testo completo (fondamentale per le tue 13 aree tematiche)
        try:
            body_text = page.locator("body").inner_text(timeout=5000)
        except Exception:
            body_text = ""
        row["full_text"] = clean(body_text) or ""

        # 3. LOGICA "CACCIATORE DI RIGHE" (Espansione Tabella DOM)
        # Questa funzione deve essere definita sopra _enrich_one
        budget_val_dom = extract_budget_per_project_dom(page, topic_id)
        
        if budget_val_dom:
            # Se il cacciatore trova il valore nella tabella, ha la priorità
            row["budget_raw"] = budget_val_dom
        elif captured.get("budget"):
            # Altrimenti usiamo il valore numerico trovato via XHR
            row["budget_raw"] = captured["budget"]
        elif body_text:
            # Fallback finale: scansione Regex del testo
            val_reg = extract_budget_from_text(body_text)
            if val_reg > 0:
                row["budget_raw"] = val_reg

    except Exception as e:
        print(f"    [ERR goto] {e}", flush=True)
    finally:
        page.remove_listener("response", handle)

    # 4. Assegnazione finale metadati (Logica originale intatta)
    if captured.get("prog") and not row.get("programme_raw"):
        row["programme_raw"] = captured["prog"]
    if captured.get("action") and not row.get("action_raw"):
        row["action_raw"] = captured["action"]
    if captured.get("call_id") and not row.get("call_id"):
        row["call_id"] = captured["call_id"]

    return bool(captured) or bool(row.get("full_text"))


def enrich(ctx, rows: list):
    to_fix = [r for r in rows
              if (not r.get("programme_raw") or not r.get("action_raw") or not r.get("call_id"))
              and r.get("url")]
    if not to_fix:
        print("  Tutti i campi già presenti ✓", flush=True)
        return

    print(f"  {len(to_fix)} call da arricchire…", flush=True)
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

    u_cnum, u_clabel, u_thematic, u_benef = url_classify(url)
    if u_cnum:
        cluster_num = u_cnum

    cluster_label = u_clabel or THEMATIC_MAP.get(cluster_num, "")
    thematic      = u_thematic or resolve_thematic(cluster_num, prog_raw) or name_classify(row.get("name",""))
    action        = normalize_action(action_raw)
    is_mission    = bool("/HORIZON-MISS" in url.upper())

    opening_raw  = row.get("opening_raw") or ""
    deadline_raw = row.get("deadline_raw") or ""

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
        "opening":          opening_raw,
        "opening_iso":      parse_date_iso(opening_raw),
        "deadline":         deadline_raw,
        "deadline_iso":     parse_date_iso(deadline_raw),
        "url":              url,
        "is_mission":       is_mission,
        "beneficiary_hint": beneficiary_hint(action, prog_raw, u_benef),
        "budget":           row.get("budget_raw") or 0,
        "full_text":        multi["full_text"],
        "keyword_hits":     multi["keyword_hits"],
        "multi_thematic":   multi["multi_thematic"],
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
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            locale="en-US",
            viewport={"width": 1920, "height": 1080},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
        )
        page = ctx.new_page()

        # ── Passo 1: lista ────────────────────────────────────────────────────
        page.goto(LIST_URL.format(page=1, ps=PAGE_SIZE),
                  wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(3000)
        accept_cookies(page)
        wait_cookie_gone(page)

        total = read_total(page)
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
            page.goto(url, wait_until="domcontentloaded", timeout=90000)
            page.wait_for_timeout(1200)
            accept_cookies(page)
            wait_cookie_gone(page)

            scroll_until(page, expected=expected)
            links     = extract_links(page)
            new_links = [u for u in links if u not in seen_urls]
            print(f" → trovati {len(new_links)} nuovi", flush=True)

            for u in new_links:
                seen_urls.add(u)
                rows.append(parse_card(page, u))
            time.sleep(0.1)

        # ── Passo 2: arricchimento ────────────────────────────────────────────
        print(f"\n═══ Passo 2: arricchimento {len(rows)} call totali ═══", flush=True)
        enrich(ctx, rows)
        browser.close()

    # ── Classificazione e output ──────────────────────────────────────────────
    calls = []
    seen  = set()
    for row in rows:
        call = to_call(row)
        if call["url"] and call["url"] not in seen:
            seen.add(call["url"])
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












