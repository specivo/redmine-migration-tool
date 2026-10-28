# Migrating from Redmine to Specivo

This guide walks through moving a Redmine installation into [Specivo](https://specivo.io) with
`specivo-redmine-migration`. The tool reads Redmine's database and files folder and writes an
import bundle. Specivo then checks the bundle and loads it.

Contents:

1. [Who this is for, and what moves](#1-who-this-is-for-and-what-moves)
2. [Before you start](#2-before-you-start)
3. [Export from Redmine](#3-export-from-redmine)
4. [Move the export to the Specivo host](#4-move-the-export-to-the-specivo-host)
5. [Load it into Specivo](#5-load-it-into-specivo)
6. [Migrating in several runs](#6-migrating-in-several-runs)
7. [Coming from the import built into Specivo before 0.11.0](#7-coming-from-the-import-built-into-specivo-before-0110)
8. [After cut-over](#8-after-cut-over)
9. [Verifying and troubleshooting](#9-verifying-and-troubleshooting)

Throughout, `redmine.example.com` is your Redmine and `specivo.example.com` is your Specivo.

## 1. Who this is for, and what moves

This guide is for the person who runs both systems: someone who can reach Redmine's database, copy
files to the Specivo server, and run commands in Specivo's containers. You do not need to write
code.

**What moves**, under stable identifiers so you can export again later:

- trackers, statuses, priorities, time tracking activities and roles;
- users (login, email address, status, language), groups and their members;
- projects with their hierarchy and enabled modules, memberships, versions, issue categories and
  issue custom fields;
- issues with their full history (comments, private notes, field changes), relations, watchers,
  attachments and logged time;
- wiki pages with every revision, watchers, redirects left by renames and page attachments;
- what people without a membership could read in public projects.

Textile is converted to Markdown. References such as `#123` become links to the new issue.

**What does not move.** Anything with no place in Specivo is left out and listed in the bundle's
`manifest.json` under `not_carried_over`. The main items are modules such as repositories and
forums, per-project priorities and activities, custom fields on anything but issues, attachments
on versions, documents or forum messages, relation types Specivo does not have, and attachments
whose file is missing from the files folder. Role permissions are not translated either: roles are
matched by name, and new ones arrive without permissions for an administrator to fill in.

The tool has a few known limitations, such as a `#N` reference to an issue loaded in a later run.
Read them before you plan the migration:
[Known limitations in the README](../README.md#known-limitations).

The exact bundle format is specified in
[the bundle format spec](https://github.com/specivo/specivo/blob/v0.11.0/docs/import-bundle/v1.md).
You do not need it to follow this guide.

## 2. Before you start

You need:

- **A Specivo 0.11.0 or newer instance** to load into. It does not have to be empty. Run
  `docker compose exec api python -m specivo.cli.import_data --list-sources` and check that
  `bundle` is listed.
- **Read-only access to Redmine's database**, PostgreSQL or MySQL. Create an account that can only
  `SELECT` (on PostgreSQL a role with `SELECT` on the Redmine tables; on MySQL
  `GRANT SELECT ON redmine.* TO ...`). The tool never writes, and its session is read-only even if
  the account could write.
- **A replica, or a quiet hour.** The export reads everything in one consistent snapshot, which
  stays open for the whole export. On a large instance that can be a long time, and a long-open
  transaction stops the database from cleaning up old row versions. Export from a replica if you
  have one. On a PostgreSQL hot standby, a long export may need `max_standby_streaming_delay`
  raised or `hot_standby_feedback` on.
- **Redmine's files folder**, readable from the machine where you run the export (often
  `/var/lib/redmine/files` or the `files` folder of the Redmine application). Without it, the
  export has no attachments.
- **The tool**, on a machine that can reach the database (it needs `git` and access to GitHub,
  because the migration SDK is pulled from its git tag):

  ```bash
  uv tool install git+https://github.com/specivo/redmine-migration-tool@v0.1.0
  specivo-redmine-migration --version
  ```

  `pipx install` with the same URL works too. The tool needs Python 3.12 or later.

You do not need to stop Redmine. Plan to freeze it only at the final cut-over (see
[section 6](#6-migrating-in-several-runs)).

## 3. Export from Redmine

Pass the database password in the environment so that it does not show in the process list:

```bash
export REDMINE_DB_URL='postgresql://readonly:PASSWORD@db.example.com/redmine'   # or mysql://...
specivo-redmine-migration \
  --files-dir /var/lib/redmine/files \
  --out redmine-export \
  --validate
```

By default the tool writes a **folder**: `manifest.json`, one `.jsonl` file per kind of record,
and the attachments under `files/<sha256>`. A folder is the right choice for a real migration.
Attachments are copied once, `rsync` can resume an interrupted copy, and a later export only adds
the new files. `--zip` (or an `--out` ending in `.zip`) writes one archive instead; use it only for
small exports and trial runs. `--out` must be a new or empty folder.

`--validate` checks the finished bundle the way Specivo will. Exit status 0 means success, 1 means
the export failed or validation found problems.

### The instance name

Specivo remembers which Redmine record became which Specivo record, per source system and
**instance name**. The instance name is how a later export is recognised as the same installation.

- By default the tool uses Redmine's *Host name and path* setting (Administration, Settings,
  General).
- If that setting is empty, or is a default or local value (`localhost...`, `127....`, `[::1]...`,
  `0.0.0.0...`, `example....`), the tool refuses to guess. Pass `--instance redmine.example.com`.
- Use the **same name for every export of one installation**, forever. Changing it makes the next
  load create everything a second time.
- Two different Redmine installations loaded into one Specivo must have different names.

### Exporting only some projects

`--project` exports one project (identifier or numeric id) and its subprojects. It can be repeated:

```bash
specivo-redmine-migration --files-dir /var/lib/redmine/files --out export-acme \
  --project acme-app --project acme-web --validate
```

Do a trial first with `--limit 50`, which exports at most 50 issues per project.

### Passwords

By default accounts arrive without a usable password. With `--import-passwords` the bundle carries
every local account's Redmine password hash, so people can keep signing in with the password they
have. Specivo uses the hashes only if the load also runs with `--import-passwords` (see
[section 5](#administrators-and-passwords)).

> **The bundle is then a secret.** It holds password hashes and salts next to logins and email
> addresses. Treat it like a copy of Redmine's database. The tool creates it readable by you only
> (a folder with mode 0700, files 0600). Keep it that way, copy it only over an encrypted channel,
> and delete it once the migration is done. Administrators' hashes are never exported.

### Other mapping options

| Option | Use |
|---|---|
| `--status-category-map "Awaiting Review=active,Verified=done"` | Redmine only records whether a status closes an issue. Specivo groups statuses into backlog, active, done and closed; use it to correct the grouping. |
| `--cf-key-map "Story Points=points"` | Choose the metadata key a custom field is stored under. |
| `--batch-size N` | Rows read per query (default 500). |
| `--quiet` | Print only errors. |

Run `specivo-redmine-migration --help` for the full list.

## 4. Move the export to the Specivo host

Copy the folder with `rsync`. It resumes if interrupted and skips files already there, so the
second run after a later export only moves what is new:

```bash
rsync -a --partial redmine-export/ specivo.example.com:/srv/specivo-import/export/
```

(`--info=progress2` helps on a big copy.) Attachments are named by the hash of their content, so
a re-export never changes a file that was already copied.

### Where things live on the Specivo host

The Specivo containers do not see the host's folders unless you mount them. Use two folders:

| Host folder | Holds | Mounted in the container |
|---|---|---|
| `/srv/specivo-import/export` | the export (records and `files/`) | `/import/export`, read-only |
| `/srv/specivo-import/state` | the **links file** | `/import/state`, writable |

The links file is a small SQLite file in which the load remembers texts that point at records not
loaded yet (see [section 6](#6-migrating-in-several-runs)). Two rules for it:

- **Keep it on a mounted host folder**, not inside the container, or it is lost with the
  container.
- **Keep that folder writable only by the operator** (and by the container's user, `10001`).
  Anyone else who can write there could tamper with the file while an import opens it.

Create the folders and give the state folder to the container user:

```bash
sudo mkdir -p /srv/specivo-import/export /srv/specivo-import/state
sudo chown -R 10001:10001 /srv/specivo-import/state
sudo chmod 0700 /srv/specivo-import/state
```

Make sure the export folder is readable by user `10001`
(`sudo chmod -R a+rX /srv/specivo-import/export` is enough if it holds no passwords; if you used
`--import-passwords`, `chown -R 10001:10001` it and keep mode 0700 instead). Then add a
`docker-compose.override.yml` next to Specivo's `docker-compose.yml`:

```yaml
services:
  api:
    volumes:
      - /srv/specivo-import/export:/import/export:ro
      - /srv/specivo-import/state:/import/state
```

and recreate the container:

```bash
docker compose up -d api
```

## 5. Load it into Specivo

All commands run inside the `api` container of your Specivo stack (`docker compose exec api ...`).
Do these three steps in order. The first two change nothing.

**1. Validate.** Checks the bundle's structure, cross-references and files. It does not touch the
database.

```bash
docker compose exec api python -m specivo.cli.import_data --source bundle \
  --path /import/export --validate
```

**2. Dry run.** Runs the whole import inside a transaction that is always rolled back, then prints
the summary. Read it: it lists the decisions the import made that you may want to change.

```bash
docker compose exec api python -m specivo.cli.import_data --source bundle \
  --path /import/export --links-file /import/state/links.sqlite --dry-run
```

**3. Import.** The same command without `--dry-run`:

```bash
docker compose exec api python -m specivo.cli.import_data --source bundle \
  --path /import/export --links-file /import/state/links.sqlite
```

If the import is interrupted, it prints a run identifier. Continue with
`--resume RUN_ID`; whatever landed is kept and the rest continues.

Notes on the options:

- `--links-file` is given on every load of this source, with the same path. Without it the file
  goes next to the bundle, which is a read-only mount here: the import still succeeds, but with a
  warning that its references will not be linked later.
- `--source-instance NAME` overrides the instance name in the bundle's manifest. You rarely need it.
- `--report-json PATH` also writes the summary as JSON.
- `--flatten-excess-depth` imports projects nested deeper than Specivo allows by re-parenting them
  higher up. Without it, they are skipped with a warning.
- If the bundle is a `.zip` with the files apart, add `--files-path /import/files` to read the
  attachments from there. A zip needs free space to be unpacked, and has size limits
  (`--max-unpacked-size`, `--max-entries`, `--max-entry-size`, `--max-compression-ratio`,
  `--max-line-size`, `--max-json-size`) that a real migration stays well under; a bundle over a
  limit is refused with a `too_large` problem and nothing is loaded.

### Administrators and passwords

**Administrators.** By default Redmine's administrators arrive as ordinary accounts, and the
summary lists them so you can grant the rights by hand. Add `--import-admins` to make them
administrators in Specivo too.

**Passwords.** Without `--import-passwords`, every imported account has a password nobody holds
and is marked as owing one. Set a password from **Administration, Users**, or tell people to use
password recovery if mail is configured.

To let people keep their password, you must ask for it in three places: export with
`--import-passwords` (section 3), enable Redmine's hash format in Specivo's `.env`, and load with
`--import-passwords`:

```bash
# in .env, then: docker compose up -d
LEGACY_PASSWORD_ALGORITHMS=["redmine_sha1"]
```

```bash
docker compose exec api python -m specivo.cli.import_data --source bundle \
  --path /import/export --links-file /import/state/links.sqlite --import-passwords
```

If the algorithm is not enabled, a load with `--import-passwords` stops before it writes anything.

What happens to each account:

- Local accounts keep their password. Specivo wraps the old salted SHA1 hash in bcrypt on import
  and replaces it with an ordinary bcrypt hash at the person's first sign-in.
- **Administrators always set a new password**, even with the flag.
- LDAP accounts and accounts without a local password take the reset path as usual.

Each imported password costs about a quarter of a second, roughly four extra minutes per thousand
accounts. Redmine's SHA1 scheme is weak, so retire the old hashes once most people have signed in:

```bash
docker compose exec api python -m specivo.cli.admin legacy-passwords          # who still holds one
docker compose exec api python -m specivo.cli.admin legacy-passwords expire --yes
```

or set `LEGACY_PASSWORD_HASHES_EXPIRE_AT=2027-03-31T00:00:00Z` in `.env`.

## 6. Migrating in several runs

A big installation is rarely moved in one go. There are two ways to split the work, and they can be
combined.

### Delta exports: a rehearsal, then the real thing

Export the whole installation early, load it, and let people look. Close to cut-over, export again
into the **same folder with the same instance name**, `rsync` again, and load with
`--continue-import`:

```bash
docker compose exec api python -m specivo.cli.import_data --source bundle \
  --path /import/export --links-file /import/state/links.sqlite --continue-import --dry-run
docker compose exec api python -m specivo.cli.import_data --source bundle \
  --path /import/export --links-file /import/state/links.sqlite --continue-import
```

Records and files already imported are recognised and skipped. Only what is new is created.

A bundle names its own source, so loading one whose instance was imported before is refused
unless you pass `--continue-import` (`--resume` implies it). That is on purpose: a bundle cannot
quietly attach its records to an import it does not belong to. The refusal also happens in
`--dry-run`. The first load of a new instance does not need the flag.

A delta adds new records. It does not update records you already imported, so edits made in Redmine
after the first load do not overwrite anything in Specivo.

### One project at a time, in any order

Export each project (or group of projects) on its own with `--project`, into its own folder, and
use the same `--instance` every time:

```bash
specivo-redmine-migration --files-dir /var/lib/redmine/files --out export-acme \
  --instance redmine.example.com --project acme-app --validate
specivo-redmine-migration --files-dir /var/lib/redmine/files --out export-ops \
  --instance redmine.example.com --project internal-ops --validate
```

Load the first as usual and every later one with `--continue-import`, always with the same
`--links-file`. Lookups, users and groups that two bundles share are recognised from the
first one.

You can load the projects in any order. A reference to an issue of a project that is not loaded
yet is written as plain text at first, because Specivo cannot know the new key. For example,
`#15` shows as `#15`. The load records that text in the **links file**. When the other project's
bundle is loaded, the load goes back through the waiting texts and turns them into links. The
summary counts the texts it linked.

Things to know about the links file:

- It holds the source text of those pieces of content. It is created readable by its owner only.
- A text somebody edited in Specivo between two loads is never overwritten. It keeps the plain-text
  reference, and the summary says how many texts were left alone.
- It works only with the Specivo that wrote it. Each entry is signed with a key derived from
  `SECRET_KEY`. After changing `SECRET_KEY`, or on another installation, entries are ignored (with
  a warning).
- It must be a regular file. A symbolic link is refused.
- It deletes itself once nothing is waiting. It is a throwaway: if you lose it, the remaining
  references stay plain text.
- `--dry-run` reads it and reports what would be linked, without writing anything. `--validate`
  ignores it.

### Linking what is waiting, without a new import

To see what is still waiting, and to link what can be linked now:

```bash
docker compose exec api python -m specivo.cli.admin import-map pending \
  --links-file /import/state/links.sqlite
# a dry run: reports what it would link
docker compose exec api python -m specivo.cli.admin import-map resolve-pending \
  --links-file /import/state/links.sqlite
# for real
docker compose exec api python -m specivo.cli.admin import-map resolve-pending \
  --links-file /import/state/links.sqlite --yes
```

Without `--yes`, `resolve-pending` changes nothing.

## 7. Coming from the import built into Specivo before 0.11.0

Specivo before 0.11.0 could import Redmine directly (`--source redmine`). That importer is gone from
0.11.0. The tool in this repository carries the same data under the same identifiers, so you can
carry on from where that import stopped, as long as the bundle uses the same scope. A scope is
the pair of source system and instance name under which Specivo recorded the earlier import.

The built-in import named its scope after `--source-instance` if you gave one, otherwise after the
source database URL with its password masked, such as
`postgresql://redmine:***@db.internal/redmine`. Find out what you have:

```bash
docker compose exec api python -m specivo.cli.admin import-map list
```

Then pick the instance name for the tool's export. If the old import used `--source-instance`, just
export with `--instance` set to that name. Otherwise move the old scope to the name the export
will use. The move is one transaction, refused if the new name already holds rows, and a dry run
unless you add `--yes`:

```bash
docker compose exec api python -m specivo.cli.admin import-map rename --system redmine \
  --from 'postgresql://redmine:***@db.internal/redmine' --to redmine.example.com
# the dry run counts the rows that would move; then do it
docker compose exec api python -m specivo.cli.admin import-map rename --system redmine \
  --from 'postgresql://redmine:***@db.internal/redmine' --to redmine.example.com --yes
```

Now export with the tool using `--instance redmine.example.com` (the same name you moved the scope
to) and load the bundle with `--continue-import`. Do the dry run first and read what it says:

```bash
docker compose exec api python -m specivo.cli.import_data --source bundle \
  --path /import/export --links-file /import/state/links.sqlite --continue-import --dry-run
```

It should report that the source was imported before and that nothing is created except what was
added in Redmine since. Then run it without `--dry-run`. Records that have no author still belong
to the `redmine-import` account.

If you skip the rename, the tool's bundle has a new scope, and the load would create everything a
second time.

## 8. After cut-over

Once Redmine is retired and nothing more will be imported from it, clean up.

**Forget what the import remembered.** The identifier map is how a later import recognises what is
already in Specivo. A dry run counts the rows; add `--yes` to delete them:

```bash
docker compose exec api python -m specivo.cli.admin import-map purge \
  --system redmine --instance redmine.example.com
docker compose exec api python -m specivo.cli.admin import-map purge \
  --system redmine --instance redmine.example.com --yes
```

The imported issues, pages and accounts stay. But after a purge **no further delta import of that
source is possible**: another load would create everything again.

**Delete the copies of your data:**

- the export folder on the Specivo host and on the machine where you ran the export, especially if
  it holds password hashes;
- the links file (`/srv/specivo-import/state/links.sqlite`);
- the `docker-compose.override.yml` mounts you added, then `docker compose up -d api`;
- the read-only database account you created in Redmine.

Then finish the setup in Specivo: give new roles their permissions (**Administration, Roles**),
check the imported groups under **Administration, Groups** (suffixed names point at a clash with an
existing group), give accounts that owe a password a way in, and decide on anonymous access (below).

## 9. Verifying and troubleshooting

### Reading the summary

Every dry run and import ends with a summary of what was created, skipped and left alone, plus
warnings. Keep the dry-run summary and compare it to the real one. Then spot-check:

- project, issue and wiki page counts against Redmine;
- a few long issues with history, one with attachments, one wiki page with several revisions;
- logged time totals for one project;
- references: open an issue whose description mentions `#123`.

Specivo identifies an issue by a project key and a number (`ACME-15`), where Redmine had only
`#123`, so a reference you check should now show the new key.

The summary also lists what needs a decision: administrators, locked or not yet active accounts,
LDAP accounts, newly created roles, and anonymous access. Redmine's anonymous read access is
carried over per project, but **never turns on** Specivo's instance-wide switch for it; review the
listed projects, then enable it yourself under **Administration, Settings, Anonymous access**.

### Common warnings and what they mean

| Warning | Meaning |
|---|---|
| `external_ref` (from `--validate`) | Texts refer to an issue that is not in this bundle (a project exported separately). Fine when you load the other project later; it becomes a link then. |
| Source "was imported before", import refused | The bundle's instance already has rows. Add `--continue-import` if it is the same installation. If it is a different one, give it another `--instance`. |
| `too_large` | The bundle is over a size limit. Raise the matching `--max-...` option for a legitimate bundle. |
| `missing_file` | An attachment's file is not in `files/`. Re-export with the right `--files-dir`, or accept it; the manifest lists it under `not_carried_over`. |
| `hash_mismatch` | A file's bytes do not match its name. The copy was damaged: run `rsync` again. |
| `unsafe_entry`, or a symbolic link refused | A zip entry or file is not allowed, for instance a link. Re-export; do not edit bundles by hand. |
| Links file warnings (not writable, unreadable, other scope, symbolic link) | The import went ahead, but its unresolved references will not be linked later. Fix the folder or path and pass the same `--links-file` next time. |
| Some texts "left alone" | Somebody edited them in Specivo after the load. They keep the plain-text reference. |
| Suffixed logins, emails or group names (`jsmith-2`) | They clashed with existing accounts or groups. Each is listed in the summary. |
| Projects skipped for depth | They are nested deeper than Specivo allows. Use `--flatten-excess-depth`. |
| Texts "written as plain text" | They name records neither in this bundle nor in an earlier import. They become links if a later load brings those records. |

### Other problems

- **The export refuses with an instance error.** Redmine's host name setting is empty or a local
  value. Pass `--instance`.
- **The first load worked, the second one says nothing was created.** That is the expected result of
  loading the same export twice.
- **A second load created everything again.** The instance name changed between exports. Rename
  the scope back or forward with `import-map rename` (section 7), before loading anything else.
- **The container cannot see the folder.** Check the override file and run `docker compose up -d
  api` again. The folder must be readable by user `10001`.
- **A long export makes the database complain** (replication conflicts, growing bloat). Export from
  a replica or at a quiet hour; see [section 2](#2-before-you-start).
- **Only some things went wrong.** Read the dry-run summary first. `--report-json PATH` keeps it as
  a file.

For a problem in the export itself, open an issue at
[github.com/specivo/redmine-migration-tool](https://github.com/specivo/redmine-migration-tool/issues).
Remove passwords, hostnames and personal data from anything you paste.

---

This tool is not affiliated with or endorsed by the Redmine project. "Redmine" is a trademark of
its respective owner and is used here only to say which system the tool reads.
