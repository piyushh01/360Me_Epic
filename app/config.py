import os
from dotenv import load_dotenv

load_dotenv()

SLACK_SIGNING_SECRET = os.environ["SLACK_SIGNING_SECRET"]
SLACK_BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]
JIRA_BASE_URL = os.environ["JIRA_BASE_URL"].rstrip("/")
JIRA_EMAIL = os.environ["JIRA_EMAIL"]
JIRA_API_TOKEN = os.environ["JIRA_API_TOKEN"]
JIRA_AUTH = (JIRA_EMAIL, JIRA_API_TOKEN)

DONE_CATEGORY = "done"

TEAM = [
    "Manav Prajapati",
    "Vimal Raval",
    "Smita Chauhan",
    "Sergei Markochev",
    "Rituraj Thakur",
    "Samet Macit",
    "juned mansuri",
    "Abhinav Singh",
    "Piyush.Yadav",
    "Muhammad Zuraid",
]

QA_PERSON = "Abhinav Singh"
QA_STATUSES = {"ready for test", "in qa"}
STATUS_BUCKETS = ["To Do", "In Progress", "Ready for Test", "In QA", "Blocked"]
ISSUE_FIELDS = ["issuetype", "status", "assignee"]
EPIC_SNAPSHOT_DIR = os.environ.get("EPIC_SNAPSHOT_DIR", "epic_snapshots")
