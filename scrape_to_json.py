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

LIST_URL = (
    "https://ec.europa.eu/info/funding-tenders/opportunities/portal/screen"
    "/opportunities/calls-for-proposals"
    "?order=DESC&pageNumber={page}&pageSize={ps}&sortBy=startDate"
    "&isExactMatch=true&type=1&status=31094501,31094502&programmePeriod=2021%20-%202027"
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
# Source: official Horizon Europe Work Programme structure
HE_CLUSTER_MAP = {
    "1": "Health & Life Sciences",
    "2": "Culture, Creativity & Inclusion",
    "3": "Security & Resilience",
    "4": "Digital, Industry & Space",
    "5": "Climate, Energy & Mobility",
    "6": "Food, Bioeconomy & Environment",
}

# Horizon Europe: mapping for non-CL sub-programmes (topic ID prefix)
# Order: from most specific to most generic.
# (prefix_in_url_or_callid, cluster_num, cluster_label, thematic_area)
HE_SUBPROGRAMME_MAP = [
    #  Missions 
    ("MISS-CIT",    "M-CIT",  "Climate-neutral & Smart Cities",                "Climate-neutral & Smart Cities"),
    ("MISS-OCEAN",  "M-OCEAN","Healthy Oceans, Seas, Coastal & Inland Waters", "Healthy Oceans, Seas, Coastal & Inland Waters"),
    ("MISS-CLIMA",  "5",      "Climate, Energy and Mobility",                  "Climate, Energy & Mobility"),
    ("MISS-CANCER", "1",      "Health",                                        "Health & Life Sciences"),
    ("MISS-SOIL",   "6",      "Food, Bioeconomy, Natural Resources, Agriculture and Environment","Food, Bioeconomy & Environment"),
    ("MISS-CROSS",  "",       "",                                              "Climate, Energy & Mobility"),   # Adaptation Mission cross-cutting → CL5
    ("MISS",        "",       "",                                              "Climate, Energy & Mobility"),   # generic MISS fallback

    #  Health cluster (explicit prefix) 
    ("HLTH",        "1",      "Health",                                        "Health & Life Sciences"),

    #  EIC / EIT / EIE 
    ("EITUM-BP",    "M-CIT",  "Climate-neutral & Smart Cities",               "Climate-neutral & Smart Cities"),
    ("EITUM",       "M-CIT",  "Climate-neutral & Smart Cities",               "Climate-neutral & Smart Cities"),
    ("EIC",         "",       "European Innovation Council",                  "SME, Entrepreneurship & Market Uptake"),
    ("EIE",         "",       "European Innovation Ecosystems",               "SME, Entrepreneurship & Market Uptake"),
    ("EIT",         "",       "European Institute of Innovation & Technology","SME, Entrepreneurship & Market Uptake"),

    #  JU – Joint Undertakings 
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

    #  ERC 
    # ERC is frontier research — classified by scientific domain from title/text;
    # without domain info we use "Internships, fellowships & scholarships"
    ("ERC",         "",       "European Research Council",                    "Internships, fellowships & scholarships"),

    #  MSCA 
    ("MSCA",        "",       "Marie Skłodowska-Curie Actions",               "Internships, fellowships & scholarships"),

    #  Research Infrastructures 
    ("CL3-INFRA",   "3",      "Civil Security for Society",                   "Security & Resilience"),
    ("INFRA-TECH",  "4",      "Digital, Industry and Space",                  "Digital, Industry & Space"),
    ("INFRA-SERV",  "4",      "Digital, Industry and Space",                  "Digital, Industry & Space"),
    ("INFRA-EOSC",  "4",      "Digital, Industry and Space",                  "Digital, Industry & Space"),   # EOSC is digital infrastructure
    ("INFRA-DEV",   "4",      "Digital, Industry and Space",                  "Digital, Industry & Space"),
    ("INFRA",       "4",      "Research Infrastructures",                     "Digital, Industry & Space"),   # generic INFRA → CL4 (INFRA is mainly digital/data)

    #  Euratom / Nuclear 
    ("EURATOM",     "5",      "Climate, Energy and Mobility",                 "Climate, Energy & Mobility"),
    ("CID",         "5",      "Climate, Energy and Mobility",                 "Climate, Energy & Mobility"),

    #  EUROHPC 
    ("EUROHPC",     "4",      "Digital, Industry and Space",                  "Digital, Industry & Space"),

    #  Widening / NEB 
    ("WIDERA",      "",       "Widening Participation & ERA",                 "Cross-cutting / Other"),   # genuine cross-cutting support programme
    ("NEB",         "M-CIT",  "New European Bauhaus",                         "Climate-neutral & Smart Cities"),

    #  RAISE 
    ("RAISE",       "4",      "Digital, Industry and Space",                  "Digital, Industry & Space"),
]

# Non-Horizon: mapping by programme name string or numeric ID 
# Order: from most specific to most generic.
# (substring_in_programme_name, thematic_area)
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

# Keyword rules for calls whose programme ID is purely numeric;
# match against the call title/name to infer the thematic area
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

# Hard-coded beneficiary type overrides keyed on URL topic-ID prefix
URL_BENEFICIARY_OVERRIDE = {
    "MSCA":  ["Research organisation"],
    "INFRA": ["Research organisation"],
    "EUBA":  ["Public body"],
}

# Thematic label used for ERC/MSCA and fellowship-type calls
SPECIAL_BASIC_RESEARCH_CATEGORY = "Internships, fellowships & scholarships"
# Keywords in a call title that indicate it is a fellowship or scholarship call
SPECIAL_TITLE_KEYWORDS = ["internship","internships","fellowship","fellowships","msca","scholarship","scholarships"]
# Per-thematic keyword lists used for full-text classification of calls
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
    """Escape a string for use in a regular expression."""
    return re.escape(s or "")

def text_has_keyword(text: str, keyword: str) -> bool:
    """Return True if the keyword appears as a whole word in text (case-insensitive)."""
    return bool(re.search(rf"(?<![A-Za-z]){escape_rx(keyword.lower())}(?![A-Za-z])", (text or "").lower()))

def keyword_hits_for_thematic(text: str, thematic: str):
    """Return the list of topic keywords for a thematic area that appear in text."""
    hits = []
    for kw in TOPIC_KEYWORDS.get(thematic, []):
        if text_has_keyword(text, kw):
            hits.append(kw)
    return list(dict.fromkeys(hits))


def keyword_hits_in_title(title: str) -> dict:
    """
    Scan the call title against every thematic keyword list and return a dict
    mapping thematic_area -> [matched_keywords].

    This is used to compute ``title_match_score``: calls whose title contains
    the searched keyword rank higher than calls that only mention it in the
    body text, even when the consumer searches across the full ``full_text``
    field.  The result is stored as ``title_keyword_hits`` in the output JSON
    so that any downstream client (API, front-end, script) can apply the
    title-boost without re-implementing the keyword logic.

    Example output:
        {
            "Digital, Industry & Space": ["digital", "quantum"],
            "Climate, Energy & Mobility": ["energy"]
        }
    """
    title_lower = (title or "").lower()
    hits: dict = {}
    for thematic, keywords in TOPIC_KEYWORDS.items():
        matched = [kw for kw in keywords if text_has_keyword(title_lower, kw)]
        if matched:
            hits[thematic] = list(dict.fromkeys(matched))
    return hits


def title_is_special_basic_research(title: str) -> bool:
    """Return True if the title contains keywords typical of fellowships or scholarship calls."""
    tl = (title or "").lower()
    return any(text_has_keyword(tl, kw) for kw in SPECIAL_TITLE_KEYWORDS)

def classify_multitopic(name: str, full_text: str, thematic: str):
    """
    Scan full_text for keywords across all thematic areas and return a dict
    with hits and matched themes.

    New fields added for title-based ranking:
      - ``title_keyword_hits``  (dict)  thematic -> keywords found in the title only
      - ``title_match_score``   (int)   total number of distinct keywords found in
                                        the title; used by consumers to sort
                                        title-matched calls above body-only matches
                                        without altering the existing ranking logic.
    """
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

    # --- Title-level keyword scan -------------------------------------------
    # Run the same keyword matching restricted to the call *title* (name).
    # This produces a lighter-weight signal than full_text hits: a keyword
    # present in the title is a much stronger relevance indicator than one
    # buried in the body.  Downstream search/ranking code can use
    # ``title_match_score > 0`` as a boost tier without touching the existing
    # ``multi_thematic`` / ``keyword_hits`` fields.
    title_kw_hits = keyword_hits_in_title(name)

    # title_match_score = total unique keywords matched in the title across all
    # thematic areas.  A score of 0 means "keyword only in body text";
    # any positive value means "keyword appears in the call title".
    title_match_score = sum(len(v) for v in title_kw_hits.values())
    # ------------------------------------------------------------------------

    return {
        "full_text":           text,
        "keyword_hits":        keyword_hits,
        "multi_thematic":      multi_thematic,
        "is_special_basic_research": special,
        # New fields — title-level relevance signals
        "title_keyword_hits":  title_kw_hits,
        "title_match_score":   title_match_score,
    }

# Structural classification functions 

def _topic_id(url: str) -> str:
    """Extract the topic ID from a URL (everything after /topic-details/ or /competitive-calls-cs/)."""
    s = (url or "").upper().split("?")[0]
    for m in ["/TOPIC-DETAILS/", "/COMPETITIVE-CALLS-CS/", "/PROSPECT-DETAILS/"]:
        i = s.find(m)
        if i >= 0:
            return s[i + len(m):]
    return s


def _is_horizon_europe(prog: str, url: str, call_id: str) -> bool:
    """Return True if the call belongs to Horizon Europe (including JU, ERC, MSCA, etc.)."""
    prog_l = (prog or "").lower()
    if "horizon" in prog_l:
        return True
    tid = _topic_id(url)
    # All known topic-ID prefixes that belong to Horizon Europe
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
    Structural classification for Horizon Europe calls.

    Cascade logic:
      1. Explicit CL1-6 cluster in call_id or URL  -> use HE_CLUSTER_MAP
      2. Known sub-programme (HE_SUBPROGRAMME_MAP) -> use that entry
      3. Legacy URL_RULES fallback                  -> use that entry
      4. Unclassified                               -> ("", "", "")

    Returns (cluster_num, cluster_label, thematic)
    """
    tid = _topic_id(url)
    cid_up = (call_id or "").upper()

    # 1. Structural CL1-6: search for HORIZON-CL<N> or CL<N>- in call_id/URL 
    m = RE_CLUSTER.search(cid_up) or RE_CLUSTER.search(tid)
    if m:
        cnum = m.group(1)
        thematic = HE_CLUSTER_MAP.get(cnum, "")
        # Refine cluster_label using the full name from THEMATIC_MAP
        clabel_map = {
            "1":"Health","2":"Culture, Creativity and Inclusion",
            "3":"Civil Security for Society","4":"Digital, Industry and Space",
            "5":"Climate, Energy and Mobility",
            "6":"Food, Bioeconomy, Natural Resources, Agriculture and Environment",
        }
        return cnum, clabel_map.get(cnum, ""), thematic

    # 2. Sub-programme via HE_SUBPROGRAMME_MAP 
    # Important order: more specific entries (MISS-CIT, MISS-OCEAN, ...) come
    # before generic ones (MISS) in the table.
    # Problem: for MISS topics the actual format is HORIZON-MISS-{YEAR}-{SUBCODE}-...
    # so "MISS-CIT" never appears as a contiguous substring.
    # Solution: normalize tid by removing purely numeric year segments
    # (years like 2026, 2027) before matching.
    import re as _re
    # Strip 4-digit year segments so "HORIZON-MISS-2026-CIT" becomes "HORIZON-MISS-CIT"
    tid_norm = _re.sub(r"-20\d\d(?=-)", "", tid)
    cid_norm = _re.sub(r"-20\d\d(?=-)", "", cid_up)  # same normalisation for the call ID
    for prefix, cnum, clabel, thematic in HE_SUBPROGRAMME_MAP:
        # a) Direct match on the original tid
        if tid.startswith(prefix) or cid_up.startswith(prefix):
            return cnum, clabel, thematic
        # b) Match with HORIZON- prefix
        if tid.startswith("HORIZON-" + prefix) or cid_up.startswith("HORIZON-" + prefix):
            return cnum, clabel, thematic
        # c) Match on the normalised tid (year removed)
        if tid_norm.startswith(prefix) or cid_norm.startswith(prefix):
            return cnum, clabel, thematic
        if tid_norm.startswith("HORIZON-" + prefix) or cid_norm.startswith("HORIZON-" + prefix):
            return cnum, clabel, thematic
        # d) Prefix present as an internal segment (e.g. -MSCA- inside HORIZON-MSCA-2026-)
        if ("-" + prefix + "-") in tid or ("-" + prefix + "-") in cid_up:
            return cnum, clabel, thematic

    #  3. Fallback: URL_RULES legacy 
    for prefix, subcode, c_num, c_label, thematic in URL_RULES:
        if prefix not in tid:
            continue
        if subcode is not None and subcode not in tid:
            continue
        return c_num, c_label, thematic

    return "", "", ""


def classify_non_he_by_programme(prog: str) -> str:
    """
    For non-HE programmes: derive the thematic_cluster from the programme name
    using PROGRAMME_THEMATIC_MAP (ordered from most to least specific).
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
    For non-HE programmes: derive the thematic_cluster from the topic ID prefix
    in the URL, using NON_HE_URL_PREFIX_MAP.
    """
    tid = _topic_id(url)
    for prefix, thematic in NON_HE_URL_PREFIX_MAP:
        if tid.startswith(prefix):
            return thematic
    return ""


def url_classify(url: str):
    """
    Legacy wrapper: applies the new structural logic for Horizon Europe and
    non-HE programmes; falls back to old URL_RULES as a safety net.
    Returns (cluster_num, cluster_label, thematic, beneficiary_hint_or_None).
    """
    tid = _topic_id(url)

    # Beneficiary hint based on the URL prefix (unchanged logic)
    benef = None
    for key, hint in URL_BENEFICIARY_OVERRIDE.items():
        if key in tid:
            benef = hint
            break

    # Try structural HE classification
    cnum, clabel, thematic = classify_horizon_europe(url, "")
    if thematic:
        return cnum, clabel, thematic, benef

    # Try non-HE classification by URL prefix
    thematic_non_he = classify_non_he_by_url(url)
    if thematic_non_he:
        return "", "", thematic_non_he, benef

    return "", "", "", benef


def name_classify(name: str) -> str:
    """Derive a thematic area from the call name using NUMERIC_ID_NAME_RULES keyword matching."""
    name_up = (name or "").upper()
    for keyword, thematic in NUMERIC_ID_NAME_RULES:
        if keyword.upper() in name_up:
            return thematic
    return ""


def prog_thematic(prog: str) -> str:
    """Return the thematic area for a given programme name string."""
    return classify_non_he_by_programme(prog)


def resolve_thematic(cluster_num: str, prog: str) -> str:
    """Return the thematic area from a cluster number if available, otherwise from the programme name."""
    if cluster_num and THEMATIC_MAP.get(cluster_num):
        return THEMATIC_MAP[cluster_num]
    return prog_thematic(prog)

def normalize_action(v: str) -> str:
    """Normalise a raw action type string to a standard short code (RIA, IA, CSA, COFUND, etc.)."""
    s = (v or "").lower()
    if "research and innovation action" in s: return "RIA"
    if "innovation action" in s:              return "IA"
    if "coordination and support" in s:       return "CSA"
    if "cofund" in s:                         return "COFUND"
    # Direct abbreviations (already normalised)
    u = (v or "").strip().upper()
    if u in ("RIA", "HORIZON-RIA"):  return "RIA"
    if u in ("IA",  "HORIZON-IA"):   return "IA"
    if u in ("CSA", "HORIZON-CSA"):  return "CSA"
    if u in ("COFUND", "HORIZON-COFUND"): return "COFUND"
    # If none of the known patterns matched, return the raw value unchanged
    return v or ""

def beneficiary_hint(action: str, prog: str, url_benef):
    """Return a list of likely beneficiary types based on the action type and programme name."""
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

# Date and budget parsing 

def parse_date_iso(s: str) -> str:
    """Parse a date string in various formats and return it as an ISO 8601 string (YYYY-MM-DD)."""
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
    # Could not parse the date; return empty string so downstream code can detect it
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
        return 0  # give up and signal "no budget" with 0

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
    # The largest figure is most likely the total call budget (not a per-project cap)
    return max(candidates)

#  Playwright utilities 

def clean(s):
    """Strip and collapse whitespace in a string; return None if the result is empty."""
    if not s:
        return None
    s = re.sub(r"\s+", " ", str(s)).strip()
    return s or None

def pick(rx, text):
    """Apply a regex to text and return the first capture group (cleaned), or None if no match."""
    m = rx.search(text or "")
    return clean(m.group(1)) if m else None

def accept_cookies(page):
    """Click the cookie acceptance button on the page if it is visible."""
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
    """Poll the page until the cookie banner text disappears or the timeout is reached."""
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
    """Return the number of call links currently visible on the page."""
    return page.locator(LINK_SELECTOR).count()

def read_total(page, timeout_ms=30000):
    """Read the total number of results from the SEDIA API response; falls back to a CSS selector."""
    print("  Waiting for SEDIA API response...")
    try:
        # Wait specifically for an API response matching the SEDIA key
        with page.expect_response(lambda r: "apiKey=SEDIA" in r.url and r.status == 200, timeout=timeout_ms) as response_info:
            data = response_info.value.json()
            # The exact field name in the new system is 'totalResults'
            count = data.get("totalResults")
            if count is not None:
                print(f"  Total detected from API: {count}")
                return int(count)
    except Exception as e:
        print(f"  API did not respond in time or blocked the request.")
        
    # FALLBACK: if the API fails, try reading the new CSS selector
    try:
        # In v1.0.15 the count is often inside a class like 'wt-count'
        page.wait_for_selector(".ecl-u-type-bold", timeout=5000) 
        txt = page.locator("body").inner_text()
        m = re.search(r"(\d[\d,\.]*)\s*(?:results?|items?|found)", txt, re.I)
        if m:
            return int(m.group(1).replace(",", "").replace(".", ""))
    except:
        pass
    return None
        
        
def scroll_until(page, expected, max_ms=50000):
    """Scroll the page until the expected number of call links are loaded or the timeout expires."""
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
            stable_since = time.time()  # reset stability timer whenever count changes
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
    """Extract all unique call URLs from the links currently on the page."""
    hrefs = page.evaluate(f"""
        () => Array.from(document.querySelectorAll('{LINK_SELECTOR}'))
                  .map(a => a.getAttribute('href'))
    """)
    out, seen = [], set()
    for h in hrefs or []:
        if not h:
            continue
        full = "https://ec.europa.eu" + h if h.startswith("/") else h  # make relative URLs absolute
        if full not in seen:
            seen.add(full)
            out.append(full)
    return out

# Parse a call card from the listing page 

def parse_card(page, full_url: str) -> dict:
    """Extract basic metadata (title, dates, programme, action) from a call card on the listing page."""
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
    """Return the first non-empty value from a metadata dict for the given list of keys."""
    for k in keys:
        v = meta.get(k)
        if isinstance(v, list) and v:
            return re.sub(r"\s+", " ", str(v[0])).strip()
        if v and isinstance(v, str):
            return v.strip()
    return ""

def extract_budget_per_project_dom(page, topic_id):
    """Row-hunter logic: expand the topic conditions table in the DOM and extract the per-project budget."""
    parts = topic_id.split('?')[0].split('-')
    target_match = "-".join(parts[-2:]) if len(parts) > 1 else parts[-1]
    try:
        # Expand the 'Topic conditions' section
        btn = page.locator("button:has-text('Topic conditions and documents')").first
        if btn.count() > 0:
            btn.scroll_into_view_if_needed()
            if btn.get_attribute("aria-expanded") == "false":
                btn.click(force=True)
                page.wait_for_timeout(3500)
        
        # Scroll to the specific row for the ID
        row_locator = page.locator(f"tr:has-text('{target_match}')").first
        if row_locator.count() > 0:
            row_locator.scroll_into_view_if_needed()
            page.wait_for_timeout(1000)
            
        # Extract the value via JavaScript
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
    Opens a detail page, captures missing fields via XHR (response handler)
    and integrates the new DOM logic for precise budget extraction.
    """
    url      = row["url"]
    # dict that accumulates data from XHR responses intercepted during page load
    captured = {}
    # Extract topic_id from the URL for the row-hunter logic
    topic_id = url.split('/')[-1].split('?')[0]

    def handle(response, _c=captured):
        if SEARCH_API in response.url and response.status == 200:
            try:
                body = response.json()
                for item in body.get("results", [body]):
                    meta    = item.get("metadata", {}) or {}
                    prog_id = _first(meta, "frameworkProgramme", "programme")
                    cid     = _first(meta, "identifier", "callIdentifier")

                    if prog_id and not _c.get("prog"):
                        _c["prog"] = PROGRAMME_MAP.get(prog_id, prog_id)
                    if cid and not _c.get("call_id"):
                        _c["call_id"] = cid

                    # Parse the `actions` field (most reliable source for action type and deadline) 
                    # "actions" is a JSON string: [{"types":[{"typeOfAction":"..."}], "deadlineDates":[...], "plannedOpeningDate":"..."}]
                    raw_actions = meta.get("actions")
                    if isinstance(raw_actions, list): raw_actions = raw_actions[0] if raw_actions else None
                    if raw_actions and not _c.get("action_parsed"):
                        try:
                            acts = json.loads(raw_actions) if isinstance(raw_actions, str) else raw_actions
                            if isinstance(acts, list) and acts:
                                act0 = acts[0]
                                # Action type: inside types[0].typeOfAction
                                types = act0.get("types", [])
                                if types and not _c.get("action"):
                                    toa = types[0].get("typeOfAction", "")
                                    if toa:
                                        _c["action"] = toa
                                # Deadline: deadlineDates is a list of "YYYY-MM-DD" strings
                                dl_dates = act0.get("deadlineDates", [])
                                if dl_dates and not _c.get("deadline"):
                                    # Take the furthest one (last deadline)
                                    _c["deadline"] = sorted(dl_dates)[-1]
                                # Opening date
                                pod = act0.get("plannedOpeningDate", "")
                                if pod and not _c.get("opening"):
                                    _c["opening"] = pod
                            _c["action_parsed"] = True
                        except Exception:
                            pass

                    # Fallback action from typesOfAction if actions field didn't provide it
                    if not _c.get("action"):
                        action_fb = _first(meta, "typesOfAction", "typeOfAction", "fundingScheme")
                        if action_fb:
                            _c["action"] = action_fb

                    # Deadline from direct fields (fallback if actions not available) 
                    if not _c.get("deadline"):
                        deadline_detail = _first(meta, "deadlineDate", "nextDeadline", "closingDate")
                        if deadline_detail:
                            _c["deadline"] = deadline_detail
                    # Opening date from direct fields
                    if not _c.get("opening"):
                        opening_detail = _first(meta, "startDate", "openingDate", "publicationDate")
                        if opening_detail:
                            _c["opening"] = opening_detail

                    # Budget from budgetOverview (primary and most precise source) 
                    if not _c.get("budget"):
                        raw_overview = meta.get("budgetOverview")
                        if isinstance(raw_overview, list):
                            raw_overview = raw_overview[0] if raw_overview else None
                        if raw_overview:
                            try:
                                overview = json.loads(raw_overview) if isinstance(raw_overview, str) else raw_overview
                                # Topic identifier from the URL (e.g. HORIZON-CL6-2027-02-FARM2FORK-06)
                                # NOT the callIdentifier (e.g. HORIZON-CL6-2027-02) which is too generic
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

                    # Budget from other XHR fields (fallback) 
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

                    # full_text from XHR (primary source, always complete) 
                    # descriptionByte -> scope + expected outcomes (HTML)
                    # destinationDetails -> destination context (HTML)
                    # topicConditions -> eligibility conditions (HTML)
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
        # 1. Navigate to the detail page
        page.goto(url, wait_until="domcontentloaded", timeout=40000)
        # Active wait: exits as soon as the XHR arrives or after 8s
        t0 = time.time()
        while not captured and time.time() - t0 < 8:
            page.wait_for_timeout(400)
        
        # 2. DOM text — only as fallback if XHR hasn't already provided the text
        try:
            body_text = page.locator("body").inner_text(timeout=5000)
        except Exception:
            body_text = ""

        # 3. ROW-HUNTER LOGIC (DOM table expansion)
        # This function must be defined above _enrich_one
        budget_val_dom = extract_budget_per_project_dom(page, topic_id)

        # full_text: XHR takes priority (always complete and without cookie banner),
        #              fallback to DOM only if it doesn't contain cookie banner text.
        if captured.get("full_text"):
            row["full_text"] = captured["full_text"]
        elif body_text and COOKIE_TEXT.lower() not in body_text.lower():
            row["full_text"] = clean(body_text) or ""
        else:
            row["full_text"] = ""

        # Budget priority: 1) budgetOverview XHR (precise per topic)
        #                  2) DOM table  3) free-text regex
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

    # 4. Final metadata assignment
    # full_text: if XHR arrived after the try block (rare but possible), update
    if captured.get("full_text") and not row.get("full_text"):
        row["full_text"] = captured["full_text"]

    if captured.get("prog") and not row.get("programme_raw"):
        row["programme_raw"] = captured["prog"]
    if captured.get("action"):
        # Always overwrite: the detail page has the precise action type
        row["action_raw"] = captured["action"]
    if captured.get("call_id") and not row.get("call_id"):
        row["call_id"] = captured["call_id"]
    # Deadline from the detail page (most reliable source: actions.deadlineDates field)
    if captured.get("deadline"):
        row["deadline_raw"] = captured["deadline"]
    # Opening date from the detail page
    if captured.get("opening") and not row.get("opening_raw"):
        row["opening_raw"] = captured["opening"]

    # Return True if at least one useful field was successfully populated
    return bool(
        row.get("full_text") or row.get("call_id") or
        row.get("deadline_raw") or row.get("budget_raw") or
        row.get("programme_raw")
    )


def enrich(ctx, rows: list):
    """Visit each call's detail page and enrich the row with full text, budget, and metadata."""
    to_fix = [r for r in rows if r.get("url")]
    if not to_fix:
        print("  No calls with a URL to enrich.", flush=True)
        return

    print(f"  {len(to_fix)} calls to enrich (full_text + budget)...", flush=True)

    # Helper to open a fresh page with stealth applied (avoids bot detection)
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
        for attempt in range(1, 4):   # 3 attempts instead of 2
            try:
                ok = _enrich_one(page, row)
                break
            except Exception as e:
                print(f"    [attempt {attempt} failed] {e}", flush=True)
                try:
                    page.close()
                except Exception:
                    pass
                page = _new_page()    # re-apply stealth on fresh page
                time.sleep(3 * attempt)   # progressive back-off

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
    """Convert a raw scraped row into a fully classified and structured call object ready for output."""
    url        = row.get("url", "")
    prog_raw   = row.get("programme_raw") or ""
    call_id    = row.get("call_id") or ""
    action_raw = row.get("action_raw") or ""

    # Try to extract a cluster number (1-6) from the call_id, raw cluster field, or URL
    cluster_num = ""
    for src in [call_id, row.get("cluster_raw",""), url]:
        m = RE_CLUSTER.search(src or "")
        if m:
            cluster_num = m.group(1)
            break

    # Structural thematic classification at two levels 
    #
    # LEVEL 1 - Horizon Europe: structural classification by CL1-6 + sub-programmes
    # LEVEL 2 - Other programmes: programme code from the URL or the programme field
    #
    he_flag = _is_horizon_europe(prog_raw, url, call_id)
    cluster_label = ""
    thematic = ""

    if he_flag:
        # L1: Horizon Europe
        cnum, clabel, th = classify_horizon_europe(url, call_id)
        if cnum:
            cluster_num = cnum
        if clabel:
            cluster_label = clabel
        if th:
            thematic = th
        # Fallback: if CL found but not mapped
        if not thematic and cluster_num:
            thematic = HE_CLUSTER_MAP.get(cluster_num, "")
    else:
        # L2: Other (non-HE) programmes
        # 2a. Topic ID prefix in URL (more precise: catches sub-programmes like PPPA-CHIPS, CEF-T, etc.)
        thematic = classify_non_he_by_url(url)
        # 2b. Programme name (programme_raw field)
        if not thematic:
            thematic = classify_non_he_by_programme(prog_raw)
        # 2c. cluster_label from THEMATIC_MAP if we have a numeric cluster
        if not cluster_label and cluster_num:
            cluster_label = THEMATIC_MAP.get(cluster_num, "")

    # Common fallback: name_classify (NUMERIC_ID_NAME_RULES) 
    if not thematic:
        thematic = name_classify(row.get("name", ""))

    # Align cluster_num with thematic if derived from inverted THEMATIC_MAP
    if not cluster_num and cluster_label:
        _inv = {v: k for k, v in THEMATIC_MAP.items()}
        cluster_num = _inv.get(cluster_label, "")

    # Beneficiary hint (from URL prefix override table)
    u_cnum, u_clabel, u_thematic, u_benef = url_classify(url)
    # url_classify may refine cluster_label if not yet set
    if not cluster_label and u_clabel:
        cluster_label = u_clabel

    action     = normalize_action(action_raw)
    # Flag calls that belong to one of the five Horizon Europe Missions
    is_mission = bool("/HORIZON-MISS" in url.upper())

    opening_raw  = row.get("opening_raw") or ""
    deadline_raw = row.get("deadline_raw") or ""

    full_text = row.get("full_text") or ""
    multi = classify_multitopic(row.get("name") or "", full_text, thematic)

    # Thematic promotion: if still generic, use keywords from full_text 
    # "Cross-cutting / Other" is acceptable ONLY for WIDERA and genuinely
    # interdisciplinary calls; everything else must have a specific area.
    GENERIC_THEMATICS = {"Cross-cutting / Other", ""}
    # Areas that must NOT be promoted via keywords (they are already specific)
    PROMOTION_EXEMPT = {
        "Internships, fellowships & scholarships",  # ERC/MSCA: domain-dependent, do not promote randomly
    }
    effective_thematic = thematic
    if effective_thematic in GENERIC_THEMATICS and multi["multi_thematic"]:
        for candidate in multi["multi_thematic"]:
            if candidate not in GENERIC_THEMATICS and candidate not in PROMOTION_EXEMPT:
                effective_thematic = candidate
                break

    # Ensure the primary thematic is included in multi_thematic
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
        # --- Title-level relevance signals (for search ranking) ---
        # ``title_keyword_hits``: thematic areas whose keywords appear in the
        # call *title* (not just in the body text).  Consumers can use this to
        # surface title-matching calls above body-only matches.
        # Example: {"Digital, Industry & Space": ["digital", "quantum"]}
        "title_keyword_hits":  multi["title_keyword_hits"],
        # ``title_match_score``: total number of distinct topic keywords found
        # in the title.  A score > 0 means the call title itself contains the
        # searched term; use it as a sort-tier boost.
        # Suggested sort key: (-title_match_score, deadline, name)
        "title_match_score":   multi["title_match_score"],
    }

# Changelog 

def write_changelog(old_calls: list, new_calls: list, changelog_path: Path, generated: str):
    """Compare old and new call lists and write a markdown changelog file with added/removed entries."""
    old_by_url = {c["url"]: c for c in old_calls}
    new_by_url = {c["url"]: c for c in new_calls}

    old_urls = set(old_by_url)
    new_urls = set(new_by_url)

    added   = [new_by_url[u] for u in sorted(new_urls - old_urls)]
    removed = [old_by_url[u] for u in sorted(old_urls - new_urls)]

    # Count calls per thematic area for the summary table
    def thematic_counts(calls):
        tc = {}
        for c in calls:
            k = c.get("thematic_cluster") or "(unclassified)"
            tc[k] = tc.get(k, 0) + 1
        return tc

    date_str = generated[:10]

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
                meta = " · ".join(filter(None, [prog, action, f"Deadline: {dead}" if dead else ""]))
                lines.append(f"- **{name}**")
                if meta:
                    lines.append(f"  {meta}")
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
        # Only append if this exact line is not already recorded (idempotent)
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

# Main 

def main(out_path: Path):
    """Main entry point: scrape all calls from the EU portal, enrich them, classify them, and write calls.json."""
    rows      = []
    seen_urls = set()

    with sync_playwright() as p:
        # Launch Chromium in headless mode; disable the AutomationControlled flag to reduce bot detection
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

        # Step 1: listing 
        # Intercept the API response BEFORE goto to capture totalResults
        total_captured = {}
        api_url_captured = {}   # captures the real SEDIA API URL for direct use

        def handle_first_response(response, _tc=total_captured, _au=api_url_captured):
            if SEARCH_API in response.url and response.status == 200 and "total" not in _tc:
                try:
                    body = response.json()
                    t = body.get("totalResults")
                    if t is not None:
                        _tc["total"] = int(t)
                        _au["url"] = response.url   # save full URL for requests
                        print(f"  Total detected from API: {t}")
                except Exception:
                    pass

        page.on("response", handle_first_response)
        page.goto(LIST_URL.format(page=1, ps=PAGE_SIZE),
                  wait_until="domcontentloaded", timeout=90000)

        # Force cookie acceptance
        try:
            cookie_button = page.get_by_role("button", name="Accept all cookies")
            if cookie_button.is_visible():
                cookie_button.click()
                print("  Cookies accepted")
                page.wait_for_timeout(4000)
        except:
            pass

        # Wait for the API to respond (max 30s)
        print("  Waiting for SEDIA API response...")
        deadline_init = time.time() + 30
        while "total" not in total_captured and time.time() < deadline_init:
            page.wait_for_timeout(500)

        page.remove_listener("response", handle_first_response)

        # Use the captured total if the response handler caught it, otherwise fall back to DOM scraping
        total = total_captured.get("total") or read_total(page)

        if total is None:
            print(f"  Could not read the call counter.")
            browser.close()
            return
        max_pages = math.ceil(total / PAGE_SIZE)
        print(f"  Total: {total} calls | pages: {max_pages}")

        for pnum in range(1, max_pages + 1):
            remaining = total - (pnum - 1) * PAGE_SIZE
            expected  = min(PAGE_SIZE, remaining)
            url = LIST_URL.format(page=pnum, ps=PAGE_SIZE)
            print(f"\n[p{pnum}/{max_pages}] expected ~{expected}", end="", flush=True)

            page_results = []
            import threading as _threading
            # Use a debounce timer: after each valid response we wait
            # 2s before considering the page complete. If another response
            # arrives in the meantime, the timer resets. This handles the case
            # where SEDIA sends multiple partial responses for the same page.
            _debounce_timer: list = [None]   # list for mutability inside closure

            def _mark_done(_pr=page_results, _dt=_debounce_timer):
                # Called 2s after the last valid response — signals completion
                _dt.append("done")

            def handle_list_response(response, _pr=page_results, _dt=_debounce_timer):
                """
                Playwright response listener fired for every network response on the page.
                _pr and _dt are captured by default-argument binding so mutations to the
                outer lists are visible to the calling scope even inside a closure.
                """
                # Ignore anything that is not a successful SEDIA API call
                if SEARCH_API not in response.url or response.status != 200:
                    return

                # CRITICAL: read the body IMMEDIATELY — Playwright releases the buffer
                # as soon as the listener returns ("No resource with given identifier")
                try:
                    body = response.json()
                except Exception:
                    return

                try:
                    results = body.get("results", [])
                    # If the API returned an empty results list, nothing to do for this response
                    if not results:
                        return

                    api_count = len(results)
                    print(f" [API p{body.get('pageNumber','?')}: {api_count}]", end="", flush=True)

                    for item in results:
                        # "reference" is the unique SEDIA identifier for the call; skip if missing
                        ref = item.get("reference", "")
                        if not ref:
                            continue

                        meta = item.get("metadata", {}) or {}

                        # Build the full detail-page URL, trying several fallback sources
                        full_url = (
                            item.get("url")           # preferred: explicit URL field
                            or _first(meta, "url", "esST_URL")  # sometimes nested in metadata
                            or ""
                        )
                        if not full_url:
                            # Last resort: construct the URL from the call identifier
                            cid_tmp = _first(meta, "identifier", "callIdentifier") or ref
                            full_url = TOPIC_BASE_URL + cid_tmp

                        # Extract all available metadata fields, trying multiple key names
                        # because the SEDIA API uses inconsistent field names across programme types
                        prog_id      = _first(meta, "frameworkProgramme", "programme")
                        action       = _first(meta, "typesOfAction", "typeOfAction", "fundingScheme")
                        cid          = _first(meta, "identifier", "callIdentifier")
                        title        = _first(meta, "title", "name") or item.get("summary") or ref
                        opening_raw  = _first(meta, "startDate", "openingDate", "publicationDate")
                        deadline_raw = _first(meta, "deadlineDate", "nextDeadline", "closingDate")
                        # Try to extract the Horizon Europe cluster number (CL1-6) from URL or call ID
                        cluster_raw  = pick(RE_CLUSTER, full_url) or pick(RE_CLUSTER, cid or "")

                        # Append a raw row to the shared results list; it will be enriched later
                        _pr.append({
                            "name":          clean(title) or ref,
                            "call_id":       cid,
                            # Map numeric programme ID to a human-readable name, or keep raw if unknown
                            "programme_raw": PROGRAMME_MAP.get(prog_id, prog_id) if prog_id else None,
                            "action_raw":    action or None,
                            "cluster_raw":   cluster_raw,
                            "opening_raw":   opening_raw or None,
                            "deadline_raw":  deadline_raw or None,
                            "url":           full_url,
                            "_ref":          ref,       # internal deduplication key
                            "_needs_enrich": False,
                        })

                    # Debounce: cancel any pending completion timer and restart it.
                    # This ensures we keep waiting if SEDIA sends another partial response
                    # within the next 2 seconds before declaring the page fully loaded.
                    old = _dt[0]
                    if old is not None:
                        old.cancel()
                    t = _threading.Timer(2.0, _mark_done)
                    _dt[0] = t
                    t.start()

                except Exception as e:
                    print(f"\n    [WARN parse API p{pnum}] {e}", flush=True)

            page.on("response", handle_list_response)
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=90000)
                accept_cookies(page)
                # Wait up to expected results or 45s timeout
                deadline_t = time.time() + 45
                while len(page_results) < expected and time.time() < deadline_t:
                    # Check if the debounce timer has signalled completion
                    if len(_debounce_timer) > 1:
                        break
                    page.wait_for_timeout(300)
                # Cancel timer if still active
                if _debounce_timer[0] is not None:
                    _debounce_timer[0].cancel()
                if len(page_results) == 0:
                    print(f"  No API response for page {pnum}", flush=True)
            finally:
                page.remove_listener("response", handle_list_response)

            new_items = [r for r in page_results if r.get("_ref") not in seen_urls]
            print(f" -> found {len(new_items)} new (API total: {len(page_results)})", flush=True)

            for r in new_items:
                seen_urls.add(r["_ref"])   # _ref is always unique on the SEDIA side
                rows.append(r)
            time.sleep(0.8)

        # Step 1b: recover missing calls with a second Playwright pass 
        missing = total - len(rows)
        if missing > 0:
            print(f"\n  {missing} calls missing (SEDIA cross-page duplicates). Second pass...", flush=True)
            for pg in range(1, max_pages + 1):
                if len(rows) >= total:
                    break
                url2 = LIST_URL.format(page=pg, ps=PAGE_SIZE)
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
                    })
                    print(f"   {clean(title) or ref}", flush=True)
            still_missing = total - len(rows)
            print(f"  After recovery: {len(rows)}/{total} calls (still missing: {still_missing})", flush=True)
            if still_missing > 0:
                print(f"    {still_missing} calls unreachable after double pass — may be server-side SEDIA duplicates or calls withdrawn in the meantime.", flush=True)

        # Step 2: open each call detail page and enrich with full text, budget and metadata
        print(f"\n=== Step 2: enriching {len(rows)} calls in total ===", flush=True)
        enrich(ctx, rows)
        browser.close()

    # Classification and output 
    calls = []
    # Deduplicate rows: prefer _ref as primary key, fall back to URL
    seen_refs = set()
    seen_urls = set()
    for row in rows:
        call = to_call(row)
        # Primary key = _ref (API reference, always unique); fallback to url
        ref_key = row.get("_ref") or ""
        url_key = call.get("url") or ""
        # Skip only if BOTH keys have already been seen (avoids false duplicates
        # caused by different heuristic URLs for the same topic)
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
        k = c["thematic_cluster"] or "(unclassified)"
        tc[k] = tc.get(k, 0) + 1
    print(f"\nClassification ({len(calls)} calls total):")
    for k, v in sorted(tc.items(), key=lambda x: -x[1]):
        print(f"  {v:5d}  {k}")
    print(f"\nUnclassified: {tc.get('(unclassified)', 0)}")

    generated = datetime.now(timezone.utc).isoformat()

    # Changelog 
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

    # Save 
    # Build the final JSON payload with a generation timestamp and the full call list
    payload = {
        "generated": generated,
        "calls": calls,
    }
    out_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"\n  Written {out_path} with {len(calls)} calls")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="calls.json", help="Output JSON file path")
    args = parser.parse_args()
    main(Path(args.out))












































































































































































































































































































































































