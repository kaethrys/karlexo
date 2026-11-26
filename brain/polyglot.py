import re

# Pre-compiled Unicode block scanners (microsecond execution, 0 MB extra RAM)
_SCRIPTS = [
    ("gu", "Gujarati", re.compile(r"[\u0A80-\u0AFF]")),
    ("bn", "Bengali", re.compile(r"[\u0980-\u09FF]")),
    ("ta", "Tamil", re.compile(r"[\u0B80-\u0BFF]")),
    ("te", "Telugu", re.compile(r"[\u0C00-\u0C7F]")),
    ("ar", "Arabic", re.compile(r"[\u0600-\u06FF]")),
    ("ja", "Japanese", re.compile(r"[\u3040-\u30FF\u4E00-\u9FFF]")),
    ("ko", "Korean", re.compile(r"[\uAC00-\uD7AF\u1100-\u11FF\u3130-\u318F]")),
    ("ru", "Russian", re.compile(r"[\u0400-\u04FF]")),
]

_DEVANAGARI = re.compile(r"[\u0900-\u097F]")
_SANSKRIT_MARKERS = re.compile(
    r"(?:अस्ति|भवति|कथम्|किम्|श्लोक|वदतु|नमः|तत्त्व|संस्कृत|\b(?:katham|bhavati|asti|vadatu|shloka|sanskrit)\b)",
    re.IGNORECASE,
)
_MARATHI_MARKERS = re.compile(
    r"(?:आहे|काय|कसं|कसे|सांग|करा|मराठी|आणि|\b(?:kasa\s+ahes|kay\s+challay|sang\s+na|marathi)\b)",
    re.IGNORECASE,
)

# Transliterated / Romanized & Latin-script lexical cues (word-boundary anchored)
_ROMAN_CUES = [
    ("gu", "Gujarati", re.compile(r"\b(?:kem\s+cho|shu\s+che|mane\s+samjav|gujarati|majama)\b", re.I)),
    ("bn", "Bengali", re.compile(r"\b(?:kemon\s+acho|ki\s+korcho|bhalo\s+achi|bengali|bangla)\b", re.I)),
    ("ta", "Tamil", re.compile(r"\b(?:epdi\s+irukka|enna\s+panra|vanakkam|sollu\s+da|tamil)\b", re.I)),
    ("te", "Telugu", re.compile(r"\b(?:ela\s+unnav|enti\s+sangathi|cheppu\s+bro|namaskaram|telugu)\b", re.I)),
    ("mr", "Marathi", re.compile(r"\b(?:kasa\s+ahes|kay\s+challay|sang\s+na|marathi)\b", re.I)),
    ("ar", "Arabic", re.compile(r"\b(?:marhaba|shukran|kayf\s+halak|yalla|arabic)\b", re.I)),
    ("es", "Spanish", re.compile(r"(?:¿|¡|\b(?:hola|cómo|qué\s+tal|explica|dime|español)\b)", re.I)),
    ("fr", "French", re.compile(r"\b(?:bonjour|salut|comment\s+ça\s+va|explique|français)\b", re.I)),
    ("de", "German", re.compile(r"(?:ß|\b(?:hallo|wie\s+geht|erkläre|bitte|deutsch)\b)", re.I)),
    ("pt", "Portuguese", re.compile(r"(?:\b\w+ão\b|\b(?:olá|como\s+está|explica|português)\b)", re.I)),
    ("ja", "Japanese", re.compile(r"\b(?:konnichiwa|arigatou|nihongo|japanese)\b", re.I)),
    ("ko", "Korean", re.compile(r"\b(?:annyeong|kamsahamnida|hangul|korean)\b", re.I)),
    ("ru", "Russian", re.compile(r"\b(?:privet|spasibo|kak\s+dela|russian)\b", re.I)),
    ("hi", "Hinglish/Hindi", re.compile(r"\b(?:kya|bhai|yrr|yaar|suna|bata|kaise|kyu|samjha)\b", re.I)),
]


def detect_language(text: str) -> tuple[str, str]:
    """Returns (iso_code, language_label) in <0.1ms."""
    if not text:
        return ("en", "English")

    # 1. Unique/non-Devanagari script check
    for code, label, pattern in _SCRIPTS:
        if pattern.search(text):
            return (code, label)

    # 2. Devanagari split: Sanskrit vs Marathi vs Hindi
    if _DEVANAGARI.search(text):
        if _SANSKRIT_MARKERS.search(text):
            return ("sa", "Sanskrit")
        if _MARATHI_MARKERS.search(text):
            return ("mr", "Marathi")
        return ("hi", "Hindi")

    # 3. Transliterated / Romanized & Latin cues
    if _SANSKRIT_MARKERS.search(text):
        return ("sa", "Sanskrit")

    for code, label, pattern in _ROMAN_CUES:
        if pattern.search(text):
            return (code, label)

    return ("en", "English")


def polyglot_directive(text: str) -> str:
    """Compact system instruction injected only when non-English is detected."""
    code, label = detect_language(text)
    if code == "en":
        return ""
    return (
        f"[POLYGLOT MODE: {label} ({code})] Respond naturally and fluently in {label}, "
        "matching the user's exact script/register (native script if they used native script, "
        "or clean Romanized/code-mixed style if they wrote Romanized text). Keep technical terms, "
        "code, tickers, and numbers crisp and accurate."
    )
