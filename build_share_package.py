"""Build a source-only starter archive from an explicit, reviewable allowlist."""

import argparse
import hashlib
import json
from pathlib import Path
import zipfile
from automation_paths import data_root

ROOT = Path(__file__).resolve().parent


def build(destination, root=ROOT, target_platform="windows"):
    root = Path(root).resolve()
    destination = Path(destination).resolve()
    manifest = json.loads((root / "packaging/files.json").read_text(encoding="utf-8"))
    if target_platform == "macos":
        manifest["README.md"] = "packaging/macos/README.md"
        manifest["AGENTS.md"] = "packaging/macos/AGENTS.md"
    elif target_platform != "windows":
        raise ValueError("Choose windows or macos")
    profile_path = data_root(root) / 'monitor/profile.json'
    owner_stores = []
    if profile_path.is_file():
        owner_stores = json.loads(profile_path.read_text(encoding="utf-8-sig")).get("mail", {}).values()
    contents = {}
    for target, source in manifest.items():
        for entry in (target, source):
            if "\\" in entry or ":" in entry or any(part.casefold() in {".local", ".git", ".venv", "threads", "backups"} for part in Path(entry).parts):
                raise ValueError("Private or nonportable package path: " + entry)
        relative = Path(target)
        if relative.is_absolute() or ".." in relative.parts or target.startswith(".") and target not in {".gitignore", ".gitattributes"}:
            raise ValueError("Invalid archive path: " + target)
        path = root / source
        if any(p.is_symlink() for p in (path, *path.parents) if p != root) or not path.resolve().is_relative_to(root):
            raise ValueError("Package source must be a regular file within the project: " + source)
        contents[target] = path.read_bytes()
    # Ownership-specific files are never inputs; this extra guard catches mistakes
    # when expanding the allowlist, rather than attempting to redact private state.
    for target, data in contents.items():
        if any(part in {".local", ".git", ".venv", "threads", "backups"} for part in Path(target).parts):
            raise ValueError("Private path in package: " + target)
        if Path(target).suffix.lower() in {".db", ".sqlite", ".pst", ".ost", ".msg", ".eml"}:
            raise ValueError("Private artifact in package: " + target)
        for marker in (address.encode("utf-8") for address in owner_stores):
            if marker.lower() in data.lower():
                raise ValueError("Owner-specific identity in package: " + target)
    receipt = {"schema_version": 1, "files": {name: hashlib.sha256(data).hexdigest() for name, data in sorted(contents.items())}}
    contents["PACKAGE_MANIFEST.json"] = (json.dumps(receipt, indent=2) + "\n").encode()
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(contents.items()):
            archive.writestr("OutlookAutomation/" + name, data)
    return {"path": str(destination), "files": len(contents), "sha256": hashlib.sha256(destination.read_bytes()).hexdigest()}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="output/OutlookAutomation-starter.zip")
    parser.add_argument("--platform", choices=["windows", "macos"], default="windows")
    args = parser.parse_args()
    print(json.dumps(build(args.output, target_platform=args.platform), indent=2))
