# specivo-redmine-migration

Exports a Redmine installation to a [Specivo](https://github.com/specivo/specivo) import bundle.
It reads Redmine's database (PostgreSQL or MySQL) and its files directory, converts Textile to
Markdown, and writes the bundle with [`specivo-migration-sdk`](https://github.com/specivo/migration-sdk).
Specivo then validates the bundle and loads it.

This tool is not affiliated with or endorsed by the Redmine project. "Redmine" is a trademark of its
respective owner and is used here only to say which system the tool reads (nominative use).

**Compatibility:** bundle format v1, Specivo 0.11.0 or newer.

- SDK: `specivo-migration-sdk` v0.1.0, pinned to its git tag.
- Tested against Redmine 7.0.1 on PostgreSQL 16 and MySQL 8.4. Python 3.12 or later.

[Specivo](https://specivo.io) is a self-hosted platform for project tracking, a knowledge base and
AI-safe automation: source at [github.com/specivo/specivo](https://github.com/specivo/specivo),
documentation at [specivo.io/docs/specivo](https://specivo.io/docs/specivo/).

## Documentation

- [Migrating from Redmine to Specivo](docs/migrating-from-redmine.md): the step-by-step guide, from
  preparing the export to loading it into Specivo and cleaning up after cut-over.
- [Bundle format specification](https://github.com/specivo/specivo/blob/v0.11.0/docs/import-bundle/v1.md).

## What it carries

Everything the Redmine import built into earlier Specivo releases carried, under the same
identifiers, so a bundle can continue an installation that was first imported directly:

- trackers (with disabled core fields), statuses, priorities, time tracking activities, roles (with
  their user and time entry visibility);
- users with their default email address, status, language and, with `--import-passwords`, their
  Redmine password hash; groups and their members;
- projects with their parents and enabled modules, memberships (direct grants only), versions,
  issue categories, issue custom fields (with their trackers, projects and choices);
- issues with custom values, their history (comments, private notes and field changes), relations,
  watchers, attachments and logged time;
- wiki pages with every revision and its author, page watchers, redirects left by renames, page
  attachments;
- what people without a membership could read (`public_access.json`).

Text (issue descriptions, comments, wiki revisions) is converted from Textile to Markdown, unless
the instance already stores Markdown. A reference to another issue, `#123`, becomes a placeholder
(`{{issue:123}}`) in issue descriptions and comments, which Specivo turns into the issue's new key.
A reference to an issue Redmine has but the bundle does not becomes `{{issue:123|#123}}`, so an
installation can be exported one project at a time (`--project`): load the bundles in order, the
later ones with `--continue-import`, and the reference links to the issue a bundle loaded earlier
from the same instance; when there is none, it reads `#123` as before. `--validate` reports such
references as an `external_ref` warning. A number that names no issue in Redmine stays `#123`.

What has no place in Specivo is left out and listed in the manifest's `not_carried_over`: modules
such as repositories and forums, per-project priorities and activities, custom fields on anything
but issues, attachments on versions, documents or forum messages, relation types Specivo does not
have, and any attachment whose file is missing.

### Known limitations

These are shared with the Redmine import built into earlier Specivo releases:

- **`#N` to an issue loaded later.** A reference to an issue of a project that is loaded after
  the one referring to it stays the text `#N`; loading the other project later does not turn it
  into a link. Load the projects so that referenced issues come first.
- **`#N` that is not a reference.** The match is a `#` followed by digits at a word boundary,
  outside code blocks; a number written that way for another reason ("step #2", a colour such as
  `#123`) becomes a link if an issue has that id.
- **Description history stays Textile.** The old and new descriptions Redmine records when a
  description is edited are carried as Redmine stored them, not converted to Markdown.
- **Projects scheduled for deletion** (Redmine status 10) are exported as active projects.
- **Wiki redirects across projects.** A redirect to a page in another project's wiki is carried as
  if it pointed into its own wiki.

One deliberate difference from that built-in import: Textile text that Redmine shows as escaped
markup (`&lt;img ...&gt;`) stays text in the bundle, where the built-in import let it become a tag.

## Install

There is no PyPI package. Releases are tags of this repository, with the wheel and sdist attached
to each [GitHub Release](https://github.com/specivo/redmine-migration-tool/releases). Install a tag:

```bash
uv tool install git+https://github.com/specivo/redmine-migration-tool@v0.1.0
# or: pipx install git+https://github.com/specivo/redmine-migration-tool@v0.1.0
specivo-redmine-migration --help
```

The tool pulls in the SDK from its own git tag (`specivo-migration-sdk @
git+https://github.com/specivo/migration-sdk@v0.1.0`), so the machine needs `git` and access to
GitHub.

## Use

Run it where it can reach Redmine's database and read its files directory. The database password
is best passed in the environment, so it does not show in the process list:

```bash
export REDMINE_DB_URL='postgresql://redmine:PASSWORD@db.example.com/redmine'   # or mysql://...
specivo-redmine-migration --files-dir /var/lib/redmine/files --out redmine-export --validate
```

#### Which database account, and when

Give the tool a database account that can only `SELECT` (on PostgreSQL, a role with `SELECT` on
the Redmine schema's tables; on MySQL, `GRANT SELECT ON redmine.* TO ...`). The tool never writes,
and does not need to.

Everything is read through one connection, in one read-only, repeatable-read transaction
(PostgreSQL: `SET TRANSACTION READ ONLY, ISOLATION LEVEL REPEATABLE READ`; MySQL:
`START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY`). So the bundle shows Redmine as it was at
one moment, even when people keep working in it, and the session could not write even with an
account that can. That snapshot is held for the whole export, which on a large instance can be a
long time, and a long-open transaction keeps the database from cleaning up old row versions
(PostgreSQL's vacuum, MySQL's undo log). Export from a replica, or at a quiet hour; on a
PostgreSQL hot standby, a long export may also need `max_standby_streaming_delay` raised, or
`hot_standby_feedback` on.

This writes a **directory**: `manifest.json`, one `.jsonl` file per kind of record, and the
attachments under `files/<sha256>`. A directory is the right container for a real migration: the
attachments are copied once, not packed and unpacked, and `rsync` can copy the folder to the
Specivo server, resume an interrupted copy, and on a later export send only the new files. For a
small export or a trial run, `--zip` (or an `--out` ending in `.zip`) writes a single archive
instead.

Then, on the Specivo server:

```bash
python -m specivo.cli.import_data --source bundle --path redmine-export --validate
python -m specivo.cli.import_data --source bundle --path redmine-export --dry-run
python -m specivo.cli.import_data --source bundle --path redmine-export
```

### Options

| Option | Meaning |
|---|---|
| `--db-url URL` | Redmine's database (default: `$REDMINE_DB_URL`). `postgresql://`, `mysql://` or `mysql2://`; the async driver is chosen for you. |
| `--files-dir DIR` | Redmine's files directory. Without it, attachments are left out (and listed as such). |
| `--out PATH` | A new or empty directory, or a `.zip` path. |
| `--zip` | Write a `.zip` instead of a directory. |
| `--instance NAME` | The installation's name in the bundle. Defaults to Redmine's *Host name and path* setting; required when that is empty or a default or local value (`localhost…`, `127.…`, `[::1]…`, `0.0.0.0…`, `example.…`). |
| `--project P` | Only this project (identifier or id) and its subprojects. Repeatable. |
| `--import-passwords` | Carry local accounts' password hashes (`redmine_sha1`). Specivo uses them only if its import also runs with `--import-passwords`; administrators always set a new password. |
| `--status-category-map NAME=CATEGORY,...` | Place statuses in Specivo's backlog/active/done/closed grouping (Redmine only records "closed"). |
| `--cf-key-map NAME=KEY,...` | Override the metadata key derived from a custom field's name. |
| `--limit N` | At most N issues per project (parents first), for a trial run. |
| `--validate` | Check the bundle once written, as Specivo will. |
| `--quiet` | Print only errors. Progress goes to stderr, the bundle's path to stdout. |

Exit status: 0 on success, 1 when the export failed or `--validate` found problems, 2 for a usage
error.

### Passwords: the bundle is then a secret

With `--import-passwords` the bundle holds every local account's Redmine password hash and salt,
next to its login and email address. Handle it like a copy of Redmine's database: the tool creates
it readable by you only (a directory with mode 0700 and its files 0600, or a `.zip` with mode
0600), so keep it that way, copy it only over an encrypted channel, and delete it once the import is
done. Administrators' hashes are never exported, since Specivo makes every imported administrator
set a new password anyway. Without `--import-passwords` the bundle still holds logins and email
addresses.

### The instance name, and exporting again

Specivo remembers which record of the source became which record in Specivo, per source system and
instance. Export the same installation again later (just before switching over, say) with the
**same instance name**, and loading the new bundle adds only what is new. Two different Redmine
installations imported into one Specivo must have different names, which is why a host name left at
Redmine's default (`localhost:3000`) or at a local or example address is refused. The instance is
never derived from the database URL.

## Development

```bash
uv sync
uv run pytest
uv run ruff check . && uv run ruff format --check .
uv run mypy --strict src
```

The SDK is pinned to a release tag of
[specivo/migration-sdk](https://github.com/specivo/migration-sdk). To work on both at once, install
a local SDK checkout over it: `uv pip install -e ../migration-sdk`.

Most tests run the whole export against a small Redmine database built in SQLite
(`tests/sample_redmine.py`). The end-to-end test runs it against a real Redmine.

### The Redmine fixture

`docker-compose.fixture.yml` runs Redmine 7.0.1 on PostgreSQL (`PROFILE=pg`, the default) or MySQL
(`PROFILE=mysql`), and `fixture/seed.rb` fills it through Redmine's own models.

```bash
make fixture-up PROFILE=pg       # start it
make fixture-seed PROFILE=pg     # fill it
make fixture-files PROFILE=pg    # copy its files directory out to .fixture-files/pg
make fixture-test PROFILE=pg     # the end-to-end test, failing (not skipping) without the fixture
make fixture-down PROFILE=pg     # stop it; data and files are discarded
```

Host ports, all bound to 127.0.0.1 and each overridable with the variable shown:

| Service | Port | Variable |
|---|---|---|
| PostgreSQL | 5452 | `REDMINE_TOOL_FIXTURE_PG_PORT` |
| Redmine on PostgreSQL (web) | 8034 | `REDMINE_TOOL_FIXTURE_PG_HTTP_PORT` |
| MySQL | 3309 | `REDMINE_TOOL_FIXTURE_MYSQL_PORT` |
| Redmine on MySQL (web) | 8035 | `REDMINE_TOOL_FIXTURE_MYSQL_HTTP_PORT` |

Each checkout has a fixture of its own: the Makefile names the compose project after the
checkout's path, the databases are in tmpfs, and Redmine's files directory is a named volume of
that project, copied out with `docker compose cp` instead of being bind-mounted. Two checkouts can
run their fixtures at the same time when the second one is given other ports.

The end-to-end test reads `REDMINE_FIXTURE_PG_URL` / `REDMINE_FIXTURE_MYSQL_URL` and
`REDMINE_FIXTURE_PG_FILES` / `REDMINE_FIXTURE_MYSQL_FILES` when the defaults above do not fit.

### A bundle from the fixture

```bash
make fixture-export PROFILE=pg    # writes and validates out/fixture-pg
```

It runs the command with `--instance pytest-fixture`; `FIXTURE_OUT`, `FIXTURE_INSTANCE` and
`ARGS='--import-passwords --project acme-app'` change the target, the name and the options.

## Releasing

Set the version in `pyproject.toml`, commit, then tag and push the tag (`git tag v0.1.0 && git push
origin v0.1.0`). The release workflow builds the wheel and sdist with `uv build` and attaches them
to a GitHub Release for the tag.

## Licence

Apache-2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
