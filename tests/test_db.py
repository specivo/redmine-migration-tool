"""Unit tests for the Redmine database layer.

Covers URL handling and the hand-written table definitions. No database is
needed: the point of declaring tables by hand rather than reflecting them is
that they can be checked without a live Redmine.
"""

from __future__ import annotations

import pytest

from specivo_redmine_migration import db


class TestUrlNormalisation:
    def test_bare_postgres_url_gets_the_async_driver(self):
        """Operators copy connection strings out of Redmine's database.yml."""
        assert db.normalise_source_url("postgresql://u:p@host/redmine").drivername == "postgresql+asyncpg"

    def test_postgres_alias_is_handled(self):
        assert db.normalise_source_url("postgres://u:p@host/redmine").drivername == "postgresql+asyncpg"

    def test_sync_postgres_driver_is_upgraded(self):
        assert db.normalise_source_url("postgresql+psycopg2://u:p@h/r").drivername == "postgresql+asyncpg"

    def test_bare_mysql_url_gets_the_async_driver(self):
        assert db.normalise_source_url("mysql://u:p@host/redmine").drivername == "mysql+aiomysql"

    def test_sync_mysql_drivers_are_upgraded(self):
        assert db.normalise_source_url("mysql+pymysql://u:p@h/r").drivername == "mysql+aiomysql"
        assert db.normalise_source_url("mysql+mysqldb://u:p@h/r").drivername == "mysql+aiomysql"

    def test_explicit_async_driver_is_left_alone(self):
        url = db.normalise_source_url("postgresql+asyncpg://u:p@host/redmine")
        assert url.drivername == "postgresql+asyncpg"
        assert url.password == "p"

    def test_database_name_survives(self):
        assert db.normalise_source_url("mysql://u:p@host/redmine_production").database == "redmine_production"

    def test_password_survives_normalisation(self):
        """Regression: str(URL) masks the password, so a string round trip
        would replace the credential with asterisks and the connection would
        fail authentication with no clue why."""
        url = db.normalise_source_url("postgresql://redmine:s3cret@host/redmine")
        assert url.password == "s3cret"
        assert "***" not in url.render_as_string(hide_password=False)

    def test_port_and_user_survive(self):
        url = db.normalise_source_url("postgresql://redmine:s3cret@host:5444/redmine")
        assert url.port == 5444
        assert url.username == "redmine"


class TestCredentialMasking:
    def test_password_is_masked(self):
        """The URL reaches logs and the import report; the password must not."""
        masked = db.safe_url("postgresql://redmine:s3cret@host/redmine")
        assert "s3cret" not in masked
        assert "***" in masked

    def test_host_and_database_survive_masking(self):
        masked = db.safe_url("postgresql://redmine:s3cret@tracker.example.org/redmine")
        assert "tracker.example.org" in masked
        assert "redmine" in masked

    def test_url_without_a_password_is_fine(self):
        assert "host" in db.safe_url("postgresql://redmine@host/redmine")


class TestDriverCheck:
    def test_missing_driver_names_the_extra_to_install(self, monkeypatch):
        import importlib.util

        real_find_spec = importlib.util.find_spec
        monkeypatch.setattr(
            importlib.util,
            "find_spec",
            lambda name, *a, **k: None if name == "aiomysql" else real_find_spec(name, *a, **k),
        )
        with pytest.raises(db.SourceDriverMissingError) as exc:
            db.create_source_engine("mysql://u:p@host/redmine")
        assert "aiomysql" in str(exc.value)
        assert "pip install" in str(exc.value)

    def test_postgres_source_needs_no_extra(self):
        """asyncpg is a runtime dependency, so a PostgreSQL source just works."""
        engine = db.create_source_engine("postgresql://u:p@host/redmine")
        assert engine.dialect.name == "postgresql"

    def test_engine_url_keeps_the_database(self):
        engine = db.create_source_engine("postgresql://u:p@host/redmine_production")
        assert engine.url.database == "redmine_production"

    def test_engine_keeps_the_credential(self):
        """The engine must get the real password, not the masked rendering."""
        engine = db.create_source_engine("postgresql://redmine:s3cret@host/redmine")
        assert engine.url.password == "s3cret"


class TestTableDefinitions:
    def test_every_declared_table_is_registered(self):
        expected = {
            "projects",
            "enabled_modules",
            "projects_trackers",
            "trackers",
            "issue_statuses",
            "enumerations",
            "issue_categories",
            "versions",
            "settings",
            "users",
            "email_addresses",
            "groups_users",
            "roles",
            "members",
            "member_roles",
            "issues",
            "journals",
            "journal_details",
            "issue_relations",
            "watchers",
            "custom_fields",
            "custom_values",
            "custom_fields_trackers",
            "custom_fields_projects",
            "custom_field_enumerations",
            "wikis",
            "wiki_pages",
            "wiki_contents",
            "wiki_content_versions",
            "wiki_redirects",
            "attachments",
            "time_entries",
        }
        assert expected <= set(db.metadata.tables)

    def test_tracker_carries_the_bitmask_column(self):
        """Redmine 7 stores disabled core fields as fields_bits, not a name list."""
        assert "fields_bits" in db.trackers.c

    def test_status_has_only_the_binary_closed_flag(self):
        assert "is_closed" in db.issue_statuses.c
        assert "category" not in db.issue_statuses.c

    def test_users_table_has_no_email_column(self):
        """Redmine moved addresses to email_addresses in 3.2."""
        assert "mail" not in db.users.c
        assert "address" in db.email_addresses.c

    def test_users_table_discriminates_principals(self):
        assert "type" in db.users.c

    def test_wiki_history_is_compressed_bytes(self):
        """wiki_content_versions.data is bytea and may be gzipped."""
        assert "data" in db.wiki_content_versions.c
        assert "compression" in db.wiki_content_versions.c

    def test_attachments_carry_the_disk_layout_columns(self):
        assert "disk_filename" in db.attachments.c
        assert "disk_directory" in db.attachments.c

    def test_journal_detail_value_column_keeps_its_redmine_name(self):
        """Renamed to new_value only when it becomes a Specivo row."""
        assert "value" in db.journal_details.c
        assert "new_value" not in db.journal_details.c

    def test_only_the_password_columns_the_hasher_needs_are_declared(self):
        """The salted hash is read for an opt-in password import; nothing else is."""
        for needed in ("hashed_password", "salt", "auth_source_id"):
            assert needed in db.users.c
        for forbidden in ("twofa_scheme", "twofa_totp_key", "twofa_totp_last_used_at"):
            assert forbidden not in db.users.c
