"""wt — drive Watch from the terminal, as any user (ADR-056).

  wt --as t1a incidents                   open incidents
  wt --as t1a intake "cpu high on web-2"  fire the alert webhook (no user needed)
  wt --as t1a ack INC-0042
  wt --as t1a escalate INC-0042 --expect T1 --reason "needs T2"
  wt --as t2a wait INC-0042 --tier T2
  wt --as t2a show INC-0042               detail + transitions
  wt --as t1a show ASM-0007               an assessment + its history
  wt --as admin api incidents/?current_tier=T3
  wt --as t1a api -X POST incidents/INC-0042/ack/ -d '{"reason": "x"}'

Every call goes to the server's /api/ with that user's key, so it is authorized exactly as the
UI is. wt never touches the database.
"""
import argparse
import json
import os
import sys
import time

from . import __version__, config
from .http import ApiError, request

ASSESSMENT_PREFIX = "ASM-"


class Api:
    def __init__(self, server: str, user: str | None):
        self.server = server
        self.user = user

    def _headers(self):
        return {"Authorization": f"Bearer {config.key_for(self.server, self.user)}"}

    def url(self, path: str) -> str:
        return f"{self.server}/api/{path.lstrip('/')}"

    def get(self, path):
        return request("GET", self.url(path), headers=self._headers())

    def send(self, method, path, body=None):
        return request(method, self.url(path), body=body, headers=self._headers())

    def webhook(self, path, body):
        secret = {"X-Watch-Webhook-Secret": config.webhook_secret(self.server)}
        return request("POST", self.url(path), body=body, headers=secret)


def _parser():
    p = argparse.ArgumentParser(prog="wt", description="Drive Watch from the terminal (ADR-056).")
    p.add_argument("--server", help="local (default) or a URL. Env: WT_SERVER.")
    p.add_argument("--as", dest="user", help="Act as this user. Env: WT_AS.")
    p.add_argument("--json", action="store_true", help="Print the server's reply as JSON.")
    sub = p.add_subparsers(dest="command", metavar="COMMAND")

    sub.add_parser("version", help="wt's version.")
    sub.add_parser("whoami", help="Who the key belongs to, and their tiers.")
    sub.add_parser("users", help="Users wt has keys for on this server.")
    key = sub.add_parser("key", help="Store a key copied from /ui/settings.")
    key.add_argument("action", choices=["set"])
    key.add_argument("username")
    key.add_argument("key")

    inc = sub.add_parser("incidents", help="List incidents (open by default).")
    inc.add_argument("--status", default="OPEN", help="OPEN, RESOLVED or ALL")
    inc.add_argument("--tier", help="T1, T2 or T3")
    asm = sub.add_parser("assessments", help="List assessments.")
    asm.add_argument("--status", help="new, investigating, monitoring, no_issue, promoted")
    show = sub.add_parser("show", help="One incident (INC-…) or assessment (ASM-…).")
    show.add_argument("number")

    for verb, what in (("ack", "Acknowledge"), ("escalate", "Escalate"), ("resolve", "Resolve")):
        act = sub.add_parser(verb, help=f"{what} an incident.")
        act.add_argument("number")
        act.add_argument("--reason", default="")
        act.add_argument("--expect", metavar="TIER", help="Fail with 409 unless still at TIER.")

    intake = sub.add_parser("intake", help="Fire the incident intake webhook.")
    intake.add_argument("title")
    intake.add_argument("--source", default="wt")
    intake.add_argument("--event-id", default="", help="Same id twice = deduped while open.")
    intake.add_argument("--payload", default="{}", type=json.loads)

    for name, what in (("assess", "Open one assessment from a signal."),
                       ("signal", "Send a signal through the routing policies.")):
        sig = sub.add_parser(name, help=what)
        sig.add_argument("title")
        sig.add_argument("--signal", required=True, help="What was observed.")
        sig.add_argument("--source", default="wt")
        for field in ("customer", "service", "supplier"):
            sig.add_argument(f"--{field}", default="")
        sig.add_argument("--event-id", default="")
        sig.add_argument("--payload", default="{}", type=json.loads)

    wait = sub.add_parser("wait", help="Poll an incident until every given condition holds.")
    wait.add_argument("number")
    wait.add_argument("--tier")
    wait.add_argument("--status")
    wait.add_argument("--acked", action="store_true")
    wait.add_argument("--timeout", type=float, default=60)
    wait.add_argument("--interval", type=float, default=1)

    api = sub.add_parser("api", help="Any /api/ path, raw.")
    api.add_argument("-X", dest="method", default="GET")
    api.add_argument("-d", dest="data", type=json.loads, help="JSON body")
    api.add_argument("path")
    return p


def main(argv=None) -> int:
    args = _parser().parse_args(sys.argv[1:] if argv is None else argv)
    if getattr(args, "number", None):
        args.number = args.number.strip().upper()
    server = config.server_url(args.server)
    client = Api(server, args.user or os.environ.get("WT_AS"))
    try:
        return run(args, client) or 0
    except ApiError as exc:
        print(f"wt: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


def _out(args, data, human):
    if args.json:
        print(json.dumps(data, indent=2))
    else:
        human(data)


def run(args, client: Api):
    match args.command:
        case None:
            _parser().print_help()
        case "version":
            print(f"wt {__version__}")
        case "whoami":
            _out(args, client.get("me"), lambda me: print(
                f"{me['username']} (id {me['id']}) tiers: {', '.join(me['tiers']) or 'none'}"
                f"{' staff' if me['is_staff'] else ''} on {client.server}"))
        case "users":
            for name in sorted(config.users(client.server)):
                print(name)
        case "key":
            config.set_key(client.server, args.username, args.key)
            print(f"Stored {args.username}'s key for {client.server}.")
        case "incidents":
            query = "" if args.status.upper() == "ALL" else f"status={args.status}"
            if args.tier:
                query += f"&current_tier={args.tier}"
            _out(args, client.get(f"incidents/?{query.lstrip('&')}"), _print_incidents)
        case "assessments":
            path = f"assessments/?status={args.status}" if args.status else "assessments/"
            _out(args, client.get(path), _print_assessments)
        case "show":
            return _show(args, client)
        case "ack" | "escalate" | "resolve":
            body = {"reason": args.reason}
            if args.expect:
                body["expected_tier"] = args.expect.upper()
            incident = client.send("POST", f"incidents/{args.number}/{args.command}/", body)
            _out(args, incident, lambda i: print(_incident_line(i)))
        case "intake":
            body = {"source": args.source, "title": args.title,
                    "source_event_id": args.event_id, "payload": args.payload}
            _out(args, client.webhook("intake/webhook", body), lambda i: print(
                _incident_line(i) + ("" if i["created"] else "  (existing, deduped)")))
        case "assess" | "signal":
            return _signal(args, client)
        case "wait":
            return _wait(args, client)
        case "api":
            reply = client.send(args.method.upper(), args.path, args.data)
            print(json.dumps(reply, indent=2) if not isinstance(reply, str) else reply)


def _signal(args, client):
    body = {"title": args.title, "signal": args.signal, "source": args.source,
            "customer": args.customer, "service": args.service, "supplier": args.supplier,
            "source_event_id": args.event_id, "payload": args.payload}
    path = "assessments/intake" if args.command == "assess" else "signals/intake"
    reply = client.webhook(path, body)
    rows = [reply] if args.command == "assess" else reply["assessments"]
    def human(_):
        for row in rows:
            print(_assessment_line(row) + ("" if row["created"] else "  (existing)"))

    _out(args, reply, human)


def _incident_line(i) -> str:
    acked = " acked" if i.get("acknowledged_at") else ""
    number = i.get("number") or i["id"]
    return f"{number:9} {i['status']:8} {i['current_tier']}{acked:6}  {i['title']}"


def _assessment_line(a) -> str:
    promoted = f" -> incident {a['incident']}" if a.get("incident") else ""
    return f"{a['number']:9} {a['status']:13} {a['title']}{promoted}"


def _print_incidents(rows):
    if not rows:
        print("No incidents.")
    for row in rows:
        print(_incident_line(row))


def _print_assessments(rows):
    if not rows:
        print("No assessments.")
    for row in rows:
        print(_assessment_line(row))


def _show(args, client):
    if args.number.upper().startswith(ASSESSMENT_PREFIX):
        assessment = client.get(f"assessments/{args.number}/")
        history = client.get(f"assessments/{args.number}/history/")
        if args.json:
            print(json.dumps({**assessment, "history": history}, indent=2))
            return
        print(_assessment_line(assessment))
        for key in ("verdict", "confidence", "severity", "conclusion", "customer", "service",
                    "supplier", "automation_state"):
            if assessment.get(key) not in (None, ""):
                print(f"  {key:16} {assessment[key]}")
        for event in history:
            print(f"  {event['occurred_at'][:19]}  {event['kind']:22} {event['actor']}: "
                  f"{(event['body'] or '').splitlines()[0] if event['body'] else ''}")
        return
    incident = client.get(f"incidents/{args.number}/")
    if args.json:
        print(json.dumps(incident, indent=2))
        return
    print(_incident_line(incident))
    print(f"  sla deadline     {incident.get('sla_deadline_at')}")
    for t in incident.get("transitions", []):
        who = t.get("actor_name") or t["actor"]
        print(f"  {t['at'][:19]}  {t['from_status']}/{t['from_tier']} -> "
              f"{t['to_status']}/{t['to_tier']}  by {who}  {t['reason']}")


def _matches(incident, args) -> bool:
    return ((not args.tier or incident["current_tier"] == args.tier.upper())
            and (not args.status or incident["status"] == args.status.upper())
            and (not args.acked or bool(incident.get("acknowledged_at"))))


def _wait(args, client):
    deadline = time.monotonic() + args.timeout
    while True:
        incident = client.get(f"incidents/{args.number}/")
        if _matches(incident, args):
            _out(args, incident, lambda i: print(_incident_line(i)))
            return 0
        if time.monotonic() >= deadline:
            print(f"wt: timed out after {args.timeout:g}s; now {_incident_line(incident)}",
                  file=sys.stderr)
            return 2
        time.sleep(args.interval)
