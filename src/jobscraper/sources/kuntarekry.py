"""kuntarekry.fi — the recruitment portal of Finland's municipalities and wellbeing counties.

Cities, hospital districts, schools and the inhouse IT companies they own (Esko Systems, Istekki,
2M-IT, …) publish here first. Same platform, same parser as valtiolle.fi — see
:mod:`jobscraper.sources._grade` — with two differences worth knowing:

* the results grid is followed by a second ``<job-list>`` of paid promotions that repeats on
  every page; ``parse_cards`` drops those (they carry ``is-promoted``);
* there are category pages next to the free-text search, e.g.
  ``/fi/tyopaikat-tyypin-mukaan/harjoittelu/`` (traineeships) and ``…/kesatyo/`` (summer work).
  They are not used here — the ``desc`` search reaches the same postings and one code path is
  worth more than two — but they are the place to look if seasonal coverage ever matters.

``robots.txt`` is ``Allow: /``. Verified live 2026-09-13: ``?desc=ohjelmisto`` → 27 hits over
two pages, ``?desc=harjoittelija`` → 25, ``?desc=data`` → 187 over eight.
"""

from __future__ import annotations

from jobscraper.sources._grade import GradeBoard, GradeSource
from jobscraper.sources.base import register

BOARD = GradeBoard(
    name="kuntarekry",
    description="kuntarekry.fi — Finnish municipal employers (Grade Solutions platform)",
    base="https://kuntarekry.fi/",
    default_queries=("ohjelmisto", "kehittäjä", "software", "harjoittelija", "ict"),
)


class Kuntarekry(GradeSource):
    board = BOARD


register(Kuntarekry())
