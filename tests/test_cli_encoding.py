"""Console output must never abort a stage (PLAN.md, follow-up of 2026-10-13).

With stdout redirected to a file or pipe on Windows, Python falls back to the ANSI code page
(cp1252), which cannot encode "✓" or U+2011; a ``console.print``/``typer.echo`` of such text used
to raise UnicodeEncodeError and end the whole scrape. These tests simulate that stream.
"""

from __future__ import annotations

import io
import sys

import typer

from jobscraper import cli as cli_mod
from jobscraper import config
from jobscraper import store as store_mod

TEXT = "✓ arbeitnow: 12 new ‑ non‑breaking"


def _cp1252_stream() -> io.TextIOWrapper:
    return io.TextIOWrapper(io.BytesIO(), encoding="cp1252")


def _written(stream: io.TextIOWrapper) -> str:
    stream.flush()
    return stream.buffer.getvalue().decode("utf-8")


def test_cp1252_stdout_is_made_safe_for_console_and_echo(monkeypatch):
    out, err = _cp1252_stream(), _cp1252_stream()
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)

    cli_mod._safe_console_streams()
    cli_mod.console.print(TEXT)  # the module-level rich Console resolves sys.stdout lazily
    typer.echo(TEXT)
    typer.echo(TEXT, err=True)

    assert _written(out).count(TEXT) == 2
    assert TEXT in _written(err)


def test_app_callback_makes_the_streams_safe(monkeypatch):
    out = _cp1252_stream()
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", _cp1252_stream())
    monkeypatch.setattr(config, "use_profile", lambda profile: None)
    monkeypatch.setattr(store_mod, "DB_OVERRIDE", None)

    cli_mod.main(db=None, profile=None)
    cli_mod.console.print(TEXT)

    assert TEXT in _written(out)


def test_streams_that_already_cope_or_cannot_be_reconfigured_are_left_alone(monkeypatch):
    utf8 = io.TextIOWrapper(io.BytesIO(), encoding="utf-8", errors="strict")
    monkeypatch.setattr(sys, "stdout", utf8)
    monkeypatch.setattr(sys, "stderr", None)  # pythonw / detached: no stream at all

    cli_mod._safe_console_streams()

    assert sys.stdout is utf8 and utf8.errors == "strict"
    assert sys.stderr is None

    plain = io.StringIO()  # no reconfigure(); nothing to do, and nothing must raise
    monkeypatch.setattr(sys, "stdout", plain)
    cli_mod._safe_console_streams()
    assert sys.stdout is plain
