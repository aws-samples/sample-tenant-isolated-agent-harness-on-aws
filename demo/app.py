# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Local demo page for the deployed split harness.

    python -m demo.app --profile <profile>        # then open http://127.0.0.1:8770/

The page drives AWS with the operator's own credentials, so it serves the person at this
machine and nobody else:

* it binds to 127.0.0.1 only (there is no option to change that);
* it answers only requests whose Host header is this server, which stops DNS-rebinding pages;
* POSTs must be JSON and may not come from another origin, so other pages open in the same
  browser cannot drive it;
* ID tokens and passwords stay in this process; the page only sees API answers.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from demo import checks, cost_model, measured, security  # noqa: E402
from demo.client import STACK_NAME, TENANTS, Demo  # noqa: E402
from shared import tenancy  # noqa: E402

BIND = "127.0.0.1"
HERE = Path(__file__).resolve().parent
STATIC = {"/": ("index.html", "text/html; charset=utf-8"), "/app.js": ("app.js", "text/javascript; charset=utf-8")}
MAX_BODY = 16 * 1024
MAX_MESSAGE = 4000
CSP = ("default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'; "
       "img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
# The diagrams are PNG renders (the JSON sources are in docs/diagrams/src). They are served as
# images under the page's own CSP, so there is no third-party script or font to sandbox.
DIAGRAM_DIR = ROOT / "docs" / "diagrams"
DIAGRAMS = {
    "split-harness": (
        "Split agent harness with tenant isolation",
        "Architecture. The shared worker sends routine events to Gateway tools and starts the tenant's runtime only "
        "for code. Every hop is labelled with its isolation control.",
    ),
    "turn-isolation": (
        "One turn: every tenant check in order",
        "Sequence. Submit, start the turn, a routine tool call, a code task and the answer, with each refusal on "
        "the hop that enforces it.",
    ),
    "billed-time": (
        "Billed compute for one event",
        "Waterfall. One event all on AgentCore bills a 305-second microVM window; in the split a routine event "
        "bills 5 seconds of shared Lambda, and only code events start a microVM.",
    ),
}
LOG = logging.getLogger("demo")


class BadRequest(ValueError):
    pass


class State:
    """Creates the Demo on first use, so the page loads (and shows the error) before the stack exists."""

    def __init__(self, args: argparse.Namespace, factory: Callable[[], Any] | None = None):
        self.args = args
        self._factory = factory or (lambda: Demo(args.profile, args.region, args.stack))
        self._demo: Any = None
        self._lock = threading.Lock()

    def demo(self) -> Any:
        with self._lock:
            if self._demo is None:
                self._demo = self._factory()  # not cached on failure: retried on the next request
            return self._demo


def _tenant(value: Any) -> str:
    if value not in TENANTS:
        raise BadRequest(f"tenant must be one of {sorted(TENANTS)}")
    return value


class Handler(BaseHTTPRequestHandler):
    server_version = "split-harness-demo"
    state: State
    allowed_hosts: frozenset[str] = frozenset()

    # ------------------------------------------------------------------ plumbing
    def _send(self, status: int, body: Any, content_type: str = "application/json", csp: str = CSP) -> None:
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", csp)
        self.end_headers()
        self.wfile.write(data)

    def _host_ok(self) -> bool:
        return (self.headers.get("Host") or "") in self.allowed_hosts

    def _origin_ok(self) -> bool:
        origin = self.headers.get("Origin")
        return origin is None or origin in {f"http://{host}" for host in self.allowed_hosts}

    def _fetch_site_ok(self) -> bool:
        """Browsers label each request's initiator; refuse API reads started by another site's page."""
        return (self.headers.get("Sec-Fetch-Site") or "same-origin").lower() in {"same-origin", "none"}

    def _api(self, fn: Callable[[], Any]) -> None:
        try:
            self._send(200, fn())
        except BadRequest as err:
            self._send(400, {"message": str(err)})
        except Exception as err:  # noqa: BLE001 - AWS and stack errors are shown on the page
            LOG.exception("demo request failed")
            self._send(502, {"message": f"{type(err).__name__}: {err}"[:600]})

    def log_message(self, fmt: str, *args: Any) -> None:
        LOG.info("%s %s", self.address_string(), fmt % args)

    # ------------------------------------------------------------------ routes
    def do_GET(self) -> None:  # noqa: N802
        if not self._host_ok():
            return self._send(421, {"message": "unexpected Host header"})
        url = urlparse(self.path)
        if url.path in STATIC:
            name, content_type = STATIC[url.path]
            return self._send(200, (HERE / name).read_bytes(), content_type)
        if url.path.startswith("/diagrams/"):
            slug = url.path[len("/diagrams/"):].removesuffix(".png")
            page = DIAGRAM_DIR / f"{slug}.png" if slug in DIAGRAMS else None
            if page is None or not url.path.endswith(".png") or not page.is_file():
                return self._send(404, {"message": "not found"})
            return self._send(200, page.read_bytes(), "image/png")
        if url.path.startswith("/api/") and not self._fetch_site_ok():
            return self._send(403, {"message": "cross-site request refused"})
        if url.path == "/api/info":
            return self._api(self._info)
        if url.path == "/api/turn":
            query = parse_qs(url.query)
            return self._api(lambda: self._get_turn(query.get("tenant", [""])[0], query.get("id", [""])[0]))
        if url.path == "/api/measured":
            query = parse_qs(url.query)
            return self._api(lambda: self._measured(query.get("hours", ["24"])[0]))
        return self._send(404, {"message": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if not self._host_ok():
            return self._send(421, {"message": "unexpected Host header"})
        if not self._origin_ok():
            return self._send(403, {"message": "cross-origin request refused"})
        if (self.headers.get("Content-Type") or "").split(";")[0].strip().lower() != "application/json":
            return self._send(415, {"message": "send application/json"})
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            return self._send(400, {"message": "bad Content-Length"})
        if not 0 <= length <= MAX_BODY:
            return self._send(413, {"message": "request too large"})
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self._send(400, {"message": "body is not JSON"})
        if not isinstance(body, dict):
            return self._send(400, {"message": "body must be an object"})
        routes: dict[str, Callable[[], Any]] = {
            "/api/setup": lambda: {"seeded": self.state.demo().setup()},
            "/api/turn": lambda: self._post_turn(body),
            "/api/check": lambda: self._check(body),
            "/api/cost": lambda: self._cost(body),
        }
        path = urlparse(self.path).path
        if path not in routes:
            return self._send(404, {"message": "not found"})
        return self._api(routes[path])

    # ------------------------------------------------------------------ actions
    def _info(self) -> dict:
        demo = self.state.demo()
        return {
            "region": demo.region, "stack": self.state.args.stack,
            "outputs": {key: demo.outputs.get(key) for key in ("ApiUrl", "GatewayUrl", "RuntimeArn")},
            "tenants": [{"id": tenant, "label": spec["label"], "storageId": Demo.storage_id(tenant),
                         "jobs": [job_id for job_id, _, _ in spec["jobs"]]} for tenant, spec in TENANTS.items()],
            "checks": list(checks.CHECKS),
            "requirements": security.REQUIREMENTS,
            "notYetLive": security.NOT_YET_LIVE,
            "diagrams": [{"title": title, "summary": summary, "path": f"/diagrams/{slug}.png"}
                         for slug, (title, summary) in DIAGRAMS.items() if (DIAGRAM_DIR / f"{slug}.png").is_file()],
        }

    def _post_turn(self, body: dict) -> dict:
        tenant = _tenant(body.get("tenant"))
        message = body.get("message")
        if not isinstance(message, str) or not message.strip() or len(message) > MAX_MESSAGE:
            raise BadRequest(f"message must be 1 to {MAX_MESSAGE} characters")
        status, result = self.state.demo().submit(tenant, message.strip())
        return {"status": status, "body": result}

    def _get_turn(self, tenant: str, request_id: str) -> dict:
        tenant = _tenant(tenant)
        if not tenancy.REQUEST_ID_RE.match(request_id or ""):
            raise BadRequest("invalid request id")
        status, result = self.state.demo().turn(tenant, request_id)
        return {"status": status, "body": result}

    def _check(self, body: dict) -> dict:
        check_id = body.get("id")
        if check_id not in checks.CHECKS:
            raise BadRequest(f"check must be one of {list(checks.CHECKS)}")
        return checks.run(self.state.demo(), check_id)

    def _measured(self, hours: str) -> dict:
        """Read-only usage of the deployed stack over the last ``hours``, priced at Sydney list rates."""
        try:
            window = measured.parse_hours(hours)
        except measured.MeasureError as err:
            raise BadRequest(str(err)) from err
        demo = self.state.demo()
        return measured.measure(demo.client, demo.outputs, self.state.args.stack, hours=window)

    @staticmethod
    def _cost(body: dict) -> dict:
        """Both designs' monthly cost for the posted assumptions; {} returns the defaults."""
        try:
            return cost_model.compute(body)
        except cost_model.CostInputError as err:
            raise BadRequest(str(err)) from err


def make_server(args: argparse.Namespace, factory: Callable[[], Any] | None = None) -> ThreadingHTTPServer:
    handler = type("DemoHandler", (Handler,), {"state": State(args, factory)})
    server = ThreadingHTTPServer((BIND, args.port), handler)
    server.daemon_threads = True
    port = server.server_address[1]
    handler.allowed_hosts = frozenset({f"127.0.0.1:{port}", f"localhost:{port}"})
    return server


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Local demo page for the deployed split harness (127.0.0.1 only).")
    parser.add_argument("--profile", help="AWS profile for the account with the stack (default: the SDK's usual chain)")
    parser.add_argument("--region", default="ap-southeast-2")
    parser.add_argument("--stack", default=STACK_NAME)
    parser.add_argument("--port", type=int, default=8770)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    server = make_server(parse_args(argv))
    print(f"Demo page: http://127.0.0.1:{server.server_address[1]}/  (Ctrl+C to stop)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
