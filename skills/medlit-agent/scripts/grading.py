# -*- coding: utf-8 -*-
"""证据分级的机械规则部分。

为什么要程序侧定级（B1）
------------------------
GRADE 的**起始等级**由研究设计决定、**降级与升级因素**都是可机械判定的。
如果完全交给模型"拍脑袋"，同一个人两次问可能给出不同等级，且无法复核。
所以这里先用规则算出 `mechanical_certainty`，模型只在它之上做一次复核，
两者的差异会记进审查意见里——这才是可审计的做法。

本模块只做规则，不做判断；所有阈值都写在这里，方便调整。
"""
from __future__ import annotations

import re

# 研究设计 → 起始等级分值（4=高 3=中 2=低 1=极低）
DESIGN_START = {
    'meta-analysis': 4, 'systematic review': 4, 'meta analysis': 4,
    'rct': 4, 'randomized controlled trial': 4, '随机对照试验': 4,
    'cohort': 3, '队列': 3, 'prospective cohort': 3, 'retrospective cohort': 3,
    'case-control': 2, '病例对照': 2,
    'cross-sectional': 2, '横断面': 2, '横断面研究': 2,
    'diagnostic accuracy': 2, '诊断准确性': 2,
    'validation study': 2,
    'qualitative': 2, '质性研究': 2,
    'case series': 1, 'case report': 1, '病例报告': 1,
}

# 偏倚风险：这些设计本身就带有难以消除的偏倚
DESIGN_BIAS_PENALTY = {
    'case-control': 1, '病例对照': 1,
    'cross-sectional': 1, '横断面': 2, '横断面研究': 2,
    'case series': 1, 'case report': 2, '病例报告': 2,
    'qualitative': 1, '质性研究': 1,
}

SCORE_TO_LEVEL = {4: '高', 3: '中', 2: '低', 1: '极低'}
LEVEL_ORDER = {'高': 4, '中': 3, '低': 2, '极低': 1}


def _norm(s: str) -> str:
    return re.sub(r'\s+', ' ', str(s or '').strip().lower())


def design_start_score(study_design: str) -> int:
    """按研究设计取起始等级分值。未知设计按 2（低）处理。"""
    d = _norm(study_design)
    if not d:
        return 2
    if d in DESIGN_START:
        return DESIGN_START[d]
    # 包含匹配（例如 "RCT (double-blind)"）
    for key, val in DESIGN_START.items():
        if key in d:
            return val
    if 'random' in d or '随机' in d:
        return 4
    if 'cohort' in d or '队列' in d:
        return 3
    if 'cross' in d or '横断面' in d:
        return 2
    if 'case' in d or '病例' in d:
        return 2
    if 'meta' in d or 'review' in d or '综述' in d:
        return 4
    return 2


def _bias_gap(evidence: dict) -> list[str]:
    """从 bias_assessment 里找出该降级的偏倚要素。"""
    ba = evidence.get('bias_assessment') or {}
    if not isinstance(ba, dict):
        return []
    gaps = []
    rand = _norm(ba.get('randomization'))
    blind = _norm(ba.get('blindING') or ba.get('blinding'))
    alloc = _norm(ba.get('allocation_concealment'))
    # 明确写了"未提及"不等于有缺陷，但无法排除 → 保守起见只对
    # 明确写出的缺陷降级；"未提及"不降级（避免过度惩罚短摘要）
    if rand and rand not in ('未提及', 'na', 'n/a', '-') and ('未随机' in rand or 'not random' in rand or '非随机' in rand):
        gaps.append('未随机分配')
    if blind and blind not in ('未提及', 'na', 'n/a', '-') and ('未设盲' in blind or '单盲' in blind or 'open label' in blind or '未盲' in blind):
        gaps.append('盲法不足')
    if alloc and alloc not in ('未提及', 'na', 'n/a', '-') and ('未隐藏' in alloc or '不充分' in alloc or 'unclear' in alloc):
        gaps.append('分配隐藏不充分')
    return gaps


def _imprecision(evidence: list[dict], pooled: list[dict]) -> list[str]:
    """不精确性：样本量普遍偏小，或合并后 CI 很宽。"""
    reasons = []
    small = 0
    total = 0
    for e in evidence:
        total += 1
        ss = str(e.get('sample_size') or '')
        m = re.search(r'n\s*=\s*(\d+)', ss, re.I)
        if m:
            try:
                if int(m.group(1)) < 50:
                    small += 1
            except ValueError:
                pass
    if total and small / total > 0.5:
        reasons.append(f'{small}/{total} 篇样本量小于 50')
    for p in pooled or []:
        lo, hi = p.get('ci_low'), p.get('ci_high')
        if lo is None or hi is None:
            continue
        try:
            lo, hi = float(lo), float(hi)
        except (TypeError, ValueError):
            continue
        if lo <= 0 < hi and (hi - lo) / max(abs(hi), 1e-9) > 1.0:
            reasons.append(f'{p.get("metric", "合并效应量")} 置信区间很宽且跨越无效线')
            break
    return reasons


def _inconsistency(evidence: list[dict], pooled: list[dict], critique: dict | None) -> list[str]:
    reasons = []
    for p in pooled or []:
        try:
            i2 = float(p.get('i2') or 0)
        except (TypeError, ValueError):
            i2 = 0
        if i2 >= 50:
            reasons.append(f'{p.get("metric", "合并效应量")} 异质性 I²={i2:.0f}%（>50%，存在实质不一致）')
    if critique:
        cons = _norm(critique.get('consistency'))
        if '冲突' in cons:
            reasons.append('审查环节判定存在证据冲突')
    return reasons


def _indirectness(evidence: list[dict]) -> list[str]:
    n = len(evidence)
    if not n:
        return []
    indirect = sum(1 for e in evidence if _norm(e.get('pico_match')) == '间接')
    if indirect and indirect / n >= 0.5:
        return [f'{indirect}/{n} 篇为间接证据（PICO 关键要素不覆盖）']
    return []


def _publication_bias(evidence: list[dict]) -> list[str]:
    """发表偏倚：程序侧无法自动检测，因此**不作为降级项**，只作为备注。

    理由：GRADE 手册把「发表偏倚未检出或被怀疑」列为降级因素，但前提是证据量足够
    大到能评估（通常 ≥10 篇）。证据少的时候降级是过度惩罚，反而会让明明是高质量
    RCT 的证据被误判成"低确定性"。这里把证据量不足的情况写成备注而非降级。
    """
    return []


def _publication_bias_note(evidence: list[dict]) -> str:
    n = len(evidence)
    if n < 5:
        return f'仅 {n} 篇证据，无法评估发表偏倚（证据量不足，未据此降级）'
    return f'{n} 篇证据，发表偏倚需人工用漏斗图/小样本效应量另行评估（程序未据此降级）'


def _preprint_penalty(evidence: list[dict]) -> list[str]:
    """预印本未经过同行评议，应作为降级因素。"""
    n = sum(1 for e in evidence if e.get('_is_preprint'))
    if not n:
        return []
    return [f'{n}/{len(evidence)} 篇为预印本（未经过同行评议）']


def _upgrade_factors(evidence: list[dict], pooled: list[dict]) -> list[str]:
    up = []
    for p in pooled or []:
        try:
            est = float(p.get('pooled') or 0)
        except (TypeError, ValueError):
            continue
        if est and (est < 0.5 or est > 2.0):
            up.append(f'{p.get("metric", "合并效应量")} 效应量大（{est:.2f}）')
            break
    for e in evidence:
        lim = ' '.join(e.get('limitations') or []).lower()
        if '剂量' in lim and '反应' in lim:
            up.append('存在剂量反应关系')
            break
    return up


def mechanical_grading(evidence: list[dict], pooled: list[dict] | None = None,
                       critique: dict | None = None) -> dict:
    """按 GRADE 规则算出证据确定性的机械结果。

    返回：
      {
        'certainty': '高/中/低/极低',
        'score': 4..1,
        'start_score': int,
        'designs': [...],
        'downgrades': [...],
        'upgrades': [...],
        'reason': '中文说明',
        'note': '说明这是机械定级，最终等级由审查环节复核'
      }
    """
    evidence = evidence or []
    pooled = pooled or []
    if not evidence:
        return {'certainty': '极低', 'score': 1, 'start_score': 1, 'designs': [],
                'downgrades': [], 'upgrades': [],
                'reason': '本轮未获得可用的结构化证据', 'note': '机械定级'}

    designs = [(e.get('study_design') or '未标注') for e in evidence]
    start = max(design_start_score(d) for d in designs)
    # 观察性研究为主时起始等级封顶为「中」，除非有系统综述/Meta 参与
    if start == 4 and not any(
            any(k in _norm(d) for k in ('meta', 'systematic', 'review', '综述', 'rct', '随机'))
            for d in designs):
        start = 3

    downgrades: list[str] = []
    # 1) 偏倚风险
    bias_terms: list[str] = []
    for e in evidence:
        bias_terms.extend(_bias_gap(e))
    for e in evidence:
        pen = DESIGN_BIAS_PENALTY.get(_norm(e.get('study_design')), 0)
        if pen:
            bias_terms.append(f'研究设计固有偏倚（{e.get("study_design")}）')
    if bias_terms:
        downgrades.append('偏倚风险：' + '；'.join(sorted(set(bias_terms))[:4]))

    # 2) 不一致性
    inc = _inconsistency(evidence, pooled, critique)
    if inc:
        downgrades.append('不一致性：' + '；'.join(inc))

    # 3) 不精确性
    imp = _imprecision(evidence, pooled)
    if imp:
        downgrades.append('不精确性：' + '；'.join(imp))

    # 4) 间接性
    ind = _indirectness(evidence)
    if ind:
        downgrades.append('间接性：' + '；'.join(ind))

    # 5) 发表偏倚（只记备注，不降级——见 _publication_bias 的说明）
    pb_note = _publication_bias_note(evidence)

    # 6) 预印本未同行评议
    pp = _preprint_penalty(evidence)
    if pp:
        downgrades.append('间接性：' + '；'.join(pp))

    upgrades = _upgrade_factors(evidence, pooled)

    score = start - len(downgrades) + len(upgrades)
    score = max(1, min(4, score))
    level = SCORE_TO_LEVEL[score]

    reason = (f'研究设计起始等级：{SCORE_TO_LEVEL.get(start, "低")}；'
              f'降级因素 {len(downgrades)} 项'
              + (f'（{"；".join(downgrades)}）' if downgrades else '（无）')
              + (f'；升级因素 {len(upgrades)} 项（{"；".join(upgrades)}）' if upgrades else '')
              + f' → {level}。{pb_note}')
    return {'certainty': level, 'score': score, 'start_score': start,
            'designs': designs, 'downgrades': downgrades, 'upgrades': upgrades,
            'notes': [pb_note],
            'reason': reason, 'note': '程序侧机械定级，供审查环节复核'}


def retraction_notes(papers: list[dict]) -> list[str]:
    """汇总撤稿/勘误提示，供成文阶段插入。"""
    from pubmed import check_retraction
    notes = []
    for p in papers or []:
        chk = check_retraction(p)
        if chk['note']:
            notes.append(f'PMID {p.get("pmid")}：{chk["note"]}')
    return notes


def excluded_reason_table(excluded: list[dict]) -> list[str]:
    """把排除理由汇总成可读列表（C6）。"""
    reasons: dict[str, int] = {}
    for e in excluded or []:
        r = e.get('reason') or '未说明'
        reasons[r] = reasons.get(r, 0) + 1
    return [f'{r}：{n} 篇' for r, n in sorted(reasons.items(), key=lambda kv: -kv[1])]
