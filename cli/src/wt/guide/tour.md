# A hands-on tour

Needs the local stack: `make dev` (and `make operations` in another terminal for automation and
deadlines), then once: `make cli-install && make cli-keys`. Every step shows what Watch decided.

## 1. An alert becomes an incident and climbs the tiers

    wt intake "tour: cpu high on web-2" --event-id tour-1      # note the INC number
    wt --as t1a ack INC-XXXX                                   # acknowledged; the clock keeps running
    wt --as t1a escalate INC-XXXX --expect T1 --reason "needs T2"
    wt --as t1a resolve INC-XXXX                               # refused: T1 can't act at T2
    wt --as t2a escalate INC-XXXX --expect T1                  # refused: it's at T2 now (409)
    wt --as t2a resolve INC-XXXX --reason "restarted"
    wt --as t2a show INC-XXXX                                  # who did what, when

## 2. A supplier outage that never becomes an incident

    wt signal "tour: Square down" --signal "HTTP 503" --supplier Square --key tour-sq
    wt signal "tour: Square down" --signal "HTTP 503" --supplier Square --key tour-sq --event-id again
    wt signal "tour: Square up" --signal "HTTP 200" --supplier Square --key tour-sq --resolved
    wt --as t1a assessments                                    # one assessment, not three
    wt --as t1a show ASM-XXXX                                  # received, repeated, resolved

## 3. A customer email, worked to an RCA

    wt email-in some-message.eml                               # opens an assessment
    wt --as t1a do ASM-XXXX evidence summary="5xx 4%" result="4% for 10m"
    wt --as t1a do ASM-XXXX conclusion verdict=degraded confidence=high reason="sustained"
    wt --as t1a do ASM-XXXX promote reason="customer impact"   # an incident opens
    wt --as t1a resolve INC-YYYY --reason "rolled back"
    wt --as t1a show ASM-XXXX                                  # pick event ids to curate
    wt --as t1a do ASM-XXXX curate event=NNN section=root_cause
    wt --as t1a do ASM-XXXX rca summary="..." root_cause="..." state=final
    wt --as t1a rca ASM-XXXX
