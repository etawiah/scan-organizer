"""Config-file resolution: SCAN_CONFIG_DIR, /config, /app/config, ./config.

Fake data only. Path logs must never include file contents.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from scanorganizer import config as config_mod
from scanorganizer.config import (
    VOCABULARY_FILENAME,
    resolve_private_terms_path,
    resolve_taxonomy_path,
    resolve_vocabulary_path,
)
from scanorganizer.privacy import SensitiveTerms
from scanorganizer.vocabulary import (
    clear_vocabulary_cache,
    load_outbound_vocabulary,
)


MINIMAL_TAXONOMY = """
confidence_threshold: 0.75
tree:
  Personal:
    "":
      - Correspondence
"""

SECRET_TERM = "Zebulon Quill"
UNIQUE_OVERRIDE = "overridemarker"
UNIQUE_HOST = "hostmarker"
UNIQUE_APP = "appmarker"
UNIQUE_CWD = "cwdmarker"


@pytest.fixture()
def search_tree(tmp_path, monkeypatch):
    override = tmp_path / "override"
    host = tmp_path / "config_mount"
    app = tmp_path / "app_config"
    cwd = tmp_path / "cwd_config"
    for directory in (override, host, app, cwd):
        directory.mkdir()
    monkeypatch.setattr(
        config_mod, "_builtin_config_dirs", lambda: (host, app, cwd),
    )
    monkeypatch.delenv("SCAN_CONFIG_DIR", raising=False)
    clear_vocabulary_cache()
    return {"override": override, "host": host, "app": app, "cwd": cwd}


def _write_vocab(directory: Path, word: str) -> Path:
    path = directory / VOCABULARY_FILENAME
    path.write_text(f"# test vocab\n{word}\ninvoice\n", encoding="utf-8")
    return path


def _write_taxonomy(directory: Path, marker_owner: str = "Personal") -> Path:
    path = directory / "taxonomy.yaml"
    path.write_text(
        MINIMAL_TAXONOMY.replace("Personal", marker_owner),
        encoding="utf-8",
    )
    return path


def _write_terms(directory: Path, term: str = SECRET_TERM) -> Path:
    path = directory / "private_terms.txt"
    path.write_text(f"# secret file\n{term}\n", encoding="utf-8")
    return path


def test_scan_config_dir_wins_over_later_dirs(search_tree, monkeypatch):
    monkeypatch.setenv("SCAN_CONFIG_DIR", str(search_tree["override"]))
    override = _write_vocab(search_tree["override"], UNIQUE_OVERRIDE)
    _write_vocab(search_tree["host"], UNIQUE_HOST)
    _write_vocab(search_tree["app"], UNIQUE_APP)
    _write_vocab(search_tree["cwd"], UNIQUE_CWD)
    clear_vocabulary_cache()
    assert resolve_vocabulary_path() == override
    vocab = load_outbound_vocabulary()
    assert UNIQUE_OVERRIDE in vocab
    assert UNIQUE_HOST not in vocab
    assert UNIQUE_APP not in vocab
    assert UNIQUE_CWD not in vocab


def test_config_mount_wins_over_app_and_cwd(search_tree):
    host = _write_vocab(search_tree["host"], UNIQUE_HOST)
    _write_vocab(search_tree["app"], UNIQUE_APP)
    _write_vocab(search_tree["cwd"], UNIQUE_CWD)
    clear_vocabulary_cache()
    assert resolve_vocabulary_path() == host
    assert UNIQUE_HOST in load_outbound_vocabulary()
    assert UNIQUE_APP not in load_outbound_vocabulary()


def test_app_config_wins_over_cwd(search_tree):
    app = _write_vocab(search_tree["app"], UNIQUE_APP)
    _write_vocab(search_tree["cwd"], UNIQUE_CWD)
    clear_vocabulary_cache()
    assert resolve_vocabulary_path() == app
    assert UNIQUE_APP in load_outbound_vocabulary()
    assert UNIQUE_CWD not in load_outbound_vocabulary()


def test_cwd_config_used_when_others_missing(search_tree):
    cwd = _write_vocab(search_tree["cwd"], UNIQUE_CWD)
    clear_vocabulary_cache()
    assert resolve_vocabulary_path() == cwd
    assert UNIQUE_CWD in load_outbound_vocabulary()


def test_taxonomy_and_private_terms_use_the_same_order(search_tree, monkeypatch):
    monkeypatch.setenv("SCAN_CONFIG_DIR", str(search_tree["override"]))
    tax = _write_taxonomy(search_tree["override"], "Campaign")
    terms = _write_terms(search_tree["override"])
    _write_taxonomy(search_tree["host"], "Household")
    _write_terms(search_tree["host"], "Marta Quill")
    clear_vocabulary_cache()
    assert resolve_taxonomy_path() == tax
    assert resolve_private_terms_path() == terms
    loaded = SensitiveTerms.load(path=terms, include_builtins=False)
    assert loaded.contains_user_term("ZEBULON")


def test_vocabulary_missing_fails_closed(search_tree):
    clear_vocabulary_cache()
    with pytest.raises(RuntimeError, match="missing or empty"):
        resolve_vocabulary_path()
    with pytest.raises(RuntimeError, match="missing or empty"):
        load_outbound_vocabulary()


def test_vocabulary_empty_fails_closed(search_tree):
    path = search_tree["cwd"] / VOCABULARY_FILENAME
    path.write_text("# only comments\n\n", encoding="utf-8")
    clear_vocabulary_cache()
    # The file exists so resolve succeeds; load must still fail closed.
    assert resolve_vocabulary_path() == path
    with pytest.raises(RuntimeError, match="missing or empty"):
        load_outbound_vocabulary()


def test_resolution_logs_path_not_contents(search_tree, caplog):
    path = _write_vocab(search_tree["cwd"], UNIQUE_CWD)
    _write_terms(search_tree["cwd"], SECRET_TERM)
    clear_vocabulary_cache()
    caplog.set_level("INFO")
    assert resolve_vocabulary_path() == path
    assert resolve_private_terms_path() is not None
    joined = " ".join(record.getMessage() for record in caplog.records)
    assert str(path) in joined
    assert UNIQUE_CWD not in joined
    assert SECRET_TERM not in joined
    assert "Zebulon" not in joined


def test_cli_uses_resolved_taxonomy(search_tree, monkeypatch):
    from scanorganizer.cli import _resolve_paths, build_parser

    tax = _write_taxonomy(search_tree["host"], "Family")
    monkeypatch.setattr(
        config_mod, "_builtin_config_dirs",
        lambda: (search_tree["host"], search_tree["app"], search_tree["cwd"]),
    )
    clear_vocabulary_cache()
    args = build_parser().parse_args(["--non-interactive"])
    args = _resolve_paths(args)
    assert args.taxonomy == tax
    assert args.private_terms is None


def test_compose_mount_target_is_on_the_search_path():
    dirs = config_mod._builtin_config_dirs()
    assert Path("/config") in dirs
    assert Path("/app/config") in dirs
    assert Path("config") in dirs
    # Env override is first when set — compose sets SCAN_CONFIG_DIR=/config.
    assert config_mod.config_search_dirs()[0] == Path("/config")
