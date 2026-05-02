"""
scrape_closed_calls.py
──────────────────────
Scrapa il portale EU Funding & Tenders con Playwright e produce
closed_calls_batch_N.json — chiamate con status CLOSED (31094503),
divise in batch di pagine per rispettare il limite di 6 ore di GitHub Actions.

Uso:
    python scrape_closed_calls.py --batch 1 --total-batches 5
    python scrape_closed_calls.py --batch 3 --total-batches 5 --out /path/batch3.json
    python scrape_closed_calls.py --batch 1 --total-batches 5 --max-pages 5  # test
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

# Status 31094503 = CLOSED
LIST_URL = (
    "https://ec.europa.eu/info/funding-tenders/opportunities/portal/screen"
    "/opportunities/calls-for-proposals"
    "?order=DESC&pageNumber={page}&pageSize={ps}&sortBy=startDate"
    "&isExactMatch=true&status=31094503&programmePeriod=2021%20-%202027"
)

SEARCH_API  = "search-api/prod/rest/search"
COOKIE_TEXT = "This site uses cookies"

LINK_SELECTOR = (
    'a[href*="/topic-details/"], '
    'a[href*="/competitive-calls-cs/"], '
    'a[href*="/prospect-details/"]'
)

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
    ("BRAINHEALTH", "Health & Life Sciences"),
    ("ERDERA",      "Health & Life Sciences"),
    ("EITUM",       "Climate-neutral & Smart Cities"),
    ("EIC AWARDEE", "SME, Entrepreneurship & Market Uptake"),
    ("INNOMATCH",   "SME, Entrepreneurship & Market Uptake"),
    ("BLUEACTION",  "Food, Bioeconomy & Environment"),
    ("RESTORE",     "Food, Bioeconomy & Environment"),
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

# ── Helpers ───────────────────────────────────────────────────────────────────

def escape_rx(s):
    return re.escape(s or "")

def text_has_keyword(text, keyword):
    return bool(re.search(rf"(?<![A-Za-z]){escape_rx(keyword.lower())}(?![A-Za-z])", (text or "").lower()))

def keyword_hits_for_thematic(text, thematic):
    hits = []
    for kw in TOPIC_KEYWORDS.get(thematic, []):
        if text_has_keyword(text, kw):
            hits.append(kw)
    return list(dict.fromkeys(hits))

def title_is_special_basic_research(title):
    tl = (title or "").lower()
    return any(text_has_keyword(tl, kw) for kw in SPECIAL_TITLE_KEYWORDS)

def classify_multitopic(name, full_text, thematic):
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

def _topic_id(url):
    s = (url or "").upper().split("?")[0]
    for m in ["/TOPIC-DETAILS/", "/COMPETITIVE-CALLS-CS/"]:
        i = s.find(m)
        if i >= 0:
            return s[i + len(m):]
    return s

def url_classify(url):
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

def name_classify(name):
    name_up = (name or "").upper()
    for keyword, thematic in NUMERIC_ID_NAME_RULES:
        if keyword.upper() in name_up:
            return thematic
    return ""

def prog_thematic(prog):
    pl = (prog or "").lower()
    for key, label in PROGRAMME_THEMATIC_MAP:
        if key.lower() in pl:
            return label
    return ""

def resolve_thematic(cluster_num, prog):
    if cluster_num and THEMATIC_MAP.get(cluster_num):
        return THEMATIC_MAP[cluster_num]
    return prog_thematic(prog)

def normalize_action(v):
    s = (v or "").lower()
    if "research and innovation action" in s: return "RIA"
    if "innovation action" in s:              return "IA"
    if "coordination and support" in s:       return "CSA"
    if "cofund" in s:                         return "COFUND"
    return v or ""

def beneficiary_hint(action, prog, url_benef):
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

def parse_date_iso(s):
    s = re.sub(r"\s+", " ", str(s or "")).strip()
    if not s: return ""
    m = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", s)
    if m: return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    m = re.search(r"\b(\d{1,2})[\/\-.](\d{1,2})[\/\-.](\d{4})\b", s)
    if m:
        try: return datetime(int(m.group(3)), int(m.group(2)), int(m.group(1))).strftime("%Y-%m-%d")
        except ValueError: pass
    m = re.search(r"\b(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})\b", s)
    if m:
        mo = MONTHS.get(m.group(2).lower())
        if mo:
            try: return datetime(int(m.group(3)), mo, int(m.group(1))).strftime("%Y-%m-%d")
            except ValueError: pass
    return ""

def parse_budget(s):
    if not s: return 0
    s = s.strip()
    m = re.match(r"^([\d]+[.,][\d]+)\s*[Mm]$", s)
    if m:
        try: return int(float(m.group(1).replace(",", ".")) * 1_000_000)
        except ValueError: pass
    m2 = re.match(r"^([\d]+)\s*[Mm]$", s)
    if m2:
        try: return int(m2.group(1)) * 1_000_000
        except ValueError: pass
    cleaned = re.sub(r"[^\d,. ]", "", s).strip()
    if re.match(r"^\d{1,3}(\.\d{3})+(,\d+)?$", cleaned):
        cleaned = cleaned.replace(".", "").replace(",", ".")
    elif re.match(r"^\d{1,3}(,\d{3})+(\.\d+)?$", cleaned):
        cleaned = cleaned.replace(",", "")
    else:
        cleaned = cleaned.replace(" ", "").replace(",", ".")
    try: return int(float(cleaned))
    except ValueError: return 0

def extract_budget_from_text(text):
    candidates = []
    for rx in (RE_BUDGET_INDICATIVE, RE_BUDGET_EXPECTED, RE_BUDGET_LABEL, RE_BUDGET_SUFFIX):
        for m in rx.finditer(text or ""):
            val = parse_budget(m.group(1))
            if 10_000 <= val <= 10_000_000_000:
                candidates.append(val)
    if not candidates: return 0
    return max(candidates)

def clean(s):
    if not s: return None
    s = re.sub(r"\s+", " ", str(s)).strip()
    return s or None

def pick(rx, text):
    m = rx.search(text or "")
    return clean(m.group(1)) if m else None

# ── Playwright helpers ────────────────────────────────────────────────────────

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
        try: body = page.locator("body").inner_text()
        except Exception: body = ""
        if COOKIE_TEXT.lower() not in (body or "").lower(): return
        page.wait_for_timeout(600)

def count_links(page):
    return page.locator(LINK_SELECTOR).count()

def read_total(page, timeout_ms=30000):
    PATTERNS = [
        re.compile(r"(\d[\d,\.]*)\s*item\s*\(?s\)?\s*found",      re.IGNORECASE),
        re.compile(r"(\d[\d,\.]*)\s*results?\s*found",             re.IGNORECASE),
        re.compile(r"(\d[\d,\.]*)\s*opportunit\w+\s*found",        re.IGNORECASE),
        re.compile(r"(\d[\d,\.]*)\s*calls?\s*found",               re.IGNORECASE),
        re.compile(r"found\s+(\d[\d,\.]*)\s*results?",             re.IGNORECASE),
        re.compile(r"Total[:\s]+(\d[\d,\.]*)",                     re.IGNORECASE),
        re.compile(r"(\d[\d,\.]*)\s*result",                       re.IGNORECASE),
    ]
    start = time.time()
    while (time.time() - start) * 1000 < timeout_ms:
        try: txt = page.locator("body").inner_text()
        except Exception: txt = ""
        for pat in PATTERNS:
            m = pat.search(txt or "")
            if m:
                raw = m.group(1).replace(",", "").replace(".", "")
                print(f" Contatore trovato: {raw}")
                return int(raw)
        page.wait_for_timeout(1000)
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
        if c >= expected: return c
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
                if container: page.evaluate("(el)=>{ el.scrollTop = el.scrollHeight; }", container)
                else: page.mouse.wheel(0, 5000)
            except Exception: pass
            page.wait_for_timeout(600)
    return count_links(page)

def extract_links(page):
    hrefs = page.evaluate(f"""
        () => Array.from(document.querySelectorAll('{LINK_SELECTOR}'))
                  .map(a => a.getAttribute('href'))
    """)
    out, seen = [], set()
    for h in hrefs or []:
        if not h: continue
        full = "https://ec.europa.eu" + h if h.startswith("/") else h
        if full not in seen:
            seen.add(full)
            out.append(full)
    return out

# ── Card parsing ──────────────────────────────────────────────────────────────

def parse_card(page, full_url):
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
        "name":          title,
        "call_id":       call_id,
        "programme_raw": pick(RE_PROG, text),
        "action_raw":    pick(RE_ACTION, text),
        "cluster_raw":   cluster_raw,
        "opening_raw":   pick(RE_OPEN, text),
        "deadline_raw":  dead,
        "url":           full_url,
        "status":        "closed",
    }

# ── Detail-page enrichment ────────────────────────────────────────────────────

def _first(meta, *keys):
    for k in keys:
        v = meta.get(k)
        if isinstance(v, list) and v:
            return re.sub(r"\s+", " ", str(v[0])).strip()
        if v and isinstance(v, str):
            return v.strip()
    return ""

def extract_budget_per_project_dom(page, topic_id):
    parts = topic_id.split('?')[0].split('-')
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
    except:
        return None

def _enrich_one(page, row):
    url = row["url"]
    captured = {}
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
                    if not _c.get("budget"):
                        for key in ("budgetOverviewTotal","totalBudget","budget","budgetTopicActions","indicativeBudget","availableBudget","estimatedTotalContribution"):
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
        page.goto(url, wait_until="domcontentloaded", timeout=40000)
        page.wait_for_timeout(2500)
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
        page.remove_listener("response", handle)

    if captured.get("prog") and not row.get("programme_raw"):
        row["programme_raw"] = captured["prog"]
    if captured.get("action") and not row.get("action_raw"):
        row["action_raw"] = captured["action"]
    if captured.get("call_id") and not row.get("call_id"):
        row["call_id"] = captured["call_id"]

    return bool(captured) or bool(row.get("full_text"))


def enrich(ctx, rows):
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
                try: page.close()
                except Exception: pass
                page = ctx.new_page()
                time.sleep(2)
        if not ok:
            skipped += 1
            print(f"    [SKIP] nessun dato recuperato", flush=True)
        if idx % 100 == 0:
            print(f"  [checkpoint] processate {idx} call…", flush=True)
        time.sleep(0.3)

    try: page.close()
    except Exception: pass
    print(f"  Arricchimento completato. Saltate: {skipped}/{len(to_fix)}", flush=True)

# ── Transform row → call object ───────────────────────────────────────────────

def to_call(row):
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
    if u_cnum: cluster_num = u_cnum

    cluster_label = u_clabel or THEMATIC_MAP.get(cluster_num, "")
    thematic      = u_thematic or resolve_thematic(cluster_num, prog_raw) or name_classify(row.get("name",""))
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
        "status":           "closed",
        "is_mission":       is_mission,
        "beneficiary_hint": beneficiary_hint(action, prog_raw, u_benef),
        "budget":           row.get("budget_raw") or 0,
        "keyword_hits":     multi["keyword_hits"],
        "multi_thematic":   multi["multi_thematic"],
        "is_special_basic_research": multi["is_special_basic_research"],
    }

# ── Main ──────────────────────────────────────────────────────────────────────

def main(batch: int, total_batches: int, out_path: Path, max_pages: int = None):
    """
    Scrapa solo le pagine assegnate a questo batch.

    Esempio con 5 batch e 100 pagine totali:
      batch 1 → pagine  1-20
      batch 2 → pagine 21-40
      ...
      batch 5 → pagine 81-100

    Il totale viene letto dalla pagina 1 (sempre disponibile) e poi
    ogni job salta direttamente alle proprie pagine.
    """
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

        # ── Step 1: leggi il totale dalla pagina 1 ────────────────────────────
        print(f"[batch {batch}/{total_batches}] Lettura totale call…", flush=True)
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

        real_max_pages = math.ceil(total / PAGE_SIZE)
        # Se --max-pages è impostato lo usiamo come tetto globale (utile per test)
        global_max = min(real_max_pages, max_pages) if max_pages else real_max_pages

        # Calcola il range di pagine per questo batch
        pages_per_batch = math.ceil(global_max / total_batches)
        page_from = (batch - 1) * pages_per_batch + 1
        page_to   = min(batch * pages_per_batch, global_max)

        print(
            f"✅ Totale call: {total} | pagine totali: {real_max_pages} "
            f"| questo batch: pagine {page_from}–{page_to} "
            f"(~{page_to - page_from + 1} pagine, ~{(page_to - page_from + 1) * PAGE_SIZE} call)",
            flush=True,
        )

        if page_from > global_max:
            print(f"ℹ️  Nessuna pagina da scrapare per il batch {batch} (totale pagine: {global_max})")
            browser.close()
            return

        # ── Step 2: scrapa le pagine del batch ───────────────────────────────
        for pnum in range(page_from, page_to + 1):
            remaining = total - (pnum - 1) * PAGE_SIZE
            expected  = min(PAGE_SIZE, max(0, remaining))
            url = LIST_URL.format(page=pnum, ps=PAGE_SIZE)
            print(f"\n[p{pnum}/{global_max}] attese ~{expected}", end="", flush=True)
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

        # ── Step 3: enrichment ────────────────────────────────────────────────
        print(f"\n═══ Passo 3: arricchimento {len(rows)} call (batch {batch}) ═══", flush=True)
        enrich(ctx, rows)
        browser.close()

    # ── Classification and output ─────────────────────────────────────────────
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
    print(f"\nClassificazione batch {batch} ({len(calls)} call):")
    for k, v in sorted(tc.items(), key=lambda x: -x[1]):
        print(f"  {v:5d}  {k}")

    generated = datetime.now(timezone.utc).isoformat()

    # ── Build autofill index ──────────────────────────────────────────────────
    autofill_index = {}
    for c in calls:
        slug = c["url"].split("/topic-details/")[-1].split("?")[0].upper() if "/topic-details/" in c["url"].lower() else ""
        if not slug:
            slug = c["url"].split("/competitive-calls-cs/")[-1].split("?")[0].upper()
        if slug:
            autofill_index[slug] = {
                "name":             c["name"],
                "thematic_cluster": c["thematic_cluster"],
                "multi_thematic":   c["multi_thematic"],
                "action":           c["action"],
                "call_id":          c["call_id"],
                "keyword_hits":     {k: v[:5] for k, v in c.get("keyword_hits", {}).items()},
            }
        if c["call_id"]:
            cid_key = c["call_id"].upper()
            if cid_key not in autofill_index:
                autofill_index[cid_key] = autofill_index.get(slug) or {
                    "name":             c["name"],
                    "thematic_cluster": c["thematic_cluster"],
                    "multi_thematic":   c["multi_thematic"],
                    "action":           c["action"],
                    "call_id":          c["call_id"],
                    "keyword_hits":     {k: v[:5] for k, v in c.get("keyword_hits", {}).items()},
                }

    # ── Salva output per-batch ────────────────────────────────────────────────
    payload = {
        "generated":      generated,
        "status":         "closed",
        "batch":          batch,
        "total_batches":  total_batches,
        "page_from":      page_from,
        "page_to":        page_to,
        "total_scraped":  len(calls),
        "calls":          calls,
        "autofill_index": autofill_index,
    }
    out_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"\n✅ Scritto {out_path} con {len(calls)} call (batch {batch}/{total_batches})")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Scrapa le call CHIUSE dal portale EU F&T in batch di pagine"
    )
    parser.add_argument("--batch",         type=int, required=True,
                        help="Numero del batch corrente (1-based, es. 1)")
    parser.add_argument("--total-batches", type=int, default=5,
                        help="Numero totale di batch (default: 5)")
    parser.add_argument("--out",           default=None,
                        help="Percorso output JSON (default: closed_calls_batch_N.json)")
    parser.add_argument("--max-pages",     type=int, default=None,
                        help="Numero massimo pagine globale (per test)")
    args = parser.parse_args()

    out = Path(args.out) if args.out else Path(f"closed_calls_batch_{args.batch}.json")
    main(args.batch, args.total_batches, out, args.max_pages)
