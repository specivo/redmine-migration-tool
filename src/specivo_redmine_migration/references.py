"""Turn Redmine's ``#N`` issue references into bundle placeholders.

Specivo's direct Redmine import rewrites ``#N`` to the issue's display key once
every issue exists. A bundle cannot know display keys, so the tool writes a
placeholder instead, wherever the direct import would rewrite (issue
descriptions and comments) and by the same matching rule, never inside a fenced
code block:

- an issue in the bundle becomes ``{{issue:N}}``;
- an issue of Redmine that is not in the bundle becomes ``{{issue:N|#N}}``. An
  installation exported one project at a time refers to issues an earlier
  bundle carried; Specivo looks those up among the earlier imports of the same
  source, and writes the label, ``#N``, for one it cannot find;
- a number naming no issue of Redmine (a deleted issue, "step #2") is left as
  written.
"""

from __future__ import annotations

import re
from collections.abc import Container

from specivo_migration_sdk import placeholders

# The direct import's matching rule. The lookbehind keeps it from matching an
# anchor in a URL, an HTML entity, or a doubled hash.
_REFERENCE_RE = re.compile(r"(?<![\w&#])#(\d+)\b")

# Splits text on fenced code blocks, keeping the fences so they can be rejoined.
_FENCE_RE = re.compile(r"(```.*?```|~~~.*?~~~)", re.DOTALL)


def issue_placeholders(text: str, bundle_issues: Container[str], redmine_issues: Container[str]) -> str:
    """Return *text* with each ``#N`` outside fenced code blocks that names an issue replaced by its placeholder.

    *bundle_issues* holds the refs of the issues in the bundle, *redmine_issues*
    those of every issue in Redmine.
    """
    if "#" not in text:
        return text

    def replace(match: re.Match[str]) -> str:
        ref = match.group(1)
        if ref in bundle_issues:
            return placeholders.issue(ref)
        if ref in redmine_issues:
            return placeholders.issue(ref, match.group(0))
        return match.group(0)

    parts = _FENCE_RE.split(text)
    for index, part in enumerate(parts):
        # Odd positions are the fenced blocks themselves.
        if index % 2 == 0:
            parts[index] = _REFERENCE_RE.sub(replace, part)
    return "".join(parts)
