"""One tool call spends at most one sync time limit on all mailboxes together (S7)."""

import json
from typing import Any, cast

from runs_rig import MAIL, Rig

from chamberlain.app.wiring import run_tick
from chamberlain.ports.sync import FOLDER_SECONDS
from chamberlain.store.db import transaction
from chamberlain.store.schema import MAIL_SYNC_KEY, set_state

SECOND = "mail:zz-second"  # sorts after MAIL: synced second
LIMIT_SECONDS = 60


def test_slow_mailboxes_share_one_limit_and_the_rest_wait_for_the_next_tick(rig: Rig) -> None:
    with transaction(rig.ctx.db):
        set_state(
            rig.ctx.db,
            rig.clock,
            MAIL_SYNC_KEY,
            json.dumps({"fresh_minutes": 10, "time_limit_seconds": LIMIT_SECONDS}),
        )
    rig.fake.mail.add_source(SECOND)
    cast(dict[str, Any], rig.ctx.sources.mail)[SECOND] = rig.fake.mail
    rig.fake.mail.stick_folder(MAIL, "f-archive")  # the first mailbox eats the whole limit

    before = rig.clock.now()
    run_tick(rig.app)
    spent = (rig.clock.now() - before).total_seconds()
    assert spent <= LIMIT_SECONDS + FOLDER_SECONDS, "one overall limit, not one per mailbox"
    asked = [source for source, _folders in rig.fake.world.sync_log]
    assert MAIL in asked
    assert SECOND not in asked, "the mailbox the limit cut off was not asked at all"
    rows = rig.rows("SELECT failure FROM mail_sync_folders WHERE source_id = ?", SECOND)
    assert rows
    assert all(r["failure"] == "time-limit" for r in rows), "it is reported, not silent"

    rig.fake.mail.stick_folder(MAIL, "f-archive", stuck=False)
    rig.clock.advance(minutes=20)
    run_tick(rig.app)
    assert SECOND in [source for source, _folders in rig.fake.world.sync_log], "tried next tick"
    after = rig.rows("SELECT failure FROM mail_sync_folders WHERE source_id = ?", SECOND)
    assert all(r["failure"] == "" for r in after)
