"""The inline Octicons helper."""
import pytest

from app.icons import _ICONS, icon


def test_icon_is_decoration_by_default() -> None:
    html = str(icon("git-merge"))
    assert html.startswith('<svg class="octicon octicon-git-merge" width="16" height="16"')
    assert 'aria-hidden="true"' in html and "role=" not in html
    assert html.endswith("</path></svg>")


def test_icon_label_is_escaped() -> None:
    html = str(icon("tag", label='a "b" <c>'))
    assert 'role="img" aria-label="a &#34;b&#34; &lt;c&gt;"' in html
    assert "aria-hidden" not in html


def test_unknown_icon_fails() -> None:
    with pytest.raises(KeyError):
        icon("no-such-icon")


def test_icon_data_is_only_paths() -> None:
    # The SVG body is trusted markup; it must hold nothing but path elements.
    for width, body in _ICONS.values():
        assert width == 16
        assert body.startswith("<path ") and body.endswith("</path>")
        assert "<script" not in body and " on" not in body
