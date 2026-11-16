"""统一从 config.yaml 选择运行模式，不读取 ELECFEE_ENV。"""
from pathlib import Path

import yaml


class ConfigError(ValueError):
    """启动配置缺失或格式错误。"""


def _load_mapping(path):
    try:
        with path.open(encoding="utf-8") as stream:
            data = yaml.safe_load(stream)
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ConfigError(f"无法读取 {path}：{exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path} 必须是 YAML 对象，不能为空")
    return data


def load_config(config_dir):
    """返回模式、业务配置路径、订阅路径、业务配置内容。"""
    config_dir = Path(config_dir)
    entry_path = config_dir / "config.yaml"
    entry = _load_mapping(entry_path)
    env = entry.get("env")
    if not isinstance(env, str) or env not in ("dev", "prod"):
        raise ConfigError(f"{entry_path} 的 env 必须明确填写 dev 或 prod")
    suffix = "dev" if env == "dev" else "prods"
    config_path = config_dir / f"config_{suffix}.yaml"
    subs_path = config_dir / f"subscriptions_{suffix}.yaml"
    config = _load_mapping(config_path)
    return env, str(config_path), str(subs_path), config
