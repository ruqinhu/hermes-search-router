from __future__ import annotations

import time

import pytest

from hsr.config import (
    ConfigError,
    ConfigStore,
    load_config,
    parse_config,
)


def test_missing_file_returns_defaults(tmp_path):
    config = load_config(tmp_path / "routes.yaml")
    assert config.active == "auto"
    assert set(config.backends) == {"deepseek", "anysearch"}
    assert config.search_fallback == ("deepseek", "anysearch")
    assert config.on_empty == "fallback"


def test_parse_valid_config(tmp_path):
    path = tmp_path / "routes.yaml"
    path.write_text(
        """
version: 1
active: script:local
fallback:
  search: "deepseek, anysearch"
  extract: [anysearch]
on_empty: accept
search_total_timeout: 12
extract_total_timeout: 34
cooldown_seconds: 7
backends:
  deepseek:
    type: deepseek
    timeout: 9
  anysearch:
    type: anysearch
  local:
    type: script
    mode: argv
    command: ["/usr/bin/python3", "search.py", "{query}", "--count", "{limit}"]
    results_path: data.results
    fields: {title: t, url: u, description: s}
    timeout: 5
""",
        encoding="utf-8",
    )
    config = load_config(path)
    assert config.active == "local"
    assert config.search_fallback == ("deepseek", "anysearch")
    assert config.extract_fallback == ("anysearch",)
    assert config.on_empty == "accept"
    assert config.search_total_timeout == 12
    assert config.extract_total_timeout == 34
    assert config.cooldown_seconds == 7
    assert config.backends["deepseek"].timeout == 9
    script = config.backends["local"]
    assert script.mode == "argv"
    assert script.command[0] == "/usr/bin/python3"
    assert script.results_path == "data.results"
    assert script.fields == {"title": "t", "url": "u", "description": "s"}


def test_unknown_active_is_rejected():
    with pytest.raises(ConfigError, match="active backend"):
        parse_config(
            {
                "active": "nope",
                "backends": {"deepseek": {"type": "deepseek"}},
            }
        )


def test_script_requires_command():
    with pytest.raises(ConfigError, match="command"):
        parse_config(
            {
                "backends": {
                    "local": {"type": "script", "mode": "canonical"},
                }
            }
        )


def test_shell_string_command_is_rejected():
    with pytest.raises(ConfigError, match="not a shell string"):
        parse_config(
            {
                "backends": {
                    "local": {
                        "type": "script",
                        "command": "python3 search.py {query}",
                    }
                }
            }
        )


def test_invalid_type_is_rejected():
    with pytest.raises(ConfigError, match="type must be"):
        parse_config({"backends": {"local": {"type": "curl"}}})


def test_shell_eval_command_is_rejected():
    with pytest.raises(ConfigError, match="shell eval flag"):
        parse_config(
            {
                "backends": {
                    "local": {
                        "type": "script",
                        "command": ["bash", "-lc", "python search.py {query}"],
                    }
                }
            }
        )


def test_interpreter_eval_command_is_rejected():
    with pytest.raises(ConfigError, match="interpreter eval flag"):
        parse_config(
            {
                "backends": {
                    "local": {
                        "type": "script",
                        "command": ["python3", "-c", "print('{query}')"],
                    }
                }
            }
        )


def test_shell_script_path_without_eval_flag_is_allowed():
    config = parse_config(
        {
            "backends": {
                "local": {
                    "type": "script",
                    "command": ["bash", "/tmp/search.sh", "{query}"],
                }
            }
        }
    )
    assert config.backends["local"].command[0] == "bash"


@pytest.mark.parametrize(
    "command",
    [
        ["/usr/bin/env", "bash", "-c", "curl {query}"],
        ["python3", "-uc", "print('{query}')"],
        ["node", "--eval=console.log('{query}')"],
        ["busybox", "sh", "-c", "wget {query}"],
    ],
)
def test_wrapped_eval_commands_are_rejected(command):
    with pytest.raises(ConfigError, match="eval flag"):
        parse_config(
            {
                "backends": {
                    "local": {"type": "script", "command": command},
                }
            }
        )


def test_env_passthrough_is_validated():
    with pytest.raises(ConfigError, match="env_passthrough"):
        parse_config(
            {
                "backends": {
                    "local": {
                        "type": "script",
                        "command": ["/bin/true"],
                        "env_passthrough": ["NOT-A-NAME"],
                    }
                }
            }
        )


def test_on_empty_is_validated():
    with pytest.raises(ConfigError, match="on_empty"):
        parse_config(
            {
                "on_empty": "retry",
                "backends": {"deepseek": {"type": "deepseek"}},
            }
        )


def test_hot_reload_and_last_good_config(tmp_path):
    path = tmp_path / "routes.yaml"
    path.write_text(
        "version: 1\nactive: deepseek\nbackends:\n"
        "  deepseek: {type: deepseek}\n  anysearch: {type: anysearch}\n",
        encoding="utf-8",
    )
    store = ConfigStore(path)
    assert store.get().active == "deepseek"

    time.sleep(0.02)
    path.write_text(
        "version: 1\nactive: nope\nbackends:\n"
        "  deepseek: {type: deepseek}\n",
        encoding="utf-8",
    )
    assert store.get().active == "deepseek"
    assert store.last_error

    time.sleep(0.02)
    path.write_text(
        "version: 1\nactive: anysearch\nbackends:\n"
        "  deepseek: {type: deepseek}\n  anysearch: {type: anysearch}\n",
        encoding="utf-8",
    )
    assert store.get().active == "anysearch"


def test_invalid_utf8_config_raises(tmp_path):
    path = tmp_path / "routes.yaml"
    path.write_bytes(b"\xff\xfe\x00active: auto\n")
    with pytest.raises(ConfigError, match="UTF-8"):
        load_config(path)


def test_malformed_file_after_missing_default_raises(tmp_path):
    path = tmp_path / "routes.yaml"
    store = ConfigStore(path)
    assert store.get().active == "auto"  # synthesized default
    time.sleep(0.02)
    path.write_text("version: 1\nactive: nope\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        store.get()


def test_deleting_real_config_reverts_to_defaults(tmp_path):
    path = tmp_path / "routes.yaml"
    path.write_text(
        "version: 1\nactive: anysearch\nbackends:\n"
        "  deepseek: {type: deepseek}\n  anysearch: {type: anysearch}\n",
        encoding="utf-8",
    )
    store = ConfigStore(path)
    assert store.get().active == "anysearch"
    path.unlink()
    defaults = store.get()
    assert defaults.active == "auto"
    assert set(defaults.backends) == {"deepseek", "anysearch"}


def test_store_without_cached_invalid_config_raises(tmp_path):
    path = tmp_path / "routes.yaml"
    path.write_text("version: 1\nactive: nope\n", encoding="utf-8")
    store = ConfigStore(path)
    with pytest.raises(ConfigError):
        store.get()


def test_custom_backends_default_fallback_to_all():
    config = parse_config(
        {
            "backends": {
                "one": {"type": "script", "command": ["/bin/true"]},
                "two": {"type": "script", "command": ["/bin/true"]},
            }
        }
    )
    assert config.search_fallback == ("one", "two")
    assert config.extract_fallback == ("one", "two")
