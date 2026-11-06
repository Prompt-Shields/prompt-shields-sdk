"""Opt-in PII categories: person names, postal addresses, and health and
financial vocabulary beyond English (#351).

Nothing here runs unless it is called: ``detect_pii_categories`` in
``prompt_shields.pii``, the instrumented clients, the router and the
pseudonymiser are unchanged. ``detect_pii_categories_extended`` returns what
``detect_pii_categories`` returns, plus:

- ``person_name``: a common given name followed by a capitalised surname
  ("Lars Andersson", "Daan de Jong"), a title ("Frau Weber", "M. Dubois"), or
  a cue ("my name is", "jag heter", "Name:") followed by a capitalised word.
  With ``ner="spacy"`` (the ``prompt-shields[ner]`` extra plus a spaCy model,
  ``xx_ent_wiki_sm`` by default) the model's ``PER`` entities are added too.
- ``postal_address``: a street with a house number in the forms used in the
  benchmark's locales ("Storgatan 12", "Kongens gate 5", "12 rue de la Paix",
  "Via Roma 3", "ul. Długa 5", "10 High Street") or a UK postcode.
- ``health_data`` and ``financial_data``: wider, multilingual keyword lists
  (sv, nb, da, fi, de, fr, nl, es, it, pl, en), matched on whole words or
  word stems chosen so that, for example, "diagnostics" is not "diagnosis".
  A checksum-valid IBAN also counts as ``financial_data``.

These are signals for detection and telemetry only. They must not be used to
block requests until security signs off their thresholds (#35, #351); the
measured recall is in ``benchmarks/pii/README.md``.

Pure Python and linear in the input for the rule-based part. Detects
presence only; matched text is never returned, logged or stored.
"""

from __future__ import annotations

import re
from functools import lru_cache

from prompt_shields.pii import detect_pii_categories

#: Categories only this module reports.
OPT_IN_CATEGORIES: tuple[str, ...] = ("person_name", "postal_address")

#: Default spaCy model for ``ner="spacy"``: multilingual, PER/LOC/ORG/MISC.
DEFAULT_SPACY_MODEL = "xx_ent_wiki_sm"

_TOKEN = re.compile(r"\w+")


def _words(s: str) -> frozenset[str]:
    return frozenset(s.split())


# --- person names --------------------------------------------------------------

# Common given names in the benchmark's locales (and English). A name is a
# given name followed by a capitalised surname, so "Anna" alone or "Maria"
# before a lowercase word is not reported.
_GIVEN_NAMES = _words("""
Agnes Albin Alexander Alfred Alice Alma Anders Andreas Anna Anton Astrid Axel
Birgitta Björn Carl Charlotte Christina Daniel David Ebba Elin Elsa Emil Emma
Erik Eva Filip Fredrik Gustav Hanna Hans Helena Henrik Hugo Ingrid Isak Jakob
Johan Johanna Jonas Josefin Karin Karl Kerstin Kristina Lars Lena Linnea Lisa
Lucas Magnus Malin Maria Marie Martin Matilda Mats Mattias Maja Mikael Moa
Nils Olof Oskar Per Peter Sara Sofia Stefan Sven Thomas Tove Ulf Viktor Wilma
Arne Bjørn Eirik Even Hilde Ingeborg Jon Kari Kjetil Knut Lise Magne Marit
Nora Ola Ole Rune Sigrid Silje Sindre Siri Solveig Stian Synne Thea Tor Trond
Turid Vegard Øystein Anne Bente Christian Freja Frederik Ida Jens Jesper
Kasper Lone Mads Mette Mikkel Morten Niels Pernille Poul Rasmus Rikke Signe
Søren Trine Aino Antti Eeva Eino Elina Heikki Jari Juha Jukka Kaisa Kalle
Leena Liisa Matti Mikko Minna Niina Onni Pekka Riikka Sanna Satu Timo Tuomas
Ville Ben Elias Felix Finn Hannah Jan Jürgen Katharina Klaus Lea Leon Lukas
Luisa Maximilian Mia Michael Monika Noah Paul Sabine Sophie Stefanie Thorsten
Uwe Wolfgang Antoine Camille Chloé Claire Élise Inès Jean Jeanne Juliette Léa
Louis Manon Marc Margaux Mathilde Nicolas Philippe Pierre Théo Bram Daan Fenna
Femke Jasper Joost Julia Lieke Lotte Luuk Maartje Mila Noor Pieter Ruben Sanne
Sem Thijs Tim Willem Alejandro Ana Antonio Carlos Carmen Diego Elena Francisco
Isabel Javier Jorge José Juan Laura Lucía Luis Manuel María Marta Miguel Pablo
Paula Raquel Sergio Alessandro Andrea Chiara Davide Federica Francesca
Francesco Giorgia Giovanni Giulia Giuseppe Lorenzo Luca Marco Matteo Paola
Roberto Simone Valentina Agnieszka Aleksandra Barbara Jakub Joanna Kacper
Katarzyna Krzysztof Magdalena Małgorzata Marcin Michał Piotr Tomasz Wojciech
Zofia Zuzanna Amelia Charlie Edward Emily George Grace Harry Isla Jack James
Jessica John Lily Mary Oliver Olivia Robert Sarah Sophia William
""")

# Lowercase surname particles: "Daan de Jong", "Ludwig van Beethoven".
_PARTICLES = _words("""
af av d da dal de del della den der di dos du el la le ten ter van von zu
""")

# Capitalised words that follow a given name without making a person name.
_NOT_SURNAMES = _words("""
AB AS ASA ApS Oy Oyj GmbH AG SA SAS SRL BV NV Ltd Inc LLC PLC Street Road
Avenue Hospital Hotel Church Kirke Kyrka School Skola University January
February March April May June July August September October November December
Monday Tuesday Wednesday Thursday Friday Saturday Sunday
""")

_TITLES = _words("""
Mr Mrs Ms Miss Dr Prof Sir Dame Herr Frau Fru Hr Mme Mlle Monsieur Madame M Sr
Sra Srta Señor Señora Don Doña Sig Signor Signora Dott Pan Pani Dhr Mevr
Mevrouw Meneer
""")

# Token sequences after which a capitalised word is a name.
_NAME_CUES = (
    ("my", "name", "is"), ("jag", "heter"), ("jeg", "heter"), ("jeg", "hedder"),
    ("nimeni", "on"), ("ich", "heiße"), ("ich", "heisse"), ("mein", "name", "ist"),
    ("je", "m", "appelle"), ("mijn", "naam", "is"), ("ik", "heet"), ("me", "llamo"),
    ("mi", "nombre", "es"), ("mi", "chiamo"), ("nazywam", "się"),
)
_NAME_CUE_HEADS = {cue[-1] for cue in _NAME_CUES}
# Form labels followed by ":" and a capitalised word ("Namn: Lars ...").
_NAME_LABELS = _words("""
name namn navn nimi nom naam nombre nome nazwisko patient patienten
""")


def _capitalised(tok: str) -> bool:
    return tok[:1].isupper() and (len(tok) == 1 or not tok.isupper())


def _surname_like(tok: str) -> bool:
    return (len(tok) >= 2 and _capitalised(tok) and tok not in _NOT_SURNAMES
            and not any(c.isdigit() for c in tok))


def _has_rule_name(text: str, toks: list[re.Match]) -> bool:
    n = len(toks)

    def sep(i: int) -> str:  # text between token i and token i + 1
        return text[toks[i].end():toks[i + 1].start()]

    for i, m in enumerate(toks):
        tok = m.group()
        if tok in _GIVEN_NAMES and i + 1 < n:
            j = i
            # Hyphenated given names: "Anna-Karin Berg" (at most two more
            # parts, so a long hyphen chain is not rescanned per name).
            while (j - i < 2 and j + 1 < n and sep(j) == "-"
                   and _capitalised(toks[j + 1].group())):
                j += 1
            if j + 1 < n and sep(j) == " ":
                k = j + 1
                # Up to two particles: "van der Berg", "de la Cruz".
                for _ in range(2):
                    if k + 1 < n and toks[k].group() in _PARTICLES and sep(k) == " ":
                        k += 1
                if _surname_like(toks[k].group()):
                    return True
        # The rest need a capitalised word right after this token.
        if i + 1 >= n or not _surname_like(toks[i + 1].group()):
            continue
        if tok in _TITLES and sep(i) in (" ", ". "):
            return True
        low = tok.lower()
        if low in _NAME_LABELS and sep(i).strip() == ":":
            return True
        if low in _NAME_CUE_HEADS and sep(i).strip() in ("", ","):
            for cue in _NAME_CUES:
                start = i - len(cue) + 1
                if cue[-1] == low and start >= 0 and all(
                        toks[start + c].group().lower() == w for c, w in enumerate(cue)):
                    return True
    return False


# --- postal addresses ----------------------------------------------------------

_UP = "A-ZÀ-ÖØ-ÞĄĆČĘŁŃŚŠŹŻŽ"
_NUM = r"\d{1,4}[a-zA-Z]?(?!\d)"

_ADDRESS_PATTERNS = (
    # Street name ending in a street word, then a number: "Storgatan 12",
    # "Vesterbrogade 4", "Hämeenkatu 9", "Bahnhofstraße 3", "Kerkstraat 7".
    # One run of word characters per start (\w*), so scanning stays linear.
    re.compile(
        rf"(?<!\w)[{_UP}]\w*(?:gatan|gata|gate|gränd|vägen|väg|veien|vei|vegen|veg|"
        r"gade|vej|stræde|torv|katu|tie|kuja|polku|tori|straße|strasse|weg|allee|"
        r"platz|gasse|ring|straat|laan|plein|gracht|kade|singel|dijk)"
        rf",? {_NUM}"
    ),
    # Street word as a separate word: "Kongens gate 5", "Lange Straße 3".
    re.compile(
        rf"(?<!\w)[{_UP}]\w* (?:gate|gata|vei|veg|vej|gade|allé|Straße|Strasse|Weg|"
        rf"Allee|Platz|Gasse)\.?,? {_NUM}"
    ),
    # Number, then a street word: "12 rue de la République", "4 avenue Foch".
    re.compile(
        rf"(?<![\w,.]){_NUM}(?: bis| ter)?,? (?:rue|avenue|av\.|boulevard|bd|place|"
        rf"chemin|allée|impasse|quai|cours|route) (?:(?:de|du|des|la|le|d['’]|l['’]) ?)*[{_UP}]"
    ),
    # Street word, a capitalised name, then a number: "Calle Mayor 12",
    # "Via Roma, 3", "ul. Długa 5", "Avenida da Liberdade 10".
    re.compile(
        rf"(?<!\w)(?:Calle|C/|Carrer|Avenida|Avda\.|Plaza|Paseo|Camino|Ronda|Via|Viale|"
        rf"Corso|Piazza|Piazzale|Largo|Vicolo|ul\.|Ul\.|ulica|al\.|Al\.|aleja|pl\.|os\.|Rua) "
        rf"(?:(?:de|del|di|della|da|dos|la) )?[{_UP}][\w'’]*(?: [\w'’]+){{0,3}}?,? {_NUM}"
    ),
    # Number, one to three capitalised words, a street word: "10 High Street",
    # "221B Baker St".
    re.compile(
        rf"(?<![\w,.]){_NUM} (?:[{_UP}][\w'’]* ){{1,3}}(?:Street|St|Road|Rd|Avenue|Ave|"
        r"Lane|Ln|Drive|Dr|Way|Close|Court|Ct|Place|Pl|Square|Sq|Terrace|Crescent|"
        r"Gardens|Grove|Hill|Row|Mews|Parade|Walk|Boulevard|Blvd)\b"
    ),
    # UK postcode: "BS1 2AW", "SW1A 1AA".
    re.compile(r"(?<!\w)[A-Z]{1,2}\d[A-Z\d]? \d[ABD-HJLNP-UW-Z]{2}(?!\w)"),
)


def _has_rule_address(text: str) -> bool:
    return any(p.search(text) for p in _ADDRESS_PATTERNS)


# --- health and financial vocabulary -------------------------------------------

# Whole words and phrases (lowercase, tokens joined by one space: "reg.nr" is
# "reg nr"), word stems (a token that starts with one), and case-sensitive
# acronyms. Ambiguous words are left out: "recept"/"Rezept"/"receta" are also
# recipes, "diagnostic" is also a software check, "enceinte" a loudspeaker,
# "Verordnung" a regulation, "prescrizione" a statute of limitations.
_VOCAB: dict[str, dict[str, frozenset[str]]] = {
    "health_data": {
        "phrases": frozenset({
            # en
            "diagnosis", "diagnosed", "prescription", "prescribed", "medication",
            "patient id", "medical record", "medical records", "medical history",
            "medical condition", "icd 10", "blood pressure", "blood test", "blood sugar",
            "lab result", "lab results", "sick leave", "diabetes", "asthma",
            "epilepsy", "migraine", "dementia", "hepatitis", "hiv", "insulin",
            "chemotherapy", "antidepressant", "antidepressants", "pregnant", "pregnancy",
            # sv
            "diagnos", "diagnosen", "diagnoser", "medicin", "medicinen", "mediciner",
            "patientjournal", "vårdcentral", "vårdcentralen", "astma", "migrän",
            "epilepsi", "demens", "gravid",
            # nb
            "diagnose", "resept", "resepten", "resepter", "lege", "legen", "fastlege",
            "fastlegen", "medisin", "medisinen", "medisiner", "kreft", "migrene",
            # da
            "læge", "lægen", "migræne", "kræft", "sukkersyge", "ordineret",
            # fi
            "migreeni", "insuliini", "raskaana", "raskaus",
            # de
            "arzt", "ärztin", "ärzte", "hausarzt", "hausärztin", "ärztlich",
            "krankgeschrieben", "migräne", "epilepsie", "schwanger",
            "schwangerschaft",
            # fr
            "ordonnance", "médecin", "médecins", "tension artérielle", "arrêt maladie",
            "asthme", "diabète", "grossesse", "insuline",
            # nl
            "huisarts", "medicatie", "kanker", "zwanger", "zwangerschap", "ziekgemeld",
            # es
            "diagnóstico", "receta médica", "médico", "médica", "presión arterial",
            "baja médica", "asma", "cáncer", "migraña", "embarazada", "embarazo",
            "insulina",
            # it
            "diagnosi", "medico", "pressione arteriosa", "pressione sanguigna",
            "diabete", "cancro", "emicrania", "incinta", "gravidanza", "ricetta medica",
            # pl
            "recepta", "receptę", "recepty", "leki", "leków", "ciśnienie krwi",
            "zwolnienie lekarskie", "nowotwór", "migrena", "ciąża", "ciąży",
        }),
        "stems": frozenset({
            "diagnostis",  # sv diagnostiserad, nb diagnostisert
            "diagnosticer", "gediagnosticeer", "diagnostiz", "diagnostiqu",
            "diagnosticad", "diagnosticat", "zdiagnozow", "diagnoosi", "diagnoz",
            "läkar", "blodtryck", "blodtrykk", "blodtryk", "sjukskriv", "sjukdom",
            "sykemeld", "sykdom", "sygemeld", "sygdom", "lægeerklæring",
            "lääkär", "lääkk", "lääkit", "verenpain", "resepti", "sairau", "syöpä", "syövä",
            "blutdruck", "medikament", "krankschreib", "krankheit",
            "médicament", "maladie", "bloeddruk", "medicijn", "ziekte",
            "medicament", "enfermedad", "farmac", "malatti", "lekarz", "chorob",
            "cukrzyc",
        }),
        "acronyms": frozenset(),
    },
    "financial_data": {
        "phrases": frozenset({
            # en
            "account number", "routing number", "iban", "swift code", "tax id",
            "vat number", "bank account", "bank details", "sort code",
            # sv, nb, da
            "bankgiro", "plusgiro", "lönekonto", "kontonr", "reg nr",
            # fi
            "tilinumero", "tilinumeron", "tilinumeroa",
            # de
            "bankverbindung", "bankleitzahl", "steuernummer", "steuer id",
            "steueridentifikationsnummer",
            # fr
            "numéro de compte", "numéro fiscal", "compte bancaire", "coordonnées bancaires",
            # nl
            "btw nummer", "bankrekening",
            # es
            "número de cuenta", "cuenta bancaria", "datos bancarios",
            # it
            "numero di conto", "conto corrente", "partita iva", "codice fiscale",
            "coordinate bancarie",
            # pl
            "numer rachunku", "numer konta", "rachunek bankowy", "konto bankowe",
        }),
        "stems": frozenset({
            "kontonum",  # kontonummer, kontonumret, kontonummeret
            "clearingnummer", "bankkonto", "rekeningnummer", "pankkitil",
        }),
        "acronyms": frozenset({"RIB", "NIF", "NIP", "BLZ", "IBAN", "BIC"}),
    },
}
_STEM_LENGTHS = {
    cat: sorted({len(s) for s in v["stems"]}) for cat, v in _VOCAB.items()
}


def _vocabulary_categories(toks: list[re.Match]) -> set[str]:
    found: set[str] = set()
    words = [m.group() for m in toks]
    lower = [w.lower() for w in words]
    for cat, vocab in _VOCAB.items():
        phrases, stems, acronyms = vocab["phrases"], vocab["stems"], vocab["acronyms"]
        lengths = _STEM_LENGTHS[cat]
        for i, w in enumerate(lower):
            if (w in phrases or words[i] in acronyms
                    or any(w[:k] in stems for k in lengths if k <= len(w))
                    or (i + 1 < len(lower) and f"{w} {lower[i + 1]}" in phrases)
                    or (i + 2 < len(lower)
                        and f"{w} {lower[i + 1]} {lower[i + 2]}" in phrases)):
                found.add(cat)
                break
    return found


# --- optional NER (spaCy) --------------------------------------------------------

@lru_cache(maxsize=4)
def _spacy_pipeline(model: str):
    try:
        import spacy
    except ImportError as exc:  # pragma: no cover - depends on the extra
        raise ImportError(
            "ner='spacy' needs the optional NER extra: pip install 'prompt-shields[ner]' "
            f"and a spaCy model (python -m spacy download {model})"
        ) from exc
    # Only the entity recogniser is needed.
    return spacy.load(model, exclude=["parser", "tagger", "lemmatizer", "attribute_ruler"])


def _has_ner_name(text: str, model: str) -> bool:
    doc = _spacy_pipeline(model)(text)
    return any(ent.label_ in ("PER", "PERSON") for ent in doc.ents)


# --- public API ------------------------------------------------------------------

def detect_pii_categories_extended(
    text: str | None,
    *,
    names: bool = True,
    addresses: bool = True,
    vocabulary: bool = True,
    ner: str | None = None,
    ner_model: str = DEFAULT_SPACY_MODEL,
) -> list[str]:
    """``detect_pii_categories(text)`` plus the opt-in categories, sorted.

    ``names`` / ``addresses`` add ``person_name`` / ``postal_address``;
    ``vocabulary`` adds the multilingual ``health_data`` / ``financial_data``
    keywords (and ``financial_data`` for a checksum-valid IBAN). ``ner=None``
    uses the rule-based name detector only; ``ner="spacy"`` also adds the
    ``PER`` entities of the spaCy model ``ner_model`` (needs the
    ``prompt-shields[ner]`` extra and the model installed; raises ImportError
    or OSError otherwise, never falls back silently).

    Best-effort signals for detection and telemetry; not for blocking until
    their thresholds are signed off (#35).
    """
    if not text:
        return []
    if ner not in (None, "spacy"):
        raise ValueError(f"unknown ner backend {ner!r}: use None or 'spacy'")
    found = set(detect_pii_categories(text))
    toks = list(_TOKEN.finditer(text)) if names or vocabulary else []
    if names and (_has_rule_name(text, toks)
                  or (ner == "spacy" and _has_ner_name(text, ner_model))):
        found.add("person_name")
    if addresses and _has_rule_address(text):
        found.add("postal_address")
    if vocabulary:
        found |= _vocabulary_categories(toks)
        if "iban" in found:
            found.add("financial_data")
    return sorted(found)
