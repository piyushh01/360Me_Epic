import os
import requests
from app.config import EPIC_SNAPSHOT_DIR
from app.clients.jira import (
    fetch_all_projects, resolve_single_epic, fetch_epic_metadata,
    fetch_epic_child_status_counts, fetch_epic_daily_trend, fetch_epic_attention_items,
    fetch_project_epics_with_activity
)
from app.presentation.html_views import generate_epic_html_report
from app.clients.slack import upload_file_to_slack

def build_project_help_overview():
    """Builds the /project-status help listing: every space the token can
    see, each with its epics underneath, ordered by how recently active
    that space is -- based on its most-recently-updated epic's timestamp.
    Spaces with zero epics have no activity signal to sort by, so they
    sort to the bottom rather than being placed arbitrarily."""
    projects = fetch_all_projects()
    overview = []

    for p in projects:
        epics = fetch_project_epics_with_activity(p["key"])
        latest_updated = epics[0]["fields"]["updated"] if epics else None
        overview.append(
            {
                "key": p["key"],
                "name": p["name"],
                "epics": epics,
                "latest_updated": latest_updated,
            }
        )

    overview.sort(key=lambda o: o["latest_updated"] or "", reverse=True)
    return overview

def format_project_help_blocks(overview):
    """Formats the space -> epics listing as Slack Block Kit, chunked
    under Slack's per-block character limit (2900 chars, leaving headroom
    under Slack's 3000 hard limit)."""
    lines = ["*Spaces and their epics* (ordered by most recent activity)", ""]

    for o in overview:
        lines.append(f"*{o['key']} — {o['name']}*")
        if o["epics"]:
            for epic in o["epics"]:
                lines.append(f"  \u2022 {epic['fields']['summary']} ({epic['key']})")
        else:
            lines.append("  _no epics found_")
        lines.append("")

    text = "\n".join(lines)
    chunks = []
    current = ""
    for line in text.split("\n"):
        if len(current) + len(line) + 1 > 2900:
            chunks.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        chunks.append(current)

    return [{"type": "section", "text": {"type": "mrkdwn", "text": chunk}} for chunk in chunks]

def handle_project_help_request(response_url, channel_id):
    """Runs in the background: lists every space and its epics, ordered by
    which space has the most recently active epic."""
    try:
        overview = build_project_help_overview()
        if not overview:
            requests.post(response_url, json={"response_type": "ephemeral", "text": "No spaces found."}, timeout=10)
            return

        blocks = format_project_help_blocks(overview)
        requests.post(response_url, json={"response_type": "ephemeral", "blocks": blocks}, timeout=15)
    except Exception as e:
        requests.post(
            response_url,
            json={"response_type": "ephemeral", "text": f"Something went wrong listing spaces: {e}"},
            timeout=10,
        )

def compute_epic_risk_projection(due_date_str, counts, trend, lookback_days=14, min_confident_completions=3):
    """Computes current pace (items completed per week, from the daily
    trend), projects it forward, and compares the projected finish against
    the epic's due date. Returns a dict describing one of these states:
      - "complete"      -- nothing left to project
      - "stalled"       -- zero completions in the lookback window, so no
                           pace exists to extrapolate from
      - "no_due_date"   -- pace/projection computed, but there's no due
                           date to compare it against
      - "on_track"      -- projected finish lands at or before the due date
      - "at_risk"       -- projected finish lands after the due date
    Callers should check `status` before assuming pace/gap fields exist.

    Also sets `confidence` ("normal" or "low") on any state where a pace
    was actually computed. "low" means the pace was calculated from fewer
    than `min_confident_completions` completions in the lookback window --
    e.g. 1 completion in 14 days can swing the projected finish date by
    weeks on the next run, so callers should flag that instability to the
    reader rather than presenting the projection as equally solid either way.
    """
    from datetime import date

    total = counts["done"] + counts["in_progress"] + counts["in_qa"] + counts["to_do"]
    if total == 0:
        return {"status": "no_scope"}

    remaining = max(total - counts["done"], 0)
    if remaining == 0:
        return {"status": "complete", "total": total}

    completed_in_window = sum(trend["velocity"])
    weeks_in_window = lookback_days / 7
    pace_per_week = completed_in_window / weeks_in_window if weeks_in_window else 0
    confidence = "normal" if completed_in_window >= min_confident_completions else "low"

    if pace_per_week <= 0:
        return {
            "status": "stalled",
            "pace_per_week": 0,
            "remaining": remaining,
            "total": total,
            "lookback_days": lookback_days,
        }

    weeks_to_finish = remaining / pace_per_week
    today = date.today()

    due_date = date.fromisoformat(due_date_str) if due_date_str else None
    if due_date is None:
        return {
            "status": "no_due_date",
            "pace_per_week": round(pace_per_week, 1),
            "weeks_to_finish": round(weeks_to_finish, 1),
            "remaining": remaining,
            "total": total,
            "confidence": confidence,
            "completed_in_window": completed_in_window,
        }

    weeks_to_due = (due_date - today).days / 7
    gap_weeks = round(weeks_to_finish - weeks_to_due, 1)

    return {
        "status": "at_risk" if gap_weeks > 0.5 else "on_track",
        "pace_per_week": round(pace_per_week, 1),
        "weeks_to_finish": round(weeks_to_finish, 1),
        "weeks_to_due": round(weeks_to_due, 1),
        "gap_weeks": gap_weeks,
        "remaining": remaining,
        "total": total,
        "due_date": due_date_str,
        "confidence": confidence,
        "completed_in_window": completed_in_window,
    }

def _snapshot_path(epic_key):
    os.makedirs(EPIC_SNAPSHOT_DIR, exist_ok=True)
    safe_key = epic_key.replace("/", "_")
    return os.path.join(EPIC_SNAPSHOT_DIR, f"{safe_key}.json")

def load_epic_snapshot(epic_key):
    """Loads the last saved snapshot for this epic, if one exists, so the
    report can show what changed since the last time someone ran it.

    Caveat: this stores one JSON file per epic on local disk. That's fine
    for a single, long-running instance, but it will NOT survive a
    redeploy on most container/PaaS platforms unless EPIC_SNAPSHOT_DIR
    points at a persistent volume -- and it won't work at all if you ever
    run more than one instance behind a load balancer, since each instance
    would have its own disk. Swap this for a real key-value store (Redis,
    a database table, S3) if either of those apply to your deployment.
    """
    import json

    path = _snapshot_path(epic_key)
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f)

def save_epic_snapshot(epic_key, meta, counts, risk):
    """Saves the current run's numbers so the *next* run can diff against
    them. Called at the end of a successful report generation."""
    import json
    from datetime import datetime

    total = counts["done"] + counts["in_progress"] + counts["in_qa"] + counts["to_do"]
    pct_done = round(100 * counts["done"] / total) if total else 0

    snapshot = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "total": total,
        "done": counts["done"],
        "pct_done": pct_done,
        "pace_per_week": risk.get("pace_per_week"),
        "due_date": meta.get("due_date"),
    }
    with open(_snapshot_path(epic_key), "w") as f:
        json.dump(snapshot, f)

def compute_epic_delta(previous, meta, counts):
    """Compares the current run against the last saved snapshot. Returns
    None if there's no previous snapshot (first time this epic has been
    reported on) -- callers should treat that as "nothing to compare
    against yet," not as an error."""
    if previous is None:
        return None

    total = counts["done"] + counts["in_progress"] + counts["in_qa"] + counts["to_do"]
    pct_done = round(100 * counts["done"] / total) if total else 0

    closed_since = max(counts["done"] - previous.get("done", counts["done"]), 0)
    ticket_delta = total - previous.get("total", total)
    due_changed = meta.get("due_date") != previous.get("due_date")

    pct_prev = previous.get("pct_done", pct_done)
    if pct_done > pct_prev:
        pct_trend = "up"
    elif pct_done < pct_prev:
        pct_trend = "down"
    else:
        pct_trend = "same"

    return {
        "closed_since": closed_since,
        "ticket_delta": ticket_delta,
        "due_date_changed": due_changed,
        "previous_due_date": previous.get("due_date"),
        "previous_timestamp": previous.get("timestamp", "").split("T")[0] or None,
        "pct_trend": pct_trend,
    }

def parse_epic_summary_command(text):
    """Matches '<epic name> summary' -- e.g. '12 week reset summary' or
    'Evolve Program summary' -- with no quotes required. Also still accepts
    the old quoted form ('epic name' summary / "epic name" summary) for
    backward compatibility, stripping the quotes either way.

    Distinguishing rule vs. the existing project-level `summary <space
    name>` command: that one has "summary" as its FIRST word, this one has
    "summary" as its LAST word. A bare "summary" with nothing before it
    doesn't match here, so it falls through to the project-level handler
    untouched. Returns the epic name text, or None if the pattern doesn't
    match at all.
    """
    import re

    stripped = text.strip()
    m = re.match(r"^(.+?)\s+summary$", stripped, re.IGNORECASE)
    if not m:
        return None

    candidate = m.group(1).strip()
    if not candidate or re.match(r"^summary\b", candidate, re.IGNORECASE):
        return None  # leave "summary <space name>" alone

    quoted = re.match(r"""^['"](.+)['"]$""", candidate)
    return quoted.group(1).strip() if quoted else candidate

def handle_epic_html_summary_request(response_url, channel_id, epic_text):
    """Runs in the background: resolves free text to one epic (fuzzy-matched
    via Jira's own '~' operator, so "12 week" still resolves to
    "12 Week Reset" -- with clear ephemeral errors if it matches zero or
    several epics), builds the three-section HTML/Chart.js report, and
    uploads it as a downloadable file for opening in a desktop browser."""
    try:
        if not epic_text:
            requests.post(
                response_url,
                json={
                    "response_type": "ephemeral",
                    "text": "Missing epic name -- usage: /project-status 'epic name' summary",
                },
                timeout=10,
            )
            return

        epic_match, error = resolve_single_epic(epic_text)
        if error:
            requests.post(response_url, json={"response_type": "ephemeral", "text": error}, timeout=10)
            return

        epic_key = epic_match["key"]
        meta = fetch_epic_metadata(epic_key)
        counts = fetch_epic_child_status_counts(epic_key)
        attention_items = fetch_epic_attention_items(epic_key)
        trend = fetch_epic_daily_trend(epic_key, days=14)
        risk = compute_epic_risk_projection(meta.get("due_date"), counts, trend)

        previous_snapshot = load_epic_snapshot(epic_key)
        delta = compute_epic_delta(previous_snapshot, meta, counts)

        html = generate_epic_html_report(meta, counts, attention_items, trend, risk, delta)
        upload_file_to_slack(
            channel_id,
            html.encode("utf-8"),
            filename=f"{epic_key}_summary.html",
            title=f"{meta['summary']} -- desktop summary",
        )

        save_epic_snapshot(epic_key, meta, counts, risk)

        requests.post(
            response_url,
            json={
                "response_type": "in_channel",
                "text": f"Desktop HTML summary for *{epic_key}: {meta['summary']}* is attached above -- download and open it in a browser.",
            },
            timeout=10,
        )
    except Exception as e:
        requests.post(
            response_url,
            json={"response_type": "ephemeral", "text": f"Something went wrong building the epic HTML summary: {e}"},
            timeout=10,
        )

