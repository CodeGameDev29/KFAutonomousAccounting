"""Tests for the Gmail OAuth flow.

Exercises the GmailGatherer OAuth methods against the real implementation in
core/gather/gmail.py.
"""
import os
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlparse

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_gatherer():
    """Create a GmailGatherer with simple mocks for required __init__ args."""
    from core.gather.gmail import GmailGatherer

    creds_store = MagicMock()
    db = MagicMock()
    # Use a temp-style path that won't conflict
    output_dir = os.path.join(os.environ.get("TEMP", "/tmp"), "test_gmail_gather")
    return GmailGatherer(credentials_store=creds_store, db=db, output_dir=output_dir)


# ---------------------------------------------------------------------------
# start_oauth_flow returns a valid Google OAuth URL
# ---------------------------------------------------------------------------

class TestStartOAuthFlowReturnsURL:
    """start_oauth_flow(state) returns a URL starting with
    https://accounts.google.com/o/oauth2/auth when GOOGLE_CLIENT_ID and
    GOOGLE_CLIENT_SECRET are set.  The URL must contain redirect_uri= (NOT
    urn:ietf:wg:oauth:2.0:oob), gmail.readonly scope, access_type=offline,
    and the provided state parameter.
    """

    @patch.dict(os.environ, {
        "GOOGLE_CLIENT_ID": "test-client-id.apps.googleusercontent.com",
        "GOOGLE_CLIENT_SECRET": "test-client-secret",
    })
    def test_returns_google_oauth_url(self):
        gatherer = _make_gatherer()
        url = gatherer.start_oauth_flow(state="abc123")

        assert url is not None, "Expected a URL, got None"
        assert url.startswith("https://accounts.google.com/o/oauth2/auth"), (
            f"URL does not start with expected prefix: {url}"
        )

    @patch.dict(os.environ, {
        "GOOGLE_CLIENT_ID": "test-client-id.apps.googleusercontent.com",
        "GOOGLE_CLIENT_SECRET": "test-client-secret",
    })
    def test_url_contains_redirect_uri_not_oob(self):
        gatherer = _make_gatherer()
        url = gatherer.start_oauth_flow(state="abc123")

        assert url is not None
        parsed = urlparse(url)
        params = parse_qs(parsed.query)
        assert "redirect_uri" in params, "URL missing redirect_uri parameter"
        redirect_uri = params["redirect_uri"][0]
        assert redirect_uri != "urn:ietf:wg:oauth:2.0:oob", (
            "redirect_uri must NOT be the OOB URN"
        )

    @patch.dict(os.environ, {
        "GOOGLE_CLIENT_ID": "test-client-id.apps.googleusercontent.com",
        "GOOGLE_CLIENT_SECRET": "test-client-secret",
    })
    def test_url_contains_gmail_readonly_scope(self):
        gatherer = _make_gatherer()
        url = gatherer.start_oauth_flow(state="abc123")

        assert url is not None
        parsed = urlparse(url)
        params = parse_qs(parsed.query)
        assert "scope" in params, "URL missing scope parameter"
        scope_value = params["scope"][0]
        assert "gmail.readonly" in scope_value, (
            f"scope does not contain gmail.readonly: {scope_value}"
        )

    @patch.dict(os.environ, {
        "GOOGLE_CLIENT_ID": "test-client-id.apps.googleusercontent.com",
        "GOOGLE_CLIENT_SECRET": "test-client-secret",
    })
    def test_url_contains_access_type_offline(self):
        gatherer = _make_gatherer()
        url = gatherer.start_oauth_flow(state="abc123")

        assert url is not None
        parsed = urlparse(url)
        params = parse_qs(parsed.query)
        assert "access_type" in params, "URL missing access_type parameter"
        assert params["access_type"][0] == "offline", (
            f"access_type is not offline: {params['access_type'][0]}"
        )

    @patch.dict(os.environ, {
        "GOOGLE_CLIENT_ID": "test-client-id.apps.googleusercontent.com",
        "GOOGLE_CLIENT_SECRET": "test-client-secret",
    })
    def test_url_contains_provided_state(self):
        gatherer = _make_gatherer()
        state_value = "unique-state-token-xyz"
        url = gatherer.start_oauth_flow(state=state_value)

        assert url is not None
        parsed = urlparse(url)
        params = parse_qs(parsed.query)
        assert "state" in params, "URL missing state parameter"
        assert params["state"][0] == state_value, (
            f"state does not match: expected {state_value}, got {params['state'][0]}"
        )


# ---------------------------------------------------------------------------
# start_oauth_flow returns None when env vars not set
# ---------------------------------------------------------------------------

class TestStartOAuthFlowNoEnvVars:
    """start_oauth_flow(state) returns None when GOOGLE_CLIENT_ID and
    GOOGLE_CLIENT_SECRET are NOT set.
    """

    @patch.dict(os.environ, {}, clear=True)
    def test_returns_none_when_env_vars_missing(self):
        # Explicitly remove any env vars that might leak in
        os.environ.pop("GOOGLE_CLIENT_ID", None)
        os.environ.pop("GOOGLE_CLIENT_SECRET", None)

        gatherer = _make_gatherer()
        url = gatherer.start_oauth_flow(state="some-state")
        assert url is None, f"Expected None when env vars not set, got: {url}"

    @patch.dict(os.environ, {"GOOGLE_CLIENT_ID": "", "GOOGLE_CLIENT_SECRET": ""})
    def test_returns_none_when_env_vars_empty(self):
        gatherer = _make_gatherer()
        url = gatherer.start_oauth_flow(state="some-state")
        assert url is None, f"Expected None when env vars are empty strings, got: {url}"

    @patch.dict(os.environ, {
        "GOOGLE_CLIENT_ID": "has-id",
        "GOOGLE_CLIENT_SECRET": "",
    })
    def test_returns_none_when_only_secret_missing(self):
        gatherer = _make_gatherer()
        url = gatherer.start_oauth_flow(state="some-state")
        assert url is None, "Expected None when GOOGLE_CLIENT_SECRET is empty"


# ---------------------------------------------------------------------------
# exchange_code_for_tokens returns credentials on success
# ---------------------------------------------------------------------------

class TestExchangeCodeSuccess:
    """exchange_code_for_tokens(code) is a classmethod.  When called with a
    valid code (mock the Flow.fetch_token), returns a truthy credentials
    object.
    """

    @patch.dict(os.environ, {
        "GOOGLE_CLIENT_ID": "test-client-id.apps.googleusercontent.com",
        "GOOGLE_CLIENT_SECRET": "test-client-secret",
    })
    @patch("google_auth_oauthlib.flow.Flow.from_client_config")
    def test_returns_truthy_credentials(self, mock_from_config):
        from core.gather.gmail import GmailGatherer

        mock_flow = MagicMock()
        mock_credentials = MagicMock()
        mock_credentials.token = "access-token-123"
        mock_credentials.refresh_token = "refresh-token-456"
        mock_flow.credentials = mock_credentials
        mock_flow.fetch_token = MagicMock()  # succeeds without raising
        mock_from_config.return_value = mock_flow

        result = GmailGatherer.exchange_code_for_tokens(
            code="4/test-auth-code",
            redirect_uri="http://localhost:8080/api/onboarding/gmail/callback",
        )

        assert result is not None, "Expected truthy credentials, got None"
        assert result, "Expected truthy credentials object"
        mock_flow.fetch_token.assert_called_once_with(code="4/test-auth-code")


# ---------------------------------------------------------------------------
# exchange_code_for_tokens returns None on token exchange failure
# ---------------------------------------------------------------------------

class TestExchangeCodeFailure:
    """exchange_code_for_tokens(code) returns None when the token exchange
    fails (mock Flow.fetch_token to raise).
    """

    @patch.dict(os.environ, {
        "GOOGLE_CLIENT_ID": "test-client-id.apps.googleusercontent.com",
        "GOOGLE_CLIENT_SECRET": "test-client-secret",
    })
    @patch("google_auth_oauthlib.flow.Flow.from_client_config")
    def test_returns_none_on_fetch_token_exception(self, mock_from_config):
        from core.gather.gmail import GmailGatherer

        mock_flow = MagicMock()
        mock_flow.fetch_token.side_effect = Exception("Token exchange failed: invalid_grant")
        mock_from_config.return_value = mock_flow

        result = GmailGatherer.exchange_code_for_tokens(
            code="4/bad-auth-code",
            redirect_uri="http://localhost:8080/api/onboarding/gmail/callback",
        )

        assert result is None, f"Expected None on token failure, got: {result}"

    @patch.dict(os.environ, {
        "GOOGLE_CLIENT_ID": "test-client-id.apps.googleusercontent.com",
        "GOOGLE_CLIENT_SECRET": "test-client-secret",
    })
    @patch("google_auth_oauthlib.flow.Flow.from_client_config")
    def test_returns_none_on_network_error(self, mock_from_config):
        from core.gather.gmail import GmailGatherer

        mock_flow = MagicMock()
        mock_flow.fetch_token.side_effect = ConnectionError("Network unreachable")
        mock_from_config.return_value = mock_flow

        result = GmailGatherer.exchange_code_for_tokens(
            code="4/some-code",
            redirect_uri="http://localhost:8080/api/onboarding/gmail/callback",
        )

        assert result is None, f"Expected None on network error, got: {result}"


# ---------------------------------------------------------------------------
# exchange_code_for_tokens returns None when env vars not set
# ---------------------------------------------------------------------------

class TestExchangeCodeNoEnvVars:
    """exchange_code_for_tokens(code) returns None when GOOGLE_CLIENT_ID
    and GOOGLE_CLIENT_SECRET env vars are not set.
    """

    @patch.dict(os.environ, {}, clear=True)
    def test_returns_none_when_env_vars_missing(self):
        os.environ.pop("GOOGLE_CLIENT_ID", None)
        os.environ.pop("GOOGLE_CLIENT_SECRET", None)

        from core.gather.gmail import GmailGatherer

        result = GmailGatherer.exchange_code_for_tokens(code="4/some-code")
        assert result is None, f"Expected None when env vars not set, got: {result}"

    @patch.dict(os.environ, {"GOOGLE_CLIENT_ID": "", "GOOGLE_CLIENT_SECRET": ""})
    def test_returns_none_when_env_vars_empty(self):
        from core.gather.gmail import GmailGatherer

        result = GmailGatherer.exchange_code_for_tokens(code="4/some-code")
        assert result is None, f"Expected None when env vars are empty, got: {result}"
