"""The sandbox's way out: an HTTP proxy that only reaches allowed hosts.

    python sandbox_proxy.py --port 3128 --allow api.anthropic.com --allow pypi.org ...

The agent's container has no route to the internet; this proxy (in its own container, on both
the private network and the outside one) is the only door. HTTPS goes through CONNECT, so the
proxy sees the host name but never the contents. Everything not on the list is refused with
403 and logged. Standard library only, so it runs in a bare python image.
"""

from __future__ import annotations

import argparse
import select
import socket
import sys
import threading
from datetime import datetime, timezone
from urllib.parse import urlparse


def allowed(host: str, allow: list[str]) -> bool:
    host = host.lower().rstrip(".")
    return any(host == a or host.endswith("." + a) for a in (x.lower().lstrip("*.").rstrip(".") for x in allow))


def _log(line: str) -> None:
    sys.stdout.write(f"{datetime.now(timezone.utc).isoformat()} {line}\n")
    sys.stdout.flush()


def _pipe(a: socket.socket, b: socket.socket) -> None:
    sockets = [a, b]
    try:
        while True:
            ready, _, broken = select.select(sockets, [], sockets, 300)
            if broken or not ready:
                return
            for s in ready:
                data = s.recv(65536)
                if not data:
                    return
                (b if s is a else a).sendall(data)
    except OSError:
        return


def handle(client: socket.socket, allow: list[str]) -> None:
    try:
        head = b""
        while b"\r\n\r\n" not in head and len(head) < 65536:
            chunk = client.recv(4096)
            if not chunk:
                return
            head += chunk
        request_line = head.split(b"\r\n", 1)[0].decode("latin-1")
        method, target, _ = (request_line.split(" ") + ["", ""])[:3]
        if method.upper() == "CONNECT":
            host, _, port = target.rpartition(":")
            port_n = int(port or 443)
        else:
            url = urlparse(target)
            host, port_n = url.hostname or "", url.port or 80
        if not host or not allowed(host, allow):
            _log(f"DENY {method} {host}")
            client.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Type: text/plain\r\nConnection: close\r\n\r\n"
                           b"Covenant sandbox: this host isn't on the allowlist\n")
            return
        upstream = socket.create_connection((host, port_n), timeout=30)
        _log(f"ALLOW {method} {host}:{port_n}")
        if method.upper() == "CONNECT":
            client.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
            rest = head.split(b"\r\n\r\n", 1)[1]
        else:  # plain http: pass the request on with an origin-form path
            url = urlparse(target)
            path = (url.path or "/") + (f"?{url.query}" if url.query else "")
            rest = head.replace(target.encode("latin-1"), path.encode("latin-1"), 1)
        if rest:
            upstream.sendall(rest)
        _pipe(client, upstream)
        upstream.close()
    except (OSError, ValueError) as exc:
        _log(f"ERROR {type(exc).__name__}: {exc}")
    finally:
        try:
            client.close()
        except OSError:
            pass


def serve(port: int, allow: list[str], bind: str = "0.0.0.0", ready: threading.Event | None = None,
          stop: threading.Event | None = None) -> None:
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((bind, port))
    server.listen(64)
    server.settimeout(0.5)
    _log(f"Covenant sandbox proxy on {bind}:{server.getsockname()[1]}; allowed: {', '.join(allow) or 'nothing'}")
    if ready is not None:
        ready.port = server.getsockname()[1]  # type: ignore[attr-defined]
        ready.set()
    while stop is None or not stop.is_set():
        try:
            client, _ = server.accept()
        except socket.timeout:
            continue
        threading.Thread(target=handle, args=(client, allow), daemon=True).start()
    server.close()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--port", type=int, default=3128)
    p.add_argument("--bind", default="0.0.0.0")
    p.add_argument("--allow", action="append", default=[], help="host to allow (and its subdomains); repeatable")
    args = p.parse_args(argv)
    serve(args.port, args.allow, args.bind)
    return 0


if __name__ == "__main__":
    sys.exit(main())
