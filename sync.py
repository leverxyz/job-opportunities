#!/usr/bin/env python3
"""
Project Opportunity Board — Sync Pipeline v2
HigherGov → Tier Classification → data.json → GitHub Pages
Per sourcing-rules.md v2026-07-06

Usage: python3 sync.py
"""
import json, math, os, re, sys, subprocess
from datetime import datetime, timezone, timedelta
from pathlib import Path
import httpx
import pgeocode

# Import DPMC scraper
try:
    from dpmc_scraper import fetch_and_parse as fetch_dpmc, classify_dpmc, dpmc_to_job
    HAS_DPMC = True
except ImportError:
    HAS_DPMC = False

REPO_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(REPO_DIR, "data.json")

# --- Config ---
def _load_highergov_key():
    """Same fallback hg.py already uses: env var first, then ~/.hermes/.env
    directly. Lets `python3 sync.py` run bare -- no caller-side `export` --
    which matters because the bare form is what's in command_allowlist; an
    `export $(...) && python3 sync.py` chain trips the gate's compound-
    command check and forces an approval prompt on every cron run."""
    key = os.environ.get("HIGHERGOV_API_KEY")
    if key:
        return key.strip()
    env_file = Path.home() / ".hermes" / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line.startswith("HIGHERGOV_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


HIGHERGOV_KEY = _load_highergov_key()
SEARCH_IDS = {
    "federal": "RUjIV5pvOv6TKsq2QJbUA",  # §1 — Federal Northeast
    # "roofing_nationwide": None,          # §1 — TBD by Chief
    # "nj_state_local": None,              # §1 — TBD by Chief
}
HIGHERGOV_URL = "https://www.highergov.com/api-external/opportunity/"

# --- §0.7 Keyword Sets (whole-word matching via word boundaries) ---

ROOFING_KEYWORDS = [
    "roof", "roofing", "reroof", "re-roof", "membrane", "epdm", "tpo", "sbs",
    "pvc roof", "shingle", "standing seam", "built-up roof", "bur", "flashing",
    "roof deck", "skylight", "siding", "window replacement", "windows", "facade",
    "building envelope", "exterior restoration", "masonry restoration", "brick",
]

ROOFING_EXCLUSIONS = [  # These contain "roof" but are HVAC, not roofing
    "rooftop unit", "roof top unit", "rtu",
]

MEP_KEYWORDS = [
    "mechanical", "electrical", "plumbing", "hvac", "mep", "boiler", "chiller",
    "fire protection", "sprinkler",
]

ENVIRONMENTAL_KEYWORDS = [
    "environmental remediation", "remediation", "abatement", "asbestos",
    "lead abatement", "mold", "soil", "groundwater", "hazardous waste",
    "wetlands", "ust removal", "tank removal",
]

GC_CONTEXT_KEYWORDS = [
    "renovation", "renovations", "rehab", "restoration",
    "alteration", "alterations", "addition", "additions", "demolition", "architectural",
    "masonry", "concrete", "carpentry", "drywall", "painting",
    "flooring", "carpet", "framing", "site work", "earthwork",
    "finish", "interior", "exterior",
]
# "repair", "replacement", "improvement" deliberately excluded — appear in MEP-only

HIGHWAY_KEYWORDS = ["highway", "paving", "asphalt", "roadway"]
BRIDGE_PAIRS = ["deck", "span", "girder", "culvert", "abutment"]  # bridge only skips when paired

EXCLUDE_KEYWORDS = ["cyber security", "cybersecurity"]

# --- §0.5 Distance Tiers ---
#
# Was a flat per-STATE lookup (every job in a state got the same anchor
# city's mileage -- e.g. all of PA showed "35 miles", whether the job was
# actually in Philadelphia (really ~28mi) or Franklin, PA (really ~282mi,
# a D3 Stretch job silently reported as D1 Core/nearby, which also meant
# it skipped the distance-tier review flag below). Fixed 2026-07-19:
# real per-job distance from the actual place of performance, using
# pgeocode (offline GeoNames zip data, no network call, no API key --
# this pipeline already learned the hard way tonight not to add a new
# live external dependency it doesn't need). Falls back to the old
# per-state table only if a job's location can't be resolved at all, so
# a geocoding miss degrades gracefully instead of dropping the job.

HOME_ZIP = "08505"
_geo = pgeocode.Nominatim("us")
_home = _geo.query_postal_code(HOME_ZIP)
HOME_LAT, HOME_LON = _home.latitude, _home.longitude

# Old table, kept only as the fallback for the rare job pgeocode can't place.
DISTANCE_TIERS = {
    "NJ": ("D1 Core", "Trenton, NJ", "20 miles", "0.5 hours away"),
    "PA": ("D1 Core", "Philadelphia, PA", "35 miles", "0.75 hours away"),
    "DE": ("D1 Core", "Wilmington, DE", "55 miles", "1 hour away"),
    "NY": ("D1 Core", "New York, NY", "70 miles", "1.5 hours away"),
    "MD": ("D2 Regional", "Baltimore, MD", "130 miles", "2.25 hours away"),
    "DC": ("D2 Regional", "Washington, DC", "175 miles", "3 hours away"),
    "CT": ("D2 Regional", "Hartford, CT", "155 miles", "2.75 hours away"),
    "MA": ("D3 Stretch", "Boston, MA", "270 miles", "4.5 hours away"),
    "RI": ("D3 Stretch", "Providence, RI", "230 miles", "3.75 hours away"),
    "VA": ("D3 Stretch", "Richmond, VA", "260 miles", "4.25 hours away"),
    "NH": ("D3 Stretch", "Concord, NH", "280 miles", "4.75 hours away"),
    "VT": ("D3 Stretch", "Montpelier, VT", "330 miles", "5.5 hours away"),
    "ME": ("D3 Stretch", "Portland, ME", "380 miles", "6 hours away"),
    "OH": ("D4 Far", "Cleveland, OH", "430 miles", "6.5 hours away"),
    "WV": ("D4 Far", "Charleston, WV", "430 miles", "6.5 hours away"),
    "NC": ("D4 Far", "Raleigh, NC", "450 miles", "7 hours away"),
}


def _haversine_miles(lat1, lon1, lat2, lon2):
    r = 3958.8  # earth radius, miles
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _geocode_place(zip_code, city, state):
    """Best-effort (lat, lon) for a place of performance. Zip first (precise,
    ~65% of HigherGov records have one) -- falls back to city+state name
    match, filtered to the right state so same-named cities in different
    states (there are 5+ "Franklin"s alone) don't collide. None if neither
    resolves."""
    if zip_code:
        r = _geo.query_postal_code(str(zip_code).strip().zfill(5))
        if r is not None and not math.isnan(r.latitude):
            return r.latitude, r.longitude
    if city and state:
        matches = _geo.query_location(city, top_k=200)
        if len(matches):
            matches = matches[matches.state_code == state]
            if len(matches):
                row = matches.iloc[0]
                return row.latitude, row.longitude
    return None


def compute_distance_tier(zip_code, city, state):
    """Real per-job distance tier. Tier boundaries picked to bracket the old
    table's own anchor points (D1 anchors topped out ~70mi, D2 ran
    130-175mi, D3 ran 230-380mi, D4 started at 430mi) rather than inventing
    new ones -- same intent, now actually measured per job instead of
    per state."""
    coords = _geocode_place(zip_code, city, state)
    label = f"{city}, {state}" if city else state
    if coords is None:
        return DISTANCE_TIERS.get(state, ("Unknown", label, "unknown", "unknown"))
    miles = _haversine_miles(HOME_LAT, HOME_LON, coords[0], coords[1])
    if miles < 100:
        tier = "D1 Core"
    elif miles < 200:
        tier = "D2 Regional"
    elif miles < 400:
        tier = "D3 Stretch"
    else:
        tier = "D4 Far"
    hours = max(0.3, miles / 50)
    return (tier, label, f"{round(miles)} miles", f"{round(hours, 2)} hours away")

SET_ASIDE_MAP = {
    "SBA": "SBA", "SB": "SBA", "SBP": "SBA-Partial",
    "SDVOSB": "SDVOSB", "SDVOSBC": "SDVOSB",
    "HUBZONE": "HUBZone",
    "WOSB": "WOSB", "EDWOSB": "WOSB",
    "NONE": "NONE", "": "NONE",
}

VALID_SET_ASIDES = {"SBA", "SB", "SBP", "NONE", "SDVOSB", "SDVOSBC", "WOSB", "EDWOSB", "HUBZONE"}

# --- Helpers ---

def wb_match(keyword, text):
    """Whole-word / phrase boundary match, case-insensitive."""
    pattern = r'\b' + re.escape(keyword) + r'\b'
    return bool(re.search(pattern, text, re.IGNORECASE))

def any_kw_match(keywords, text):
    """True if any keyword matches with word boundaries."""
    for kw in keywords:
        if wb_match(kw, text):
            return True
    return False

def count_kw_match(keywords, text):
    """Count how many keywords match with word boundaries."""
    return sum(1 for kw in keywords if wb_match(kw, text))

def safe_int(v):
    if v is None: return 0
    try: return int(v)
    except (ValueError, TypeError): return 0

def fmt_date(iso_str):
    """Format ISO date to 'Mon DD, YYYY'."""
    if not iso_str: return iso_str
    try:
        d = datetime.strptime(iso_str, "%Y-%m-%d")
        return d.strftime("%b %d, %Y").replace(" 0", " ")
    except ValueError:
        return iso_str

# --- Skip Log ---
skip_log = []

def is_false_gc(text):
    """'Building N' patterns are location references, not GC construction context."""
    no_building_n = re.sub(r'\bbuilding\s+\d+\w?\b', '', text, flags=re.IGNORECASE)
    return any_kw_match(GC_CONTEXT_KEYWORDS, no_building_n)

def log_skip(opp_key, title, rule):
    skip_log.append({"key": opp_key, "title": title[:80], "rule": rule})

# --- §0.4 Actionability ---

def fails_actionability(due_date_str, val_high):
    """Return (True, reason) if opportunity should be skipped per §0.4."""
    if due_date_str:
        try:
            due = datetime.strptime(due_date_str, "%Y-%m-%d").date()
            today = datetime.now(timezone.utc).date()
            if due < today:
                return (True, "past due")
            if (due - today).days < 5:
                return (True, f"short window: { (due-today).days }d out")
        except ValueError:
            pass
    # Value floor — $25K, exempt Tier 1 (checked after tiering)
    return (False, "")

def below_value_floor(val_high, is_tier1):
    """Return True if below $25K and not Tier 1."""
    if is_tier1:
        return False
    return val_high > 0 and val_high < 25000


# --- §0.1 Tier Classification ---

def classify_tier(title, desc, set_aside, val_low, val_high, distance_tier, is_nj_state_funded):
    """
    Classify an opportunity into Tier 1-5 or SKIP.
    Returns: (tier_label, tag, reasoning, is_skip)
    """
    combined = f"{title} {desc}".lower()

    # Check roofing exclusions first (RTU trap)
    has_roofing_exclusion = any_kw_match(ROOFING_EXCLUSIONS, combined)

    has_roofing = any_kw_match(ROOFING_KEYWORDS, combined) and not has_roofing_exclusion
    has_envelope = any_kw_match(["siding", "window replacement", "windows", "facade",
                                  "building envelope", "exterior restoration",
                                  "masonry restoration", "brick"], combined) and not has_roofing_exclusion
    mep_count = count_kw_match(MEP_KEYWORDS, combined)
    has_gc_context = is_false_gc(combined)  # excludes "Building N" references
    has_environmental = any_kw_match(ENVIRONMENTAL_KEYWORDS, combined)
    is_highway = any_kw_match(HIGHWAY_KEYWORDS, combined)
    is_bridge = wb_match("bridge", combined) and any_kw_match(BRIDGE_PAIRS, combined)
    is_cyber = any_kw_match(EXCLUDE_KEYWORDS, combined)
    is_8a = "8(A)" in (set_aside or "").upper() or "8A" in (set_aside or "").upper()

    # Use high value for tiering; missing → assume ≤$3M (§0.1)
    effective_val = val_high if val_high > 0 else val_low
    if effective_val == 0:
        effective_val = 3_000_000  # assume ≤$3M

    # --- NJ State-Funded routing (§0.2) ---
    if is_nj_state_funded:
        if is_highway or is_cyber or is_8a:
            return ("SKIP", "", "", True)
        # MEP-only or environmental-only with no roofing
        if not has_roofing:
            if mep_count >= 1:
                return ("SKIP", "", "", True)
            if has_environmental and not has_gc_context:
                return ("SKIP", "", "", True)
            # No roofing at all → skip (Concord can't touch, Applied can't perform)
            return ("SKIP", "", "", True)

        # Has roofing — route to lanes
        is_strictly_roofing = has_roofing and not has_envelope
        if is_strictly_roofing:
            return ("Lane 1", "APPLIED-PRIME", "NJ state-funded, strictly roofing", False)
        # Mixed envelope
        if has_roofing and has_envelope:
            return ("CHIEF-REVIEW", "", "NJ state-funded, mixed roof+envelope — Chief review", False)
        # Roofing inside larger project
        return ("Lane 2", "APPLIED-SUB", "NJ state-funded, roofing scope in larger project", False)

    # --- Federal / Private / Commercial routing (§0.1) ---
    if is_highway or is_bridge or is_cyber or is_8a:
        return ("SKIP", "", "", True)

    # Environmental-only (no GC context)
    if has_environmental and not has_gc_context:
        return ("SKIP", "", "", True)

    # MEP check — MEP keywords without roofing/envelope = skip
    # Also check for Tier 5: MEP WITH roofing = promote
    is_mep = mep_count >= 1
    is_mep_only = is_mep and not has_roofing and not has_envelope

    # Tier 1: Roofing/Envelope — highest priority
    if has_roofing or has_envelope:
        tag = "APPLIED-PRIME"
        reason_parts = []
        if has_roofing: reason_parts.append("roofing scope detected")
        if has_envelope: reason_parts.append("envelope scope detected")
        if is_mep:
            # MEP with roofing — promote to Tier 5, not Tier 1
            return ("Tier 5", "APPLIED-SUB-MEP", "MEP-heavy with roofing scope", False)
        return ("Tier 1", tag, "; ".join(reason_parts), False)

    # MEP-only, no roofing → skip
    if is_mep_only:
        return ("SKIP", "", "", True)

    # Tier 2 vs Tier 4: Concord GC, split at $3M
    if effective_val <= 3_000_000:
        tier_label = "Tier 2"
        tag = "CONCORD-PRIME"
    else:
        tier_label = "Tier 4"
        tag = "CONCORD-SUB"

    # Distance tier filter for Tiers 2/4
    distance_block = False
    if distance_tier in ("D3 Stretch", "D4 Far"):
        distance_block = True

    if distance_block:
        return (tier_label, tag, f"GC work at {distance_tier} — boresighted, needs Chief review", False)

    return (tier_label, tag, "", False)


# --- Pipeline ---

def load_existing():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE) as f:
            data = json.load(f)
        return data.get("jobs", [])
    return []

def load_excluded_keys():
    """Jobs Chief deleted from the board. Board-side delete writes
    "sourceType:opportunityId" strings here; a synced source that still
    returns one of these must never re-add it -- a hard delete alone gets
    silently undone by the next sync that still finds the opportunity."""
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE) as f:
            data = json.load(f)
        pairs = set()
        for entry in data.get("excludedKeys", []):
            if isinstance(entry, str) and ":" in entry:
                src, _, oid = entry.partition(":")
                pairs.add((src, oid))
        return pairs
    return set()

def load_signals():
    """Chief's interested/not-interested decisions -- the training data for
    the future learning loop (own session, not built yet). Two kinds:
    explicit ("interested", logged via `board.py interested <id>`, run by
    Sam on Chief's instruction -- the board page has been view-only since
    20546f3, no button triggers this anymore) and implicit
    ("expired_no_interest", logged here the first time a job's bid due
    date passes without him ever marking it interested). Standalone
    records -- a job can be deleted later and the signal still stands on
    its own."""
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE) as f:
            data = json.load(f)
        return data.get("signals", [])
    return []

def existing_keys(jobs):
    return {(j.get("sourceType", ""), j.get("opportunityId", j.get("id", ""))) for j in jobs}

def pull_highergov():
    """Pull from all configured HigherGov searches, following pagination.

    A single request only ever returns page 1 (up to page_size, default
    100). The API reports meta.pagination.pages -- verified live 2026-07-17
    that a real search can span multiple pages (265 results / 3 pages at
    page_size=100) and every prior sync silently kept only page 1.
    """
    if not HIGHERGOV_KEY:
        print("ERROR: HIGHERGOV_API_KEY not set")
        return []

    all_results = []
    for label, search_id in SEARCH_IDS.items():
        if not search_id:
            continue
        print(f"  Pulling {label} (search {search_id})...")
        page = 1
        total_pages = 1
        label_results = []
        while page <= total_pages:
            params = {
                "api_key": HIGHERGOV_KEY,
                "search_id": search_id,
                "page_size": "100",
                "page_number": str(page),
            }
            try:
                resp = httpx.get(HIGHERGOV_URL, params=params, timeout=30)
                data = resp.json()
                results = data.get("results", [])
                if isinstance(results, dict):
                    results = results.get("data", results.get("results", []))
                label_results.extend(results)
                total_pages = data.get("meta", {}).get("pagination", {}).get("pages", 1)
                if not results:
                    break
            except Exception as e:
                print(f"    ERROR on page {page}: {e}")
                break
            page += 1
        print(f"    {len(label_results)} results across {total_pages} page(s)")
        all_results.extend(label_results)
    return all_results

def process_opportunity(opp, excluded_keys):
    """Process one HigherGov opportunity through all rules."""
    title = opp.get("title", "")
    desc = (opp.get("description_text") or opp.get("ai_summary") or "")
    combined = f"{title} {desc}".lower()
    opp_id = opp.get("id", opp.get("opp_key", ""))
    source_key = ("HigherGov", opp_id)

    # Chief deleted this one from the board -- never re-add it, ahead of
    # every other rule.
    if source_key in excluded_keys:
        log_skip(opp_id, title, "excluded by Chief (deleted from board)")
        return None

    set_aside_raw = (opp.get("set_aside") or "").upper()
    due_date_str = opp.get("due_date", "")
    val_low = safe_int(opp.get("val_est_low"))
    val_high = safe_int(opp.get("val_est_high"))

    # §0.4 Actionability (before tiering)
    skip, reason = fails_actionability(due_date_str, val_high)
    if skip:
        log_skip(opp_id, title, f"actionability: {reason}")
        return None

    # Set-aside lane check
    if set_aside_raw not in VALID_SET_ASIDES and set_aside_raw:
        log_skip(opp_id, title, f"unrecognized set-aside: {set_aside_raw}")
        # Don't drop — flag for review
        set_aside_display = f"REVIEW: {set_aside_raw}"
    else:
        set_aside_display = SET_ASIDE_MAP.get(set_aside_raw, set_aside_raw)

    # 8(a) skip
    if "8(A)" in set_aside_raw or "8A" in set_aside_raw:
        log_skip(opp_id, title, "8(a) set-aside")
        return None

    # Cyber skip
    if any_kw_match(EXCLUDE_KEYWORDS, combined):
        log_skip(opp_id, title, "cyber/IT exclusion")
        return None

    # Highway skip
    if any_kw_match(HIGHWAY_KEYWORDS, combined):
        log_skip(opp_id, title, "highway/paving")
        return None

    # Bridge skip (paired)
    if wb_match("bridge", combined) and any_kw_match(BRIDGE_PAIRS, combined):
        log_skip(opp_id, title, "bridge work")
        return None

    # Distance tier -- real per-job distance from place of performance
    state = opp.get("pop_state", "")
    city = opp.get("pop_city", "")
    zip_code = opp.get("pop_zip", "")
    distance_info = compute_distance_tier(zip_code, city, state)
    distance_tier, location_label, miles, hours = distance_info

    # NJ state-funded check — HigherGov Federal search should be federal money
    # But flag mixed funding if state/local somehow appears
    source_type = opp.get("source_type", "")
    is_nj_state = (source_type == "sled" and state == "NJ")

    # Tier classification
    tier_label, tag, reasoning, is_skip = classify_tier(
        title, desc, set_aside_raw, val_low, val_high, distance_tier, is_nj_state
    )

    if is_skip:
        log_skip(opp_id, title, f"tier skip: {tier_label}")
        return None

    # §0.4 Value floor (after tiering — Tier 1 exempt)
    is_tier1 = tier_label == "Tier 1"
    if below_value_floor(val_high, is_tier1):
        log_skip(opp_id, title, f"below $25K value floor (val_high={val_high})")
        return None

    # Magnitude
    magnitude = "TBD"
    if val_low and val_high:
        if val_high < 1_000_000:
            lo = int(val_low / 1000)
            hi = int(val_high / 1000)
            if lo == 0:
                magnitude = f"Under ${hi}K"
            elif lo == hi:
                magnitude = f"${lo}K"
            else:
                magnitude = f"${lo}K – ${hi}K"
        else:
            lo = val_low / 1_000_000
            hi = val_high / 1_000_000
            if lo == hi:
                magnitude = f"${lo:.1f}M".replace(".0M", "M")
            else:
                magnitude = f"${lo:.1f}M – ${hi:.1f}M".replace(".0M", "M")
    elif val_low:
        if val_low < 1_000_000:
            magnitude = f"${int(val_low/1000)}K+"
        else:
            magnitude = f"${val_low/1e6:.1f}M+".replace(".0M", "M")

    # Location
    location = f"{location_label}\n{miles}\n{hours}"

    # Key dates
    if due_date_str:
        key_dates = f"Bid Due: {fmt_date(due_date_str)}"
    else:
        key_dates = "Bid Due: TBD"

    # NAICS
    naics = opp.get("naics_code", {})
    naics_code = naics.get("code", "") if isinstance(naics, dict) else ""

    # Link -- HigherGov's `path` field is already a full URL (verified live
    # 2026-07-18: "https://www.highergov.com/contract-opportunity/..."), not
    # a relative path. Prepending the base again produced a doubled/broken
    # URL on every entry ("...comhttps://..."). Defensive fallback kept in
    # case the API ever actually returns a relative path.
    path = opp.get("path", "")
    if path.startswith("http"):
        link = path
    elif path:
        link = f"https://www.highergov.com{path}"
    else:
        link = ""

    job = {
        "opportunityId": opp_id,
        "jobName": title[:200],
        "projectDescription": desc[:500],
        "tier": tier_label,
        "tag": tag,
        "reasoning": reasoning,
        "setAside": set_aside_display,
        "sourceType": "HigherGov",
        "sourceDetail": (opp.get("agency") or {}).get("agency_name", "Unknown"),
        "location": location,
        "magnitude": magnitude,
        "keyDates": key_dates,
        "bidDueDate": due_date_str if due_date_str else None,
        "status": "new",
        "naics": naics_code,
        "link": link,
        "distanceTier": distance_tier,
    }
    return job


def merge_and_save(existing_jobs, new_jobs, excluded_keys=None, signals=None):
    existing_by_key = existing_keys(existing_jobs)
    added = 0
    updated = 0

    for job in new_jobs:
        key = (job["sourceType"], job["opportunityId"])
        if key in existing_by_key:
            # Update mutable fields, preserve status
            for ej in existing_jobs:
                if (ej.get("sourceType"), ej.get("opportunityId", ej.get("id"))) == key:
                    ej["bidDueDate"] = job.get("bidDueDate")
                    ej["keyDates"] = job.get("keyDates")
                    ej["magnitude"] = job.get("magnitude")
                    ej["projectDescription"] = job.get("projectDescription")
                    ej["tier"] = job.get("tier")
                    ej["tag"] = job.get("tag")
                    ej["reasoning"] = job.get("reasoning")
                    ej["link"] = job.get("link")
                    # location/distanceTier were left out of this refresh list
                    # until 2026-07-19 -- meant a distance-calc fix (like the
                    # move off the flat per-state table) would silently never
                    # reach any job already sitting on the board, only new
                    # ones. Recomputing this every sync is safe: it's a pure
                    # function of the job's own location fields.
                    ej["location"] = job.get("location")
                    ej["distanceTier"] = job.get("distanceTier")
                    updated += 1
                    break
        else:
            existing_jobs.append(job)
            existing_by_key.add(key)
            added += 1

    # Sort by bidDueDate (nulls last)
    existing_jobs.sort(key=lambda j: (j.get("bidDueDate") is None, j.get("bidDueDate", "")))

    # Purge: MEP/environmental jobs that slipped through with "new" status
    before = len(existing_jobs)
    existing_jobs[:] = [
        j for j in existing_jobs
        if not (
            j.get("status") == "new"
            and any_kw_match(MEP_KEYWORDS, (j.get("jobName","") + " " + j.get("projectDescription","")).lower())
            and not any_kw_match(ROOFING_KEYWORDS, (j.get("jobName","") + " " + j.get("projectDescription","")).lower())
        )
        and not (
            j.get("status") == "new"
            and any_kw_match(ENVIRONMENTAL_KEYWORDS, (j.get("jobName","") + " " + j.get("projectDescription","")).lower())
            and not any_kw_match(ROOFING_KEYWORDS, (j.get("jobName","") + " " + j.get("projectDescription","")).lower())
        )
        and j.get("tier") is not None  # also purge untiered
    ]
    purged = before - len(existing_jobs)
    if purged:
        print(f"  Purged {purged} jobs (MEP/env or untiered)")

    # Mark expired: keys no longer returned AND past due
    today = datetime.now(timezone.utc).date()
    for job in existing_jobs:
        if job.get("status") == "new" and job.get("bidDueDate"):
            try:
                due = datetime.strptime(job["bidDueDate"], "%Y-%m-%d").date()
                if due < today:
                    is_still_live = any(
                        (nj["sourceType"], nj["opportunityId"]) == (job.get("sourceType"), job.get("opportunityId", job.get("id")))
                        for nj in new_jobs
                    )
                    if not is_still_live:
                        job["status"] = "expired"
            except ValueError:
                pass

    # Implicit "not interested" signal: bid due date passed, Chief never
    # clicked Interested on it. Logged once per job (expirySignaled guards
    # against re-logging on every subsequent daily sync). Separate from the
    # "expired" status above on purpose -- that one also requires the
    # source to have stopped returning it (an amendment can push the due
    # date and keep a job live); this is purely "the date passed and he
    # never said yes," which is the actual training signal.
    if signals is None:
        signals = []
    for job in existing_jobs:
        if job.get("interested") or job.get("expirySignaled"):
            continue
        due_str = job.get("bidDueDate")
        if not due_str:
            continue
        try:
            due = datetime.strptime(due_str, "%Y-%m-%d").date()
        except ValueError:
            continue
        if due < today:
            signals.append({
                "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "type": "expired_no_interest",
                "sourceType": job.get("sourceType", ""),
                "opportunityId": job.get("opportunityId", job.get("id", "")),
                "jobName": job.get("jobName", ""),
                "tier": job.get("tier"),
                "tag": job.get("tag"),
                "sourceDetail": job.get("sourceDetail", ""),
                "magnitude": job.get("magnitude", ""),
                "distanceTier": job.get("distanceTier", ""),
            })
            job["expirySignaled"] = True

    data = {
        "board": "Project Opportunity Board",
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "jobs": existing_jobs,
        "excludedKeys": sorted(f"{src}:{oid}" for src, oid in (excluded_keys or set())),
        "signals": signals,
    }

    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")

    return added, updated


def commit_and_push():
    os.chdir(REPO_DIR)
    subprocess.run(["git", "add", "data.json"], check=True)
    result = subprocess.run(
        ["git", "commit", "-m", f"Sync: pipeline update — {datetime.now().strftime('%Y-%m-%d %H:%M')}"],
        capture_output=True, text=True
    )
    if result.returncode == 0 or "nothing to commit" in result.stdout + result.stderr:
        subprocess.run(["git", "push"], check=True)
        return True
    return False


def pull_latest():
    """Fast-forward-only pull before touching data.json.

    The board page now writes data.json directly via the GitHub Contents
    API (Add/Edit/Delete), bypassing this clone entirely. Without this,
    load_existing() would read a stale local file -- silently resurrecting
    something a person just deleted (excludedKeys would be stale too) --
    and the eventual `git push` would be rejected as non-fast-forward.
    --ff-only rather than a merge or reset: this workflow should only ever
    see linear history (independent commits to data.json from either side),
    so a real divergence should fail loudly, not get silently merged or
    (worse) discard uncommitted work sitting in the tree in an unrelated
    file. Never `reset --hard` here for the same reason.
    """
    os.chdir(REPO_DIR)
    subprocess.run(["git", "fetch", "origin"], check=True, capture_output=True)
    result = subprocess.run(["git", "merge", "--ff-only", "origin/main"], capture_output=True, text=True)
    if result.returncode != 0:
        print(f"  WARNING: could not fast-forward to origin/main: {result.stderr.strip()}")
        print("  Continuing with local data.json as-is -- history may have diverged, needs a human look.")


def main():
    global skip_log
    skip_log = []
    print("=== Project Opportunity Board — Sync v2 ===")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M UTC')}")

    pull_latest()

    existing = load_existing()
    print(f"Existing jobs: {len(existing)}")

    excluded = load_excluded_keys()
    if excluded:
        print(f"Excluded (Chief-deleted) keys: {len(excluded)}")

    print("Pulling HigherGov...")
    raw = pull_highergov()
    print(f"Raw results: {len(raw)}")

    new_jobs = []
    for opp in raw:
        job = process_opportunity(opp, excluded)
        if job:
            new_jobs.append(job)

    print(f"Passed filters: {len(new_jobs)}")
    print(f"Skipped: {len(skip_log)}")

    # Print skip log
    for s in skip_log[:10]:
        print(f"  SKIP [{s['rule']:30s}] {s['title'][:60]}")
    if len(skip_log) > 10:
        print(f"  ... and {len(skip_log)-10} more")

    # DPMC scraper (§3)
    if HAS_DPMC:
        print("\nPulling DPMC...")
        dpmc_raw = fetch_dpmc()
        print(f"  DPMC projects: {len(dpmc_raw)}")
        dpmc_passed = 0
        for proj in dpmc_raw:
            tier_label, tag, reasoning, is_skip = classify_dpmc(proj)
            if is_skip:
                continue
            job = dpmc_to_job(proj, tier_label, tag, reasoning)
            if (job.get("sourceType"), job.get("opportunityId")) in excluded:
                continue
            # Always hand off to merge_and_save, same as the HigherGov path --
            # it decides add vs. update against the live board itself. The old
            # `if key not in eks` gate here meant an already-boarded DPMC job
            # never re-entered new_jobs, so it never got its due date/tier/
            # description refreshed, and looked indistinguishable from a job
            # that had genuinely disappeared from the source (both "not in
            # new_jobs") to the expiry check below.
            new_jobs.append(job)
            dpmc_passed += 1
        print(f"  DPMC passed: {dpmc_passed}")

    # Merge
    signals = load_signals()
    added, updated = merge_and_save(existing, new_jobs, excluded, signals)
    print(f"Added: {added}, Updated: {updated}")

    final = load_existing()
    print(f"Total jobs: {len(final)}")

    if added > 0 or updated > 0:
        commit_and_push()
        print("Pushed to GitHub.")
    else:
        print("No changes to push.")

    print(f"Board: https://leverxyz.github.io/job-opportunities/")


if __name__ == "__main__":
    main()
