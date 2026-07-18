#!/usr/bin/env python3
"""
hg.py — HigherGov diagnostic + pull tool for Sam.

Fixed sub-commands, allowlisted so NO per-run approval is needed.
Mirrors the dbx.py pattern: no shell operators, no `python3 -c`,
stdlib-only (urllib) so plain `python3` works — no venv, no httpx.

Usage:
  hg.py health              Ping the API, report up/down + latency
  hg.py pull [--search ID]  Pull search results -> /tmp/hg_page1.json
  hg.py summary             Analyze last pull: states, values, set-asides, roofing hits

Key is read from ~/.hermes/.env (HIGHERGOV_API_KEY=...) or the environment.
"""

import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path

# --- config -----------------------------------------------------------------
# NOTE: verify endpoint + param names once against the government-sourcing
# skill's highergov-api-reference.md before trusting output. Flagged in chat.
API_BASE = "https://www.highergov.com/api-external/opportunity/"
DEFAULT_SEARCH_ID = "RUjIV5pvOv6TKsq2QJbUA"  # search #1 (Northeast federal)
CACHE = Path("/tmp/hg_page1.json")
ENV_FILE = Path.home() / ".hermes" / ".env"
TIMEOUT = 30


def load_key():
    key = os.environ.get("HIGHERGOV_API_KEY")
    if key:
        return key.strip()
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if line.startswith("HIGHERGOV_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    print("ERROR: HIGHERGOV_API_KEY not found in env or ~/.hermes/.env")
    sys.exit(1)


def build_url(search_id):
    params = {"api_key": load_key(), "search_id": search_id}
    return API_BASE + "?" + urllib.parse.urlencode(params)


def fetch(search_id):
    url = build_url(search_id)
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return resp.status, resp.read()


# --- commands ---------------------------------------------------------------
def cmd_health(_args):
    search_id = DEFAULT_SEARCH_ID
    t0 = time.time()
    try:
        status, body = fetch(search_id)
        ms = int((time.time() - t0) * 1000)
        try:
            n = len(json.loads(body).get("results", []))
        except Exception:
            n = "?"
        if status == 200:
            print(f"UP   HTTP {status}  {ms} ms  results={n}")
        else:
            print(f"DOWN HTTP {status}  {ms} ms  (non-200)")
    except Exception as e:
        ms = int((time.time() - t0) * 1000)
        print(f"DOWN {type(e).__name__}: {e}  ({ms} ms)")
        sys.exit(1)


def cmd_pull(args):
    search_id = DEFAULT_SEARCH_ID
    if "--search" in args:
        i = args.index("--search")
        if i + 1 < len(args):
            search_id = args[i + 1]
    try:
        status, body = fetch(search_id)
    except Exception as e:
        print(f"PULL FAILED  {type(e).__name__}: {e}")
        sys.exit(1)
    if status != 200:
        print(f"PULL FAILED  HTTP {status}")
        sys.exit(1)
    CACHE.write_bytes(body)
    n = len(json.loads(body).get("results", []))
    print(f"OK  search={search_id}  results={n}  -> {CACHE}")


def cmd_summary(_args):
    if not CACHE.exists():
        print(f"No cache at {CACHE} — run `hg.py pull` first.")
        sys.exit(1)
    data = json.loads(CACHE.read_text())
    results = data.get("results", [])
    print(f"Total results: {len(results)}\n")

    states = Counter()
    buckets = Counter()
    setasides = Counter()
    for r in results:
        states[(r.get("pop_state") or "UNKNOWN").strip()] += 1
        setasides[(r.get("set_aside") or "NONE").strip() or "NONE"] += 1
        low_raw, high_raw = r.get("val_est_low"), r.get("val_est_high")
        low = int(low_raw) if low_raw else None
        high = int(high_raw) if high_raw else None
        avg = (low + high) / 2 if low and high else (high or low)
        if avg is None:
            buckets["UNKNOWN"] += 1
        elif avg < 100_000:
            buckets["<100K"] += 1
        elif avg < 500_000:
            buckets["100K-500K"] += 1
        elif avg < 2_000_000:
            buckets["500K-2M"] += 1
        elif avg < 10_000_000:
            buckets["2M-10M"] += 1
        else:
            buckets[">10M"] += 1

    print("=== STATE DISTRIBUTION ===")
    for s, c in states.most_common():
        print(f"  {s}: {c}")
    print("\n=== SIZE BUCKETS ===")
    for b in ["<100K", "100K-500K", "500K-2M", "2M-10M", ">10M", "UNKNOWN"]:
        print(f"  {b}: {buckets[b]}")
    print("\n=== SET-ASIDE BREAKDOWN ===")
    for sa, c in setasides.most_common():
        print(f"  {sa}: {c}")

    roofing_kw = [
        "roof", "roofing", "reroof", "re-roof", "membrane", "epdm", "tpo",
        "sbs", "shingle", "standing seam", "bur", "flashing", "skylight",
        "siding", "window replacement", "windows", "facade",
        "building envelope", "exterior restoration", "masonry restoration",
        "brick",
    ]
    rtu_trap = re.compile(r"\brooftop unit\b|\broof top unit\b|\brtu\b")
    hits = []
    for r in results:
        text = ((r.get("title") or "") + " " + (r.get("description_text") or "")).lower()
        has_roof = any(re.search(r"\b" + re.escape(k) + r"\b", text) for k in roofing_kw)
        if has_roof and not rtu_trap.search(text):
            hits.append(r)

    print(f"\n=== ROOFING HITS: {len(hits)} ===")
    for r in hits[:10]:
        low_raw, high_raw = r.get("val_est_low"), r.get("val_est_high")
        low = int(low_raw) if low_raw else None
        high = int(high_raw) if high_raw else None
        val = f"${low/1000:.0f}K-${high/1000:.0f}K" if low and high else "Unknown"
        sa = r.get("set_aside") or "NONE"
        print(f"  [{sa}] {(r.get('title') or '')[:80]}")
        print(f"    {r.get('pop_state', '')} | {val} | Due: {r.get('due_date', '')}")


def _audit(args):
    """Append one line per invocation. Never raises."""
    try:
        from datetime import datetime, timezone
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        line = ts + "  " + " ".join(str(a) for a in args) + "\n"
        with open(Path(__file__).resolve().parent / "hg_audit.log", "a", encoding="utf-8") as fh:
            fh.write(line)
    except Exception:
        pass


COMMANDS = {"health": cmd_health, "pull": cmd_pull, "summary": cmd_summary}


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(f"Usage: hg.py <{' | '.join(COMMANDS)}> [args]")
        sys.exit(1)
    _audit(sys.argv[1:])
    COMMANDS[sys.argv[1]](sys.argv[2:])


if __name__ == "__main__":
    main()
