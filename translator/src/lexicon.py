"""Persistent lexicons and fluency data for specific dialects.
Data sourced from authentic publications (JW.org) to ensure high-fidelity translation."""

from __future__ import annotations

# Lexicon entries are injected into the Gemini system prompt.
# Format: { "concept": "target_dialect_term" }
DIALECT_LEXICONS: dict[str, dict[str, str]] = {
    "itv": {
        # Core Vocabulary (Itawit/Itawes)
        "God": "Dios (or Namaratu for Creator)",
        "life": "attolay",
        "forever": "mannanayun",
        "truth": "kinakurug",
        "world": "mundo",
        "good news": "nakakkasta nga balita",
        "kingdom": "pappatulan",
        "death": "nakkakatay",
        "suffering": "pazziriyat",
        "word": "ergo",
        "people": "tattolay",
        "family": "famillia",
        "study": "paggilammu",
        "answer": "tabbag",
        "future": "mappanget",
        "now": "sangaw",
        "today": "sangaw",
        "many": "aru",
        "more": "mas aru peba",
        "yes": "oon",
        "no": "awan",
        # Common Phrases
        "welcome": "Welcome kan",
        "do you want to know?": "Ikayat Mu nga mannammuan?",
        "where can we find?": "Pakatafulan tera kan...?",
        "what is?": "Hanna yo...?",
        "who are?": "Innia ira yo...?",
        "happy family": "napagayayat nga famillia",
        # Grammatical Markers
        "the": "yo",
        "to/in/at/for": "kan",
        "plural marker": "ira",
        "linker": "nga",
        "you (subject)": "ka",
        "you (possessive)": "mu",
    },
    "byv": {
        # Core Vocabulary (Medumba/Nufi)
        "God": "Nsi",
        "life": "yôg",
        "forever": "matmat",
        "truth": "nenùne",
        "love": "nkôni",
        "people": "benntùn",
        "way": "manze",
        "book": "ñwaʼni",
        "bible": "Ñwaʼni Nsi",
        "word": "ntshu",
        "voice": "ntshu",
        "language": "ntshu",
        "one": "taʼ",
        "all": "njoñ",
        "every": "njoñ",
        "about": "num",
        "study": "ziʼde",
        "learn": "ziʼde",
        "teach": "tswide",
        "yes": "ee",
        "no": "ŋga'a",
        # Common Phrases
        "enjoy life forever": "Jù bin yôg matmat",
        "listen to God": "Juʼ ntshu Nsi",
        "hear God's word": "Juʼ ntshu Nsi",
        "a conversation": "taʼ nswade",
        # Grammatical Markers
        "you (plural)": "bin",
        "and/as/like": "mba",
        "question marker": "yi à? (placed at end of sentence)",
    }
}

def get_lexicon_instructions(lang_code: str) -> str:
    """Returns a formatted instruction string for the given language code."""
    lexicon = DIALECT_LEXICONS.get(lang_code)
    if not lexicon:
        return ""

    # Specific instructions for syntax and markers (MAJOR UPDATE 3: GRAMMAR SKELETON ANCHORING)
    syntax_extra = ""
    if lang_code == "itv":
        syntax_extra = (
            "\n\nGRAMMAR SKELETON for Itawit:\n"
            "- Word Order: Verbs usually come FIRST in the sentence (V-S-O structure).\n"
            "- Connectives: Use 'nga' as a mandatory linker between adjectives and the nouns they describe.\n"
            "- Plurality: Place 'ira' AFTER the noun, never before.\n"
            "- Respect: Use appropriate respectful particles if the source is formal."
        )
    elif lang_code == "byv":
        syntax_extra = (
            "\n\nGRAMMAR SKELETON for Medumba:\n"
            "- Tones: Medumba is highly tonal; adjust your pitch to match the meaning of the lexicon provided.\n"
            "- Negation: Use 'ŋga'a' correctly at the start or end as per local Bangangte patterns.\n"
            "- Word Order: Maintain a strict S-V-O structure unless emphasizing a specific noun."
        )

    lines = [f'  - "{concept}" should be translated as "{term}"' for concept, term in lexicon.items()]
    return (
        "\n\nCRITICAL FLUENCY DATA: Use the following specific lexicon and syntax rules for this dialect. "
        "This data MUST take precedence to ensure the output sounds like a native human speaker:\n"
        + "\n".join(lines)
        + syntax_extra
    )
