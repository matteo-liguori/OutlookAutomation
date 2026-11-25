"""The owner's read-state options (docs/contracts/05-grants.md, G-24)."""

from chamberlain.schema.base import Model


class ReadStateConfig(Model):
    """When the harness may mark mail read (05-grants.md G-24). Both off by default.

    Turning an option on grants nothing: marking mail read also needs a standing grant for the
    mailbox that the owner confirms in the viewer.
    """

    filing_marks_read: bool = False
    """A filing move marks the message read."""
    reporting_marks_read: bool = False
    """After a pass's report is kept, the mail that pass triaged is marked read."""
