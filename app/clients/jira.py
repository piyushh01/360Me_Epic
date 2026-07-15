import requests
from app.config import JIRA_BASE_URL, JIRA_AUTH, ISSUE_FIELDS

def strip_common_label(names):
    """Given a batch of space names, strips whatever leading word(s) all of
    them share, leaving just the distinguishing part (e.g. "360Me Product",
    "360Me Marketing" -> "Product", "Marketing"). If there's only one name
    to compare, or a name has nothing left after stripping, the full name
    is kept so nothing ever displays blank."""
    if len(names) < 2:
        return {n: n for n in names}

    word_lists = [n.split() for n in names]
    min_len = min(len(words) for words in word_lists)
    common_len = 0
    for i in range(min_len):
        if len({words[i].lower() for words in word_lists}) == 1:
            common_len += 1
        else:
            break

    result = {}
    for name, words in zip(names, word_lists):
        remainder = " ".join(words[common_len:]).strip()
        result[name] = remainder if remainder else name
    return result

def jira_approx_count(jql):
    """Gets a JQL match count without fetching issue data -- faster than
    paginating through /search/jql when we only need the total."""
    resp = requests.post(
        f"{JIRA_BASE_URL}/rest/api/3/search/approximate-count",
        auth=JIRA_AUTH,
        json={"jql": jql},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json().get("count", 0)

def fetch_all_projects():
    """Lists all projects (key + name) the API token can see."""
    resp = requests.get(
        f"{JIRA_BASE_URL}/rest/api/3/project/search",
        auth=JIRA_AUTH,
        params={"maxResults": 100},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json().get("values", [])

def fetch_project_epics_with_activity(project_key):
    """Pulls every epic in a project with its own summary and last-updated
    timestamp, ordered most-recently-updated first. Used by /project-status
    help both to list a space's epics and to judge how active that space
    currently is (its most-recently-updated epic's timestamp)."""
    jql = f'project = "{project_key}" AND issuetype = Epic ORDER BY updated DESC'
    epics = []
    next_page_token = None

    while True:
        params = {
            "jql": jql,
            "maxResults": 100,
            "fields": "summary,updated",
        }
        if next_page_token:
            params["nextPageToken"] = next_page_token

        resp = requests.get(
            f"{JIRA_BASE_URL}/rest/api/3/search/jql",
            auth=JIRA_AUTH,
            params=params,
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        epics.extend(data.get("issues", []))

        next_page_token = data.get("nextPageToken")
        if not next_page_token:
            break

    return epics

def fetch_matching_epics(text, max_results=10):
    """Free-text search across all epics using Jira's own text-match ('~')
    operator, so partial/incomplete names (e.g. "12 week" instead of the
    full "12 Week Reset") still resolve without us building fuzzy matching."""
    escaped = text.replace('"', '\\"')
    jql = f'issuetype = Epic AND summary ~ "{escaped}"'
    resp = requests.get(
        f"{JIRA_BASE_URL}/rest/api/3/search/jql",
        auth=JIRA_AUTH,
        params={"jql": jql, "maxResults": max_results, "fields": "summary,project"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json().get("issues", [])

def resolve_single_epic(text):
    """Resolves free text to exactly one epic, reusing fetch_matching_epics'
    fuzzy text match so a partial name like "12 week" still resolves to
    "12 Week Reset" without any custom fuzzy-matching logic of our own.

    Returns (epic_or_None, error_message_or_None) -- exactly one of the two
    is set, so callers can check `error` and post it straight to Slack:
      - zero matches -> clear "not found" message, suggests trying a
        shorter or more exact fragment
      - 2+ matches -> lists the candidates (key + summary) so the person
        can pick the exact one or re-run with the exact key
      - exactly 1 match -> returns it, no error
    """
    matches = fetch_matching_epics(text, max_results=10)

    if not matches:
        return None, (
            f'No epic found matching "{text}". '
            f"Try a shorter fragment of the name (e.g. \"12 week\") or the exact epic key."
        )

    if len(matches) > 1:
        options = ", ".join(f"{m['key']}: {m['fields']['summary']}" for m in matches[:10])
        return None, (
            f'"{text}" matched more than one epic: {options}. '
            f"Try being more specific, or use the exact epic key."
        )

    match = matches[0]
    return {"key": match["key"], "summary": match["fields"]["summary"]}, None

def fetch_all_epics(project_key=None):
    """Pulls epics, optionally scoped to a single project key, ordered so
    that each project's epics arrive newest-created first."""
    if project_key:
        jql = f'project = "{project_key}" AND issuetype = Epic ORDER BY created DESC'
    else:
        jql = "issuetype = Epic ORDER BY project ASC, created DESC"

    epics = []
    next_page_token = None
    page_size = 100

    while True:
        params = {
            "jql": jql,
            "maxResults": page_size,
            "fields": "project,summary,created",
        }
        if next_page_token:
            params["nextPageToken"] = next_page_token

        resp = requests.get(
            f"{JIRA_BASE_URL}/rest/api/3/search/jql",
            auth=JIRA_AUTH,
            params=params,
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        epics.extend(data.get("issues", []))

        next_page_token = data.get("nextPageToken")
        if not next_page_token:
            break

    return epics

def group_last_three_epics_per_project(epics):
    """Buckets epics by project, keeping only the 3 most recently created per project."""
    from collections import OrderedDict

    projects = OrderedDict()
    for epic in epics:
        fields = epic["fields"]
        project = fields["project"]
        key = project["key"]

        if key not in projects:
            projects[key] = {"name": project["name"], "epics": []}

        if len(projects[key]["epics"]) < 3:
            projects[key]["epics"].append(
                {
                    "key": epic["key"],
                    "summary": fields["summary"],
                    "created": fields["created"][:10],
                }
            )

    return projects

def fetch_epic_child_status_counts(epic_key):
    """Counts status breakdown, release progress, and KPIs for one epic's children.

    Paginates through every child issue (not capped at 100) so counts are
    accurate even for large epics.

    Release progress = done vs total among child issues that have at least
    one Fix Version assigned (issues with no release set are excluded from
    this ratio, since they're not scoped to a release yet).

    no_due_date counts open (not-yet-done) child issues that have no due
    date set at all -- a scoping-gap signal distinct from "overdue" (which
    requires a due date that's already passed). A high no_due_date count
    means the pace/projection numbers elsewhere in the report are only as
    good as the fraction of work that's actually been dated.

    Uses `parent = epic_key`, which links epics to issues in team-managed
    Jira projects. If your projects are company-managed, this may return
    zero issues even when the epic has children -- in that case swap the
    jql line below for: f'"Epic Link" = "{epic_key}"'
    """
    from datetime import date, timedelta

    jql = f'parent = "{epic_key}"'
    issues = []
    next_page_token = None
    page_size = 100

    while True:
        params = {
            "jql": jql,
            "maxResults": page_size,
            "fields": "status,duedate,fixVersions,issuetype,created,resolutiondate",
        }
        if next_page_token:
            params["nextPageToken"] = next_page_token

        resp = requests.get(
            f"{JIRA_BASE_URL}/rest/api/3/search/jql",
            auth=JIRA_AUTH,
            params=params,
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        issues.extend(data.get("issues", []))

        next_page_token = data.get("nextPageToken")
        if not next_page_token:
            break

    today = date.today().isoformat()
    week_ago = (date.today() - timedelta(days=7)).isoformat()
    counts = {
        "to_do": 0,
        "in_progress": 0,
        "done": 0,
        "overdue": 0,
        "no_due_date": 0,
        "release_done": 0,
        "release_total": 0,
        "ready_for_test": 0,
        "in_qa": 0,
        "bugs": 0,
        "completed_this_week": 0,
        "created_this_week": 0,
    }

    for issue in issues:
        fields = issue["fields"]
        status_category = fields["status"]["statusCategory"]["name"]
        status_name = fields["status"]["name"].strip().lower()
        issue_type = fields["issuetype"]["name"].strip().lower()

        if status_category == "To Do":
            counts["to_do"] += 1
        elif status_category == "Done":
            counts["done"] += 1
        elif status_category == "In Progress":
            if status_name == "in qa":
                counts["in_qa"] += 1
            else:
                counts["in_progress"] += 1

        due = fields.get("duedate")
        if due and due < today and status_category != "Done":
            counts["overdue"] += 1
        if not due and status_category != "Done":
            counts["no_due_date"] += 1

        if fields.get("fixVersions"):
            counts["release_total"] += 1
            if status_category == "Done":
                counts["release_done"] += 1

        if status_name == "ready for test":
            counts["ready_for_test"] += 1

        if issue_type == "bug":
            counts["bugs"] += 1

        resolution_date = fields.get("resolutiondate")
        if resolution_date and resolution_date[:10] >= week_ago:
            counts["completed_this_week"] += 1

        created_date = fields.get("created")
        if created_date and created_date[:10] >= week_ago:
            counts["created_this_week"] += 1

    return counts

def fetch_epic_weekly_transition_counts(epic_key):
    """Counts issues that transitioned INTO Ready For Test, In QA, or Done
    in the last 7 days, using Jira's 'status changed to X during (...)' JQL.
    This is more accurate than inferring movement from the resolution date,
    since it captures the actual transition rather than just current state.
    """

    def count_for_status(status_name):
        jql = f'parent = "{epic_key}" AND status changed to "{status_name}" during (-7d, now())'
        resp = requests.post(
            f"{JIRA_BASE_URL}/rest/api/3/search/approximate-count",
            auth=JIRA_AUTH,
            json={"jql": jql},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json().get("count", 0)

    return {
        "moved_to_ready_for_test": count_for_status("Ready For Test"),
        "moved_to_in_qa": count_for_status("In QA"),
        "moved_to_done": count_for_status("Done"),
    }

def fetch_epic_metadata(epic_key):
    """Gets the epic's own summary, created date, due date, and parent
    project info. due_date is None when the epic has no due date set --
    callers that need a deadline (the risk projection) must handle that."""
    resp = requests.get(
        f"{JIRA_BASE_URL}/rest/api/3/issue/{epic_key}",
        auth=JIRA_AUTH,
        params={"fields": "project,summary,created,duedate"},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    fields = data["fields"]
    return {
        "key": data["key"],
        "summary": fields["summary"],
        "created": fields["created"][:10],
        "due_date": fields.get("duedate"),
        "project_key": fields["project"]["key"],
        "project_name": fields["project"]["name"],
    }

def fetch_epic_daily_trend(epic_key, days=14):
    """Builds a day-by-day trend for the last N days: how many issues moved
    to Done each day (velocity), and how many remained open as of each day
    (burndown). Uses Jira's transition history via JQL rather than our own
    snapshots, so no separate storage is needed -- at the cost of roughly
    2 API calls per day plus 2 baseline calls.
    """
    from datetime import date, timedelta

    day_list = [date.today() - timedelta(days=offset) for offset in range(days - 1, -1, -1)]
    window_start = day_list[0]

    baseline_created = jira_approx_count(
        f'parent = "{epic_key}" AND created < "{window_start.isoformat()}"'
    )
    baseline_done = jira_approx_count(
        f'parent = "{epic_key}" AND status = Done AND resolutiondate < "{window_start.isoformat()}"'
    )

    velocity = []
    created_counts = []
    for d in day_list:
        next_d = d + timedelta(days=1)
        created_counts.append(
            jira_approx_count(
                f'parent = "{epic_key}" AND created >= "{d.isoformat()}" AND created < "{next_d.isoformat()}"'
            )
        )
        velocity.append(
            jira_approx_count(
                f'parent = "{epic_key}" AND status changed to Done during ("{d.isoformat()}", "{next_d.isoformat()}")'
            )
        )

    cumulative_created = baseline_created
    cumulative_done = baseline_done
    burndown = []
    for created_count, done_count in zip(created_counts, velocity):
        cumulative_created += created_count
        cumulative_done += done_count
        burndown.append(max(cumulative_created - cumulative_done, 0))

    return {
        "labels": [d.strftime("%b %d") for d in day_list],
        "velocity": velocity,
        "burndown": burndown,
    }

def fetch_epic_attention_items(epic_key, stale_days=14, max_items=8):
    """Pulls individual child issues that need attention: overdue (due date
    passed, not yet done) or long-pending (no field update in `stale_days`+
    while still open). Returns one combined list, worst-first, capped at
    max_items, for the "Attention needed" section.

    Caveat: "long pending" uses Jira's `updated` timestamp as a stand-in for
    "no real progress" -- `updated` also bumps on comments or field edits,
    not just status transitions, so a ticket with a stray comment can look
    more active than it really is. A changelog-based check would be more
    accurate but costs one extra API call per issue; swap this out if that
    precision matters more than the extra request volume.
    """
    from datetime import date

    today = date.today()

    overdue_jql = f'parent = "{epic_key}" AND duedate < now() AND statusCategory != Done ORDER BY duedate ASC'
    stale_jql = f'parent = "{epic_key}" AND updated <= "-{stale_days}d" AND statusCategory != Done ORDER BY updated ASC'

    def run(jql):
        resp = requests.get(
            f"{JIRA_BASE_URL}/rest/api/3/search/jql",
            auth=JIRA_AUTH,
            params={"jql": jql, "maxResults": max_items, "fields": "summary,duedate,updated"},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json().get("issues", [])

    overdue_issues = run(overdue_jql)
    stale_issues = run(stale_jql)
    seen_keys = set()

    items = []
    for issue in overdue_issues:
        due = date.fromisoformat(issue["fields"]["duedate"])
        items.append({"key": issue["key"], "days": (today - due).days, "kind": "overdue"})
        seen_keys.add(issue["key"])

    for issue in stale_issues:
        if issue["key"] in seen_keys:
            continue  # already counted as overdue, don't double list it
        updated = date.fromisoformat(issue["fields"]["updated"][:10])
        items.append({"key": issue["key"], "days": (today - updated).days, "kind": "pending"})

    items.sort(key=lambda r: -r["days"])
    return items[:max_items]

def _paginated_search(jql, fields):
    """Runs a JQL search with nextPageToken pagination (same pattern as the
    epic server) and returns the full list of raw issue dicts."""
    issues = []
    next_page_token = None
    while True:
        params = {"jql": jql, "maxResults": 100, "fields": ",".join(fields)}
        if next_page_token:
            params["nextPageToken"] = next_page_token
        resp = requests.get(
            f"{JIRA_BASE_URL}/rest/api/3/search/jql",
            auth=JIRA_AUTH,
            params=params,
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        issues.extend(data.get("issues", []))
        next_page_token = data.get("nextPageToken")
        if not next_page_token:
            break
    return issues

def resolve_project(name):
    """Resolves a quoted name to a single project/space."""
    resp = requests.get(
        f"{JIRA_BASE_URL}/rest/api/3/project/search",
        auth=JIRA_AUTH,
        params={"query": name, "maxResults": 50},
        timeout=30,
    )
    resp.raise_for_status()
    projects = resp.json().get("values", [])

    for p in projects:
        if p["key"].lower() == name.lower():
            return "project", {"key": p["key"], "name": p["name"]}

    name_matches = [p for p in projects if name.lower() in p["name"].lower()]
    if len(name_matches) == 1:
        p = name_matches[0]
        return "project", {"key": p["key"], "name": p["name"]}
    if len(name_matches) > 1:
        return "ambiguous", [{"key": p["key"], "name": p["name"]} for p in name_matches[:8]]

    return "none", None

def fetch_project_tickets(project_key):
    """All tickets in a project/space."""
    return _paginated_search(f'project = "{project_key}"', ISSUE_FIELDS)

def fetch_project_board(project_key):
    """Finds the Jira Software board backing this project/space. Returns
    None if the project has no board -- e.g. a plain Jira Work Management
    project -- in which case there's nothing to resolve sprints from."""
    resp = requests.get(
        f"{JIRA_BASE_URL}/rest/agile/1.0/board",
        auth=JIRA_AUTH,
        params={"projectKeyOrId": project_key, "maxResults": 1},
        timeout=30,
    )
    resp.raise_for_status()
    values = resp.json().get("values", [])
    return values[0] if values else None

def fetch_board_sprints(board_id, state):
    """Pulls every sprint in the given state ('active', 'closed', or
    'future') for a board, in Jira's own oldest-first order. Returns []
    for a kanban-only board, which the agile API rejects with a 400
    rather than an empty list."""
    sprints = []
    start_at = 0
    while True:
        resp = requests.get(
            f"{JIRA_BASE_URL}/rest/agile/1.0/board/{board_id}/sprint",
            auth=JIRA_AUTH,
            params={"state": state, "startAt": start_at, "maxResults": 50},
            timeout=30,
        )
        if resp.status_code == 400:
            return []
        resp.raise_for_status()
        data = resp.json()
        values = data.get("values", [])
        sprints.extend(values)
        if data.get("isLast", True) or not values:
            break
        start_at += len(values)
    return sprints

def resolve_project_sprints(project_key):
    """Resolves the last (most recently closed), current (active), and
    next (nearest scheduled) sprint for a project's board. Any of the
    three can be None -- a brand-new project has no closed sprint yet, a
    board with no scheduled future sprint has no "next" either, and a
    kanban-only project has no sprints at all. Fails soft (all None)
    instead of raising, so a Jira Agile API hiccup doesn't take down the
    rest of the allocation report."""
    try:
        board = fetch_project_board(project_key)
        if not board:
            return {"current": None, "last": None, "next": None}

        active = fetch_board_sprints(board["id"], "active")
        closed = fetch_board_sprints(board["id"], "closed")
        future = fetch_board_sprints(board["id"], "future")

        return {
            "current": active[0] if active else None,
            "last": closed[-1] if closed else None,
            "next": future[0] if future else None,
        }
    except requests.RequestException:
        return {"current": None, "last": None, "next": None}

def fetch_sprint_tickets(sprint_id):
    """All tickets in a single sprint."""
    return _paginated_search(f"sprint = {sprint_id}", ISSUE_FIELDS)

