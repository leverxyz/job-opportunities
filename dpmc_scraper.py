#!/usr/bin/env python3
"""
DPMC Scraper — NJ Treasury Project Advertisements
§3 of sourcing-rules.md
Static HTML table parsing — no browser needed.
"""
import re, json
from datetime import datetime, timezone
import httpx

DPMC_URL = "https://www.nj.gov/treasury/dpmc/project_construction_advertisements.shtml"
BASE_URL = "https://www.nj.gov/treasury/dpmc/"

# --- Keyword sets (from §0.7) ---
ROOFING_KEYWORDS = [
    "roof", "roofing", "reroof", "re-roof", "membrane", "epdm", "tpo", "sbs",
    "pvc roof", "shingle", "standing seam", "built-up roof", "bur", "flashing",
    "roof deck", "skylight",
]

ROOFING_EXCLUSIONS = ["rooftop unit", "roof top unit", "rtu"]

MEP_KEYWORDS = [
    "mechanical", "electrical", "plumbing", "hvac", "mep", "boiler", "chiller",
    "fire protection", "sprinkler", "fire alarm",
]

ENVIRONMENTAL_KEYWORDS = [
    "environmental remediation", "remediation", "abatement", "asbestos",
    "lead abatement", "mold", "soil", "groundwater", "hazardous waste",
    "wetlands", "ust removal", "tank removal",
]

def wb_match(keyword, text):
    pattern = r'\b' + re.escape(keyword) + r'\b'
    return bool(re.search(pattern, text, re.IGNORECASE))

def any_kw_match(keywords, text):
    for kw in keywords:
        if wb_match(kw, text):
            return True
    return False


def fetch_and_parse():
    """Fetch DPMC page and return list of parsed project dicts."""
    resp = httpx.get(DPMC_URL, timeout=30)
    html = resp.text

    # Find tbody
    tbody_match = re.search(r'<tbody>(.*?)</tbody>', html, re.DOTALL)
    if not tbody_match:
        print("DPMC: no tbody found")
        return []

    tbody = tbody_match.group(1)
    rows = re.findall(r'<tr>(.*?)</tr>', tbody, re.DOTALL)

    projects = []
    for row in rows:
        cells = re.findall(r'<td[^>]*>(.*?)</td>', row, re.DOTALL)
        if len(cells) < 4:
            continue

        proj_num = re.sub(r'<[^>]+>', '', cells[0]).strip()
        desc_raw = re.sub(r'<[^>]+>', ' ', cells[1]).strip()
        desc_raw = re.sub(r'\s+', ' ', desc_raw)
        cost_raw = re.sub(r'<[^>]+>', '', cells[2]).strip()
        due_raw = re.sub(r'<[^>]+>', '', cells[3]).strip()

        # Status cell (cell 4) — contains links for sign-in sheet, bid results, award
        status_cell = cells[4] if len(cells) > 4 else ""
        status_links = re.findall(r'href="([^"]+)"', status_cell)
        status_text = re.sub(r'<[^>]+>', ' ', status_cell).strip()
        status_text = re.sub(r'\s+', ' ', status_text)

        if not proj_num or not desc_raw:
            continue

        # Parse cost
        cost = 0
        cost_str = cost_raw.replace("$", "").replace(",", "").strip()
        try:
            cost = int(float(cost_str))
        except ValueError:
            cost = 0

        # Parse due date
        due_date = None
        try:
            due_date = datetime.strptime(due_raw.strip(), "%m/%d/%Y").strftime("%Y-%m-%d")
        except ValueError:
            # Try extended date (second date)
            parts = due_raw.split()
            for p in parts:
                try:
                    due_date = datetime.strptime(p.strip(), "%m/%d/%Y").strftime("%Y-%m-%d")
                    break
                except ValueError:
                    continue

        # Extract location from description
        location = desc_raw
        city = ""
        # Try to find "City, County, NJ" pattern
        loc_match = re.search(r'([A-Z][a-z]+(?:[\s-][A-Z][a-z]+)*),\s*([A-Z][a-z]+(?:\s+County)?),\s*NJ', desc_raw)
        if loc_match:
            city = loc_match.group(1)

        # Status determination
        status = "new"
        if "cancelled" in status_text.lower():
            status = "cancelled"
        elif "award" in status_text.lower() or "ntp" in status_text.lower():
            status = "awarded"
        elif "bid result" in status_text.lower():
            status = "bid_opened"
        elif "sign-in" in status_text.lower():
            status = "pre_bid_held"

        projects.append({
            "project_number": proj_num,
            "description": desc_raw,
            "cost": cost,
            "cost_display": cost_raw,
            "due_date": due_date,
            "due_date_display": due_raw,
            "city": city,
            "status_links": status_links,
            "status_text": status_text,
            "dpmc_status": status,
        })

    return projects


def classify_dpmc(proj):
    """§0.2 lane routing for NJ state-funded DPMC projects."""
    desc = proj["description"].lower()
    cost = proj["cost"]

    # Check roofing exclusions first
    has_exclusion = any_kw_match(ROOFING_EXCLUSIONS, desc)
    has_roofing = any_kw_match(ROOFING_KEYWORDS, desc) and not has_exclusion
    has_envelope = any_kw_match(["siding", "window replacement", "windows", "facade",
                                  "building envelope", "exterior restoration",
                                  "masonry restoration", "brick"], desc) and not has_exclusion
    has_mep = any_kw_match(MEP_KEYWORDS, desc)
    has_env = any_kw_match(ENVIRONMENTAL_KEYWORDS, desc)

    # Already awarded or cancelled — don't board new, keep for lifecycle tracking
    if proj["dpmc_status"] in ("awarded", "cancelled"):
        return ("SKIP", "", f"DPMC already {proj['dpmc_status']}", True)

    # Past due
    if proj["due_date"]:
        try:
            due = datetime.strptime(proj["due_date"], "%Y-%m-%d").date()
            today = datetime.now(timezone.utc).date()
            if due < today:
                return ("SKIP", "", "past due", True)
            if (due - today).days < 5:
                return ("SKIP", "", f"short window: {(due-today).days}d", True)
        except ValueError:
            pass

    # No roofing at all → skip
    if not has_roofing and not has_envelope:
        if has_mep:
            return ("SKIP", "", "MEP — no roofing scope", True)
        if has_env:
            return ("SKIP", "", "environmental — no roofing scope", True)
        return ("SKIP", "", "no roofing/envelope scope", True)

    # Has roofing — route to lanes
    is_strictly_roofing = has_roofing and not has_envelope
    if is_strictly_roofing:
        # $25K floor doesn't apply to Lane 1 / roofing
        return ("Lane 1", "APPLIED-PRIME", "NJ DPMC — strictly roofing", False)

    # Mixed envelope
    if has_roofing and has_envelope:
        return ("CHIEF-REVIEW", "", "NJ DPMC — mixed roof+envelope", False)

    # Roofing in larger project
    if cost > 0 and cost < 25000:
        return ("SKIP", "", f"below $25K floor", True)

    return ("Lane 2", "APPLIED-SUB", "NJ DPMC — roofing sub in larger project", False)


def dpmc_to_job(proj, tier_label, tag, reasoning):
    """Convert DPMC project to board job entry."""
    key_dates = f"Bid Due: {proj['due_date_display']}"

    # Try to format the date nicer
    if proj["due_date"]:
        try:
            d = datetime.strptime(proj["due_date"], "%Y-%m-%d")
            key_dates = f"Bid Due: {d.strftime('%b %d, %Y').replace(' 0', ' ')}"
        except:
            pass

    location = f"{proj['city']}, NJ\nLocal — NJ state work" if proj["city"] else "NJ\nLocal — NJ state work"

    return {
        "opportunityId": f"dpmc-{proj['project_number']}",
        "jobName": proj["description"][:200],
        "projectDescription": proj["description"][:500],
        "tier": tier_label,
        "tag": tag,
        "reasoning": reasoning,
        "setAside": "NONE",
        "sourceType": "DPMC",
        "sourceDetail": "NJ DPMC",
        "location": location,
        "magnitude": proj["cost_display"],
        "keyDates": key_dates,
        "bidDueDate": proj["due_date"],
        "status": "new",
        "naics": "",
        "link": DPMC_URL,
        "distanceTier": "D1 Core",
        "dpmcStatus": proj["dpmc_status"],
    }
