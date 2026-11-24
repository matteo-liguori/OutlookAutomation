"""Windows DPAPI decryption for the secrets the old harness saved on Windows.

The old harness kept the iCloud app-specific password in `icloud-caldav.json`, encrypted with
DPAPI: schema 2 with machine scope and a fixed entropy, schema 1 as a PowerShell SecureString
(user scope, hex). Only the user who saved it, on the same machine, can decrypt it. The value
goes straight to the credential store; it is never printed or written to a file.
"""

import ctypes
import sys
from collections.abc import Callable

Unprotect = Callable[[bytes, bytes | None], bytes]

_UI_FORBIDDEN = 0x1


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob(data: bytes) -> tuple[_Blob, ctypes.Array[ctypes.c_char]]:
    buffer = ctypes.create_string_buffer(data, len(data))
    return _Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char))), buffer


def unprotect(data: bytes, entropy: bytes | None) -> bytes:
    """Decrypt a DPAPI blob for the current user; OSError when it cannot be decrypted here."""
    if sys.platform != "win32":
        raise OSError("DPAPI exists only on Windows.")
    windll = ctypes.windll  # pyright: ignore[reportAttributeAccessIssue,reportUnknownMemberType,reportUnknownVariableType]
    crypt32 = windll.crypt32  # pyright: ignore[reportUnknownMemberType,reportUnknownVariableType]
    kernel32 = windll.kernel32  # pyright: ignore[reportUnknownMemberType,reportUnknownVariableType]
    source, _keep = _blob(data)
    salt, _keep_salt = _blob(entropy) if entropy is not None else (None, None)
    out = _Blob()
    ok = crypt32.CryptUnprotectData(  # pyright: ignore[reportUnknownMemberType]
        ctypes.byref(source),
        None,
        ctypes.byref(salt) if salt is not None else None,
        None,
        None,
        _UI_FORBIDDEN,
        ctypes.byref(out),
    )
    if not ok:
        raise OSError("The saved secret cannot be decrypted by this Windows user.")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(out.pbData)  # pyright: ignore[reportUnknownMemberType]
