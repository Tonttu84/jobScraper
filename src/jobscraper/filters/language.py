"""Language detection and 'language X required' detection.

Two questions per posting:
1. What language is the posting written in? (lingua, restricted to the languages we care about)
2. Does the text *require* a language the candidate can't work in? We look at sentences that
   mention a language name and classify them as required vs. nice-to-have with keyword heuristics.
   This is intentionally conservative: when in doubt we flag for review rather than drop.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache

# Language names as they appear in job ads, in English, Finnish, German, Swedish, Portuguese and
# the local language itself. Keys are ISO-639-1 codes.
LANGUAGE_NAMES: dict[str, list[str]] = {
    "en": ["english", "englanti", "englannin", "englisch", "engelska", "inglês", "ingles"],
    "fi": ["finnish", "suomi", "suomen kiel", "finnisch", "finska", "finlandês", "finlandes"],
    "de": ["german", "saksa", "saksan kiel", "deutsch", "tyska", "alemão", "alemao"],
    "sv": ["swedish", "ruotsi", "ruotsin kiel", "schwedisch", "svenska", "sueco"],
    "no": ["norwegian", "norja", "norsk", "norwegisch", "norueguês", "noruegues"],
    "da": ["danish", "tanska", "dansk", "dänisch", "dinamarquês", "dinamarques"],
    "et": ["estonian", "viro", "viron kiel", "eesti keel", "estnisch"],
    "pl": ["polish", "puola", "polski", "polnisch", "języka polskiego", "język polski",
           "polaco", "polonês", "polones"],
    "nl": ["dutch", "hollanti", "nederlands", "niederländisch", "flemish",
           "holandês", "holandes", "neerlandês", "neerlandes"],
    "fr": ["french", "ranska", "français", "francais", "französisch", "francês"],
    "es": ["spanish", "espanja", "español", "espanol", "spanisch", "castellano",
           "espanhol", "castelhano"],
    "pt": ["portuguese", "portugali", "português", "portugues", "portugiesisch"],
    "it": ["italian", "italia ", "italiano", "italienisch"],
    "cs": ["czech", "tšekki", "čeština", "cestina", "tschechisch"],
    "sk": ["slovak", "slovakki", "slovenčina", "slowakisch"],
    "hu": ["hungarian", "unkari", "magyar", "ungarisch"],
    "ro": ["romanian", "romania", "română", "rumänisch"],
    "ru": ["russian", "venäjä", "venäjän kiel", "русск", "russisch", "russo"],
    "uk": ["ukrainian", "ukraina", "українськ"],
    "lt": ["lithuanian", "liettua", "lietuvių"],
    "lv": ["latvian", "latvia", "latviešu"],
    "ar": ["arabic", "arabia", "arabisch", "árabe", "arabe"],
    "tr": ["turkish", "turkki", "türkçe", "türkisch"],
    "el": ["greek", "kreikka", "ελληνικ"],
    "hr": ["croatian", "kroatia", "hrvatski"],
    "sl": ["slovenian", "slovene", "slovenščina"],
    "bg": ["bulgarian", "bulgaria", "български"],
    "ja": ["japanese", "japani", "japonês", "japones"],
    "zh": ["chinese", "mandarin", "kiina", "chinês", "chines", "mandarim"],
}

# Names match at a word start only (a trailing inflection is fine: "viron kielen", "englannin"),
# so "viro" cannot fire inside "environment" and "italia" cannot fire inside "italiano"-free text.
_LANGUAGE_RES: dict[str, re.Pattern[str]] = {
    code: re.compile(r"(?<!\w)(?:" + "|".join(re.escape(n.strip()) for n in names) + ")", re.I)
    for code, names in LANGUAGE_NAMES.items()
}


def _mentioned(text: str) -> set[str]:
    return {code for code, pattern in _LANGUAGE_RES.items() if pattern.search(text)}


_REQUIRED_WORDS = re.compile(
    r"\b(required|require|requirement|must|mandatory|essential|necessary|need(ed)?|fluent|fluency|native|"
    r"proficien|excellent|business[- ]level|c1|c2|working language|"
    r"vaaditaan|edellyt|välttämät|sujuva|erinomai|äidinkiel|työkieli|"
    r"erforderlich|voraussetzung|vorausgesetzt|zwingend|fließend|fliessend|verhandlungssicher|muttersprach|sehr gut|"
    r"krävs|flytande|obligatorisk|wymagan|biegł|płynn|vereist|vloeiend|requis|courant|imprescindible|obligatorio|"
    # Portuguese (European and Brazilian spellings). Accented and bare forms are spelled out
    # rather than left open-ended so they cannot fire inside English words ("necessarily").
    r"obrigat[óo]ri|requisito|exigid|exige-se|exigimos|exig[êe]ncia|necess[áa]ri[oa]|flu[êe]ncia|nativ[oa]|"
    r"dom[íi]nio|avan[çc]ad[oa]|imprescind[íi]vel|essencial|indispens[áa]vel|l[íi]ngua de trabalho)",
    re.I,
)
_OPTIONAL_WORDS = re.compile(
    r"\b(plus|bonus|advantage|asset|nice[- ]to[- ]have|preferred|preferably|appreciated|beneficial|desirable|"
    r"optional|not required|not necessary|no need|would be|is a merit|helpful|"
    r"eduksi|etu|katsotaan eduksi|plussaa|hyödyksi|ei vaadita|ei edellytetä|"
    r"von vorteil|wünschenswert|nicht erforderlich|kein muss|gerne gesehen|"
    r"meriterande|mile widziane|dodatkowym atutem|pré|een pre|un plus|deseable|"
    # Portuguese. This table is anchored on both sides, so plurals are spelled out.
    r"valorizad[oa]s?|valoriza-se|valorizamos|preferencial(?:mente)?|desej[áa]ve[li]s?|"
    r"mais[- ]valia|vantagens?|opcional|n[ãa]o (?:é |e )?(?:obrigat[óo]ri[oa]|necess[áa]ri[oa]))\b",
    re.I,
)
_NOT_NEEDED = re.compile(
    r"\b(no|not|don'?t|without|isn'?t|aren'?t|ei|nicht|kein|ohne|inte|n[ãa]o)\b[^.\n]{0,40}\b(need|require|necessary|must|"
    r"vaadi|tarvitse|edellyt|erforderlich|voraussetzung|notwendig|krävs|"
    r"necess[áa]ri[oa]|obrigat[óo]ri|exig|precisa)\w*",
    re.I,
)
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?•\n])\s+|\s*[•·▪▸►]\s*|\n+")
_CLAUSE_SPLIT = re.compile(r"\s*[;,()/]\s*|\s+[-–—]\s+")

# lingua is slow to build; restrict to languages that actually show up in EU tech ads.
_DETECT_LANGS = ["en", "fi", "de", "sv", "nb", "da", "et", "pl", "nl", "fr", "es", "pt", "it", "cs", "sk",
                 "hu", "ro", "bg", "hr", "sl", "lt", "lv", "ru", "uk", "tr", "el"]


@lru_cache(maxsize=1)
def _detector():
    from lingua import IsoCode639_1, LanguageDetectorBuilder

    codes = [getattr(IsoCode639_1, c.upper()) for c in _DETECT_LANGS]
    return LanguageDetectorBuilder.from_iso_codes_639_1(*codes).with_preloaded_language_models().build()


def detect_language(text: str | None, min_confidence: float = 0.75) -> tuple[str | None, float]:
    """Return (iso639-1, confidence) for the posting text, or (None, 0) if unsure/short."""
    if not text or len(text) < 80:
        return None, 0.0
    sample = text[:4000]
    try:
        values = _detector().compute_language_confidence_values(sample)
    except Exception:
        return None, 0.0
    if not values:
        return None, 0.0
    best = values[0]
    code = best.language.iso_code_639_1.name.lower()
    if code in ("nb", "nn"):  # lingua has no generic "no"; our policy lists use it
        code = "no"
    return (code, best.value) if best.value >= min_confidence else (None, best.value)


@dataclass
class LanguageRequirements:
    required: set[str] = field(default_factory=set)
    optional: set[str] = field(default_factory=set)
    mentioned: set[str] = field(default_factory=set)
    evidence: dict[str, str] = field(default_factory=dict)  # code → sentence


def find_language_requirements(text: str | None) -> LanguageRequirements:
    """Scan sentences mentioning a natural language and classify required vs optional."""
    out = LanguageRequirements()
    if not text:
        return out
    for sentence in _SENTENCE_SPLIT.split(text):
        s = sentence.strip()
        if not s or len(s) > 600:
            continue
        low = s.lower()
        # Only natural-language names are in the table, so "Go"/"Rust"/"Swift" never collide.
        if not _mentioned(low):
            continue
        sentence_required = bool(_REQUIRED_WORDS.search(low)) and not _OPTIONAL_WORDS.search(low)
        sentence_negated = bool(_NOT_NEEDED.search(low))
        # Classify per clause so "Fluent English required; Finnish is a plus" splits correctly;
        # a clause without any keyword inherits the sentence-level verdict.
        for clause in _CLAUSE_SPLIT.split(low):
            hits = _mentioned(clause)
            if not hits:
                continue
            out.mentioned |= hits
            has_req, has_opt, has_neg = _REQUIRED_WORDS.search(clause), _OPTIONAL_WORDS.search(clause), _NOT_NEEDED.search(clause)
            if has_neg or (not has_req and not has_opt and sentence_negated):
                out.optional |= hits
                continue
            if has_req or has_opt:
                required = bool(has_req) and not has_opt
            else:
                required = sentence_required
            for code in hits:
                if required:
                    out.required.add(code)
                    out.evidence.setdefault(code, s[:200])
                else:
                    out.optional.add(code)
    # A language that is required in one sentence and optional in another counts as required,
    # except when explicitly negated; keep it simple.
    out.optional -= out.required
    return out
