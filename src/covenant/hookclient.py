"""Fast Claude Code hook: forwards the hook event to a running `covenant daemon`.

Run as a plain file with `python -I -S hookclient.py ...`, so it uses only the standard
library and starts in a few hundredths of a second instead of loading Covenant (about half a
second). When no daemon answers, it runs the normal hook in a fresh process (the same checks,
just slower) and starts a daemon in the background for the next call. So a missing or broken
daemon never skips a check.

    python -I -S hookclient.py --repo <repo> --daemon-file <state>/daemon.json [--state-dir <state>] claude-hook
"""

from __future__ import annotations

import json
import socket
import sys

TIMEOUT = 300  # matches the hook timeout; a Stop check can run verifiers


def _args(argv: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    i = 0
    while i < len(argv):
        if argv[i] in ("--repo", "--daemon-file", "--state-dir") and i + 1 < len(argv):
            out[argv[i][2:]] = argv[i + 1]
            i += 2
        else:
            i += 1
    return out


def ask_daemon(daemon_file: str, body: bytes) -> bytes | None:
    """The daemon's reply, or None when there is no daemon or it can't answer.

    Plain HTTP/1.0 over a socket: importing http.client alone costs more than the request.
    """
    try:
        with open(daemon_file, encoding="utf-8") as f:
            info = json.load(f)
        request = (
            f"POST /hook HTTP/1.0\r\nHost: 127.0.0.1\r\nAuthorization: Bearer {info['token']}\r\n"
            f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n"
        ).encode("ascii") + body
        with socket.create_connection(("127.0.0.1", int(info["port"])), timeout=TIMEOUT) as conn:
            conn.sendall(request)
            chunks = []
            while True:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
        head, _, payload = b"".join(chunks).partition(b"\r\n\r\n")
        status = head.split(b" ", 2)[1] if head.startswith(b"HTTP/") else b""
        return payload if status == b"200" else None
    except (OSError, ValueError, KeyError, IndexError):
        return None


def _covenant_args(opts: dict[str, str]) -> list[str]:
    args = [sys.executable, "-m", "covenant", "--repo", opts["repo"]]
    if opts.get("state-dir"):
        args += ["--state-dir", opts["state-dir"]]
    return args


def start_daemon(opts: dict[str, str]) -> None:
    import os
    import subprocess

    kwargs: dict = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    attempts = [{"start_new_session": True}]
    if os.name == "nt":
        # A hidden console (not a detached process), so the git commands the daemon runs share it
        # instead of each creating its own; break away from the hook's job so it outlives the hook.
        group_no_window = 0x00000200 | 0x08000000
        attempts = [{"creationflags": group_no_window | 0x01000000}, {"creationflags": group_no_window}]
    for extra in attempts:
        try:
            subprocess.Popen(_covenant_args(opts) + ["daemon"], close_fds=True, **kwargs, **extra)
            return
        except OSError:
            continue  # e.g. the job doesn't allow breakaway; the slow path still works regardless


def main(argv: list[str]) -> int:
    opts = _args(argv)
    body = sys.stdin.buffer.read()
    if opts.get("daemon-file"):
        try:
            reply = ask_daemon(opts["daemon-file"], body)
        except Exception:  # never skip a check because of the shortcut; take the full path below
            reply = None
        if reply is not None:
            text = reply.decode("utf-8").strip()
            if text and text != "{}":
                sys.stdout.write(text + "\n")
            return 0
    # No daemon: run the hook the normal way, then start a daemon for next time.
    import subprocess

    result = subprocess.run(_covenant_args(opts) + ["claude-hook"], input=body, capture_output=True)
    sys.stdout.buffer.write(result.stdout)
    sys.stderr.buffer.write(result.stderr)
    import os

    if opts.get("daemon-file") and not os.environ.get("COVENANT_NO_DAEMON"):
        start_daemon(opts)
    return result.returncode


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
