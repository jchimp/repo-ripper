"""Server-side syntax highlighting with Pygments.

The output uses CSS classes only (`style.css` maps them to the theme
variables), one HTML fragment for each source line.
"""
from markupsafe import Markup
from pygments import highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import get_lexer_for_filename
from pygments.util import ClassNotFound

MAX_HIGHLIGHT_CHARS = 300_000

_FORMATTER = HtmlFormatter(nowrap=True)


def split_lines(text: str) -> list[str]:
    """Split text into lines the same way for the plain and the coloured view."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if len(lines) > 1 and lines[-1] == "":
        lines.pop()
    return lines


def highlight_lines(filename: str, text: str) -> list[Markup] | None:
    """Return one highlighted HTML fragment for each line, or None.

    None means "show the plain text": the file type is not known, the text is
    larger than `MAX_HIGHLIGHT_CHARS`, or the result did not line up.

    The fragments are safe: Pygments escapes the source text and adds only
    `<span class="...">` tags.
    """
    if len(text) > MAX_HIGHLIGHT_CHARS:
        return None
    try:
        lexer = get_lexer_for_filename(filename, stripnl=False, ensurenl=False)
    except ClassNotFound:
        return None
    plain = split_lines(text)
    try:
        html = highlight("\n".join(plain), lexer, _FORMATTER)
    except Exception:  # noqa: BLE001  a lexer bug must not break the page
        return None
    lines = html.split("\n")
    if len(lines) > len(plain) and lines[-1] == "":
        lines.pop()
    if len(lines) != len(plain):
        return None
    return [Markup(line) for line in lines]
