"""Guards for config/sources.yaml: YAML pitfalls that would silently change what gets scraped."""

from __future__ import annotations


def test_country_lists_are_strings(settings):
    """Unquoted NO/ON/OFF/YES parse as booleans in YAML 1.1, silently dropping Norway etc."""
    for name, cfg in settings.sources.items():
        for key in ("countries", "regions", "sites"):
            values = cfg.options.get(key) or []
            assert all(isinstance(v, str) for v in values), f"{name}.{key} has a non-string entry: {values}"
