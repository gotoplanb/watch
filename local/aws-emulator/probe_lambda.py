"""Can the AWS Emulator run Watch's escalation Lambdas yet? (ADR-067)

    make emulator-probe

Deploys a throwaway python3.12 function into the running emulator and reports what Watch's
`record_token` / `commit` handlers need: CPython (so compiled packages like psycopg load), TLS,
and a TCP connection out to Postgres on the host. Run it after an emulator upgrade, especially
after an AWS launch; when it prints READY, revisit replacing Step Functions Local + the Lambda
shim (and reproducing watch#57 locally).

Talks only to the emulator: it refuses to run unless AWS_ENDPOINT_URL points at 127.0.0.1.
"""
import io
import json
import os
import sys
import time
import zipfile

import boto3

HANDLER = '''
import platform, socket, sys

def handler(event, context):
    out = {"implementation": platform.python_implementation(), "version": sys.version.split()[0]}
    for mod in ("_ssl", "_sqlite3", "zoneinfo"):
        try:
            __import__(mod)
            out["import " + mod] = "ok"
        except Exception as exc:
            out["import " + mod] = type(exc).__name__
    try:
        socket.create_connection((event["host"], event["port"]), timeout=3).close()
        out["postgres"] = "connected"
    except Exception as exc:
        out["postgres"] = (type(exc).__name__ + ": " + str(exc))[:120]
    return out
'''
NAME = "watch-capability-probe"
TRUST = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": "sts:AssumeRole",
                                                  "Principal": {"Service": "lambda.amazonaws.com"}}]}


def _package() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("probe.py", HANDLER)
    return buf.getvalue()


def _deploy(lam, iam):
    try:
        role = iam.create_role(RoleName=NAME, AssumeRolePolicyDocument=json.dumps(TRUST))["Role"]["Arn"]
    except iam.exceptions.EntityAlreadyExistsException:
        role = iam.get_role(RoleName=NAME)["Role"]["Arn"]
    try:
        lam.delete_function(FunctionName=NAME)
    except lam.exceptions.ResourceNotFoundException:
        pass
    lam.create_function(FunctionName=NAME, Runtime="python3.12", Handler="probe.handler", Role=role,
                        Code={"ZipFile": _package()}, Timeout=15)
    for _ in range(30):
        if lam.get_function(FunctionName=NAME)["Configuration"]["State"] == "Active":
            return
        time.sleep(0.5)


def main() -> int:
    if not os.environ.get("AWS_ENDPOINT_URL", "").startswith("https://127.0.0.1:"):
        print("refusing: AWS_ENDPOINT_URL must point at the local emulator (source local/aws-emulator/env.sh)")
        return 2
    lam, iam = boto3.client("lambda"), boto3.client("iam")
    _deploy(lam, iam)
    event = {"host": os.environ.get("PROBE_PG_HOST", "host.docker.internal"),
             "port": int(os.environ.get("PROBE_PG_PORT", "5433"))}
    resp = lam.invoke(FunctionName=NAME, Payload=json.dumps(event).encode())
    result = json.loads(resp["Payload"].read())
    for key, value in result.items():
        print(f"  {key:22} {value}")
    ready = (result.get("implementation") == "CPython" and result.get("import _ssl") == "ok"
             and result.get("postgres") == "connected")
    print("READY: the emulator can run Watch's Lambdas; revisit ADR-067." if ready else
          "NOT YET: Watch's Lambdas need CPython, TLS and a connection to Postgres (ADR-067).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
