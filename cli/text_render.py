
import re

try:
    import arabic_reshaper
    from bidi.algorithm import get_display as _bidi_get_display
    BIDI_AVAILABLE = True
except ImportError:
    BIDI_AVAILABLE = False

_ARABIC_RE = re.compile(
    r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\uFB50-\uFDFF\uFE70-\uFEFF]"
)


def display_text(text: str) -> str:
    """Reshape + bidi-reorder Arabic text for correct terminal display.

    No-ops instantly (regex miss) for pure Latin/other-script text, so it's
    safe to call on every render tick / every print without a perf hit.
    Reshaping/reordering is done per line so multi-line output doesn't get
    its line order scrambled by the bidi algorithm.
    """
    if not BIDI_AVAILABLE or not text or not _ARABIC_RE.search(text):
        return text
    try:
        return "\n".join(
            _bidi_get_display(arabic_reshaper.reshape(line)) if line else line
            for line in text.split("\n")
        )
    except Exception:
        # Never let a display-only transform break the actual response.
        return text
