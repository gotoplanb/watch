"""
Register (or replace) the escalation state machine in Step Functions Local, with the
function placeholders bound to the names the lambda shim routes (record_token / commit).

    python manage.py sfn_register
    # then point the app at it:
    #   ESCALATION_LOCAL_MODE=0
    #   ESCALATION_ENDPOINT_URL=http://localhost:8083
    #   ESCALATION_STATE_MACHINE_ARN=<printed arn>
    # and run `python manage.py run_lambda_shim` in another window.
"""
from pathlib import Path

import boto3
from django.conf import settings
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Register the escalation state machine in Step Functions Local."

    def add_arguments(self, parser):
        parser.add_argument("--name", default="Escalation")
        parser.add_argument("--endpoint", default=settings.ESCALATION_ENDPOINT_URL or "http://localhost:8083")

    def handle(self, *args, **opts):
        asl = (Path(settings.BASE_DIR).parent / "escalation" / "statemachine.asl.json").read_text()
        definition = (
            asl.replace("${record_token_function_arn}", "record_token")
               .replace("${commit_function_arn}", "commit")
        )
        client = boto3.client(
            "stepfunctions", endpoint_url=opts["endpoint"], region_name=settings.AWS_REGION,
            aws_access_key_id="x", aws_secret_access_key="x",
        )
        arn = f"arn:aws:states:{settings.AWS_REGION}:000000000000:stateMachine:{opts['name']}"
        try:
            client.delete_state_machine(stateMachineArn=arn)
        except Exception:
            pass
        resp = client.create_state_machine(
            name=opts["name"], definition=definition,
            roleArn="arn:aws:iam::000000000000:role/Dummy",
        )
        self.stdout.write(self.style.SUCCESS(f"registered: {resp['stateMachineArn']}"))
        self.stdout.write("Set ESCALATION_STATE_MACHINE_ARN to that, ESCALATION_LOCAL_MODE=0, "
                          "ESCALATION_ENDPOINT_URL=" + opts["endpoint"] + ", and run run_lambda_shim.")
