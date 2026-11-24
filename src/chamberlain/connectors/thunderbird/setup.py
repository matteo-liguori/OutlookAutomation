"""Build the add-on package and write the native host manifest into a directory.

Contract: docs/contracts/12-bridge-v2.md section 10. Nothing is registered
system-wide here: no registry key, no Thunderbird profile, no install. The
caller names the bridge directory (`<data root>/bridge/`, R9) and the manifest
directory; installing the package and pointing Thunderbird at the manifest
are separate, owner-approved steps. An existing manifest from another
installation is never overwritten without naming its launcher.
"""

import argparse
import json
import shlex
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Final

from chamberlain.connectors.thunderbird.native_host import private_directory
from chamberlain.dataroot import refuse_inside_checkout
from chamberlain.schema.errors import ChamberlainError, ErrorCode

HOST_NAME: Final = "chamberlain_thunderbird_bridge"
EXTENSION_ID: Final = "chamberlain-bridge@chamberlain.local"
ADDON_DIR: Final = Path(__file__).parent / "addon"
_FIXED_TIME: Final = (2026, 1, 1, 0, 0, 0)
_SHELL_CHARS: Final = '"%\r\n\x00^&|<>!'


def addon_version() -> str:
    manifest = json.loads((ADDON_DIR / "manifest.json").read_text(encoding="utf-8"))
    return str(manifest["version"])


def package_files() -> tuple[str, ...]:
    """Every experiment file the manifest names, so the package can never miss one."""
    manifest = json.loads((ADDON_DIR / "manifest.json").read_text(encoding="utf-8"))
    names: set[str] = set()
    for api in manifest.get("experiment_apis", {}).values():
        names.add(str(api["schema"]))
        names.add(str(api["parent"]["script"]))
    return tuple(sorted(names))


def build_addon_script(output: Path, *, node: str | None = None) -> Path:
    """Strip the TypeScript types into one classic `background.js` (needs Node 22.13 or newer)."""
    executable = node or shutil.which("node")
    if executable is None:
        raise ChamberlainError(
            ErrorCode.CONFIG_INVALID,
            "Building the add-on needs Node.js.",
            fix="Install Node.js 22.13 or newer, then run setup again.",
        )
    output.mkdir(parents=True, exist_ok=True)
    done = subprocess.run(
        [executable, "--no-warnings", str(ADDON_DIR / "build.mjs"), str(output)],
        capture_output=True,
        text=True,
        check=False,
    )
    if done.returncode:
        raise ChamberlainError(
            ErrorCode.CONFIG_INVALID,
            "The add-on could not be built.",
            detail=done.stderr[:500],
        )
    return output / "background.js"


def build_package(bridge_dir: Path, *, node: str | None = None) -> Path:
    """A reproducible `.xpi`: fixed timestamps and sorted entries."""
    private_directory(bridge_dir)
    staging = bridge_dir / "build"
    script = build_addon_script(staging, node=node)
    package = bridge_dir / f"chamberlain-bridge-{addon_version()}.xpi"
    entries = {
        "manifest.json": (ADDON_DIR / "manifest.json").read_bytes(),
        "background.js": script.read_bytes(),
        **{name: (ADDON_DIR / name).read_bytes() for name in package_files()},
    }
    with zipfile.ZipFile(package, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in sorted(entries):
            info = zipfile.ZipInfo(name, date_time=_FIXED_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, entries[name])
    shutil.rmtree(staging)
    return package


def write_launcher(bridge_dir: Path, python: Path) -> Path:
    """The private script Thunderbird starts as the native host."""
    for value in (str(bridge_dir), str(python)):
        if any(char in value for char in _SHELL_CHARS):
            raise ChamberlainError(
                ErrorCode.CONFIG_INVALID,
                "Paths with command-shell characters cannot be used by the launcher.",
            )
    module = "chamberlain.connectors.thunderbird.native_host"
    if sys.platform == "win32":
        launcher = bridge_dir / "native-host.cmd"
        launcher.write_text(
            f'@echo off\r\n"{python}" -m {module} "{bridge_dir}"\r\n', encoding="utf-8", newline=""
        )
    else:
        launcher = bridge_dir / "native-host"
        launcher.write_text(
            f"#!/bin/sh\nexec {shlex.quote(str(python))} -m {module} "
            f"{shlex.quote(str(bridge_dir))}\n",
            encoding="utf-8",
        )
        launcher.chmod(0o700)
    return launcher


def write_manifest(
    manifest_dir: Path, launcher: Path, *, replace_host_path: Path | None = None
) -> Path:
    """Write the native messaging manifest; never take over another installation silently."""
    manifest_dir.mkdir(parents=True, exist_ok=True)
    target = manifest_dir / f"{HOST_NAME}.json"
    registration = {
        "name": HOST_NAME,
        "description": "Chamberlain Thunderbird bridge",
        "path": str(launcher),
        "type": "stdio",
        "allowed_extensions": [EXTENSION_ID],
    }
    if target.exists():
        try:
            old: dict[str, object] = json.loads(target.read_text(encoding="utf-8"))
        except ValueError:
            old = {}
        if old.get("name") != HOST_NAME or old.get("allowed_extensions") != [EXTENSION_ID]:
            raise ChamberlainError(
                ErrorCode.CONFIG_INVALID,
                "An unrelated native host manifest is already there; it was left alone.",
            )
        if old.get("path") != str(launcher) and (
            replace_host_path is None or old.get("path") != str(replace_host_path.resolve())
        ):
            raise ChamberlainError(
                ErrorCode.CONFIG_INVALID,
                "That native host belongs to another installation.",
                fix="Name its launcher with --replace-host-path to migrate it.",
            )
    target.write_text(json.dumps(registration, indent=2) + "\n", encoding="utf-8")
    return target


def prepare(
    bridge_dir: Path,
    manifest_dir: Path,
    *,
    python: Path | None = None,
    replace_host_path: Path | None = None,
    node: str | None = None,
) -> dict[str, str]:
    private_directory(bridge_dir)
    package = build_package(bridge_dir, node=node)
    launcher = write_launcher(bridge_dir, python or Path(sys.executable))
    manifest = write_manifest(manifest_dir, launcher, replace_host_path=replace_host_path)
    return {
        "package": str(package),
        "launcher": str(launcher),
        "native_manifest": str(manifest),
        "next": "Install the package once in Thunderbird (Add-ons Manager, "
        "Install Add-on From File) and register the manifest for your platform; "
        "nothing was registered by this command.",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bridge-dir", type=Path, required=True)
    parser.add_argument("--manifest-dir", type=Path, required=True)
    parser.add_argument("--replace-host-path", type=Path)
    args = parser.parse_args(argv)
    try:
        refuse_inside_checkout(args.bridge_dir)
        done = prepare(
            args.bridge_dir.resolve(),
            args.manifest_dir.resolve(),
            replace_host_path=args.replace_host_path,
        )
    except ChamberlainError as error:
        sys.stderr.write(f"{error.message} {error.fix}\n")
        return 2
    sys.stdout.write(json.dumps(done, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
