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
    """缺少必需的密钥。消息要能直接当行动清单给用户看。"""


# 缺什么 → 怎么补。这是用户看到的第一句话，必须可执行。
FIX_HINTS = {
    'provider': (
        '缺少模型服务密钥。任选一种方式补上：\n'
        '   1) 在技能目录下的 .env 里写 SILICONFLOW_API_KEY=<你的密钥>\n'
        '      （.env.example 是模板，复制一份改名即可）\n'
        '   2) 或设置系统环境变量 SILICONFLOW_API_KEY\n'
        '   3) 或运行 `python scripts/ask.py --setup` 由向导代填\n'
        '   密钥获取：硅基流动 https://cloud.siliconflow.cn/ → 账户 → API Key\n'
        '   （也支持 OpenAI / DeepSeek / Kimi / 本地 Ollama 等任意 OpenAI 兼容端点，'
        '改 config.json 的 provider 段即可）'),
    'pubmed': (
        '缺少 NCBI API key（可选项，但建议补）：\n'
        '   在 .env 里写 NCBI_API_KEY=<你的密钥>，申请地址\n'
        '   https://www.ncbi.nlm.nih.gov/account/settings/ → API Key Management\n'
        '   有了它 NCBI 限速从 3 请求/秒 提升到 10 请求/秒。\n'
        '   不填也能用，只是慢一些。'),
    'email': (
        '建议填写 NCBI 开发者邮箱（可选项）：\n'
        '   在 .env 里写 NCBI_EMAIL=<你的邮箱>。\n'
        '   NCBI 规定 email 填「软件开发者」的邮箱，不是终端使用者的邮箱——'
        '   万一这个软件被违规使用并导致 IP 被封，NCBI 需要能联系到作者。\n'
        '   填了之后使用者也可以在 config.json 的 pubmed.email 里改成自己的。'),
}


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
            f'{FIX_HINTS.get(label.split("(")[0].strip(), "缺少必要的配置项：")}\n'
            f'  （配置项：{label}，期望的环境变量名：{env_name or "未声明 api_key_env"}）')
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


# ---------------- 环境体检与配置向导 ----------------
def doctor() -> list[dict]:
    """检查当前环境缺什么、怎么补。返回 [{'item', 'ok', 'detail'}]。

    这是「主动提醒用户少了什么」的入口：技能每次运行前可以先调用它。
    """
    items: list[dict] = []
    dotenv = load_dotenv()
    cfg = load_config(require_secrets=False)

    prov = cfg.get('provider') or {}
    prov_env = prov.get('api_key_env') or 'SILICONFLOW_API_KEY'
    has_key = bool((os.environ.get(prov_env) or dotenv.get(prov_env) or '').strip())
    items.append({
        'item': '模型服务密钥',
        'ok': has_key,
        'detail': ('已配置（' + prov_env + '）' if has_key else FIX_HINTS['provider']),
    })

    has_pm = bool((os.environ.get('NCBI_API_KEY') or dotenv.get('NCBI_API_KEY') or '').strip())
    items.append({
        'item': 'NCBI API key（选填）',
        'ok': has_pm,
        'detail': ('已配置，限速 10 请求/秒' if has_pm
                   else '未配置，限速 3 请求/秒（功能正常，只是稍慢）'),
    })

    has_mail = bool((cfg.get('pubmed') or {}).get('email'))
    items.append({
        'item': 'NCBI 开发者邮箱（建议）',
        'ok': has_mail,
        'detail': ('已配置' if has_mail else FIX_HINTS['email']),
    })

    items.append({'item': '配置文件', 'ok': True, 'detail': describe_sources(cfg)})

    import sys
    v = sys.version_info
    items.append({'item': 'Python 版本', 'ok': v >= (3, 9),
                  'detail': str(v.major) + '.' + str(v.minor) + '.' + str(v.micro) + '（需要 ≥ 3.9）'})
    items.append({'item': '第三方依赖', 'ok': True,
                  'detail': '无（纯标准库，可直接运行）'})
    return items


def save_env(values: dict, path=ENV_FILE) -> str:
    """把键值写进 .env（已存在的键会被覆盖，其余保留）。"""
    p = Path(path)
    existing: dict[str, str] = {}
    order: list[str] = []
    if p.exists():
        for line in p.read_text(encoding='utf-8').splitlines():
            s = line.strip()
            if not s or s.startswith('#') or '=' not in s:
                continue
            k, _, v = s.partition('=')
            k = k.strip()
            if k:
                existing[k] = v.strip()
                order.append(k)
    for k, v in values.items():
        if k not in existing:
            order.append(k)
        existing[k] = v
    lines = ['# 由 --setup 向导生成，可手工编辑']
    for k in order:
        lines.append(k + '=' + existing.get(k, ''))
    p.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    return str(p)
