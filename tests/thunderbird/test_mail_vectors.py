"""Both sides of the bridge use these Message-ID and duplicate-subject examples."""

import json
from pathlib import Path

from chamberlain.connectors.thunderbird import protocol
from chamberlain.domain.mail import bare, normalise_subject


def test_mail_identity_vectors() -> None:
    path = Path(protocol.__file__).parent / "addon/test/mail-vectors.json"
    vectors = json.loads(path.read_text(encoding="utf-8"))
    for value, expected in vectors["ids"]:
        assert bare(value) == expected
    for value, expected in vectors["subjects"]:
        assert normalise_subject(value) == expected
