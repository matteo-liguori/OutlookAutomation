"""The WebDAV and CalDAV requests and answers this connector needs, as plain XML.

Contract: docs/contracts/08-ports.md. Only what is used: PROPFIND for discovery and
health, and the calendar-query REPORT (time range with server-side expansion, or by UID).
"""

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import unquote, urlsplit

DAV = "DAV:"
CALDAV = "urn:ietf:params:xml:ns:caldav"
_NS = f'xmlns:D="{DAV}" xmlns:C="{CALDAV}"'
_HEAD = '<?xml version="1.0" encoding="utf-8"?>'


@dataclass(frozen=True)
class DavResponse:
    href: str
    status: int
    etag: str | None
    calendar_data: str | None
    display_name: str | None
    is_calendar: bool
    supports_events: bool
    principal: str | None
    home_set: str | None


def stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def propfind(*props: str) -> bytes:
    inner = "".join(f"<{p}/>" for p in props)
    return f"{_HEAD}<D:propfind {_NS}><D:prop>{inner}</D:prop></D:propfind>".encode()


PROPFIND_HEALTH = propfind("D:resourcetype")
PROPFIND_PRINCIPAL = propfind("D:current-user-principal")
PROPFIND_HOME = propfind("C:calendar-home-set")
PROPFIND_CALENDARS = propfind(
    "D:resourcetype", "D:displayname", "C:supported-calendar-component-set"
)


def query_window(start: datetime, end: datetime, *, expand: bool) -> bytes:
    window = f'start="{stamp(start)}" end="{stamp(end)}"'
    data = f"<C:expand {window}/>" if expand else ""
    return (
        f"{_HEAD}<C:calendar-query {_NS}><D:prop><D:getetag/><C:calendar-data>{data}"
        '</C:calendar-data></D:prop><C:filter><C:comp-filter name="VCALENDAR">'
        f'<C:comp-filter name="VEVENT"><C:time-range {window}/></C:comp-filter>'
        "</C:comp-filter></C:filter></C:calendar-query>"
    ).encode()


def query_uid(uid: str) -> bytes:
    safe = uid.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return (
        f"{_HEAD}<C:calendar-query {_NS}><D:prop><D:getetag/><C:calendar-data/></D:prop>"
        '<C:filter><C:comp-filter name="VCALENDAR"><C:comp-filter name="VEVENT">'
        '<C:prop-filter name="UID"><C:text-match collation="i;octet">'
        f"{safe}</C:text-match></C:prop-filter></C:comp-filter></C:comp-filter></C:filter>"
        "</C:calendar-query>"
    ).encode()


def _text(node: ET.Element | None) -> str | None:
    return None if node is None or node.text is None else node.text.strip()


def _status(text: str | None) -> int:
    parts = (text or "").split()
    return int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0


def parse_multistatus(body: bytes) -> list[DavResponse]:
    """Every response with the properties this connector reads. Raises ValueError if unreadable."""
    root = ET.fromstring(body)
    found: list[DavResponse] = []
    for response in root.findall(f"{{{DAV}}}response"):
        href = _text(response.find(f"{{{DAV}}}href")) or ""
        merged: dict[str, ET.Element] = {}
        status = 200
        for propstat in response.findall(f"{{{DAV}}}propstat"):
            code = _status(_text(propstat.find(f"{{{DAV}}}status")))
            prop = propstat.find(f"{{{DAV}}}prop")
            if prop is None:
                continue
            if code == 200:
                for child in prop:
                    merged[child.tag] = child
            else:
                status = code
        resource = merged.get(f"{{{DAV}}}resourcetype")
        components = merged.get(f"{{{CALDAV}}}supported-calendar-component-set")
        principal = merged.get(f"{{{DAV}}}current-user-principal")
        home = merged.get(f"{{{CALDAV}}}calendar-home-set")
        found.append(
            DavResponse(
                href=href,
                status=status,
                etag=_text(merged.get(f"{{{DAV}}}getetag")),
                calendar_data=_text(merged.get(f"{{{CALDAV}}}calendar-data")),
                display_name=_text(merged.get(f"{{{DAV}}}displayname")),
                is_calendar=resource is not None
                and resource.find(f"{{{CALDAV}}}calendar") is not None,
                supports_events=components is None
                or any(c.get("name") == "VEVENT" for c in components),
                principal=_text(principal.find(f"{{{DAV}}}href"))
                if principal is not None
                else None,
                home_set=_text(home.find(f"{{{DAV}}}href")) if home is not None else None,
            )
        )
    return found


def name_in(collection_path: str, href: str) -> str | None:
    """The resource file name under a collection, or None when the href is somewhere else."""
    path = unquote(urlsplit(href).path)
    prefix = unquote(collection_path)
    if not path.startswith(prefix) or path == prefix:
        return None
    name = path[len(prefix) :]
    return None if "/" in name else name
