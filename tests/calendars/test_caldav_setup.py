"""CalDAV guided setup: the credential is checked before it is saved and stays in the store."""

from pathlib import Path

import pytest
import yaml
from cal_keyring import MemoryKeyring
from cal_radicale import PASSWORD, USER, RadicaleServer

from chamberlain.connectors.caldav import setup
from chamberlain.connectors.caldav.config import CalDavBinding
from chamberlain.connectors.caldav.credentials import SERVICE, credential_name, password_source
from chamberlain.connectors.calendar_support.secrets import KeyringStore, SecretStore
from chamberlain.ports.clock import ManualClock
from chamberlain.schema.errors import ChamberlainError, ErrorCode

GOOD = "abcd-efgh-ijkl-mnop"
APPLE_ID = "me@example.test"


@pytest.fixture
def store(memory_keyring: MemoryKeyring) -> SecretStore:
    return KeyringStore()


def test_a_credential_is_saved_only_after_the_server_and_1_related_checks(
    store: SecretStore, memory_keyring: MemoryKeyring, tmp_path: Path
) -> None:
    # a_credential_is_saved_only_after_the_server_accepts_it
    seen: list[tuple[str, str]] = []

    def accepting(user: str, password: str) -> list[setup.Discovered]:
        seen.append((user, password))
        return [setup.Discovered("Home", "https://x/home/")]

    found = setup.save_credential(store, f" {APPLE_ID} ", GOOD, accepting)
    assert seen == [(APPLE_ID, GOOD)]
    assert [c.name for c in found] == ["Home"]
    assert store.get(SERVICE, credential_name(APPLE_ID)) == GOOD
    assert list(tmp_path.rglob("*")) == []

    def refusing(user: str, password: str) -> list[setup.Discovered]:
        raise ChamberlainError(ErrorCode.SOURCE_SIGN_IN_NEEDED, "refused")

    with pytest.raises(ChamberlainError):
        setup.save_credential(store, "other@example.test", GOOD, refusing)
    assert store.get(SERVICE, credential_name("other@example.test")) is None

    # normal_passwords_and_bad_addresses_never_reach_the_server
    def never(user: str, password: str) -> list[setup.Discovered]:
        raise AssertionError("the server must not be contacted")

    for user, password in (
        (APPLE_ID, "my normal password"),
        ("not-an-address", GOOD),
        (APPLE_ID, ""),
    ):
        with pytest.raises(ChamberlainError) as raised:
            setup.save_credential(store, user, password, never)
        assert raised.value.code is ErrorCode.REQUEST_INVALID


def test_forget_and_a_missing_credential_and_1_related_checks(store: SecretStore) -> None:
    # forget_and_a_missing_credential
    with pytest.raises(ChamberlainError) as missing:
        setup.saved_password(store, APPLE_ID)
    assert "credential" in missing.value.fix
    setup.save_credential(store, APPLE_ID, GOOD, lambda *a: [])
    assert setup.saved_password(store, APPLE_ID) == GOOD
    assert setup.forget(store, APPLE_ID)
    assert not setup.forget(store, APPLE_ID)

    # the_connector_asks_the_store_for_the_password_when_it_needs_it
    binding = CalDavBinding(
        url="https://x/y/", username=APPLE_ID, credential=credential_name(APPLE_ID)
    )
    lookup = password_source(store)
    with pytest.raises(ChamberlainError) as unsaved:
        lookup(binding)
    assert unsaved.value.code is ErrorCode.SOURCE_SIGN_IN_NEEDED
    store.set(SERVICE, binding.credential, GOOD)
    assert lookup(binding) == GOOD


def test_discovery_lists_the_event_calendars_of_a_real_server(radicale: RadicaleServer) -> None:
    url = radicale.new_calendar()
    found = setup.discover(radicale.base + "/", USER, PASSWORD)
    assert url in [c.url for c in found]
    with pytest.raises(ChamberlainError) as refused:
        setup.discover(radicale.base + "/", USER, "wrong")
    assert refused.value.code is ErrorCode.SOURCE_SIGN_IN_NEEDED


def test_registering_is_idempotent_keeps_a_backup_and_holds_no_password(tmp_path: Path) -> None:
    path = setup.profile_path(tmp_path)
    clock = ManualClock()
    home = setup.Discovered("Home", "https://p01-caldav.icloud.com/123/calendars/home/")
    assert setup.register(path, "calendar:icloud", APPLE_ID, home, clock)
    assert not setup.register(path, "calendar:icloud", APPLE_ID, home, clock)
    assert not list(path.parent.glob("*.bak-*"))
    work = setup.Discovered("Work", "https://p01-caldav.icloud.com/123/calendars/work/")
    clock.advance(seconds=5)
    assert setup.register(path, "calendar:work", APPLE_ID, work, clock)
    assert len(list(path.parent.glob("*.bak-*"))) == 1
    profile = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert profile["version"] == 1
    assert set(profile["sources"]) == {"calendar:icloud", "calendar:work"}
    assert GOOD not in path.read_text(encoding="utf-8")


def test_the_forget_command_removes_the_password(
    store: SecretStore,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CHAMBERLAIN_DATA", raising=False)
    setup.save_credential(store, APPLE_ID, GOOD, lambda *a: [])
    lines: list[str] = []
    assert (
        setup.main(["--data", str(tmp_path), "forget", "--apple-id", APPLE_ID], say=lines.append)
        == 0
    )
    assert lines == ["Removed the saved credential."]
    assert store.get(SERVICE, credential_name(APPLE_ID)) is None
    assert setup.main(["forget", "--apple-id", APPLE_ID]) == 2
    assert "data root" in capsys.readouterr().err
