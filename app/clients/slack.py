import hashlib
import hmac
import time
import requests

from app.config import SLACK_SIGNING_SECRET, SLACK_BOT_TOKEN

def verify_slack_request(req):
    """Confirms the request really came from Slack using the signing secret."""
    timestamp = req.headers.get("X-Slack-Request-Timestamp", "")
    if not timestamp:
        return False
    try:
        if abs(time.time() - int(timestamp)) > 60 * 5:
            return False  # too old, possible replay attack
    except ValueError:
        return False

    sig_basestring = f"v0:{timestamp}:{req.get_data(as_text=True)}"
    my_signature = "v0=" + hmac.new(
        SLACK_SIGNING_SECRET.encode(), sig_basestring.encode(), hashlib.sha256
    ).hexdigest()

    slack_signature = req.headers.get("X-Slack-Signature", "")
    return hmac.compare_digest(my_signature, slack_signature)

def upload_file_to_slack(channel_id, file_bytes, filename, title):
    """Slack's current 3-step external upload flow: get an upload URL, POST
    the bytes to it, then finalize. Used for both chart images and HTML
    reports -- Slack doesn't care about content type, just bytes."""
    get_url_resp = requests.post(
        "https://slack.com/api/files.getUploadURLExternal",
        headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"},
        data={"filename": filename, "length": len(file_bytes)},
        timeout=15,
    )
    get_url_data = get_url_resp.json()
    if not get_url_data.get("ok"):
        raise RuntimeError(f"files.getUploadURLExternal failed: {get_url_data}")

    upload_url = get_url_data["upload_url"]
    file_id = get_url_data["file_id"]

    upload_resp = requests.post(upload_url, data=file_bytes, timeout=30)
    upload_resp.raise_for_status()

    complete_resp = requests.post(
        "https://slack.com/api/files.completeUploadExternal",
        headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}", "Content-Type": "application/json"},
        json={"files": [{"id": file_id, "title": title}], "channel_id": channel_id},
        timeout=15,
    )
    complete_data = complete_resp.json()
    if not complete_data.get("ok"):
        raise RuntimeError(f"files.completeUploadExternal failed: {complete_data}")
