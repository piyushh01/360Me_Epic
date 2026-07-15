import requests
from app.clients.jira import fetch_all_epics, group_last_three_epics_per_project, fetch_epic_child_status_counts, fetch_epic_weekly_transition_counts
from app.presentation.charts import generate_weekly_chart
from app.clients.slack import upload_file_to_slack

def handle_weekly_progress_request(response_url, channel_id, project_key=None):
    """Runs in the background: pulls last-7-days progress, then posts the chart to Slack."""
    try:
        epics = fetch_all_epics(project_key)
        projects = group_last_three_epics_per_project(epics)

        if not projects:
            message = (
                f'No epics found for project "{project_key}". Check the key is correct.'
                if project_key
                else "No epics found."
            )
            requests.post(response_url, json={"response_type": "ephemeral", "text": message}, timeout=10)
            return

        for info in projects.values():
            for epic in info["epics"]:
                epic["counts"] = fetch_epic_child_status_counts(epic["key"])
                epic["counts"].update(fetch_epic_weekly_transition_counts(epic["key"]))

        chart_bytes = generate_weekly_chart(projects)
        upload_file_to_slack(channel_id, chart_bytes, filename="weekly_progress.png", title="Weekly progress")
    except Exception as e:
        requests.post(
            response_url,
            json={"response_type": "ephemeral", "text": f"Something went wrong pulling weekly progress: {e}"},
            timeout=10,
        )

