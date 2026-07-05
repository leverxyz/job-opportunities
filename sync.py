#!/usr/bin/env python3
"""
HigherGov → data.json sync pipeline.
Pulls opportunities, filters per Chief's rules, merges with existing data.json,
commits and pushes to job-opportunities repo.

Usage: python3 sync.py
"""
import json, os, sys, subprocess
from datetime import datetime, timezone
import httpx

REPO_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(REPO_DIR, "data.json")

# --- Config ---
HIGHERGOV_KEY = os.environ.get("HIGHERGOV_API_KEY", "")
SEARCH_ID = "1AhI2Gg-oi0-u2w9mSrGw"
HIGHERGOV_URL = "https://www.highergov.com/api-external/opportunity/"

# Filtering rules
MEP_KEYWORDS = ["mechanical", "electrical", "plumbing", "hvac", "mep", "boiler", "chiller",
                "fire protection", "sprinkler"]
CONSTRUCTION_KEYWORDS = ["construction", "building", "renovation", "repair", "rehab",
                         "restoration", "replacement", "improvement", "alteration"]
HIGHWAY_KEYWORDS = ["highway", "paving", "asphalt", "roadway", "bridge"]

# Job titles containing these keywords are always skipped
EXCLUDE_KEYWORDS = ["cyber repair", "cyber security"]

SET_ASIDE_MAP = {
    "SBA": "Small Business", "SB": "Small Business",
    "SDVOSB": "SDVOSB", "SDVOSBC": "SDVOSB",
    "HUBZONE": "HUBZone", "WOSB": "WOSB",
    "NONE": "None", "": "None",
}

DISTANCE_MAP = {
    "NJ": "0.5–1.5 Hours / 15–60 miles",
    "PA": "0.5–2 Hours / 25–120 miles",
    "NY": "1.5–3 Hours / 60–150 miles",
    "DE": "1–1.5 Hours / 50–90 miles",
    "MD": "2–3 Hours / 120–180 miles",
    "DC": "2.5–3.5 Hours / 150–210 miles",
    "CT": "2–3 Hours / 100–180 miles",
    "VA": "3–5 Hours / 180–320 miles",
    "MA": "3–4.5 Hours / 200–280 miles",
    "OH": "6–8 Hours / 380–500 miles",
    "RI": "3–4 Hours / 180–240 miles",
}


def load_existing():
    """Load existing data.json, return jobs list."""
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE) as f:
            data = json.load(f)
        return data.get("jobs", [])
    return []


def next_id(existing_jobs):
    """Generate next job ID: CCE-YYYY-NNNN."""
    year = datetime.now().strftime("%Y")
    max_n = 0
    prefix = f"CCE-{year}-"
    for job in existing_jobs:
        jid = job.get("id", "")
        if jid.startswith(prefix):
            try:
                n = int(jid.split("-")[-1])
                max_n = max(max_n, n)
            except ValueError:
                pass
    return f"{prefix}{max_n + 1:04d}"


def safe_int(v):
    if v is None:
        return 0
    try:
        return int(v)
    except (ValueError, TypeError):
        return 0


def should_skip(opp):
    """Return True if this opportunity should be filtered out."""
    title = (opp.get("title") or "").lower()
    desc = (opp.get("description_text") or "").lower()
    combined = title + " " + desc
    set_aside = (opp.get("set_aside") or "").upper()

    # Skip 8(a)
    if "8(A)" in set_aside or "8A" in set_aside:
        return True

    # Skip MEP-only (no construction context)
    mep_count = sum(1 for kw in MEP_KEYWORDS if kw in combined)
    has_construction = any(kw in combined for kw in CONSTRUCTION_KEYWORDS)
    if mep_count >= 2 and not has_construction:
        return True

    # Skip highway/paving
    if any(kw in title for kw in HIGHWAY_KEYWORDS):
        return True

    # Skip excluded keywords
    if any(kw in title for kw in EXCLUDE_KEYWORDS):
        return True

    return False


def pull_highergov():
    """Pull and filter opportunities from HigherGov."""
    if not HIGHERGOV_KEY:
        print("ERROR: HIGHERGOV_API_KEY not set")
        return []

    params = {"api_key": HIGHERGOV_KEY, "search_id": SEARCH_ID}
    resp = httpx.get(HIGHERGOV_URL, params=params, timeout=30)
    data = resp.json()

    results = data.get("results", [])
    if isinstance(results, dict):
        results = results.get("data", results.get("results", []))

    filtered = []
    for opp in results:
        if should_skip(opp):
            continue

        set_aside_raw = (opp.get("set_aside") or "").upper()
        # Active lane only: SBA, NONE, SB
        if set_aside_raw not in ("SBA", "SB", "NONE", "", "SDVOSB", "SDVOSBC"):
            continue

        filtered.append(opp)

    return filtered


def opp_to_job(opp, next_job_id):
    """Convert a HigherGov opportunity to a job board entry."""
    set_aside_raw = (opp.get("set_aside") or "").upper()
    val_low = safe_int(opp.get("val_est_low"))
    val_high = safe_int(opp.get("val_est_high"))

    # Magnitude
    if val_low and val_high:
        magnitude = f"${val_low/1e6:.1f}M – ${val_high/1e6:.1f}M"
    elif val_low:
        magnitude = f"${val_low/1e6:.1f}M+"
    else:
        magnitude = "Unknown"

    # Location
    state = opp.get("pop_state", "")
    city = opp.get("pop_city", "")
    
    if state in DISTANCE_MAP:
        drive = DISTANCE_MAP[state]
    else:
        drive = "Unknown"
    
    if city and state:
        location = f"{city}, {state}\n{drive}"
    elif state:
        location = f"{state}\n{drive}"
    else:
        location = "Unknown"

    # Key dates
    due_date = opp.get("due_date", "")
    if due_date:
        try:
            d = datetime.strptime(due_date, "%Y-%m-%d")
            key_dates = f"Bid Due: {d.strftime('%b %d').replace(' 0', ' ')}"
        except ValueError:
            key_dates = f"Bid Due: {due_date}"
    else:
        key_dates = "Bid Due: TBD"

    # Description
    desc = (opp.get("description_text") or opp.get("ai_summary") or "")[:500]

    return {
        "id": next_job_id,
        "jobName": opp.get("title", "")[:200],
        "projectDescription": desc,
        "setAside": SET_ASIDE_MAP.get(set_aside_raw, "Other"),
        "sourceType": "HigherGov",
        "sourceDetail": (opp.get("agency") or {}).get("name", "Unknown"),
        "location": location,
        "magnitude": magnitude,
        "keyDates": key_dates,
        "bidDueDate": due_date if due_date else None,
        "status": "new",
    }


def merge_and_save(existing_jobs, new_jobs):
    """Merge new jobs into existing list, deduplicate by jobName, save."""
    existing_names = {j.get("jobName", "") for j in existing_jobs}
    added = 0
    next_id_counter = None

    for job in new_jobs:
        if job["jobName"] in existing_names:
            continue
        # Assign fresh ID
        if next_id_counter is None:
            # Recalculate from existing + already-added
            all_jobs = existing_jobs + [j for j in new_jobs if j != job]
            next_id_counter = int(next_id(all_jobs).split("-")[-1])
        next_id_counter += 1
        year = datetime.now().strftime("%Y")
        job["id"] = f"CCE-{year}-{next_id_counter:04d}"
        existing_jobs.append(job)
        existing_names.add(job["jobName"])
        added += 1

    # Sort by bidDueDate (nulls last)
    existing_jobs.sort(key=lambda j: (j.get("bidDueDate") is None, j.get("bidDueDate", "")))

    data = {
        "board": "Concord CE — Job Opportunity Board",
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "jobs": existing_jobs,
    }

    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")

    return added


def commit_and_push():
    """Commit data.json changes and push to origin."""
    os.chdir(REPO_DIR)
    subprocess.run(["git", "add", "data.json"], check=True)
    result = subprocess.run(
        ["git", "commit", "-m", f"Sync: HigherGov update — {datetime.now().strftime('%Y-%m-%d %H:%M')}"],
        capture_output=True, text=True
    )
    # Exit 1 means nothing to commit — that's fine
    if result.returncode == 0 or "nothing to commit" in result.stdout + result.stderr:
        subprocess.run(["git", "push"], check=True)
        return True
    return False


def main():
    print("=== HigherGov → data.json sync ===")

    # Load existing
    existing = load_existing()
    print(f"Existing jobs: {len(existing)}")

    # Pull HigherGov
    print("Pulling HigherGov...")
    raw = pull_highergov()
    print(f"HigherGov results (filtered): {len(raw)}")

    if not raw:
        print("No new opportunities. Done.")
        return

    # Convert to job entries
    new_jobs = []
    base_id = int(next_id(existing).split("-")[-1])
    for i, opp in enumerate(raw):
        nid = f"CCE-{datetime.now().strftime('%Y')}-{base_id + i:04d}"
        new_jobs.append(opp_to_job(opp, nid))

    # Merge and save
    added = merge_and_save(existing, new_jobs)
    print(f"Added: {added} new jobs")

    if added > 0:
        commit_and_push()
        print("Committed and pushed to GitHub.")
    else:
        print("No changes to commit.")

    print(f"Board: https://leverxyz.github.io/job-opportunities/")


if __name__ == "__main__":
    main()
