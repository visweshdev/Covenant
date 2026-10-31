"""Alerts: tell a person when an agent needs a human, or (optionally) whenever it is blocked.

Configure in ~/.covenant/alerts.yaml (or with environment variables, which win):

    slack_webhook: https://hooks.slack.com/services/...     # COVENANT_SLACK_WEBHOOK
    webhook: https://example.com/covenant                   # COVENANT_ALERT_WEBHOOK (JSON POST)
    email:                                                  # COVENANT_ALERT_EMAIL (the "to" address)
      to: you@example.com
      from: covenant@example.com
      host: smtp.example.com
      port: 587
      user: covenant@example.com
      password_env: COVENANT_SMTP_PASSWORD                  # the name of a variable, never the password
    events: [escalated]          # add "blocked" to hear about every block, "denied" for refused commands

The same problem alerts at most once every 10 minutes. Sending never stops or slows a check
for long: each channel gets a few seconds and failures are only noted in alerts.log.
`covenant alerts test` sends a test message.
"""

from __future__ import annotations

import json
import os
import smtplib
import ssl
import urllib.request
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import yaml

from .paths import covenant_home

QUIET_SECONDS = 600
TIMEOUT = 4


def config_path() -> Path:
    return covenant_home() / "alerts.yaml"


def load_config() -> dict[str, Any]:
    cfg: dict[str, Any] = {}
    try:
        cfg = yaml.safe_load(config_path().read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        cfg = {}
    if not isinstance(cfg, dict):
        cfg = {}
    if os.environ.get("COVENANT_SLACK_WEBHOOK"):
        cfg["slack_webhook"] = os.environ["COVENANT_SLACK_WEBHOOK"]
    if os.environ.get("COVENANT_ALERT_WEBHOOK"):
        cfg["webhook"] = os.environ["COVENANT_ALERT_WEBHOOK"]
    if os.environ.get("COVENANT_ALERT_EMAIL"):
        cfg.setdefault("email", {})["to"] = os.environ["COVENANT_ALERT_EMAIL"]
    cfg.setdefault("events", ["escalated"])
    return cfg


def configured(cfg: dict[str, Any] | None = None) -> bool:
    cfg = load_config() if cfg is None else cfg
    return bool(cfg.get("slack_webhook") or cfg.get("webhook") or (cfg.get("email") or {}).get("to"))


def describe(cfg: dict[str, Any] | None = None) -> str:
    cfg = load_config() if cfg is None else cfg
    lines = []
    if cfg.get("slack_webhook"):
        lines.append(f"Slack: {cfg['slack_webhook'][:34]}…")
    if cfg.get("webhook"):
        lines.append(f"Webhook: {cfg['webhook']}")
    if (cfg.get("email") or {}).get("to"):
        lines.append(f"Email: {cfg['email']['to']} via {cfg['email'].get('host', 'localhost')}")
    lines.append(f"Alerts on: {', '.join(cfg.get('events', []))}")
    return "\n".join(lines if len(lines) > 1 else ["No alerts configured; see `covenant alerts --help`."])


def message(repo: Path, event: str, reports: list[Any]) -> str:
    head = {
        "escalated": f"Covenant: the agent in {repo.name} needs a human",
        "blocked": f"Covenant: the agent in {repo.name} was blocked",
        "denied": f"Covenant: the agent in {repo.name} tried something it isn't allowed to",
        "test": f"Covenant: test alert from {repo.name}",
    }.get(event, f"Covenant: {event} in {repo.name}")
    lines = [head]
    for r in reports[:5]:
        evidence = r.evidence if hasattr(r, "evidence") else r.get("evidence", {})
        rule = r.clause if hasattr(r, "clause") else r.get("clause", "")
        rid = r.violation if hasattr(r, "violation") else r.get("violation", "")
        detail = evidence.get("what") or evidence.get("target") or evidence.get("symbol") or evidence.get("command") or ""
        lines.append(f"• {rid}: {rule}" + (f" ({str(detail)[:140]})" if detail else ""))
    lines.append(f"Repo: {repo}")
    lines.append("Run `covenant explain` in the repo for what happened and what can be done.")
    return "\n".join(lines)


def _post_json(url: str, data: dict[str, Any]) -> None:
    request = urllib.request.Request(url, data=json.dumps(data).encode("utf-8"),
                                     headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:  # noqa: S310 (a URL the user configured)
        response.read(1000)


def _send_email(cfg: dict[str, Any], subject: str, body: str) -> None:
    mail = EmailMessage()
    mail["Subject"] = subject
    mail["From"] = cfg.get("from") or cfg.get("user") or "covenant@localhost"
    mail["To"] = cfg["to"]
    mail.set_content(body)
    port = int(cfg.get("port", 587))
    with smtplib.SMTP(cfg.get("host", "localhost"), port, timeout=TIMEOUT) as smtp:
        if port != 25 and cfg.get("starttls", True):
            smtp.starttls(context=ssl.create_default_context())
        password = os.environ.get(cfg.get("password_env", "COVENANT_SMTP_PASSWORD"), "")
        if cfg.get("user") and password:
            smtp.login(cfg["user"], password)
        smtp.send_message(mail)


def _quiet(state_dir: Path, key: str) -> bool:
    """True if this alert went out in the last QUIET_SECONDS (and records it otherwise)."""
    path = state_dir / "alerts-sent.json"
    try:
        sent = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        sent = {}
    now = datetime.now(timezone.utc).timestamp()
    if now - sent.get(key, 0) < QUIET_SECONDS:
        return True
    sent[key] = now
    sent = {k: v for k, v in sent.items() if now - v < QUIET_SECONDS * 6}
    state_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(sent), encoding="utf-8")
    return False


def send(repo: Path, state_dir: Path, event: str, reports: list[Any], cfg: dict[str, Any] | None = None,
         force: bool = False) -> list[str]:
    """Send an alert on every configured channel. Returns the channels that worked."""
    cfg = load_config() if cfg is None else cfg
    if not configured(cfg) or (event not in cfg.get("events", []) and not force):
        return []
    key = event + ":" + ",".join(sorted({getattr(r, "violation", "") for r in reports}))
    if not force and _quiet(state_dir, key):
        return []
    text = message(repo, event, reports)
    done, errors = [], []
    channels = []
    if cfg.get("slack_webhook"):
        channels.append(("slack", lambda: _post_json(cfg["slack_webhook"], {"text": text})))
    if cfg.get("webhook"):
        payload = {"text": text, "event": event, "repo": str(repo),
                   "reports": [r.model_dump(mode="json") if hasattr(r, "model_dump") else r for r in reports]}
        channels.append(("webhook", lambda: _post_json(cfg["webhook"], payload)))
    if (cfg.get("email") or {}).get("to"):
        channels.append(("email", lambda: _send_email(cfg["email"], text.splitlines()[0], text)))
    for name, run in channels:
        try:
            run()
            done.append(name)
        except Exception as exc:  # an alert must never break a check
            errors.append(f"{name}: {type(exc).__name__}: {exc}")
    if errors:
        try:
            with (state_dir / "alerts.log").open("a", encoding="utf-8") as f:
                for e in errors:
                    f.write(f"{datetime.now(timezone.utc).isoformat()} {e}\n")
        except OSError:
            pass
    return done
