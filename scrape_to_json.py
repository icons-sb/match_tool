"""
scrape_to_json.py

Scrapes the EU Funding & Tenders portal with Playwright and produces calls.json
directly, without going through Excel.
Incorporates all classification logic from make_calls_json.py.

Usage:
    python scrape_to_json.py              # writes calls.json in the current folder
    python scrape_to_json.py --out /path  # custom output path
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

# Configuration parameters 

PAGE_SIZE = 50  # number of results per API page

# Standard direct calls (type=1 is implicit when no type filter is specified)
LIST_URL = (
    "https://ec.europa.eu/info/funding-tenders/opportunities/portal/screen"
    "/opportunities/calls-for-proposals"
    "?order=DESC&pageNumber={page}&pageSize={ps}&sortBy=startDate"
    "&isExactMatch=true&status=31094501,31094502&programmePeriod=2021%20-%202027"
)

# Cascade / FSTP calls (type=8): open calls emitted by Horizon-funded projects
# under the Financial Support to Third Parties mechanism.
LIST_URL_CASCADE = (
    "https://ec.europa.eu/info/funding-tenders/opportunities/portal/screen"
    "/opportunities/calls-for-proposals"
    "?order=DESC&pageNumber={page}&pageSize={ps}&sortBy=startDate"
    "&isExactMatch=true&status=31094501,31094502&programmePeriod=2021%20-%202027"
    "&type=8"
)

# Substring present in every SEDIA API request URL — used to identify XHR calls
SEARCH_API  = "apiKey=SEDIA"
# Text that appears in the cookie banner — used to detect unloaded pages
COOKIE_TEXT = "This site uses cookies"

# CSS selector that matches any call link on the listing page
LINK_SELECTOR = (
    'a[href*="/topic-details/"], '
    'a[href*="/competitive-calls-cs/"], '
    'a[href*="/prospect-details/"]'
)

# Base URLs for call links, built from the API 'reference' field
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

# Regex to extract the parent project name from cascade call descriptions.
# FSTP calls typically mention the parent project in the title or description
# with patterns like "JARVIS Open Call" or "funded by the PROJECTNAME project".
RE_PARENT_PROJECT = re.compile(
    r"\b([A-Z][A-Z0-9\-]{2,})\s+(?:Open\s+Call|cascade|third.party|FSTP)",
    re.IGNORECASE,
)

# Month name -> number mapping used when parsing written-out dates (e.g. "15 March 2026")
MONTHS = {
    "january":1,"february":2,"march":3,"april":4,"may":5,"june":6,
    "july":7,"august":8,"september":9,"october":10,"november":11,"december":12,
}

# Classification tables 

# Maps numeric SEDIA programme IDs to human-readable programme names
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

# Horizon Europe: structural mapping for clusters CL1-6 
HE_CLUSTER_MAP = {
    "1": "Health & Life Sciences",
    "2": "Culture, Creativity & Inclusion",
    "3": "Security & Resilience",
    "4": "Digital, Industry & Space",
    "5": "Climate, Energy & Mobility",
    "6": "Food, Bioeconomy & Environment",
}

# Horizon Europe: mapping for non-CL sub-programmes (topic ID prefix)
HE_SUBPROGRAMME_MAP = [
    ("MISS-CIT",    "M-CIT",  "Climate-neutral & Smart Cities",                "Climate-neutral & Smart Cities"),
    ("MISS-OCEAN",  "M-OCEAN","Healthy Oceans, Seas, Coastal & Inland Waters", "Healthy Oceans, Seas, Coastal & Inland Waters"),
    ("MISS-CLIMA",  "5",      "Climate, Energy and Mobility",                  "Climate, Energy & Mobility"),
    ("MISS-CANCER", "1",      "Health",                                        "Health & Life Sciences"),
    ("MISS-SOIL",   "6",      "Food, Bioeconomy, Natural Resources, Agriculture and Environment","Food, Bioeconomy & Environment"),
    ("MISS-CROSS",  "",       "",                                              "Climate, Energy & Mobility"),
    ("MISS",        "",       "",                                              "Climate, Energy & Mobility"),
    ("HLTH",        "1",      "Health",                                        "Health & Life Sciences"),
    ("EITUM-BP",    "M-CIT",  "Climate-neutral & Smart Cities",               "Climate-neutral & Smart Cities"),
    ("EITUM",       "M-CIT",  "Climate-neutral & Smart Cities",               "Climate-neutral & Smart Cities"),
    ("EIC",         "",       "European Innovation Council",                  "SME, Entrepreneurship & Market Uptake"),
    ("EIE",         "",       "European Innovation Ecosystems",               "SME, Entrepreneurship & Market Uptake"),
    ("EIT",         "",       "European Institute of Innovation & Technology","SME, Entrepreneurship & Market Uptake"),
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
    ("JU-",         "5",      "Climate, Energy and Mobility",                 "Climate, Energy & Mobility"),
    ("ERC",         "",       "European Research Council",                    "Internships, fellowships & scholarships"),
    ("MSCA",        "",       "Marie Skłodowska-Curie Actions",               "Internships, fellowships & scholarships"),
    ("CL3-INFRA",   "3",      "Civil Security for Society",                   "Security & Resilience"),
    ("INFRA-TECH",  "4",      "Digital, Industry and Space",                  "Digital, Industry & Space"),
    ("INFRA-SERV",  "4",      "Digital, Industry and Space",                  "Digital, Industry & Space"),
    ("INFRA-EOSC",  "4",      "Digital, Industry and Space",                  "Digital, Industry & Space"),
    ("INFRA-DEV",   "4",      "Digital, Industry and Space",                  "Digital, Industry & Space"),
    ("INFRA",       "4",      "Research Infrastructures",                     "Digital, Industry & Space"),
    ("EURATOM",     "5",      "Climate, Energy and Mobility",                 "Climate, Energy & Mobility"),
    ("CID",         "5",      "Climate, Energy and Mobility",                 "Climate, Energy & Mobility"),
    ("EUROHPC",     "4",      "Digital, Industry and Space",                  "Digital, Industry & Space"),
    ("WIDERA",      "",       "Widening Participation & ERA",                 "Cross-cutting / Other"),
    ("NEB",         "M-CIT",  "New European Bauhaus",                         "Climate-neutral & Smart Cities"),
    ("RAISE",       "4",      "Digital, Industry and Space",                  "Digital, Industry & Space"),
]

PROGRAMME_THEMATIC_MAP = [
    ("European Defence Fund",           "Defence"),
    ("EDF",                             "Defence"),
    ("EU External Action",              "External Action & International Cooperation"),
    ("EU External Action-Prospect",     "External Action & International Cooperation"),
    ("EUBA",                            "External Action & International Cooperation"),
    ("Digital Europe",                  "Digital, Industry & Space"),
    ("EUAF",                            "Digital, Industry & Space"),
    ("PPPA",                            "Digital, Industry & Space"),
    ("Just Transition",                 "Climate, Energy & Mobility"),
    ("Innovation Fund",                 "Climate, Energy & Mobility"),
    ("Euratom",                         "Climate, Energy & Mobility"),
    ("Connecting Europe",               "Climate, Energy & Mobility"),
    ("RENEWFM",                         "Climate, Energy & Mobility"),
    ("RFCS",                            "Climate, Energy & Mobility"),
    ("EMFAF",                           "Food, Bioeconomy & Environment"),
    ("LIFE",                            "Food, Bioeconomy & Environment"),
    ("AGRIP",                           "Food, Bioeconomy & Environment"),
    ("Internal Security Fund",          "Security & Resilience"),
    ("ISF",                             "Security & Resilience"),
    ("UCPM",                            "Security & Resilience"),
    ("CERV",                            "Culture, Creativity & Inclusion"),
    ("Creative Europe",                 "Culture, Creativity & Inclusion"),
    ("Erasmus+",                        "Culture, Creativity & Inclusion"),
    ("European Social Fund+",           "Culture, Creativity & Inclusion"),
    ("European Solidarity Corps",       "Culture, Creativity & Inclusion"),
    ("SOCPL",                           "Culture, Creativity & Inclusion"),
    ("JUST",                            "Culture, Creativity & Inclusion"),
    ("Pericles IV",                     "Culture, Creativity & Inclusion"),
    ("Single Market Programme",         "SME, Entrepreneurship & Market Uptake"),
    ("I3",                              "SME, Entrepreneurship & Market Uptake"),
    ("Horizon Europe",                  None),
]

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

THEMATIC_MAP = {
    "1":"Health & Life Sciences","2":"Culture, Creativity & Inclusion",
    "3":"Security & Resilience","4":"Digital, Industry & Space",
    "5":"Climate, Energy & Mobility","6":"Food, Bioeconomy & Environment",
    "M-CIT":"Climate-neutral & Smart Cities",
    "M-OCEAN":"Healthy Oceans, Seas, Coastal & Inland Waters",
}

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

# Classification helpers 

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

# Structural classification functions 

def _topic_id(url: str) -> str:
    s = (url or "").upper().split("?")[0]
    for m in ["/TOPIC-DETAILS/", "/COMPETITIVE-CALLS-CS/", "/PROSPECT-DETAILS/"]:
        i = s.find(m)
        if i >= 0:
            return s[i + len(m):]
    return s


def _is_horizon_europe(prog: str, url: str, call_id: str) -> bool:
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
    tid = _topic_id(url)
    cid_up = (call_id or "").upper()

    m = RE_CLUSTER.search(cid_up) or RE_CLUSTER.search(tid)
    if m:
        cnum = m.group(1)
        thematic = HE_CLUSTER_MAP.get(cnum, "")
        clabel_map = {
            "1":"Health","2":"Culture, Creativity and Inclusion",
            "3":"Civil Security for Society","4":"Digital, Industry and Space",
            "5":"Climate, Energy and Mobility",
            "6":"Food, Bioeconomy, Natural Resources, Agriculture and Environment",
        }
        return cnum, clabel_map.get(cnum, ""), thematic

    import re as _re
    tid_norm = _re.sub(r"-20\d\d(?=-)", "", tid)
    cid_norm = _re.sub(r"-20\d\d(?=-)", "", cid_up)
    for prefix, cnum, clabel, thematic in HE_SUBPROGRAMME_MAP:
        if tid.startswith(prefix) or cid_up.startswith(prefix):
            return cnum, clabel, thematic
        if tid.startswith("HORIZON-" + prefix) or cid_up.startswith("HORIZON-" + prefix):
            return cnum, clabel, thematic
        if tid_norm.startswith(prefix) or cid_norm.startswith(prefix):
            return cnum, clabel, thematic
        if tid_norm.startswith("HORIZON-" + prefix) or cid_norm.startswith("HORIZON-" + prefix):
            return cnum, clabel, thematic
        if ("-" + prefix + "-") in tid or ("-" + prefix + "-") in cid_up:
            return cnum, clabel, thematic

    for prefix, subcode, c_num, c_label, thematic in URL_RULES:
        if prefix not in tid:
            continue
        if subcode is not None and subcode not in tid:
            continue
        return c_num, c_label, thematic

    return "", "", ""


def classify_non_he_by_programme(prog: str) -> str:
    pl = (prog or "").lower()
    for key, label in PROGRAMME_THEMATIC_MAP:
        if label is None:
            continue
        if key.lower() in pl:
            return label
    return ""


def classify_non_he_by_url(url: str) -> str:
    tid = _topic_id(url)
    for prefix, thematic in NON_HE_URL_PREFIX_MAP:
        if tid.startswith(prefix):
            return thematic
    return ""


def url_classify(url: str):
    tid = _topic_id(url)

    benef = None
    for key, hint in URL_BENEFICIARY_OVERRIDE.items():
        if key in tid:
            benef = hint
            break

    cnum, clabel, thematic = classify_horizon_europe(url, "")
    if thematic:
        return cnum, clabel, thematic, benef

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


def extract_parent_project(name: str, full_text: str) -> str:
    """
    Attempt to extract the name of the parent Horizon project from a cascade call.
    Checks the call title first (most reliable), then the first 500 chars of full_text.
    Returns an empty string if no match is found.
    """
    for src in [name or "", (full_text or "")[:500]]:
        m = RE_PARENT_PROJECT.search(src)
        if m:
            candidate = m.group(1).strip()
            # Discard generic words that match the regex but are not project names
            if candidate.upper() not in {"THE", "THIS", "EU", "EC", "AN", "A"}:
                return candidate
    return ""


# Date and budget parsing 

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


# Playwright utilities 

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
    print("  Waiting for SEDIA API response...")
    try:
        with page.expect_response(lambda r: "apiKey=SEDIA" in r.url and r.status == 200, timeout=timeout_ms) as response_info:
            data = response_info.value.json()
            count = data.get("totalResults")
            if count is not None:
                print(f"  Total detected from API: {count}")
                return int(count)
    except Exception as e:
        print(f"  API did not respond in time or blocked the request.")
    try:
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


# Enrichment via XHR 

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
    except: return None


def _enrich_one(page, row: dict) -> bool:
    url      = row["url"]
    captured = {}
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

                    raw_actions = meta.get("actions")
                    if isinstance(raw_actions, list): raw_actions = raw_actions[0] if raw_actions else None
                    if raw_actions and not _c.get("action_parsed"):
                        try:
                            acts = json.loads(raw_actions) if isinstance(raw_actions, str) else raw_actions
                            if isinstance(acts, list) and acts:
                                act0 = acts[0]
                                types = act0.get("types", [])
                                if types and not _c.get("action"):
                                    toa = types[0].get("typeOfAction", "")
                                    if toa:
                                        _c["action"] = toa
                                dl_dates = act0.get("deadlineDates", [])
                                if dl_dates and not _c.get("deadline"):
                                    _c["deadline"] = sorted(dl_dates)[-1]
                                pod = act0.get("plannedOpeningDate", "")
                                if pod and not _c.get("opening"):
                                    _c["opening"] = pod
                            _c["action_parsed"] = True
                        except Exception:
                            pass

                    if not _c.get("action"):
                        action_fb = _first(meta, "typesOfAction", "typeOfAction", "fundingScheme")
                        if action_fb:
                            _c["action"] = action_fb

                    if not _c.get("deadline"):
                        deadline_detail = _first(meta, "deadlineDate", "nextDeadline", "closingDate")
                        if deadline_detail:
                            _c["deadline"] = deadline_detail
                    if not _c.get("opening"):
                        opening_detail = _first(meta, "startDate", "openingDate", "publicationDate")
                        if opening_detail:
                            _c["opening"] = opening_detail

                    if not _c.get("budget"):
                        raw_overview = meta.get("budgetOverview")
                        if isinstance(raw_overview, list):
                            raw_overview = raw_overview[0] if raw_overview else None
                        if raw_overview:
                            try:
                                overview = json.loads(raw_overview) if isinstance(raw_overview, str) else raw_overview
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
        page.goto(url, wait_until="domcontentloaded", timeout=40000)
        t0 = time.time()
        while not captured and time.time() - t0 < 8:
            page.wait_for_timeout(400)

        try:
            body_text = page.locator("body").inner_text(timeout=5000)
        except Exception:
            body_text = ""

        budget_val_dom = extract_budget_per_project_dom(page, topic_id)

        if captured.get("full_text"):
            row["full_text"] = captured["full_text"]
        elif body_text and COOKIE_TEXT.lower() not in body_text.lower():
            row["full_text"] = clean(body_text) or ""
        else:
            row["full_text"] = ""

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

    if captured.get("full_text") and not row.get("full_text"):
        row["full_text"] = captured["full_text"]

    if captured.get("prog") and not row.get("programme_raw"):
        row["programme_raw"] = captured["prog"]
    if captured.get("action"):
        row["action_raw"] = captured["action"]
    if captured.get("call_id") and not row.get("call_id"):
        row["call_id"] = captured["call_id"]
    if captured.get("deadline"):
        row["deadline_raw"] = captured["deadline"]
    if captured.get("opening") and not row.get("opening_raw"):
        row["opening_raw"] = captured["opening"]

    return bool(
        row.get("full_text") or row.get("call_id") or
        row.get("deadline_raw") or row.get("budget_raw") or
        row.get("programme_raw")
    )


def enrich(ctx, rows: list):
    to_fix = [r for r in rows if r.get("url")]
    if not to_fix:
        print("  No calls with a URL to enrich.", flush=True)
        return

    print(f"  {len(to_fix)} calls to enrich (full_text + budget)...", flush=True)

    def _new_page():
        p = ctx.new_page()
        try:
            if hasattr(playwright_stealth, 'stealth_sync'):
                playwright_stealth.stealth_sync(p)
        except Exception:
            pass
        return p

    page = _new_page()
    skipped = 0

    for idx, row in enumerate(to_fix, 1):
        print(f"  [{idx:>4}/{len(to_fix)}] {(row['name'] or '')[:60]}", flush=True)

        ok = False
        for attempt in range(1, 4):
            try:
                ok = _enrich_one(page, row)
                break
            except Exception as e:
                print(f"    [attempt {attempt} failed] {e}", flush=True)
                try:
                    page.close()
                except Exception:
                    pass
                page = _new_page()
                time.sleep(3 * attempt)

        if not ok:
            skipped += 1
            print(f"    [SKIP] no data retrieved", flush=True)

        if idx % 100 == 0:
            print(f"  [checkpoint] {idx} calls processed...", flush=True)

        time.sleep(0.3)

    try:
        page.close()
    except Exception:
        pass
    print(f"  Enrichment complete. Skipped: {skipped}/{len(to_fix)}", flush=True)


# Transform a raw scraped row into a classified call object 

def to_call(row: dict) -> dict:
    url        = row.get("url", "")
    prog_raw   = row.get("programme_raw") or ""
    call_id    = row.get("call_id") or ""
    action_raw = row.get("action_raw") or ""

    # is_cascade is set by the scraping loop when the row comes from LIST_URL_CASCADE
    is_cascade = bool(row.get("_is_cascade", False))

    cluster_num = ""
    for src in [call_id, row.get("cluster_raw",""), url]:
        m = RE_CLUSTER.search(src or "")
        if m:
            cluster_num = m.group(1)
            break

    he_flag = _is_horizon_europe(prog_raw, url, call_id)
    cluster_label = ""
    thematic = ""

    if he_flag:
        cnum, clabel, th = classify_horizon_europe(url, call_id)
        if cnum:
            cluster_num = cnum
        if clabel:
            cluster_label = clabel
        if th:
            thematic = th
        if not thematic and cluster_num:
            thematic = HE_CLUSTER_MAP.get(cluster_num, "")
    else:
        thematic = classify_non_he_by_url(url)
        if not thematic:
            thematic = classify_non_he_by_programme(prog_raw)
        if not cluster_label and cluster_num:
            cluster_label = THEMATIC_MAP.get(cluster_num, "")

    if not thematic:
        thematic = name_classify(row.get("name", ""))

    if not cluster_num and cluster_label:
        _inv = {v: k for k, v in THEMATIC_MAP.items()}
        cluster_num = _inv.get(cluster_label, "")

    u_cnum, u_clabel, u_thematic, u_benef = url_classify(url)
    if not cluster_label and u_clabel:
        cluster_label = u_clabel

    action     = normalize_action(action_raw)
    is_mission = bool("/HORIZON-MISS" in url.upper())

    opening_raw  = row.get("opening_raw") or ""
    deadline_raw = row.get("deadline_raw") or ""

    full_text = row.get("full_text") or ""
    multi = classify_multitopic(row.get("name") or "", full_text, thematic)

    GENERIC_THEMATICS = {"Cross-cutting / Other", ""}
    PROMOTION_EXEMPT = {
        "Internships, fellowships & scholarships",
    }
    effective_thematic = thematic
    if effective_thematic in GENERIC_THEMATICS and multi["multi_thematic"]:
        for candidate in multi["multi_thematic"]:
            if candidate not in GENERIC_THEMATICS and candidate not in PROMOTION_EXEMPT:
                effective_thematic = candidate
                break

    all_thematics = list(multi["multi_thematic"])
    if effective_thematic and effective_thematic not in all_thematics:
        all_thematics.insert(0, effective_thematic)

    # For cascade calls, attempt to extract the parent project name.
    # This is done after full_text is available from enrichment.
    parent_project = ""
    if is_cascade:
        parent_project = extract_parent_project(row.get("name") or "", full_text)

    return {
        "name":             row.get("name") or "",
        "call_id":          call_id,
        "programme":        prog_raw,
        "cluster_num":      cluster_num,
        "cluster_label":    cluster_label,
        "thematic_cluster": effective_thematic,
        "action":           action,
        "opening":          parse_date_iso(opening_raw),
        "deadline":         parse_date_iso(deadline_raw),
        "url":              url,
        "is_mission":       is_mission,
        # NEW: cascade funding fields
        "is_cascade":       is_cascade,
        "parent_project":   parent_project,
        "beneficiary_hint": beneficiary_hint(action, prog_raw, u_benef),
        "budget":           int(row.get("budget_raw") or 0),
        "budget_total":     int(row.get("budget_total_raw") or row.get("budget_raw") or 0),
        "expected_grants":  row.get("expected_grants"),
        "full_text":        multi["full_text"],
        "keyword_hits":     multi["keyword_hits"],
        "multi_thematic":   all_thematics,
        "is_special_basic_research": multi["is_special_basic_research"],
    }


# Changelog 

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
            k = c.get("thematic_cluster") or "(unclassified)"
            tc[k] = tc.get(k, 0) + 1
        return tc

    date_str = generated[:10]

    # Count cascade vs direct calls for the summary
    n_cascade = sum(1 for c in new_calls if c.get("is_cascade"))
    n_direct  = len(new_calls) - n_cascade

    lines = []
    lines.append(f"# Changelog calls.json")
    lines.append(f"")
    lines.append(f"**Last updated:** {generated.replace('T',' ').replace('+00:00',' UTC')[:22]}")
    lines.append(f"")
    lines.append(f"## Summary")
    lines.append(f"")
    lines.append(f"| | Count |")
    lines.append(f"|---|---|")
    lines.append(f"| Total calls (new) | {len(new_calls)} |")
    lines.append(f"| — of which direct calls | {n_direct} |")
    lines.append(f"| — of which cascade / FSTP | {n_cascade} |")
    lines.append(f"| Total calls (previous) | {len(old_calls)} |")
    lines.append(f"| **New calls added** | **{len(added)}** |")
    lines.append(f"| Calls removed (expired/closed) | {len(removed)} |")
    lines.append(f"")

    if added:
        lines.append(f"## Calls added ({len(added)})")
        lines.append(f"")
        by_thematic = {}
        for c in added:
            t = c.get("thematic_cluster") or "(unclassified)"
            by_thematic.setdefault(t, []).append(c)
        for thematic, calls in sorted(by_thematic.items()):
            lines.append(f"### {thematic} ({len(calls)})")
            lines.append(f"")
            for c in calls:
                name    = c.get("name") or "(unnamed)"
                prog    = c.get("programme") or ""
                action  = c.get("action") or ""
                dead    = c.get("deadline") or ""
                url     = c.get("url") or ""
                cascade_tag = " `[CASCADE]`" if c.get("is_cascade") else ""
                parent  = f" · Parent: {c['parent_project']}" if c.get("parent_project") else ""
                meta = " · ".join(filter(None, [prog, action, f"Deadline: {dead}" if dead else ""]))
                lines.append(f"- **{name}**{cascade_tag}")
                if meta or parent:
                    lines.append(f"  {meta}{parent}")
                if url:
                    lines.append(f"  {url}")
                lines.append(f"")
    else:
        lines.append(f"## Calls added")
        lines.append(f"")
        lines.append(f"No new calls compared to the previous snapshot.")
        lines.append(f"")

    if removed:
        lines.append(f"## Calls removed ({len(removed)})")
        lines.append(f"")
        for c in removed:
            name = c.get("name") or "(unnamed)"
            prog = c.get("programme") or ""
            dead = c.get("deadline") or ""
            meta = " · ".join(filter(None, [prog, f"Deadline: {dead}" if dead else ""]))
            lines.append(f"- **{name}**{(' — ' + meta) if meta else ''}")
        lines.append(f"")

    lines.append(f"## Distribution by thematic area (new dataset)")
    lines.append(f"")
    lines.append(f"| Thematic area | Calls |")
    lines.append(f"|---|---|")
    for k, v in sorted(thematic_counts(new_calls).items(), key=lambda x: -x[1]):
        lines.append(f"| {k} | {v} |")
    lines.append(f"")

    changelog_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n  Changelog written: {changelog_path} (+{len(added)} added, -{len(removed)} removed)")

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
            "# calls.json update history\n\n"
            "| Date | Total calls | Added | Removed |\n"
            "|---|---|---|---|\n"
            + history_line + "\n"
        )
        history_path.write_text(header, encoding="utf-8")
    print(f"  History updated: {history_path}")


# Listing loop (reusable for both direct and cascade feeds) 

def _scrape_listing(page, list_url_template: str, is_cascade: bool, seen_urls: set) -> list:
    """
    Scrapes all pages of a single listing feed (direct or cascade) and returns
    a list of raw rows. seen_urls is updated in place for cross-feed deduplication.
    """
    rows = []
    label = "CASCADE" if is_cascade else "DIRECT"

    # --- detect total ---
    total_captured = {}

    def handle_first(response, _tc=total_captured):
        if SEARCH_API in response.url and response.status == 200 and "total" not in _tc:
            try:
                body = response.json()
                t = body.get("totalResults")
                if t is not None:
                    _tc["total"] = int(t)
                    print(f"  [{label}] Total detected from API: {t}")
            except Exception:
                pass

    page.on("response", handle_first)
    page.goto(list_url_template.format(page=1, ps=PAGE_SIZE),
              wait_until="domcontentloaded", timeout=90000)

    try:
        cookie_button = page.get_by_role("button", name="Accept all cookies")
        if cookie_button.is_visible():
            cookie_button.click()
            page.wait_for_timeout(4000)
    except:
        pass

    deadline_init = time.time() + 30
    while "total" not in total_captured and time.time() < deadline_init:
        page.wait_for_timeout(500)

    page.remove_listener("response", handle_first)

    total = total_captured.get("total") or read_total(page)
    if total is None:
        print(f"  [{label}] Could not read the call counter. Skipping feed.")
        return rows

    max_pages = math.ceil(total / PAGE_SIZE)
    print(f"  [{label}] {total} calls | {max_pages} pages")

    for pnum in range(1, max_pages + 1):
        remaining = total - (pnum - 1) * PAGE_SIZE
        expected  = min(PAGE_SIZE, remaining)
        url = list_url_template.format(page=pnum, ps=PAGE_SIZE)
        print(f"\n  [{label} p{pnum}/{max_pages}] expected ~{expected}", end="", flush=True)

        page_results = []
        import threading as _threading
        _debounce_timer: list = [None]

        def _mark_done(_pr=page_results, _dt=_debounce_timer):
            _dt.append("done")

        def handle_list(response, _pr=page_results, _dt=_debounce_timer):
            if SEARCH_API not in response.url or response.status != 200:
                return
            try:
                body = response.json()
            except Exception:
                return
            try:
                results = body.get("results", [])
                if not results:
                    return
                print(f" [API p{body.get('pageNumber','?')}: {len(results)}]", end="", flush=True)
                for item in results:
                    ref = item.get("reference", "")
                    if not ref:
                        continue
                    meta = item.get("metadata", {}) or {}
                    full_url = (
                        item.get("url")
                        or _first(meta, "url", "esST_URL")
                        or ""
                    )
                    if not full_url:
                        cid_tmp = _first(meta, "identifier", "callIdentifier") or ref
                        # Cascade calls live under /competitive-calls-cs/
                        if is_cascade:
                            full_url = COMPETITIVE_BASE_URL + cid_tmp
                        else:
                            full_url = TOPIC_BASE_URL + cid_tmp

                    prog_id      = _first(meta, "frameworkProgramme", "programme")
                    action       = _first(meta, "typesOfAction", "typeOfAction", "fundingScheme")
                    cid          = _first(meta, "identifier", "callIdentifier")
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
                        "_is_cascade":   is_cascade,   # tag the row so to_call() knows
                    })

                old = _dt[0]
                if old is not None:
                    old.cancel()
                t = _threading.Timer(2.0, _mark_done)
                _dt[0] = t
                t.start()

            except Exception as e:
                print(f"\n    [WARN parse API {label} p{pnum}] {e}", flush=True)

        page.on("response", handle_list)
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=90000)
            accept_cookies(page)
            deadline_t = time.time() + 45
            while len(page_results) < expected and time.time() < deadline_t:
                if len(_debounce_timer) > 1:
                    break
                page.wait_for_timeout(300)
            if _debounce_timer[0] is not None:
                _debounce_timer[0].cancel()
            if len(page_results) == 0:
                print(f"  No API response for {label} page {pnum}", flush=True)
        finally:
            page.remove_listener("response", handle_list)

        new_items = [r for r in page_results if r.get("_ref") not in seen_urls]
        print(f" -> {len(new_items)} new", flush=True)

        for r in new_items:
            seen_urls.add(r["_ref"])
            rows.append(r)
        time.sleep(0.8)

    # --- second pass for missing items ---
    missing = total - len(rows)
    if missing > 0:
        print(f"\n  [{label}] {missing} calls missing. Second pass...", flush=True)
        for pg in range(1, max_pages + 1):
            if len(rows) >= total:
                break
            url2 = list_url_template.format(page=pg, ps=PAGE_SIZE)
            page_results2 = []

            def handle_recovery(response, _pr=page_results2):
                if SEARCH_API not in response.url or response.status != 200:
                    return
                try:
                    body = response.json()
                except Exception:
                    return
                try:
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
                while len(page_results2) == 0 and time.time() - t0 < 25:
                    page.wait_for_timeout(400)
                if len(page_results2) == 0:
                    page.reload(wait_until="domcontentloaded", timeout=60000)
                    t1 = time.time()
                    while len(page_results2) == 0 and time.time() - t1 < 20:
                        page.wait_for_timeout(400)
            finally:
                page.remove_listener("response", handle_recovery)
            time.sleep(0.8)

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
                    "_is_cascade":   is_cascade,
                })
                print(f"   [{label} recovery] {clean(title) or ref}", flush=True)

        still_missing = total - len(rows)
        print(f"  [{label}] After recovery: {len(rows)}/{total} (still missing: {still_missing})", flush=True)

    return rows


# Main 

def main(out_path: Path):
    rows      = []
    seen_urls = set()   # shared across both feeds for cross-feed deduplication

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
                print("  Could not apply stealth, continuing anyway...")
        except Exception as e:
            print(f"  Stealth error ignored: {e}")

        # Step 1a: direct calls 
        print("\n=== Step 1a: scraping direct calls ===", flush=True)
        direct_rows = _scrape_listing(page, LIST_URL, is_cascade=False, seen_urls=seen_urls)
        rows.extend(direct_rows)
        print(f"\n  Direct calls collected: {len(direct_rows)}", flush=True)

        # Step 1b: cascade / FSTP calls 
        print("\n=== Step 1b: scraping cascade / FSTP calls ===", flush=True)
        cascade_rows = _scrape_listing(page, LIST_URL_CASCADE, is_cascade=True, seen_urls=seen_urls)
        rows.extend(cascade_rows)
        print(f"\n  Cascade calls collected: {len(cascade_rows)}", flush=True)

        # Step 2: enrichment (all rows together, single browser context) 
        print(f"\n=== Step 2: enriching {len(rows)} calls in total ===", flush=True)
        enrich(ctx, rows)
        browser.close()

    # Classification and output 
    calls = []
    seen_refs = set()
    seen_call_urls = set()
    for row in rows:
        call = to_call(row)
        ref_key = row.get("_ref") or ""
        url_key = call.get("url") or ""
        if ref_key and ref_key in seen_refs:
            continue
        if not ref_key and url_key and url_key in seen_call_urls:
            continue
        if ref_key:
            seen_refs.add(ref_key)
        if url_key:
            seen_call_urls.add(url_key)
        calls.append(call)

    # Summary 
    tc = {}
    n_cascade_out = sum(1 for c in calls if c.get("is_cascade"))
    for c in calls:
        k = c["thematic_cluster"] or "(unclassified)"
        tc[k] = tc.get(k, 0) + 1
    print(f"\nClassification ({len(calls)} calls total, {n_cascade_out} cascade):")
    for k, v in sorted(tc.items(), key=lambda x: -x[1]):
        print(f"  {v:5d}  {k}")
    print(f"\nUnclassified: {tc.get('(unclassified)', 0)}")

    generated = datetime.now(timezone.utc).isoformat()

    old_calls = []
    if out_path.exists():
        try:
            old_data = json.loads(out_path.read_text(encoding="utf-8"))
            old_calls = old_data.get("calls", [])
            print(f"\nPrevious dataset: {len(old_calls)} calls")
        except Exception:
            print("\nNo previous dataset found.")

    changelog_path = out_path.parent / "changelog.md"
    write_changelog(old_calls, calls, changelog_path, generated)

    payload = {
        "generated": generated,
        "calls": calls,
    }
    out_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"\n  Written {out_path} with {len(calls)} calls ({n_cascade_out} cascade)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="calls.json", help="Output JSON file path")
    args = parser.parse_args()
    main(Path(args.out))



























































































