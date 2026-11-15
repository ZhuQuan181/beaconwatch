#!/usr/bin/env python3
"""Generate a high-entropy BeaconWatch API key and its key-file entry."""
from __future__ import annotations

import argparse
import hashlib
import json
import secrets
import uuid


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Create a named API key. Store the printed secret in the "
            "client's secret manager; only the digest belongs in the "
            "server key file."
        )
    )
    parser.add_argument("--subject", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument(
        "--role",
        action="append",
        choices=["viewer", "operator", "admin"],
        default=[],
    )
    args = parser.parse_args()
    roles = args.role or ["viewer"]
    api_key = "bw_" + secrets.token_urlsafe(32)
    entry = {
        "key_id": uuid.uuid4().hex[:12],
        "subject": args.subject,
        "display_name": args.name,
        "roles": roles,
        "api_key_sha256": hashlib.sha256(api_key.encode()).hexdigest(),
        "enabled": True,
    }
    print(f"API_KEY={api_key}")
    print("KEY_ENTRY=" + json.dumps(entry, separators=(",", ":")))


if __name__ == "__main__":
    main()
