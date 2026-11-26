"""Setup status resumes from private choices and current local evidence."""

import json
import sys
from io import StringIO
from pathlib import Path
from zipfile import ZipFile

import pytest

from chamberlain.connections import onboarding
from chamberlain.connections.load import profile_path
from chamberlain.connections.onboarding import SetupProvider, setup_select, setup_status
from chamberlain.connections.setup import provider_setup
from chamberlain.connectors.caldav.credentials import SERVICE as CALDAV_SERVICE
from chamberlain.connectors.caldav.credentials import credential_name
from chamberlain.connectors.google_calendar import auth
from chamberlain.connectors.thunderbird.client import BridgeClient
from chamberlain.connectors.thunderbird.native_host import RUNTIME_FILE
from chamberlain.connectors.thunderbird.protocol import PROTOCOL_VERSION, Welcome
from chamberlain.connectors.thunderbird.setup import addon_version
from chamberlain.schema.errors import ChamberlainError, ErrorCode

GOOGLE = "fixture@example.org"
APPLE = "fixture@icloud.com"
CLIENT_SECRET = "fixture-client-secret"
REFRESH = "fixture-refresh-token"
PASSWORD = "xxxx-yyyy-zzzz-aaaa"


class Secrets:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}

    def get(self, service: str, name: str) -> str | None:
        return self.values.get((service, name))

    def set(self, service: str, name: str, value: str) -> None:
        self.values[service, name] = value

    def delete(self, service: str, name: str) -> bool:
        return self.values.pop((service, name), None) is not None


def profile(root: Path, sources: dict[str, dict[str, str]]) -> None:
    path = profile_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 1, "sources": sources}), encoding="utf-8")


def test_missing_and_saved_selections_resume_without_a_profile(data_root: Path) -> None:
    before = setup_status(data_root)
    assert [step.id for step in before.waiting] == ["setup.selection"]
    assert not data_root.exists()
    selected = setup_select(data_root, (SetupProvider.THUNDERBIRD, SetupProvider.GOOGLE))
    file = data_root / "config" / "setup.json"
    snapshot = file.read_bytes()
    modified = file.stat().st_mtime_ns
    assert setup_select(data_root, selected.providers) == selected
    assert file.read_bytes() == snapshot
    assert file.stat().st_mtime_ns == modified

    after = setup_status(data_root, store=Secrets())
    assert after.providers == selected.providers
    assert [step.id for step in after.next] == ["thunderbird.package"]
    assert [step.id for step in after.waiting] == ["google.client"]
    assert not profile_path(data_root).exists()


def test_setup_dispatch_selects_providers_and_resumes_by_default(
    data_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secrets = Secrets()
    monkeypatch.setattr(onboarding, "default_store", lambda root: secrets)
    out = StringIO()
    assert provider_setup(None, [], data_root, out) == 0
    assert json.loads(out.getvalue())["waiting"][0]["id"] == "setup.selection"

    args = ["thunderbird", "google", "icloud", "--google-account", GOOGLE, "--apple-id", APPLE]
    selected = StringIO()
    assert provider_setup("select", args, data_root, selected) == 0
    assert json.loads(selected.getvalue()) == {
        "providers": ["thunderbird", "google", "icloud"],
        "google_account": GOOGLE,
        "apple_id": APPLE,
    }
    resumed = StringIO()
    assert provider_setup("status", [], data_root, resumed) == 0
    reply = json.loads(resumed.getvalue())
    assert [step["id"] for step in reply["next"]] == ["thunderbird.package"]
    assert [step["id"] for step in reply["waiting"]] == ["google.client", "icloud.credential"]


def test_bad_selection_and_profile_cannot_leak_offending_text(data_root: Path) -> None:
    with pytest.raises(ChamberlainError) as raised:
        setup_select(data_root, (SetupProvider.THUNDERBIRD,), apple_id=PASSWORD)
    assert raised.value.code is ErrorCode.REQUEST_INVALID
    assert PASSWORD not in str(raised.value)
    selected = (SetupProvider.ICLOUD,)
    setup_select(data_root, selected)
    profile_path(data_root).write_text(f"version: {PASSWORD}\n", encoding="utf-8")
    status = setup_status(data_root, store=Secrets())
    assert [step.id for step in status.next] == ["setup.profile"]
    assert PASSWORD not in status.model_dump_json()


def test_google_requires_saved_link_and_binding_for_the_chosen_account(data_root: Path) -> None:
    secrets = Secrets()
    setup_select(data_root, (SetupProvider.GOOGLE,), google_account=GOOGLE)
    auth.save_client(secrets, "fixture.apps.googleusercontent.com", CLIENT_SECRET)
    auth.save_refresh_token(secrets, "different@example.org", REFRESH, auth.SCOPE)
    pending = setup_status(data_root, store=secrets)
    assert [step.id for step in pending.done] == ["google.client"]
    assert [step.id for step in pending.waiting] == ["google.consent"]

    auth.save_refresh_token(secrets, GOOGLE, REFRESH, auth.SCOPE)
    profile(
        data_root,
        {
            "calendar:other": {
                "kind": "google",
                "account": "different@example.org",
                "calendar_id": "primary",
            }
        },
    )
    assert [step.id for step in setup_status(data_root, store=secrets).waiting] == [
        "google.binding"
    ]
    profile(
        data_root,
        {"calendar:test": {"kind": "google", "account": GOOGLE, "calendar_id": "primary"}},
    )
    ready = setup_status(data_root, store=secrets)
    assert [step.id for step in ready.done] == ["google.client", "google.consent", "google.binding"]
    assert not ready.next
    assert not ready.waiting
    status_file = data_root / "config" / "setup.json"
    for document in (ready.model_dump_json(), status_file.read_text(encoding="utf-8")):
        assert CLIENT_SECRET not in document
        assert REFRESH not in document

    # A retained account index alone does not prove its refresh token is still readable.
    auth.save_refresh_token(secrets, GOOGLE, "", auth.SCOPE)
    assert [step.id for step in setup_status(data_root, store=secrets).waiting] == [
        "google.consent"
    ]


def test_icloud_reports_password_then_binding_without_treating_other_caldav_as_icloud(
    data_root: Path,
) -> None:
    secrets = Secrets()
    setup_select(data_root, (SetupProvider.ICLOUD,))
    assert [step.id for step in setup_status(data_root, store=secrets).waiting] == [
        "icloud.account"
    ]
    setup_select(data_root, (SetupProvider.ICLOUD,), apple_id=APPLE)
    assert [step.id for step in setup_status(data_root, store=secrets).waiting] == [
        "icloud.credential"
    ]
    name = credential_name(APPLE)
    secrets.set(CALDAV_SERVICE, name, PASSWORD)
    fields = {"kind": "caldav", "username": APPLE, "credential": name}
    profile(data_root, {"calendar:other": {**fields, "url": "https://example.org/caldav/"}})
    assert [step.id for step in setup_status(data_root, store=secrets).waiting] == [
        "icloud.binding"
    ]
    profile(
        data_root,
        {"calendar:test": {**fields, "url": "https://p01-caldav.icloud.com/123/calendars/home/"}},
    )
    status = setup_status(data_root, store=secrets)
    assert [step.id for step in status.done] == ["icloud.credential", "icloud.binding"]
    assert not status.waiting
    assert PASSWORD not in status.model_dump_json()


def test_thunderbird_checks_package_handshake_version_and_binding(
    data_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    setup_select(data_root, (SetupProvider.THUNDERBIRD,))
    bridge = data_root / "bridge"
    bridge.mkdir()
    with ZipFile(bridge / f"chamberlain-bridge-{addon_version()}.xpi", "w") as package:
        package.writestr("manifest.json", "{}")
    launcher = "native-host.cmd" if sys.platform == "win32" else "native-host"
    (bridge / launcher).touch()
    staged = setup_status(data_root)
    assert [step.id for step in staged.done] == ["thunderbird.package"]
    assert [step.id for step in staged.waiting] == ["thunderbird.install"]

    (bridge / RUNTIME_FILE).touch()
    version = "0.0.0"

    def hello(self: object) -> Welcome:
        return Welcome(
            v=PROTOCOL_VERSION,
            id="request-0001",
            host_version="2.0",
            addon_version=version,
            calendar_available=True,
        )

    monkeypatch.setattr(BridgeClient, "hello", hello)
    assert [step.id for step in setup_status(data_root).next] == ["thunderbird.update"]
    version = addon_version()
    assert [step.id for step in setup_status(data_root).waiting] == ["thunderbird.binding"]
    profile(data_root, {"calendar:test": {"kind": "owl", "calendar_id": "fixture"}})
    ready = setup_status(data_root)
    assert [step.id for step in ready.done] == ["thunderbird.bridge", "thunderbird.binding"]
    assert not ready.waiting
