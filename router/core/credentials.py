"""Cross-platform secret store. Read-only by default; writes are explicit.

Backends, in order of preference:
  1. Environment override  ROUTER_CRED_<SERVICE>_<ACCOUNT> (transient, never stored)
  2. macOS Keychain        `security find-generic-password`
  3. Linux Secret Service  `secret-tool lookup/store/clear`
  4. Windows Credential Manager via ctypes (CredReadW/CredWriteW/CredDeleteW)

No third-party deps. Secrets are never logged; on failure we return None
instead of raising, so callers degrade gracefully (sentinel layer).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys


def _env_name(service: str, account: str) -> str:
    clean = lambda s: "".join(c if c.isalnum() else "_" for c in s).upper()
    return f"ROUTER_CRED_{clean(service)}_{clean(account)}"


def get(service: str, account: str) -> str | None:
    """Return the secret, or None if not found / backend unavailable."""
    hit = os.environ.get(_env_name(service, account))
    if hit:
        return hit
    try:
        if sys.platform == "darwin":
            return _macos_get(service, account)
        if sys.platform == "win32":
            return _windows_get(service, account)
        return _linux_get(service, account)
    except Exception:
        return None


def set(service: str, account: str, secret: str) -> bool:
    """Store a secret. Returns True on success."""
    try:
        if sys.platform == "darwin":
            _run(["security", "add-generic-password", "-U",
                  "-s", service, "-a", account, "-w", secret],
                 check=True)
        elif sys.platform == "win32":
            _windows_set(service, account, secret)
        else:
            _run(["secret-tool", "store", "--label", f"router/{service}",
                  "service", service, "account", account],
                 input=secret.encode(), check=True)
        return True
    except Exception:
        return False


def delete(service: str, account: str) -> bool:
    """Remove a secret. Returns True on success (or if it wasn't there)."""
    try:
        if sys.platform == "darwin":
            r = _run(["security", "delete-generic-password",
                      "-s", service, "-a", account])
            return r.returncode == 0
        if sys.platform == "win32":
            return _windows_delete(service, account)
        r = _run(["secret-tool", "clear", "service", service,
                  "account", account])
        return r.returncode == 0
    except Exception:
        return False


def backend_name() -> str:
    """Human-readable name of the active credential backend."""
    if sys.platform == "darwin":
        return "macos-keychain"
    if sys.platform == "win32":
        return "windows-credential-manager"
    return "linux-secret-service"


# -- macOS -----------------------------------------------------------------
def _run(argv: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, timeout=15, **kw)


def _macos_get(service: str, account: str) -> str | None:
    if not shutil.which("security"):
        return None
    r = _run(["security", "find-generic-password", "-w",
              "-s", service, "-a", account])
    return r.stdout.decode().strip() or None if r.returncode == 0 else None


# -- Linux -----------------------------------------------------------------
def _linux_get(service: str, account: str) -> str | None:
    if not shutil.which("secret-tool"):
        return None
    r = _run(["secret-tool", "lookup", "service", service, "account", account])
    return r.stdout.decode().strip() or None if r.returncode == 0 else None


# -- Windows ---------------------------------------------------------------
# stdlib-only access to the Credential Manager (no pywin32 needed).
def _windows_api():
    import ctypes
    from ctypes import wintypes
    adv = ctypes.windll.advapi32

    class CREDENTIALW(ctypes.Structure):
        """win32 CREDENTIALW struct for CredReadW/CredWriteW."""
        _fields_ = [("Flags", wintypes.DWORD),
                    ("Type", wintypes.DWORD),
                    ("TargetName", wintypes.LPWSTR),
                    ("Comment", wintypes.LPWSTR),
                    ("LastWritten", wintypes.FILETIME),
                    ("CredentialBlobSize", wintypes.DWORD),
                    ("CredentialBlob", wintypes.LPBYTE),
                    ("Persist", wintypes.DWORD),
                    ("AttributeCount", wintypes.DWORD),
                    ("Attributes", wintypes.LPVOID),
                    ("TargetAlias", wintypes.LPWSTR),
                    ("UserName", wintypes.LPWSTR)]

    PCREDENTIALW = ctypes.POINTER(CREDENTIALW)
    adv.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD,
                              wintypes.DWORD,
                              ctypes.POINTER(PCREDENTIALW)]
    adv.CredReadW.restype = wintypes.BOOL
    adv.CredWriteW.argtypes = [PCREDENTIALW, wintypes.DWORD]
    adv.CredWriteW.restype = wintypes.BOOL
    adv.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD,
                                wintypes.DWORD]
    adv.CredDeleteW.restype = wintypes.BOOL
    adv.CredFree.argtypes = [wintypes.LPVOID]
    return adv, CREDENTIALW


def _target(service: str, account: str) -> str:
    return f"router/{service}/{account}"


def _windows_get(service: str, account: str) -> str | None:
    adv, CREDENTIALW = _windows_api()
    import ctypes
    from ctypes import wintypes
    cred = ctypes.POINTER(CREDENTIALW)()
    ok = adv.CredReadW(_target(service, account), 1, 0, ctypes.byref(cred))
    if not ok:
        return None
    try:
        blob = ctypes.string_at(cred.contents.CredentialBlob,
                                cred.contents.CredentialBlobSize)
        return blob.decode("utf-16-le") or None
    finally:
        adv.CredFree(cred)


def _windows_set(service: str, account: str, secret: str) -> None:
    adv, CREDENTIALW = _windows_api()
    import ctypes
    blob = secret.encode("utf-16-le")
    buf = ctypes.create_string_buffer(blob)
    cred = CREDENTIALW()
    cred.Flags = 0
    cred.Type = 1  # CRED_TYPE_GENERIC
    cred.TargetName = _target(service, account)
    cred.CredentialBlobSize = len(blob)
    cred.CredentialBlob = ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte))
    cred.Persist = 2  # CRED_PERSIST_LOCAL_MACHINE
    cred.UserName = account
    if not adv.CredWriteW(ctypes.byref(cred), 0):
        raise OSError("CredWriteW failed")


def _windows_delete(service: str, account: str) -> bool:
    adv, _ = _windows_api()
    return bool(adv.CredDeleteW(_target(service, account), 1, 0))
