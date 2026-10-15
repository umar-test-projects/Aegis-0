"""Receive Slack policy threads or ingest dashboard simulations into FalkorDB."""

import argparse
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import logging
import os
import re
import time
import uuid

from aegis_falkordb_engine import AegisGraphEngine


AUTHORS = ("@marcus.compliance", "@sarah.chen")
ALIASES = {
    "svc_eu_zurich_dr": ["zurich", "zurich dr", "zurich dr node", "eu sovereign dr node"],
    "svc_core_ledger": ["ledger", "core ledger"],
    "svc_merchant_checkout": ["checkout", "checkout api"],
    "svc_us_failover_fast": ["us failover", "us failover node", "us east failover"],
    "svc_eu_gateway_primary": ["primary gateway", "eu gateway primary"],
    "pipeline_eu_payments": ["eu payments", "eu payment pipeline", "payment stream", "payments pipeline"],
}


def parse_slack_message(text, author, targets):
    """Resolve one target and an explicit freeze/resume directive without an LLM.

    IDs, graph names and known aliases are recognized. Ambiguous targets,
    negation, questions, or mixed actions must be rewritten explicitly.
    """
    if not isinstance(text, str) or not text.strip() or len(text) > 8000:
        raise ValueError("Enter a Slack message of 1–8000 characters.")
    text = text.strip()
    prefix = re.match(r"^(@[a-zA-Z0-9_.]+)\s*:?\s+", text)
    if prefix:
        if author and author != prefix[1]:
            raise ValueError("The message author does not match the selected Slack author.")
        author = prefix[1]
        text = text[prefix.end():]
    if author not in AUTHORS:
        raise ValueError("Slack author must be @marcus.compliance or @sarah.chen.")
    normalized = text.lower().replace("’", "'").replace("‘", "'")
    if "?" in text or re.search(r"\b(no|not|never|don't|do not|should|could|if|unless|except)\b", normalized):
        raise ValueError("Use an explicit freeze or resume directive without questions, exceptions or negation.")
    if not re.match(
        r"^(?:urgent\s*:\s*)?(?:please\s+)?"
        r"(?:freeze|stop|block|disable|suspend|halt|unfreeze|resume|unblock|reopen|enable|lift)\b",
        normalized,
    ):
        raise ValueError("Start the message with an explicit freeze or resume directive.")
    resume = bool(re.search(r"\b(unfreeze|resume|unblock|reopen|enable)\b|\blift\s+(?:the\s+)?freeze\b", normalized))
    stripped = re.sub(r"\blift\s+(?:the\s+)?freeze\b", "", normalized)
    freeze = bool(re.search(r"\b(freeze|stop|block|disable|suspend|halt)\b", stripped))
    if freeze == resume:
        raise ValueError("Specify one action: freeze/block/stop or resume/unfreeze/unblock traffic.")
    matches = {}
    for target in targets:
        aliases = [target["id"], target.get("name", "")] + ALIASES.get(target["id"], [])
        if any(alias and re.search(r"(?<!\w)" + re.escape(alias.lower()) + r"(?!\w)", normalized)
               for alias in aliases):
            matches[target["id"]] = target
    if len(matches) != 1:
        raise ValueError("Name exactly one existing Service or DataPipeline, using its ID or name (for example Zurich DR node).")
    target = next(iter(matches.values()))
    return {"author": author, "summary": text, "target_id": target["id"],
            "target_name": target.get("name", target["id"]),
            "traffic_blocked": freeze, "action": "FREEZE" if freeze else "RESUME"}


def ingest_slack_message(engine, text, author=None, *, event_id=None, source_url=None):
    """Atomically record the directive and its same-target supersession lineage.

    An event ID makes delivery retries idempotent. Existing non-EU restrictions
    are inherited, so resuming traffic never silently grants a regional waiver.
    """
    target_rows = engine.graph.ro_query("""
        MATCH (n) WHERE n:Service OR n:DataPipeline RETURN n.id, n.name
    """).result_set
    parsed = parse_slack_message(text, author, [{"id": row[0], "name": row[1]} for row in target_rows])
    if event_id is not None and (not isinstance(event_id, str) or not 1 <= len(event_id) <= 200):
        raise ValueError("Event ID must contain 1–200 characters.")
    event_id = event_id or uuid.uuid4().hex
    decision_id = "DEC-SLACK-" + hashlib.sha256(event_id.encode()).hexdigest()[:24].upper()
    payload_hash = hashlib.sha256(json.dumps(parsed, sort_keys=True).encode()).hexdigest()
    token = uuid.uuid4().hex
    now = int(time.time())
    result = engine.graph.query("""
        MATCH (target {id: $target_id}) WHERE target:Service OR target:DataPipeline
        MATCH (author:Person {slack_handle: $author})
        MERGE (decision:Decision {id: $decision_id})
        ON CREATE SET decision.summary = $summary, decision.timestamp = $ts,
            decision.active = true, decision.source_type = 'Slack_Thread',
            decision.source_url = $source_url, decision.raw_text = $raw_text,
            decision.target_id = $target_id, decision.traffic_blocked = $blocked,
            decision.author_handle = $author, decision.event_id = $event_id,
            decision.payload_hash = $payload_hash, decision.ingest_token = $token
        WITH target, author, decision WHERE decision.ingest_token = $token
        OPTIONAL MATCH (previous:Decision)-[:GOVERNS]->(target)
        WHERE NOT (previous)<-[:SUPERSEDES]-() AND previous.id <> decision.id
        WITH target, author, decision, collect(previous) AS prior,
             max(CASE WHEN previous.forbids_non_eu = true THEN 1 ELSE 0 END) AS regional_freeze
        SET decision.forbids_non_eu = (regional_freeze = 1)
        MERGE (author)-[:AUTHORED]->(decision)
        MERGE (decision)-[:GOVERNS]->(target)
        FOREACH (old IN prior |
            SET old.active = false
            CREATE (decision)-[:SUPERSEDES {reason: 'Incoming Slack directive', timestamp: $ts}]->(old)
        )
        RETURN decision.id, [old IN prior | old.id]
    """, {
        "target_id": parsed["target_id"], "author": parsed["author"],
        "decision_id": decision_id, "summary": parsed["summary"], "ts": now,
        "source_url": source_url or f"slack://simulated/{event_id}", "raw_text": text,
        "blocked": parsed["traffic_blocked"], "event_id": event_id,
        "payload_hash": payload_hash, "token": token,
    }).result_set
    if result:
        return dict(parsed, status="DECISION_RECORDED", decision_id=result[0][0],
                    superseded_decision_ids=result[0][1], timestamp=now)
    existing = engine.graph.ro_query("""
        MATCH (d:Decision {id: $id})
        OPTIONAL MATCH (d)-[:SUPERSEDES]->(old:Decision)
        RETURN d.payload_hash, collect(old.id), d.timestamp
    """, {"id": decision_id}).result_set
    if existing and existing[0][0] == payload_hash:
        return dict(parsed, status="DUPLICATE_EVENT", decision_id=decision_id,
                    superseded_decision_ids=existing[0][1], timestamp=existing[0][2])
    raise ValueError("Target/author is missing from the graph, or the event ID was reused with different content.")


def verify_slack_signature(body, timestamp, signature, secret, *, now=None):
    if not isinstance(timestamp, str):
        return False
    try:
        if abs((time.time() if now is None else now) - int(timestamp)) > 300:
            return False
    except (TypeError, ValueError):
        return False
    expected = "v0=" + hmac.new(secret.encode(), b"v0:" + timestamp.encode() + b":" + body, hashlib.sha256).hexdigest()
    return isinstance(signature, str) and hmac.compare_digest(expected, signature)


def make_handler(engine, *, signing_secret=None, user_map=None):
    """POST /slack/events: local simulation JSON or signed Slack Events payloads."""
    class SlackHandler(BaseHTTPRequestHandler):
        def respond(self, status, payload):
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            if self.path != "/slack/events":
                self.respond(404, {"error": "Not found"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 65536:
                    self.respond(413, {"error": "Request body must contain 1–65536 bytes"})
                    return
                body = self.rfile.read(length)
                if signing_secret and not verify_slack_signature(
                    body, self.headers.get("X-Slack-Request-Timestamp"),
                    self.headers.get("X-Slack-Signature"), signing_secret,
                ):
                    self.respond(401, {"error": "Invalid Slack signature"})
                    return
                payload = json.loads(body)
                if not isinstance(payload, dict):
                    raise ValueError("JSON body must be an object")
                if payload.get("type") == "url_verification":
                    self.respond(200, {"challenge": payload.get("challenge")})
                    return
                if payload.get("type") == "event_callback":
                    event = payload.get("event", {})
                    if event.get("type") != "message" or event.get("subtype") or event.get("bot_id"):
                        self.respond(200, {"status": "IGNORED_EVENT"})
                        return
                    author = (user_map or {}).get(event.get("user"))
                    if author not in AUTHORS:
                        raise ValueError("Slack user ID is not mapped to an allowed author")
                    text = event.get("text")
                    event_id = payload.get("event_id")
                    if not event_id:
                        raise ValueError("Slack event_id is required")
                    source = f"slack://channel/{event.get('channel', '')}/thread/{event.get('thread_ts', event.get('ts', ''))}"
                elif signing_secret:
                    raise ValueError("Signed listener expects a Slack Events API payload")
                else:
                    text, author = payload.get("text"), payload.get("author")
                    event_id, source = payload.get("event_id"), None
                result = ingest_slack_message(engine, text, author, event_id=event_id, source_url=source)
                self.respond(200, result)
            except (ValueError, TypeError, AttributeError):
                self.respond(422, {"error": "Invalid or unsupported Slack directive; specify one target, action and allowed author"})
            except Exception:
                logging.exception("Slack ingestion failed")
                self.respond(503, {"error": "Database ingestion failed; retry with the same event_id"})

        def log_message(self, format, *args):
            logging.info(format, *args)
    return SlackHandler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    engine = AegisGraphEngine(host=os.getenv("FALKORDB_HOST", "localhost"),
                              port=int(os.getenv("FALKORDB_PORT", "6379")),
                              password=os.getenv("FALKORDB_PASSWORD"))
    user_map = json.loads(os.getenv("AEGIS_SLACK_USER_MAP", "{}"))
    server = HTTPServer((args.host, args.port), make_handler(
        engine, signing_secret=os.getenv("SLACK_SIGNING_SECRET"), user_map=user_map,
    ))
    logging.info("Aegis Slack listener at http://%s:%s/slack/events", args.host, args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
