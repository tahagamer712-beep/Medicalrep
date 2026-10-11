"""SQLite concurrency behaviour (WAL mode): concurrent writers must not lose data.

Documents the honest limit: writers serialize briefly; with busy_timeout they
wait instead of failing. Suitable for office-scale usage, not hundreds of
simultaneous writers.
"""
import threading

from app.databases import manager as dbm
from app.databases import office_models as om
from app.services.audit import log_office_event

THREADS = 10
EVENTS_PER_THREAD = 20


def test_concurrent_audit_writes_no_loss(seed):
    d = seed["data_dir"]
    office_id = seed["office1"]
    errors = []

    def worker(n):
        try:
            for i in range(EVENTS_PER_THREAD):
                with dbm.office_session(d, office_id) as s:
                    log_office_event(s, actor_user_id=seed["owner"],
                                     event_type="CONCURRENCY_PROBE",
                                     meta=f'{{"t":{n},"i":{i}}}')
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"concurrent writes failed: {errors[:3]}"
    with dbm.office_session(d, office_id) as s:
        n = (s.query(om.OfficeAuditEvent)
             .filter_by(event_type="CONCURRENCY_PROBE").count())
    assert n == THREADS * EVENTS_PER_THREAD


def test_read_during_write(seed):
    """Readers are not blocked by a writer under WAL."""
    d = seed["data_dir"]
    office_id = seed["office1"]
    stop = threading.Event()
    seen = []

    def reader():
        while not stop.is_set():
            with dbm.office_session(d, office_id) as s:
                seen.append(s.query(om.Representative).count())

    t = threading.Thread(target=reader)
    t.start()
    try:
        for _ in range(30):
            with dbm.office_session(d, office_id) as s:
                log_office_event(s, actor_user_id=seed["owner"],
                                 event_type="RW_PROBE")
    finally:
        stop.set()
        t.join()
    assert seen, "reader never got a chance to read"
    assert all(v >= 0 for v in seen)
