"""Packaging and native host manifest (registration test intent; nothing is registered)."""

import json
import re
import zipfile
from pathlib import Path

import pytest

from chamberlain.connectors.thunderbird import native_host, setup
from chamberlain.connectors.thunderbird.setup import (
    ADDON_DIR,
    EXTENSION_ID,
    HOST_NAME,
    build_package,
    prepare,
    write_launcher,
    write_manifest,
)
from chamberlain.schema.errors import ChamberlainError, ErrorCode


@pytest.fixture(autouse=True)
def no_acl_changes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(native_host, "_restrict_to_user", lambda path: None)


def test_b10_the_package_is_reproducible_and_complete(tmp_path: Path) -> None:
    first = build_package(tmp_path / "one").read_bytes()
    second = build_package(tmp_path / "two").read_bytes()
    assert first == second
    package = tmp_path / "one" / f"chamberlain-bridge-{setup.addon_version()}.xpi"
    with zipfile.ZipFile(package) as archive:
        assert sorted(archive.namelist()) == [
            "background.js",
            "experiment/api.js",
            "experiment/schema.json",
            "manifest.json",
        ]
        script = archive.read("background.js").decode()
    assert "function cmdMove" in script
    assert ": string" not in script  # types were stripped
    assert not (tmp_path / "one" / "build").exists()


def test_b10_the_manifest_version_and_extension_id_agree_with_the_code() -> None:
    manifest = json.loads((ADDON_DIR / "manifest.json").read_text(encoding="utf-8"))
    background = (ADDON_DIR / "src" / "background.ts").read_text(encoding="utf-8")
    match = re.search(r'ADDON_VERSION = "([^"]+)"', background)
    assert match is not None
    assert match.group(1) == manifest["version"]
    assert manifest["browser_specific_settings"]["gecko"]["id"] == EXTENSION_ID
    assert manifest["experiment_apis"]["chamberlainMail"]["parent"]["script"] == "experiment/api.js"
    assert HOST_NAME in (ADDON_DIR / "src" / "background.ts").read_text(encoding="utf-8")


def test_b10_registration_preserves_another_installation(tmp_path: Path) -> None:
    """Old: test_registration_preserves_other_installation..."""
    manifest_dir = tmp_path / "native"
    (tmp_path / "mine").mkdir()
    mine = write_launcher(tmp_path / "mine", Path("python"))
    other = tmp_path / "other" / "native-host.cmd"
    other.parent.mkdir()
    other.write_text("x", encoding="utf-8")
    target = write_manifest(manifest_dir, other)
    before = target.read_text(encoding="utf-8")
    with pytest.raises(ChamberlainError) as raised:
        write_manifest(manifest_dir, mine)
    assert raised.value.code is ErrorCode.CONFIG_INVALID
    assert target.read_text(encoding="utf-8") == before
    write_manifest(manifest_dir, mine, replace_host_path=other)
    assert json.loads(target.read_text(encoding="utf-8"))["path"] == str(mine)


def test_b10_an_unrelated_manifest_is_left_alone(tmp_path: Path) -> None:
    manifest_dir = tmp_path / "native"
    manifest_dir.mkdir()
    target = manifest_dir / f"{HOST_NAME}.json"
    target.write_text(json.dumps({"name": "something-else", "path": "x"}), encoding="utf-8")
    with pytest.raises(ChamberlainError):
        write_manifest(manifest_dir, tmp_path / "launcher")
    assert json.loads(target.read_text(encoding="utf-8"))["name"] == "something-else"


def test_b10_setup_writes_only_where_told_and_registers_nothing(tmp_path: Path) -> None:
    bridge = tmp_path / "bridge"
    manifests = tmp_path / "manifests"
    done = prepare(bridge, manifests)
    assert Path(done["package"]).parent == bridge
    assert Path(done["native_manifest"]).parent == manifests
    written = {p for p in tmp_path.rglob("*") if p.is_file()}
    assert all(bridge in p.parents or manifests in p.parents for p in written)
    manifest = json.loads(Path(done["native_manifest"]).read_text(encoding="utf-8"))
    assert manifest["allowed_extensions"] == [EXTENSION_ID]
    assert manifest["type"] == "stdio"
    assert "nothing was registered" in done["next"]


def test_b10_launcher_paths_with_shell_characters_are_refused(tmp_path: Path) -> None:
    with pytest.raises(ChamberlainError):
        write_launcher(tmp_path, Path("C:/bad&path/python.exe"))
