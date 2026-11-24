"""The ContactsPort contract suite.

Contract: docs/contracts/08-ports.md. Subclass and provide a `rig` fixture
returning a fresh `ContactsRig`.
"""

from chamberlain.domain.capabilities import CapabilityName
from chamberlain.domain.contacts import ContactQuery
from chamberlain.testing.rigs import ContactsRig


class ContactsPortContract:
    def test_p1_capabilities_are_declared(self, rig: ContactsRig) -> None:
        declared = rig.port.capabilities(rig.source)
        assert declared.get(CapabilityName.CONTACTS_SEARCH) is not None

    def test_search_matches_name_and_address(self, rig: ContactsRig) -> None:
        rig.seed("Ada Contracttest", "ada.contracttest@example.org")
        rig.seed("Someone Else", "else@example.org")
        by_name = rig.port.search(rig.source, ContactQuery(text="contracttest"))
        by_address = rig.port.search(rig.source, ContactQuery(text="ADA.CONTRACTTEST@"))
        assert [c.name for c in by_name.items] == ["Ada Contracttest"]
        assert [c.emails for c in by_address.items] == [("ada.contracttest@example.org",)]

    def test_p4_pages_cover_everything_once(self, rig: ContactsRig) -> None:
        for index in range(5):
            rig.seed(f"Paging Marker {index}", f"paging{index}@example.org")
        seen: list[str] = []
        cursor: str | None = None
        for _ in range(10):
            page = rig.port.search(rig.source, ContactQuery(text="paging marker", limit=2), cursor)
            seen.extend(c.name for c in page.items)
            cursor = page.cursor
            if cursor is None:
                assert page.complete
                break
        assert len(seen) == len(set(seen)) == 5
