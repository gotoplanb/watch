"""wt — drive Watch from the terminal, as any user (ADR-056). `wt --help` and `wt guide` explain
Watch itself; the command definitions and their help text live in parser.py.

Every call goes to the server's /api/ with that user's key, so it is authorized exactly as the
UI is. wt never touches the database.
"""
import json
import os
import sys
import time
from importlib import resources
from pathlib import Path

from . import __version__, config, parser
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

    def send(self, method, path, body=None, files=None):
        return request(method, self.url(path), body=body, files=files, headers=self._headers())

    def webhook(self, path, body=None, raw=None):
        secret = {"X-Watch-Webhook-Secret": config.webhook_secret(self.server)}
        return request("POST", self.url(path), body=body, raw=raw, headers=secret)


def _parser():
    return parser.build()


def guide(topic: str) -> str:
    """A guide topic's text (packaged markdown; no server needed)."""
    topics = sorted(p.stem for p in resources.files("wt.guide").iterdir() if p.name.endswith(".md"))
    if topic not in topics:
        raise SystemExit(f"wt: no guide topic {topic!r}; topics: {', '.join(topics)}")
    return resources.files("wt.guide").joinpath(f"{topic}.md").read_text(encoding="utf-8")


def pairs(items) -> tuple[dict, dict]:
    """`key=value` → fields (JSON-parsed when it parses), `key=@path` → files to upload."""
    fields, files = {}, {}
    for item in items:
        key, sep, value = item.partition("=")
        if not sep:
            raise SystemExit(f"wt: expected key=value, got {item!r}")
        if value.startswith("@"):
            files.setdefault(key, []).append(value[1:])
            continue
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            parsed = value
        if key in fields:
            previous = fields[key] if isinstance(fields[key], list) else [fields[key]]
            fields[key] = [*previous, parsed]
        else:
            fields[key] = parsed
    return fields, files


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
        case "guide":
            print(guide(args.topic))
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
        case "email-in":
            raw = sys.stdin.buffer.read() if args.file == "-" else Path(args.file).read_bytes()
            reply = client.webhook("email/inbound", raw=(raw, "message/rfc822"))
            _out(args, reply, _print_email)
        case "wait":
            return _wait(args, client)
        case "new" | "do" | "note" | "comms" | "sme" | "remediations" | "rca" | "comm" \
                | "sme-respond" | "remediation" | "report" | "outbox" | "rules" | "areas" \
                | "heartbeats":
            return _workspace(args, client)
        case "api":
            reply = client.send(args.method.upper(), args.path, args.data)
            print(json.dumps(reply, indent=2) if not isinstance(reply, str) else reply)


def _signal(args, client):
    body = {"title": args.title, "signal": args.signal, "source": args.source,
            "customer": args.customer, "service": args.service, "supplier": args.supplier,
            "source_event_id": args.event_id, "payload": args.payload,
            "correlation_key": args.key, "state": "resolved" if args.resolved else "active",
            **({"evidence_url": args.url} if getattr(args, "url", "") else {})}
    path = "assessments/intake" if args.command == "assess" else "signals/intake"
    reply = client.webhook(path, body)
    rows = [reply] if args.command == "assess" else reply["assessments"]
    def human(_):
        if "outcome" in reply:
            print(f"{reply['outcome']}")
        for row in rows:
            how = row.get("how", "created" if row["created"] else "existing")
            print(_assessment_line(row) + ("" if how == "created" else f"  ({how})"))

    _out(args, reply, human)


def _print_email(reply):
    print(f"{reply['outcome']}: {reply['reason']}")
    for row in reply["assessments"]:
        print(_assessment_line(row) + ("" if row["created"] else "  (existing)"))


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
            print(_event_line(event))
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


# --- the assessment workspace (ADR-056) ----------------------------------------------------------

def _event_line(event) -> str:
    first = (event.get("body") or "").splitlines()[0] if event.get("body") else ""
    who = event.get("actor_name") or event["actor"]
    return f"  #{event['id']:<5} {event['occurred_at'][:19]}  {event['kind']:22} {who}: {first}"


def _print_action(result):
    for event in result["events"]:
        print(_event_line(event))
    incident = f" -> {result['incident']}" if result.get("incident") else ""
    print(f"{result['assessment']} {result['status']}{incident}")


def _comm_line(c) -> str:
    return (f"{c['id']}  {c['state']:10} v{c['revision']}  {c['subject']}  "
            f"-> {', '.join(c['recipients'])}")


def _print_rows(rows, line, empty):
    if not rows:
        print(empty)
    for row in rows:
        print(line(row))


def _act(args, client, action, fields, files=None):
    path = f"assessments/{args.number}/actions/{action}/"
    _out(args, client.send("POST", path, fields, files), _print_action)


def _comm(args, client):
    path = f"communications/{args.id}/"
    if not args.verb:
        _out(args, client.get(path), lambda c: (print(_comm_line(c)), print(), print(c["body"])))
        return
    fields, _ = pairs(args.pairs)
    if "expected_version" not in fields:  # what the page would have rendered
        fields["expected_version"] = client.get(path)["revision"]
    _out(args, client.send("POST", f"{path}{args.verb}/", fields), lambda c: print(_comm_line(c)))


SIMPLE_LISTS = {
    "comms": ("assessments/{}/communications/", _comm_line, "No communications."),
    "sme": ("assessments/{}/engagements/",
            lambda e: f"{e['id']}  {e['state']:12} {e['target'] or e['area']}  {e['reason']}",
            "No specialist engagements."),
    "remediations": ("assessments/{}/remediations/",
                     lambda t: f"{t['id']}  {t['state']:12} {t['owner']:8} due {t['due_date']}"
                               f"  {t['title']}", "No corrective actions."),
    "outbox": ("outbox/", lambda r: f"{r['delivered_at'][:19]}  {r['assessment']}  v{r['version']}"
                                    f"  {r['subject']}", "Outbox is empty."),
    "heartbeats": ("heartbeats/", lambda h: f"{h['state']:8} {h['name']:20} limit "
                                            f"{h['max_silence_minutes']} min  last seen "
                                            f"{(h['last_seen_at'] or 'never')[:19]}",
                   "No sources are watched for silence."),
    "rules": ("diagnostic-rules/", lambda r: f"{r['id']:4} {r['provider']:10} {r['name']}",
              "No saved checks."),
    "areas": ("subject-areas/", lambda a: f"{a['id']:4} {a['name']}  ({', '.join(a['members'])})",
              "No subject areas."),
}


def _workspace(args, client):
    command = args.command
    if command in SIMPLE_LISTS:
        path, line, empty = SIMPLE_LISTS[command]
        rows = client.get(path.format(getattr(args, "number", "")))
        _out(args, rows, lambda r: _print_rows(r, line, empty))
    elif command == "new":
        body = {k: getattr(args, k) for k in ("title", "signal", "source", "customer", "service",
                                              "supplier")}
        _out(args, client.send("POST", "assessments/", body), lambda a: print(_assessment_line(a)))
    elif command == "do":
        fields, files = pairs(args.pairs)
        _act(args, client, args.action, fields, files)
    elif command == "note":
        photos = {"photos": args.photo} if args.photo else None
        _act(args, client, "comment", {"body": args.body}, photos)
    elif command == "comm":
        _comm(args, client)
    elif command == "sme-respond":
        reply = client.send("POST", f"engagements/{args.id}/respond/",
                            {"action": args.verb, "body": args.body})
        _out(args, reply, lambda e: print(f"{e['id']}  {e['state']}"))
    elif command == "remediation":
        reply = client.send("POST", f"remediations/{args.id}/", pairs(args.pairs)[0])
        _out(args, reply, lambda t: print(f"{t['id']}  {t['state']}  {t['title']}"))
    elif command == "rca":
        _out(args, client.get(f"assessments/{args.number}/rca/"), lambda r: print(r["markdown"]))
    elif command == "report":
        report = client.get(f"reports/?days={args.days}")
        print(json.dumps(report, indent=2))
