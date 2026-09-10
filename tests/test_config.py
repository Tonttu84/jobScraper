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
