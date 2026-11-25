"""The add-on's own gate: Node tests, TypeScript type-check and static B-3/B-4 rules."""

import re
import shutil
import subprocess
from pathlib import Path

import pytest
from thunderbird_rig import ADDON_DIR

from chamberlain.connectors.thunderbird.protocol import Command

NODE = shutil.which("node")


def run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=ADDON_DIR, capture_output=True, text=True, check=False)


def test_addon_node_tests_pass() -> None:
    """Runs addon/test/*.test.mjs (bridge rules B-1 to B-9 against a scripted Thunderbird)."""
    if NODE is None:
        pytest.fail("Node.js is required for the add-on tests")
    tests = sorted(str(p) for p in (ADDON_DIR / "test").glob("*.test.mjs"))
    done = run([NODE, "--no-warnings", "--test", *tests])
    assert done.returncode == 0, done.stdout[-3000:] + done.stderr[-1000:]


def test_addon_typechecks() -> None:
    """`tsc --noEmit` over the generated types and the hand-written sources."""
    tsc = ADDON_DIR / "node_modules" / ".bin" / ("tsc.cmd" if Path("C:/").exists() else "tsc")
    if not tsc.exists():
        pytest.skip("run `npm ci` in connectors/thunderbird/addon to enable the type-check")
    done = run([str(tsc), "--noEmit", "-p", "."])
    assert done.returncode == 0, done.stdout[-3000:]


SOURCES = [
    *sorted((ADDON_DIR / "src").glob("*.ts")),
    ADDON_DIR / "experiment" / "api.js",
    ADDON_DIR / "experiment" / "calendar_api.js",
]
FORBIDDEN = [
    r"\beval\s*\(",
    r"new\s+Function\b",
    r"\bsetTimeout\s*\(\s*[\"'`]",
    r"\bWebSocket\b",
    r"\bXMLHttpRequest\b",
    r"\bfetch\s*\(",
    r"\.listen\s*\(",
    r"storage\.local",
    r"\bimportScripts\b",
    r"nsIProcess|subprocess|runw\b",
]


def test_b3_b4_the_addon_has_no_eval_listener_network_or_journal() -> None:
    for source in SOURCES:
        text = source.read_text(encoding="utf-8")
        code = re.sub(r"//.*", "", text)
        for pattern in FORBIDDEN:
            assert not re.search(pattern, code), f"{source.name} matches {pattern}"


def test_b2_the_addon_dispatch_covers_exactly_the_closed_command_list() -> None:
    text = (ADDON_DIR / "src" / "background.ts").read_text(encoding="utf-8")
    block = text[text.index("async function runCommand") : text.index("function execute")]
    handled = set(re.findall(r'case "([a-z_]+)":', block))
    assert handled == {command.value for command in Command}


def test_b3_the_mail_helper_exposes_only_eight_named_functions() -> None:
    api = (ADDON_DIR / "experiment" / "api.js").read_text(encoding="utf-8")
    names = set(re.findall(r"^\s{8}async (\w+)\(", api, re.MULTILINE))
    assert names == {
        "syncFolders",
        "headerAttachmentFlags",
        "setReadFlags",
        "holdComposer",
        "takePrompts",
        "openDraft",
        "findComposers",
        "closeComposer",
    }
    assert "execute(" not in api
    schema = (ADDON_DIR / "experiment" / "schema.json").read_text(encoding="utf-8")
    assert set(re.findall(r'"name": "(\w+)",\s*"type": "function"', schema)) == names


def test_b2_the_calendar_helper_exposes_only_eight_named_functions() -> None:
    api = (ADDON_DIR / "experiment" / "calendar_api.js").read_text(encoding="utf-8")
    names = set(re.findall(r"^\s{8}async (\w+)\(", api, re.MULTILINE))
    assert names == {
        "isOffline",
        "listCalendars",
        "queryEvents",
        "getEvent",
        "inspectEvent",
        "addEvent",
        "modifyEvent",
        "deleteEvent",
    }
    assert "execute(" not in api
    schema = (ADDON_DIR / "experiment" / "calendar_schema.json").read_text(encoding="utf-8")
    assert set(re.findall(r'"name": "(\w+)",\s*"type": "function"', schema)) == names
