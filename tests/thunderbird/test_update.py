"""Updating the add-on by replacing its file in the profile (owner, 2026-11-25)."""

from pathlib import Path

import pytest

from chamberlain.connectors.thunderbird.setup import EXTENSION_ID
from chamberlain.connectors.thunderbird.update import close_and_wait, profiles, update_addon
from chamberlain.schema.errors import ChamberlainError, ErrorCode


class FakeDesktop:
    def __init__(self, running: bool, closes: bool = True) -> None:
        self.is_running = running
        self.closes = closes
        self.calls: list[str] = []

    def running(self) -> bool:
        return self.is_running

    def close(self) -> None:
        self.calls.append("close")
        if self.closes:
            self.is_running = False

    def start(self) -> None:
        self.calls.append("start")
        self.is_running = True


def home_with(tmp_path: Path, *, installed: bool) -> tuple[Path, Path]:
    home = tmp_path / "Thunderbird"
    profile = home / "Profiles" / "abc.default-release"
    (profile / "extensions").mkdir(parents=True)
    other = tmp_path / "elsewhere"
    (other / "extensions").mkdir(parents=True)
    (home / "profiles.ini").write_text(
        "[Profile0]\nName=default-release\nIsRelative=1\nPath=Profiles/abc.default-release\n\n"
        f"[Profile1]\nName=other\nIsRelative=0\nPath={other}\n\n[General]\nVersion=2\n",
        encoding="utf-8",
    )
    target = profile / "extensions" / f"{EXTENSION_ID}.xpi"
    if installed:
        target.write_bytes(b"old add-on")
    return home, target


def built(tmp_path: Path) -> Path:
    package = tmp_path / "bridge" / "chamberlain-bridge.xpi"
    package.parent.mkdir(parents=True, exist_ok=True)
    package.write_bytes(b"new add-on")
    return package


def test_profiles_reads_relative_and_absolute_paths(tmp_path: Path) -> None:
    home, _ = home_with(tmp_path, installed=False)
    assert profiles(home) == [home / "Profiles/abc.default-release", tmp_path / "elsewhere"]


def test_a_running_thunderbird_is_closed_the_file_replaced_and_started_again(
    tmp_path: Path,
) -> None:
    home, target = home_with(tmp_path, installed=True)
    desktop = FakeDesktop(running=True)
    report = update_addon(
        tmp_path / "bridge",
        home=home,
        desktop=desktop,
        restart=True,
        build=lambda _: built(tmp_path),
        sleep=lambda _: None,
    )
    assert target.read_bytes() == b"new add-on"
    assert desktop.calls == ["close", "start"]
    assert report.updated == [target]
    assert report.restarted
    assert not (tmp_path / "elsewhere" / "extensions" / f"{EXTENSION_ID}.xpi").exists(), (
        "a profile without the add-on is left alone (a first install goes through Thunderbird)"
    )


def test_without_restart_a_running_thunderbird_is_left_alone(tmp_path: Path) -> None:
    home, target = home_with(tmp_path, installed=True)
    desktop = FakeDesktop(running=True)
    with pytest.raises(ChamberlainError) as raised:
        update_addon(
            tmp_path / "bridge",
            home=home,
            desktop=desktop,
            restart=False,
            build=lambda _: built(tmp_path),
        )
    assert raised.value.code is ErrorCode.SOURCE_BUSY
    assert target.read_bytes() == b"old add-on"
    assert desktop.calls == []


def test_a_thunderbird_that_will_not_close_is_never_overwritten(tmp_path: Path) -> None:
    home, target = home_with(tmp_path, installed=True)
    desktop = FakeDesktop(running=True, closes=False)
    with pytest.raises(ChamberlainError):
        update_addon(
            tmp_path / "bridge",
            home=home,
            desktop=desktop,
            restart=True,
            build=lambda _: built(tmp_path),
            sleep=lambda _: None,
        )
    assert target.read_bytes() == b"old add-on"
    assert desktop.calls == ["close"] * 7, "asked again every ten seconds of the minute"


def test_close_and_wait_asks_again_when_the_first_close_is_ignored() -> None:
    class Stubborn(FakeDesktop):
        def close(self) -> None:
            self.calls.append("close")
            if self.calls.count("close") == 2:
                self.is_running = False

    desktop = Stubborn(running=True)
    assert close_and_wait(desktop, sleep=lambda _: None)
    assert desktop.calls == ["close", "close"]
    assert close_and_wait(FakeDesktop(running=False), sleep=lambda _: None)
