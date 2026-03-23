import json
import logging
import math
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

# --- Shared config (applies to all users) ---
JIRA_BASE_URL = os.environ.get("JIRA_BASE_URL", "").rstrip("/")
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL_SECONDS", "120"))

STATE_FILE = Path(os.environ.get("STATE_FILE", "/data/state.json"))
USERS_FILE = Path(os.environ.get("USERS_FILE", "/data/users.json"))
EMBED_COLOR = 0x219AF3  # blue


@dataclass
class UserConfig:
    name: str
    jira_email: str
    jira_api_token: str
    jira_display_name: str
    discord_webhook_url: str
    discord_user_id: str = ""
    enabled: bool = True

    @property
    def jira_auth(self) -> tuple[str, str]:
        return (self.jira_email, self.jira_api_token)


def load_users() -> list[UserConfig]:
    """Load users from JSON file, falling back to env vars for single-user mode."""
    if USERS_FILE.exists():
        data = json.loads(USERS_FILE.read_text())
        return [
            UserConfig(**{k: v for k, v in u.items() if k in UserConfig.__dataclass_fields__})
            for u in data
            if u.get("enabled", True)
        ]

    # Fallback: construct single user from env vars (backward compatibility)
    return [UserConfig(
        name=os.environ.get("JIRA_DISPLAY_NAME", "default"),
        jira_email=os.environ["JIRA_EMAIL"],
        jira_api_token=os.environ["JIRA_API_TOKEN"],
        jira_display_name=os.environ["JIRA_DISPLAY_NAME"],
        discord_webhook_url=os.environ["DISCORD_WEBHOOK_URL"],
        discord_user_id=os.environ.get("DISCORD_USER_ID", ""),
    )]


def load_state() -> dict:
    """Load the full multi-user state dict."""
    if STATE_FILE.exists():
        data = json.loads(STATE_FILE.read_text())
        if isinstance(data, dict) and "users" in data:
            return data
        # Migrate old single-user format
        if isinstance(data, dict):
            old_seen = data.get("seen", [])
            old_last_poll = data.get("last_poll")
        elif isinstance(data, list):
            old_seen = data
            old_last_poll = None
        else:
            return {"users": {}}

        default_name = os.environ.get("JIRA_DISPLAY_NAME", "default")
        log.info("Migrating state from single-user to multi-user format")
        return {
            "users": {
                default_name: {
                    "seen": old_seen,
                    "last_poll": old_last_poll,
                }
            }
        }
    return {"users": {}}


def get_user_state(state: dict, user_name: str) -> tuple[set[str], str | None, str | None]:
    """Extract (seen, last_poll, account_id) for a specific user."""
    user_data = state.get("users", {}).get(user_name, {})
    seen = set(user_data.get("seen", []))
    last_poll = user_data.get("last_poll")
    account_id = user_data.get("account_id")
    return seen, last_poll, account_id


def save_state(state: dict) -> None:
    """Save the full multi-user state."""
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


def get_my_account_id(user: UserConfig) -> str:
    """Fetch the current user's Jira account ID."""
    url = f"{JIRA_BASE_URL}/rest/api/3/myself"
    resp = requests.get(url, auth=user.jira_auth, timeout=30)
    resp.raise_for_status()
    return resp.json()["accountId"]


def jira_search(jql: str, user: UserConfig) -> list[dict]:
    """Run a JQL search and return the issues."""
    url = f"{JIRA_BASE_URL}/rest/api/3/search/jql"
    params = {
        "jql": jql,
        "fields": "summary,creator,updated,description",
        "maxResults": 50,
    }
    resp = requests.get(url, params=params, auth=user.jira_auth, timeout=30)
    resp.raise_for_status()
    return resp.json().get("issues", [])


def get_comments(issue_key: str, user: UserConfig) -> list[dict]:
    """Fetch comments for an issue."""
    url = f"{JIRA_BASE_URL}/rest/api/3/issue/{issue_key}/comment"
    resp = requests.get(url, auth=user.jira_auth, timeout=30)
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
    return text[:max_len].rsplit(" ", 1)[0] + "\u2026"


def send_discord(embed: dict, user: UserConfig) -> None:
    mention = f"<@{user.discord_user_id}>" if user.discord_user_id else ""
    payload = {"content": mention, "embeds": [embed]}
    resp = requests.post(user.discord_webhook_url, json=payload, timeout=15)
    if resp.status_code == 429:
        retry_after = resp.json().get("retry_after", 5)
        log.warning("[%s] Discord rate-limited, retrying after %ss", user.name, retry_after)
        time.sleep(retry_after)
        requests.post(user.discord_webhook_url, json=payload, timeout=15).raise_for_status()
    else:
        resp.raise_for_status()


def poll_once(seen: set[str], account_id: str, user: UserConfig, lookback_minutes: int | None = None) -> set[str]:
    interval_minutes = lookback_minutes or max(POLL_INTERVAL * 2 // 60, 3)

    queries = [
        (
            f'text ~ "{user.jira_display_name}" '
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
        log.info("[%s] Searching Jira: %s", user.name, jql)
        try:
            for issue in jira_search(jql, user):
                all_issues[issue["key"]] = issue
        except requests.RequestException as exc:
            log.error("[%s] Jira search failed: %s", user.name, exc)

    log.info("[%s] Found %d unique issues to check", user.name, len(all_issues))

    for key, issue in all_issues.items():
        fields = issue["fields"]
        summary = fields.get("summary", "(no summary)")
        issue_url = f"{JIRA_BASE_URL}/browse/{key}"

        # Check description for mention
        description = fields.get("description")
        if is_mentioned(description, account_id, user.jira_display_name):
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
                }, user)
                seen.add(state_key)
                log.info("[%s] Notified: %s (description)", user.name, key)

        # Check comments for mentions
        try:
            comments = get_comments(key, user)
        except requests.RequestException as exc:
            log.error("[%s] Failed to fetch comments for %s: %s", user.name, key, exc)
            continue

        for comment in comments:
            body = comment.get("body")
            if not is_mentioned(body, account_id, user.jira_display_name):
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
            }, user)
            seen.add(state_key)
            log.info("[%s] Notified: %s comment %s", user.name, key, comment_id)

    return seen


def main() -> None:
    users = load_users()
    log.info("Loaded %d user(s)", len(users))
    for u in users:
        log.info("  - %s (%s)", u.name, u.jira_email)

    state = load_state()

    # Resolve account IDs for any users missing them
    for user in users:
        _seen, _last_poll, cached_id = get_user_state(state, user.name)
        if not cached_id:
            cached_id = get_my_account_id(user)
            state.setdefault("users", {}).setdefault(user.name, {})["account_id"] = cached_id
            log.info("[%s] Resolved account ID: %s", user.name, cached_id)

    save_state(state)

    while True:
        # Reload users each cycle (allows hot-adding users without restart)
        users = load_users()
        state = load_state()

        for user in users:
            log.info("[%s] Polling...", user.name)
            seen, last_poll, account_id = get_user_state(state, user.name)

            if not account_id:
                try:
                    account_id = get_my_account_id(user)
                except requests.RequestException as exc:
                    log.error("[%s] Failed to resolve account ID: %s", user.name, exc)
                    continue

            # Calculate lookback
            catchup_minutes = None
            if last_poll:
                last_poll_dt = datetime.fromisoformat(last_poll)
                gap = datetime.now(timezone.utc) - last_poll_dt
                catchup_minutes = min(math.ceil(gap.total_seconds() / 60) + 1, 10080)

            seen = poll_once(seen, account_id, user, lookback_minutes=catchup_minutes)

            # Update state for this user
            state.setdefault("users", {})[user.name] = {
                "seen": sorted(seen),
                "last_poll": datetime.now(timezone.utc).isoformat(),
                "account_id": account_id,
            }

        save_state(state)
        log.info("Sleeping %ds until next poll", POLL_INTERVAL)
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
