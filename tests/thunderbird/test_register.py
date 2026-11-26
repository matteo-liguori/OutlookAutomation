"""`setup thunderbird --register` and `thunderbird close|start|status`, all injected."""

from pathlib import Path

import pytest
from test_update import FakeDesktop

from chamberlain.connectors.thunderbird.setup import REGISTRY_KEY, register_host
from chamberlain.connectors.thunderbird.update import control
from chamberlain.schema.errors import ChamberlainError


class FakeRegistry:
    def __init__(self, value: str | None = None, *, takes: bool = True) -> None:
        self.values: dict[str, str] = {} if value is None else {REGISTRY_KEY: value}
        self.takes = takes
        self.writes = 0

    def read(self, key: str) -> str | None:
        return self.values.get(key)

    def write(self, key: str, value: str) -> None:
        self.writes += 1
        if self.takes:
            self.values[key] = value


MANIFEST = Path("C:/data/bridge/chamberlain_thunderbird_bridge.json")


def test_windows_dry_run_writes_no_registry_value() -> None:
    registry = FakeRegistry()
    lines = register_host(MANIFEST, apply=False, system="win32", registry=registry)
    assert "Would register" in lines[0]
    assert registry.writes == 0


def test_windows_apply_writes_the_value_once_and_is_idempotent() -> None:
    registry = FakeRegistry(r"C:\old\manifest.json")
    lines = register_host(MANIFEST, apply=True, system="win32", registry=registry)
    assert registry.values[REGISTRY_KEY] == str(MANIFEST)
    assert "old" in "\n".join(lines)
    again = register_host(MANIFEST, apply=True, system="win32", registry=registry)
    assert "Already registered" in again[0]
    assert registry.writes == 1


def test_a_registry_value_that_does_not_take_is_an_error() -> None:
    with pytest.raises(ChamberlainError):
        register_host(MANIFEST, apply=True, system="win32", registry=FakeRegistry(takes=False))


def test_mac_copies_the_manifest_keeping_a_different_old_one(tmp_path: Path) -> None:
    manifest = tmp_path / "m.json"
    manifest.write_text("new", encoding="utf-8")
    folder = tmp_path / "NativeMessagingHosts"
    register_host(manifest, apply=False, system="darwin", mac_dir=folder)
    assert not folder.exists()
    register_host(manifest, apply=True, system="darwin", mac_dir=folder)
    assert (folder / "m.json").read_text(encoding="utf-8") == "new"
    manifest.write_text("newer", encoding="utf-8")
    register_host(manifest, apply=True, system="darwin", mac_dir=folder)
    assert (folder / "m.json.bak").read_text(encoding="utf-8") == "new"
    assert (folder / "m.json").read_text(encoding="utf-8") == "newer"


def test_control_close_start_status() -> None:
    desktop = FakeDesktop(running=True)
    assert control("status", desktop) == (0, "Thunderbird is running.")
    assert control("close", desktop, sleep=lambda _: None)[0] == 0
    assert control("status", desktop)[1] == "Thunderbird is not running."
    assert control("start", desktop)[1] == "Thunderbird started."
    assert control("start", desktop)[1] == "Thunderbird is already running."
    stuck = FakeDesktop(running=True, closes=False)
    assert control("close", stuck, sleep=lambda _: None)[0] == 1
