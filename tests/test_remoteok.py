from datetime import UTC, datetime

from jobscraper.sources.remoteok import RemoteOK


def test_remoteok_parses_fixture(make_ctx):
    ctx = make_ctx({"remoteok.com/api": "remoteok.json"})
    jobs = list(RemoteOK().fetch(ctx))

    # legal-notice element, the sales job (no matching tag) and the broken row are all skipped
    assert [j.title for j in jobs] == ["Junior Backend Developer", "Site Reliability Engineer"]
    j = jobs[0]
    assert j.source == "remoteok" and j.source_id == "1099231"
    assert j.company == "Brightloop"
    assert j.url == "https://remoteok.com/remote-jobs/1099231-junior-backend-developer-brightloop"
    assert j.remote == "remote" and j.remote_region == "Europe" and j.country is None
    assert j.tags == ["dev", "backend", "python", "junior"]
    assert j.salary_text == "40,000 - 60,000 USD"
    assert "FastAPI" in j.description
    assert "Please mention the word" not in j.description  # anti-bot line stripped
    assert j.posted_at == datetime(2026, 9, 1, 8, 0, tzinfo=UTC)

    assert jobs[1].country == "DE"  # location "Germany"


def test_remoteok_tag_filter_is_permissive(make_ctx):
    ctx = make_ctx({"remoteok.com/api": "remoteok.json"}, options={"tags": []})
    assert len(list(RemoteOK().fetch(ctx))) == 3  # no filter → every real posting

    ctx = make_ctx({"remoteok.com/api": "remoteok.json"}, options={"tags": ["SALES"]})
    assert [j.title for j in RemoteOK().fetch(ctx)] == ["Senior Sales Manager"]


def test_remoteok_single_request_and_limit(make_ctx):
    ctx = make_ctx({"remoteok.com/api": "remoteok.json"}, limit=1)
    assert len(list(RemoteOK().fetch(ctx))) == 1
    assert len(ctx.http.calls) == 1
