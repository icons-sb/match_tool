"""
scrape_to_json.py  (fixed – full pagination via official REST API)
──────────────────────────────────────────────────────────────────
ROOT CAUSE OF MISSING CALLS (708/786):
  The old code used Playwright to scrape the portal's HTML list pages.
  The portal renders results client-side and has internal limits on how
  many items it will expose through the DOM.

FIX:
  The EU F&T Portal exposes a fully public REST API documented at:
    https://api.tech.ec.europa.eu/search-api/prod/rest/search?apiKey=SEDIA
  It supports `pageSize` (up to 50) and `pageNumber` parameters in the
  POST body's query JSON, returning `totalResults` so we know exactly
  how many pages to fetch.  No browser needed for listing — all calls
  are retrieved reliably this way.

  Playwright is still used ONLY for detail-page enrichment (budget DOM
  extraction, full body text, XHR interception).

Usage:
  python scrape_to_json.py           # writes calls.json in current dir
  python scrape_to_json.py --out /path/to/dir
"""

import re
import math
import time
import json
import argparse
import requests
from datetime import datetime, timezone
from pathlib import Path
from playwright.sync_api import sync_playwright

# ── API / URL constants ────────────────────────────────────────────────────────

SEARCH_API_URL  = "https://api.tech.ec.europa.eu/search-api/prod/rest/search"
API_KEY         = "SEDIA"
PAGE_SIZE       = 50          # max allowed by the API
LANGUAGE        = "en"        # filter to English records (avoids multilingual duplicates)
REQUEST_DELAY   = 0.4         # seconds between API pages (be polite)
ENRICH_DELAY    = 0.3         # seconds between detail-page visits

# Status codes: 31094501 = Open, 31094502 = Forthcoming  (add 31094503 for Closed)
STATUS_CODES    = ["31094501", "31094502"]
# Types: "1"=Grants, "2"=Grants (indirect), "8"=Grants (budget)  (add "0" for Tenders)
CALL_TYPES      = ["1", "2", "8"]
PROGRAMME_PERIOD = "2021 - 2027"

SEARCH_API_PATH = "search-api/prod/rest/search"   # substring used to detect XHR calls

# ── Regex helpers ──────────────────────────────────────────────────────────────

RE_TOTAL        = re.compile(r"(\d+)\s*item\s*\(?s\)?\s*found", re.IGNORECASE)
RE_OPEN         = re.compile(r"Opening date:\s*([^\|\n\r]+)", re.IGNORECASE)
RE_DEAD         = re.compile(r"Deadline date:\s*([^\|\n\r]+)", re.IGNORECASE)
RE_NEXT_DEAD    = re.compile(r"Next deadline:\s*([^\|\n\r]+)", re.IGNORECASE)
RE_PROG         = re.compile(r"Programme:\s*([^\|\n\r]+)", re.IGNORECASE)
RE_ACTION       = re.compile(r"Type of action:\s*([^\|\n\r]+)", re.IGNORECASE)
RE_CLUSTER      = re.compile(r"HORIZON-CL([1-6])", re.IGNORECASE)
RE_CALL_ID      = re.compile(r"callIdentifier[=:\s]+([^\s&\|\n\r]+)", re.IGNORECASE)

RE_BUDGET_LABEL      = re.compile(r"(?:total\s+)?budget[:\s]+(?:of\s+)?(?:EUR|€|euro)?\s*([\d][0-9 .,]+)", re.IGNORECASE)
RE_BUDGET_SUFFIX     = re.compile(r"([\d][0-9 .,]+)\s*(?:EUR|€|euro)", re.IGNORECASE)
RE_BUDGET_INDICATIVE = re.compile(r"indicative\s+(?:total\s+)?budget[:\s]+(?:EUR|€|euro)?\s*([\d][0-9 .,]+)", re.IGNORECASE)
RE_BUDGET_EXPECTED   = re.compile(r"(?:total\s+)?(?:estimated|expected|available|allocated)\s+budget[:\s]+(?:EUR|€|euro)?\s*([\d][0-9 .,]+)", re.IGNORECASE)

MONTHS = {
    "january":1,"february":2,"march":3,"april":4,"may":5,"june":6,
    "july":7,"august":8,"september":9,"october":10,"november":11,"december":12,
}

# ── Classification tables (unchanged from original) ───────────────────────────

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
    ("European Defence Fund","Defence"),
    ("EDF","Defence"),
    ("EU External Action","External Action & International Cooperation"),
    ("EU External Action-Prospect","External Action & International Cooperation"),
    ("Single Market Programme","SME, Entrepreneurship & Market Uptake"),
    ("CERV","Culture, Creativity & Inclusion"),
    ("Creative Europe","Culture, Creativity & Inclusion"),
    ("Erasmus+","Culture, Creativity & Inclusion"),
    ("European Social Fund+","Culture, Creativity & Inclusion"),
    ("Just Transition","Climate, Energy & Mobility"),
    ("Innovation Fund","Climate, Energy & Mobility"),
    ("EMFAF","Food, Bioeconomy & Environment"),
    ("LIFE","Food, Bioeconomy & Environment"),
    ("Euratom","Climate, Energy & Mobility"),
    ("Connecting Europe","Climate, Energy & Mobility"),
    ("Internal Security Fund","Security & Resilience"),
    ("European Solidarity Corps","Culture, Creativity & Inclusion"),
    ("Digital Europe","Digital, Industry & Space"),
    ("RENEWFM","Climate, Energy & Mobility"),
    ("SOCPL","Culture, Creativity & Inclusion"),
    ("JUST","Culture, Creativity & Inclusion"),
    ("Pericles IV","Culture, Creativity & Inclusion"),
    ("I3","SME, Entrepreneurship & Market Uptake"),
    ("ERC","Cross-cutting / Other"),
    ("43392145","Food, Bioeconomy & Environment"),
    ("Horizon Europe","Cross-cutting / Other"),
]

URL_RULES = [
    ("MISS","CIT",   "M-CIT",  "Climate-neutral & Smart Cities","Climate-neutral & Smart Cities"),
    ("MISS","OCEAN", "M-OCEAN","Healthy Oceans, Seas, Coastal & Inland Waters","Healthy Oceans, Seas, Coastal & Inland Waters"),
    ("MISS","CLIMA", "5","Climate, Energy and Mobility","Climate, Energy & Mobility"),
    ("MISS","CANCER","1","Health","Health & Life Sciences"),
    ("MISS","SOIL",  "6","Food, Bioeconomy, Natural Resources, Agriculture and Environment","Food, Bioeconomy & Environment"),
    ("MISS","CROSS", "","","Cross-cutting / Other"),
    ("HLTH",None,"1","Health","Health & Life Sciences"),
    ("EIC", None,"","","SME, Entrepreneurship & Market Uptake"),
    ("EIE", None,"","","SME, Entrepreneurship & Market Uptake"),
    ("EITUM-BP",None,"M-CIT","Climate-neutral & Smart Cities","Climate-neutral & Smart Cities"),
    ("EIT", None,"","","SME, Entrepreneurship & Market Uptake"),
    ("CID", None,"5","Climate, Energy and Mobility","Climate, Energy & Mobility"),
    ("EURATOM",None,"5","Climate, Energy and Mobility","Climate, Energy & Mobility"),
    ("EUROHPC",None,"4","Digital, Industry and Space","Digital, Industry & Space"),
    ("JU-CLEAN-AVIATION",None,"","","Clean Aviation"),
    ("JU-", None,"","","Climate, Energy & Mobility"),
    ("MSCA",None,"","","Cross-cutting / Other"),
    ("NEB", None,"","","Climate-neutral & Smart Cities"),
    ("RAISE",None,"4","Digital, Industry and Space","Digital, Industry & Space"),
    ("WIDERA",None,"","","Cross-cutting / Other"),
    ("CL3","INFRA","3","Civil Security for Society","Security & Resilience"),
    ("INFRA","TECH","4","Digital, Industry and Space","Digital, Industry & Space"),
    ("INFRA","SERV","4","Digital, Industry and Space","Digital, Industry & Space"),
    ("INFRA","DEV","","","Cross-cutting / Other"),
    ("INFRA","EOSC","","","Cross-cutting / Other"),
    ("INFRA",None,"","","Cross-cutting / Other"),
    ("AGRIP",None,"6","Food, Bioeconomy, Natural Resources, Agriculture and Environment","Food, Bioeconomy & Environment"),
    ("EUAF",None,"4","Digital, Industry and Space","Digital, Industry & Space"),
    ("DIGITAL",None,"4","Digital, Industry and Space","Digital, Industry & Space"),
    ("UCPM",None,"","","Cross-cutting / Other"),
    ("RFCS",None,"5","Climate, Energy and Mobility","Climate, Energy & Mobility"),
    ("EUBA",None,"","","External Action & International Cooperation"),
    ("PPPA","CHIPS","4","Digital, Industry and Space","Digital, Industry & Space"),
    ("PPPA","MEDIA","","","Culture, Creativity & Inclusion"),
    ("PPPA",None,"4","Digital, Industry and Space","Digital, Industry & Space"),
    ("RENEWFM",None,"5","Climate, Energy and Mobility","Climate, Energy & Mobility"),
    ("SOCPL",None,"","","Culture, Creativity & Inclusion"),
    ("ERC", None,"","","Cross-cutting / Other"),
    ("EMFAF",None,"6","Food, Bioeconomy, Natural Resources, Agriculture and Environment","Food, Bioeconomy & Environment"),
    ("JUST",None,"","","Culture, Creativity & Inclusion"),
    ("I3",  None,"","","SME, Entrepreneurship & Market Uptake"),
]

NUMERIC_ID_NAME_RULES = [
    ("OHAMR","Health & Life Sciences"),
    ("ERA4HEALTH","Health & Life Sciences"),
    ("ERDERA","Health & Life Sciences"),
    ("BE READY","Health & Life Sciences"),
    ("OVERWEIGHT","Health & Life Sciences"),
    ("OBESITY","Health & Life Sciences"),
    ("CARDIOVASC","Health & Life Sciences"),
    ("CLINICAL TRIAL","Health & Life Sciences"),
    ("NEUROSCI","Health & Life Sciences"),
    ("RARE DISEASE","Health & Life Sciences"),
    ("EITUM","Climate-neutral & Smart Cities"),
    ("URBAN MOBILITY","Climate-neutral & Smart Cities"),
    ("EIC AWARDEE","SME, Entrepreneurship & Market Uptake"),
    ("INNOMATCH","SME, Entrepreneurship & Market Uptake"),
    ("STARTUP","SME, Entrepreneurship & Market Uptake"),
    ("FOOD SUSTAINABILITY","Food, Bioeconomy & Environment"),
    ("MARINE BIODIVERSITY","Food, Bioeconomy & Environment"),
    ("BLUEACTION","Food, Bioeconomy & Environment"),
    ("TASC-RESTOREMED","Food, Bioeconomy & Environment"),
    ("RESTORE","Food, Bioeconomy & Environment"),
    ("FERMENTED","Food, Bioeconomy & Environment"),
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

# ── NEW: fetch ALL calls via the official REST API ─────────────────────────────

def build_query(page_number: int) -> dict:
    # This specific structure is required to get the ~787 grants
    return {
        "bool": {
            "must": [
                { "terms": { "type": ["1", "2", "8"] } },        # Grants only
                { "terms": { "status": ["31094501", "31094502"] } }, # Open & Forthcoming
                { "term":  { "programmePeriod": "2021 - 2027" } }    # Current period
            ]
        }
    }


def fetch_all_calls_via_api() -> list[dict]:
    """
    Paginate through the Search API and collect every call record.
    Returns a list of raw metadata dicts (one per call/topic).

    Key insight from the PDF docs:
      POST https://api.tech.ec.europa.eu/search-api/prod/rest/search
           ?apiKey=SEDIA&text=***
      Form data: { query: <JSON>, languages: ["en"] }
      Response: { totalResults: N, results: [...] }

    The API supports pageSize up to 50 and uses pageNumber (1-based).
    To avoid multilingual duplicates (noted in the PDF), we filter to "en".
    """
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (compatible; EU-FT-Scraper/2.0)",
        "Accept": "application/json",
    })

    all_rows = []
    page_num = 1
    total_results = None

    print("═══ Step 1: Fetching all calls via REST API ═══")

    while True:
        params = {
            "apiKey": API_KEY,
            "text": "***",
            "pageSize": PAGE_SIZE,
            "pageNumber": page_num,
        }
        form_data = {
            "query": json.dumps(build_query(page_num)),
            "languages": json.dumps([LANGUAGE]),
        }

        for attempt in range(1, 4):
            try:
                resp = session.post(
                    SEARCH_API_URL,
                    params=params,
                    data=form_data,
                    timeout=30,
                )
                resp.raise_for_status()
                body = resp.json()
                break
            except Exception as e:
                print(f"  [page {page_num}, attempt {attempt}] Error: {e}")
                if attempt == 3:
                    raise
                time.sleep(2 * attempt)

        if total_results is None:
            total_results = body.get("totalResults", 0)
            total_pages = math.ceil(total_results / PAGE_SIZE)
            print(f"  Total results: {total_results} | Pages: {total_pages}")

        results = body.get("results", [])
        if not results:
            print(f"  Page {page_num}: no results returned, stopping.")
            break

        for item in results:
            meta = item.get("metadata", {}) or {}
            url_raw = item.get("url", "") or ""
            # Build canonical portal URL from callIdentifier if url is NA/missing
            identifier_list = meta.get("identifier") or meta.get("callIdentifier") or []
            identifier = identifier_list[0] if isinstance(identifier_list, list) and identifier_list else str(identifier_list or "")
            if url_raw and url_raw != "NA":
                portal_url = url_raw
            elif identifier:
                portal_url = f"https://ec.europa.eu/info/funding-tenders/opportunities/portal/screen/opportunities/topic-details/{identifier}"
            else:
                portal_url = ""

            # Normalise programme ID → name
            fp_list = meta.get("frameworkProgramme") or []
            fp_id = fp_list[0] if isinstance(fp_list, list) and fp_list else str(fp_list or "")
            prog_name = PROGRAMME_MAP.get(fp_id, fp_id)

            # Deadline / opening from metadata
            deadline_list = meta.get("deadlineDate") or meta.get("closingDate") or []
            deadline_raw  = deadline_list[0] if isinstance(deadline_list, list) and deadline_list else str(deadline_list or "")

            opening_list  = meta.get("startDate") or []
            opening_raw   = opening_list[0] if isinstance(opening_list, list) and opening_list else str(opening_list or "")

            action_list   = meta.get("typesOfAction") or meta.get("typeOfAction") or []
            action_raw    = action_list[0] if isinstance(action_list, list) and action_list else str(action_list or "")

            title_list    = meta.get("title") or []
            title         = title_list[0] if isinstance(title_list, list) and title_list else (item.get("content") or identifier or "")

            call_id_list  = meta.get("callIdentifier") or meta.get("identifier") or []
            call_id       = call_id_list[0] if isinstance(call_id_list, list) and call_id_list else str(call_id_list or "")

            all_rows.append({
                "name":          clean(title),
                "call_id":       call_id,
                "programme_raw": prog_name,
                "action_raw":    action_raw,
                "cluster_raw":   "",
                "opening_raw":   opening_raw,
                "deadline_raw":  deadline_raw,
                "url":           portal_url,
                "full_text":     "",
                "_api_meta":     meta,   # keep for enrichment fallback
            })

        fetched_so_far = (page_num - 1) * PAGE_SIZE + len(results)
        print(f"  Page {page_num}/{total_pages}: +{len(results)} rows  (total so far: {fetched_so_far}/{total_results})", flush=True)

        if fetched_so_far >= total_results:
            break
        page_num += 1
        time.sleep(REQUEST_DELAY)

    # Deduplicate by URL (same topic can appear under multiple API pages if multilingual)
    seen_urls = set()
    deduped = []
    for row in all_rows:
        key = row["url"] or row["call_id"]
        if key and key not in seen_urls:
            seen_urls.add(key)
            deduped.append(row)

    print(f"\n  ✅ {len(deduped)} unique calls collected (raw: {len(all_rows)})")
    return deduped


# ── Classification helpers (unchanged from original) ──────────────────────────

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
        "full_text":               text,
        "keyword_hits":            keyword_hits,
        "multi_thematic":          multi_thematic,
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
        if subcode is not None and subcode not in tid:
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
    if a == "IA":  hints.extend(["SME","Large enterprise","Research organisation"])
    if a == "RIA": hints.extend(["Research organisation","SME","Large enterprise"])
    if a == "CSA": hints.extend(["Research organisation","Public body","NGO","SME"])
    if "external action" in p: hints.extend(["NGO","Public body","Research organisation"])
    return list(dict.fromkeys(hints))

# ── Date / budget parsing (unchanged) ─────────────────────────────────────────

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
    return max(candidates) if candidates else 0

def clean(s):
    if not s:
        return None
    s = re.sub(r"\s+", " ", str(s)).strip()
    return s or None

# ── Detail-page enrichment (Playwright, unchanged logic) ──────────────────────

def _first(meta, *keys):
    for k in keys:
        v = meta.get(k)
        if isinstance(v, list) and v:
            return re.sub(r"\s+", " ", str(v[0])).strip()
        if v and isinstance(v, str):
            return v.strip()
    return ""

def extract_budget_per_project_dom(page, topic_id):
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
                        const isDate   = /202[0-9]/.test(txt) && txt.length < 15;
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

def _enrich_one(page, row: dict) -> bool:
    url = row["url"]
    if not url or url == "NA":
        return False
    captured = {}
    topic_id = url.split("/")[-1].split("?")[0]

    def handle(response, _c=captured):
        if SEARCH_API_PATH in response.url and response.status == 200:
            try:
                body = response.json()
                for item in body.get("results", [body]):
                    meta = item.get("metadata", {}) or {}
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
                        for key in ("budgetOverviewTotal","totalBudget","budget",
                                    "budgetTopicActions","indicativeBudget",
                                    "availableBudget","estimatedTotalContribution"):
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
        accept_cookies(page)
        try:
            body_text = page.locator("body").inner_text(timeout=5000)
        except Exception:
            body_text = ""
        row["full_text"] = clean(body_text) or ""

        budget_dom = extract_budget_per_project_dom(page, topic_id)
        if budget_dom:
            row["budget_raw"] = budget_dom
        elif captured.get("budget"):
            row["budget_raw"] = captured["budget"]
        elif body_text:
            val_reg = extract_budget_from_text(body_text)
            if val_reg > 0:
                row["budget_raw"] = val_reg
    except Exception as e:
        print(f"  [ERR goto] {e}", flush=True)
    finally:
        page.remove_listener("response", handle)

    # Also try to fill from _api_meta already available (avoids unnecessary page visits)
    api_meta = row.get("_api_meta", {})
    if captured.get("prog") and not row.get("programme_raw"):
        row["programme_raw"] = captured["prog"]
    elif not row.get("programme_raw") and api_meta:
        fp = api_meta.get("frameworkProgramme") or []
        fp_id = fp[0] if isinstance(fp, list) and fp else str(fp or "")
        if fp_id:
            row["programme_raw"] = PROGRAMME_MAP.get(fp_id, fp_id)

    if captured.get("action") and not row.get("action_raw"):
        row["action_raw"] = captured["action"]
    if captured.get("call_id") and not row.get("call_id"):
        row["call_id"] = captured["call_id"]

    return True

def enrich(ctx, rows: list):
    """
    Only enrich rows where full_text or budget is still missing.
    Rows fetched via the API already have programme/action/call_id filled,
    so far fewer rows need a Playwright visit than before.
    """
    to_fix = [r for r in rows if not r.get("full_text") and r.get("url") and r["url"] != "NA"]
    if not to_fix:
        print("  All rows already have full_text ✓", flush=True)
        return

    print(f"\n═══ Step 2: Enriching {len(to_fix)} detail pages via Playwright ═══", flush=True)
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
                print(f"  [attempt {attempt} failed] {e}", flush=True)
                try:
                    page.close()
                except Exception:
                    pass
                page = ctx.new_page()
                time.sleep(2)
        if not ok:
            skipped += 1
        if idx % 100 == 0:
            print(f"  [checkpoint] {idx} enriched so far…", flush=True)
        time.sleep(ENRICH_DELAY)

    try:
        page.close()
    except Exception:
        pass
    print(f"  Enrichment done. Skipped: {skipped}/{len(to_fix)}", flush=True)

# ── Build final call object ────────────────────────────────────────────────────

def to_call(row: dict) -> dict:
    url       = row.get("url", "")
    prog_raw  = row.get("programme_raw") or ""
    call_id   = row.get("call_id") or ""
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
    full_text     = row.get("full_text") or ""
    multi = classify_multitopic(row.get("name") or "", full_text, thematic)

    return {
        "name":                    row.get("name") or "",
        "call_id":                 call_id,
        "programme":               prog_raw,
        "cluster_num":             cluster_num,
        "cluster_label":           cluster_label,
        "thematic_cluster":        thematic,
        "action":                  action,
        "opening":                 row.get("opening_raw") or "",
        "opening_iso":             parse_date_iso(row.get("opening_raw") or ""),
        "deadline":                row.get("deadline_raw") or "",
        "deadline_iso":            parse_date_iso(row.get("deadline_raw") or ""),
        "url":                     url,
        "is_mission":              is_mission,
        "beneficiary_hint":        beneficiary_hint(action, prog_raw, u_benef),
        "budget":                  row.get("budget_raw") or 0,
        "full_text":               multi["full_text"],
        "keyword_hits":            multi["keyword_hits"],
        "multi_thematic":          multi["multi_thematic"],
        "is_special_basic_research": multi["is_special_basic_research"],
    }

# ── Changelog (unchanged) ──────────────────────────────────────────────────────

def write_changelog(old_calls: list, new_calls: list, changelog_path: Path, generated: str):
    old_by_url = {c["url"]: c for c in old_calls}
    new_by_url = {c["url"]: c for c in new_calls}
    added   = [new_by_url[u] for u in sorted(set(new_by_url) - set(old_by_url))]
    removed = [old_by_url[u] for u in sorted(set(old_by_url) - set(new_by_url))]

    def thematic_counts(calls):
        tc = {}
        for c in calls:
            k = c.get("thematic_cluster") or "(unclassified)"
            tc[k] = tc.get(k, 0) + 1
        return tc

    date_str = generated[:10]
    lines = [
        "# Changelog calls.json", "",
        f"**Last update:** {generated.replace('T',' ').replace('+00:00',' UTC')[:22]}", "",
        "## Summary", "",
        "| | Count |", "|---|---|",
        f"| Total calls (new) | {len(new_calls)} |",
        f"| Total calls (previous) | {len(old_calls)} |",
        f"| **New calls added** | **{len(added)}** |",
        f"| Calls removed (expired/closed) | {len(removed)} |", "",
    ]
    if added:
        lines += [f"## Added calls ({len(added)})", ""]
        by_thematic = {}
        for c in added:
            t = c.get("thematic_cluster") or "(unclassified)"
            by_thematic.setdefault(t, []).append(c)
        for thematic, calls in sorted(by_thematic.items()):
            lines += [f"### {thematic} ({len(calls)})", ""]
            for c in calls:
                name  = c.get("name") or "(no name)"
                prog  = c.get("programme") or ""
                action = c.get("action") or ""
                dead  = c.get("deadline") or ""
                url   = c.get("url") or ""
                meta  = " · ".join(filter(None, [prog, action, f"Deadline: {dead}" if dead else ""]))
                lines.append(f"- **{name}**")
                if meta:  lines.append(f"  {meta}")
                if url:   lines.append(f"  {url}")
            lines.append("")
    else:
        lines += ["## Added calls", "", "No new calls vs previous run.", ""]

    if removed:
        lines += [f"## Removed calls ({len(removed)})", ""]
        for c in removed:
            name = c.get("name") or "(no name)"
            prog = c.get("programme") or ""
            dead = c.get("deadline") or ""
            meta = " · ".join(filter(None, [prog, f"Deadline: {dead}" if dead else ""]))
            lines.append(f"- **{name}**{(' — ' + meta) if meta else ''}")
        lines.append("")

    lines += ["## Thematic distribution (new dataset)", "", "| Thematic area | Calls |", "|---|---|"]
    for k, v in sorted(thematic_counts(new_calls).items(), key=lambda x: -x[1]):
        lines.append(f"| {k} | {v} |")
    lines.append("")

    changelog_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n📋 Changelog written: {changelog_path} (+{len(added)} added, -{len(removed)} removed)")

    history_path = changelog_path.parent / "changelog_history.md"
    history_line = f"| {date_str} | {len(new_calls)} | +{len(added)} | -{len(removed)} |"
    if history_path.exists():
        hist = history_path.read_text(encoding="utf-8")
        if history_line not in hist:
            history_path.write_text(hist.rstrip() + "\n" + history_line + "\n", encoding="utf-8")
    else:
        history_path.write_text(
            "# Update history\n\n| Date | Total | Added | Removed |\n|---|---|---|---|\n" + history_line + "\n",
            encoding="utf-8",
        )
    print(f"📋 History updated: {history_path}")

# ── Main ───────────────────────────────────────────────────────────────────────

def main(out_path: Path):
    # ── Step 1: collect all calls via REST API (no browser needed) ──────────
    rows = fetch_all_calls_via_api()

    if not rows:
        print("❌ No calls returned from API. Check STATUS_CODES / CALL_TYPES filters.")
        return

    # ── Step 2: enrich detail pages via Playwright ──────────────────────────
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
        enrich(ctx, rows)
        browser.close()

    # ── Step 3: classify & deduplicate ──────────────────────────────────────
    calls = []
    seen  = set()
    for row in rows:
        call = to_call(row)
        if call["url"] and call["url"] not in seen:
            seen.add(call["url"])
            calls.append(call)

    tc = {}
    for c in calls:
        k = c["thematic_cluster"] or "(unclassified)"
        tc[k] = tc.get(k, 0) + 1
    print(f"\nClassification ({len(calls)} total calls):")
    for k, v in sorted(tc.items(), key=lambda x: -x[1]):
        print(f"  {v:5d}  {k}")

    # ── Step 4: write output ─────────────────────────────────────────────────
    out_path.mkdir(parents=True, exist_ok=True)
    calls_file = out_path / "calls.json"
    generated  = datetime.now(timezone.utc).isoformat()

    old_calls = []
    if calls_file.exists():
        try:
            old_calls = json.loads(calls_file.read_text(encoding="utf-8"))
        except Exception:
            pass

    output = {"generated": generated, "total": len(calls), "calls": calls}
    calls_file.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n✅ Written: {calls_file}  ({len(calls)} calls)")

    changelog_path = out_path / "CHANGELOG.md"
    write_changelog(old_calls if isinstance(old_calls, list) else old_calls.get("calls", []),
                    calls, changelog_path, generated)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Scrape EU F&T Portal → calls.json")
    parser.add_argument("--out", default=".", help="Output directory (default: current dir)")
    args = parser.parse_args()
    main(Path(args.out))





































