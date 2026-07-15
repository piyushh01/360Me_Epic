from flask import Blueprint, request, jsonify
from app.clients.slack import verify_slack_request
import threading
from app.services.epic import handle_project_help_request, handle_epic_html_summary_request, parse_epic_summary_command
from app.services.weekly import handle_weekly_progress_request
from app.services.allocation import handle_allocation_request, parse_allocation_command

bp = Blueprint('routes', __name__)

@bp.route("/slack/project-status", methods=["POST"])
def project_status():
    if not verify_slack_request(request):
        return jsonify({"error": "invalid signature"}), 401

    response_url = request.form.get("response_url")
    channel_id = request.form.get("channel_id")
    text = request.form.get("text", "").strip()

    if text.lower() == "help":
        threading.Thread(target=handle_project_help_request, args=(response_url, channel_id)).start()
        return jsonify({"response_type": "ephemeral", "text": "Pulling all spaces and their epics, one moment..."})

    epic_summary_text = parse_epic_summary_command(text)
    if epic_summary_text is None:
        return jsonify(
            {
                "response_type": "ephemeral",
                "text": (
                    "Usage: /project-status <epic name> summary (e.g. /project-status 12 week reset summary), "
                    "or /project-status help to list all spaces and their epics."
                ),
            }
        )

    threading.Thread(
        target=handle_epic_html_summary_request, args=(response_url, channel_id, epic_summary_text)
    ).start()
    return jsonify(
        {
            "response_type": "ephemeral",
            "text": f'Building a desktop HTML summary for "{epic_summary_text}", one moment...',
        }
    )

@bp.route("/slack/weekly-progress", methods=["POST"])
def weekly_progress():
    if not verify_slack_request(request):
        return jsonify({"error": "invalid signature"}), 401

    response_url = request.form.get("response_url")
    channel_id = request.form.get("channel_id")
    project_key = request.form.get("text", "").strip().upper() or None

    threading.Thread(target=handle_weekly_progress_request, args=(response_url, channel_id, project_key)).start()

    ack_text = (
        f"Pulling the last 7 days for {project_key}, one moment..."
        if project_key
        else "Pulling the last 7 days across all projects, one moment..."
    )
    return jsonify({"response_type": "ephemeral", "text": ack_text})

@bp.route("/slack/project-allocation", methods=["POST"])
def project_allocation():
    if not verify_slack_request(request):
        return jsonify({"error": "invalid signature"}), 401

    response_url = request.form.get("response_url")
    channel_id = request.form.get("channel_id")
    text = request.form.get("text", "")
    name = parse_allocation_command(text)

    threading.Thread(
        target=handle_allocation_request,
        args=(response_url, channel_id, name),
    ).start()

    ack = (f"Building team work distribution for \u201c{name}\u201d, one moment..."
           if name else
           "Usage: /project-allocation 'Space name' summary")
    return jsonify({"response_type": "ephemeral", "text": ack})

@bp.route("/healthz", methods=["GET"])
def healthz():
    return "ok"

