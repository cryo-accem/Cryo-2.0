import re
import unicodedata


_PI_TITLES = re.compile(
    r"^(?:(?:prof(?:essor)?|dr|doctor|mr|mrs|ms|miss)\.?\s*)+",
    re.IGNORECASE,
)


def normalize_pi_name(value):
    """Return a comparison key that ignores honorifics and punctuation."""
    text = unicodedata.normalize("NFKC", str(value or "")).strip()
    text = _PI_TITLES.sub("", text)
    return re.sub(r"[^a-z0-9]+", "", text.casefold())


def clean_display_name(value):
    """Collapse formatting noise while retaining a useful display label."""
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).split())


def preferred_pi_label(current, candidate):
    """Prefer the more informative formatted name for a grouped PI."""
    current = clean_display_name(current)
    candidate = clean_display_name(candidate)
    if not current:
        return candidate
    if not candidate:
        return current
    return candidate if len(candidate) > len(current) else current
