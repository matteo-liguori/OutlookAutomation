"""Google OAuth and guided setup: PKCE loopback flow, credential storage, no secret leaks."""

import json
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from pathlib import Path

import keyring
import pytest
from cal_keyring import MemoryKeyring
from keyring.backends import fail

from chamberlain.connectors.calendar_support.http import HttpReply
from chamberlain.connectors.calendar_support.secrets import (
    FileStore,
    KeyringStore,
    SecretStore,
    default_store,
)
from chamberlain.connectors.google_calendar import auth, setup
from chamberlain.ports.clock import ManualClock
from chamberlain.schema.errors import ChamberlainError, ErrorCode

CLIENT_ID = "fixture-123.apps.googleusercontent.com"
CLIENT_SECRET = "client-secret-DO-NOT-LEAK"
REFRESH = "refresh-token-DO-NOT-LEAK"


@pytest.fixture
def store(memory_keyring: MemoryKeyring) -> SecretStore:
    return KeyringStore()


def files_under(root: Path) -> list[Path]:
    return [p for p in root.rglob("*") if p.is_file()]


def test_s17_the_client_is_saved_in_the_credential_store_and_validated(
    store: SecretStore, memory_keyring: MemoryKeyring, tmp_path: Path
) -> None:
    auth.save_client(store, f" {CLIENT_ID} ", f" {CLIENT_SECRET} ")
    assert auth.load_client(store) == (CLIENT_ID, CLIENT_SECRET)
    assert any(CLIENT_SECRET in value for value in memory_keyring.items.values())
    assert files_under(tmp_path) == []
    for bad in (("not-a-client-id", "secret"), (CLIENT_ID, " ")):
        with pytest.raises(ChamberlainError) as raised:
            auth.save_client(store, *bad)
        assert raised.value.code is ErrorCode.REQUEST_INVALID


def test_s17_a_downloaded_client_must_be_a_desktop_app(tmp_path: Path) -> None:
    good, web = tmp_path / "good.json", tmp_path / "web.json"
    good.write_text(json.dumps({"installed": {"client_id": CLIENT_ID, "client_secret": "s"}}))
    web.write_text(json.dumps({"web": {"client_id": CLIENT_ID, "client_secret": "s"}}))
    assert setup.read_downloaded(good) == (CLIENT_ID, "s")
    with pytest.raises(ChamberlainError) as raised:
        setup.read_downloaded(web)
    assert "Desktop app" in raised.value.message


def get(url: str) -> int:
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            return response.status
    except urllib.error.HTTPError as error:
        error.close()
        return error.code


def run_flow(
    store: SecretStore, *, stray: bool = True, tokens: dict[str, object] | None = None
) -> tuple[list[Mapping[str, str]], list[str]]:
    """Run `authorize`, playing the browser: one stray request, then the real redirect."""
    posted: list[Mapping[str, str]] = []
    urls: list[str] = []

    def post(url: str, fields: Mapping[str, str]) -> dict[str, object]:
        posted.append(fields)
        return tokens if tokens is not None else {"refresh_token": REFRESH, "scope": "calendar"}

    def on_url(url: str) -> None:
        urls.append(url)
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        redirect = query["redirect_uri"][0]
        state = query["state"][0]

        def browse() -> None:
            if stray:
                assert get(redirect + "/favicon.ico") == 400
            assert get(f"{redirect}/?code=abc&state={state}") == 200

        thread = threading.Thread(target=browse)
        thread.start()
        thread.join(10)

    auth.authorize(store, "Me@Example.test", on_url, timeout=15, post=post)
    return posted, urls


def test_s17_the_loopback_flow_uses_pkce_ignores_strays_and_saves_the_token(
    store: SecretStore, memory_keyring: MemoryKeyring, tmp_path: Path
) -> None:
    auth.save_client(store, CLIENT_ID, CLIENT_SECRET)
    posted, urls = run_flow(store)
    query = urllib.parse.parse_qs(urllib.parse.urlparse(urls[0]).query)
    assert query["code_challenge_method"] == ["S256"]
    assert query["code_challenge"] == [auth.pkce_challenge(posted[0]["code_verifier"])]
    assert posted[0]["grant_type"] == "authorization_code"
    assert auth.accounts(store) == ["me@example.test"]
    assert any(REFRESH in value for value in memory_keyring.items.values())
    assert files_under(tmp_path) == []


def test_a_refusal_or_missing_refresh_token_does_not_save_anything(store: SecretStore) -> None:
    auth.save_client(store, CLIENT_ID, CLIENT_SECRET)
    with pytest.raises(auth.AuthorisationFailed):
        run_flow(store, tokens={"access_token": "only"})
    assert auth.accounts(store) == []


def test_p17_access_tokens_are_cached_until_a_minute_before_expiry(store: SecretStore) -> None:
    auth.save_client(store, CLIENT_ID, CLIENT_SECRET)
    auth.save_refresh_token(store, "me@example.test", REFRESH, "calendar")
    clock = ManualClock()
    calls: list[Mapping[str, str]] = []

    def post(url: str, fields: Mapping[str, str]) -> dict[str, object]:
        calls.append(fields)
        return {"access_token": f"access-{len(calls)}", "expires_in": 3600}

    tokens = auth.AccessTokens(store, clock, post)
    assert tokens("me@example.test") == "access-1"
    clock.advance(minutes=30)
    assert tokens("ME@example.test") == "access-1"
    clock.advance(minutes=30)
    assert tokens("me@example.test") == "access-2"
    assert calls[0]["refresh_token"] == REFRESH


def test_sign_in_is_asked_for_when_the_account_is_not_linked_or_refused(store: SecretStore) -> None:
    auth.save_client(store, CLIENT_ID, CLIENT_SECRET)
    tokens = auth.AccessTokens(store, ManualClock())
    with pytest.raises(ChamberlainError) as unlinked:
        tokens("nobody@example.test")
    assert unlinked.value.code is ErrorCode.SOURCE_SIGN_IN_NEEDED

    class Refusing:
        def send(self, method: str, url: str, **kwargs: object) -> HttpReply:
            return HttpReply(400, {}, b'{"error":"invalid_grant"}')

    with pytest.raises(ChamberlainError) as refused:
        auth.post_form(auth.TOKEN_URI, {"client_secret": CLIENT_SECRET}, Refusing())
    assert refused.value.code is ErrorCode.SOURCE_SIGN_IN_NEEDED
    assert CLIENT_SECRET not in refused.value.detail + refused.value.message


def test_link_prints_the_address_and_the_binding_ids(store: SecretStore) -> None:
    lines: list[str] = []
    opened: list[str] = []

    def authorize(s: SecretStore, email: str, on_url: Callable[[str], None]) -> None:
        on_url("https://example.invalid/auth")

    def lister(s: SecretStore, email: str) -> list[dict[str, str]]:
        return [{"summary": "Primary", "id": "me@example.test", "role": "owner"}]

    calendars = setup.link(
        store,
        "me@example.test",
        say=lines.append,
        authorize=authorize,
        lister=lister,
        open_browser=opened.append,
    )
    assert calendars[0]["id"] == "me@example.test"
    assert "https://example.invalid/auth" in lines
    assert opened == ["https://example.invalid/auth"]
    assert any("calendar_id: me@example.test" in line for line in lines)


def test_check_reports_each_account_and_how_to_relink_a_lapsed_one(store: SecretStore) -> None:
    lines: list[str] = []
    assert not setup.check(store, say=lines.append)
    assert "client" in lines[-1]
    auth.save_client(store, CLIENT_ID, CLIENT_SECRET)
    assert not setup.check(store, say=lines.append)
    assert "link" in lines[-1]
    for email in ("a@example.test", "b@example.test"):
        auth.save_refresh_token(store, email, REFRESH, "calendar")

    def lister(s: SecretStore, email: str) -> list[dict[str, str]]:
        if email.startswith("b@"):
            raise ChamberlainError(ErrorCode.SOURCE_SIGN_IN_NEEDED, "refused")
        return [{"summary": "x", "id": "x", "role": "owner"}]

    lines.clear()
    assert not setup.check(store, say=lines.append, lister=lister)
    assert "a@example.test: ok (1 calendars)" in lines
    assert any(
        line.startswith("b@example.test: needs re-linking") and "link b@example.test" in line
        for line in lines
    )


def test_pages_are_filled_in_with_the_app_name(tmp_path: Path) -> None:
    written = setup.write_pages(
        tmp_path / "site", "My Calendar Tool", "https://example.invalid/src"
    )
    assert sorted(p.name for p in written) == ["index.html", "privacy.html"]
    for path in written:
        text = path.read_text(encoding="utf-8")
        assert "My Calendar Tool" in text
        assert "{{" not in text
    assert "https://example.invalid/src" in written[0].read_text(encoding="utf-8")
    bare = setup.write_pages(tmp_path / "bare", "Tool")
    assert "Source code" not in bare[0].read_text(encoding="utf-8")


def test_s17_without_a_credential_store_a_restricted_file_is_used(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    previous = keyring.get_keyring()
    keyring.set_keyring(fail.Keyring())
    try:
        fallback = default_store(tmp_path)
    finally:
        keyring.set_keyring(previous)
    assert isinstance(fallback, FileStore)
    auth.save_client(fallback, CLIENT_ID, CLIENT_SECRET)
    assert auth.load_client(fallback) == (CLIENT_ID, CLIENT_SECRET)
    (saved,) = files_under(tmp_path / "secrets")
    assert saved.read_text(encoding="utf-8").count(CLIENT_SECRET) == 1
    assert not list(saved.parent.glob("*.tmp"))
    captured = capsys.readouterr()
    assert CLIENT_SECRET not in captured.out + captured.err
    assert fallback.delete(auth.SERVICE, auth.CLIENT_NAME)
    assert not fallback.delete(auth.SERVICE, auth.CLIENT_NAME)


def test_s17_with_a_credential_store_no_secret_file_is_made(
    memory_keyring: MemoryKeyring, tmp_path: Path
) -> None:
    assert isinstance(default_store(tmp_path), KeyringStore)
    assert files_under(tmp_path) == []
