# -*- coding: utf-8 -*-
"""学科域预设：识别、提示词注入、检索参数调整。

设计意图
--------
做成**通用底座 + 可插拔域预设**：

- 通用底座负责「怎么检索、怎么读、怎么写」；
- 域预设负责「这个领域该查哪些 MeSH、优先什么研究设计、用什么 Clinical Query 过滤器」；
- 用户把自己的 JSON 丢进 `presets/` 目录即可新增或覆盖内置预设，**不需要改代码**。

自动识别靠关键词命中打分；也可用 `--preset <id>` 显式指定，`--no-preset` 关闭。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
DATA_DIR = ROOT / 'data'
PRESETS_DIR = ROOT / 'presets'

# 命中多少分才算识别成功（关键词命中一次记 1 分）
DETECT_THRESHOLD = 2


def _read_json(path) -> dict:
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return {}


def load_presets() -> dict:
    """内置预设 + 用户预设（用户目录同 id 覆盖内置）。"""
    presets = {k: v for k, v in _read_json(DATA_DIR / 'presets.json').items()
               if not k.startswith('_')}
    if PRESETS_DIR.exists():
        for p in sorted(PRESETS_DIR.glob('*.json')):
            data = _read_json(p)
            if not data:
                continue
            # 单文件可写 {"id": {...}} 或直接 {...}（以含 id 字段为准）
            if 'id' in data and isinstance(data.get('id'), str):
                pid = data.pop('id')
                data.setdefault('name', pid)
                presets[pid] = data
            else:
                for pid, body in data.items():
                    if not pid.startswith('_') and isinstance(body, dict):
                        presets[pid] = body
    return presets


def list_presets() -> list[dict]:
    """给 CLI 展示用的预设清单。"""
    out = []
    for pid, p in load_presets().items():
        out.append({
            'id': pid,
            'name': p.get('name', pid),
            'description': p.get('description', ''),
            'keywords': len(p.get('keywords') or []),
            'journal_profile': p.get('journal_profile', 'clinical'),
            'cq': p.get('cq') or [],
        })
    return sorted(out, key=lambda x: x['id'])


def detect(question: str, constraints: str = '') -> tuple[str | None, int, list[str]]:
    """按关键词命中打分识别预设。

    返回 (预设 id, 命中分数, 命中的关键词)。分数不足返回 (None, 分数, 命中词)。
    """
    text = f'{question} {constraints}'.lower()
    best_id, best_score, best_hits = None, 0, []
    for pid, p in load_presets().items():
        hits = []
        for kw in (p.get('keywords') or []):
            k = str(kw).lower()
            if k and k in text:
                hits.append(kw)
        if len(hits) > best_score:
            best_id, best_score, best_hits = pid, len(hits), hits
    if best_score < DETECT_THRESHOLD:
        return None, best_score, best_hits
    return best_id, best_score, best_hits


def get(preset_id: str | None) -> dict:
    if not preset_id:
        return {}
    return load_presets().get(preset_id, {})


def build_hint(preset: dict) -> str:
    """把预设转成一段给 planner 看的中文提示。"""
    if not preset:
        return ''
    lines = [f'学科域：{preset.get("name", "")}']
    if preset.get('description'):
        lines.append(f'领域范围：{preset["description"]}')
    if preset.get('mesh_boost'):
        lines.append('建议纳入的 MeSH 主题词（可按需取舍，不要全部堆上）：')
        lines.append('  ' + '; '.join(preset['mesh_boost']))
    if preset.get('design_priority'):
        lines.append('优先关注的研究设计：' + '、'.join(preset['design_priority']))
    if preset.get('notes'):
        lines.append(f'领域要点：{preset["notes"]}')
    return '\n'.join(lines)


def apply_to_config(cfg: dict, preset: dict) -> None:
    """把预设的年份下限与期刊画像写入检索配置。"""
    if not preset:
        return
    ret = cfg.setdefault('retrieval', {})
    floor = preset.get('year_floor')
    if floor:
        cur = int(ret.get('min_year') or 0)
        ret['min_year'] = max(cur, int(floor)) if cur else int(floor)
    profile = preset.get('journal_profile')
    if profile:
        j = ret.setdefault('journals', {})
        j['profile'] = profile


def resolve(question: str, constraints: str = '', explicit: str | None = None,
            disabled: bool = False) -> tuple[str | None, dict, list[str]]:
    """综合解析最终使用的预设。

    返回 (预设 id, 预设内容, 命中关键词列表)。
    """
    if disabled:
        return None, {}, []
    if explicit:
        p = get(explicit)
        if not p:
            raise KeyError(f'未知预设 "{explicit}"，用 --list-presets 查看可用的')
        return explicit, p, []
    pid, _score, hits = detect(question, constraints)
    return pid, get(pid), hits


def load_journal_profile(profile: str, mode: str = 'ta') -> list[str]:
    """读取期刊画像，返回该 mode 下的刊名列表。"""
    data = _read_json(DATA_DIR / 'journal_profiles.json')
    prof = data.get(profile) or {}
    names = prof.get(mode) or prof.get('ta') or []
    return [n for n in names if n]


def load_clinical_queries() -> dict:
    return {k: v for k, v in _read_json(DATA_DIR / 'clinical_queries.json').items()
            if not k.startswith('_')}
