from datetime import datetime
import json
from app.config import JIRA_BASE_URL

def _pipeline_phrase(counts):
    """Builds the 'coming soon' pipeline fragment from current-state In QA
    count -- pipeline-only, so it never changes the progress %, just
    surfaces near-done work so an epic with tickets in QA doesn't read as
    stalled. Returns "" when nothing is in QA (an empty pipeline shouldn't
    add noise). Wording is hedged ("pending verification") since a QA
    ticket can still bounce back to In Progress if it fails testing --
    swap to a plain "currently in QA" if your team's QA rarely bounces."""
    in_qa = counts.get("in_qa", 0)
    if in_qa <= 0:
        return ""
    return f"{in_qa} ticket{'s' if in_qa != 1 else ''} currently in QA (pending verification)"

def _timeline_clause(risk, counts):
    """Builds the third clause of the headline -- the timeline verdict --
    matched to compute_epic_risk_projection's status values. Returns "" for
    "complete", since a finished epic doesn't need a timeline sentence.

    Where a pace is actually being projected (on_track / at_risk /
    no_due_date), the current In QA pipeline is surfaced as a "coming soon"
    note so an epic with work sitting in QA never reads as stalled. This is
    pipeline-only: it does NOT change the progress % or the projected date,
    it just adds visibility that near-done work exists. The stalled state
    is the important exception -- if there ARE tickets in QA, the epic isn't
    truly stalled, so its message is softened to say so rather than claiming
    no progress at all."""
    from datetime import date as date_cls

    status = risk["status"]
    pipeline = _pipeline_phrase(counts)

    if status == "no_scope":
        return "No child work items are linked, so there's nothing to project."
    if status == "complete":
        return ""
    if status == "stalled":
        if pipeline:
            return (
                f"No tickets have completed in the last {risk['lookback_days']} days, "
                f"but {pipeline} -- a finish date can't be projected until those clear."
            )
        return (
            f"No progress in the last {risk['lookback_days']} days -- "
            f"there isn't enough completed work yet to project a finish date."
        )
    if status == "no_due_date":
        weeks = risk["weeks_to_finish"]
        base = f"No epic due date is set -- at the current pace, it would finish in about {weeks} week{'s' if weeks != 1 else ''}"
        return f"{base}, with {pipeline}." if pipeline else f"{base}."
    if status == "at_risk":
        gap = abs(risk["gap_weeks"])
        base = f"At the current pace, it's likely to miss the deadline by ~{gap} week{'s' if gap != 1 else ''}"
        return f"{base}, with {pipeline}." if pipeline else f"{base}."

    due_date = date_cls.fromisoformat(risk["due_date"])
    due_human = f"{due_date.strftime('%b')} {due_date.day}"
    base = f"At the current pace, it's on track to hit the deadline ({due_human})"
    return f"{base}, with {pipeline}." if pipeline else f"{base}."

def build_headline(meta, counts, risk, delta):
    """Builds the headline sentence a stakeholder reads first, before any
    chart, following the fixed three-part template:
      "The <epic name> is <progress>% complete. It has <N> overdue
      tickets and <N> tickets with no due dates. <timeline clause>."
    Always states the overdue/no-due-date counts even when both are zero
    -- "0 overdue and 0 with no due dates" confirms the data is clean
    rather than silently omitting the sentence when there's nothing to flag.
    Progress is done-only (pipeline-only design): tickets in QA do NOT
    inflate the %, they're surfaced separately in the timeline clause as
    "coming soon" so the epic doesn't read as stalled. A confidence caveat
    and a trend note are appended when applicable.
    """
    total = counts["done"] + counts["in_progress"] + counts["in_qa"] + counts["to_do"]
    pct_done = round(100 * counts["done"] / total) if total else 0
    overdue = counts.get("overdue", 0)
    no_due_date = counts.get("no_due_date", 0)

    overdue_word = "ticket" if overdue == 1 else "tickets"
    no_due_word = "ticket" if no_due_date == 1 else "tickets"

    parts = [
        f"The <strong>{meta['summary']}</strong> is {pct_done}% complete.",
        f"It has {overdue} overdue {overdue_word} and {no_due_date} {no_due_word} with no due dates.",
    ]

    timeline_clause = _timeline_clause(risk, counts)
    if timeline_clause:
        parts.append(timeline_clause)

    headline = " ".join(parts)

    if risk.get("confidence") == "low":
        n = risk.get("completed_in_window", 0)
        headline += f" (based on only {n} completion{'s' if n != 1 else ''} in the last 2 weeks -- treat this projection with caution)"

    if delta and delta["pct_trend"] != "same":
        trend_word = "improving" if delta["pct_trend"] == "up" else "slipping further"
        headline += f" -- {trend_word} vs. last check"

    return headline

def build_change_summary(delta):
    """Builds the 'what changed since last time' line. Returns None when
    there's no previous snapshot, so the caller can skip the section
    entirely on an epic's first run instead of showing an empty line."""
    if delta is None:
        return None

    parts = []
    if delta["closed_since"] > 0:
        n = delta["closed_since"]
        parts.append(f"{n} ticket{'s' if n != 1 else ''} closed")
    if delta["ticket_delta"] > 0:
        n = delta["ticket_delta"]
        parts.append(f"{n} new ticket{'s' if n != 1 else ''} added")
    elif delta["ticket_delta"] < 0:
        n = abs(delta["ticket_delta"])
        parts.append(f"{n} ticket{'s' if n != 1 else ''} removed from scope")
    if delta["due_date_changed"]:
        prev = delta["previous_due_date"] or "no due date"
        parts.append(f"due date changed from {prev}")
    if not parts:
        parts.append("no material change")

    since = f"since last check ({delta['previous_timestamp']})" if delta["previous_timestamp"] else "since last check"
    return f"{since}: {', '.join(parts)}."

def _risk_banner(risk):
    """Builds the colored one-line callout under the Projected timeline
    heading, matched to compute_epic_risk_projection's status values."""
    if risk["status"] == "no_scope":
        return ("gray", "no child work items linked -- nothing to project")
    if risk["status"] == "complete":
        return ("green", "all work items are complete")
    if risk["status"] == "stalled":
        return ("red", f"no progress in the last {risk['lookback_days']} days -- can't project a finish date at this pace")
    if risk["status"] == "no_due_date":
        return ("amber", f"no due date set -- at {risk['pace_per_week']}/wk, finishing in about {risk['weeks_to_finish']} weeks")
    if risk["status"] == "at_risk":
        return ("red", f"at current pace, likely to miss the deadline by ~{abs(risk['gap_weeks'])} weeks")
    return ("green", "on pace to hit the deadline at the current rate")

def generate_epic_html_report(meta, counts, attention_items, trend, risk, delta=None):
    """Builds a single self-contained HTML file (Chart.js loaded from CDN,
    all data inlined). Structure is headline-first, details-on-demand:
      1. A headline sentence + what-changed-since-last-time line, always
         visible, no scrolling or clicking required
      2. Three <details> sections (Attention needed, Overall status,
         Projected timeline) collapsed by default -- each <summary> line
         carries the key number so the verdict is visible even collapsed
    Charts are NOT created until their section is opened for the first
    time (listens for the <details> `toggle` event) -- Chart.js measures
    canvas size at draw time, and a display:none container has zero size,
    so drawing charts up front while collapsed would render them blank.
    Lazy-rendering also means we never draw a chart nobody looks at.

    delta is the output of compute_epic_delta -- pass None on an epic's
    first run (no previous snapshot to compare against).
    """
    import json
    from datetime import date

    total = counts["done"] + counts["in_progress"] + counts["in_qa"] + counts["to_do"]
    pct_done = round(100 * counts["done"] / total) if total else 0

    attention_labels = [a["key"] for a in attention_items]
    attention_days = [a["days"] for a in attention_items]
    attention_colors = ["#e34948" if a["kind"] == "overdue" else "#eda100" for a in attention_items]
    attention_tooltips = [
        f"{a['days']} days " + ("overdue" if a["kind"] == "overdue" else "with no status change")
        for a in attention_items
    ]
    attention_links = [f"{JIRA_BASE_URL}/browse/{a['key']}" for a in attention_items]

    overdue_count = sum(1 for a in attention_items if a["kind"] == "overdue")
    if attention_items:
        worst = attention_items[0]
        worst_phrase = f"worst: {worst['key']} ({worst['days']}d)"
        attention_summary = f"{len(attention_items)} ticket{'s' if len(attention_items) != 1 else ''} need attention"
        if overdue_count:
            attention_summary += f" ({overdue_count} overdue)"
        attention_summary += f" -- {worst_phrase}"
    else:
        attention_summary = "No overdue or stale tickets"

    no_due_date = counts.get("no_due_date", 0)
    status_summary = f"{pct_done}% complete ({counts['done']}/{total} items)" if total else "No items linked"
    if no_due_date:
        status_summary += f" · {no_due_date} with no due date"

    no_due_date_stat_html = (
        f'<div><p class="stat-label">no due date</p><p class="stat-value">{no_due_date}</p>'
        f'<p class="stat-sub">open items with no due date set</p></div>'
        if no_due_date
        else ""
    )

    risk_color, risk_message = _risk_banner(risk)
    risk_colors_css = {
        "red": ("#fcebeb", "#791f1f"),
        "amber": ("#faeeda", "#633806"),
        "green": ("#eaf3de", "#27500a"),
        "gray": ("#f1efe8", "#444441"),
    }
    risk_bg, risk_fg = risk_colors_css[risk_color]
    risk_summary = risk_message[0].upper() + risk_message[1:]

    headline = build_headline(meta, counts, risk, delta)
    change_summary = build_change_summary(delta)

    show_projection_chart = risk["status"] in ("on_track", "at_risk", "no_due_date")
    projection_json = "null"
    if show_projection_chart:
        labels = list(trend["labels"])
        burndown = trend["burndown"]
        scope_series = [total] * len(labels)
        actual_completed = [total - b for b in burndown]

        weeks_to_finish = risk["weeks_to_finish"]
        future_weeks = max(1, int(weeks_to_finish) + 1)
        future_labels = [f"+{w}w" for w in range(1, future_weeks + 1)]
        pace_per_week = risk["pace_per_week"]
        projected = []
        current = actual_completed[-1] if actual_completed else counts["done"]
        for w in range(1, future_weeks + 1):
            current = min(total, current + pace_per_week)
            projected.append(round(current, 1))

        due_index = None
        if risk["status"] in ("on_track", "at_risk"):
            weeks_to_due = risk["weeks_to_due"]
            due_index = len(labels) + max(0, round(weeks_to_due)) - 1

        projection_json = json.dumps(
            {
                "labels": labels + future_labels,
                "scope": scope_series + [total] * len(future_labels),
                "actual": actual_completed + [None] * len(future_labels),
                "projected": [None] * (len(labels) - 1) + [actual_completed[-1] if actual_completed else counts["done"]] + projected,
                "due_index": due_index,
            }
        )

    attention_body_html = ""
    if attention_items:
        links_list = "\n".join(
            f'<li><a href="{link}" target="_blank" rel="noopener">{item["key"]}</a> &middot; '
            f'{tooltip}</li>'
            for item, link, tooltip in zip(attention_items, attention_links, attention_tooltips)
        )
        attention_body_html = """
        <div class="legend">
          <span><span class="dot" style="background:#e34948"></span>overdue (past due date)</span>
          <span><span class="dot" style="background:#eda100"></span>long pending (no movement)</span>
        </div>
        <div class="chart-wrap" style="height: {height}px;">
          <canvas id="attentionChart" role="img" aria-label="Horizontal bar chart ranking tickets by days overdue or without status movement"></canvas>
        </div>
        <ul class="ticket-links">
          {links_list}
        </ul>
        """.format(height=max(180, len(attention_items) * 42 + 60), links_list=links_list)
    else:
        attention_body_html = '<p class="empty-state">No overdue or stale tickets in this epic right now.</p>'

    projection_body_html = f'<div class="risk-banner" style="background:{risk_bg};color:{risk_fg};">{risk_message}</div>'
    if show_projection_chart:
        projection_body_html += """
        <div class="legend">
          <span><span class="line" style="background:#c3c2b7"></span>total scope</span>
          <span><span class="line" style="background:#2a78d6"></span>actual completed</span>
          <span><span class="line dashed"></span>projected</span>
        </div>
        <div class="chart-wrap" style="height: 340px;">
          <canvas id="riskChart" role="img" aria-label="Line chart projecting epic completion trend against total scope and due date"></canvas>
        </div>
        """
    elif risk["status"] == "stalled":
        projection_body_html += '<p class="empty-state">No completions in the lookback window -- nothing to project forward yet.</p>'
    elif risk["status"] == "complete":
        projection_body_html += '<p class="empty-state">This epic is fully complete.</p>'
    else:
        projection_body_html += '<p class="empty-state">No child work items linked to this epic.</p>'

    stats_html = ""
    if risk["status"] in ("on_track", "at_risk", "no_due_date"):
        stats_html = f"""
        <div class="stats">
          <div class="stat"><p class="stat-label">current pace</p><p class="stat-value">{risk['pace_per_week']}/wk</p><p class="stat-sub">items completed per week, last {len(trend['labels']) if trend else 14} days</p></div>
          <div class="stat"><p class="stat-label">projected finish</p><p class="stat-value">{risk['weeks_to_finish']} wks</p><p class="stat-sub">time to close remaining {risk['remaining']} items at this pace</p></div>
        """
        if risk["status"] != "no_due_date":
            stats_html += f"""
          <div class="stat"><p class="stat-label">time to due date</p><p class="stat-value">{risk['weeks_to_due']} wks</p><p class="stat-sub">from today to the epic due date</p></div>
        """
        stats_html += "</div>"
    projection_body_html = stats_html + projection_body_html

    change_summary_html = f'<p class="change-summary">{change_summary}</p>' if change_summary else ""

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{meta['key']}: {meta['summary']} -- epic report</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Arial, sans-serif; max-width: 820px; margin: 0 auto; padding: 32px 24px 64px; color: #232220; background: #ffffff; }}
  h1 {{ font-size: 20px; font-weight: 600; margin: 0 0 2px; }}
  .subtitle {{ font-size: 13px; color: #767268; margin: 0 0 24px; }}
  .headline {{ font-size: 17px; font-weight: 600; line-height: 1.4; margin: 0 0 6px; }}
  .change-summary {{ font-size: 13px; color: #767268; margin: 0 0 28px; }}
  details {{ border: 1px solid #e5e4df; border-radius: 10px; margin-bottom: 12px; overflow: hidden; }}
  summary {{ list-style: none; cursor: pointer; padding: 14px 18px; font-size: 15px; font-weight: 600; display: flex; justify-content: space-between; align-items: center; background: #fbfaf7; }}
  summary::-webkit-details-marker {{ display: none; }}
  summary::after {{ content: '+'; font-weight: 400; color: #9a968c; font-size: 18px; }}
  details[open] summary::after {{ content: '\\2212'; }}
  summary .summary-detail {{ font-weight: 400; color: #767268; font-size: 13px; margin-left: 10px; }}
  .details-body {{ padding: 18px; border-top: 1px solid #e5e4df; }}
  .section-sub {{ font-size: 13px; color: #767268; margin: 0 0 12px; }}
  .legend {{ display: flex; flex-wrap: wrap; gap: 16px; font-size: 12px; color: #767268; margin-bottom: 10px; }}
  .legend .dot {{ display: inline-block; width: 10px; height: 10px; border-radius: 2px; margin-right: 4px; vertical-align: -1px; }}
  .legend .line {{ display: inline-block; width: 14px; height: 2px; margin-right: 4px; vertical-align: 2px; }}
  .legend .line.dashed {{ background: repeating-linear-gradient(90deg, #2a78d6 0 4px, transparent 4px 7px); }}
  .chart-wrap {{ position: relative; width: 100%; }}
  .empty-state {{ font-size: 13px; color: #767268; padding: 4px 0; }}
  .ticket-links {{ list-style: none; margin: 12px 0 0; padding: 0; font-size: 13px; }}
  .ticket-links li {{ padding: 4px 0; border-top: 1px solid #f1efe8; color: #767268; }}
  .ticket-links li:first-child {{ border-top: none; }}
  .ticket-links a {{ color: #185fa5; text-decoration: none; font-weight: 500; }}
  .ticket-links a:hover {{ text-decoration: underline; }}
  .stats {{ display: flex; gap: 24px; flex-wrap: wrap; margin-bottom: 12px; }}
  .stat-label {{ font-size: 13px; color: #767268; margin: 0; }}
  .stat-value {{ font-size: 22px; font-weight: 600; margin: 2px 0 0; }}
  .stat-sub {{ font-size: 11px; color: #9a968c; margin: 2px 0 0; }}
  .risk-banner {{ font-size: 13px; padding: 8px 12px; border-radius: 6px; display: inline-block; margin-bottom: 16px; }}
  .total-stats {{ display: flex; gap: 24px; margin-bottom: 12px; }}
</style>
</head>
<body>
  <h1>{meta['key']}: {meta['summary']}</h1>
  <p class="subtitle">{meta['project_name']} &middot; generated {date.today().strftime('%B %d, %Y')}</p>

  <p class="headline">{headline}</p>
  {change_summary_html}

  <details id="attentionDetails">
    <summary>Attention needed <span class="summary-detail">{attention_summary}</span></summary>
    <div class="details-body">
      <p class="section-sub">tickets overdue or with no recent movement</p>
      {attention_body_html}
    </div>
  </details>

  <details id="statusDetails">
    <summary>Overall status <span class="summary-detail">{status_summary}</span></summary>
    <div class="details-body">
      <p class="section-sub">breakdown of all items in this epic</p>
      <div class="total-stats">
        <div><p class="stat-label">total items</p><p class="stat-value">{total}</p></div>
        <div><p class="stat-label">complete</p><p class="stat-value">{pct_done}%</p></div>
        {no_due_date_stat_html}
      </div>
      <div class="chart-wrap" style="height: 300px;">
        <canvas id="statusChart" role="img" aria-label="Bar chart showing percentage breakdown across to do, in progress, in QA, and done"></canvas>
      </div>
    </div>
  </details>

  <details id="riskDetails" open>
    <summary>Projected timeline <span class="summary-detail">{risk_summary}</span></summary>
    <div class="details-body">
      <p class="section-sub">completion trend projected against the due date</p>
      {projection_body_html}
    </div>
  </details>

<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/chartjs-plugin-datalabels/2.2.0/chartjs-plugin-datalabels.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/chartjs-plugin-annotation/3.0.1/chartjs-plugin-annotation.min.js"></script>
<script>
const attentionLabels = {json.dumps(attention_labels)};
const attentionDays = {json.dumps(attention_days)};
const attentionColors = {json.dumps(attention_colors)};
const attentionTooltips = {json.dumps(attention_tooltips)};
const attentionLinks = {json.dumps(attention_links)};
const statusCounts = [{counts['to_do']}, {counts['in_progress']}, {counts['in_qa']}, {counts['done']}];
const projectionData = {projection_json};

// Lazy-render: each chart is only created the first time its <details>
// section is opened. Chart.js measures the canvas at draw time, and a
// collapsed (display:none) container reports zero size, so creating
// these up front would draw blank charts. rendered flags stop us
// re-creating (and duplicating) a chart on subsequent opens.
let attentionRendered = false, statusRendered = false, riskRendered = false;

function renderAttentionChart() {{
  if (attentionRendered || !attentionLabels.length) return;
  attentionRendered = true;
  new Chart(document.getElementById('attentionChart'), {{
    type: 'bar',
    data: {{ labels: attentionLabels, datasets: [{{ data: attentionDays, backgroundColor: attentionColors, borderRadius: 4, barPercentage: 0.6 }}] }},
    options: {{
      indexAxis: 'y',
      responsive: true,
      maintainAspectRatio: false,
      onHover: (evt, elements) => {{ evt.native.target.style.cursor = elements.length ? 'pointer' : 'default'; }},
      onClick: (evt, elements) => {{
        if (elements.length) {{ window.open(attentionLinks[elements[0].index], '_blank', 'noopener'); }}
      }},
      plugins: {{
        legend: {{ display: false }},
        tooltip: {{ callbacks: {{ label: (ctx) => attentionTooltips[ctx.dataIndex], footer: () => 'click bar to open in Jira' }} }}
      }},
      scales: {{
        x: {{ beginAtZero: true, grid: {{ color: '#e1e0d9' }}, ticks: {{ callback: (v) => v + 'd' }} }},
        y: {{ grid: {{ display: false }} }}
      }}
    }}
  }});
}}

function renderStatusChart() {{
  if (statusRendered) return;
  statusRendered = true;
  const statusTotal = statusCounts.reduce((a, b) => a + b, 0);
  const statusPct = statusTotal ? statusCounts.map((c) => Math.round((c / statusTotal) * 100)) : [0, 0, 0, 0];
  new Chart(document.getElementById('statusChart'), {{
    type: 'bar',
    data: {{
      labels: ['To do', 'In progress', 'In QA', 'Done'],
      datasets: [{{ data: statusPct, backgroundColor: ['#c3c2b7', '#eda100', '#4a3aa7', '#1baf7a'], borderRadius: 4, barPercentage: 0.6 }}]
    }},
    plugins: [ChartDataLabels],
    options: {{
      responsive: true,
      maintainAspectRatio: false,
      plugins: {{
        legend: {{ display: false }},
        tooltip: {{ callbacks: {{ label: (ctx) => statusCounts[ctx.dataIndex] + ' items (' + ctx.parsed.y + '%)' }} }},
        datalabels: {{ anchor: 'end', align: 'top', color: '#52514e', font: {{ weight: '500', size: 13 }}, formatter: (v) => v + '%' }}
      }},
      scales: {{
        y: {{ beginAtZero: true, max: 100, grid: {{ color: '#e1e0d9' }}, ticks: {{ callback: (v) => v + '%' }} }},
        x: {{ grid: {{ display: false }} }}
      }}
    }}
  }});
}}

function renderRiskChart() {{
  if (riskRendered || !projectionData) return;
  riskRendered = true;
  const annotations = {{}};
  if (projectionData.due_index !== null && projectionData.due_index !== undefined) {{
    annotations.dueLine = {{
      type: 'line',
      xMin: projectionData.due_index,
      xMax: projectionData.due_index,
      borderColor: '#e34948',
      borderWidth: 2,
      borderDash: [4, 4],
      label: {{ display: true, content: 'due date', position: 'start', backgroundColor: '#e34948', color: '#fff', font: {{ size: 11 }} }}
    }};
  }}
  new Chart(document.getElementById('riskChart'), {{
    type: 'line',
    data: {{
      labels: projectionData.labels,
      datasets: [
        {{ label: 'Total scope', data: projectionData.scope, borderColor: '#c3c2b7', borderWidth: 2, pointRadius: 0, tension: 0 }},
        {{ label: 'Actual completed', data: projectionData.actual, borderColor: '#2a78d6', backgroundColor: '#2a78d6', borderWidth: 2, pointRadius: 3, tension: 0.2, spanGaps: false }},
        {{ label: 'Projected', data: projectionData.projected, borderColor: '#2a78d6', borderWidth: 2, borderDash: [6, 4], pointRadius: 0, tension: 0.2, spanGaps: true }}
      ]
    }},
    options: {{
      responsive: true,
      maintainAspectRatio: false,
      plugins: {{ legend: {{ display: false }}, annotation: {{ annotations }} }},
      scales: {{
        y: {{ beginAtZero: true, grid: {{ color: '#e1e0d9' }}, title: {{ display: true, text: 'items', color: '#898781', font: {{ size: 12 }} }} }},
        x: {{ grid: {{ display: false }} }}
      }}
    }}
  }});
}}

document.getElementById('attentionDetails').addEventListener('toggle', function() {{ if (this.open) renderAttentionChart(); }});
document.getElementById('statusDetails').addEventListener('toggle', function() {{ if (this.open) renderStatusChart(); }});
document.getElementById('riskDetails').addEventListener('toggle', function() {{ if (this.open) renderRiskChart(); }});

// "Projected timeline" ships open by default (it's the section most
// worth a stakeholder's first click), so render its chart immediately
// rather than waiting for a toggle event that won't fire until it's
// closed and reopened.
renderRiskChart();
</script>
</body>
</html>"""
    return html

def _initials(name):
    parts = [p for p in name.split() if p]
    return ((parts[0][0] + (parts[-1][0] if len(parts) > 1 else "")).upper()) or "?"

def _build_view_payload(summary):
    """Builds the JSON-serializable rendering payload for one dashboard
    view (the overall space, or a single sprint) from a
    compute_distribution_summary() result. Kept separate from the HTML
    template so the client-side view switcher in generate_distribution_html
    can hold one of these per view and swap between them with no server
    round-trip."""
    per_person = summary["per_person"]
    names = sorted(per_person.keys(), key=lambda n: per_person[n]["open"], reverse=True)
    buckets = summary["buckets"]

    bucket_colours = {
        "To Do": "#9aa7b0", "In Progress": "#4a7c9e", "Ready for Test": "#c9a227",
        "In QA": "#c47f2a", "Blocked": "#b0503f", "Other": "#7d7a72",
    }

    open_counts = [per_person[n]["open"] for n in names]
    stacked = []
    for b in buckets:
        stacked.append({
            "label": b,
            "data": [per_person[n]["by_status"].get(b, 0) for n in names],
            "backgroundColor": bucket_colours.get(b, "#7d7a72"),
        })

    bar_colours = ["#b0503f" if n in summary["over_allocated"] else "#4a7c9e" for n in names]

    ramp = ["#4a7c9e", "#5b8fa8", "#c47f2a", "#c9a227", "#8a9b6e", "#b0503f",
            "#7d7a72", "#9aa7b0", "#6b8e9e", "#a8763a"]
    share_colours = [ramp[i % len(ramp)] for i in range(len(names))]

    detail = {}
    for n in names:
        d = per_person[n]
        share = (d["open"] / summary["total_open"] * 100) if summary["total_open"] else 0
        status_role = (
            "over" if n in summary["over_allocated"]
            else "light" if n in summary["under_allocated"]
            else "idle" if n in summary["idle"]
            else "none" if n in summary.get("no_work", [])
            else "normal"
        )
        detail[n] = {
            "open": d["open"], "done": d["done"], "share": round(share),
            "by_status": d["by_status"], "role": status_role,
        }

    avatar_ramp = ["#4a7c9e", "#c47f2a", "#8a9b6e", "#b0503f", "#5b8fa8",
                   "#c9a227", "#7d7a72", "#6b8e9e", "#a8763a", "#9aa7b0"]
    avatars = []
    for i, n in enumerate(names):
        d = per_person[n]
        ring = ""
        if n in summary["over_allocated"]:
            ring = "over"
        elif n in summary["idle"] or n in summary.get("no_work", []):
            ring = "idle"
        avatars.append({
            "name": n,
            "initials": _initials(n),
            "colour": avatar_ramp[i % len(avatar_ramp)],
            "count": d["open"],
            "ring": ring,
        })

    busiest = names[0] if names else "—"
    busiest_n = per_person[busiest]["open"] if names else 0

    return {
        "names": names,
        "open_counts": open_counts,
        "bar_colours": bar_colours,
        "buckets": buckets,
        "stacked": stacked,
        "share_counts": open_counts,
        "share_colours": share_colours,
        "detail": detail,
        "avatars": avatars,
        "avg": summary["avg_open"],
        "n_people": summary["n_people"],
        "total_open": summary["total_open"],
        "unassigned": summary["unassigned_open"],
        "busiest": busiest,
        "busiest_n": busiest_n,
        "load_chart_height": max(180, len(names) * 34 + 40),
    }

def generate_distribution_html(project_meta, views):
    """Builds a self-contained team work-distribution DASHBOARD with a
    view switcher: Overall (all open work) plus Last/Current/Next Sprint.
    Every view is fetched up front and baked into the page as JSON, so
    switching the "Sprint" dropdown is instant and entirely client-side
    (destroy + recreate the 3 charts, rebuild the avatar strip) -- no
    server round-trip, and no Slack app "Interactivity" configuration
    needed.

    `views` is an ordered list of dicts, each with:
      key          -- e.g. "overall", "last", "current", "next"
      label        -- dropdown option text, e.g. "Last Sprint"
      summary      -- a compute_distribution_summary() result, or None if
                      this slot has no sprint (e.g. no closed sprint yet,
                      or the space's board has no sprints at all)
      sprint_name  -- the sprint's own Jira name, or None for "overall"
      date_range   -- "Jun 1 – Jun 14", or "" / None if unscheduled
    """
    generated = datetime.now().strftime("%d %b %Y, %H:%M")
    project_title = project_meta.get("name", project_meta.get("key", ""))

    views_js = json.dumps({
        v["key"]: {
            "label": v["label"],
            "sprint_name": v.get("sprint_name"),
            "date_range": v.get("date_range"),
            "payload": _build_view_payload(v["summary"]) if v["summary"] is not None else None,
        }
        for v in views
    })

    options_html = "\n        ".join(
        f'<option value="{v["key"]}">{v["label"]}</option>' for v in views
    )

    html = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Team Work Distribution — {project_title}</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<style>
  :root {{ --ink:#2b2a27; --muted:#898781; --line:#e4e2da; --card:#ffffff; --bg:#f4f3ee; }}
  * {{ box-sizing: border-box; }}
  body {{ font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
    background:var(--bg); color:var(--ink); margin:0; padding:28px; }}
  .wrap {{ max-width:1000px; margin:0 auto; }}
  h1 {{ font-size:22px; margin:0 0 2px; }}
  .topbar {{ display:flex; justify-content:space-between; align-items:flex-start; gap:16px;
    margin-bottom:22px; flex-wrap:wrap; }}
  .sub {{ color:var(--muted); font-size:13px; margin:0; }}
  .viewbar {{ display:flex; align-items:center; gap:8px; }}
  .viewbar label {{ font-size:12px; color:var(--muted); font-weight:600; }}
  .viewbar select {{ font-size:13px; padding:7px 10px; border-radius:7px; border:1px solid var(--line);
    background:var(--card); color:var(--ink); cursor:pointer; }}
  .empty-state-big {{ background:var(--card); border-radius:8px; padding:28px; text-align:center;
    color:var(--muted); font-size:14px; box-shadow:0 1px 3px rgba(0,0,0,.05); margin-bottom:18px; }}
  .kpis {{ display:grid; grid-template-columns:repeat(4,1fr); gap:14px; margin-bottom:22px; }}
  .kpi {{ background:var(--card); border-radius:8px; padding:16px; box-shadow:0 1px 3px rgba(0,0,0,.05); }}
  .kpi .n {{ font-size:26px; font-weight:700; line-height:1; }}
  .kpi .l {{ font-size:12px; color:var(--muted); margin-top:6px; }}
  .grid2 {{ display:grid; grid-template-columns:1fr 1fr; gap:18px; margin-bottom:18px; }}
  .card {{ background:var(--card); border-radius:8px; padding:18px; box-shadow:0 1px 3px rgba(0,0,0,.05); }}
  .card h2 {{ font-size:14px; margin:0 0 14px; }}
  .card .cap {{ font-size:12px; color:var(--muted); font-weight:400; }}
  canvas {{ max-width:100%; }}
  .chart-wrap {{ position: relative; width: 100%; }}
  /* Avatar strip */
  .avatars {{ display:flex; flex-wrap:wrap; gap:14px; padding:4px 0 6px; }}
  .av {{ position:relative; border:none; background:none; padding:0; cursor:pointer;
    width:46px; height:46px; border-radius:50%; transition:transform .1s; }}
  .av:hover {{ transform:translateY(-2px); }}
  .av img, .av .ini {{ width:46px; height:46px; border-radius:50%; display:block; }}
  .av .ini {{ color:#fff; font-size:15px; font-weight:600; line-height:46px;
    text-align:center; }}
  .av.sel {{ outline:3px solid #2b2a27; outline-offset:2px; }}
  .av.over::after {{ content:''; position:absolute; inset:-3px; border-radius:50%;
    border:2px solid #b0503f; }}
  .av.idle {{ opacity:.55; }}
  .av .cnt {{ position:absolute; bottom:-3px; right:-3px; background:#2b2a27; color:#fff;
    font-size:11px; font-weight:600; min-width:18px; height:18px; line-height:18px;
    border-radius:9px; padding:0 4px; text-align:center; border:2px solid #fff; }}
  /* Detail panel */
  .detail {{ margin-top:16px; border-top:1px solid var(--line); padding-top:16px; }}
  .detail.empty {{ color:var(--muted); font-size:13px; }}
  .dhead {{ display:flex; align-items:baseline; gap:12px; margin-bottom:12px; }}
  .dname {{ font-size:16px; font-weight:700; }}
  .drole {{ font-size:12px; color:var(--muted); }}
  .dstats {{ display:flex; gap:24px; margin-bottom:16px; }}
  .ds .dn {{ font-size:22px; font-weight:700; line-height:1; }}
  .ds .dl {{ font-size:11px; color:var(--muted); margin-top:4px; }}
  .dsub {{ font-size:12px; color:var(--muted); font-weight:600; margin-bottom:8px; }}
  .srow {{ display:flex; align-items:center; gap:8px; font-size:13px; padding:4px 0;
    max-width:320px; }}
  .srow .dot {{ width:10px; height:10px; border-radius:2px; }}
  .srow .sname {{ flex:1; }}
  .srow .sval {{ font-weight:600; }}
  .srow.muted {{ color:var(--muted); }}
  @media (max-width:720px) {{ .kpis {{ grid-template-columns:repeat(2,1fr); }} .grid2 {{ grid-template-columns:1fr; }} }}
</style></head>
<body><div class="wrap">
  <div class="topbar">
    <div>
      <h1>Team Work Distribution</h1>
      <div class="sub" id="subtitle"></div>
    </div>
    <div class="viewbar">
      <label for="viewSelect">Sprint</label>
      <select id="viewSelect" onchange="renderView(this.value)">
        {options_html}
      </select>
    </div>
  </div>

  <div id="emptyState" class="empty-state-big" style="display:none;"></div>

  <div id="dashboardBody">
    <div class="kpis">
      <div class="kpi"><div class="n" id="kpiTotal">0</div><div class="l">Open tickets</div></div>
      <div class="kpi"><div class="n" id="kpiAvg">0</div><div class="l">Avg per person</div></div>
      <div class="kpi"><div class="n" id="kpiBusiestN">0</div><div class="l" id="kpiBusiestLabel">Busiest: —</div></div>
      <div class="kpi"><div class="n" id="kpiUnassigned">0</div><div class="l">Unassigned open</div></div>
    </div>

    <div class="card" style="margin-bottom:18px;">
      <h2>Open tickets per person <span class="cap" id="loadChartCap"></span></h2>
      <div class="chart-wrap" id="loadChartWrap" style="height: 180px;"><canvas id="loadChart"></canvas></div>
    </div>

    <div class="grid2">
      <div class="card">
        <h2>Status breakdown <span class="cap">&mdash; where each person's open work sits</span></h2>
        <canvas id="statusChart" height="150"></canvas>
      </div>
      <div class="card">
        <h2>Share of open work <span class="cap">&mdash; % of all open tickets</span></h2>
        <canvas id="shareChart" height="150"></canvas>
      </div>
    </div>

    <div class="card">
      <h2>Team members <span class="cap">&mdash; click anyone to see their breakdown</span></h2>
      <div class="avatars" id="avatarsContainer"></div>
      <div id="personDetail" class="detail empty">Select a team member above to see their open tickets, status breakdown, and share of work.</div>
    </div>
  </div>
</div>
<script>
  const VIEWS = {views_js};
  const PROJECT_TITLE = {json.dumps(project_title)};
  const GENERATED = {json.dumps(generated)};
  const BUCKET_COLOURS = {{
    "To Do":"#9aa7b0","In Progress":"#4a7c9e","Ready for Test":"#c9a227",
    "In QA":"#c47f2a","Blocked":"#b0503f","Other":"#7d7a72"
  }};
  const ROLE_LABEL = {{
    over:"▲ Above team average", light:"▼ Lighter than average",
    idle:"— No open work (has completed tickets)",
    none:"— No tickets in this space", normal:"On par with team average"
  }};

  let currentDetail = {{}};
  let loadChart, statusChart, shareChart;

  function showPerson(btn) {{
    document.querySelectorAll('.av').forEach(a => a.classList.remove('sel'));
    btn.classList.add('sel');
    const name = btn.getAttribute('data-name');
    const d = currentDetail[name];
    const el = document.getElementById('personDetail');
    el.classList.remove('empty');

    let statusRows = '';
    const buckets = Object.keys(d.by_status);
    if (buckets.length) {{
      for (const b of buckets) {{
        const c = BUCKET_COLOURS[b] || '#7d7a72';
        statusRows += `<div class="srow"><span class="dot" style="background:${{c}}"></span>`
          + `<span class="sname">${{b}}</span><span class="sval">${{d.by_status[b]}}</span></div>`;
      }}
    }} else {{
      statusRows = '<div class="srow muted">No open tickets.</div>';
    }}

    el.innerHTML = `
      <div class="dhead">
        <div class="dname">${{name}}</div>
        <div class="drole">${{ROLE_LABEL[d.role] || ''}}</div>
      </div>
      <div class="dstats">
        <div class="ds"><div class="dn">${{d.open}}</div><div class="dl">Open tickets</div></div>
        <div class="ds"><div class="dn">${{d.share}}%</div><div class="dl">Share of open work</div></div>
        <div class="ds"><div class="dn">${{d.done}}</div><div class="dl">Completed</div></div>
      </div>
      <div class="dstatus"><div class="dsub">Status breakdown</div>${{statusRows}}</div>`;
  }}

  function renderAvatars(avatars) {{
    document.getElementById('avatarsContainer').innerHTML = avatars.map(a => {{
      const badge = a.count ? `<span class="cnt">${{a.count}}</span>` : '';
      const ring = a.ring ? ' ' + a.ring : '';
      return `<button class="av${{ring}}" data-name="${{a.name}}" title="${{a.name}}" onclick="showPerson(this)">`
        + `<span class="ini" style="background:${{a.colour}}">${{a.initials}}</span>${{badge}}</button>`;
    }}).join('');
  }}

  function renderView(key) {{
    const view = VIEWS[key];
    const emptyEl = document.getElementById('emptyState');
    const bodyEl = document.getElementById('dashboardBody');

    if (!view.payload) {{
      bodyEl.style.display = 'none';
      emptyEl.style.display = 'block';
      emptyEl.textContent = `No ${{view.label.toLowerCase()}} found for this space right now.`;
      document.getElementById('subtitle').textContent = `${{PROJECT_TITLE}} · generated ${{GENERATED}}`;
      return;
    }}
    emptyEl.style.display = 'none';
    bodyEl.style.display = '';

    const p = view.payload;
    currentDetail = p.detail;

    const sprintBit = view.sprint_name
      ? `${{view.sprint_name}}${{view.date_range ? ' (' + view.date_range + ')' : ''}} · `
      : '';
    document.getElementById('subtitle').textContent =
      `${{PROJECT_TITLE}} · ${{sprintBit}}${{p.n_people}} people · ${{p.total_open}} open tickets · generated ${{GENERATED}}`;

    document.getElementById('kpiTotal').textContent = p.total_open;
    document.getElementById('kpiAvg').textContent = p.avg.toFixed(1);
    document.getElementById('kpiBusiestN').textContent = p.busiest_n;
    document.getElementById('kpiBusiestLabel').textContent = `Busiest: ${{p.busiest}}`;
    document.getElementById('kpiUnassigned').textContent = p.unassigned;
    document.getElementById('loadChartCap').textContent =
      `— red = above team average (${{p.avg.toFixed(1)}}); dashed line = average`;
    document.getElementById('loadChartWrap').style.height = p.load_chart_height + 'px';

    renderAvatars(p.avatars);
    document.querySelectorAll('.av').forEach(a => a.classList.remove('sel'));
    const detailEl = document.getElementById('personDetail');
    detailEl.className = 'detail empty';
    detailEl.textContent = 'Select a team member above to see their open tickets, status breakdown, and share of work.';

    if (loadChart) loadChart.destroy();
    if (statusChart) statusChart.destroy();
    if (shareChart) shareChart.destroy();

    loadChart = new Chart(document.getElementById('loadChart'), {{
      type: 'bar',
      data: {{ labels: p.names, datasets: [{{ label:'Open tickets', data:p.open_counts,
        backgroundColor:p.bar_colours }}] }},
      options: {{ indexAxis:'y', responsive:true, maintainAspectRatio:false,
        plugins: {{ legend:{{display:false}} }},
        scales: {{ x:{{ beginAtZero:true, ticks:{{precision:0}}, grid:{{color:'#ece9e1'}} }},
          y:{{ grid:{{display:false}}, ticks:{{autoSkip:false}} }} }} }},
      plugins: [{{
        id:'avgline',
        afterDraw(c) {{
          if (p.avg <= 0) return;
          const x = c.scales.x.getPixelForValue(p.avg);
          const ctx = c.ctx; ctx.save();
          ctx.strokeStyle='#b0503f'; ctx.setLineDash([5,4]); ctx.lineWidth=1.5;
          ctx.beginPath(); ctx.moveTo(x, c.chartArea.top); ctx.lineTo(x, c.chartArea.bottom); ctx.stroke();
          ctx.restore();
        }}
      }}]
    }});

    statusChart = new Chart(document.getElementById('statusChart'), {{
      type:'bar',
      data: {{ labels: p.names, datasets: p.stacked }},
      options: {{ responsive:true,
        plugins: {{ legend:{{position:'bottom', labels:{{boxWidth:12,font:{{size:11}}}}}} }},
        scales: {{ x:{{ stacked:true, grid:{{display:false}} }},
          y:{{ stacked:true, beginAtZero:true, ticks:{{precision:0}}, grid:{{color:'#ece9e1'}} }} }} }}
    }});

    shareChart = new Chart(document.getElementById('shareChart'), {{
      type:'doughnut',
      data: {{ labels: p.names, datasets:[{{ data:p.share_counts,
        backgroundColor:p.share_colours, borderWidth:1, borderColor:'#fff' }}] }},
      options: {{ responsive:true, cutout:'58%',
        plugins:{{ legend:{{position:'right', labels:{{boxWidth:12,font:{{size:11}}}}}} }} }}
    }});
  }}

  renderView(document.getElementById('viewSelect').value);
</script>
</body></html>"""
    return html
