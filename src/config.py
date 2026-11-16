"""从项目根目录 .env 选择运行模式，不依赖进程环境变量。"""
from pathlib import Path
import shlex

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


def _load_env(path):
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeError) as exc:
        raise ConfigError(f"无法读取 {path}：{exc}") from exc
    env = None
    for number, line in enumerate(lines, 1):
        line = line.strip()
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, value = line.partition("=")
        if key.strip() != "ELECFEE_ENV":
            continue
        if env is not None:
            raise ConfigError(f"{path}:{number} 重复定义 ELECFEE_ENV")
        try:
            values = shlex.split(value, comments=True)
        except ValueError as exc:
            raise ConfigError(f"{path}:{number} 的 ELECFEE_ENV 格式错误") from exc
        if not separator or len(values) != 1 or values[0] not in ("dev", "prod"):
            raise ConfigError(f"{path}:{number} 的 ELECFEE_ENV 必须填写 dev 或 prod")
        env = values[0]
    if env is None:
        raise ConfigError(f"{path} 必须明确填写 ELECFEE_ENV=dev 或 ELECFEE_ENV=prod")
    return env


def load_config(config_dir):
    """先读取根目录 .env，再按模式选择业务配置和订阅文件。"""
    config_dir = Path(config_dir).resolve()
    env = _load_env(config_dir.parent / ".env")
    config_path = config_dir / f"config_{env}.yaml"
    subs_path = config_dir / f"subscriptions_{env}.yaml"
    config = _load_mapping(config_path)
    return env, str(config_path), str(subs_path), config
