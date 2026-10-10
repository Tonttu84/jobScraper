"""Guards for config/sources.yaml: YAML pitfalls that would silently change what gets scraped."""

from __future__ import annotations


def test_country_lists_are_strings(settings):
    """Unquoted NO/ON/OFF/YES parse as booleans in YAML 1.1, silently dropping Norway etc."""
    for name, cfg in settings.sources.items():
        for key in ("countries", "regions", "sites"):
            values = cfg.options.get(key) or []
            assert all(isinstance(v, str) for v in values), f"{name}.{key} has a non-string entry: {values}"


def test_ats_board_countries_are_iso2_strings(settings):
    """``country: NO`` is boolean False in YAML 1.1 — and a typo there mislabels a whole board."""
    from jobscraper.sources._common import ISO2_CODES

    for entry in settings.sources["ats_boards"].options.get("urls") or []:
        if not isinstance(entry, dict) or "country" not in entry:
            continue
        code = entry["country"]
        assert isinstance(code, str), f"{entry.get('company')}: country is {code!r}, quote it"
        assert code == code.upper() and code.lower() in ISO2_CODES, f"{entry}: not an ISO-2 code"


def test_ats_board_country_lists_are_iso2_strings(settings):
    """``countries: [.., NO, ..]`` unquoted is ``False`` — and would silently drop Norway."""
    from jobscraper.sources._common import ISO2_CODES

    marked = 0
    for entry in settings.sources["ats_boards"].options.get("urls") or []:
        if not isinstance(entry, dict) or "countries" not in entry:
            continue
        marked += 1
        codes = entry["countries"]
        assert isinstance(codes, list) and codes, f"{entry.get('company')}: countries is {codes!r}"
        for code in codes:
            assert isinstance(code, str), f"{entry.get('company')}: {code!r} in countries, quote it"
            assert code == code.upper() and code.lower() in ISO2_CODES, f"{entry}: not an ISO-2 code"
    assert marked, "no board declares 'countries' any more — the worldwide boards need it"


def test_missing_profile_points_at_the_example(tmp_path):
    """A fresh clone has only config/profile.example.yaml; the error must say what to copy."""
    import pytest

    from jobscraper import config

    (tmp_path / "profile.example.yaml").write_text("name: x\n", encoding="utf-8")
    (tmp_path / "sources.yaml").write_text("sources: {}\n", encoding="utf-8")
    with pytest.raises(FileNotFoundError) as exc:
        config.load_settings(tmp_path)
    assert "profile.example.yaml" in str(exc.value) and "profile.yaml" in str(exc.value)


def test_missing_yaml_without_example_is_a_plain_error(tmp_path):
    import pytest

    from jobscraper import config

    with pytest.raises(FileNotFoundError) as exc:
        config.load_settings(tmp_path)
    assert "copy" not in str(exc.value)


def test_rank_order_defaults_to_the_lexical_prior():
    """The queue is ordered by the free prior unless a profile asks for the old screen order."""
    from jobscraper.config import AIPolicy

    assert AIPolicy().rank_order == "prior"
    assert AIPolicy(rank_order="screen").rank_order == "screen"


def test_gone_after_misses_defaults_to_one_complete_run():
    """One complete run of a source that misses a posting is enough to call it taken down."""
    from jobscraper.config import Profile

    profile = Profile(name="x", summary="y")
    assert profile.gone_after_misses == 1
    assert Profile(name="x", summary="y", gone_after_misses=0).gone_after_misses == 0


def test_the_scrape_section_is_its_own_settings_block(tmp_path):
    """``scrape:`` (the pool across sources) is popped like ``http:``, not read as a source."""
    from jobscraper import config

    (tmp_path / "profile.yaml").write_text("name: x\nsummary: y\n", encoding="utf-8")
    (tmp_path / "sources.yaml").write_text("scrape:\n  workers: 2\nsources: {}\n", encoding="utf-8")
    settings = config.load_settings(tmp_path)
    assert settings.scrape == {"workers": 2}
    assert "scrape" not in settings.sources


def test_without_a_scrape_section_the_settings_are_empty(tmp_path):
    from jobscraper import config

    (tmp_path / "profile.yaml").write_text("name: x\nsummary: y\n", encoding="utf-8")
    (tmp_path / "sources.yaml").write_text("scrape:\nsources: {}\n", encoding="utf-8")
    assert config.load_settings(tmp_path).scrape == {}
    (tmp_path / "sources.yaml").write_text("sources: {}\n", encoding="utf-8")
    assert config.load_settings(tmp_path).scrape == {}


def test_company_aliases_are_their_own_settings_block(tmp_path):
    """``company_aliases:`` feeds dedupe; absent or empty it is ``{}``, never a source."""
    from jobscraper import config

    (tmp_path / "profile.yaml").write_text("name: x\nsummary: y\n", encoding="utf-8")
    (tmp_path / "sources.yaml").write_text("company_aliases:\n  vois: vodafone\nsources: {}\n",
                                           encoding="utf-8")
    settings = config.load_settings(tmp_path)
    assert settings.company_aliases == {"vois": "vodafone"}
    assert "company_aliases" not in settings.sources
    (tmp_path / "sources.yaml").write_text("company_aliases:\nsources: {}\n", encoding="utf-8")
    assert config.load_settings(tmp_path).company_aliases == {}
    (tmp_path / "sources.yaml").write_text("sources: {}\n", encoding="utf-8")
    assert config.load_settings(tmp_path).company_aliases == {}
