from hazm import Normalizer

# Initialize normalizer once
normalizer = Normalizer(
    correct_spacing=True,
    persian_style=True,
    persian_numbers=True,
    remove_diacritics=True,
)

def normalize_persian_text(text: str) -> str:
    """Normalizes Persian text using Hazm."""
    if not text:
        return ""
    return normalizer.normalize(text)
