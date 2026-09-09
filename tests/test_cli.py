import json

import pytest

from chess_tracker.cli import build_parser, load_config


def test_load_config_missing_default_path_returns_empty(tmp_path):
    missing = tmp_path / "does-not-exist.json"
    assert load_config(str(missing), required=False) == {}


def test_load_config_missing_explicit_path_exits(tmp_path):
    missing = tmp_path / "does-not-exist.json"
    with pytest.raises(SystemExit, match="Config file not found"):
        load_config(str(missing), required=True)


def test_load_config_invalid_json_exits(tmp_path):
    bad = tmp_path / "config.json"
    bad.write_text("{not valid json", encoding="utf-8")
    with pytest.raises(SystemExit, match="Invalid JSON"):
        load_config(str(bad), required=False)


def test_load_config_non_object_exits(tmp_path):
    bad = tmp_path / "config.json"
    bad.write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(SystemExit, match="must contain a JSON object"):
        load_config(str(bad), required=False)


def test_load_config_returns_recognised_keys(tmp_path):
    good = tmp_path / "config.json"
    good.write_text(json.dumps({"depth": 20, "threads": 4}), encoding="utf-8")
    assert load_config(str(good), required=False) == {"depth": 20, "threads": 4}


def test_load_config_drops_unrecognised_keys_without_failing(tmp_path, capsys):
    mixed = tmp_path / "config.json"
    mixed.write_text(json.dumps({"depth": 20, "bogus": "nope"}), encoding="utf-8")
    result = load_config(str(mixed), required=False)
    assert result == {"depth": 20}
    assert "bogus" in capsys.readouterr().err


def test_build_parser_applies_config_defaults():
    parser = build_parser({"depth": 20, "threads": 4})
    args = parser.parse_args(["--user", "alice"])
    assert args.depth == 20
    assert args.threads == 4


def test_build_parser_cli_flag_overrides_config_default():
    parser = build_parser({"depth": 20})
    args = parser.parse_args(["--user", "alice", "--depth", "10"])
    assert args.depth == 10


def test_build_parser_without_config_keeps_hardcoded_defaults():
    parser = build_parser()
    args = parser.parse_args(["--user", "alice"])
    assert args.depth == 14
    assert args.threads == 2


def test_quiet_and_verbose_are_mutually_exclusive():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["--user", "alice", "--quiet", "--verbose"])
