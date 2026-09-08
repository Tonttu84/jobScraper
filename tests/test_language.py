"""Tests for :mod:`jobscraper.filters.language`.

Two questions are covered: what language a posting is written in, and which natural
languages the posting *requires* versus merely appreciates.
"""

from __future__ import annotations

import pytest

from jobscraper.filters.language import detect_language, find_language_requirements

# Realistic multi-sentence job-ad paragraphs, all well over the 80-character floor that
# detect_language() refuses to look at.
SAMPLE_PARAGRAPHS: dict[str, str] = {
    "en": (
        "We are looking for a junior software developer to join our platform team in Helsinki. "
        "You will work with TypeScript, React and Node.js and help us build reliable services "
        "for our customers. We offer mentoring, a flexible hybrid setup and a friendly team "
        "that likes to teach and to learn."
    ),
    "fi": (
        "Etsimme junior-ohjelmistokehittäjää Helsingin toimistollemme vakituiseen työsuhteeseen. "
        "Työskentelet osana tiimiä, joka kehittää verkkopalveluita TypeScriptillä ja Reactilla. "
        "Tarjoamme sinulle hyvän perehdytyksen, joustavat työajat ja mukavat työkaverit."
    ),
    "de": (
        "Wir suchen einen Junior Softwareentwickler für unser Team in Berlin, der uns bei der "
        "Entwicklung unserer Webanwendungen unterstützt. Du arbeitest eng mit erfahrenen "
        "Kolleginnen und Kollegen zusammen. Wir bieten dir flexible Arbeitszeiten, ein modernes "
        "Büro und viel Raum zum Lernen."
    ),
    "pl": (
        "Poszukujemy młodszego programisty do naszego zespołu w Warszawie, który pomoże nam "
        "rozwijać nasze aplikacje internetowe. Będziesz pracować w zespole doświadczonych "
        "inżynierów nad nowymi funkcjami produktu. Oferujemy elastyczne godziny pracy, prywatną "
        "opiekę medyczną i przyjazną atmosferę."
    ),
    "et": (
        "Otsime nooremtarkvaraarendajat oma meeskonda Tallinnas, kes aitab meil arendada "
        "veebirakendusi. Sinu igapäevatöö on seotud rakenduste arendamise ja koostööga kogenud "
        "arendajatega. Pakume paindlikku tööaega, kaasaegset kontorit ja häid võimalusi "
        "enesearenguks."
    ),
    "nl": (
        "Wij zoeken een junior softwareontwikkelaar voor ons team in Amsterdam die ons helpt "
        "onze webapplicaties verder te ontwikkelen. Je werkt samen met ervaren collega's aan "
        "nieuwe functionaliteit. Wij bieden flexibele werktijden, een modern kantoor en veel "
        "ruimte om te leren en te groeien."
    ),
}


@pytest.mark.parametrize("code", sorted(SAMPLE_PARAGRAPHS))
def test_detect_language_identifies_realistic_paragraphs(code: str) -> None:
    text = SAMPLE_PARAGRAPHS[code]
    assert len(text) > 80
    detected, confidence = detect_language(text)
    assert detected == code
    assert confidence >= 0.75


def test_detect_language_ignores_short_text() -> None:
    short = "Junior dev, Helsinki"  # 20 characters
    assert len(short) == 20
    assert detect_language(short) == (None, 0.0)


def test_detect_language_ignores_empty_text() -> None:
    assert detect_language(None) == (None, 0.0)
    assert detect_language("") == (None, 0.0)


def test_finnish_requirement_is_required() -> None:
    req = find_language_requirements("Sujuva suomen kielen taito vaaditaan.")
    assert req.required == {"fi"}
    assert req.optional == set()
    assert "fi" in req.evidence


def test_german_requirement_is_required() -> None:
    req = find_language_requirements("Fließende Deutschkenntnisse erforderlich.")
    assert req.required == {"de"}
    assert req.optional == set()


def test_negated_requirement_is_optional_only() -> None:
    req = find_language_requirements("Polish is not required, we work in English.")
    assert "pl" in req.optional
    assert "pl" not in req.required
    assert "pl" in req.mentioned


def test_advantage_wording_is_optional() -> None:
    req = find_language_requirements("Swedish would be an advantage.")
    assert req.optional == {"sv"}
    assert req.required == set()


def test_native_or_c1_is_required() -> None:
    req = find_language_requirements("Native or C1 Dutch.")
    assert req.required == {"nl"}
    assert req.optional == set()


def test_no_language_mention_yields_nothing() -> None:
    text = (
        "We build distributed backend services and ship them with Docker and Kubernetes. "
        "You will join a small team that owns its own infrastructure end to end."
    )
    req = find_language_requirements(text)
    assert req.required == set()
    assert req.optional == set()
    assert req.mentioned == set()


def test_programming_languages_are_not_natural_languages() -> None:
    req = find_language_requirements("We use Go and Rust; fluency with either is required.")
    assert req.mentioned == set()
    assert req.required == set()
    assert req.optional == set()


def test_required_and_optional_in_one_line_are_separated() -> None:
    req = find_language_requirements("Fluent English required; Finnish is a plus.")
    assert req.required == {"en"}
    assert req.optional == {"fi"}


def test_polish_requirement_is_required() -> None:
    req = find_language_requirements("Wymagana biegła znajomość języka polskiego.")
    assert req.required == {"pl"}
    assert req.optional == set()


def test_language_names_match_whole_words_only() -> None:
    """'viro' (Finnish for Estonia) must not fire inside 'environment' — it dropped real jobs."""
    req = find_language_requirements(
        "You will work in a fast-paced, cloud-native environment. Python is required. "
        "At Aiven we are committed to providing reasonable accommodations for qualified individuals."
    )
    assert "et" not in req.required
    assert "et" not in req.mentioned


def test_inflected_finnish_language_names_still_match() -> None:
    req = find_language_requirements("Viron kielen sujuva taito vaaditaan. Englannin kielen taito on eduksi.")
    assert "et" in req.required
    assert "en" in req.optional
