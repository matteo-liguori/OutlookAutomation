"""Pure types and rules. No I/O, no clock, no store, no connector.

Contract: docs/contracts/01-domain.md.

Shared modules (the skeleton; change only via docs/BOARD.md): handles,
canonical, events, capabilities, mail, calendar, contacts, intents,
operations, grants.

Track-owned modules to be added in wave 1: cards.py and artifacts.py
(memory); grant rules and review dates in grants_rules.py (writes).
"""
