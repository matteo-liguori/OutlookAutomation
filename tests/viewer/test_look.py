"""The viewer's icon, manifest, theme switch and the rule that colours live in variables."""

import json
import re
from pathlib import Path

from viewer_rig import Rig

import chamberlain.surface.viewer as viewer_package

STATIC = Path(viewer_package.__file__).parent / "static"


def test_the_icon_and_manifest_are_served_with_their_types_and_the_gate_headers(rig: Rig) -> None:
    for path, content_type in (
        ("/icon.svg", "image/svg+xml"),
        ("/manifest.json", "application/manifest+json"),
        ("/static/theme.js", "text/javascript; charset=utf-8"),
    ):
        assert rig.call("GET", path, token=None, origin=None).status == 200, path
        assert rig.last_headers["Content-Type"] == content_type
        assert rig.last_headers["X-Content-Type-Options"] == "nosniff"
        csp = rig.last_headers["Content-Security-Policy"]
        assert "img-src 'self'" in csp
        assert "manifest-src 'self'" in csp
    assert rig.call("GET", "/icon.svg", token=None, host="evil.example").status == 403
    assert rig.call("GET", "/manifest.json", token=None, remote="10.0.0.5").status == 403
    assert rig.call("POST", "/icon.svg", {}, token=None).status == 405
    manifest = json.loads((STATIC / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["name"] == "Chamberlain"
    assert manifest["icons"][0]["src"] == "/icon.svg"
    assert manifest["theme_color"]
    assert manifest["background_color"]
    assert "<script" not in (STATIC / "icon.svg").read_text(encoding="utf-8")


def test_the_page_links_the_icon_manifest_and_loads_the_theme_script_in_the_head() -> None:
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    head = html.partition("</head>")[0]
    assert '<link rel="icon" type="image/svg+xml" href="/icon.svg">' in head
    assert '<link rel="manifest" href="/manifest.json">' in head
    assert '<meta name="theme-color"' in head
    assert '<meta name="color-scheme" content="light dark">' in head
    assert '<script src="/static/theme.js"></script>' in head  # no defer: before first paint
    assert 'id="theme-select"' in html
    assert 'aria-label="Colour theme"' in html
    theme = (STATIC / "theme.js").read_text(encoding="utf-8")
    assert "chamberlain.viewer.theme" in theme
    assert "data-theme" in theme


def test_dark_values_apply_for_the_system_and_for_an_explicit_choice() -> None:
    css = (STATIC / "page.css").read_text(encoding="utf-8")
    assert "@media (prefers-color-scheme: dark)" in css
    assert ':root:not([data-theme="light"])' in css
    assert ':root[data-theme="dark"]' in css


_VARIABLE = re.compile(r"--[\w-]+\s*:[^;{}]*;")
_COMMENT = re.compile(r"/\*.*?\*/", re.S)
_HARD = re.compile(r"(?<![\w#-])(#fff|#ffffff|#000|#000000|white|black)(?![\w-])", re.I)


def test_no_css_outside_the_variable_declarations_hard_codes_white_or_black() -> None:
    for name in ("page.css", "calendar.css", "report.css"):
        text = _VARIABLE.sub("", _COMMENT.sub("", (STATIC / name).read_text(encoding="utf-8")))
        assert _HARD.findall(text) == [], name
