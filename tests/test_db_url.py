"""``--db-url`` is checked before anything runs, and its password never shows."""

from __future__ import annotations

import pytest

from specivo_redmine_migration.cli import _redacted, main


@pytest.mark.parametrize(
    "url",
    [
        "not a url",
        "oracle://redmine:s3cret-value@db.example.com/redmine",
        "postgresql://redmine:s3cret-value@db.example.com:notaport/redmine",
        "postgresql://redmine:s3cret-value@db.example.com/",
        "mysql+nosuchdriver://redmine:s3cret-value@db.example.com/redmine",
    ],
)
def test_a_bad_db_url_is_a_usage_error(tmp_path, capsys, url):
    assert main(["--db-url", url, "--out", str(tmp_path / "b"), "--instance", "x"]) == 2
    err = capsys.readouterr().err
    assert "--db-url" in err
    assert "s3cret-value" not in err
    assert "Traceback" not in err
    assert len(err.splitlines()) <= 3
    assert not (tmp_path / "b").exists()


def test_redacted_masks_the_password_of_a_good_url():
    url = "postgresql://redmine:s3cret-value@db.example.com/redmine"
    assert "s3cret-value" not in _redacted(RuntimeError(f"failed for {url} with s3cret-value"), url)


@pytest.mark.parametrize("url", ["not a url", "postgresql://redmine:s3cret-value@db:bad/redmine", ""])
def test_redacted_never_raises_and_still_masks(url):
    text = _redacted(RuntimeError("connecting with s3cret-value failed"), url)
    assert "RuntimeError" in text
    if "s3cret-value" in url:
        assert "s3cret-value" not in text
