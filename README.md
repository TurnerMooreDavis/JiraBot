# Jira Discord Connect

A lightweight polling daemon that monitors Jira for mentions of your name and sends notifications to a Discord channel via webhook.

## How It Works

The poller runs continuously and:

1. Searches Jira for recently updated issues that mention you (by `@mention` or plain text name match)
2. Checks both issue descriptions and comments
3. Sends a Discord embed with the issue title, link, author, and a snippet of the mention
4. Tracks what it has already notified you about in a state file to avoid duplicates
5. On restart, catches up on anything it missed while stopped (up to 7 days back)

## Setup

### Prerequisites

- A Jira Cloud instance with an [API token](https://id.atlassian.com/manage-profile/security/api-tokens)
- A [Discord webhook URL](https://support.discord.com/hc/en-us/articles/228383668-Intro-to-Webhooks) for the channel you want notifications in

### Configuration

Copy the example env file and set your Jira instance URL:

```bash
cp .env.example .env
```

| Variable | Required | Description |
|---|---|---|
| `JIRA_BASE_URL` | Yes | Your Jira instance URL (e.g. `https://yourcompany.atlassian.net`) |
| `POLL_INTERVAL_SECONDS` | No | Seconds between polls (default: `120`) |

### Adding Users

Per-user config (Jira credentials, Discord webhook) lives in `users.json`. Copy the example and add your users:

```bash
cp users.json.example users.json
```

Each entry looks like:

```json
{
  "name": "Turner Davis",
  "jira_email": "turner@example.com",
  "jira_api_token": "your-jira-api-token",
  "jira_display_name": "Turner Davis",
  "discord_webhook_url": "https://discord.com/api/webhooks/your/webhook",
  "discord_user_id": "236315893158641674",
  "enabled": true
}
```

| Field | Required | Description |
|---|---|---|
| `name` | Yes | A label for this user (used in logs and state tracking) |
| `jira_email` | Yes | The user's Jira account email |
| `jira_api_token` | Yes | The user's Jira API token |
| `jira_display_name` | Yes | Display name in Jira (used for plain-text mention matching) |
| `discord_webhook_url` | Yes | Discord webhook URL where this user's alerts are sent |
| `discord_user_id` | No | Discord user ID -- if set, the bot will `@mention` them in notifications |
| `enabled` | No | Set to `false` to temporarily disable a user (default: `true`) |

Users can be added or removed by editing `users.json` -- changes are picked up on the next poll cycle without restarting.

#### How to add a new user

1. **Get a Jira API token.** The new user should go to [Atlassian API tokens](https://id.atlassian.com/manage-profile/security/api-tokens) and click "Create API token". Copy the token.

2. **Find their Jira display name.** This is the name shown on their Jira profile (e.g. "Jane Smith"). It needs to match exactly for plain-text mention detection to work.

3. **Create a Discord webhook.** In the Discord channel where the user wants notifications, go to channel settings > Integrations > Webhooks > New Webhook. Copy the webhook URL. Each user can have their own channel/webhook.

4. **(Optional) Get their Discord user ID.** In Discord, enable Developer Mode (User Settings > Advanced > Developer Mode), then right-click the user and click "Copy User ID". This enables `@mention` pings in notifications.

5. **Edit `users.json`.** Add a new entry to the array:

    ```json
    [
      { "...existing user..." },
      {
        "name": "Jane Smith",
        "jira_email": "jane@yourcompany.com",
        "jira_api_token": "paste-token-here",
        "jira_display_name": "Jane Smith",
        "discord_webhook_url": "https://discord.com/api/webhooks/your/new-webhook",
        "discord_user_id": "123456789012345678",
        "enabled": true
      }
    ]
    ```

6. **Wait.** The poller reloads `users.json` on every cycle, so the new user will start receiving notifications within one poll interval (default 120 seconds). No restart required.

7. **Verify.** Check the logs to confirm the new user is being polled:

    ```bash
    docker compose logs -f
    ```

    You should see lines like:

    ```
    [Jane Smith] Resolved account ID: 712020:abcd1234...
    [Jane Smith] Polling...
    ```

#### Single-user mode

If no `users.json` exists, the poller falls back to reading per-user config from environment variables (`JIRA_EMAIL`, `JIRA_API_TOKEN`, `JIRA_DISPLAY_NAME`, `DISCORD_WEBHOOK_URL`, `DISCORD_USER_ID`). This preserves backward compatibility with the original single-user setup.

### Running with Docker Compose

```bash
docker compose up -d
```

This builds the image, starts the poller in the background, and persists state across restarts via a Docker volume. The `users.json` file is bind-mounted read-only into the container.

### Running directly

```bash
pip install -r requirements.txt
python poller.py
```

When running outside Docker, set `STATE_FILE` and `USERS_FILE` to writable/readable paths (they default to `/data/state.json` and `/data/users.json`).

## Mention Detection

The poller detects mentions two ways:

- **@mentions** -- Jira stores these as structured nodes in Atlassian Document Format (ADF). The poller walks the document tree and matches your account ID.
- **Plain text** -- If someone types your display name without using `@`, the poller catches that too via case-insensitive text search.

Both issue descriptions and comments are checked.
