"""Markdown rendering: safe output and local links."""
import re

import pytest

from app import highlight
from app.mdrender import LinkContext, render

SHA = "0123456789abcdef0123456789abcdef01234567"
CTX = LinkContext(repo_base="/repos/7", code_base="/repos/7", rev="feature/x",
                  directory="docs", numbers={1: "pull", 2: "issue"}, link_commits=True)


def hrefs(html: str) -> list[str]:
    return re.findall(r'href="([^"]*)"', html)


def test_raw_html_is_shown_as_text() -> None:
    html = render("<script>alert(1)</script>\n\n<b onclick=\"x()\">bold</b> "
                  "<img src=x onerror=y>\n\n<iframe src=\"https://e.com\"></iframe>")
    assert "<script" not in html and "<b " not in html and "<img" not in html
    assert "<iframe" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


@pytest.mark.parametrize("target", [
    "javascript:alert(1)", "JaVaScRiPt:alert(1)", "vbscript:x", "data:text/html,<script>",
    "file:///etc/passwd", "&#106;avascript:alert(1)", "java&#x09;script:alert(1)",
    "ftp://e.com/x", "blob:x", "x-custom:run",
])
def test_unsafe_link_schemes_make_no_link(target: str) -> None:
    for ctx in (None, CTX):
        html = render(f"[click]({target}) ![img]({target})", ctx)
        for href in hrefs(html):
            assert href.startswith("/repos/7/"), href  # at most a harmless local path
        assert "src=" not in html
        assert not re.search(r'href="\s*(javascript|vbscript|data|file|ftp|blob|x-custom):',
                             html, re.I)


def test_angle_bracket_link_with_a_tab_cannot_become_a_script_link() -> None:
    html = render("[x](<java\tscript:alert(1)>)", None)
    assert hrefs(html) == []


def test_external_links_get_rel() -> None:
    html = render("[a](https://e.com/x) <https://auto.example> [m](mailto:a@b.c) "
                  "[p](//proto.example/x) and https://bare.example/path.")
    assert html.count('rel="noopener noreferrer nofollow"') == 5
    assert 'href="https://bare.example/path"' in html  # the final dot is not in the link


def test_remote_images_are_not_loaded() -> None:
    html = render("![the alt](https://img.example.com/a.png) ![x](//img.example.com/b.png)", CTX)
    assert "<img" not in html
    assert ('<a class="md-image" href="https://img.example.com/a.png" '
            'rel="noopener noreferrer nofollow"') in html
    assert ">the alt</a>" in html


def test_image_inside_a_link_makes_one_link() -> None:
    html = render("[![build](https://ci.example/b.svg)](https://ci.example/run)", CTX)
    assert html.count("<a ") == 1 and "<img" not in html
    assert '<span class="md-image">build</span>' in html


def test_relative_links_go_to_the_local_file_pages() -> None:
    html = render("[up](../README.md#usage) [same](other.md) [root](/src/a%20b.py) "
                  "[dir](/) [frag](#top)", CTX)
    assert hrefs(html) == [
        "/repos/7/blob/README.md?rev=feature%2Fx#usage",
        "/repos/7/blob/docs/other.md?rev=feature%2Fx",
        "/repos/7/blob/src/a%20b.py?rev=feature%2Fx",
        "/repos/7/tree/?rev=feature%2Fx",
        "#top",
    ]


def test_relative_images() -> None:
    html = render("![p](img/a.PNG) ![s](img/a.svg) ![up](../../../x.png)", CTX)
    assert '<img src="/repos/7/raw/docs/img/a.PNG?rev=feature%2Fx" alt="p" loading="lazy">' in html
    # SVG is never an <img>: it goes to the file page.
    assert '<a class="md-image" href="/repos/7/blob/docs/img/a.svg?rev=feature%2Fx">s</a>' in html
    assert html.count("<img") == 1
    assert '<span class="md-image">up</span>' in html


def test_relative_link_cannot_leave_the_repository() -> None:
    html = render("[x](../../etc/passwd) [y](/../../x)", CTX)
    assert hrefs(html) == []
    assert html.count('class="md-nolink"') == 2


def test_relative_links_without_a_code_context_have_no_target() -> None:
    html = render("[x](docs/a.md) ![i](a.png)", LinkContext(repo_base="/repos/7"))
    assert hrefs(html) == [] and "<img" not in html


def test_numbers_and_shas_become_local_links() -> None:
    html = render(f"Fixes #1, see #2 and #99. abc#1 &#1; `#1` [#2](https://e.com) {SHA} "
                  f"x{SHA}", CTX)
    assert '<a href="/repos/7/pulls/1">#1</a>' in html
    assert '<a href="/repos/7/issues/2">#2</a>' in html
    assert "#99." in html and "/99" not in html
    assert html.count("/repos/7/pulls/1") == 1  # not inside a word, code or another link
    assert f'<a href="/repos/7/commit/{SHA}">{SHA[:10]}</a>' in html
    assert html.count("/commit/") == 1


def test_no_local_links_without_a_context() -> None:
    html = render(f"#1 and {SHA}")
    assert "<a" not in html


def test_tables_strikethrough_and_task_lists() -> None:
    html = render("| a | b |\n|---|---|\n| 1 | ~~2~~ |\n\n- [ ] todo\n- [x] done\n- plain\n")
    assert "<table>" in html and "<s>2</s>" in html
    assert '<li class="task"><input type="checkbox" disabled> todo</li>' in html
    assert '<input type="checkbox" disabled checked> done' in html
    assert html.count("<input") == 2


def test_code_blocks_are_escaped() -> None:
    html = render("```html\n<script>x</script>\n```\n")
    assert "&lt;script&gt;x&lt;/script&gt;" in html and "<script" not in html


def test_attribute_values_are_escaped() -> None:
    html = render('[x](https://e.com/"onmouseover="alert(1)) ![a"b](https://e.com/i.png)')
    assert 'onmouseover="' not in html
    assert not re.search(r'<a [^>]*\son\w+=', html)


def test_empty_and_very_large_text() -> None:
    assert render(None) == "" and render("") == ""
    big = render("<b>x</b>" * 200_000)
    assert big.startswith("<pre>") and "<b>" not in big


def test_highlight_lines() -> None:
    source = 'def f():\n    """doc\n    more"""\n    return "<b>"\n'
    lines = highlight.highlight_lines("a.py", source)
    assert len(lines) == 4
    assert '<span class="k">def</span>' in lines[0]
    assert "&lt;b&gt;" in lines[3] and "<b>" not in lines[3]
    # Each line is balanced, so one line can sit in one table cell.
    assert all(ln.count("<span") == ln.count("</span>") for ln in lines)
    assert highlight.highlight_lines("file.unknown-type", "x") is None
    assert highlight.highlight_lines("a.py", "x = 1\n" * 100_000) is None
    assert highlight.split_lines("a\r\nb\n") == ["a", "b"]
