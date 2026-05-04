from __future__ import annotations

import os
from pathlib import Path
from tempfile import TemporaryDirectory
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cocoa.config import (
    load_workspace_config,
    save_workspace_config,
    resolve_config,
    resolve_model_mode,
    to_env_mapping,
    legacy_load_workspace_config,
    merge_session_overrides,
    convert_flat_overrides,
    set_dotted,
    config_to_routing_payload,
)


class ConfigTests(unittest.TestCase):
    def test_load_toml_returns_empty_when_missing(self) -> None:
        with TemporaryDirectory() as tmpdir:
            cfg = load_workspace_config(Path(tmpdir))
        self.assertEqual(cfg, {})

    def test_save_and_load_roundtrip(self) -> None:
        with TemporaryDirectory() as tmpdir:
            data = {
                "default_mode": "premium",
                "provider_used": "openai",
                "provider": {
                    "openai": {
                        "api_key": "sk-test",
                        "model": "gpt-5",
                    },
                },
                "mode": {
                    "premium": {
                        "provider": "codex",
                        "overrides": {
                            "openai": {
                                "model": "deepseek-v4-pro",
                            },
                        },
                    },
                },
            }
            save_workspace_config(Path(tmpdir), data)
            loaded = load_workspace_config(Path(tmpdir))
        self.assertEqual(loaded.get("default_mode"), "premium")
        self.assertEqual(loaded.get("provider_used"), "openai")
        provider = loaded.get("provider", {})
        self.assertEqual(provider.get("openai", {}).get("api_key"), "sk-test")
        self.assertEqual(provider.get("openai", {}).get("model"), "gpt-5")

    def test_resolve_config_with_toml(self) -> None:
        with TemporaryDirectory() as tmpdir:
            data = {
                "default_mode": "cheap",
                "provider_used": "openai",
                "provider": {
                    "openai": {
                        "api_key": "sk-test",
                        "model": "gpt-5-mini",
                    },
                },
            }
            save_workspace_config(Path(tmpdir), data)
            cfg = resolve_config(Path(tmpdir), {})
        self.assertEqual(cfg.get("default_mode"), "cheap")
        self.assertEqual(cfg.get("provider_used"), "openai")

    def test_resolve_config_legacy_migration(self) -> None:
        with TemporaryDirectory() as tmpdir:
            cfg_dir = Path(tmpdir) / ".cocoa"
            cfg_dir.mkdir(parents=True, exist_ok=True)
            (cfg_dir / "config.env").write_text(
                "\n".join([
                    "COCOA_PROVIDER=openai",
                    "COCOA_OPENAI_API_KEY=sk-legacy",
                    "COCOA_OPENAI_MODEL=gpt-4",
                ]),
                encoding="utf-8",
            )
            cfg = resolve_config(Path(tmpdir), {})
        self.assertEqual(cfg.get("provider_used"), "openai")
        self.assertEqual(cfg.get("provider", {}).get("openai", {}).get("api_key"), "sk-legacy")
        self.assertEqual(cfg.get("provider", {}).get("openai", {}).get("model"), "gpt-4")

    def test_legacy_load_workspace_config(self) -> None:
        with TemporaryDirectory() as tmpdir:
            cfg_dir = Path(tmpdir) / ".cocoa"
            cfg_dir.mkdir(parents=True, exist_ok=True)
            (cfg_dir / "config.env").write_text(
                "\n".join([
                    "COCOA_PROVIDER=codex-http",
                    "COCOA_CODEX_API_KEY=sk-codex",
                    "COCOA_CODEX_MODEL=codex-mini",
                ]),
                encoding="utf-8",
            )
            cfg = legacy_load_workspace_config(Path(tmpdir))
        self.assertEqual(cfg.get("provider_used"), "codex-http")
        self.assertEqual(cfg.get("provider", {}).get("codex", {}).get("api_key"), "sk-codex")
        self.assertEqual(cfg.get("provider", {}).get("codex", {}).get("model"), "codex-mini")

    def test_legacy_mode_env_vars(self) -> None:
        with TemporaryDirectory() as tmpdir:
            cfg_dir = Path(tmpdir) / ".cocoa"
            cfg_dir.mkdir(parents=True, exist_ok=True)
            (cfg_dir / "config.env").write_text(
                "\n".join([
                    "COCOA_PREMIUM_PROVIDER=codex",
                    "COCOA_PREMIUM_OPENAI_MODEL=deepseek-v4-pro",
                ]),
                encoding="utf-8",
            )
            cfg = legacy_load_workspace_config(Path(tmpdir))
        mode_cfg = cfg.get("mode", {}).get("premium", {})
        self.assertEqual(mode_cfg.get("provider"), "codex")
        self.assertEqual(
            mode_cfg.get("overrides", {}).get("openai", {}).get("model"),
            "deepseek-v4-pro",
        )

    def test_env_var_override_priority(self) -> None:
        with TemporaryDirectory() as tmpdir:
            data = {
                "default_mode": "balanced",
                "provider_used": "openai",
                "provider": {
                    "openai": {
                        "api_key": "sk-toml",
                        "model": "gpt-5-mini",
                    },
                },
            }
            save_workspace_config(Path(tmpdir), data)
            with mock.patch.dict(
                os.environ,
                {"COCOA_OPENAI_MODEL": "gpt-5-turbo"},
                clear=False,
            ):
                cfg = resolve_config(Path(tmpdir), {})
        self.assertEqual(
            cfg.get("provider", {}).get("openai", {}).get("model"),
            "gpt-5-turbo",
        )

    def test_env_var_override_provider(self) -> None:
        with TemporaryDirectory() as tmpdir:
            data = {
                "default_mode": "balanced",
                "provider": {
                    "openai": {
                        "api_key": "sk-toml",
                        "model": "gpt-5-mini",
                    },
                },
            }
            save_workspace_config(Path(tmpdir), data)
            with mock.patch.dict(
                os.environ,
                {"COCOA_PROVIDER": "openai"},
                clear=False,
            ):
                cfg = resolve_config(Path(tmpdir), {})
        self.assertEqual(cfg.get("provider_used"), "openai")

    def test_session_overrides_priority(self) -> None:
        with TemporaryDirectory() as tmpdir:
            data = {
                "default_mode": "balanced",
                "provider_used": "openai",
                "provider": {
                    "openai": {
                        "api_key": "sk-toml",
                        "model": "gpt-5-mini",
                    },
                },
            }
            save_workspace_config(Path(tmpdir), data)
            overrides = {"COCOA_OPENAI_MODEL": "gpt-5-session"}
            cfg = resolve_config(Path(tmpdir), overrides)
        self.assertEqual(
            cfg.get("provider", {}).get("openai", {}).get("model"),
            "gpt-5-session",
        )

    def test_resolve_model_mode_default(self) -> None:
        self.assertEqual(resolve_model_mode({}), "balanced")

    def test_resolve_model_mode_from_config(self) -> None:
        self.assertEqual(resolve_model_mode({"default_mode": "cheap"}), "cheap")
        self.assertEqual(resolve_model_mode({"default_mode": "premium"}), "premium")
        self.assertEqual(resolve_model_mode({"default_mode": "local"}), "local")

    def test_resolve_model_mode_invalid_fallback(self) -> None:
        self.assertEqual(resolve_model_mode({"default_mode": "invalid"}), "balanced")

    def test_to_env_mapping_basic(self) -> None:
        config = {
            "default_mode": "balanced",
            "provider_used": "openai",
            "provider": {
                "openai": {
                    "api_key": "sk-test",
                    "model": "gpt-5-mini",
                    "base_url": "https://api.openai.com/v1",
                },
            },
        }
        env = to_env_mapping(config, "balanced")
        self.assertEqual(env.get("COCOA_MODEL_MODE"), "balanced")
        self.assertEqual(env.get("COCOA_PROVIDER"), "openai")
        self.assertEqual(env.get("COCOA_OPENAI_API_KEY"), "sk-test")
        self.assertEqual(env.get("COCOA_OPENAI_MODEL"), "gpt-5-mini")

    def test_to_env_mapping_mode_overrides(self) -> None:
        config = {
            "default_mode": "premium",
            "provider": {
                "openai": {
                    "api_key": "sk-test",
                    "model": "gpt-5-mini",
                },
            },
            "mode": {
                "premium": {
                    "overrides": {
                        "openai": {
                            "model": "deepseek-v4-pro",
                        },
                    },
                },
            },
        }
        env = to_env_mapping(config, "premium")
        self.assertEqual(env.get("COCOA_OPENAI_MODEL"), "deepseek-v4-pro")

    def test_to_env_mapping_mode_force_provider(self) -> None:
        config = {
            "default_mode": "premium",
            "provider": {
                "openai": {
                    "api_key": "sk-test",
                    "model": "gpt-5-mini",
                },
                "codex": {
                    "api_key": "sk-codex",
                    "model": "codex-pro",
                },
            },
            "mode": {
                "premium": {
                    "provider": "codex",
                    "overrides": {
                        "codex": {
                            "model": "codex-ultra",
                        },
                    },
                },
            },
        }
        env = to_env_mapping(config, "premium")
        self.assertEqual(env.get("COCOA_PROVIDER"), "codex")
        self.assertEqual(env.get("COCOA_CODEX_MODEL"), "codex-ultra")

    def test_set_dotted_creates_nested_dict(self) -> None:
        data: dict = {}
        set_dotted(data, "a.b.c", "value")
        self.assertEqual(data, {"a": {"b": {"c": "value"}}})

    def test_convert_flat_overrides_openai(self) -> None:
        overrides = {
            "COCOA_PROVIDER": "openai",
            "COCOA_OPENAI_API_KEY": "sk-test",
            "COCOA_OPENAI_MODEL": "gpt-5",
        }
        data = convert_flat_overrides(overrides)
        self.assertEqual(data.get("provider_used"), "openai")
        self.assertEqual(data.get("provider", {}).get("openai", {}).get("api_key"), "sk-test")
        self.assertEqual(data.get("provider", {}).get("openai", {}).get("model"), "gpt-5")

    def test_merge_session_overrides(self) -> None:
        data = {
            "default_mode": "balanced",
            "provider": {
                "openai": {
                    "api_key": "sk-base",
                    "model": "gpt-4",
                },
            },
        }
        merged = merge_session_overrides(data, {"COCOA_OPENAI_MODEL": "gpt-5"})
        self.assertEqual(merged.get("provider", {}).get("openai", {}).get("model"), "gpt-5")
        self.assertEqual(merged.get("provider", {}).get("openai", {}).get("api_key"), "sk-base")

    def test_config_to_routing_payload_returns_dict(self) -> None:
        with TemporaryDirectory() as tmpdir:
            data = {
                "default_mode": "balanced",
                "provider_used": "openai",
                "provider": {
                    "openai": {
                        "api_key": "sk-test",
                        "model": "gpt-5-mini",
                    },
                },
            }
            save_workspace_config(Path(tmpdir), data)
            payload = config_to_routing_payload(Path(tmpdir), {})
        self.assertIn("mode", payload)
        self.assertIn("provider", payload)
        self.assertIn("status", payload)
        self.assertEqual(payload.get("mode"), "balanced")

    def test_global_config_merged_with_local(self) -> None:
        with TemporaryDirectory() as tmpdir:
            home = Path.home()
            global_cfg_dir = home / ".cocoa"
            global_cfg_dir.mkdir(parents=True, exist_ok=True)
            global_toml = global_cfg_dir / "cocoa.toml"
            global_toml.write_text(
                'default_mode = "cheap"\n'
                '[provider.openai]\n'
                'api_key = "sk-global"\n'
                'model = "gpt-4"\n',
                encoding="utf-8",
            )
            local_data = {
                "provider": {
                    "openai": {
                        "model": "gpt-5",
                    },
                },
            }
            save_workspace_config(Path(tmpdir), local_data)
            try:
                cfg = resolve_config(Path(tmpdir), {})
                self.assertEqual(cfg.get("default_mode"), "cheap")
                self.assertEqual(
                    cfg.get("provider", {}).get("openai", {}).get("api_key"),
                    "sk-global",
                )
                self.assertEqual(
                    cfg.get("provider", {}).get("openai", {}).get("model"),
                    "gpt-5",
                )
            finally:
                global_toml.unlink(missing_ok=True)

    def test_serialize_and_reload_toml(self) -> None:
        with TemporaryDirectory() as tmpdir:
            data = {
                "default_mode": "premium",
                "provider_used": "openai",
                "provider": {
                    "openai": {
                        "api_key": "sk-test",
                        "model": "gpt-5",
                        "base_url": "https://api.openai.com/v1",
                        "timeout_seconds": 60,
                        "temperature": 0.7,
                        "max_tokens": 4096,
                    },
                },
                "mode": {
                    "premium": {
                        "provider": "codex",
                        "overrides": {
                            "openai": {
                                "model": "deepseek-v4-pro",
                            },
                        },
                    },
                    "cheap": {
                        "overrides": {
                            "openai": {
                                "model": "deepseek-v4-flash",
                            },
                        },
                    },
                },
            }
            save_workspace_config(Path(tmpdir), data)
            loaded = load_workspace_config(Path(tmpdir))
        self.assertEqual(loaded, data)


if __name__ == "__main__":
    unittest.main()
