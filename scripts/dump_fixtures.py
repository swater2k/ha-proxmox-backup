#!/usr/bin/env python3
"""Dump raw PBS API responses into tests/fixtures/.

Standalone by design: only the Python standard library, so it can be run
directly on any machine that reaches the PBS host (including the PBS host
itself). The captured JSON is what the unit tests replay later, so it must be
the *raw* API payload, not a prettified or reduced version.

Usage:
    python3 scripts/dump_fixtures.py \
        --host 192.168.178.138 --token-id 'ha@pbs!homeassistant' \
        --token-secret 'xxxxxxxx-xxxx-...' --insecure

Endpoints that fail (most often 403 because of a missing ACL) are not fatal:
they are recorded in _errors.json so the integration can be taught to degrade
gracefully for exactly those cases.
"""

from __future__ import annotations

import argparse
import json
import re
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

# Values that identify the machine or leak secrets. Replaced before writing.
REDACT_KEYS = {
    "fingerprint",
    "serial",
    "devpath",
    "wwn",
    "san",
    "subject",
    "issuer",
    "pem",
    "email",
    "mail",
    "key",
    "publickey",
    "sockets",
}

MAC_RE = re.compile(r"\b([0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}\b")


def redact(value: Any, enabled: bool) -> Any:
    """Recursively blank out machine identifying values."""
    if not enabled:
        return value
    if isinstance(value, dict):
        return {
            key: ("<redacted>" if key.lower() in REDACT_KEYS else redact(val, enabled))
            for key, val in value.items()
        }
    if isinstance(value, list):
        return [redact(item, enabled) for item in value]
    if isinstance(value, str):
        return MAC_RE.sub("<redacted>", value)
    return value


class Dumper:
    """Small synchronous PBS API caller."""

    def __init__(self, args: argparse.Namespace) -> None:
        self.base = f"https://{args.host}:{args.port}/api2/json"
        self.headers = {
            "Authorization": f"PBSAPIToken={args.token_id}:{args.token_secret}"
        }
        self.context = ssl.create_default_context()
        if args.insecure:
            self.context.check_hostname = False
            self.context.verify_mode = ssl.CERT_NONE
        self.out = Path(args.out)
        self.out.mkdir(parents=True, exist_ok=True)
        self.redact = not args.no_redact
        self.errors: dict[str, str] = {}

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = f"{self.base}{path}"
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        request = urllib.request.Request(url, headers=self.headers)
        with urllib.request.urlopen(
            request, timeout=30, context=self.context
        ) as response:
            return json.loads(response.read().decode())

    def dump(self, name: str, path: str, params: dict[str, Any] | None = None) -> Any:
        """Fetch one endpoint and write it to <name>.json."""
        try:
            payload = self.get(path, params)
        except urllib.error.HTTPError as err:
            body = err.read().decode(errors="replace")[:300]
            self.errors[name] = f"HTTP {err.code} {path} :: {body}"
            print(f"  !! {name}: HTTP {err.code}")
            return None
        except Exception as err:
            self.errors[name] = f"{type(err).__name__} {path} :: {err}"
            print(f"  !! {name}: {type(err).__name__}: {err}")
            return None

        target = self.out / f"{name}.json"
        target.write_text(
            json.dumps(redact(payload, self.redact), indent=2, sort_keys=True) + "\n"
        )
        data = payload.get("data") if isinstance(payload, dict) else None
        count = len(data) if isinstance(data, list) else ""
        print(f"  ok {name}{f' ({count} entries)' if count != '' else ''}")
        return data

    def run(self, node: str, only_store: str | None) -> int:
        print("node level")
        self.dump("ping", "/ping")
        self.dump("version", "/version")
        self.dump("node_status", f"/nodes/{node}/status")
        self.dump("node_services", f"/nodes/{node}/services")
        self.dump("node_subscription", f"/nodes/{node}/subscription")
        self.dump("node_apt_update", f"/nodes/{node}/apt/update")
        self.dump("node_certificates", f"/nodes/{node}/certificates/info")
        self.dump("node_disks", f"/nodes/{node}/disks/list", {"include-partitions": 0})
        self.dump("node_tasks", f"/nodes/{node}/tasks", {"limit": 50})
        self.dump(
            "node_tasks_errors", f"/nodes/{node}/tasks", {"limit": 50, "errors": 1}
        )
        self.dump("node_tasks_running", f"/nodes/{node}/tasks", {"running": 1})

        print("datastore level")
        self.dump("datastore_usage", "/status/datastore-usage")
        stores = self.dump("datastore_list", "/admin/datastore") or []
        self.dump("datastore_config", "/config/datastore")

        print("jobs")
        for kind in ("prune", "verify", "sync"):
            self.dump(f"job_{kind}", f"/admin/{kind}")

        names = [entry["store"] for entry in stores if isinstance(entry, dict)]
        if only_store:
            names = [name for name in names if name == only_store]

        for store in names:
            print(f"store {store}")
            safe = re.sub(r"[^a-zA-Z0-9_-]", "_", store)
            base = f"/admin/datastore/{urllib.parse.quote(store)}"
            self.dump(f"store_{safe}_status", f"{base}/status", {"verbose": 1})
            self.dump(f"store_{safe}_gc", f"{base}/gc")
            self.dump(f"store_{safe}_groups", f"{base}/groups")
            self.dump(f"store_{safe}_snapshots", f"{base}/snapshots")
            self.dump(f"store_{safe}_namespaces", f"{base}/namespace")
            self.dump(f"store_{safe}_active_operations", f"{base}/active-operations")

        (self.out / "_errors.json").write_text(
            json.dumps(self.errors, indent=2, sort_keys=True) + "\n"
        )
        if self.errors:
            print(f"\n{len(self.errors)} endpoint(s) failed, see _errors.json")
            print("403 usually means the ACL is missing on the user *and* the token.")
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, default=8007)
    parser.add_argument("--token-id", required=True, help="e.g. ha@pbs!homeassistant")
    parser.add_argument("--token-secret", required=True)
    parser.add_argument("--node", default="localhost")
    parser.add_argument("--store", help="limit per-store dumps to this datastore")
    parser.add_argument("--out", default="tests/fixtures")
    parser.add_argument(
        "--insecure", action="store_true", help="skip TLS verification (self-signed)"
    )
    parser.add_argument(
        "--no-redact", action="store_true", help="keep serials and fingerprints"
    )
    args = parser.parse_args()
    return Dumper(args).run(args.node, args.store)


if __name__ == "__main__":
    sys.exit(main())
