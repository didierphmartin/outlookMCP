"""Live check (L0) of the organization connection: python org_check.py <vip-address> <non-vip-address>

Reads .env next to this file the way run.sh does, gets an app-only token, lists the group's members, reads
the VIP's mail folders and tries the non-VIP's. Prints only fixed status lines (never the secret or a token).
Exit 0 only when the VIP is readable and the non-VIP is refused.
"""
import os
import sys
from pathlib import Path

import httpx

from config import AppConfig
from graph import AppTokenSource, GraphClient, GraphError


def load_env(path: Path) -> None:
    """KEY=value, optionally quoted; blank lines and # comments skipped; variables already set win (as run.sh)."""
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        if not line.strip() or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = "".join(key.split())
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        if key and key not in os.environ:
            os.environ[key] = val


def _folders(graph: GraphClient, address: str) -> str:
    """'ok' when the mailbox's folders can be read, else the error code (a short fixed word)."""
    try:
        graph.get(f"/users/{address}/mailFolders", {"$top": "1"})
        return "ok"
    except GraphError as e:
        return e.code


def main(argv: list[str], http: httpx.Client | None = None, cfg: AppConfig | None = None) -> int:
    if len(argv) != 2:
        print("usage: python org_check.py <vip-address> <non-vip-address>")
        return 2
    vip, other = argv
    if cfg is None:
        load_env(Path(__file__).resolve().parent / ".env")
        cfg = AppConfig.from_env()
    if not cfg.org_enabled:
        print("token: not configured (set the four OUTLOOK_ORG_* variables in .env)")
        return 1
    http = http or httpx.Client()
    tokens = AppTokenSource(http, cfg)
    graph = GraphClient(http, cfg.graph_base, tokens)
    try:
        tokens.access_token()
    except GraphError as e:
        print(f"token: {e.code}")
        return 1
    print("token: ok")
    try:
        n, url, params = 0, f"/groups/{cfg.org_group}/transitiveMembers/microsoft.graph.user", {"$select": "id"}
        while url:
            page = graph.get(url, params)
            n += len(page.get("value", []))
            url, params = page.get("@odata.nextLink"), None
        print(f"group: {n} members")
    except GraphError as e:
        print(f"group: {e.code}")
        return 1
    vip_state = _folders(graph, vip)
    print(f"vip: {vip_state}")
    other_state = _folders(graph, other)
    print("non-vip: " + ("READABLE (scope not applied)" if other_state == "ok" else
                         "refused" if other_state == "forbidden" else other_state))
    return 0 if vip_state == "ok" and other_state == "forbidden" else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
