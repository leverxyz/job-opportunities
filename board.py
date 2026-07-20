#!/usr/bin/env python3
"""
board.py — Sam's tool for adding/editing/deleting Project Opportunity Board
entries (verbal leads, Telegram, forwarded email, a link Chief sends, etc.).

Fixed sub-commands, allowlisted so NO per-run approval is needed. Writes
data.json via git (pull --ff-only, edit, commit, push, retry once on a
race) -- the same file and the same rules the board page's own Add/Edit/
Delete buttons use (built 2026-07-18, see index.html and sync.py). This
tool exists so those rules live in code instead of something Sam has to
remember: an untiered entry gets silently purged by the next sync, a
Board Tab outside the 4 real ones is invisible everywhere on the board,
and a delete that doesn't update excludedKeys will resurrect on the next
HigherGov/DPMC sync.

NOTE ON DUPLICATION: the JSON read/write helpers here intentionally mirror
sync.py's load_existing()/load_excluded_keys()/merge shape rather than
importing sync.py (which pulls in httpx, dpmc_scraper, and a lot of
unrelated pipeline code for no reason). If data.json's schema ever
changes, update both files.

Usage:
  board.py list [--tab TAB]
  board.py find <query>
  board.py add --name NAME --tier "Tier N" [--tab TAB] [options]
  board.py edit <id> [--name NAME] [--tier "Tier N"] [options]
  board.py delete <id>
  board.py interested <id>       -- human "maybe" signal, cosmetic, no downstream effect
  board.py pursue <id>           -- the actuator (Section 5) -- starts Sam's intake work
  board.py stop-pursuing <id>    -- reverses a pursue: declined + interested cleared together
  board.py intake-done <id>      -- marks intake complete, stops the watchdog re-matching it
  board.py intake-claim <id>     -- intake-watch.py only; claims a job so overlapping runs don't collide

Board Tab must be one of: HigherGov, BidNet, DPMC, "GC Email" (default
for `add` is "GC Email" -- most manual adds are GC-sourced leads).
Tier must be one of: "Tier 1" .. "Tier 5" (tag is auto-derived).
"""

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent
DATA_FILE = REPO_DIR / "data.json"
AUDIT_LOG = REPO_DIR / "board_audit.log"

VALID_TABS = ["HigherGov", "BidNet", "DPMC", "GC Email"]
TIER_TAG = {
    "Tier 1": "APPLIED-PRIME",
    "Tier 2": "CONCORD-PRIME",
    "Tier 3": "APPLIED-SUB",
    "Tier 4": "CONCORD-SUB",
    "Tier 5": "APPLIED-SUB-MEP",
}


def _audit(args):
    """Append one line per invocation. Never raises."""
    try:
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        line = ts + "  " + " ".join(str(a) for a in args) + "\n"
        with open(AUDIT_LOG, "a", encoding="utf-8") as fh:
            fh.write(line)
    except Exception:
        pass


def die(msg):
    print(f"ERROR: {msg}")
    sys.exit(1)


def run_git(*args, check=True):
    return subprocess.run(["git", "-C", str(REPO_DIR), *args], capture_output=True, text=True, check=check)


def pull_latest():
    """Fast-forward-only. See sync.py's pull_latest() for the full reasoning
    (board page writes via the GitHub API, cron writes via git -- this tool
    is a third writer, all three must never work from a stale local file)."""
    run_git("fetch", "origin")
    result = run_git("merge", "--ff-only", "origin/main", check=False)
    if result.returncode != 0:
        die(f"could not sync with the live board (history diverged): {result.stderr.strip()}\nSomeone needs to look at this by hand -- do not force anything.")


def load():
    if not DATA_FILE.exists():
        return [], [], []
    with open(DATA_FILE) as f:
        data = json.load(f)
    return data.get("jobs", []), data.get("excludedKeys", []), data.get("signals", [])


def save(jobs, excluded_keys, signals, commit_message):
    payload = {
        "board": "Project Opportunity Board",
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "jobs": jobs,
        "excludedKeys": sorted(set(excluded_keys)),
        "signals": signals,
    }
    with open(DATA_FILE, "w") as f:
        json.dump(payload, f, indent=2)
        f.write("\n")
    run_git("add", "data.json")
    commit = run_git("commit", "-m", commit_message, check=False)
    if commit.returncode != 0:
        die(f"nothing to commit or commit failed: {commit.stderr.strip()}")
    push = run_git("push", check=False)
    if push.returncode != 0:
        # Someone (the cron, the board page can't push directly but another
        # board.py invocation could) moved origin/main since we pulled.
        # Re-sync and retry exactly once -- never force-push.
        pull_result = run_git("pull", "--rebase", check=False)
        if pull_result.returncode != 0:
            die(f"push conflicted and the automatic retry failed: {pull_result.stderr.strip()}\nSomeone needs to look at this by hand.")
        push2 = run_git("push", check=False)
        if push2.returncode != 0:
            die(f"push failed twice: {push2.stderr.strip()}")


def make_key(job):
    return f"{job.get('sourceType', '')}:{job.get('opportunityId', job.get('id', ''))}"


def find_job(jobs, job_id):
    for j in jobs:
        if j.get("opportunityId", j.get("id")) == job_id:
            return j
    return None


def fmt_row(j):
    tier = j.get("tier") or "—"
    tab = j.get("sourceType") or "?"
    detail = j.get("sourceDetail") or ""
    mag = j.get("magnitude") or "Unknown"
    due = j.get("bidDueDate") or "TBD"
    jid = j.get("opportunityId", j.get("id", "?"))
    return f"[{tier:7s}] {tab:10s} {j.get('jobName',''):50.50s}  {detail:20.20s}  {mag:12s}  Bid Due: {due}\n    id: {jid}"


# --- commands ----------------------------------------------------------

def cmd_list(a):
    jobs, _, _ = load()
    if a.tab:
        if a.tab not in VALID_TABS:
            die(f"--tab must be one of: {', '.join(VALID_TABS)}")
        jobs = [j for j in jobs if j.get("sourceType") == a.tab]
    jobs = [j for j in jobs if (j.get("status") or "").lower() not in ("expired", "archive", "declined")]
    jobs.sort(key=lambda j: (j.get("bidDueDate") is None, j.get("bidDueDate", "")))
    print(f"{len(jobs)} active opportunit{'y' if len(jobs)==1 else 'ies'}" + (f" on {a.tab}" if a.tab else "") + "\n")
    for j in jobs:
        print(fmt_row(j))


def cmd_find(a):
    jobs, _, _ = load()
    q = a.query.lower()
    hits = [j for j in jobs if q in (j.get("jobName", "") + " " + j.get("sourceDetail", "")).lower()]
    if not hits:
        print(f"No match for \"{a.query}\". Try a shorter fragment of the name or company.")
        return
    print(f"{len(hits)} match(es):\n")
    for j in hits:
        print(fmt_row(j))


def _build_job(a, existing=None):
    job = dict(existing) if existing else {}
    if a.name is not None:
        job["jobName"] = a.name
    if a.tier is not None:
        if a.tier not in TIER_TAG:
            die(f"--tier must be one of: {', '.join(TIER_TAG)}")
        job["tier"] = a.tier
        job["tag"] = TIER_TAG[a.tier]
    if a.tab is not None:
        if a.tab not in VALID_TABS:
            die(f"--tab must be one of: {', '.join(VALID_TABS)}")
        job["sourceType"] = a.tab
    if a.source_detail is not None:
        job["sourceDetail"] = a.source_detail
    if a.desc is not None:
        job["projectDescription"] = a.desc
    if a.location is not None:
        job["location"] = a.location
    if a.magnitude is not None:
        job["magnitude"] = a.magnitude
    if a.bid_due is not None or a.site_visit is not None or a.rfi_due is not None:
        if a.bid_due is not None:
            job["bidDueDate"] = a.bid_due or None
        if a.site_visit is not None:
            job["siteVisitDate"] = a.site_visit or None
        if a.rfi_due is not None:
            job["rfiDueDate"] = a.rfi_due or None
        kd = []
        if job.get("siteVisitDate"):
            kd.append(f"Site Visit: {job['siteVisitDate']}")
        if job.get("rfiDueDate"):
            kd.append(f"RFI Due: {job['rfiDueDate']}")
        kd.append(f"Bid Due: {job.get('bidDueDate') or 'TBD'}")
        job["keyDates"] = "\n".join(kd)
    job.setdefault("setAside", "NONE")
    job.setdefault("status", "new")
    job.setdefault("magnitude", "Unknown")
    return job


def cmd_add(a):
    if not a.name:
        die("--name is required")
    if not a.tier:
        die("--tier is required (Tier 1-5) -- an untiered entry gets removed by the next daily sync")
    if a.tier not in TIER_TAG:
        die(f"--tier must be one of: {', '.join(TIER_TAG)}")
    if a.tab and a.tab not in VALID_TABS:
        die(f"--tab must be one of: {', '.join(VALID_TABS)}")

    pull_latest()
    jobs, excluded, signals = load()

    job = _build_job(a)
    job.setdefault("sourceType", "GC Email")
    job["opportunityId"] = f"manual-{int(datetime.now().timestamp())}"
    jobs.insert(0, job)

    save(jobs, excluded, signals, f"board.py add ({a.actor}): {job['sourceType']} — {job['jobName']}")
    print(f"Added.\n{fmt_row(job)}\nhttps://leverxyz.github.io/job-opportunities/")


def cmd_edit(a):
    pull_latest()
    jobs, excluded, signals = load()
    existing = find_job(jobs, a.id)
    if not existing:
        die(f"no opportunity with id {a.id} -- use `board.py find <text>` to look it up first")

    updated = _build_job(a, existing=existing)
    idx = jobs.index(existing)
    jobs[idx] = updated

    save(jobs, excluded, signals, f"board.py edit ({a.actor}): {updated.get('sourceType','')} {a.id}")
    print(f"Updated.\n{fmt_row(updated)}\nhttps://leverxyz.github.io/job-opportunities/")


def cmd_delete(a):
    pull_latest()
    jobs, excluded, signals = load()
    existing = find_job(jobs, a.id)
    if not existing:
        die(f"no opportunity with id {a.id} -- use `board.py find <text>` to look it up first")

    key = make_key(existing)
    jobs = [j for j in jobs if make_key(j) != key]
    if key not in excluded:
        excluded = excluded + [key]

    save(jobs, excluded, signals, f"board.py delete ({a.actor}): {key}")
    print(f"Deleted permanently — will not reappear on the next sync.\n  was: {existing.get('jobName','')}")


def cmd_interested(a):
    """Chief said yes to this one. Highlights light green on the board and
    logs a signal for the future learning loop. Toggle, not one-way -- a
    second call clears it (no new signal logged on the way back off; the
    log only records genuine "yes" moments, not corrections)."""
    pull_latest()
    jobs, excluded, signals = load()
    existing = find_job(jobs, a.id)
    if not existing:
        die(f"no opportunity with id {a.id} -- use `board.py find <text>` to look it up first")

    idx = jobs.index(existing)
    now_interested = not jobs[idx].get("interested")
    jobs[idx] = dict(jobs[idx], interested=now_interested)

    if now_interested:
        signals = signals + [{
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "type": "interested",
            "actor": a.actor,
            "sourceType": jobs[idx].get("sourceType", ""),
            "opportunityId": a.id,
            "jobName": jobs[idx].get("jobName", ""),
            "tier": jobs[idx].get("tier"),
            "tag": jobs[idx].get("tag"),
            "sourceDetail": jobs[idx].get("sourceDetail", ""),
            "magnitude": jobs[idx].get("magnitude", ""),
            "distanceTier": jobs[idx].get("distanceTier", ""),
        }]

    save(jobs, excluded, signals, f"board.py interested ({a.actor}): {'on' if now_interested else 'off'} {a.id}")
    print(("Marked interested." if now_interested else "Un-marked (no longer interested).") + f"\n{fmt_row(jobs[idx])}")


def cmd_pursue(a):
    """The actuator (Section 5, intake.md) -- this is what wakes Sam's
    intake work: Dropbox folder creation, doc pull, scope note, one
    Telegram summary. See project-intake skill for what happens next;
    this command only flips the state.

    Idempotent, not one-way strict: calling it again on an already-
    pursuing job is a no-op message, not an error -- the intake watchdog
    may legitimately see the same job across more than one run while
    intake is mid-flight, and that must not blow up."""
    pull_latest()
    jobs, excluded, signals = load()
    existing = find_job(jobs, a.id)
    if not existing:
        die(f"no opportunity with id {a.id} -- use `board.py find <text>` to look it up first")

    idx = jobs.index(existing)
    if jobs[idx].get("status") == "pursuing":
        print(f"Already pursuing -- no change.\n{fmt_row(jobs[idx])}")
        return

    jobs[idx] = dict(
        jobs[idx],
        status="pursuing",
        pursuingStartedAt=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )

    signals = signals + [{
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "type": "pursuing_started",
        "actor": a.actor,
        "sourceType": jobs[idx].get("sourceType", ""),
        "opportunityId": a.id,
        "jobName": jobs[idx].get("jobName", ""),
        "tier": jobs[idx].get("tier"),
        "tag": jobs[idx].get("tag"),
        "sourceDetail": jobs[idx].get("sourceDetail", ""),
        "magnitude": jobs[idx].get("magnitude", ""),
        "distanceTier": jobs[idx].get("distanceTier", ""),
    }]

    save(jobs, excluded, signals, f"board.py pursue ({a.actor}): {a.id}")
    print(f"Marked pursuing -- intake starts on the next watchdog cycle.\n{fmt_row(jobs[idx])}")


def cmd_stop_pursuing(a):
    """Reverses a pursue -- for when intake already started (folder made,
    Telegram sent) but the answer turns out to be no. Sets status to a
    terminal 'declined' AND clears interested in the SAME write; both
    must flip together, or the watchdog would see interested=true again
    next cycle and re-trigger intake on a job Chief already killed.

    dbx.py has no delete/move (by design, same restraint as everywhere
    else in this codebase) -- any Dropbox folder already created is not
    touched here. Print a reminder, don't pretend to clean it up; the
    exact folder-path convention isn't box-verified enough yet to compute
    and assert a specific path from this layer."""
    pull_latest()
    jobs, excluded, signals = load()
    existing = find_job(jobs, a.id)
    if not existing:
        die(f"no opportunity with id {a.id} -- use `board.py find <text>` to look it up first")

    idx = jobs.index(existing)
    jobs[idx] = dict(jobs[idx], status="declined", interested=False)

    signals = signals + [{
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "type": "declined_after_intake",
        "actor": a.actor,
        "sourceType": jobs[idx].get("sourceType", ""),
        "opportunityId": a.id,
        "jobName": jobs[idx].get("jobName", ""),
        "tier": jobs[idx].get("tier"),
        "tag": jobs[idx].get("tag"),
        "sourceDetail": jobs[idx].get("sourceDetail", ""),
        "magnitude": jobs[idx].get("magnitude", ""),
        "distanceTier": jobs[idx].get("distanceTier", ""),
    }]

    save(jobs, excluded, signals, f"board.py stop-pursuing ({a.actor}): {a.id}")
    print(
        "Marked declined -- pulled off the active board.\n"
        f"{fmt_row(jobs[idx])}\n"
        "If Sam already created a Dropbox pursuit folder for this job, it has NOT been "
        "deleted -- dbx.py has no delete/move capability, by design. Clean it up by hand "
        "if you want it gone."
    )


def cmd_intake_claim(a):
    """Called by intake-watch.py, once per job, right before it hands that
    job's ---INTAKE--- block to an agent session -- NOT called by Sam
    directly. Stamps intakeClaimedAt so a second watchdog run (a manual
    re-trigger overlapping a still-running one, or a run that's slow for
    some reason) sees the job as already being worked and skips it,
    instead of both sessions independently creating their own Dropbox
    folder for the same job. Real bug, not hypothetical: found live
    2026-07-20 during Section 5 testing -- two overlapping runs each
    created a folder for the same test job, and dbx.py has no delete to
    clean up the duplicate with.

    Claims go stale after 20 minutes (see intake-watch.py's
    CLAIM_STALE_MINUTES) so a crashed or genuinely-stuck session doesn't
    lock a job out forever -- the next scheduled tick just re-claims it."""
    pull_latest()
    jobs, excluded, signals = load()
    existing = find_job(jobs, a.id)
    if not existing:
        die(f"no opportunity with id {a.id} -- use `board.py find <text>` to look it up first")

    idx = jobs.index(existing)
    jobs[idx] = dict(
        jobs[idx],
        intakeClaimedAt=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )

    save(jobs, excluded, signals, f"board.py intake-claim ({a.actor}): {a.id}")
    print(f"Claimed.\n{fmt_row(jobs[idx])}")


def cmd_intake_done(a):
    """Closes the loop intake-watch.py's watchdog opens. `status` alone
    can't tell "just marked pursuing" from "already had intake run" --
    it stays "pursuing" forever once intake finishes, that's correct,
    the job doesn't stop being pursued. So the watchdog matches on
    status=="pursuing" AND intakeCompletedAt not set; this command is
    the only thing that sets intakeCompletedAt, which is what makes a
    job stop re-matching every cycle.

    The project-intake skill must call this ONLY after step 5's `dbx
    find`/`ls` verification confirms the folder and note actually exist
    -- never on Sam's narration alone (intake.md's "trust the box, never
    Sam's narration" rule, the single most important rule in that spec).
    Calling this without that verification silently drops a job out of
    the watchdog's view with no folder behind it -- exactly the failure
    mode the rule exists to prevent."""
    pull_latest()
    jobs, excluded, signals = load()
    existing = find_job(jobs, a.id)
    if not existing:
        die(f"no opportunity with id {a.id} -- use `board.py find <text>` to look it up first")

    idx = jobs.index(existing)
    jobs[idx] = dict(
        jobs[idx],
        intakeCompletedAt=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )

    signals = signals + [{
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "type": "intake_completed",
        "actor": a.actor,
        "sourceType": jobs[idx].get("sourceType", ""),
        "opportunityId": a.id,
        "jobName": jobs[idx].get("jobName", ""),
    }]

    save(jobs, excluded, signals, f"board.py intake-done ({a.actor}): {a.id}")
    print(f"Intake marked complete -- will no longer appear in the watchdog's queue.\n{fmt_row(jobs[idx])}")


# --- CLI -----------------------------------------------------------------

def build_parser():
    p = argparse.ArgumentParser(prog="board.py", description="Add/edit/delete Project Opportunity Board entries.")
    sub = p.add_subparsers(dest="command", required=True)

    def add_common_fields(sp, name_required=False):
        sp.add_argument("--actor", default="sam",
                         help="Who's performing this action -- \"sam\" (default, his own "
                              "invocations) or a web login username. Goes in the audit log "
                              "and the git commit message.")
        sp.add_argument("--name", help="Project/opportunity name")
        sp.add_argument("--tier", help="Tier 1-5 (auto-derives tag)")
        sp.add_argument("--tab", help="HigherGov, BidNet, DPMC, or \"GC Email\"")
        sp.add_argument("--source-detail", help="Company/agency name, e.g. K2CG, Riley, Kaiser")
        sp.add_argument("--desc", help="Short description")
        sp.add_argument("--location", help="City, ST")
        sp.add_argument("--magnitude", help='Value, e.g. "$500K – $1M"')
        sp.add_argument("--bid-due", help="YYYY-MM-DD")
        sp.add_argument("--site-visit", help="YYYY-MM-DD")
        sp.add_argument("--rfi-due", help="YYYY-MM-DD")

    sp_list = sub.add_parser("list", help="List active opportunities")
    sp_list.add_argument("--tab", help="Filter to one Board Tab")

    sp_find = sub.add_parser("find", help="Search job names/companies for a match (to get an id)")
    sp_find.add_argument("query")

    sp_add = sub.add_parser("add", help="Add a new opportunity")
    add_common_fields(sp_add)

    sp_edit = sub.add_parser("edit", help="Edit an existing opportunity by id")
    sp_edit.add_argument("id")
    add_common_fields(sp_edit)

    sp_del = sub.add_parser("delete", help="Permanently delete an opportunity by id")
    sp_del.add_argument("id")
    sp_del.add_argument("--actor", default="sam", help="Who's performing this delete -- \"sam\" or a web login username")

    sp_int = sub.add_parser("interested", help="Toggle Chief's interest on an opportunity (highlights it, logs a signal)")
    sp_int.add_argument("id")
    sp_int.add_argument("--actor", default="sam", help="Who's toggling this -- \"sam\" or a web login username")

    sp_pursue = sub.add_parser("pursue", help="Mark an opportunity as actively pursued -- starts Sam's intake work")
    sp_pursue.add_argument("id")
    sp_pursue.add_argument("--actor", default="sam", help="Who's pursuing this -- \"sam\" or a web login username")

    sp_stop = sub.add_parser("stop-pursuing", help="Reverse a pursue -- marks declined, clears interested")
    sp_stop.add_argument("id")
    sp_stop.add_argument("--actor", default="sam", help="Who's stopping this -- \"sam\" or a web login username")

    sp_done = sub.add_parser("intake-done", help="Mark intake complete (folder+note verified) -- stops the watchdog re-matching this job")
    sp_done.add_argument("id")
    sp_done.add_argument("--actor", default="sam", help="Who's marking this done -- almost always \"sam\"")

    sp_claim = sub.add_parser("intake-claim", help="Called by intake-watch.py, not Sam -- claims a job so a second overlapping watchdog run skips it")
    sp_claim.add_argument("id")
    sp_claim.add_argument("--actor", default="intake-watch", help="Almost always \"intake-watch\" (the script), not \"sam\"")

    return p


def main():
    parser = build_parser()
    args = parser.parse_args()
    _audit(sys.argv[1:])
    {
        "list": cmd_list,
        "find": cmd_find,
        "add": cmd_add,
        "edit": cmd_edit,
        "delete": cmd_delete,
        "interested": cmd_interested,
        "pursue": cmd_pursue,
        "stop-pursuing": cmd_stop_pursuing,
        "intake-done": cmd_intake_done,
        "intake-claim": cmd_intake_claim,
    }[args.command](args)


if __name__ == "__main__":
    try:
        main()
    except BrokenPipeError:
        sys.exit(0)
