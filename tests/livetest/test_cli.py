"""The `chamberlain live-test` command over a synthetic local profile."""

import io
import json
from pathlib import Path

import pytest
import yaml

from chamberlain.app.context import SourceRouter
from chamberlain.connections import build
from chamberlain.connections.load import dump_connections, profile_path
from chamberlain.connections.model import Connections, ThunderbirdMailSource
from chamberlain.connectors.fake import FakeProvider
from chamberlain.ports.clock import ManualClock
from chamberlain.surface.cli import main

ADDRESS = "you@example.invalid"


@pytest.fixture(autouse=True)
def fake_mailbox(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = FakeProvider(ManualClock())
    provider.mail.add_source("mail:test")
    router = SourceRouter(mail={"mail:test": provider.mail}, calendar={}, contacts={})
    monkeypatch.setattr(build, "build_router", lambda *args, **kwargs: router)


def write_profile(root: Path, live: dict[str, object] | None) -> None:
    source = ThunderbirdMailSource(
        account_id="test",
        mailbox=ADDRESS,
        inbox_folder_id="in",
        sent_folder_id="sent",
        draft_folder_id="drafts",
        trash_folder_id="trash",
    )
    document = yaml.safe_load(dump_connections(Connections(sources={"mail:test": source})))
    if live is not None:
        document["live_test"] = live
    path = profile_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(document), encoding="utf-8")


ENABLED: dict[str, object] = {
    "enabled": True,
    "mail": {"mail:test": {"address": ADDRESS}},
}


def run(root: Path, *args: str) -> tuple[int, str]:
    out = io.StringIO()
    code = main(["--data", str(root), "live-test", *args], out)
    return code, out.getvalue()


def test_it_is_off_until_switched_on_and_a_dry_run_changes_nothing(tmp_path: Path) -> None:
    write_profile(tmp_path, None)
    code, text = run(tmp_path)
    assert code == 2
    reply = json.loads(text)
    assert reply["error"]["code"] == "config.invalid"
    assert "live_test.enabled" in reply["error"]["fix"]

    write_profile(tmp_path, ENABLED)
    code, text = run(tmp_path, "run", "--dry-run", "--only", "self_send")
    assert code == 0
    assert "mail.self_send[mail:test]" in text
    assert "planned (dry run)" in text
    assert not (tmp_path / "logs").exists()
    code, text = run(tmp_path, "report")
    assert (code, text.startswith("No live-test report")) == (2, True)


def test_grant_proposes_once_and_run_stops_until_the_owner_confirms(tmp_path: Path) -> None:
    write_profile(tmp_path, ENABLED)
    code, text = run(tmp_path, "run")
    assert code == 2
    assert "chamberlain live-test grant" in text

    code, granted = run(tmp_path, "grant")
    assert code == 0
    assert "mail:test: waiting for you" in granted
    assert "never confirms" in granted
    _, again = run(tmp_path, "grant")
    assert again.split("(")[1] == granted.split("(")[1]  # the same proposal, not a second one

    code, text = run(tmp_path, "run")
    assert code == 2
    assert "waiting for you" in text
    assert not (tmp_path / "logs" / "live-test").exists()
