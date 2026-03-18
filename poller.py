import json
import logging
import math
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

# --- Config ---
JIRA_BASE_URL = os.environ["JIRA_BASE_URL"].rstrip("/")
JIRA_EMAIL = os.environ["JIRA_EMAIL"]
JIRA_API_TOKEN = os.environ["JIRA_API_TOKEN"]
JIRA_DISPLAY_NAME = os.environ["JIRA_DISPLAY_NAME"]
DISCORD_WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]
DISCORD_USER_ID = os.environ.get("DISCORD_USER_ID", "")
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL_SECONDS", "120"))

STATE_FILE = Path(os.environ.get("STATE_FILE", "/data/state.json"))
JIRA_AUTH = (JIRA_EMAIL, JIRA_API_TOKEN)
EMBED_COLOR = 0x219AF3  # blue


def load_state() -> tuple[set[str], str | None]:
    """Load seen set and last poll timestamp from state file."""
    if STATE_FILE.exists():
        data = json.loads(STATE_FILE.read_text())
        if isinstance(data, dict):
            return set(data.get("seen", [])), data.get("last_poll")
        # Migrate old format (plain list)
        return set(data), None
    return set(), None


def save_state(seen: set[str]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps({
        "seen": sorted(seen),
        "last_poll": datetime.now(timezone.utc).isoformat(),
    }))


def get_my_account_id() -> str:
    """Fetch the current user's Jira account ID."""
    url = f"{JIRA_BASE_URL}/rest/api/3/myself"
    resp = requests.get(url, auth=JIRA_AUTH, timeout=30)
    resp.raise_for_status()
    return resp.json()["accountId"]


def jira_search(jql: str) -> list[dict]:
    """Run a JQL search and return the issues."""
    url = f"{JIRA_BASE_URL}/rest/api/3/search/jql"
    params = {
        "jql": jql,
        "fields": "summary,creator,updated,description",
        "maxResults": 50,
    }
    resp = requests.get(url, params=params, auth=JIRA_AUTH, timeout=30)
    resp.raise_for_status()
    return resp.json().get("issues", [])


def get_comments(issue_key: str) -> list[dict]:
    """Fetch comments for an issue."""
    url = f"{JIRA_BASE_URL}/rest/api/3/issue/{issue_key}/comment"
    resp = requests.get(url, auth=JIRA_AUTH, timeout=30)
    resp.raise_for_status()
    return resp.json().get("comments", [])


def extract_text(adf_node: dict | str | None) -> str:
    """Recursively extract plain text from Atlassian Document Format."""
    if adf_node is None:
        return ""
    if isinstance(adf_node, str):
        return adf_node
    text = adf_node.get("text", "")
    # Handle @mention nodes — their display text is in attrs.text
    if adf_node.get("type") == "mention":
        text += adf_node.get("attrs", {}).get("text", "")
    for child in adf_node.get("content", []):
        text += extract_text(child)
    return text


def has_mention(adf_node: dict | None, account_id: str) -> bool:
    """Check if ADF contains a @mention of the given account ID."""
    if adf_node is None or not isinstance(adf_node, dict):
        return False
    if (
        adf_node.get("type") == "mention"
        and adf_node.get("attrs", {}).get("id") == account_id
    ):
        return True
    for child in adf_node.get("content", []):
        if has_mention(child, account_id):
            return True
    return False


def is_mentioned(adf_node: dict | None, account_id: str, display_name: str) -> bool:
    """Check if user is mentioned — either via @mention node or plain text."""
    if has_mention(adf_node, account_id):
        return True
    text = extract_text(adf_node)
    return display_name.lower() in text.lower()


def snippet(text: str, max_len: int = 300) -> str:
    text = text.strip()
    if len(text) <= max_len:
        return text
    return text[:max_len].rsplit(" ", 1)[0] + "…"


def send_discord(embed: dict) -> None:
    mention = f"<@{DISCORD_USER_ID}>" if DISCORD_USER_ID else ""
    payload = {"content": mention, "embeds": [embed]}
    resp = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=15)
    if resp.status_code == 429:
        retry_after = resp.json().get("retry_after", 5)
        log.warning("Discord rate-limited, retrying after %ss", retry_after)
        time.sleep(retry_after)
        requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=15).raise_for_status()
    else:
        resp.raise_for_status()


def poll_once(seen: set[str], account_id: str, lookback_minutes: int | None = None) -> set[str]:
    interval_minutes = lookback_minutes or max(POLL_INTERVAL * 2 // 60, 3)

    # Two JQL queries:
    # 1) Plain text mentions (catches "Turner Davis" written as text)
    # 2) All recently updated issues (to catch @mentions stored as ADF nodes)
    queries = [
        (
            f'text ~ "{JIRA_DISPLAY_NAME}" '
            f"AND updated >= -{interval_minutes}m "
            f"ORDER BY updated DESC"
        ),
        (
            f"updated >= -{interval_minutes}m "
            f"ORDER BY updated DESC"
        ),
    ]

    all_issues: dict[str, dict] = {}
    for jql in queries:
        log.info("Searching Jira: %s", jql)
        try:
            for issue in jira_search(jql):
                all_issues[issue["key"]] = issue
        except requests.RequestException as exc:
            log.error("Jira search failed: %s", exc)

    log.info("Found %d unique issues to check", len(all_issues))

    for key, issue in all_issues.items():
        fields = issue["fields"]
        summary = fields.get("summary", "(no summary)")
        issue_url = f"{JIRA_BASE_URL}/browse/{key}"

        # Check description for mention
        description = fields.get("description")
        if is_mentioned(description, account_id, JIRA_DISPLAY_NAME):
            creator = (fields.get("creator") or {}).get("displayName", "Unknown")
            state_key = f"{key}:description:{fields.get('updated', '')}"
            if state_key not in seen:
                desc_text = extract_text(description)
                send_discord({
                    "title": f"{key}: {summary}",
                    "url": issue_url,
                    "description": (
                        f"You were mentioned in the description by **{creator}**:\n"
                        f"> {snippet(desc_text)}"
                    ),
                    "color": EMBED_COLOR,
                    "timestamp": fields.get("updated"),
                })
                seen.add(state_key)
                log.info("Notified: %s (description)", key)

        # Check comments for mentions
        try:
            comments = get_comments(key)
        except requests.RequestException as exc:
            log.error("Failed to fetch comments for %s: %s", key, exc)
            continue

        for comment in comments:
            body = comment.get("body")
            if not is_mentioned(body, account_id, JIRA_DISPLAY_NAME):
                continue

            comment_id = comment["id"]
            updated = comment.get("updated", "")
            state_key = f"{key}:{comment_id}:{updated}"
            if state_key in seen:
                continue

            author = (comment.get("author") or {}).get("displayName", "Unknown")
            body_text = extract_text(body)
            send_discord({
                "title": f"{key}: {summary}",
                "url": issue_url,
                "description": (
                    f"You were mentioned in a comment by **{author}**:\n"
                    f"> {snippet(body_text)}"
                ),
                "color": EMBED_COLOR,
                "timestamp": comment.get("updated"),
            })
            seen.add(state_key)
            log.info("Notified: %s comment %s", key, comment_id)

    return seen


def main() -> None:
    log.info(
        "Starting poller: Jira=%s, name=%s, interval=%ds",
        JIRA_BASE_URL, JIRA_DISPLAY_NAME, POLL_INTERVAL,
    )

    account_id = get_my_account_id()
    log.info("Resolved account ID: %s", account_id)

    seen, last_poll = load_state()
    log.info("Loaded %d seen entries from state file", len(seen))

    # On startup, look back to when we last polled (capped at 7 days)
    catchup_minutes = None
    if last_poll:
        last_poll_dt = datetime.fromisoformat(last_poll)
        gap = datetime.now(timezone.utc) - last_poll_dt
        catchup_minutes = min(math.ceil(gap.total_seconds() / 60) + 1, 10080)
        log.info("Last poll was %s — searching back %d minutes", last_poll, catchup_minutes)

    while True:
        seen = poll_once(seen, account_id, lookback_minutes=catchup_minutes)
        catchup_minutes = None  # Only use catchup window on first poll
        save_state(seen)
        log.info("Sleeping %ds until next poll", POLL_INTERVAL)
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
