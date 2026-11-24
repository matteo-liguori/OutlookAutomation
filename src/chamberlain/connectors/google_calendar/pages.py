# ruff: noqa: E501
"""Hosting page templates Google asks for when an app is published (setup `pages`)."""

from pathlib import Path

INDEX = """<!doctype html><html lang="en"><head><meta charset="utf-8"><title>{{app_name}}</title>
<meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="font-family:system-ui,sans-serif;max-width:40rem;margin:3rem auto;padding:0 1rem;line-height:1.5">
<h1>{{app_name}}</h1>
<p>A personal, single-user tool that reads and edits the owner's own Google Calendar events on the owner's own computer, so that an AI assistant can help manage the owner's schedule.</p>
<p>It is not offered to the public and has no other users.</p>
{{source_line}}
<p><a href="privacy.html">Privacy policy</a></p>
</body></html>
"""

PRIVACY = """<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Privacy policy - {{app_name}}</title>
<meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="font-family:system-ui,sans-serif;max-width:40rem;margin:3rem auto;padding:0 1rem;line-height:1.5">
<h1>Privacy policy</h1>
<p>{{app_name}} is a personal tool run by its owner on the owner's own computer.</p>
<h2>What it accesses</h2>
<p>With the owner's consent it reads and edits events on the owner's own Google Calendars through the Google Calendar API.</p>
<h2>Where data goes</h2>
<p>Calendar data and sign-in tokens stay on the owner's computer. They are not sent to any server operated by the developer, sold, shared or used for advertising. The tool has no servers and no analytics.</p>
<h2>Google API Services</h2>
<p>Use of information received from Google APIs adheres to the <a href="https://developers.google.com/terms/api-services-user-data-policy">Google API Services User Data Policy</a>, including the Limited Use requirements.</p>
<h2>Revoking access</h2>
<p>Access can be removed at any time at <a href="https://myaccount.google.com/permissions">myaccount.google.com/permissions</a>.</p>
</body></html>
"""


def write_pages(folder: Path, name: str, source_url: str = "") -> list[Path]:
    """Write the filled-in homepage and privacy pages for hosting on any public web page."""
    folder.mkdir(parents=True, exist_ok=True)
    source = (
        f'<p>Source code for the open-source harness: <a href="{source_url}">{source_url}</a>.</p>'
        if source_url
        else ""
    )
    written: list[Path] = []
    for filename, template in (("index.html", INDEX), ("privacy.html", PRIVACY)):
        text = template.replace("{{app_name}}", name).replace("{{source_line}}", source)
        path = folder / filename
        path.write_text(text, encoding="utf-8")
        written.append(path)
    return written
