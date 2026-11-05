# -*- coding: utf-8 -*-
"""配置加载与密钥解析。

开源版的三条硬规矩（改动本文件前请先读完）：

1. **仓库里不存任何密钥。** 密钥只从环境变量（或未纳入版本控制的 `.env`）读取。
2. `config.example.json` 是入库的模板；`config.json` 与 `config.local.json` 被 .gitignore 排除，
   用来放你自己的非敏感覆盖项。
3. 配置里只声明 **"这个 key 该去哪读"**（`api_key_env`），而不是把 key 本身写进去。

加载优先级（后者覆盖前者）：
    config.example.json  →  config.json  →  config.local.json  →  环境变量 / .env
"""
from __future__ import annotations

import json
import os
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

EXAMPLE_CONFIG = ROOT / 'config.example.json'
LOCAL_CONFIG = ROOT / 'config.json'
OVERRIDE_CONFIG = ROOT / 'config.local.json'
ENV_FILE = ROOT / '.env'


# ---------------- .env 极简解析（零依赖） ----------------
def parse_env_file(path) -> dict:
    """解析 `KEY=VALUE`，忽略空行与 # 注释，去掉首尾引号。"""
    env: dict[str, str] = {}
    try:
        text = Path(path).read_text(encoding='utf-8')
    except Exception:
        return env
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        k, _, v = line.partition('=')
        k = k.strip()
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in ('"', "'"):
            v = v[1:-1]
        if k:
            env[k] = v
    return env


def load_dotenv(path=ENV_FILE) -> dict:
    """把 .env 中的键补进 os.environ（已存在的环境变量优先，不被覆盖）。"""
    for k, v in parse_env_file(path).items():
        os.environ.setdefault(k, v)
    return dict(os.environ)


# ---------------- 配置合并 ----------------
def _read_json(path) -> dict:
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f'配置文件解析失败 {path}: {e}') from e


def deep_merge(base: dict, override: dict) -> dict:
    """递归合并，override 中的标量/列表整体替换。"""
    out = dict(base or {})
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


# ---------------- 密钥解析 ----------------
class MissingSecretError(RuntimeError):
    """缺少必需的密钥。"""


def resolve_secret(block: dict, dotenv: dict, label: str,
                   required: bool = True) -> str:
    """按 `api_key_env` → 环境变量 → `.env` → 字面量 `api_key` 的顺序取密钥。

    `api_key` 字面量是**给本地 config.local.json 用的兜底**，模板文件里不应出现。
    """
    env_name = (block.get('api_key_env') or '').strip()
    if env_name:
        val = (os.environ.get(env_name) or dotenv.get(env_name) or '').strip()
        if val:
            return val
    val = (block.get('api_key') or '').strip()
    if val:
        return val
    if required:
        raise MissingSecretError(
            f'缺少 {label} 的密钥。\n'
            f'  请设置环境变量 {env_name or "(未声明 api_key_env)"}，'
            f'或复制 .env.example 为 .env 后填写。\n'
            f'  详见 README「配置」一节。')
    return ''


# ---------------- 主入口 ----------------
def load_config(path: str | None = None, *, require_secrets: bool = True) -> dict:
    """加载并合并配置，解析密钥。

    参数
    ----
    path: 显式指定主配置文件；默认 config.json 优先，回落 config.example.json。
    require_secrets: False 时缺少模型密钥不报错（供 `--models` 等离线检查用）。
    """
    dotenv = load_dotenv()

    if path:
        cfg = _read_json(path)
    else:
        cfg = _read_json(LOCAL_CONFIG) or _read_json(EXAMPLE_CONFIG)

    override = _read_json(OVERRIDE_CONFIG)
    if override:
        cfg = deep_merge(cfg, override)

    prov = cfg.setdefault('provider', {})
    prov['api_key'] = resolve_secret(prov, dotenv, '模型服务(provider)',
                                     required=require_secrets)

    rr = prov.setdefault('rerank', {})
    rr['enabled'] = bool(rr.get('enabled'))
    rr['api_key'] = resolve_secret(rr, dotenv, '重排模型(rerank)', required=False)
    if rr['enabled'] and not rr['api_key']:
        # 没给独立的 rerank key 就复用 provider 的 key
        rr['api_key'] = prov.get('api_key', '')

    emb = prov.setdefault('embed', {})
    emb['enabled'] = bool(emb.get('enabled'))
    emb['api_key'] = resolve_secret(emb, dotenv, '嵌入模型(embed)', required=False)
    if emb['enabled'] and not emb['api_key']:
        emb['api_key'] = prov.get('api_key', '')

    pm = cfg.setdefault('pubmed', {})
    pm['api_key'] = resolve_secret(pm, dotenv, 'NCBI(PubMed)', required=False)
    pm.setdefault('tool', 'medlit-agent')
    pm.setdefault('email', '')

    cfg['_paths'] = {
        'root': str(ROOT),
        'data': str(ROOT / 'data'),
        'presets': str(ROOT / 'presets'),
        'examples': str(ROOT / 'examples'),
    }
    return cfg


def describe_sources(cfg: dict) -> str:
    """返回一行人类可读的"配置来自哪里"，用于日志与 --models 输出。"""
    bits = []
    if LOCAL_CONFIG.exists():
        bits.append('config.json')
    if OVERRIDE_CONFIG.exists():
        bits.append('config.local.json')
    if ENV_FILE.exists():
        bits.append('.env')
    prov = cfg.get('provider', {})
    if prov.get('api_key_env'):
        bits.append(f"env:{prov['api_key_env']}")
    return ' + '.join(bits) if bits else '(全部使用默认值)'
