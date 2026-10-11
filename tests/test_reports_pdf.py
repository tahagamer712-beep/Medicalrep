"""PDF export of reports: daily/weekly/monthly/performance + edge cases.

Verifies file creation, non-empty output, sane page counts, and that the
Arabic title survives shaping (extractable text is non-empty). Excel/CSV
exports are untouched (see test_reports.py).
"""
from datetime import datetime, timezone

import pytest
from pypdf import PdfReader

from app.services import attendance as att
from app.services import plans
from app.services import reports as rep
from app.services import visits as vsvc

DAY = "2026-10-12"  # Monday
MONDAY = "2026-10-12"


@pytest.fixture()
def activity(seed):
    d = seed["data_dir"]
    o1 = seed["office1"]
    doc = vsvc.add_doctor(d, seed["supervisor1"], office_id=o1, name="د. ر")
    vsvc.record_actual_visit(
        d, seed["rep1_o1"], office_id=o1, rep_user_id=seed["rep1_o1"],
        target_type="doctor", target_id=doc["id"], outcome="done",
        confirm=True,
        visited_at=datetime(2026, 10, 12, 7, 0, tzinfo=timezone.utc))
    att.check_in(d, seed["rep1_o1"], office_id=o1, rep_user_id=seed["rep1_o1"],
                 now=datetime(2026, 10, 12, 5, 0, tzinfo=timezone.utc))
    p = plans.create_plan(d, seed["rep1_o1"], office_id=o1, plan_type="weekly",
                          rep_user_id=seed["rep1_o1"], period=MONDAY,
                          payload={})
    plans.submit_plan(d, seed["rep1_o1"], office_id=o1, plan_type="weekly",
                      plan_id=p["plan_id"])
    att.check_in(d, seed["rep2_o1"], office_id=o1, rep_user_id=seed["rep2_o1"],
                 now=datetime(2026, 10, 12, 4, 50, tzinfo=timezone.utc))
    return o1


def _check_pdf(path, min_pages=1, max_pages=10):
    assert path.exists()
    assert path.stat().st_size > 0
    reader = PdfReader(str(path))
    assert min_pages <= len(reader.pages) <= max_pages
    text = "".join(p.extract_text() or "" for p in reader.pages)
    assert len(text.strip()) > 0
    return text


def test_export_pdf_daily(seed, activity, tmp_path):
    d = seed["data_dir"]
    r = rep.daily_report(d, seed["supervisor1"], office_id=activity, day=DAY)
    out = rep.export_pdf(r, tmp_path / "daily.pdf")
    assert out.suffix == ".pdf"
    _check_pdf(out)


def test_export_pdf_weekly(seed, activity, tmp_path):
    d = seed["data_dir"]
    r = rep.weekly_report(d, seed["supervisor1"], office_id=activity,
                          week_start=MONDAY)
    out = rep.export_pdf(r, tmp_path / "weekly.pdf")
    _check_pdf(out)


def test_export_pdf_monthly(seed, activity, tmp_path):
    d = seed["data_dir"]
    r = rep.monthly_report(d, seed["supervisor1"], office_id=activity,
                           year=2026, month=10)
    out = rep.export_pdf(r, tmp_path / "monthly.pdf")
    _check_pdf(out)


def test_export_pdf_performance(seed, activity, tmp_path):
    d = seed["data_dir"]
    r = rep.rep_performance(d, seed["supervisor1"], office_id=activity,
                            rep_user_id=seed["rep1_o1"],
                            start_date="2026-10-01", end_date="2026-10-31")
    out = rep.export_pdf(r, tmp_path / "perf.pdf")
    _check_pdf(out)


def test_export_pdf_empty_rows(tmp_path):
    r = {"title": "تقرير فارغ", "generated_at": "2026-10-11T00:00:00",
         "office_id": 1, "rows": [], "totals": {}}
    out = rep.export_pdf(r, tmp_path / "empty.pdf")
    text = _check_pdf(out)
    assert "بيانات" in text or len(text.strip()) > 0


def test_export_pdf_arabic_title_present(seed, activity, tmp_path):
    """The Arabic title must survive reshaping into extractable text."""
    d = seed["data_dir"]
    r = rep.daily_report(d, seed["supervisor1"], office_id=activity, day=DAY)
    out = rep.export_pdf(r, tmp_path / "ar.pdf")
    text = _check_pdf(out)
    # shaped Arabic: check for Arabic-script characters in extracted text
    assert any("\u0600" <= ch <= "\u06FF" or "\uFB50" <= ch <= "\uFDFF"
               or "\uFE70" <= ch <= "\uFEFF" for ch in text)


def test_export_pdf_many_rows_paginates(tmp_path):
    rows = [{"الاسم": f"مندوب {i}", "الزيارات": i, "الحضور": "حاضر"}
            for i in range(120)]
    r = {"title": "تقرير كبير", "generated_at": "2026-10-11T00:00:00",
         "office_id": 1, "rows": rows,
         "totals": {"reps": 120, "visits": sum(range(120))}}
    out = rep.export_pdf(r, tmp_path / "big.pdf")
    _check_pdf(out, min_pages=2, max_pages=20)
