"""Job runner wiring: idempotent start, clean stop, no duplicate reminders
across restarts. Telegram sending is NOT tested here — mocks only."""
import pytest

from app.jobs import runner
from app.jobs.notifier import FakeNotifier
from app.jobs import reminders


@pytest.fixture()
def _clean():
    yield
    # make sure no scheduler leaks between tests
    for key in list(runner._runners):
        runner.stop_runner(key)


def test_start_is_idempotent(seed, _clean):
    d = seed["data_dir"]
    s1 = runner.start_runner(d, FakeNotifier())
    s2 = runner.start_runner(d, FakeNotifier())
    assert s1 is s2  # no second scheduler
    assert len(s1.get_jobs()) == 2  # run_due_jobs + ensure_jobs
    assert s1.running
    runner.stop_runner(d)
    assert not s1.running


def test_stop_when_not_running_is_safe(seed, _clean):
    runner.stop_runner(seed["data_dir"])  # no error


def test_restart_does_not_duplicate_reminders(seed, _clean):
    """Stop + start + tick: each due job is still sent exactly once."""
    from datetime import datetime, timezone
    d = seed["data_dir"]
    o1 = seed["office1"]
    notifier = FakeNotifier()
    # create a due job directly in the office DB
    from app.databases import manager as dbm
    from app.databases import office_models as om
    import json
    with dbm.office_session(d, o1) as s:
        s.add(om.NotificationJob(
            job_key="restart-test:1", kind="test",
            scheduled_for=datetime(2020, 1, 1, tzinfo=timezone.utc),
            payload_json=json.dumps({"user_id": seed["rep1_o1"],
                                     "text": "hi"})))
    s1 = runner.start_runner(d, notifier)
    # tick manually (same function the scheduler calls)
    runner._tick_run(d, notifier)
    assert len(notifier.inbox.get(seed["rep1_o1"], [])) == 1
    runner.stop_runner(d)
    # restart and tick again: the job is already 'sent', must not resend
    runner.start_runner(d, notifier)
    runner._tick_run(d, notifier)
    assert len(notifier.inbox.get(seed["rep1_o1"], [])) == 1
    runner.stop_runner(d)


def test_logging_notifier_marks_handled(seed, _clean, caplog):
    n = runner.LoggingNotifier()
    assert n.send(123, "hello") is True
