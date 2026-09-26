"""Offline licence keys.

A key is a small JSON payload (customer, optional expiry, optional machine
binding) signed with the developer's Ed25519 private key. The app ships only
the public key, so it can verify keys anywhere with no internet connection but
cannot mint them. Keys are generated with tools/make_license.py.

Key text looks like:  MAS1-<base64url payload>.<base64url signature>
Whitespace and line breaks inside the text are ignored, so a key survives
being pasted from an e-mail or a chat message.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import sys
import uuid
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from PySide6.QtCore import QStandardPaths

# Raw 32-byte Ed25519 public key, hex. The matching private key lives only on
# the developer's machine (tools/keys/license_private.pem, git-ignored).
PUBLIC_KEY_HEX = "5ad6af179661c6bb7f370c27c5ed076446c7156bc1049f8b7284b6979b2ebc7d"

PREFIX = "MAS1-"
LICENSE_FILE = "license.key"


class LicenseError(Exception):
    """A key that cannot be accepted, with a short reason code.

    Codes: 'format', 'signature', 'expired', 'machine'.
    """

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class License:
    customer: str
    issued: date | None
    expires: date | None          # None means lifetime
    machine: str | None           # None means any computer
    raw: str

    @property
    def lifetime(self) -> bool:
        return self.expires is None

    def days_left(self, today: date | None = None) -> int | None:
        if self.expires is None:
            return None
        return (self.expires - (today or date.today())).days

    def validate(self, today: date | None = None, machine: str | None = None) -> None:
        """Raise LicenseError if this (already authentic) licence does not apply here and now."""
        today = today or date.today()
        if self.expires is not None and today > self.expires:
            raise LicenseError("expired")
        if self.machine and self.machine != (machine or machine_id()):
            raise LicenseError("machine")


# --------------------------------------------------------------------------- storage

def license_path() -> Path:
    base = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppDataLocation)
    return Path(base) / LICENSE_FILE


def load_saved_key() -> str | None:
    try:
        # utf-8-sig: tolerate the byte-order mark Notepad and PowerShell prepend.
        text = license_path().read_text(encoding="utf-8-sig").strip()
    except OSError:
        return None
    return text or None


def save_key(text: str) -> None:
    path = license_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(normalise(text) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------- machine id

def machine_id() -> str:
    """A short, stable identifier for this computer, shown as XXXX-XXXX-XXXX.

    On Windows it derives from the OS installation's MachineGuid, which
    survives reboots and network changes. Elsewhere it falls back to the
    primary network adapter's address.
    """
    seed = ""
    if sys.platform == "win32":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography") as key:
                seed = str(winreg.QueryValueEx(key, "MachineGuid")[0])
        except OSError:
            seed = ""
    if not seed:
        seed = f"{uuid.getnode():012x}"
    digest = hashlib.sha256(("mall-audio|" + seed).encode("utf-8")).hexdigest()[:12].upper()
    return f"{digest[0:4]}-{digest[4:8]}-{digest[8:12]}"


# --------------------------------------------------------------------------- parsing

def normalise(text: str) -> str:
    return "".join(text.split())


def _b64decode(part: str) -> bytes:
    padded = part + "=" * (-len(part) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii"))


def _parse_date(value: object) -> date | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise LicenseError("format")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise LicenseError("format") from exc


def parse_key(text: str, public_key_hex: str = PUBLIC_KEY_HEX) -> License:
    """Check a key's authenticity and decode it. Does NOT check expiry/machine."""
    compact = normalise(text)
    if not compact.startswith(PREFIX) or "." not in compact:
        raise LicenseError("format")
    body = compact[len(PREFIX):]
    payload_b64, _, signature_b64 = body.partition(".")
    try:
        payload = _b64decode(payload_b64)
        signature = _b64decode(signature_b64)
    except (binascii.Error, ValueError) as exc:
        raise LicenseError("format") from exc
    try:
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key_hex)).verify(signature, payload)
    except InvalidSignature as exc:
        raise LicenseError("signature") from exc
    try:
        data = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LicenseError("format") from exc
    if not isinstance(data, dict) or not isinstance(data.get("c"), str):
        raise LicenseError("format")
    machine = data.get("m") or None
    if machine is not None and not isinstance(machine, str):
        raise LicenseError("format")
    return License(
        customer=data["c"],
        issued=_parse_date(data.get("i")),
        expires=_parse_date(data.get("e")),
        machine=machine,
        raw=compact,
    )


def check_key(text: str) -> License:
    """Parse and fully validate a key for this computer today."""
    licence = parse_key(text)
    licence.validate()
    return licence


def current_license() -> tuple[License | None, str | None]:
    """The saved licence and, if it is not usable, why.

    Returns (licence, None) when everything is fine; (licence, code) when a
    key is saved but expired / bound elsewhere; (None, 'missing') when there
    is no key at all; (None, code) when the saved text is not a genuine key.
    """
    saved = load_saved_key()
    if saved is None:
        return None, "missing"
    try:
        licence = parse_key(saved)
    except LicenseError as exc:
        return None, exc.code
    try:
        licence.validate()
    except LicenseError as exc:
        return licence, exc.code
    return licence, None
