"""Convert Redmine markup to the Markdown Specivo stores.

Redmine's historical markup is Textile, and modern instances store CommonMark
instead — Redmine 7 defaults to it. Both need the same link rewriting; only
Textile needs converting.

Conversion goes Textile to HTML to Markdown, using two mature libraries rather
than a hand-written Textile parser or an external binary. Redmine's dialect is
RedCloth-derived and full of corners, and the one thing a migration cannot do is
mangle a decade of issue descriptions.

Around that sit three passes that the libraries know nothing about:

**Protect.** Code blocks are lifted out before anything touches them, so
indentation and contents survive verbatim. Wiki links and issue references are
lifted out too: ``#123`` at the start of a line is an ordered list to Textile,
which would silently turn it into "123.".

**Rewrite.** Redmine's own macros — ``attachment:``, ``version:``, ``user:`` —
are translated while the text is still Textile, because the converter would
otherwise curl their quotes and make them unparseable.

**Restore.** The protected fragments come back as Markdown: code blocks as
fenced blocks, keeping the language when Redmine recorded one.

Nothing here raises. Unconvertible input comes back as a fenced block of the
original text, because losing a description is worse than an ugly one, and the
failure is counted for the export summary.
"""

from __future__ import annotations

import html as _html
import logging
import re
from dataclasses import dataclass

import nh3
import textile
from markdownify import MarkdownConverter

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ConversionContext:
    """What a conversion needs beyond the text: the markup the instance stores."""

    source_format: str = "textile"


# A copy of the allowlists Specivo's renderer sanitises with
# (``specivo/services/markdown_service.py``), applied to the HTML between the
# two conversion steps, so a tag, attribute or link scheme Specivo would drop
# never reaches the bundle. Specivo still sanitises again when it renders.
ALLOWED_TAGS: frozenset[str] = frozenset(
    {
        "p", "br", "hr", "a", "strong", "em", "b", "i", "u", "s", "del", "code", "pre", "blockquote",
        "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "li", "table", "thead", "tbody", "tfoot", "tr",
        "th", "td", "img", "div", "span", "mark", "sup", "sub", "dl", "dt", "dd",
    }
)  # fmt: skip
ALLOWED_ATTRIBUTES: dict[str, frozenset[str]] = {
    "a": frozenset({"href", "title", "id"}),
    "img": frozenset({"src", "alt", "title", "width", "height"}),
    "th": frozenset({"align", "colspan", "rowspan"}),
    "td": frozenset({"align", "colspan", "rowspan"}),
    "code": frozenset({"class"}),
    "pre": frozenset({"class"}),
    "div": frozenset({"class"}),
    "span": frozenset({"class"}),
    **{f"h{level}": frozenset({"id"}) for level in range(1, 7)},
}
ALLOWED_URL_SCHEMES: frozenset[str] = frozenset({"http", "https", "mailto"})

# A "<" that would open a tag, a comment or a declaration in Markdown. Textile
# escapes such text as entities, and markdownify decodes them again; this puts
# them back (outside code, where the text is shown literally anyway).
_TAG_OPEN_RE = re.compile(r"<(?=[A-Za-z/!?])")

# A name the attachment macro must not turn into a link target: a URL scheme
# or anything else with a colon, which is never part of an attachment's name
# as Redmine stores it and would make the link point somewhere else.
_UNSAFE_ATTACHMENT_NAME_RE = re.compile(r":")


class _MarkdownConverter(MarkdownConverter):
    """markdownify, with tag-like text kept as text."""

    def escape(self, text: str, parent_tags: set[str]) -> str:
        # markdownify's stub leaves out escape(); it is the hook its docs name for this.
        escaped = str(super().escape(text, parent_tags))  # type: ignore[misc]
        return _TAG_OPEN_RE.sub("&lt;", escaped)


# Formats that are already Markdown and only need link rewriting.
MARKDOWN_FORMATS = frozenset({"markdown", "common_mark", "commonmark"})

# Extensions Specivo's renderer can resolve as an inline image.
_IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".bmp", ".svg", ".webp", ".ico", ".tif", ".tiff"})

# Placeholder tokens. Deliberately bare alphanumerics: anything with
# punctuation risks being escaped or reflowed by one of the two converters.
_TOKEN = "ZZSPVPROTECTED{index}ZZ"
_TOKEN_RE = re.compile(r"ZZSPVPROTECTED(\d+)ZZ")

# <pre><code class="ruby">...</code></pre> and the bare <pre> form. Redmine
# writes both, and Textile escapes the inner tag, which loses the language.
_PRE_CODE_RE = re.compile(
    r"<pre>\s*<code(?:\s+class=[\"']?([\w.+-]+)[\"']?)?\s*>(.*?)</code>\s*</pre>",
    re.DOTALL | re.IGNORECASE,
)
_PRE_RE = re.compile(r"<pre>(.*?)</pre>", re.DOTALL | re.IGNORECASE)
# Textile's own code block: "bc. " for one paragraph, "bc.. " until the next block.
_BC_RE = re.compile(r"^bc\.\.?\s?(.*?)(?=\n\n|\Z)", re.DOTALL | re.MULTILINE)

# [[Page]] and [[Page|Text]] — identical in both systems, so protected rather
# than rewritten.
_WIKI_LINK_RE = re.compile(r"\[\[[^\[\]\n]+\]\]")

# Redmine's issue reference. Protected because a line starting with # is an
# ordered list in Textile. Turned into a placeholder after conversion (see
# references.py), where the text names an exported issue.
_ISSUE_REF_RE = re.compile(r"(?<![\w&])#\d+\b")

# attachment:name.ext, attachment:"name with spaces.ext"
_ATTACHMENT_RE = re.compile(r'attachment:(?:"([^"\n]+)"|([^\s,;)\]]+))')
# version:"1.0" / version:1.0 — no Specivo equivalent, so reduced to its name.
_VERSION_RE = re.compile(r'version:(?:"([^"\n]+)"|([^\s,;)\]]+))')
# user:login becomes a Specivo mention, which is the same idea.
_USER_RE = re.compile(r"user:([A-Za-z0-9_.\-]+)")

# Three or more blank lines add nothing and make diffs noisy.
_EXCESS_BLANK_LINES_RE = re.compile(r"\n{3,}")


class RedmineTextileConverter:
    """Converts one field of Redmine markup to Markdown.

    Instances accumulate a failure count, so a run can report how many fields
    fell back to raw text rather than silently shipping them.
    """

    def __init__(self) -> None:
        self.failure_count = 0
        self.failed_samples: list[str] = []

    def convert(self, raw: str, ctx: ConversionContext | None = None) -> str:
        """Return *raw* as Markdown. Never raises."""
        if not raw or not raw.strip():
            return ""

        ctx = ctx or ConversionContext()
        try:
            protected: dict[str, str] = {}
            text = self._protect_code(raw, protected)
            text = self._rewrite_macros(text, protected)
            text = self._protect_spans(text, protected)

            if ctx.source_format.lower() not in MARKDOWN_FORMATS:
                text = self._textile_to_markdown(text)

            text = self._restore(text, protected)
            return _EXCESS_BLANK_LINES_RE.sub("\n\n", text).strip()
        except Exception as exc:  # noqa: BLE001 - a bad field must not stop an import
            self.failure_count += 1
            if len(self.failed_samples) < 20:
                self.failed_samples.append(raw[:80])
            logger.warning("Markup conversion failed (%s); keeping the original text", exc)
            return self._fallback(raw)

    # ------------------------------------------------------------------
    # Protect
    # ------------------------------------------------------------------

    def _protect_code(self, text: str, protected: dict[str, str]) -> str:
        """Replace code blocks with tokens, remembering them as fenced Markdown."""

        def store(fence: str) -> str:
            token = _TOKEN.format(index=len(protected))
            protected[token] = fence
            return f"\n\n{token}\n\n"

        def on_pre_code(match: re.Match[str]) -> str:
            language = (match.group(1) or "").strip()
            body = _unescape(match.group(2)).strip("\n")
            return store(f"```{language}\n{body}\n```")

        def on_pre(match: re.Match[str]) -> str:
            body = _unescape(match.group(1)).strip("\n")
            return store(f"```\n{body}\n```")

        def on_bc(match: re.Match[str]) -> str:
            return store(f"```\n{match.group(1).strip()}\n```")

        text = _PRE_CODE_RE.sub(on_pre_code, text)
        text = _PRE_RE.sub(on_pre, text)
        return _BC_RE.sub(on_bc, text)

    def _protect_spans(self, text: str, protected: dict[str, str]) -> str:
        """Replace wiki links and issue references with tokens, verbatim."""

        def store(match: re.Match[str]) -> str:
            token = _TOKEN.format(index=len(protected))
            protected[token] = match.group(0)
            return token

        text = _WIKI_LINK_RE.sub(store, text)
        return _ISSUE_REF_RE.sub(store, text)

    def _restore(self, text: str, protected: dict[str, str]) -> str:
        """Put every protected fragment back."""

        def put_back(match: re.Match[str]) -> str:
            return protected.get(match.group(0), match.group(0))

        # Converters may escape or re-wrap around a token, so restore until
        # nothing changes rather than assuming one pass is enough.
        for _ in range(3):
            replaced = _TOKEN_RE.sub(put_back, text)
            if replaced == text:
                break
            text = replaced
        return text

    # ------------------------------------------------------------------
    # Rewrite
    # ------------------------------------------------------------------

    def _rewrite_macros(self, text: str, protected: dict[str, str]) -> str:
        """Translate Redmine's link macros while the text is still Textile.

        Attachments become their final Markdown form immediately and are
        protected, rather than being routed through Textile's image syntax:
        that syntax cannot carry a filename with a space in it, and a
        CommonMark source would keep the Textile markers verbatim.
        """

        def on_attachment(match: re.Match[str]) -> str:
            filename = match.group(1) or match.group(2)
            if _UNSAFE_ATTACHMENT_NAME_RE.search(filename):
                # Not an attachment name: left as the text it is.
                return match.group(0)
            token = _TOKEN.format(index=len(protected))
            # A bare-filename reference, which the renderer resolves against
            # the attachments of the issue (description and comments) or the
            # project (wiki pages) the text is stored on: the image form for
            # images, a plain link for every other file.
            target = _link_target(filename)
            if _is_image(filename):
                protected[token] = f"![{_escape_label(filename)}]({target})"
            else:
                protected[token] = f"[{_escape_label(filename)}]({target})"
            return token

        def on_version(match: re.Match[str]) -> str:
            return match.group(1) or match.group(2)

        text = _ATTACHMENT_RE.sub(on_attachment, text)
        text = _VERSION_RE.sub(on_version, text)
        return _USER_RE.sub(lambda m: f"@{m.group(1)}", text)

    # ------------------------------------------------------------------
    # Convert
    # ------------------------------------------------------------------

    def _textile_to_markdown(self, text: str) -> str:
        """Run the two-hop conversion, sanitising the HTML in between."""
        html = str(textile.textile(text))
        # Sanitised here rather than only when Specivo renders: what Specivo
        # would drop should not reach the stored content at all.
        html = nh3.clean(
            html,
            tags=set(ALLOWED_TAGS),
            attributes={k: set(v) for k, v in ALLOWED_ATTRIBUTES.items()},
            url_schemes=set(ALLOWED_URL_SCHEMES),
        )
        return str(_MarkdownConverter(heading_style="ATX", bullets="-").convert(html))

    def _fallback(self, raw: str) -> str:
        """Return the original text in a fenced block, so nothing is lost."""
        fence = "````" if "```" in raw else "```"
        return f"{fence}\n{raw.strip()}\n{fence}"


def _is_image(filename: str) -> bool:
    """Whether *filename* is one Specivo's renderer can show inline."""
    lowered = filename.lower()
    return any(lowered.endswith(extension) for extension in _IMAGE_EXTENSIONS)


def _link_target(filename: str) -> str:
    """Return *filename* as a Markdown link target.

    A name with a space or bracket in it is wrapped in ``<...>``, CommonMark's
    form for such targets, which the renderer also accepts.
    """
    if any(char in filename for char in " ()<>"):
        return f"<{filename.replace('<', '%3C').replace('>', '%3E')}>"
    return filename


def _escape_label(filename: str) -> str:
    """Escape the brackets that would end a Markdown link label early."""
    return filename.replace("[", "\\[").replace("]", "\\]")


def _unescape(text: str) -> str:
    """Undo the entity escaping Redmine applies inside code blocks."""
    return _html.unescape(text)


def make_converter(source_format: str) -> tuple[RedmineTextileConverter, ConversionContext]:
    """Return a converter and the context describing *source_format*."""
    return RedmineTextileConverter(), ConversionContext(source_format=source_format)
