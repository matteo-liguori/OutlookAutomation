"""The unified setup command delegates to the providers' existing entry points."""

import json
import sys
from io import StringIO
from pathlib import Path

import pytest

from chamberlain.connections.setup import provider_setup
from chamberlain.connectors.caldav import setup as icloud_setup
from chamberlain.connectors.calendar_support.secrets import FileStore
from chamberlain.connectors.google_calendar import auth
from chamberlain.connectors.google_calendar import setup as google_setup
from chamberlain.connectors.thunderbird import setup as thunderbird_setup


@pytest.mark.parametrize("explicit_bridge", [False, True])
def test_thunderbird_setup_keeps_native_host_options(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, explicit_bridge: bool
) -> None:
    data = tmp_path / "data"
    manifests = tmp_path / "native-messaging"
    replace = tmp_path / "old-host"
    bridge = tmp_path / "explicit-bridge" if explicit_bridge else data / "bridge"
    argv = ["--manifest-dir", str(manifests), "--replace-host-path", str(replace)]
    if explicit_bridge:
        argv.extend(["--bridge-dir", str(bridge)])

    def prepare(
        bridge_dir: Path, manifest_dir: Path, *, replace_host_path: Path | None = None
    ) -> dict[str, str]:
        assert bridge_dir == bridge
        assert manifest_dir == manifests
        assert replace_host_path == replace
        return {"manifest": str(manifests)}

    monkeypatch.setattr(thunderbird_setup, "prepare", prepare)
    out = StringIO()
    assert provider_setup("thunderbird", argv, data, out) == 0
    assert json.loads(out.getvalue()) == {"manifest": str(manifests)}


def test_google_setup_preserves_terminal_credential_entry(
    data_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = StringIO()
    store = FileStore(data_root)
    client_id = "fixture.apps.googleusercontent.com"
    client_secret = "fixture-secret"

    def select_store(data: str) -> FileStore:
        assert Path(data) == data_root
        return store

    def ask(prompt: str) -> str:
        assert sys.stdout is not out
        assert prompt == "Client ID: "
        return client_id

    def hidden(prompt: str) -> str:
        assert sys.stdout is not out
        assert prompt == "Client secret (hidden): "
        return client_secret

    monkeypatch.setattr(google_setup, "_store", select_store)
    monkeypatch.setattr("builtins.input", ask)
    monkeypatch.setattr("getpass.getpass", hidden)
    assert provider_setup("google", ["client"], data_root, out) == 0
    assert auth.load_client(store) == (client_id, client_secret)
    assert out.getvalue() == "Saved the client to the credential store.\n"


def test_icloud_setup_forwards_the_subcommand_and_status(
    data_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def main(argv: list[str], *, say: icloud_setup.Say) -> int:
        assert argv == ["--data", str(data_root), "check", "--apple-id", "owner@example.org"]
        say("iCloud response")
        return 1

    monkeypatch.setattr(icloud_setup, "main", main)
    out = StringIO()
    args = ["check", "--apple-id", "owner@example.org"]
    assert provider_setup("icloud", args, data_root, out) == 1
    assert out.getvalue() == "iCloud response\n"


def test_provider_help_returns_success_without_running_setup(data_root: Path) -> None:
    out = StringIO()
    assert provider_setup("thunderbird", ["--help"], data_root, out) == 0
    assert "--manifest-dir" in out.getvalue()
