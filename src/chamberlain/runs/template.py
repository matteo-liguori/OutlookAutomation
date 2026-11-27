"""The report layout: which sections a pass report has, who writes each, and how it is styled.

Contract: docs/contracts/06-memory.md (the report section). The standard layout ships in
`report_default.yaml`. The owner's own layout is one private file, `config/report-template.yaml`
in the data root (`chamberlain import` writes it from the old report template); when it is
there and valid it replaces the standard one as a whole, except that a layout without the
"Waiting for you" section gets the standard one at its top (`ALWAYS_FIRST`), so a layout written
before that section existed still shows what waits for the owner. A private file that cannot be
read is named in the report's Verification section, and the standard layout is used instead.

A section is written either by the harness (from its own records, when the pass ends) or by the
assistant (`instructions` is what it is told). The harness knows five sections: `waiting_for_you`,
`access_problems`, `important_appointments`, `permission_reviews` and `verification`.
"""

from importlib import resources
from pathlib import Path
from typing import Literal

import yaml
from pydantic import Field, ValidationError

from chamberlain.schema.base import Model

PRIVATE_NAME = "report-template.yaml"
HARNESS_SLOTS: tuple[str, ...] = (
    "waiting_for_you",
    "access_problems",
    "important_appointments",
    "permission_reviews",
    "verification",
)
TEMPLATE_MAX_BYTES = 40_000
ALWAYS_FIRST = "waiting_for_you"
"""The harness section every layout has, at the top unless the layout places it itself."""


class Slot(Model):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,39}$")
    by: Literal["harness", "agent"]
    heading: str = Field(min_length=1, max_length=120)
    style: Literal["plain", "red", "orange"] = "plain"
    mark: str = Field(default="", max_length=8)
    instructions: str = Field(default="", max_length=4000)


class ReportTemplate(Model):
    version: Literal[1] = 1
    title: str = Field(default="Mail check {date}", min_length=1, max_length=120)
    notes: str = Field(default="", max_length=4000)
    """Wording from the owner's old template that is for the writer, kept for the outline."""
    slots: tuple[Slot, ...] = Field(min_length=1, max_length=20)


def private_path(data_root: Path) -> Path:
    return data_root / "config" / PRIVATE_NAME


def parse_template(text: str) -> ReportTemplate:
    """The template in `text`, or a ValueError saying what is wrong with it."""
    try:
        loaded: object = yaml.safe_load(text)
        template = ReportTemplate.model_validate(loaded)
    except (yaml.YAMLError, ValidationError) as error:
        raise ValueError(str(error).splitlines()[0][:160]) from error
    ids = [slot.id for slot in template.slots]
    unknown = [s.id for s in template.slots if s.by == "harness" and s.id not in HARNESS_SLOTS]
    if len(set(ids)) != len(ids) or unknown:
        raise ValueError("a section id is repeated, or the harness cannot write one of them")
    if not any(slot.by == "agent" for slot in template.slots):
        raise ValueError("a template needs at least one section the assistant writes")
    return template


def default_template() -> ReportTemplate:
    text = resources.files("chamberlain.runs").joinpath("report_default.yaml").read_text("utf-8")
    return parse_template(text)


def _with_waiting(template: ReportTemplate) -> ReportTemplate:
    """The owner's layout, with the standard "Waiting for you" section first if it has none."""
    if any(slot.id == ALWAYS_FIRST for slot in template.slots):
        return template
    first = next(s for s in default_template().slots if s.id == ALWAYS_FIRST)
    return template.model_copy(update={"slots": (first, *template.slots)})


def load_template(data_root: Path) -> tuple[ReportTemplate, str | None]:
    """`(template, problem)`: the owner's layout when it is valid, else the standard one."""
    path = private_path(data_root)
    if not path.is_file():
        return default_template(), None
    try:
        if path.stat().st_size > TEMPLATE_MAX_BYTES:
            raise ValueError("the file is too large")
        return _with_waiting(parse_template(path.read_text(encoding="utf-8-sig"))), None
    except (OSError, ValueError) as error:
        return default_template(), f"The private report layout could not be used ({error})."
