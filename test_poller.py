import json
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

import poller
from poller import (
    UserConfig,
    extract_text,
    get_comments,
    get_my_account_id,
    get_user_state,
    has_mention,
    is_mentioned,
    jira_search,
    load_state,
    load_users,
    poll_once,
    save_state,
    send_discord,
    snippet,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def make_user(**overrides) -> UserConfig:
    defaults = dict(
        name="Test User",
        jira_email="test@example.com",
        jira_api_token="tok-123",
        jira_display_name="Test User",
        discord_webhook_url="https://discord.com/api/webhooks/1/abc",
        discord_user_id="999",
    )
    defaults.update(overrides)
    return UserConfig(**defaults)


def make_adf_mention(account_id: str, display_text: str = "Someone") -> dict:
    """Create a minimal ADF document with an @mention node."""
    return {
        "type": "doc",
        "content": [
            {
                "type": "paragraph",
                "content": [
                    {"type": "text", "text": "Hello "},
                    {
                        "type": "mention",
                        "attrs": {"id": account_id, "text": display_text},
                    },
                ],
            }
        ],
    }


def make_adf_text(text: str) -> dict:
    """Create a minimal ADF document with plain text."""
    return {
        "type": "doc",
        "content": [
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": text}],
            }
        ],
    }


# ---------------------------------------------------------------------------
# extract_text
# ---------------------------------------------------------------------------

class TestExtractText:
    def test_plain_text_node(self):
        adf = {"type": "text", "text": "hello world"}
        assert extract_text(adf) == "hello world"

    def test_with_mention(self):
        adf = make_adf_mention("acc-1", "Turner Davis")
        result = extract_text(adf)
        assert "Hello" in result
        assert "Turner Davis" in result

    def test_nested(self):
        adf = {
            "type": "doc",
            "content": [
                {
                    "type": "paragraph",
                    "content": [
                        {"type": "text", "text": "aaa"},
                        {
                            "type": "paragraph",
                            "content": [{"type": "text", "text": "bbb"}],
                        },
                    ],
                }
            ],
        }
        assert extract_text(adf) == "aaabbb"

    def test_none(self):
        assert extract_text(None) == ""

    def test_string_input(self):
        assert extract_text("plain string") == "plain string"


# ---------------------------------------------------------------------------
# has_mention
# ---------------------------------------------------------------------------

class TestHasMention:
    def test_true(self):
        adf = make_adf_mention("acc-1")
        assert has_mention(adf, "acc-1") is True

    def test_false_different_id(self):
        adf = make_adf_mention("acc-1")
        assert has_mention(adf, "acc-2") is False

    def test_none(self):
        assert has_mention(None, "acc-1") is False

    def test_non_dict(self):
        assert has_mention("not a dict", "acc-1") is False


# ---------------------------------------------------------------------------
# is_mentioned
# ---------------------------------------------------------------------------

class TestIsMentioned:
    def test_by_account_id(self):
        adf = make_adf_mention("acc-1", "Test User")
        assert is_mentioned(adf, "acc-1", "Somebody Else") is True

    def test_by_display_name(self):
        adf = make_adf_text("Hey Test User, check this out")
        assert is_mentioned(adf, "no-match", "Test User") is True

    def test_display_name_case_insensitive(self):
        adf = make_adf_text("Hey test user, check this out")
        assert is_mentioned(adf, "no-match", "Test User") is True

    def test_neither(self):
        adf = make_adf_text("No mention here")
        assert is_mentioned(adf, "acc-1", "Test User") is False


# ---------------------------------------------------------------------------
# snippet
# ---------------------------------------------------------------------------

class TestSnippet:
    def test_short(self):
        assert snippet("short text") == "short text"

    def test_long(self):
        text = "word " * 100  # 500 chars
        result = snippet(text, max_len=20)
        assert len(result) <= 25  # some tolerance for the ellipsis
        assert result.endswith("\u2026")

    def test_strips_whitespace(self):
        assert snippet("  hello  ") == "hello"


# ---------------------------------------------------------------------------
# UserConfig
# ---------------------------------------------------------------------------

class TestUserConfig:
    def test_jira_auth(self):
        user = make_user(jira_email="a@b.com", jira_api_token="tok")
        assert user.jira_auth == ("a@b.com", "tok")

    def test_defaults(self):
        user = UserConfig(
            name="X",
            jira_email="x@x.com",
            jira_api_token="t",
            jira_display_name="X",
            discord_webhook_url="http://hook",
        )
        assert user.discord_user_id == ""
        assert user.enabled is True


# ---------------------------------------------------------------------------
# load_users
# ---------------------------------------------------------------------------

class TestLoadUsers:
    def test_from_file(self, tmp_path):
        users_data = [
            {
                "name": "Alice",
                "jira_email": "alice@co.com",
                "jira_api_token": "tok-a",
                "jira_display_name": "Alice A",
                "discord_webhook_url": "http://hook-a",
                "discord_user_id": "111",
            },
            {
                "name": "Bob",
                "jira_email": "bob@co.com",
                "jira_api_token": "tok-b",
                "jira_display_name": "Bob B",
                "discord_webhook_url": "http://hook-b",
            },
        ]
        f = tmp_path / "users.json"
        f.write_text(json.dumps(users_data))

        with patch.object(poller, "USERS_FILE", f):
            users = load_users()

        assert len(users) == 2
        assert users[0].name == "Alice"
        assert users[1].discord_user_id == ""

    def test_skips_disabled(self, tmp_path):
        users_data = [
            {
                "name": "Alice",
                "jira_email": "a@co.com",
                "jira_api_token": "t",
                "jira_display_name": "Alice",
                "discord_webhook_url": "http://hook",
                "enabled": True,
            },
            {
                "name": "Bob",
                "jira_email": "b@co.com",
                "jira_api_token": "t",
                "jira_display_name": "Bob",
                "discord_webhook_url": "http://hook",
                "enabled": False,
            },
        ]
        f = tmp_path / "users.json"
        f.write_text(json.dumps(users_data))

        with patch.object(poller, "USERS_FILE", f):
            users = load_users()

        assert len(users) == 1
        assert users[0].name == "Alice"

    def test_fallback_to_env(self, tmp_path):
        f = tmp_path / "nonexistent.json"
        env = {
            "JIRA_DISPLAY_NAME": "Env User",
            "JIRA_EMAIL": "env@co.com",
            "JIRA_API_TOKEN": "env-tok",
            "DISCORD_WEBHOOK_URL": "http://env-hook",
            "DISCORD_USER_ID": "555",
        }
        with patch.object(poller, "USERS_FILE", f), patch.dict("os.environ", env):
            users = load_users()

        assert len(users) == 1
        assert users[0].name == "Env User"
        assert users[0].jira_email == "env@co.com"
        assert users[0].discord_user_id == "555"


# ---------------------------------------------------------------------------
# State management
# ---------------------------------------------------------------------------

class TestState:
    def test_load_empty(self, tmp_path):
        f = tmp_path / "state.json"
        with patch.object(poller, "STATE_FILE", f):
            state = load_state()
        assert state == {"users": {}}

    def test_save_and_load_roundtrip(self, tmp_path):
        f = tmp_path / "state.json"
        state = {
            "users": {
                "Alice": {
                    "seen": ["PROJ-1:description:2026-01-01"],
                    "last_poll": "2026-01-01T00:00:00+00:00",
                    "account_id": "acc-a",
                }
            }
        }
        with patch.object(poller, "STATE_FILE", f):
            save_state(state)
            loaded = load_state()

        assert loaded["users"]["Alice"]["seen"] == ["PROJ-1:description:2026-01-01"]
        assert loaded["users"]["Alice"]["account_id"] == "acc-a"

    def test_migration_old_dict_format(self, tmp_path):
        f = tmp_path / "state.json"
        f.write_text(json.dumps({
            "seen": ["KEY-1:desc:ts1"],
            "last_poll": "2026-01-01T00:00:00+00:00",
        }))
        with patch.object(poller, "STATE_FILE", f), \
             patch.dict("os.environ", {"JIRA_DISPLAY_NAME": "Migrated"}):
            state = load_state()

        assert "Migrated" in state["users"]
        assert "KEY-1:desc:ts1" in state["users"]["Migrated"]["seen"]

    def test_migration_old_list_format(self, tmp_path):
        f = tmp_path / "state.json"
        f.write_text(json.dumps(["KEY-1:desc:ts1", "KEY-2:c1:ts2"]))
        with patch.object(poller, "STATE_FILE", f), \
             patch.dict("os.environ", {"JIRA_DISPLAY_NAME": "Legacy"}):
            state = load_state()

        assert "Legacy" in state["users"]
        assert state["users"]["Legacy"]["last_poll"] is None

    def test_get_user_state(self):
        state = {
            "users": {
                "Alice": {
                    "seen": ["a", "b"],
                    "last_poll": "2026-01-01T00:00:00+00:00",
                    "account_id": "acc-a",
                }
            }
        }
        seen, last_poll, account_id = get_user_state(state, "Alice")
        assert seen == {"a", "b"}
        assert last_poll == "2026-01-01T00:00:00+00:00"
        assert account_id == "acc-a"

    def test_get_user_state_missing(self):
        seen, last_poll, account_id = get_user_state({"users": {}}, "Nobody")
        assert seen == set()
        assert last_poll is None
        assert account_id is None


# ---------------------------------------------------------------------------
# Jira API functions
# ---------------------------------------------------------------------------

class TestJiraApi:
    def test_get_my_account_id(self):
        user = make_user()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"accountId": "acc-123"}
        mock_resp.raise_for_status = MagicMock()

        with patch("poller.requests.get", return_value=mock_resp) as mock_get:
            result = get_my_account_id(user)

        assert result == "acc-123"
        mock_get.assert_called_once()
        assert mock_get.call_args[1]["auth"] == user.jira_auth

    def test_jira_search(self):
        user = make_user()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"issues": [{"key": "PROJ-1"}, {"key": "PROJ-2"}]}
        mock_resp.raise_for_status = MagicMock()

        with patch("poller.requests.get", return_value=mock_resp):
            issues = jira_search("updated >= -5m", user)

        assert len(issues) == 2
        assert issues[0]["key"] == "PROJ-1"

    def test_get_comments(self):
        user = make_user()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"comments": [{"id": "c1", "body": {}}]}
        mock_resp.raise_for_status = MagicMock()

        with patch("poller.requests.get", return_value=mock_resp):
            comments = get_comments("PROJ-1", user)

        assert len(comments) == 1
        assert comments[0]["id"] == "c1"


# ---------------------------------------------------------------------------
# Discord sending
# ---------------------------------------------------------------------------

class TestSendDiscord:
    def test_success(self):
        user = make_user(discord_user_id="")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()

        with patch("poller.requests.post", return_value=mock_resp) as mock_post:
            send_discord({"title": "test"}, user)

        payload = mock_post.call_args[1]["json"]
        assert payload["content"] == ""
        assert payload["embeds"] == [{"title": "test"}]

    def test_with_user_id(self):
        user = make_user(discord_user_id="12345")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()

        with patch("poller.requests.post", return_value=mock_resp) as mock_post:
            send_discord({"title": "test"}, user)

        payload = mock_post.call_args[1]["json"]
        assert payload["content"] == "<@12345>"

    def test_rate_limited(self):
        user = make_user()
        rate_resp = MagicMock()
        rate_resp.status_code = 429
        rate_resp.json.return_value = {"retry_after": 0.01}

        retry_resp = MagicMock()
        retry_resp.raise_for_status = MagicMock()

        with patch("poller.requests.post", side_effect=[rate_resp, retry_resp]) as mock_post, \
             patch("poller.time.sleep") as mock_sleep:
            send_discord({"title": "test"}, user)

        assert mock_post.call_count == 2
        mock_sleep.assert_called_once_with(0.01)


# ---------------------------------------------------------------------------
# poll_once
# ---------------------------------------------------------------------------

class TestPollOnce:
    def _mock_jira(self, issues, comments_by_key=None):
        """Set up mocks for jira_search and get_comments."""
        comments_by_key = comments_by_key or {}

        def fake_search(jql, user):
            return issues

        def fake_comments(key, user):
            return comments_by_key.get(key, [])

        return fake_search, fake_comments

    def test_finds_mention_in_description(self):
        user = make_user(jira_display_name="Test User")
        adf = make_adf_text("Hey Test User, look at this")
        issues = [{
            "key": "PROJ-1",
            "fields": {
                "summary": "Bug report",
                "description": adf,
                "updated": "2026-01-01T00:00:00+00:00",
                "creator": {"displayName": "Alice"},
            },
        }]
        search_fn, comments_fn = self._mock_jira(issues)

        with patch("poller.jira_search", side_effect=search_fn), \
             patch("poller.get_comments", side_effect=comments_fn), \
             patch("poller.send_discord") as mock_discord:
            seen = poll_once(set(), "acc-1", user, lookback_minutes=5)

        mock_discord.assert_called_once()
        embed = mock_discord.call_args[0][0]
        assert "PROJ-1" in embed["title"]
        assert "Alice" in embed["description"]
        assert len(seen) == 1

    def test_finds_mention_in_comment(self):
        user = make_user(jira_display_name="Test User")
        issues = [{
            "key": "PROJ-1",
            "fields": {
                "summary": "Task",
                "description": make_adf_text("No mention here"),
                "updated": "2026-01-01T00:00:00+00:00",
                "creator": {"displayName": "Alice"},
            },
        }]
        comments = [{
            "id": "c1",
            "body": make_adf_text("Hey Test User"),
            "updated": "2026-01-01T00:00:00+00:00",
            "author": {"displayName": "Bob"},
        }]
        search_fn, _ = self._mock_jira(issues)

        with patch("poller.jira_search", side_effect=search_fn), \
             patch("poller.get_comments", return_value=comments), \
             patch("poller.send_discord") as mock_discord:
            seen = poll_once(set(), "acc-1", user, lookback_minutes=5)

        mock_discord.assert_called_once()
        embed = mock_discord.call_args[0][0]
        assert "Bob" in embed["description"]
        assert "PROJ-1:c1:" in next(iter(seen))

    def test_skips_already_seen(self):
        user = make_user(jira_display_name="Test User")
        adf = make_adf_text("Hey Test User")
        issues = [{
            "key": "PROJ-1",
            "fields": {
                "summary": "Bug",
                "description": adf,
                "updated": "2026-01-01T00:00:00+00:00",
                "creator": {"displayName": "Alice"},
            },
        }]
        already_seen = {"PROJ-1:description:2026-01-01T00:00:00+00:00"}
        search_fn, comments_fn = self._mock_jira(issues)

        with patch("poller.jira_search", side_effect=search_fn), \
             patch("poller.get_comments", side_effect=comments_fn), \
             patch("poller.send_discord") as mock_discord:
            seen = poll_once(already_seen, "acc-1", user, lookback_minutes=5)

        mock_discord.assert_not_called()

    def test_no_mentions(self):
        user = make_user(jira_display_name="Test User")
        issues = [{
            "key": "PROJ-1",
            "fields": {
                "summary": "Bug",
                "description": make_adf_text("Nothing relevant"),
                "updated": "2026-01-01T00:00:00+00:00",
                "creator": {"displayName": "Alice"},
            },
        }]
        search_fn, comments_fn = self._mock_jira(issues)

        with patch("poller.jira_search", side_effect=search_fn), \
             patch("poller.get_comments", side_effect=comments_fn), \
             patch("poller.send_discord") as mock_discord:
            seen = poll_once(set(), "acc-1", user, lookback_minutes=5)

        mock_discord.assert_not_called()
        assert len(seen) == 0

    def test_multi_user_isolation(self):
        """Two users polling the same issues get notifications to their own webhooks."""
        user_a = make_user(
            name="Alice",
            jira_display_name="Alice A",
            discord_webhook_url="http://hook-a",
        )
        user_b = make_user(
            name="Bob",
            jira_display_name="Bob B",
            discord_webhook_url="http://hook-b",
        )

        issues = [{
            "key": "PROJ-1",
            "fields": {
                "summary": "Meeting notes",
                "description": make_adf_text("Alice A and Bob B should review"),
                "updated": "2026-01-01T00:00:00+00:00",
                "creator": {"displayName": "Charlie"},
            },
        }]

        def fake_search(jql, user):
            return issues

        def fake_comments(key, user):
            return []

        with patch("poller.jira_search", side_effect=fake_search), \
             patch("poller.get_comments", side_effect=fake_comments), \
             patch("poller.send_discord") as mock_discord:
            seen_a = poll_once(set(), "acc-a", user_a, lookback_minutes=5)
            seen_b = poll_once(set(), "acc-b", user_b, lookback_minutes=5)

        assert mock_discord.call_count == 2
        # First call was for Alice, second for Bob
        call_a = mock_discord.call_args_list[0]
        call_b = mock_discord.call_args_list[1]
        assert call_a[0][1] == user_a  # user param
        assert call_b[0][1] == user_b
        assert len(seen_a) == 1
        assert len(seen_b) == 1
