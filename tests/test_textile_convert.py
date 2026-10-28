"""Unit tests for Redmine markup conversion.

Table-driven over the constructs a real Redmine instance actually contains.
The cases that matter most are the ones where a converter would quietly change
meaning: an issue reference at the start of a line, a code block's indentation,
a filename with a space in it.
"""

from __future__ import annotations

import pytest

from specivo_redmine_migration.textile_convert import ConversionContext, RedmineTextileConverter

TEXTILE = ConversionContext(source_format="textile")
MARKDOWN = ConversionContext(source_format="common_mark")


@pytest.fixture
def convert():
    converter = RedmineTextileConverter()
    return lambda raw, ctx=TEXTILE: converter.convert(raw, ctx)


class TestBlockConstructs:
    @pytest.mark.parametrize(
        ("source", "expected"),
        [
            ("h1. Title", "# Title"),
            ("h2. Title", "## Title"),
            ("h3. Title", "### Title"),
            ("bq. quoted", "> quoted"),
        ],
    )
    def test_headings_and_quotes(self, convert, source, expected):
        assert convert(source) == expected

    def test_unordered_list_keeps_nesting(self, convert):
        assert convert("* one\n* two\n** nested") == "- one\n- two\n  - nested"

    def test_ordered_list(self, convert):
        assert convert("# first\n# second") == "1. first\n2. second"

    def test_table_gains_a_header_separator(self, convert):
        assert convert("|_. Name |_. Value |\n| a | 1 |") == "| Name | Value |\n| --- | --- |\n| a | 1 |"

    def test_paragraphs_are_separated(self, convert):
        assert convert("one\n\ntwo") == "one\n\ntwo"

    def test_single_newline_becomes_a_hard_break(self, convert):
        """Redmine renders a lone newline as a line break, so Markdown must too."""
        assert convert("line one\nline two") == "line one  \nline two"


class TestInlineConstructs:
    @pytest.mark.parametrize(
        ("source", "expected"),
        [
            ("*bold*", "**bold**"),
            ("_italic_", "*italic*"),
            ("@code@", "`code`"),
            ('"Redmine":https://www.redmine.org', "[Redmine](https://www.redmine.org)"),
        ],
    )
    def test_inline_markup(self, convert, source, expected):
        assert convert(source) == expected

    def test_non_ascii_survives(self, convert):
        assert convert("h2. รายละเอียด\n\nข้อความ") == "## รายละเอียด\n\nข้อความ"


class TestCodeBlocks:
    def test_language_is_kept_on_the_fence(self, convert):
        """Textile escapes the inner code tag, which would lose the language."""
        source = '<pre><code class="ruby">\ndef hi\n  puts 1\nend\n</code></pre>'
        assert convert(source) == "```ruby\ndef hi\n  puts 1\nend\n```"

    def test_plain_pre_becomes_a_fence(self, convert):
        assert convert("<pre>\nplain\n</pre>") == "```\nplain\n```"

    def test_textile_code_block(self, convert):
        assert convert("bc. one\ntwo") == "```\none\ntwo\n```"

    def test_indentation_inside_a_block_is_preserved(self, convert):
        source = "<pre><code>\nif x:\n    y = 1\n</code></pre>"
        assert convert(source) == "```\nif x:\n    y = 1\n```"

    def test_entities_inside_a_block_are_unescaped(self, convert):
        """Redmine stores the block escaped; the code itself must come back."""
        source = "<pre><code>a &lt; b &amp;&amp; c &gt; d</code></pre>"
        assert convert(source) == "```\na < b && c > d\n```"

    def test_markup_inside_a_block_is_not_converted(self, convert):
        source = "<pre><code>*not bold* and _not italic_</code></pre>"
        assert convert(source) == "```\n*not bold* and _not italic_\n```"


class TestIssueReferences:
    def test_inline_reference_is_untouched(self, convert):
        """Rewritten to a display key later, once every issue exists."""
        assert convert("Fixed in #123.") == "Fixed in #123."

    def test_reference_at_the_start_of_a_line_survives(self, convert):
        """A line starting with # is an ordered list to Textile, so #123 would
        silently become "123." without protection."""
        assert convert("#123 is the cause") == "#123 is the cause"

    def test_several_references_survive(self, convert):
        assert convert("See #1, #22 and #333.") == "See #1, #22 and #333."

    def test_commit_reference_is_left_alone(self, convert):
        """Specivo understands commit:hash, so it needs no translation."""
        assert convert("Fixed in commit:abc123") == "Fixed in commit:abc123"


class TestWikiLinks:
    def test_plain_wiki_link_is_unchanged(self, convert):
        """Both systems use the same syntax, so it is protected, not rewritten."""
        assert convert("See [[Architecture]].") == "See [[Architecture]]."

    def test_wiki_link_with_display_text_is_unchanged(self, convert):
        assert convert("See [[Architecture|the design]].") == "See [[Architecture|the design]]."

    def test_wiki_link_at_the_start_of_a_line(self, convert):
        assert convert("[[Home]] first") == "[[Home]] first"


class TestAttachments:
    def test_image_macro_becomes_a_bare_filename_image(self, convert):
        """The renderer resolves a bare filename against the issue's or page's attachments."""
        assert convert("See attachment:diagram.png here.") == "See ![diagram.png](diagram.png) here."

    def test_quoted_filename_with_spaces(self, convert):
        """A target with a space uses CommonMark's angle-bracket form."""
        assert convert('See attachment:"my file.png" here.') == "See ![my file.png](<my file.png>) here."

    def test_non_image_becomes_a_bare_filename_link(self, convert):
        assert convert("See attachment:notes.pdf here.") == "See [notes.pdf](notes.pdf) here."

    def test_non_image_with_spaces(self, convert):
        assert convert('See attachment:"release notes.pdf" here.') == (
            "See [release notes.pdf](<release notes.pdf>) here."
        )

    def test_non_image_in_a_markdown_source(self, convert):
        assert convert("See attachment:notes.pdf", MARKDOWN) == "See [notes.pdf](notes.pdf)"

    def test_textile_inline_image(self, convert):
        assert convert("!logo.png!") == "![](logo.png)"

    def test_extension_matching_ignores_case(self, convert):
        assert convert("attachment:Diagram.PNG") == "![Diagram.PNG](Diagram.PNG)"


class TestOtherMacros:
    def test_user_macro_becomes_a_mention(self, convert):
        """Specivo has mentions, which is the same idea."""
        assert convert("Assigned to user:jsmith") == "Assigned to @jsmith"

    def test_version_macro_is_reduced_to_its_name(self, convert):
        assert convert('Targeting version:"1.0"') == "Targeting 1.0"

    def test_unquoted_version_macro(self, convert):
        assert convert("Targeting version:2.0") == "Targeting 2.0"


class TestSanitising:
    def test_script_tags_do_not_survive(self, convert):
        """A tag the renderer would drop should never reach stored content."""
        assert "alert" not in convert("<script>alert(1)</script>Safe text")

    def test_safe_text_around_a_stripped_tag_is_kept(self, convert):
        assert "Safe text" in convert("<script>alert(1)</script>Safe text")

    @pytest.mark.parametrize(
        "source",
        [
            '"click":javascript:alert(1)',
            '<a href="javascript:alert(1)">click</a>',
            "!javascript:alert(1)!",
            '<a href="data:text/html;base64,PHNjcmlwdD4=">click</a>',
            '"files":ftp://files.example.com/a.zip',
            '<a href="tel:+6620000000">call</a>',
        ],
    )
    def test_links_with_unsafe_schemes_lose_their_target(self, convert, source):
        converted = convert(source)
        for target in ("](javascript:", "](<javascript:", "](data:", "](ftp:", "](tel:", "href="):
            assert target not in converted

    @pytest.mark.parametrize("ctx", [TEXTILE, MARKDOWN])
    def test_escaped_markup_in_text_does_not_come_back_as_a_tag(self, convert, ctx):
        """``&lt;img&gt;`` is text in Redmine; it must not turn into a live tag."""
        converted = convert("a &lt;img src=x onerror=alert(1)&gt; b", ctx)
        assert "<img" not in converted
        assert "img src=x" in converted

    def test_escaped_markup_inside_code_is_kept_as_code(self, convert):
        converted = convert("<pre><code>if a &lt;b&gt; c</code></pre>")
        assert "if a <b> c" in converted

    def test_inline_code_keeps_its_angle_brackets(self, convert):
        assert "`<b>`" in convert("Use @<b>@ for bold.")

    @pytest.mark.parametrize(
        "source",
        ['attachment:"javascript:alert(1)"', "attachment:javascript:alert(1)", 'attachment:"https://x.test/a.png"'],
    )
    def test_attachment_macro_refuses_names_with_a_scheme_or_colon(self, convert, source):
        converted = convert(source)
        assert "](javascript:" not in converted
        assert "](https:" not in converted
        assert "](<javascript:" not in converted

    @pytest.mark.parametrize(
        ("source", "expected"),
        [
            ('"Specivo":https://specivo.example/docs', "[Specivo](https://specivo.example/docs)"),
            ('"mail":mailto:team@example.com', "[mail](mailto:team%40example.com)"),
            ("!https://example.com/a.png!", "![](https://example.com/a.png)"),
            ("attachment:diagram.png", "![diagram.png](diagram.png)"),
            ('<pre><code class="python">x = 1 < 2</code></pre>', "```python\nx = 1 < 2\n```"),
            ("Compare a < b and c > d.", "Compare a < b and c > d."),
        ],
    )
    def test_ordinary_links_images_and_code_survive(self, convert, source, expected):
        assert expected in convert(source)


class TestMarkdownSources:
    def test_markdown_body_is_not_run_through_textile(self, convert):
        """Redmine 7 defaults to CommonMark; converting it again would mangle it."""
        assert convert("# Already Markdown\n\n**bold**", MARKDOWN) == "# Already Markdown\n\n**bold**"

    def test_macros_are_still_rewritten_in_markdown_sources(self, convert):
        assert convert("See attachment:chart.png", MARKDOWN) == "See ![chart.png](chart.png)"

    def test_issue_references_survive_in_markdown_sources(self, convert):
        assert convert("Fixed in #42", MARKDOWN) == "Fixed in #42"


class TestEmptyAndFailure:
    def test_empty_string(self, convert):
        assert convert("") == ""

    def test_whitespace_only(self, convert):
        assert convert("   \n  ") == ""

    def test_failure_keeps_the_original_text(self, monkeypatch):
        """Losing a description is worse than an ugly one."""
        converter = RedmineTextileConverter()
        monkeypatch.setattr(
            converter,
            "_textile_to_markdown",
            lambda text: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        result = converter.convert("h1. Important notes", TEXTILE)
        assert "Important notes" in result
        assert result.startswith("```")

    def test_failure_is_counted(self, monkeypatch):
        converter = RedmineTextileConverter()
        monkeypatch.setattr(
            converter,
            "_textile_to_markdown",
            lambda text: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        converter.convert("h1. One", TEXTILE)
        converter.convert("h1. Two", TEXTILE)
        assert converter.failure_count == 2
        assert len(converter.failed_samples) == 2

    def test_fallback_fence_does_not_collide_with_content(self, monkeypatch):
        converter = RedmineTextileConverter()
        monkeypatch.setattr(
            converter,
            "_textile_to_markdown",
            lambda text: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        result = converter.convert("text with ``` inside", TEXTILE)
        assert result.startswith("````")


class TestCombined:
    def test_a_realistic_description(self, convert):
        source = (
            "h3. Notes\n\n"
            "See #42 and [[Home]] with attachment:chart.png\n\n"
            '<pre><code class="python">x = 1</code></pre>'
        )
        assert convert(source) == (
            "### Notes\n\nSee #42 and [[Home]] with ![chart.png](chart.png)\n\n```python\nx = 1\n```"
        )

    def test_no_excessive_blank_lines(self, convert):
        assert "\n\n\n" not in convert("h1. A\n\n\n\n\nh2. B")
