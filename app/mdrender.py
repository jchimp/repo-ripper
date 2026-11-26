"""Render Markdown from a repository or from GitHub metadata to safe HTML.

The text is hostile: any person who can open an issue writes it. The output is
safe by construction, not by a clean-up step after the fact:

- Raw HTML is off. Tags in the text come out as visible, escaped text.
- A link is kept only when it is http, https, mailto, or has no scheme.
- The renderer escapes every attribute value and every text node.
- A remote image is not loaded. It becomes a link with its alt text, because
  the app is used offline and a load would tell another server that the page
  was opened.

Links without a scheme point into the repository. With a `LinkContext` they go
to the local file pages at the same revision; without one they lose their
target. `#12` and full commit shas become links to the local pages.
"""
import posixpath
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from urllib.parse import quote, unquote, urlsplit

from markdown_it import MarkdownIt
from markdown_it.rules_core import StateCore
from markdown_it.token import Token
from markupsafe import Markup, escape

MAX_MARKDOWN_CHARS = 1_000_000
EXTERNAL_REL = "noopener noreferrer nofollow"
RASTER_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".webp")

_SCHEME_RE = re.compile(r"^([A-Za-z][A-Za-z0-9+.\-]*):")
_SAFE_SCHEMES = {"http", "https", "mailto"}
_AUTO_RE = re.compile(
    r"(?P<url>https?://[^\s<>\"'`]+)"
    r"|(?<![\w/&#])#(?P<num>\d{1,9})(?!\w)"
    r"|(?<![0-9A-Za-z])(?P<sha>[0-9a-f]{40})(?![0-9A-Za-z])"
)
_TASK_RE = re.compile(r"^\[([ xX])\]\s+")


@dataclass(frozen=True)
class LinkContext:
    """Where the links of one text go.

    Attributes:
        repo_base: "/repos/<id>". Target of `#12` and commit sha links. Empty: off.
        code_base: "/repos/<id>" or "/repos/<id>/wiki". Target of links with
            no scheme. Empty: such links lose their target.
        rev: The revision for the `rev` query parameter of file links.
        directory: The folder of the file that holds the text ("" for the root).
        numbers: Issue and pull request numbers that exist: number -> "issue" or "pull".
        link_commits: Make a full sha a link to the local commit page.
    """
    repo_base: str = ""
    code_base: str = ""
    rev: str = ""
    directory: str = ""
    numbers: Mapping[int, str] = field(default_factory=dict)
    link_commits: bool = False


def _validate_link(url: str) -> bool:
    # Browsers ignore control characters and spaces in a scheme, so drop them first.
    compact = "".join(ch for ch in url if ch > " ")
    match = _SCHEME_RE.match(compact)
    return match.group(1).lower() in _SAFE_SCHEMES if match else True


def _is_external(url: str) -> bool:
    return bool(_SCHEME_RE.match(url)) or url.startswith("//")


def _local_file_url(url: str, ctx: LinkContext, raw: bool) -> str | None:
    """Turn a link with no scheme into the local file route, or None."""
    if not ctx.code_base:
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    path = unquote(parts.path)
    target = path.lstrip("/") if path.startswith("/") else posixpath.join(ctx.directory, path)
    norm = posixpath.normpath(target) if target else ""
    if norm in (".", ""):
        norm = ""
    if norm == ".." or norm.startswith("../") or "\x00" in norm:
        return None
    kind = "raw" if raw else ("blob" if norm else "tree")
    out = f"{ctx.code_base}/{kind}/{quote(norm, safe='/')}"
    if ctx.rev:
        out += f"?rev={quote(ctx.rev, safe='')}"
    if parts.fragment and not raw:
        out += f"#{quote(unquote(parts.fragment), safe='')}"
    return out


def _link_tokens(href: str, text: str, level: int, external: bool) -> list[Token]:
    opener = Token("link_open", "a", 1, level=level)
    opener.attrs = {"href": href}
    if external:
        opener.attrs["rel"] = EXTERNAL_REL
    opener.meta = {"done": True}
    body = Token("text", "", 0, level=level + 1)
    body.content = text
    return [opener, body, Token("link_close", "a", -1, level=level)]


def _auto_link(token: Token, ctx: LinkContext) -> list[Token]:
    """Split a text token at URLs, `#number` and full shas."""
    text = token.content
    out: list[Token] = []
    pos = 0
    for match in _AUTO_RE.finditer(text):
        start, end = match.span()
        href, external = None, False
        if match.group("url"):
            url = match.group("url").rstrip(".,;:!?)")
            end = start + len(url)
            if _validate_link(url):
                href, external = url, True
        elif match.group("num"):
            kind = ctx.numbers.get(int(match.group("num"))) if ctx.repo_base else None
            if kind:
                area = "pulls" if kind == "pull" else "issues"
                href = f"{ctx.repo_base}/{area}/{int(match.group('num'))}"
        elif ctx.link_commits and ctx.repo_base:
            href = f"{ctx.repo_base}/commit/{match.group('sha')}"
        if href is None:
            continue
        if start > pos:
            plain = Token("text", "", 0, level=token.level)
            plain.content = text[pos:start]
            out.append(plain)
        label = text[start:end]
        if match.group("sha"):
            label = label[:10]
        out.extend(_link_tokens(href, label, token.level, external))
        pos = end
    if not out:
        return [token]
    if pos < len(text):
        rest = Token("text", "", 0, level=token.level)
        rest.content = text[pos:]
        out.append(rest)
    return out


def _rewrite_link(token: Token, ctx: LinkContext) -> None:
    href = str(token.attrGet("href") or "")
    if _is_external(href):
        token.attrSet("rel", EXTERNAL_REL)
    elif href.startswith("#"):
        pass  # a link inside the same page
    else:
        local = _local_file_url(href, ctx, raw=False)
        if local:
            token.attrSet("href", local)
        else:
            # No target in this copy. Keep the words, drop the link.
            token.attrs.pop("href", None)
            token.attrSet("class", "md-nolink")
            token.attrSet("title", "This link has no target in the local copy.")


def _local_rule(state: StateCore) -> None:
    """Core rule: rewrite links, add automatic links, mark images in links."""
    ctx: LinkContext = state.env.get("ctx") or LinkContext()
    for block in state.tokens:
        if block.type != "inline" or not block.children:
            continue
        out: list[Token] = []
        depth = 0
        for child in block.children:
            if child.type == "link_open":
                depth += 1
                _rewrite_link(child, ctx)
            elif child.type == "link_close":
                depth -= 1
            elif child.type == "image":
                child.meta = {**(child.meta or {}), "in_link": depth > 0}
            elif child.type == "text" and depth == 0:
                out.extend(_auto_link(child, ctx))
                continue
            out.append(child)
        block.children = out


def _task_rule(state: StateCore) -> None:
    """Core rule: `- [ ] text` becomes a disabled checkbox."""
    tokens = state.tokens
    for i in range(2, len(tokens)):
        if (tokens[i].type != "inline" or tokens[i - 1].type != "paragraph_open"
                or tokens[i - 2].type != "list_item_open" or not tokens[i].children):
            continue
        first = tokens[i].children[0]
        match = _TASK_RE.match(first.content) if first.type == "text" else None
        if not match:
            continue
        first.content = first.content[match.end():]
        box = Token("html_inline", "", 0)
        # A constant string: no part of it comes from the text.
        checked = " checked" if match.group(1) in "xX" else ""
        box.content = f'<input type="checkbox" disabled{checked}> '
        tokens[i].children.insert(0, box)
        tokens[i - 2].attrSet("class", "task")


def _render_image(self, tokens, idx, options, env) -> str:  # noqa: ANN001
    """Renderer rule: never load a remote image; show a local raster image."""
    token = tokens[idx]
    ctx: LinkContext = env.get("ctx") or LinkContext()
    src = str(token.attrGet("src") or "")
    alt = self.renderInlineAsText(token.children or [], options, env) or "image"
    in_link = bool((token.meta or {}).get("in_link"))
    if _is_external(src):
        if in_link:
            return f'<span class="md-image">{escape(alt)}</span>'
        return (f'<a class="md-image" href="{escape(src)}" rel="{EXTERNAL_REL}" '
                f'title="Remote image. It is not loaded.">{escape(alt)}</a>')
    if urlsplit(src).path.lower().endswith(RASTER_EXTENSIONS):
        local = _local_file_url(src, ctx, raw=True)
        if local:
            return f'<img src="{escape(local)}" alt="{escape(alt)}" loading="lazy">'
    local = _local_file_url(src, ctx, raw=False)
    if local and not in_link:
        return f'<a class="md-image" href="{escape(local)}">{escape(alt)}</a>'
    return f'<span class="md-image">{escape(alt)}</span>'


def _build() -> MarkdownIt:
    md = MarkdownIt("commonmark", {"html": False, "linkify": False, "typographer": False})
    md.enable(["table", "strikethrough"])
    md.validateLink = _validate_link  # type: ignore[method-assign]
    md.core.ruler.push("repo_ripper_tasks", _task_rule)
    md.core.ruler.push("repo_ripper_links", _local_rule)
    md.add_render_rule("image", _render_image)
    return md


_MD = _build()


def render(text: str | None, ctx: LinkContext | None = None) -> Markup:
    """Render Markdown to HTML that is safe to put in a page.

    Args:
        text: The Markdown source. None gives an empty result.
        ctx: Where the links go. None: external links only.

    Returns:
        Markup. It is safe because raw HTML is disabled, every link passed
        `_validate_link`, and the renderer escaped all text and attributes.
    """
    if not text:
        return Markup("")
    if len(text) > MAX_MARKDOWN_CHARS:
        return Markup("<pre>") + escape(text[:MAX_MARKDOWN_CHARS]) + Markup("</pre>")
    try:
        html = _MD.render(text, {"ctx": ctx or LinkContext()})
    except (RecursionError, ValueError):
        # A text that the parser cannot process is shown as plain text.
        return Markup("<pre>") + escape(text) + Markup("</pre>")
    return Markup(html)
