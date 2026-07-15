import re
import requests
from app.config import DONE_CATEGORY, QA_PERSON, QA_STATUSES, STATUS_BUCKETS, TEAM
from app.clients.jira import (
    resolve_project, fetch_project_tickets, resolve_project_sprints, fetch_sprint_tickets,
)
from app.presentation.html_views import generate_distribution_html
from app.clients.slack import upload_file_to_slack

def parse_allocation_command(text):
    """Parses `Name summary` with optional quotes around the name.
    Returns the unquoted name, or None if the text doesn't match."""
    m = re.match(r"^(.+?)\s+summary\s*$", text.strip(), re.IGNORECASE)
    if not m:
        return None
    name = m.group(1).strip()
    quoted = re.match(r"""^['"](.+)['"]$""", name)
    return quoted.group(1).strip() if quoted else name

def normalize_issue(raw):
    """Extracts the fields the distribution summary needs from a raw issue."""
    f = raw["fields"]
    a = f.get("assignee")
    assignee = a.get("displayName") if a else None
    return {
        "key": raw["key"],
        "issuetype": (f.get("issuetype") or {}).get("name", ""),
        "status_name": (f.get("status") or {}).get("name", ""),
        "status_category": (f.get("status") or {}).get("statusCategory", {}).get("key", ""),
        "assignee": assignee,
    }

def _bucket_status(status_name):
    """Maps a raw status name to one of the display buckets (or 'Other')."""
    return status_name if status_name in STATUS_BUCKETS else "Other"

def compute_distribution_summary(raw_issues):
    """Builds the team work-distribution summary from raw Jira issues."""
    per_person = {}
    unassigned_open = 0
    present_buckets = set()

    for raw in raw_issues:
        issue = normalize_issue(raw)
        is_done = issue["status_category"] == DONE_CATEGORY
        in_qa = issue["status_name"].strip().lower() in QA_STATUSES

        if is_done:
            owner = issue["assignee"]
        elif in_qa:
            owner = QA_PERSON
        else:
            owner = issue["assignee"]

        if owner is None:
            if not is_done:
                unassigned_open += 1
            continue

        p = per_person.setdefault(
            owner, {"open": 0, "done": 0, "by_status": {}}
        )
        if is_done:
            p["done"] += 1
        else:
            p["open"] += 1
            bucket = _bucket_status(issue["status_name"])
            present_buckets.add(bucket)
            p["by_status"][bucket] = p["by_status"].get(bucket, 0) + 1

    for name in TEAM:
        if name not in per_person:
            per_person[name] = {"open": 0, "done": 0, "by_status": {}}

    open_counts = [d["open"] for d in per_person.values() if d["open"] > 0]
    avg_open = sum(open_counts) / len(open_counts) if open_counts else 0

    over = [n for n, d in per_person.items() if avg_open > 0 and d["open"] > avg_open * 1.3]
    under = [n for n, d in per_person.items()
             if avg_open > 0 and 0 < d["open"] < avg_open * 0.5]
    idle = [n for n, d in per_person.items() if d["open"] == 0 and d["done"] > 0]
    no_work = [n for n, d in per_person.items() if d["open"] == 0 and d["done"] == 0]

    total_open = sum(d["open"] for d in per_person.values())
    total_done = sum(d["done"] for d in per_person.values())

    buckets = [b for b in STATUS_BUCKETS + ["Other"] if b in present_buckets]

    return {
        "per_person": per_person,
        "unassigned_open": unassigned_open,
        "buckets": buckets,
        "avg_open": avg_open,
        "over_allocated": over,
        "under_allocated": under,
        "idle": idle,
        "no_work": no_work,
        "total_open": total_open,
        "total_done": total_done,
        "n_people": len(per_person),
    }

def _format_sprint_dates(start_iso, end_iso):
    """Formats a sprint's start/end timestamps (Jira ISO 8601, e.g.
    '2024-06-01T10:23:45.000Z') as 'Jun 1 – Jun 14'. Returns "" if
    either date is missing (an unscheduled future sprint)."""
    if not start_iso or not end_iso:
        return ""
    from datetime import date

    start = date.fromisoformat(start_iso[:10])
    end = date.fromisoformat(end_iso[:10])
    return f"{start.strftime('%b')} {start.day} – {end.strftime('%b')} {end.day}"

def _sprint_view(key, label, sprint):
    """Builds one sprint's view entry for generate_distribution_html.
    summary is None when this space has no sprint in that slot (e.g. no
    closed sprint yet) -- the report renders that as an empty state
    instead of crashing."""
    if sprint is None:
        return {"key": key, "label": label, "summary": None, "sprint_name": None, "date_range": None}

    issues = fetch_sprint_tickets(sprint["id"])
    summary = compute_distribution_summary(issues)
    return {
        "key": key,
        "label": label,
        "summary": summary,
        "sprint_name": sprint["name"],
        "date_range": _format_sprint_dates(sprint.get("startDate"), sprint.get("endDate")),
    }

def handle_allocation_request(response_url, channel_id, name):
    try:
        if not name:
            requests.post(response_url, json={
                "response_type": "ephemeral",
                "text": "Usage: /project-allocation 'Space name' summary",
            }, timeout=10)
            return

        kind, meta = resolve_project(name)

        if kind == "none":
            requests.post(response_url, json={
                "response_type": "ephemeral",
                "text": f'No space matched "{name}". Check the name or key and try again.',
            }, timeout=10)
            return

        if kind == "ambiguous":
            listing = "\n".join(f"\u2022 {c['name']} ({c['key']})" for c in meta)
            requests.post(response_url, json={
                "response_type": "ephemeral",
                "text": f'"{name}" matched several spaces \u2014 be more specific:\n{listing}',
            }, timeout=10)
            return

        raw_issues = fetch_project_tickets(meta["key"])
        if not raw_issues:
            requests.post(response_url, json={
                "response_type": "ephemeral",
                "text": f'No tickets found in "{meta["name"]}".',
            }, timeout=10)
            return

        overall_summary = compute_distribution_summary(raw_issues)
        sprints = resolve_project_sprints(meta["key"])
        views = [
            {"key": "overall", "label": "Overall (All Open Work)", "summary": overall_summary,
             "sprint_name": None, "date_range": None},
            _sprint_view("last", "Last Sprint", sprints["last"]),
            _sprint_view("current", "Current Sprint", sprints["current"]),
            _sprint_view("next", "Next Sprint", sprints["next"]),
        ]

        html = generate_distribution_html(meta, views)
        upload_file_to_slack(
            channel_id, html.encode("utf-8"),
            filename="team_work_distribution.html",
            title=f"Team work distribution \u2014 {meta['name']}",
        )
    except Exception as e:
        requests.post(response_url, json={
            "response_type": "ephemeral",
            "text": f"Something went wrong building the distribution summary: {e}",
        }, timeout=10)

