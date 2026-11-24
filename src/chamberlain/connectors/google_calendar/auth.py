"""Google OAuth for the owner's own Cloud project: installed-app loopback flow with PKCE.

Contract: docs/contracts/09-storage.md (S-17). The client id and secret and each
account's refresh token live in the credential store, never in a file or a log.
A stray request to the loopback port (a prefetch, a probe) never aborts a sign-in.
"""

import base64
import hashlib
import json
import secrets
import threading
import urllib.parse
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Final, cast

from chamberlain.connectors.calendar_support.http import (
    HttpTransport,
    TransportFailure,
    UrllibTransport,
)
from chamberlain.connectors.calendar_support.secrets import SecretStore
from chamberlain.ports.clock import Clock
from chamberlain.schema.errors import ChamberlainError, ErrorCode

SCOPE: Final = "https://www.googleapis.com/auth/calendar"
AUTH_URI: Final = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URI: Final = "https://oauth2.googleapis.com/token"
SERVICE: Final = "chamberlain.google"
CLIENT_NAME: Final = "client"
ACCOUNTS_NAME: Final = "accounts"
_REFRESH_MARGIN = timedelta(seconds=60)

TokenPost = Callable[[str, Mapping[str, str]], dict[str, object]]
"""Posts form fields to a token endpoint and returns the JSON answer."""


class AuthorisationFailed(Exception):
    """The sign-in did not complete (refused, timed out, no refresh token)."""


def _client_fix() -> str:
    return "Run the Google calendar setup: client, then link."


def load_client(store: SecretStore) -> tuple[str, str]:
    raw = store.get(SERVICE, CLIENT_NAME)
    if raw is None:
        raise ChamberlainError(
            ErrorCode.SOURCE_NOT_CONFIGURED, "No Google client is saved.", fix=_client_fix()
        )
    data = json.loads(raw)
    return str(data["client_id"]), str(data["client_secret"])


def save_client(store: SecretStore, client_id: str, client_secret: str) -> None:
    client_id, client_secret = client_id.strip(), client_secret.strip()
    if not client_id.endswith(".apps.googleusercontent.com"):
        raise ChamberlainError(
            ErrorCode.REQUEST_INVALID,
            "The client id should end with .apps.googleusercontent.com.",
            field="client_id",
        )
    if not client_secret:
        raise ChamberlainError(
            ErrorCode.REQUEST_INVALID, "The client secret is missing.", field="client_secret"
        )
    store.set(
        SERVICE, CLIENT_NAME, json.dumps({"client_id": client_id, "client_secret": client_secret})
    )


def accounts(store: SecretStore) -> list[str]:
    raw = store.get(SERVICE, ACCOUNTS_NAME)
    return [str(a) for a in json.loads(raw)] if raw else []


def _token_name(email: str) -> str:
    return f"token:{email.strip().lower()}"


def save_refresh_token(store: SecretStore, email: str, refresh_token: str, scope: str) -> None:
    email = email.strip().lower()
    store.set(
        SERVICE, _token_name(email), json.dumps({"refresh_token": refresh_token, "scope": scope})
    )
    known = accounts(store)
    if email not in known:
        store.set(SERVICE, ACCOUNTS_NAME, json.dumps([*known, email]))


def forget_account(store: SecretStore, email: str) -> bool:
    email = email.strip().lower()
    removed = store.delete(SERVICE, _token_name(email))
    store.set(SERVICE, ACCOUNTS_NAME, json.dumps([a for a in accounts(store) if a != email]))
    return removed


def post_form(
    url: str, fields: Mapping[str, str], transport: HttpTransport | None = None
) -> dict[str, object]:
    """One token request. A 4xx answer is a refusal; anything else wrong is unavailable."""
    body = urllib.parse.urlencode(dict(fields)).encode()
    try:
        reply = (transport or UrllibTransport()).send(
            "POST",
            url,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            body=body,
            timeout=30,
        )
    except TransportFailure as error:
        raise ChamberlainError(
            ErrorCode.SOURCE_UNAVAILABLE, "Google did not answer.", detail=str(error)
        ) from None
    if 400 <= reply.status < 500:
        raise ChamberlainError(
            ErrorCode.SOURCE_SIGN_IN_NEEDED,
            "Google refused the saved sign-in.",
            fix="Link the Google account again.",
            detail=reply.body[:200].decode("utf-8", "replace"),
        )
    if reply.status != 200:
        raise ChamberlainError(
            ErrorCode.SOURCE_UNAVAILABLE,
            "Google could not issue a token.",
            detail=str(reply.status),
        )
    data: object = json.loads(reply.body)
    return cast(dict[str, object], data) if isinstance(data, dict) else {}


def pkce_challenge(verifier: str) -> str:
    return (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    )


def authorization_url(client_id: str, email: str, port: int, verifier: str, state: str) -> str:
    query = {
        "client_id": client_id,
        "redirect_uri": f"http://127.0.0.1:{port}",
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",
        "prompt": "consent",
        "login_hint": email,
        "state": state,
        "code_challenge": pkce_challenge(verifier),
        "code_challenge_method": "S256",
    }
    return AUTH_URI + "?" + urllib.parse.urlencode(query)


def authorize(
    store: SecretStore,
    email: str,
    on_url: Callable[[str], None],
    *,
    timeout: float = 600,
    post: TokenPost = post_form,
) -> None:
    """Run the loopback flow and save the refresh token. `on_url` shows the owner the address."""
    client_id, client_secret = load_client(store)
    verifier = secrets.token_urlsafe(64)
    state = secrets.token_urlsafe(16)
    result: dict[str, str] = {}
    done = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            code, message = 400, b"Unexpected request."
            if query.get("state", [""])[0] == state:
                if "code" in query:
                    result["code"] = query["code"][0]
                    code, message = 200, b"Authorised. You can close this tab."
                    done.set()
                elif "error" in query:
                    result["error"] = query["error"][0]
                    code, message = 200, b"Not authorised."
                    done.set()
            self.send_response(code)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(message)

        def log_message(self, format: str, *args: object) -> None:
            return None

    server = HTTPServer(("127.0.0.1", 0), Handler)
    port = int(server.server_address[1])
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        on_url(authorization_url(client_id, email, port, verifier, state))
        done.wait(timeout)
    finally:
        server.shutdown()
        server.server_close()
    if "code" not in result:
        raise AuthorisationFailed(result.get("error", "timed out"))
    tokens = post(
        TOKEN_URI,
        {
            "code": result["code"],
            "client_id": client_id,
            "client_secret": client_secret,
            "code_verifier": verifier,
            "redirect_uri": f"http://127.0.0.1:{port}",
            "grant_type": "authorization_code",
        },
    )
    refresh = tokens.get("refresh_token")
    if not isinstance(refresh, str):
        raise AuthorisationFailed("Google returned no refresh token")
    save_refresh_token(store, email, refresh, str(tokens.get("scope", "")))


class AccessTokens:
    """Short-lived access tokens, cached until a minute before they expire."""

    def __init__(self, store: SecretStore, clock: Clock, post: TokenPost = post_form) -> None:
        self._store = store
        self._clock = clock
        self._post = post
        self._cache: dict[str, tuple[str, datetime]] = {}

    def __call__(self, email: str) -> str:
        email = email.strip().lower()
        cached = self._cache.get(email)
        now = self._clock.now()
        if cached and cached[1] > now + _REFRESH_MARGIN:
            return cached[0]
        raw = self._store.get(SERVICE, _token_name(email))
        if raw is None:
            raise ChamberlainError(
                ErrorCode.SOURCE_SIGN_IN_NEEDED,
                "This Google account is not linked.",
                fix=f"Link {email} in the Google calendar setup.",
            )
        client_id, client_secret = load_client(self._store)
        tokens = self._post(
            TOKEN_URI,
            {
                "refresh_token": json.loads(raw)["refresh_token"],
                "client_id": client_id,
                "client_secret": client_secret,
                "grant_type": "refresh_token",
            },
        )
        access = str(tokens["access_token"])
        lifetime = tokens.get("expires_in", 3600)
        expires = now + timedelta(
            seconds=int(lifetime) if isinstance(lifetime, int | str) else 3600
        )
        self._cache[email] = (access, expires)
        return access
