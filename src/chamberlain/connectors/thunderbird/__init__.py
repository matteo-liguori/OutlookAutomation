"""Thunderbird connector: Python client, bridge v2, and the add-on.

Contract: docs/contracts/08-ports.md (binding); docs/contracts/12-bridge-v2.md
(written by this track). Brief: docs/waves/wave-1/d-thunderbird.md.
Owner: wave-1 track (d).

Planned modules: protocol.py (bridge v2 messages as Pydantic models),
frames.py (length-prefixed frame reader and writer), native_host.py,
client.py, mail.py (MailPort), contacts.py (ContactsPort), setup.py, and
addon/ (the TypeScript add-on with generated types).
"""
