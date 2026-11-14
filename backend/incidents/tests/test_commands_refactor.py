"""operations_tick sweep semantics: inline vs pooled dispatch, retry logging, and loop control."""
from concurrent.futures import Future
from io import StringIO
from unittest.mock import Mock, patch

import pytest
from django.core.management import call_command

from incidents.management.commands import operations_tick as tick

MODULE = "incidents.management.commands.operations_tick"


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    monkeypatch.setattr(tick, "close_old_connections", Mock())
    monkeypatch.setattr(tick.signal, "signal", Mock())


def test_single_sweep_runs_jobs_inline_and_prints_tick():
    out = StringIO()
    with patch(f"{MODULE}._queued_candidates", return_value=[("assessment", 1), ("diagnostic", 2)]), \
         patch(f"{MODULE}.run_job") as run, patch(f"{MODULE}.cadence.tick", return_value={"due": 0}):
        call_command("operations_tick", stdout=out)
    assert [c.args for c in run.call_args_list] == [("assessment", 1), ("diagnostic", 2)]
    assert "{'due': 0}" in out.getvalue()


def test_single_sweep_reraises_failures():
    with patch(f"{MODULE}._queued_candidates", return_value=[]), \
         patch(f"{MODULE}.cadence.tick", side_effect=RuntimeError("boom")):
        out = StringIO()
        with pytest.raises(RuntimeError):
            call_command("operations_tick", stdout=out)


def test_loop_logs_failures_and_continues_until_stopped():
    calls = []

    def sleep(_):
        calls.append("sleep")
        if len(calls) == 2:
            tick.signal.signal.call_args_list[0].args[1]()  # SIGTERM handler stops the loop

    with patch(f"{MODULE}._queued_candidates", return_value=[]), \
         patch(f"{MODULE}.cadence.tick", side_effect=[RuntimeError("boom"), {}]), \
         patch(f"{MODULE}.time.sleep", side_effect=sleep), patch.object(tick.logger, "exception") as log:
        call_command("operations_tick", "--loop", "--interval", "0.5", stdout=StringIO())
    assert calls == ["sleep", "sleep"]
    log.assert_called_once()


def test_loop_rejects_short_interval():
    with patch(f"{MODULE}._queued_candidates", return_value=[]), patch(f"{MODULE}.cadence.tick", return_value={}):
        out = StringIO()
        with pytest.raises(ValueError, match="at least 0.2"):
            call_command("operations_tick", "--loop", "--interval", "0.1", stdout=out)


def test_dispatch_submits_once_per_key_in_loop_mode():
    pool, jobs = Mock(), {("assessment", 1): Future()}
    tick._dispatch([("assessment", 1), ("diagnostic", 2)], jobs, pool, loop=True)
    pool.submit.assert_called_once_with(tick.run_job, "diagnostic", 2)
    assert ("diagnostic", 2) in jobs


def test_reap_finished_drops_done_and_surfaces_errors():
    done, failed, pending = Future(), Future(), Future()
    done.set_result(None)
    failed.set_exception(RuntimeError("job failed"))
    jobs = {"done": done, "pending": pending}
    tick._reap_finished(jobs)
    assert list(jobs) == ["pending"]
    jobs["failed"] = failed
    with pytest.raises(RuntimeError):
        tick._reap_finished(jobs)
    assert "failed" not in jobs


@pytest.mark.django_db
def test_queued_candidates_reads_queued_work():
    assert tick._queued_candidates() == []


@pytest.mark.django_db
@pytest.mark.parametrize("kind", ["assessment", "diagnostic"])
def test_run_job_routes_by_kind(kind):
    run = Mock()
    with patch(f"{MODULE}.automation.run") as auto, patch(f"{MODULE}.diagnostics.execute") as execute, \
         patch(f"{MODULE}.DiagnosticRun.objects.select_related") as select:
        select.return_value.get.return_value = run
        tick.run_job(kind, 7)
    if kind == "assessment":
        auto.assert_called_once_with(7)
        execute.assert_not_called()
    else:
        execute.assert_called_once_with(run.assessment, run.rule, run_id=run.pk)
