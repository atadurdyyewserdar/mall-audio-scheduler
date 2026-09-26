"""Developer tool: create licence keys for Mall Audio Scheduler.

This script and the private key in tools/keys/ must never be shipped to
customers or committed to a public repository. The app only carries the
public key and can verify keys offline.

Examples (run from the project root with the venv's Python):

  # Lifetime licence, works on any computer
  python tools/make_license.py --customer "Berkarar Mall"

  # Expires at the end of 2027
  python tools/make_license.py --customer "Berkarar Mall" --expires 2027-12-31

  # Locked to one computer (the machine ID is shown in the app's activation window)
  python tools/make_license.py --customer "Berkarar Mall" --machine 1A2B-3C4D-5E6F

  # Save the key to a file the customer can load in the activation window
  python tools/make_license.py --customer "Berkarar Mall" --out "Berkarar Mall.key"

  # Inspect an existing key
  python tools/make_license.py --verify "MAS1-...."

  # First-time setup on a new developer machine: create a fresh key pair and
  # print the public key to paste into mall_audio/license.py
  python tools/make_license.py --init
"""
from __future__ import annotations

import argparse
import base64
import json
import re
import sys
from datetime import date
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mall_audio.license import PREFIX, PUBLIC_KEY_HEX, LicenseError, parse_key  # noqa: E402

PRIVATE_KEY_PATH = Path(__file__).resolve().parent / "keys" / "license_private.pem"
MACHINE_RE = re.compile(r"^[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}$")


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def load_private_key() -> Ed25519PrivateKey:
    if not PRIVATE_KEY_PATH.exists():
        sys.exit(f"No private key at {PRIVATE_KEY_PATH}. Run with --init on the developer machine that owns the keys.")
    key = serialization.load_pem_private_key(PRIVATE_KEY_PATH.read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        sys.exit("Private key is not an Ed25519 key.")
    public_hex = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()
    if public_hex != PUBLIC_KEY_HEX:
        sys.exit(
            "This private key does not match PUBLIC_KEY_HEX in mall_audio/license.py.\n"
            f"  private key's public half: {public_hex}\n"
            f"  app expects:               {PUBLIC_KEY_HEX}\n"
            "Keys made with it would be rejected by the app."
        )
    return key


def init_keys() -> None:
    if PRIVATE_KEY_PATH.exists():
        sys.exit(f"Refusing to overwrite existing private key at {PRIVATE_KEY_PATH}. Delete it first if you really mean it.")
    key = Ed25519PrivateKey.generate()
    PRIVATE_KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
    PRIVATE_KEY_PATH.write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ))
    public_hex = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()
    print(f"Private key written to {PRIVATE_KEY_PATH}  (keep it secret, back it up)")
    print("Now set this in mall_audio/license.py and rebuild the app:")
    print(f'PUBLIC_KEY_HEX = "{public_hex}"')


def make_key(customer: str, expires: date | None, machine: str | None) -> str:
    payload = {"c": customer.strip(), "i": date.today().isoformat()}
    if expires is not None:
        payload["e"] = expires.isoformat()
    if machine:
        payload["m"] = machine
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    signature = load_private_key().sign(body)
    return f"{PREFIX}{_b64(body)}.{_b64(signature)}"


def describe(text: str) -> None:
    try:
        lic = parse_key(text)
    except LicenseError as exc:
        sys.exit(f"INVALID key ({exc.code})")
    print(f"Customer : {lic.customer}")
    print(f"Issued   : {lic.issued or '-'}")
    print(f"Expires  : {lic.expires or 'never (lifetime)'}")
    print(f"Machine  : {lic.machine or 'any computer'}")
    if lic.expires and lic.expires < date.today():
        print("Status   : EXPIRED")
    else:
        print("Status   : genuine")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--customer", help="Customer or site name shown in the app's Settings page")
    parser.add_argument("--expires", type=date.fromisoformat, help="Last valid day, YYYY-MM-DD. Omit for a lifetime licence")
    parser.add_argument("--machine", help="Lock to one computer: the machine ID shown in the activation window (XXXX-XXXX-XXXX)")
    parser.add_argument("--out", type=Path, help="Also write the key to this file")
    parser.add_argument("--verify", metavar="KEY", help="Decode and check an existing key instead of making one")
    parser.add_argument("--init", action="store_true", help="Create a new key pair (first-time developer setup)")
    args = parser.parse_args()

    if args.init:
        init_keys()
        return
    if args.verify:
        describe(args.verify)
        return
    if not args.customer:
        parser.error("--customer is required (or use --verify / --init)")
    machine = args.machine.strip().upper() if args.machine else None
    if machine and not MACHINE_RE.match(machine):
        parser.error("--machine must look like 1A2B-3C4D-5E6F, exactly as the app shows it")
    if args.expires and args.expires < date.today():
        parser.error("--expires is already in the past")

    key = make_key(args.customer, args.expires, machine)
    print(key)
    print()
    describe(key)
    if args.out:
        args.out.write_text(key + "\n", encoding="utf-8")
        print(f"\nSaved to {args.out}")


if __name__ == "__main__":
    main()
