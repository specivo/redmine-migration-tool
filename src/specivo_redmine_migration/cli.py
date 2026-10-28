"""The ``specivo-redmine-migration`` command.

Reads a Redmine database (and its files directory) and writes a Specivo import
bundle: a directory by default, so attachments stay plain files that ``rsync``
can copy and resume, or a ``.zip`` with ``--zip``.

Exit status: 0 when the bundle was written (and, with ``--validate``, is
valid); 1 when the export failed or the bundle is not valid; 2 for a usage
error, such as a missing ``--instance``.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from specivo_migration_sdk import validate_bundle

from specivo_redmine_migration import db
from specivo_redmine_migration.export import ExportError, ExportOptions, export_bundle, generator_version

DB_URL_ENV = "REDMINE_DB_URL"
_STATUS_CATEGORIES = ("backlog", "active", "done", "closed")


def _pairs(value: str) -> dict[str, str]:
    """Parse ``name=value,name=value``."""
    result: dict[str, str] = {}
    for item in value.split(","):
        if not item.strip():
            continue
        name, sep, mapped = item.partition("=")
        if not sep or not name.strip() or not mapped.strip():
            raise argparse.ArgumentTypeError(f"expected name=value pairs, got {item!r}")
        result[name.strip()] = mapped.strip()
    return result


def _status_map(value: str) -> dict[str, str]:
    pairs = _pairs(value)
    for name, category in pairs.items():
        if category not in _STATUS_CATEGORIES:
            raise argparse.ArgumentTypeError(f"{name}: {category!r} is not one of {', '.join(_STATUS_CATEGORIES)}")
    return pairs


def _positive(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be 1 or more")
    return number


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="specivo-redmine-migration",
        description=(
            "Export a Redmine installation (its database and files directory) to a Specivo import bundle. "
            "Load the bundle with: python -m specivo.cli.import_data --source bundle --path OUT"
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {generator_version()}")
    source = parser.add_argument_group("source")
    source.add_argument(
        "--db-url",
        help=(
            "Redmine's database, as a URL: postgresql://user:password@host/db or mysql://user:password@host/db. "
            f"Defaults to ${DB_URL_ENV}, which keeps the password out of the process list."
        ),
    )
    source.add_argument(
        "--files-dir",
        type=Path,
        help="Redmine's files directory (where attachments are stored). Without it, attachments are left out.",
    )
    source.add_argument(
        "--instance",
        help=(
            "Name of this Redmine installation in the bundle; defaults to Redmine's host name setting, and is "
            "required when that is empty. Use the same name on every export of one installation."
        ),
    )
    source.add_argument(
        "--project",
        action="append",
        default=[],
        metavar="PROJECT",
        help="Export only this project (identifier or id) and its subprojects. Repeatable. Default: all.",
    )

    output = parser.add_argument_group("output")
    output.add_argument("--out", type=Path, required=True, help="Where to write the bundle: a new or empty directory.")
    output.add_argument(
        "--zip", action="store_true", help="Write a .zip instead of a directory (handy for small exports)."
    )
    output.add_argument(
        "--validate", action="store_true", help="Check the bundle once written, as Specivo will before loading it."
    )
    output.add_argument("--quiet", action="store_true", help="Print nothing but errors.")

    mapping = parser.add_argument_group("mapping")
    mapping.add_argument(
        "--import-passwords",
        action="store_true",
        help=(
            "Carry each local account's Redmine password hash (redmine_sha1), so people can keep their password. "
            "Specivo uses it only when its import runs with --import-passwords too."
        ),
    )
    mapping.add_argument(
        "--status-category-map",
        type=_status_map,
        default={},
        metavar="NAME=CATEGORY,...",
        help="Place statuses in Specivo's backlog/active/done/closed grouping (Redmine records only 'closed').",
    )
    mapping.add_argument(
        "--cf-key-map",
        type=_pairs,
        default={},
        metavar="NAME=KEY,...",
        help="Override the metadata key derived from a custom field's name.",
    )

    run = parser.add_argument_group("run")
    run.add_argument("--limit", type=_positive, help="Export at most this many issues per project, for a trial run.")
    run.add_argument("--batch-size", type=_positive, default=500, help="Rows read per query (default: 500).")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    db_url = args.db_url or os.environ.get(DB_URL_ENV)
    if not db_url:
        parser.print_usage(sys.stderr)
        print(f"{parser.prog}: error: give --db-url, or set {DB_URL_ENV}", file=sys.stderr)
        return 2

    try:
        db.check_source_url(db_url)
    except db.SourceURLError as exc:
        print(f"{parser.prog}: error: --db-url {exc}", file=sys.stderr)
        return 2

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

    def progress(message: str) -> None:
        if not args.quiet:
            print(message, file=sys.stderr, flush=True)

    options = ExportOptions(
        db_url=db_url,
        out=args.out,
        files_dir=args.files_dir,
        instance=(args.instance or "").strip() or None,
        projects=args.project,
        import_passwords=args.import_passwords,
        limit=args.limit,
        zip=True if args.zip else None,
        status_category_map=args.status_category_map,
        cf_key_map=args.cf_key_map,
        batch_size=args.batch_size,
    )
    try:
        summary = asyncio.run(export_bundle(options, progress))
    except ExportError as exc:
        print(f"{parser.prog}: error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - report, never a traceback holding the URL
        print(f"{parser.prog}: export failed: {_redacted(exc, db_url)}", file=sys.stderr)
        return 1

    if not args.quiet:
        print(summary.path)
        counts = ", ".join(f"{key} {value}" for key, value in summary.counts.items() if value)
        progress(f"Records: {counts}")
        if summary.not_carried_over:
            progress(f"Not carried over: {len(summary.not_carried_over)} items, listed in the manifest")
        if summary.conversion_failures:
            progress(f"Markup that could not be converted, kept as written: {summary.conversion_failures} fields")

    if args.validate:
        report = validate_bundle(summary.path)
        print(report.format_text())
        if not report.ok:
            return 1
        print("The bundle is valid.")
    return 0


def _redacted(exc: BaseException, db_url: str) -> str:
    """Return *exc* as one line of text with the database password masked. Never raises."""
    try:
        text = f"{type(exc).__name__}: {exc}"
    except Exception:  # noqa: BLE001 - an exception whose str() fails still gets reported
        text = type(exc).__name__
    return db.mask_password(" ".join(text.split()), db_url)
