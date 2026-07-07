#!/usr/bin/env python3
"""
Project Opportunity Board — Sync Pipeline v2
HigherGov → Tier Classification → data.json → GitHub Pages
Per sourcing-rules.md v2026-07-06

Usage: python3 sync.py
"""
import json, os, re, sys, subprocess
from datetime import datetime, timezone, timedelta
import httpx

# Import DPMC scraper
try:
    from dpmc_scraper import fetch_and_parse as fetch_dpmc, classify_dpmc, dpmc_to_job
    HAS_DPMC = True
except ImportError:
    HAS_DPMC = False

REPO_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(REPO_DIR, "data.json")

# --- Config ---
HIGHERGOV_KEY = os.environ.get("HIGHERGOV_API_KEY", "")
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

def existing_keys(jobs):
    return {(j.get("sourceType", ""), j.get("opportunityId", j.get("id", ""))) for j in jobs}

def pull_highergov():
    """Pull from all configured HigherGov searches."""
    if not HIGHERGOV_KEY:
        print("ERROR: HIGHERGOV_API_KEY not set")
        return []

    all_results = []
    for label, search_id in SEARCH_IDS.items():
        if not search_id:
            continue
        print(f"  Pulling {label} (search {search_id})...")
        params = {"api_key": HIGHERGOV_KEY, "search_id": search_id, "page_size": "100"}
        try:
            resp = httpx.get(HIGHERGOV_URL, params=params, timeout=30)
            data = resp.json()
            results = data.get("results", [])
            if isinstance(results, dict):
                results = results.get("data", results.get("results", []))
            print(f"    {len(results)} results")
            all_results.extend(results)
        except Exception as e:
            print(f"    ERROR: {e}")
    return all_results

def process_opportunity(opp, existing_keys_set):
    """Process one HigherGov opportunity through all rules."""
    title = opp.get("title", "")
    desc = (opp.get("description_text") or opp.get("ai_summary") or "")
    combined = f"{title} {desc}".lower()
    opp_id = opp.get("id", opp.get("opp_key", ""))
    source_key = ("HigherGov", opp_id)
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

    # Distance tier
    state = opp.get("pop_state", "")
    distance_info = DISTANCE_TIERS.get(state, ("Unknown", state, "unknown", "unknown"))
    distance_tier, default_city, miles, hours = distance_info

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
    city = opp.get("pop_city", "")
    label = f"{city}, {state}" if city else default_city
    location = f"{label}\n{miles}\n{hours}"

    # Key dates
    if due_date_str:
        key_dates = f"Bid Due: {fmt_date(due_date_str)}"
    else:
        key_dates = "Bid Due: TBD"

    # NAICS
    naics = opp.get("naics_code", {})
    naics_code = naics.get("code", "") if isinstance(naics, dict) else ""

    # Link
    path = opp.get("path", "")
    link = f"https://www.highergov.com{path}" if path else ""

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


def merge_and_save(existing_jobs, new_jobs):
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

    data = {
        "board": "Project Opportunity Board",
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "jobs": existing_jobs,
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


def main():
    global skip_log
    skip_log = []
    print("=== Project Opportunity Board — Sync v2 ===")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M UTC')}")

    existing = load_existing()
    print(f"Existing jobs: {len(existing)}")

    print("Pulling HigherGov...")
    raw = pull_highergov()
    print(f"Raw results: {len(raw)}")

    eks = existing_keys(existing)
    new_jobs = []
    for opp in raw:
        job = process_opportunity(opp, eks)
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
        dpmc_added = 0
        for proj in dpmc_raw:
            tier_label, tag, reasoning, is_skip = classify_dpmc(proj)
            if is_skip:
                continue
            job = dpmc_to_job(proj, tier_label, tag, reasoning)
            key = (job["sourceType"], job["opportunityId"])
            if key not in eks:
                new_jobs.append(job)
                eks.add(key)
                dpmc_added += 1
        print(f"  DPMC passed: {dpmc_added}")

    # Merge
    added, updated = merge_and_save(existing, new_jobs)
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
