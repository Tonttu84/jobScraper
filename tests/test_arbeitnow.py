from datetime import UTC, datetime

from jobscraper.sources.arbeitnow import Arbeitnow


def test_arbeitnow_parses_fixture(make_ctx):
    ctx = make_ctx({"job-board-api": "arbeitnow.json"}, options={"max_pages": 1})
    jobs = list(Arbeitnow().fetch(ctx))
    assert len(jobs) == 2  # the broken record is skipped, not raised
    j = jobs[0]
    assert j.source == "arbeitnow"
    assert j.title == "Junior Software Developer (m/w/d)"
    assert j.company == "Beispiel GmbH"
    assert j.url.endswith("junior-software-developer-berlin-123456")
    assert j.country == "DE" and j.city == "Berlin" and j.remote == "onsite"
    assert "Junior Developer" in j.description and "<" not in j.description
    assert j.posted_at == datetime.fromtimestamp(1756900000, tz=UTC)
    assert jobs[1].remote == "remote"


def test_arbeitnow_respects_limit(make_ctx):
    ctx = make_ctx({"job-board-api": "arbeitnow.json"}, options={"max_pages": 3}, limit=1)
    assert len(list(Arbeitnow().fetch(ctx))) == 1
    assert len(ctx.http.calls) == 1
