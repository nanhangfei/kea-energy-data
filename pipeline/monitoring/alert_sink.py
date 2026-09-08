"""Where an alert goes when there is no Slack.

Stage 7's exit condition is that an injected defect "raises an alert". Proving
that needs somewhere the alert actually lands and can be read back -- a webhook
that posts into the void proves nothing, and `SLACK_WEBHOOK_URL` is empty in
.env because this project has no Slack workspace to post to.

So: a receiver that keeps what it is sent and hands it back.

    POST /            record an alert (any JSON body)
    GET  /alerts      everything recorded, newest first, as JSON
    POST /reset       forget everything -- used by assert_quality.sh

⚠️ THIS IS NOT A PRODUCTION ALERT PATH and is not pretending to be. It holds
alerts in memory, so a restart loses them, and it authenticates nobody. It
exists so "an alert fired" is a thing a test can assert on rather than a claim
in a document. When SLACK_WEBHOOK_URL is set, _kea_common.py's callback prefers
it and this becomes the fallback -- swapping in the real channel is a variable,
not a rewrite.
"""

from __future__ import annotations

import datetime as dt
import json
from http.server import BaseHTTPRequestHandler, HTTPServer

#: Newest first. Bounded, because a receiver that grows without limit is a
#: memory leak wearing a diagnostic's clothing.
ALERTS: list[dict] = []
MAX_ALERTS = 200


class Handler(BaseHTTPRequestHandler):
    def _json(self, code: int, payload) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802
        if self.path.rstrip("/") == "/reset":
            ALERTS.clear()
            return self._json(200, {"cleared": True})

        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        try:
            payload = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            # Grafana and Slack-shaped senders do not always send JSON. Keeping
            # the text beats rejecting the alert -- this is the last stop.
            payload = {"text": raw.decode("utf-8", "replace")}

        ALERTS.insert(0, {"received_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                          "source": self.path, "payload": payload})
        del ALERTS[MAX_ALERTS:]
        print(f"ALERT {self.path} {json.dumps(payload)[:400]}", flush=True)
        self._json(200, {"recorded": True, "total": len(ALERTS)})

    def do_GET(self) -> None:  # noqa: N802
        if self.path.rstrip("/") in ("/alerts", ""):
            return self._json(200, ALERTS)
        self._json(404, {"error": "try /alerts"})

    def log_message(self, *_args) -> None:
        """Silence the default per-request line; do_POST prints what matters."""


if __name__ == "__main__":
    print("alert-sink listening on :9099", flush=True)
    HTTPServer(("0.0.0.0", 9099), Handler).serve_forever()
