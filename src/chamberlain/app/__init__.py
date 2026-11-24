"""Application core: one entry point, the context, and the seams between tracks.

Contract: docs/contracts/02-tools.md (T-16). Shared package (docs/OWNERSHIP.md).

    core.py      App.call: validate, tick, handle, decorate, bound, audit
    context.py   Context, Result, Handler, Hooks, SourceRouter
    services.py  protocols one track offers the others
    doubles.py   in-memory stand-ins for those protocols
    doctor.py    the doctor handler
    wiring.py    assembles the app from the track modules
"""
