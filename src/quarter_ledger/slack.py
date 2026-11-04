"""Approvals in Slack: a message with Approve and Deny buttons, and the checks on a click.

Slack signs every request it sends: X-Slack-Signature is "v0=" plus the hex HMAC-SHA256,
keyed with the app's signing secret, of "v0:{X-Slack-Request-Timestamp}:{raw body}".
A click arrives form-encoded, with the block_actions JSON in the "payload" field.
"""

import hashlib
import hmac
import json
import time
import urllib.request
from dataclasses import dataclass, field

MAX_AGE = 300  # seconds; an older signed request may be a replay


@dataclass
class Slack:
    token: str = field(repr=False)  # a bot token with chat:write
    signing_secret: str = field(repr=False)
    channel: str
    users: dict[str, str]  # Slack user id: the person recorded as approver
    api: str = "https://slack.com/api"

    @classmethod
    def from_env(cls, environ) -> "Slack | None":
        """Set up when QUARTER_SLACK_BOT_TOKEN, _SIGNING_SECRET and _CHANNEL are all set."""
        names = ("BOT_TOKEN", "SIGNING_SECRET", "CHANNEL")
        values = [environ.get(f"QUARTER_SLACK_{n}") for n in names]
        if not all(values):
            return None
        return cls(*values, json.loads(environ.get("QUARTER_SLACK_USERS") or "{}"))

    def verified(self, timestamp: str | None, signature: str | None, body: bytes) -> bool:
        try:
            if abs(time.time() - int(timestamp or "")) > MAX_AGE:
                return False
        except ValueError:
            return False
        base = b"v0:" + timestamp.encode() + b":" + body
        expected = "v0=" + hmac.new(self.signing_secret.encode(), base, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected.encode(), (signature or "").encode())

    def post_approval(self, text: str, approval_id: str) -> tuple[str, str]:
        """Post the message; its channel id and ts, which chat.update needs later."""
        buttons = [
            {"type": "button", "text": {"type": "plain_text", "text": "Approve"},
             "style": "primary", "action_id": "approve", "value": approval_id},
            {"type": "button", "text": {"type": "plain_text", "text": "Deny"},
             "style": "danger", "action_id": "deny", "value": approval_id},
        ]
        reply = self.call("chat.postMessage", {
            "channel": self.channel,
            "text": escape(text),
            "blocks": [section(text), {"type": "actions", "elements": buttons}],
        })
        return reply["channel"], reply["ts"]

    def show_decision(self, channel: str, ts: str, text: str, outcome: str) -> None:
        """Replace the buttons with who decided."""
        self.call("chat.update", {
            "channel": channel, "ts": ts, "text": escape(f"{text}\n{outcome}"),
            "blocks": [section(text), section(outcome)],
        })

    def tell(self, channel: str, user: str, text: str) -> None:
        """A note only the person who clicked can see."""
        self.call("chat.postEphemeral", {"channel": channel, "user": user, "text": escape(text)})

    def call(self, method: str, payload: dict) -> dict:
        req = urllib.request.Request(
            f"{self.api}/{method}",
            data=json.dumps(payload).encode(),
            headers={
                "content-type": "application/json; charset=utf-8",
                "authorization": f"Bearer {self.token}",
            },
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            reply = json.loads(resp.read())
        if not reply.get("ok"):
            raise OSError(f"slack {method}: {reply.get('error')}")
        return reply


def escape(text: str) -> str:
    """Agent names come from request headers; escaping stops them pinging @channel."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def section(text: str) -> dict:
    return {"type": "section", "text": {"type": "mrkdwn", "text": escape(text)}}
