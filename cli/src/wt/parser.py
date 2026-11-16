"""wt's command line: every command says what it means in Watch, not just what it calls.

Each command has a one-line summary (shown in `wt --help`), a description (what it does in the
lifecycle, who may run it, what changes, which ADR), and examples that paste into a terminal.
tests/test_help.py fails if any command lacks them.
"""
import argparse
import json
import textwrap

from . import actions

PAIRS_HELP = ("key=value fields. Values parse as JSON when they can (rule=2, auto_send=true, "
              "selected=[341,342]); key=@file uploads; repeating a key makes a list.")

TOP = """\
wt drives Watch, an incident intake and tiered-escalation platform, from the terminal.

Every command goes through the server's HTTP API as the user you name with --as, so it's
allowed or refused exactly as the web UI would be. wt never touches the database.

New to Watch? These need no server:
  wt guide                 how Watch works, by topic (start here)
  wt guide tour            a hands-on walkthrough
  wt <command> --help      what a command means, who may run it, examples

Setup against a local server: make dev; make cli-install; make cli-keys
"""

TOP_EPILOG = """\
examples:
  wt guide lifecycle
  wt --as t1a incidents
  wt intake "cpu high on web-2" --event-id e1
  wt --as t1a escalate INC-0042 --expect T1 --reason "needs T2"
  wt --as t1a do ASM-0007 promote reason="customer impact"

environment: WT_AS (default user), WT_SERVER (default server), WT_WEBHOOK_SECRET (non-local intake)
exit codes:  0 ok, 1 refused by the server (reason printed), 2 `wt wait` timed out
"""


def _command(sub, name, summary, description, examples):
    """Add a subcommand with its explanation and examples (both required: see tests/test_help.py)."""
    epilog = "examples:\n" + "\n".join(f"  {line}" for line in examples)
    return sub.add_parser(name, help=summary, description=_wrap(f"{summary}\n\n{description}"),
                          epilog=epilog, formatter_class=argparse.RawDescriptionHelpFormatter)


def _wrap(text: str, width: int = 92) -> str:
    """Wrap prose paragraphs; leave pre-formatted blocks (any paragraph with its own line breaks)."""
    return "\n\n".join(p if "\n" in p else textwrap.fill(p, width) for p in text.split("\n\n"))


def build() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="wt", description=TOP, epilog=TOP_EPILOG,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--server", help="local (default, http://localhost:8010) or a URL. Env: WT_SERVER.")
    p.add_argument("--as", dest="user", metavar="USER",
                   help="Act as this user (keys from `make cli-keys`). Env: WT_AS.")
    p.add_argument("--json", action="store_true", help="Print the server's reply as JSON.")
    sub = p.add_subparsers(dest="command", metavar="COMMAND")
    _learning(sub)
    _identity(sub)
    _incidents(sub)
    _signals(sub)
    _workspace(sub)
    _raw(sub)
    return p


def _learning(sub):
    guide = _command(
        sub, "guide", "How Watch works, by topic. Needs no server.",
        "Short explanations of Watch's model and the reasons behind it, each pointing to the "
        "decisions (ADRs) in watch-adrs.md. Without a topic, prints the overview and the topic list.",
        ["wt guide", "wt guide lifecycle", "wt guide tour"])
    guide.add_argument("topic", nargs="?", default="overview")
    _command(sub, "version", "Print wt's version.", "The CLI's own version, not the server's. Use it when reporting a problem with wt.",
             ["wt version"])


def _identity(sub):
    _command(sub, "whoami", "Who --as is on the server, and their tiers.",
             "Asks the server which user the key belongs to and which tiers they hold. Tiers decide "
             "what they may act on: an incident whose current tier is theirs or lower (ADR-008).",
             ["wt --as t2a whoami"])
    _command(sub, "users", "The users wt has keys for on this server.",
             "Lists the keys in ~/.config/wt/keys.json for --server. Locally they're written by "
             "`make cli-keys`; rerun it after reseeding or rotating keys.", ["wt users"])
    key = _command(sub, "key", "Store a user's key for a non-local server.",
                   "Each person has a personal key (wm_...) on their settings page in the web UI. "
                   "It grants exactly what their own login grants (ADR-030, ADR-056).",
                   ["wt --server https://watch.example key set dave wm_12_abcd..."])
    key.add_argument("action", choices=["set"])
    key.add_argument("username")
    key.add_argument("key")


def _incidents(sub):
    inc = _command(sub, "incidents", "List incidents (open by default).",
                   "Incidents are confirmed customer impact being worked through tiers T1 → T2 → T3 "
                   "on a clock (ADR-007). Each line: number, status, tier, acknowledged, title.",
                   ["wt --as t1a incidents", "wt --as t1a incidents --tier T2",
                    "wt --as t1a incidents --status ALL"])
    inc.add_argument("--status", default="OPEN", help="OPEN, RESOLVED or ALL")
    inc.add_argument("--tier", help="T1, T2 or T3")

    show = _command(sub, "show", "One incident (INC-...) or assessment (ASM-...), with its history.",
                    "An incident shows its tier transitions: who moved it, when, and why, including "
                    "automatic escalations by the clock (system:auto-escalation). An assessment shows "
                    "its full event history with ids (#342), which `wt do ... curate` uses.",
                    ["wt --as t1a show INC-0042", "wt --as t1a show ASM-0007",
                     "wt --as t1a --json show INC-0042"])
    show.add_argument("number")

    _incident_action(sub, "ack", "Acknowledge: \"I've got this.\" The clock keeps running.",
                     "Records who acknowledged and when. It does NOT stop the tier's clock: an "
                     "acknowledged incident still escalates when its deadline passes. Acknowledging "
                     "is not progress (ADR-007). It doesn't page anyone.")
    _incident_action(sub, "escalate", "Move the incident to the next tier.",
                     "Ends this tier's clock, starts the next tier's (default 15 → 30 → 60 minutes) "
                     "and assigns the next tier's on-call (ADR-007, ADR-012). With paging enabled "
                     "(the paging_enabled flag; off in local dev) they're paged (ADR-013), and with "
                     "handoff_brief on, a brief is posted for them (ADR-040). Escalate for authority; "
                     "to get someone's "
                     "knowledge instead, engage a specialist (`wt guide specialists`).")
    _incident_action(sub, "resolve", "Close the incident, from any tier.",
                     "Ends the incident and its clock; no timer is left running (ADR-007). A person "
                     "resolves incidents: a monitor's recovery never does (ADR-059).")

    wait = _command(sub, "wait", "Poll an incident until every given condition holds.",
                    "For scripts: waits for a tier, a status, or an acknowledgement instead of "
                    "sleeping blindly. Exits 2 on timeout, printing the incident's state.",
                    ["wt --as t2a wait INC-0042 --tier T2 --timeout 120",
                     "wt --as t1a wait INC-0042 --status RESOLVED"])
    wait.add_argument("number")
    wait.add_argument("--tier")
    wait.add_argument("--status")
    wait.add_argument("--acked", action="store_true")
    wait.add_argument("--timeout", type=float, default=60, help="seconds (default 60)")
    wait.add_argument("--interval", type=float, default=1, help="seconds between polls")


def _incident_action(sub, verb, summary, description):
    act = _command(sub, verb, summary,
                   description + "\n\nWho may: anyone holding the incident's current tier or "
                   "higher (ADR-008). Others are refused with 403.",
                   [f"wt --as t1a {verb} INC-0042 --expect T1 --reason \"...\""])
    act.add_argument("number")
    act.add_argument("--reason", default="", help="Recorded on the history; read by the next tier.")
    act.add_argument("--expect", metavar="TIER",
                     help="Refuse (409) unless the incident is still at TIER, so two people acting "
                     "at once can't both succeed.")


def _signal_fields(cmd):
    cmd.add_argument("title")
    cmd.add_argument("--signal", required=True, help="What was observed.")
    cmd.add_argument("--source", default="wt", help="Where it came from (e.g. sumo, uptimerobot).")
    cmd.add_argument("--customer", default="")
    cmd.add_argument("--service", default="")
    cmd.add_argument("--supplier", default="", help="A supplier fans out to its dependents.")
    cmd.add_argument("--event-id", default="", help="The source's id for this event (dedupe).")
    cmd.add_argument("--payload", default="{}", type=json.loads, help="The raw source body, as JSON.")


def _signals(sub):
    intake = _command(sub, "intake", "Open an incident directly from an alert (skips assessment).",
                      "The direct path for sources trusted enough to skip assessment: it opens an "
                      "incident at T1 and starts its clock. The same event id while the incident is "
                      "open is a duplicate; after it's resolved, it opens a new one (ADR-009). Most "
                      "signals should use `wt signal` instead (ADR-054). Uses the intake secret, "
                      "not a user.",
                      ["wt intake \"cpu high on web-2\" --source sumo --event-id e1"])
    intake.add_argument("title")
    intake.add_argument("--source", default="wt")
    intake.add_argument("--event-id", default="", help="Same id twice = duplicate while open.")
    intake.add_argument("--payload", default="{}", type=json.loads)

    signal = _command(sub, "signal", "Send a signal: it becomes assessment(s), not an incident.",
                      "The main intake path (ADR-054). A signal is a question: are customers "
                      "affected? It opens an assessment per customer and service concerned; a "
                      "supplier signal fans out to everything depending on that supplier. A matching "
                      "policy then runs diagnostic checks on our own telemetry. A customer/service "
                      "that already has an open assessment gets the signal as evidence there instead "
                      "of a second one (ADR-060). With --key, a re-fire or recovery of the same "
                      "condition attaches to the assessment working it (ADR-059). With --url, a "
                      "cited status page is screenshotted as evidence (ADR-060). Uses the intake "
                      "secret, not a user.",
                      ["wt signal \"Square API down\" --signal \"HTTP 503\" --supplier Square "
                       "--key monitor-778",
                       "wt signal \"Square API up\" --signal \"HTTP 200\" --supplier Square "
                       "--key monitor-778 --resolved",
                       "wt signal \"Square degraded\" --signal \"elevated errors\" --supplier Square "
                       "--url https://status.example.com/incidents/abc"])
    _signal_fields(signal)
    signal.add_argument("--key", default="",
                        help="Correlation key: the source's stable id for the condition (e.g. the "
                        "monitor id).")
    signal.add_argument("--url", default="",
                        help="A status page the signal cites. Recorded; screenshotted as evidence if "
                        "its host is on the status-page allow-list (ADR-060).")
    signal.add_argument("--resolved", action="store_true",
                        help="The source reports recovery: opens nothing, records it on the open "
                        "assessment and reruns its checks. Never resolves an incident.")

    assess = _command(sub, "assess", "Open exactly one assessment from a signal (no fan-out).",
                      "Like `wt signal` but always opens the single assessment described, without "
                      "supplier fan-out or correlation. Uses the intake secret.",
                      ["wt assess \"p99 latency\" --signal \"p99 > 2s\" --customer \"Example Gateway\""])
    _signal_fields(assess)
    assess.add_argument("--key", default="", help="Stored for later correlation.")
    assess.add_argument("--resolved", action="store_true", help=argparse.SUPPRESS)

    email = _command(sub, "email-in", "Deliver a raw email (.eml) as inbound mail.",
                     "Posts the message exactly as the cloud email receiver (SES) will (ADR-058). "
                     "The outcome is a new signal (the sender's address or domain picks the customer "
                     "or supplier), a reply added to the assessment it answers, or ignored "
                     "(auto-reply, bounce, our own mail, a repeat) with the reason. Attachments "
                     "become evidence; unverified senders are marked.",
                     ["wt email-in message.eml", "cat message.eml | wt email-in -"])
    email.add_argument("file", help="An .eml file, or - for stdin.")


def _workspace(sub):
    _command(sub, "assessments", "List assessments.",
             "Assessments are signals being checked for customer impact before anyone is paged "
             "(ADR-054). Statuses: new, investigating, monitoring, no_issue, promoted.",
             ["wt --as t1a assessments", "wt --as t1a assessments --status monitoring"]
             ).add_argument("--status")

    new = _command(sub, "new", "Open an assessment by hand, owned by you.",
                   "What a responder does from the web UI's 'new assessment' page: you become its "
                   "owner, and any matching policy starts its checks.",
                   ["wt --as t1a new \"checkout errors\" --signal \"5xx up\" --customer \"Example Gateway\""])
    new.add_argument("title")
    new.add_argument("--signal", required=True, help="What was observed.")
    new.add_argument("--source", default="operator",
                     choices=["operator", "customer", "supplier", "monitor"])
    for field in ("customer", "service", "supplier"):
        new.add_argument(f"--{field}", default="")

    do = _command(sub, "do", "Run any workspace action on an assessment.",
                  "The assessment workspace, action by action: the same forms, rules and history "
                  "as the web page (ADR-055, ADR-056).\n\n" + actions.reference(),
                  ["wt --as t1a do ASM-0007 evidence summary=\"5xx 4%\" result=\"4% for 10m\"",
                   "wt --as t1a do ASM-0007 conclusion verdict=degraded confidence=high reason=\"sustained\"",
                   "wt --as t1a do ASM-0007 promote reason=\"customer impact\"",
                   "wt --as t1a do ASM-0007 engage area=1 reason=\"processor expertise\""])
    do.add_argument("number")
    do.add_argument("action", choices=list(actions.ACTIONS), metavar="ACTION")
    do.add_argument("pairs", nargs="*", help=PAIRS_HELP)

    note = _command(sub, "note", "Add a note, and photos, to an assessment's history.",
                    "Shortcut for `wt do ... comment`. Text and photos are saved together as one "
                    "event, or not at all.",
                    ["wt --as t1a note ASM-0007 \"dashboard: https://grafana.example/d/x\"",
                     "wt --as t1a note ASM-0007 \"the error\" --photo shot.png"])
    note.add_argument("number")
    note.add_argument("body", nargs="?", default="")
    note.add_argument("--photo", action="append", default=[], help="Image file; repeatable.")

    _communications(sub)
    _specialists_and_rca(sub)

    report = _command(sub, "report", "Reporting summary: assessments, incidents, outcomes.",
                      "Counts assessments and incidents separately, so incident response times are "
                      "measured from promotion, not from the first signal (ADR-054).",
                      ["wt --as t1a report --days 7"])
    report.add_argument("--days", type=int, default=30)
    _command(sub, "rules", "Saved diagnostic checks, with ids.",
             "Queries against our own telemetry (Prometheus, Sumo Logic) that policies run "
             "automatically and responders run on demand.", ["wt --as t1a rules",
                                                            "wt --as t1a do ASM-0007 query rule=2"])
    _command(sub, "heartbeats", "Monitoring sources watched for silence, and their state.",
             "Alert-only monitors are quiet when all is well, so a broken webhook looks like good "
             "news. A watched source proves it's alive via its heartbeat URL or any signal; silence "
             "past its limit opens an assessment (ADR-062). States: ok, waiting (never heard from), "
             "silent.", ["wt --as t1a heartbeats"])
    _command(sub, "areas", "Specialist subject areas and their members, with ids.",
             "Knowledge groups such as Payments, separate from tiers (ADR-052).",
             ["wt --as t1a areas", "wt --as t1a do ASM-0007 engage area=1 reason=\"...\""])


def _communications(sub):
    _command(sub, "comms", "An assessment's communications.",
             "Each line: id, state (review, sent, suppressed, expired), revision, subject, "
             "recipients. See `wt guide communications`.", ["wt --as t1a comms ASM-0007"]
             ).add_argument("number")
    comm = _command(sub, "comm", "Read a communication, or edit / send / suppress it.",
                    "Drafts wait in a review window before going out. Editing makes a new revision "
                    "and doesn't extend any deadline. send and suppress act on the revision you "
                    "name (expected_version, default: the current one); a stale one is refused with "
                    "409 (ADR-055). Delivery is a local outbox today.",
                    ["wt --as t1a comm <id>",
                     "wt --as t1a comm <id> edit subject=\"Update\" body=\"...\" recipients=ops@example.com",
                     "wt --as t1a comm <id> send",
                     "wt --as t1a comm <id> suppress reason=\"duplicate\""])
    comm.add_argument("id")
    comm.add_argument("verb", nargs="?", choices=["edit", "send", "suppress"])
    comm.add_argument("pairs", nargs="*", help=PAIRS_HELP)
    _command(sub, "outbox", "Communications delivered to the local outbox.",
             "Local delivery receipts. Nothing reaches a real mailbox until SES is set up (ADR-058).",
             ["wt --as t1a outbox"])


def _specialists_and_rca(sub):
    _command(sub, "sme", "An assessment's specialist engagements.",
             "Each line: id, state, who was paged, why. See `wt guide specialists`.",
             ["wt --as t1a sme ASM-0007"]).add_argument("number")
    respond = _command(sub, "sme-respond", "As the paged specialist: acknowledge, then complete.",
                       "Only the paged person (or a member of the paged area, or an administrator) "
                       "can respond. Completing needs a note of what you did. Neither changes the "
                       "incident's tier or clock (ADR-052).",
                       ["wt --as t3a sme-respond <id> ack",
                        "wt --as t3a sme-respond <id> complete --body \"failed over the processor\""])
    respond.add_argument("id")
    respond.add_argument("verb", choices=["ack", "complete"])
    respond.add_argument("--body", default="", help="What you did (needed to complete).")

    _command(sub, "rca", "The root-cause analysis for an assessment, as markdown.",
             "Curated from the history: selected events by section, with the analysis. See "
             "`wt guide rca`.", ["wt --as t1a rca ASM-0007"]).add_argument("number")
    _command(sub, "remediations", "An assessment's corrective actions.",
             "Each line: id, state, owner, due date, title.", ["wt --as t1a remediations ASM-0007"]
             ).add_argument("number")
    fix = _command(sub, "remediation", "Update a corrective action.",
                   "state is open, in_progress or done; done needs a verification of how it was "
                   "checked. Its owner may update it, as may anyone who can act on the case.", ["wt --as t2a remediation <id> state=done verification=\"canary live\""])
    fix.add_argument("id")
    fix.add_argument("pairs", nargs="+", help=PAIRS_HELP)


def _raw(sub):
    api = _command(sub, "api", "Call any /api/ path directly.",
                   "For anything without a dedicated command. Uses --as's key; prints the JSON.",
                   ["wt --as admin api \"incidents/?current_tier=T3\"",
                    "wt --as t1a api -X POST incidents/INC-0042/ack/ -d '{\"reason\": \"x\"}'"])
    api.add_argument("-X", dest="method", default="GET")
    api.add_argument("-d", dest="data", type=json.loads, help="JSON body")
    api.add_argument("path")
