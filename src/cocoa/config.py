from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

_COCOA_ENV_PREFIX = "COCOA_"
_MODEL_MODES = ("balanced", "cheap", "premium", "local")
_MODEL_MODE_ENV = "COCOA_MODEL_MODE"

_legacy_warning_issued: set[str] = set()


def _global_config_dir() -> Path:
    return Path.home() / ".cocoa"


def _workspace_config_dir(cwd: Path) -> Path:
    return cwd / ".cocoa"


def _toml_path(base: Path) -> Path:
    return base / "cocoa.toml"


def _legacy_env_path(base: Path) -> Path:
    return base / "config.env"


def _parse_legacy_env(raw: str) -> dict[str, str]:
    env: dict[str, str] = {}
    for line in raw.splitlines():
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        if "=" not in text:
            continue
        key, value = text.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        env[key] = value
    return env


def _load_toml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        raw = path.read_bytes()
        return dict(tomllib.loads(raw.decode("utf-8")))
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_workspace_config(cwd: Path) -> dict[str, Any]:
    global_cfg = _load_toml(_toml_path(_global_config_dir()))
    local_cfg = _load_toml(_toml_path(_workspace_config_dir(cwd)))
    merged = _deep_merge(global_cfg, local_cfg)
    return merged


def legacy_load_workspace_config(cwd: Path) -> dict[str, Any]:
    path = _legacy_env_path(_workspace_config_dir(cwd))
    if not path.is_file():
        return {}
    cfg: dict[str, Any] = {}
    env = _parse_legacy_env(path.read_text(encoding="utf-8"))
    for key, value in env.items():
        if key == "COCOA_MODEL_MODE":
            cfg["default_mode"] = value
        elif key == "COCOA_PROVIDER":
            if value:
                cfg["provider_used"] = value
        elif key.startswith("COCOA_OPENAI_"):
            suffix = key[len("COCOA_OPENAI_"):].lower()
            _set_dotted(cfg, f"provider.openai.{suffix}", value)
        elif key.startswith("COCOA_CODEX_"):
            suffix = key[len("COCOA_CODEX_"):].lower()
            _set_dotted(cfg, f"provider.codex.{suffix}", value)
        elif key.startswith("COCOA_"):
            parts = key.split("_")
            if len(parts) >= 3:
                mode_name = parts[1].lower()
                if mode_name not in _MODEL_MODES:
                    continue
                rest = "_".join(parts[2:])
                if rest == "PROVIDER":
                    _set_dotted(cfg, f"mode.{mode_name}.provider", value)
                elif rest.startswith("OPENAI_") or rest.startswith("CODEX_"):
                    provider_name = rest.split("_", 1)[0].lower()
                    suffix = rest.split("_", 1)[1].lower()
                    _set_dotted(cfg, f"mode.{mode_name}.overrides.{provider_name}.{suffix}", value)
    return cfg


def set_dotted(target: dict[str, Any], dotted_key: str, value: Any) -> None:
    parts = dotted_key.split(".")
    for part in parts[:-1]:
        if part not in target or not isinstance(target[part], dict):
            target[part] = {}
        target = target[part]
    target[parts[-1]] = value


def _set_dotted(target: dict[str, Any], dotted_key: str, value: Any) -> None:
    parts = dotted_key.split(".")
    for part in parts[:-1]:
        if part not in target or not isinstance(target[part], dict):
            target[part] = {}
        target = target[part]
    target[parts[-1]] = value


def _get_dotted(target: dict[str, Any], dotted_key: str) -> Any:
    parts = dotted_key.split(".")
    current: Any = target
    for part in parts:
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def save_workspace_config(cwd: Path, data: dict[str, Any]) -> None:
    path = _toml_path(_workspace_config_dir(cwd))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_serialize_toml(data), encoding="utf-8")


def _serialize_toml_value(value: Any, indent: int = 0) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return str(value)
    if isinstance(value, str):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    if isinstance(value, dict):
        return _serialize_toml(value)
    return str(value)


def _serialize_toml(data: dict[str, Any]) -> str:
    lines: list[str] = []
    scalars: list[tuple[str, Any]] = []
    sections: list[tuple[str, dict[str, Any]]] = []

    for key, value in data.items():
        if isinstance(value, dict):
            sections.append((key, value))
        else:
            scalars.append((key, value))

    for key, value in scalars:
        lines.append(f'{key} = {_serialize_toml_value(value, 0)}')

    if scalars and sections:
        lines.append("")

    for section_key, section_value in sections:
        if sections.index((section_key, section_value)) > 0:
            lines.append("")
        _write_section(lines, section_key, section_value)

    return "\n".join(lines) + ("\n" if lines else "")


def _write_section(lines: list[str], key: str, value: dict[str, Any]) -> None:
    sub_scalars: list[tuple[str, Any]] = []
    sub_sections: list[tuple[str, dict[str, Any]]] = []

    for k, v in value.items():
        if isinstance(v, dict):
            sub_sections.append((k, v))
        else:
            sub_scalars.append((k, v))

    has_sub = bool(sub_sections)
    if sub_scalars or not has_sub:
        lines.append(f"[{key}]")
        for k, v in sub_scalars:
            lines.append(f'{k} = {_serialize_toml_value(v, 0)}')
    if has_sub:
        for k, v in sub_sections:
            full_key = f"{key}.{k}"
            _write_section(lines, full_key, v)


def _env_var_to_config_key(env_key: str) -> tuple[str, list[str]] | None:
    if not env_key.startswith(_COCOA_ENV_PREFIX):
        return None
    rest = env_key[len(_COCOA_ENV_PREFIX):]
    if not rest:
        return None
    parts = rest.split("_")
    if not parts:
        return None
    first = parts[0]

    if first == "PROVIDER":
        return ("provider_used", [])
    if first == "MODEL" and len(parts) == 1:
        return ("default_mode", [])

    if first in ("OPENAI", "CODEX"):
        provider_name = first.lower()
        suffix_parts = parts[1:]
        if not suffix_parts:
            return None
        suffix = "_".join(suffix_parts).lower()
        return ("provider", [provider_name, suffix])

    mode_name = first.lower()
    if mode_name in _MODEL_MODES:
        suffix_parts = parts[1:]
        if not suffix_parts:
            return None
        suffix = "_".join(suffix_parts).lower()
        if suffix == "provider":
            return ("mode", [mode_name, "provider"])
        if suffix.startswith("openai_"):
            return ("mode", [mode_name, "overrides", "openai", suffix[len("openai_"):]])
        if suffix.startswith("codex_"):
            return ("mode", [mode_name, "overrides", "codex", suffix[len("codex_"):]])

    return None


def _apply_env_overrides(data: dict[str, Any]) -> dict[str, Any]:
    result = dict(data)
    for env_key, env_value in sorted(os.environ.items()):
        mapping = _env_var_to_config_key(env_key)
        if mapping is None:
            continue
        section, path = mapping[0], mapping[1]
        if not path:
            result[section] = env_value
        else:
            full_path = f"{section}.{'.'.join(path)}"
            _set_dotted(result, full_path, env_value)
    return result


def resolve_config(
    cwd: Path,
    session_overrides: Mapping[str, str],
) -> dict[str, Any]:
    toml_path = _toml_path(_workspace_config_dir(cwd))
    legacy_path = _legacy_env_path(_workspace_config_dir(cwd))

    merged: dict[str, Any] = {}
    global_cfg = _load_toml(_toml_path(_global_config_dir()))
    if global_cfg:
        merged = _deep_merge(merged, global_cfg)

    if toml_path.is_file():
        local_cfg = _load_toml(toml_path)
        merged = _deep_merge(merged, local_cfg)
    elif legacy_path.is_file():
        try:
            legacy_cfg = legacy_load_workspace_config(cwd)
            merged = _deep_merge(merged, legacy_cfg)
        except OSError:
            pass
        cwd_str = str(cwd.resolve())
        if cwd_str not in _legacy_warning_issued:
            _legacy_warning_issued.add(cwd_str)
            import sys
            print("hint: migrate to .cocoa/cocoa.toml for structured config", file=sys.stderr)

    merged = _apply_env_overrides(merged)

    for key, value in session_overrides.items():
        if key == "COCOA_MODEL_MODE":
            merged["default_mode"] = value
        elif key == "COCOA_PROVIDER":
            merged["provider_used"] = value
        elif key.startswith("COCOA_OPENAI_"):
            suffix = key[len("COCOA_OPENAI_"):].lower()
            _set_dotted(merged, f"provider.openai.{suffix}", value)
        elif key.startswith("COCOA_CODEX_"):
            suffix = key[len("COCOA_CODEX_"):].lower()
            _set_dotted(merged, f"provider.codex.{suffix}", value)
        else:
            merged[key] = value

    return merged


def resolve_model_mode(config: dict[str, Any]) -> str:
    raw = config.get("default_mode", "")
    if isinstance(raw, str) and raw.strip().lower() in _MODEL_MODES:
        return raw.strip().lower()
    return "balanced"


def resolve_model_mode_from_env(env: Mapping[str, str]) -> str:
    raw = env.get(_MODEL_MODE_ENV, "").strip().lower()
    if raw in _MODEL_MODES:
        return raw
    return "balanced"


def provider_environment_for_mode(env: Mapping[str, str]) -> tuple[dict[str, str], bool]:
    mode = resolve_model_mode_from_env(env)
    prefix = f"COCOA_{mode.upper()}_"
    routed = dict(env)
    profile_used = False

    provider = env.get(f"{prefix}PROVIDER")
    if provider:
        routed["COCOA_PROVIDER"] = provider
        profile_used = True
    elif any(key.startswith(f"{prefix}OPENAI_") for key in env):
        routed["COCOA_PROVIDER"] = "openai"
        profile_used = True
    elif any(key.startswith(f"{prefix}CODEX_") for key in env):
        routed["COCOA_PROVIDER"] = "codex-http"
        profile_used = True

    key_map = {
        "OPENAI_API_KEY": "COCOA_OPENAI_API_KEY",
        "OPENAI_MODEL": "COCOA_OPENAI_MODEL",
        "OPENAI_BASE_URL": "COCOA_OPENAI_BASE_URL",
        "OPENAI_TIMEOUT_SECONDS": "COCOA_OPENAI_TIMEOUT_SECONDS",
        "OPENAI_TEMPERATURE": "COCOA_OPENAI_TEMPERATURE",
        "OPENAI_MAX_TOKENS": "COCOA_OPENAI_MAX_TOKENS",
        "CODEX_API_KEY": "COCOA_CODEX_API_KEY",
        "CODEX_MODEL": "COCOA_CODEX_MODEL",
        "CODEX_BASE_URL": "COCOA_CODEX_BASE_URL",
        "CODEX_HOME": "COCOA_CODEX_HOME",
        "CODEX_TIMEOUT_SECONDS": "COCOA_CODEX_TIMEOUT_SECONDS",
        "CODEX_TEMPERATURE": "COCOA_CODEX_TEMPERATURE",
        "CODEX_MAX_TOKENS": "COCOA_CODEX_MAX_TOKENS",
    }
    for source_suffix, target_key in key_map.items():
        source_key = f"{prefix}{source_suffix}"
        if source_key in env:
            routed[target_key] = env[source_key]
            profile_used = True

    generic_model = env.get(f"{prefix}MODEL")
    if generic_model:
        provider_name = routed.get("COCOA_PROVIDER", "").lower()
        if provider_name in {"codex", "codex-http", "codex-responses", "openai-codex"}:
            routed["COCOA_CODEX_MODEL"] = generic_model
        else:
            routed["COCOA_OPENAI_MODEL"] = generic_model
        profile_used = True

    return routed, profile_used


def to_env_mapping(config: dict[str, Any], mode: str) -> dict[str, str]:
    env: dict[str, str] = {}

    env["COCOA_MODEL_MODE"] = mode

    provider_cfg = config.get("provider", {})
    if not isinstance(provider_cfg, dict):
        provider_cfg = {}

    mode_cfg = config.get("mode", {})
    if isinstance(mode_cfg, dict):
        active_mode_cfg = mode_cfg.get(mode, {})
        if isinstance(active_mode_cfg, dict):
            provider_override = active_mode_cfg.get("provider")
            if provider_override and isinstance(provider_override, str):
                env["COCOA_PROVIDER"] = provider_override
            overrides = active_mode_cfg.get("overrides", {})
            if isinstance(overrides, dict):
                provider_cfg = _deep_merge(provider_cfg, overrides)

    if "COCOA_PROVIDER" not in env:
        provider_used = config.get("provider_used")
        if provider_used and isinstance(provider_used, str):
            env["COCOA_PROVIDER"] = provider_used

    for provider_name in ("openai", "codex"):
        provider_config = provider_cfg.get(provider_name, {})
        if not isinstance(provider_config, dict):
            continue
        prefix = f"COCOA_{provider_name.upper()}_"
        for key, value in provider_config.items():
            env_key = f"{prefix}{key.upper()}"
            if isinstance(value, (str, int, float)):
                env[env_key] = str(value)

    return env


def merge_session_overrides(data: dict[str, Any], overrides: Mapping[str, str]) -> dict[str, Any]:
    result = dict(data)
    for key, value in overrides.items():
        if not value:
            continue
        if key == "COCOA_MODEL_MODE":
            result["default_mode"] = value
        elif key == "COCOA_PROVIDER":
            result["provider_used"] = value
        elif key.startswith("COCOA_OPENAI_"):
            suffix = key[len("COCOA_OPENAI_"):].lower()
            set_dotted(result, f"provider.openai.{suffix}", value)
        elif key.startswith("COCOA_CODEX_"):
            suffix = key[len("COCOA_CODEX_"):].lower()
            set_dotted(result, f"provider.codex.{suffix}", value)
    return result


def convert_flat_overrides(overrides: Mapping[str, str]) -> dict[str, Any]:
    data: dict[str, Any] = {}
    for key, value in overrides.items():
        if not value:
            continue
        if key == "COCOA_MODEL_MODE":
            data["default_mode"] = value
        elif key == "COCOA_PROVIDER":
            data["provider_used"] = value
        elif key.startswith("COCOA_OPENAI_"):
            suffix = key[len("COCOA_OPENAI_"):].lower()
            set_dotted(data, f"provider.openai.{suffix}", value)
        elif key.startswith("COCOA_CODEX_"):
            suffix = key[len("COCOA_CODEX_"):].lower()
            set_dotted(data, f"provider.codex.{suffix}", value)
    return data


def config_to_routing_payload(
    cwd: Path,
    session_overrides: Mapping[str, str],
) -> dict[str, Any]:
    config = resolve_config(cwd, session_overrides)
    mode = resolve_model_mode(config)
    env = to_env_mapping(config, mode)

    from .providers import provider_name_from_env

    status: str
    try:
        status = provider_name_from_env(env)
    except Exception:
        status = "stub"

    provider = status
    model: str | None = None
    if ":" in status:
        provider, model = status.split(":", 1)
    elif status in {"stub"} or status.startswith("not configured ("):
        provider = status

    profile_used = bool(env.get("COCOA_PROVIDER"))
    reason = f"mode={mode}"
    if profile_used:
        reason += " profile override"
    else:
        reason += " default provider"

    payload: dict[str, Any] = {
        "mode": mode,
        "provider": provider,
        "status": status,
        "profile": mode if profile_used else "default",
        "reason": reason,
    }
    if model:
        payload["model"] = model
    return payload
