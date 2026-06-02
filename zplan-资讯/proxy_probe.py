#!/usr/bin/env python3
"""
Probe local HTTP/SOCKS proxies for reaching X API.
- Scans common ports plus all 127.0.0.1 TCP listeners (from lsof).
- If a working proxy is found, prints suggested .env lines (--write-env appends).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import sys
from typing import Iterable

import requests


def _tcp_open(host: str, port: int, timeout: float = 0.35) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def list_localhost_listen_ports() -> list[int]:
    """Parse lsof for 127.0.0.1:* (LISTEN)."""
    try:
        out = subprocess.check_output(
            ["lsof", "-nP", "-iTCP", "-sTCP:LISTEN"],
            timeout=12,
        ).decode("utf-8", errors="replace")
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        return []
    ports: set[int] = set()
    for line in out.splitlines():
        match = re.search(r"127\.0\.0\.1:(\d+)\s+\(LISTEN\)", line)
        if match:
            ports.add(int(match.group(1)))
    return sorted(ports)


def _probe_proxy(proxy_url: str, timeout: float = 7.0) -> dict:
    url = "https://api.twitter.com/2/tweets/search/recent"
    headers = {"Authorization": "Bearer dummy"}
    params = {"query": "test", "max_results": 10}
    proxies = {"http": proxy_url, "https": proxy_url}
    try:
        resp = requests.get(
            url,
            headers=headers,
            params=params,
            proxies=proxies,
            timeout=timeout,
        )
        return {
            "proxy": proxy_url,
            "ok": True,
            "status_code": resp.status_code,
            "reachable": True,
        }
    except requests.exceptions.ProxyError as exc:
        return {"proxy": proxy_url, "ok": False, "reachable": False, "error": str(exc)[:200]}
    except requests.exceptions.RequestException as exc:
        return {"proxy": proxy_url, "ok": False, "reachable": False, "error": str(exc)[:200]}


def _candidate_ports() -> Iterable[int]:
    raw = os.getenv("PROBE_PORTS", "")
    if raw.strip():
        for part in raw.split(","):
            part = part.strip()
            if part.isdigit():
                yield int(part)
        return
    for p in (
        7897,
        7890,
        7891,
        7892,
        7893,
        9090,
        10808,
        1080,
        6152,
        20171,
        8889,
        8080,
        3128,
    ):
        yield p


def _merge_ports(include_lsof: bool) -> list[int]:
    host = "127.0.0.1"
    merged: set[int] = set()
    for p in _candidate_ports():
        if _tcp_open(host, p):
            merged.add(p)
    if include_lsof:
        for p in list_localhost_listen_ports():
            if _tcp_open(host, p):
                merged.add(p)
    return sorted(merged)


def run_probe(*, write_env: bool = False, no_lsof: bool = False) -> dict:
    host = "127.0.0.1"
    open_ports = _merge_ports(include_lsof=not no_lsof)
    lsof_ports = list_localhost_listen_ports() if not no_lsof else []

    results: list[dict] = []
    winner: str | None = None
    for port in open_ports:
        if winner is not None:
            break
        for scheme in ("http", "socks5h"):
            if scheme == "socks5h":
                try:
                    import socks  # noqa: F401  # PySocks
                except ImportError:
                    break
            proxy_url = f"{scheme}://{host}:{port}"
            row = _probe_proxy(proxy_url, timeout=7.0)
            row["port"] = port
            row["scheme"] = scheme
            results.append(row)
            if row.get("reachable"):
                winner = proxy_url
                break

    payload: dict = {
        "ok": bool(winner),
        "lsof_localhost_ports": lsof_ports,
        "open_ports_probed": open_ports,
        "results": results,
        "suggested_env": (
            {
                "HTTP_PROXY": winner,
                "HTTPS_PROXY": winner,
            }
            if winner
            else {}
        ),
    }

    if write_env and winner:
        env_path = os.path.join(os.path.dirname(__file__), ".env")
        if os.path.exists(env_path):
            existing = open(env_path, "r", encoding="utf-8").read()
            if "HTTPS_PROXY=" in existing or "HTTP_PROXY=" in existing:
                print("NOTE: .env already contains HTTP(S)_PROXY; not modified.", file=sys.stderr)
                return payload
        block = f"\n# auto-written by proxy_probe.py\nHTTP_PROXY={winner}\nHTTPS_PROXY={winner}\n"
        with open(env_path, "a", encoding="utf-8") as handle:
            handle.write(block)
        print(f"Appended proxy to {env_path}", file=sys.stderr)

    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description="Probe local proxy for X API")
    parser.add_argument(
        "--write-env",
        action="store_true",
        help="If a proxy works, append HTTP(S)_PROXY to .env (idempotent)",
    )
    parser.add_argument(
        "--no-lsof",
        action="store_true",
        help="Do not merge 127.0.0.1 listeners from lsof",
    )
    args = parser.parse_args()
    payload = run_probe(write_env=args.write_env, no_lsof=args.no_lsof)
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
