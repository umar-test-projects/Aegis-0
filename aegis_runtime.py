"""Synthetic local services with real HTTP execution and measured observations.

This is a loopback development simulator, not a production routing API. Route
receipts survive restarts when state_path is supplied. Never expose it publicly.
"""

import argparse
import hmac
import ipaddress
import json
import math
import os
from pathlib import Path
import secrets
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


PRIMARY = "svc_eu_gateway_primary"
GATEWAYS = {PRIMARY: 320, "svc_us_failover_fast": 10, "svc_eu_zurich_dr": 25}


class RuntimeControlError(ValueError):
    pass


class RuntimeConflict(RuntimeControlError):
    pass


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _opener():
    return build_opener(ProxyHandler({}), _NoRedirect())


class RuntimeClient:
    def __init__(self, url, token):
        parsed = urlsplit(url)
        try:
            loopback = ipaddress.ip_address(parsed.hostname or "").is_loopback
        except ValueError:
            loopback = False
        if (not loopback or parsed.scheme != "http" or parsed.path not in ("", "/")
                or parsed.query or parsed.fragment or parsed.username or parsed.password):
            raise ValueError("Runtime endpoint must be a literal loopback HTTP origin")
        if not token:
            raise ValueError("Runtime control token is required")
        self.url, self.token = url.rstrip("/"), token

    def _request(self, path, data=None):
        headers = {}
        if data is not None:
            headers = {"Content-Type": "application/json", "Authorization": f"Bearer {self.token}"}
        request = Request(self.url + path, headers=headers,
                          data=None if data is None else json.dumps(data, allow_nan=False).encode())
        try:
            with _opener().open(request, timeout=3) as response:
                return json.load(response)
        except HTTPError as error:
            detail = json.loads(error.read()).get("error", "Runtime request failed")
            if error.code == 409:
                raise RuntimeConflict(detail) from error
            raise RuntimeControlError(detail) from error

    def snapshot(self):
        return self._request("/status")

    def switch(self, action_id, target, expected_revision):
        return self._request("/control/route", {"action_id": action_id, "target": target,
                                               "expected_revision": expected_revision})

    def receipt(self, action_id):
        return self._request("/control/receipt", {"action_id": action_id})["receipt"]

    def set_fault(self, service_id, latency_ms, fail=False):
        return self._request("/control/fault", {"service_id": service_id,
                                               "latency_ms": latency_ms, "fail": fail})

    def measure(self, samples=3):
        if isinstance(samples, bool) or not isinstance(samples, int) or not 1 <= samples <= 20:
            raise ValueError("Sample count must be 1–20")
        observations = []
        for _ in range(samples):
            start = time.perf_counter()
            try:
                response = self._request("/request")
                observations.append({"ok": True, "service_id": response["service_id"],
                                     "revision": response["revision"],
                                     "latency_ms": (time.perf_counter() - start) * 1000})
            except (RuntimeControlError, URLError, TimeoutError, OSError) as error:
                observations.append({"ok": False, "latency_ms": (time.perf_counter() - start) * 1000,
                                     "error": str(error)})
        ordered = sorted(row["latency_ms"] for row in observations)
        return {"samples": observations, "sample_count": samples,
                "errors": sum(not row["ok"] for row in observations),
                "p95_ms": ordered[math.ceil(samples * .95) - 1],
                "service_ids": sorted({row["service_id"] for row in observations if row["ok"]}),
                "source": "loopback_http", "measured_at": time.time()}


class DemoRuntime:
    def __init__(self, state_path=None, port=0, token=None):
        self.state_path = Path(state_path) if state_path else None
        self.port, self.token = port, token or secrets.token_urlsafe(32)
        self.lock = threading.RLock()
        self.state = {"instance_id": secrets.token_hex(16), "target": PRIMARY, "revision": 0, "receipts": {},
                      "faults": {name: {"latency_ms": latency, "fail": False}
                                 for name, latency in GATEWAYS.items()}}
        if self.state_path and self.state_path.exists():
            self.state = json.loads(self.state_path.read_text(encoding="utf-8"))
            self.state.setdefault("instance_id", secrets.token_hex(16))
        self.servers, self.threads, self.gateway_urls = [], [], {}

    def _persist(self):
        if self.state_path:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(dir=self.state_path.parent, suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as output:
                    json.dump(self.state, output, allow_nan=False)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary, self.state_path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)

    def client(self):
        return RuntimeClient(self.url, self.token)

    def _start(self, handler, port=0):
        server = ThreadingHTTPServer(("127.0.0.1", port), handler)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .05}, daemon=True)
        thread.start()
        self.servers.append(server)
        self.threads.append(thread)
        return f"http://127.0.0.1:{server.server_port}"

    def __enter__(self):
        runtime = self
        self._persist()

        class Handler(BaseHTTPRequestHandler):
            # Headers and JSON bodies are separate small writes. Avoid delayed
            # ACK/Nagle interaction dominating the synthetic gateway latency.
            disable_nagle_algorithm = True

            def log_message(self, *args):
                pass

            def reply(self, status, data):
                encoded = json.dumps(data).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

        def gateway_handler(identity):
            class Gateway(Handler):
                def do_GET(self):
                    if self.path != "/request":
                        return self.reply(404, {"error": "Unknown path"})
                    with runtime.lock:
                        fault = dict(runtime.state["faults"][identity])
                    time.sleep(fault["latency_ms"] / 1000)
                    self.reply(503 if fault["fail"] else 200,
                               {"error": "Injected gateway failure"} if fault["fail"] else {"service_id": identity})
            return Gateway

        class Router(Handler):
            def do_GET(self):
                with runtime.lock:
                    target, revision = runtime.state["target"], runtime.state["revision"]
                if self.path == "/status":
                    return self.reply(200, {"target": target, "revision": revision,
                                            "instance_id": runtime.state["instance_id"],
                                            "execution_count": len(runtime.state["receipts"])})
                if self.path != "/request":
                    return self.reply(404, {"error": "Unknown path"})
                if target is None:
                    return self.reply(503, {"error": "Router disabled"})
                try:
                    with _opener().open(runtime.gateway_urls[target] + "/request", timeout=2.5) as result:
                        response = json.load(result)
                    self.reply(200, {**response, "revision": revision})
                except (URLError, TimeoutError, OSError):
                    self.reply(503, {"error": "Gateway unavailable"})

            def do_POST(self):
                authorization = self.headers.get("Authorization", "")
                if not hmac.compare_digest(authorization, "Bearer " + runtime.token):
                    return self.reply(401, {"error": "Control authorization required"})
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= 16384:
                        raise ValueError("Control body must be 1–16384 bytes")
                    data = json.loads(self.rfile.read(length))
                    if not isinstance(data, dict):
                        raise ValueError("Expected an object")
                    with runtime.lock:
                        if self.path == "/control/receipt":
                            action = data["action_id"]
                            if not isinstance(action, str) or not 1 <= len(action) <= 128:
                                raise ValueError("Invalid action ID")
                            return self.reply(200, {"receipt": runtime.state["receipts"].get(action)})
                        if self.path == "/control/route":
                            action, target, expected = data["action_id"], data["target"], data["expected_revision"]
                            if not isinstance(action, str) or not 1 <= len(action) <= 128:
                                raise ValueError("Invalid action ID")
                            if target is not None and target not in GATEWAYS:
                                raise ValueError("Unknown gateway")
                            if isinstance(expected, bool) or not isinstance(expected, int) or expected < 0:
                                raise ValueError("Invalid routing revision")
                            previous = runtime.state["receipts"].get(action)
                            if previous:
                                if previous["target"] != target or previous["expected_revision"] != expected:
                                    return self.reply(409, {"error": "Action ID reused with different input"})
                                return self.reply(200, previous)
                            if runtime.state["revision"] != expected:
                                return self.reply(409, {"error": "Routing revision changed"})
                            receipt = {"action_id": action, "target": target,
                                       "previous_target": runtime.state["target"], "expected_revision": expected,
                                       "revision": expected + 1, "applied_at": time.time()}
                            runtime.state["target"], runtime.state["revision"] = target, expected + 1
                            runtime.state["receipts"][action] = receipt
                            runtime._persist()
                            return self.reply(200, receipt)
                        if self.path == "/control/fault":
                            service, latency, fail = data["service_id"], data["latency_ms"], data["fail"]
                            if service not in GATEWAYS or isinstance(latency, bool) or not isinstance(latency, (float, int)):
                                raise ValueError("Invalid gateway or latency")
                            if not math.isfinite(latency) or not 0 <= latency <= 2000 or not isinstance(fail, bool):
                                raise ValueError("Latency must be finite, 0–2000 ms; fail must be boolean")
                            runtime.state["faults"][service] = {"latency_ms": latency, "fail": fail}
                            runtime._persist()
                            return self.reply(200, {"service_id": service, "latency_ms": latency, "fail": fail})
                    self.reply(404, {"error": "Unknown control"})
                except (ValueError, KeyError, TypeError):
                    self.reply(400, {"error": "Invalid runtime control request"})

        try:
            for identity in GATEWAYS:
                self.gateway_urls[identity] = self._start(gateway_handler(identity))
            self.url = self._start(Router, self.port)
            return self
        except Exception:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *args):
        for server in self.servers:
            server.shutdown()
            server.server_close()
        for thread in self.threads:
            thread.join(timeout=2)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--state", default="scratch/runtime.json")
    args = parser.parse_args()
    token = os.getenv("AEGIS_RUNTIME_TOKEN")
    if not token:
        parser.error("Set AEGIS_RUNTIME_TOKEN for authenticated local control")
    with DemoRuntime(args.state, args.port, token) as demo:
        print(f"Synthetic gateways and router listening at {demo.url}", flush=True)
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            pass
