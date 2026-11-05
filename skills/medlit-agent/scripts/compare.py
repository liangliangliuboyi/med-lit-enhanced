# -*- coding: utf-8 -*-
"""干预措施的横向对比（多臂对比）。

为什么需要这个模块
------------------
使用者最常问的一句话是「A 药和 B 药，哪个证据更强」。而单臂检索只能给出
各自独立的结论，把两份结论摆到一起做判断，是使用者自己的人工活儿——
既费时，又容易先入为主（先看到的那一臂显得更重要）。

本模块把「横向对比」做成**纯程序化**的：输入若干个干预臂（每臂 = 该干预
检索到的证据 + 合并效应量 + GRADE 定级），输出一张总览表、一张逐条证据
对照表、一段差异说明和一段共同局限。

三条设计约束（改代码前请先读完）
--------------------------------
1. **不调模型**：对比表里的每个数字都来自既有程序（meta / grading）的产出，
   不经过模型改写，因此可复核、可重放。
2. **不出临床建议**：本模块只描述「证据有多强」，不描述「该用哪个药」。
   措辞上刻意回避「推荐」「应当使用」这类临床决策用语，并在句尾声明
   「不等同于临床推荐」——因为检索范围有限、语言范围有限，
   把证据强度直接等同于用药决策是越界的。
3. **表格安全**：所有进入单元格的文本都要把竖线换成斜杠、换行换成空格，
   否则一条含有 `|` 的原始结论就会把整张 Markdown 表打散。

本模块只做展示与措辞，不引入新的统计方法。
"""
from __future__ import annotations

import hashlib
import re

__all__ = [
    'build_comparison', 'compare_summary', 'recommend_wording',
    'comparison_dir_name', 'CERTAINTY_ORDER', 'DIRECT_GAP_THRESHOLD',
]

# GRADE 确定性的排序权重：高 > 中 > 低 > 极低。
# 「未评估」（没有 grading 结果或证据为空）排在所有等级之后，
# 这样排序时"没评过的"永远不会被当成"证据最强"。
CERTAINTY_ORDER = {'高': 4, '中': 3, '低': 2, '极低': 1, '未评估': 0}

# 直接证据占比与其他臂的最大差值达到该阈值（20 个百分点）时，
# 提示该臂结论外推需谨慎。
DIRECT_GAP_THRESHOLD = 0.20

# 共同局限最多列出的条数
_MAX_LIMITATIONS = 5


# ---------------- 通用小工具 ----------------
def _cell(text, limit: int = 0) -> str:
    """把任意文本转成可以安全放进 Markdown 表格单元格的字符串。

    必须处理竖线：一篇文献的原始结论里出现 `A | B`（常见于把两组数据
    并列书写）时，竖线会被 Markdown 解析成新的列，整张表就错位了。
    换行同理——单元格里的换行会把一行拆成两行。

    limit > 0 时按字符截断（不追加省略号，保证长度严格不超过 limit）。
    """
    s = '' if text is None else str(text)
    s = s.replace('\r\n', ' ').replace('\n', ' ').replace('\r', ' ').replace('\t', ' ')
    s = s.replace('|', '/')
    s = re.sub(r'\s+', ' ', s).strip()
    if limit and len(s) > limit:
        s = s[:limit]
    return s


def _fmt_num(value) -> str:
    """数字格式化：保留最多 3 位小数，去掉多余的尾零（1.0 而不是 1.000）。"""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return '' if value is None else str(value)
    s = f'{f:.3f}'
    if '.' in s:
        s = s.rstrip('0')
        if s.endswith('.'):
            s += '0'
    return s


def _arm_label(arm: dict) -> str:
    return str((arm.get('label') or '')).strip() or '未命名干预'


def _arm_evidence(arm: dict) -> list:
    """取该臂的证据列表，非 dict 的脏数据直接跳过。"""
    ev = arm.get('evidence') or []
    if not isinstance(ev, (list, tuple)):
        return []
    return [e for e in ev if isinstance(e, dict)]


def _arm_pooled(arm: dict) -> list:
    """取该臂的合并效应量结果（meta.summarize_effects 的产物）。"""
    pooled = arm.get('pooled') or []
    if not isinstance(pooled, (list, tuple)):
        return []
    return [p for p in pooled if isinstance(p, dict)]


def _arm_grading(arm: dict) -> dict:
    grading = arm.get('grading') or {}
    return grading if isinstance(grading, dict) else {}


def _arm_certainty(arm: dict) -> str:
    """该臂的 GRADE 确定性；缺失或不在已知等级内一律记「未评估」。"""
    c = str(_arm_grading(arm).get('certainty') or '').strip()
    return c if c in CERTAINTY_ORDER and c != '未评估' else '未评估'


def _certainty_rank(certainty: str) -> int:
    return CERTAINTY_ORDER.get(certainty, 0)


def _direct_ratio(evidence: list) -> float:
    """PICO 判定为「直接」的证据占比。"""
    if not evidence:
        return 0.0
    n = sum(1 for e in evidence if str(e.get('pico_match') or '').strip() == '直接')
    return n / len(evidence)


def _design_distribution(evidence: list) -> str:
    """研究设计分布，形如 `RCT 3；队列 2`（按篇数降序）。"""
    counts: dict = {}
    for e in evidence:
        d = str(e.get('study_design') or '').strip() or '未标注'
        counts[d] = counts.get(d, 0) + 1
    if not counts:
        return '—'
    items = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return '；'.join(f'{d} {n}' for d, n in items)


def _pooled_ok(pooled: list) -> bool:
    """是否至少有一条真正合并成功（k > 0）的效应量。"""
    for p in pooled:
        try:
            if int(p.get('k') or 0) > 0:
                return True
        except (TypeError, ValueError):
            continue
    return False


def _pooled_metrics(pooled: list) -> list:
    """合并成功的指标名列表（去重、保持出现顺序）。"""
    out: list = []
    for p in pooled:
        try:
            k = int(p.get('k') or 0)
        except (TypeError, ValueError):
            continue
        if k <= 0:
            continue
        m = str(p.get('metric') or '').strip()
        if m and m not in out:
            out.append(m)
    return out


def _key_effect(pooled: list) -> str:
    """总览表的「关键效应量」列：取第一条合并成功的效应量。

    形如 `RR 0.65 (0.56–0.76)，I²=0%`；没有合并成功的结果写「无法合并」。
    """
    merged = None
    for p in pooled:
        try:
            if int(p.get('k') or 0) > 0:
                merged = p
                break
        except (TypeError, ValueError):
            continue
    if merged is None:
        return '无法合并'
    metric = str(merged.get('metric') or '').strip() or '效应量'
    est = merged.get('pooled')
    lo = merged.get('ci_low')
    hi = merged.get('ci_high')
    if est is None:
        text = metric
    else:
        text = f'{metric} {_fmt_num(est)}'
        if lo is not None and hi is not None:
            text += f' ({_fmt_num(lo)}–{_fmt_num(hi)})'
    i2 = merged.get('i2')
    if i2 is not None:
        try:
            text += f'，I²={float(i2):.0f}%'
        except (TypeError, ValueError):
            pass
    return text


def _arm_rows(arms: list) -> list:
    """把原始臂字典整理成内部统一结构，供三个公开 API 共用。"""
    rows = []
    for arm in arms:
        if not isinstance(arm, dict):
            continue
        evidence = _arm_evidence(arm)
        pooled = _arm_pooled(arm)
        rows.append({
            'label': _arm_label(arm),
            'evidence': evidence,
            'pooled': pooled,
            'grading': _arm_grading(arm),
            'certainty': _arm_certainty(arm),
            'n_evidence': len(evidence),
            'direct_ratio': _direct_ratio(evidence),
            'pooled_ok': _pooled_ok(pooled),
            'pooled_metrics': _pooled_metrics(pooled),
        })
    return rows


def _join_names(names: list) -> str:
    """把一组干预名串成中文习惯的「A、B 与 C」。"""
    names = [n for n in names if n]
    if not names:
        return ''
    if len(names) == 1:
        return names[0]
    return '、'.join(names[:-1]) + ' 与 ' + names[-1]


# ---------------- 差异说明 ----------------
def _difference_notes(rows: list) -> list:
    """生成差异说明条目（纯规则，不调模型）。"""
    notes: list = []
    if not rows:
        # 没有臂可比时不能说"未见显著差异"——那会把"没数据"说成"没差别"
        return notes

    # 1) 确定性差异：取确定性最高与最低的两臂作比较
    ranked = sorted(rows, key=lambda r: (-_certainty_rank(r['certainty']),
                                          -r['n_evidence']))
    top, bottom = ranked[0], ranked[-1]
    if _certainty_rank(top['certainty']) != _certainty_rank(bottom['certainty']):
        # 主要差异来源优先取高确定性一侧记录的降级因素；
        # 高确定性一侧没有降级因素时，改用低确定性一侧的——
        # 因为「为什么低」才是「为什么两者有差距」的直接解释。
        reasons = [str(x) for x in (top['grading'].get('downgrades') or []) if str(x).strip()]
        if not reasons:
            reasons = [str(x) for x in (bottom['grading'].get('downgrades') or []) if str(x).strip()]
        if reasons:
            source = '；'.join(reasons)
        else:
            source = '未记录降级因素（差异可能来自证据体量与研究设计构成不同）'
        notes.append(
            f'{top["label"]} 的证据确定性高于 {bottom["label"]}'
            f'（{top["certainty"]} vs {bottom["certainty"]}），主要差异来源：{source}')

    # 2) 直接证据占比偏低（与其他有证据的臂相比差 ≥20 个百分点）
    with_ev = [r for r in rows if r['n_evidence'] > 0]
    for r in with_ev:
        others = [o['direct_ratio'] for o in with_ev if o is not r]
        if not others:
            continue
        if max(others) - r['direct_ratio'] >= DIRECT_GAP_THRESHOLD:
            notes.append(f'{r["label"]} 的直接证据占比偏低，结论外推需谨慎')

    # 3) 效应量无法合并
    for r in rows:
        if r['pooled_ok']:
            continue
        if r['n_evidence'] == 0:
            # 一条证据都没有时，"效应量不足以合并"并不是真正的原因——
            # 没有研究就谈不上"研究设计或指标口径差异"，那样写会误导读者
            # 去怀疑口径问题，而实际问题是没有数据。因此这里换一套措辞。
            notes.append(f'{r["label"]} 未检索到可用证据，无法参与对比')
        else:
            notes.append(f'{r["label"]} 的效应量不足以合并，可能存在研究设计或指标口径差异')

    return notes


# ---------------- 公开 API ----------------
def build_comparison(arms: list, question: str = '') -> str:
    """生成多臂横向对比的 Markdown 文本。

    arms 每项：
        {'label': 干预名, 'evidence': [证据条目], 'pooled': [合并效应量],
         'grading': {GRADE 结果}, 'query': '英文检索式（可选）'}

    输出结构（Markdown）：
        1. 总览表：干预 / 纳入证据 / 研究设计分布 / GRADE 确定性 /
                   关键效应量 / 直接证据占比
        2. 逐条证据对照表：干预 / 引用标识 / 年份 / 期刊 / 设计 /
                          样本量 / PICO / 效应量 / 核心发现
        3. 差异说明（程序生成，不调模型）
        4. 共同局限（所有臂的 limitations 去重后取前 5 条）
    """
    rows = _arm_rows(arms or [])
    lines: list = []

    lines.append('## 干预措施横向对比')
    lines.append('')
    if question:
        lines.append(f'**对比问题**：{_cell(question)}')
        lines.append('')

    # ---- 表一：总览 ----
    lines.append('### 总览')
    lines.append('')
    lines.append('| 干预 | 纳入证据 | 研究设计分布 | GRADE 确定性 | 关键效应量 | 直接证据占比 |')
    lines.append('|---|---|---|---|---|---|')
    for r in rows:
        pct = int(r['direct_ratio'] * 100 + 0.5)  # 四舍五入，不用 round() 的银行家舍入
        lines.append(
            f'| {_cell(r["label"])} | {r["n_evidence"]} | '
            f'{_cell(_design_distribution(r["evidence"]))} | '
            f'{_cell(r["certainty"])} | {_cell(_key_effect(r["pooled"]))} | '
            f'{pct}% |')
    if not rows:
        lines.append('| （未提供干预臂） | 0 | — | 未评估 | 无法合并 | 0% |')
    lines.append('')

    # ---- 表二：逐条证据对照 ----
    lines.append('### 逐条证据对照')
    lines.append('')
    lines.append('| 干预 | 引用标识 | 年份 | 期刊 | 设计 | 样本量 | PICO | 效应量 | 核心发现 |')
    lines.append('|---|---|---|---|---|---|---|---|---|')
    any_row = False
    for r in rows:
        for e in r['evidence']:
            any_row = True
            key = e.get('_citation_key') or e.get('pmid') or ''
            findings = e.get('key_findings') or []
            if not isinstance(findings, (list, tuple)):
                findings = [findings]
            core = '；'.join(str(f) for f in list(findings)[:2])
            effects = e.get('effect_sizes') or []
            if not isinstance(effects, (list, tuple)):
                effects = [effects]
            lines.append(
                f'| {_cell(r["label"])} | {_cell(key)} | {_cell(e.get("_year"))} | '
                f'{_cell(e.get("_journal"))} | {_cell(e.get("study_design"))} | '
                f'{_cell(e.get("sample_size"))} | {_cell(e.get("pico_match"))} | '
                f'{_cell("；".join(str(x) for x in effects))} | '
                f'{_cell(core, 90)} |')
    if not any_row:
        lines.append('| （无可对照的证据条目） | — | — | — | — | — | — | — | — |')
    lines.append('')

    # ---- 第三段：差异说明 ----
    lines.append('### 差异说明')
    lines.append('')
    if not rows:
        # 没数据 != 没差别，不能用"未见显著差异"把没数据说圆过去
        lines.append('未提供可对比的干预臂。')
    elif sum(r['n_evidence'] for r in rows) == 0:
        # 所有臂都没有证据：这时逐条列"效应量不足以合并"是误导——
        # 压根没有效应量可合，并不是"研究设计或指标口径差异"。
        # 单独用一句话说明，才不会让读者怀疑口径而忽略真正的问题（没数据）。
        # 两个臂说"两臂"、三个以上说"各臂"，措辞与臂数一致。
        word = '两臂' if len(rows) == 2 else '各臂'
        lines.append(f'{word}均未检索到可用证据，无法比较。')
    else:
        notes = _difference_notes(rows)
        if notes:
            for n in notes:
                lines.append(f'- {_cell(n)}')
        else:
            lines.append('各臂在证据数量、确定性与效应量方向上未见显著差异。')
    lines.append('')

    # ---- 共同局限 ----
    lines.append('### 共同局限')
    lines.append('')
    seen = set()
    limits = []
    for r in rows:
        for e in r['evidence']:
            lims = e.get('limitations') or []
            if not isinstance(lims, (list, tuple)):
                lims = [lims]
            for lim in lims:
                text = _cell(lim)
                if not text or text in seen:
                    continue
                seen.add(text)
                limits.append(text)
    if limits:
        for lim in limits[:_MAX_LIMITATIONS]:
            lines.append(f'- {lim}')
    else:
        lines.append('- （未提取到局限描述）')

    return '\n'.join(lines)


def compare_summary(arms: list) -> dict:
    """程序侧的机读对比总结，供 trace / JSON 输出使用。

    返回：
        {
          'arms': [{'label':..., 'n_evidence':int,
                    'certainty':'高/中/低/极低/未评估',
                    'direct_ratio':0.67, 'pooled_metrics':['RR'],
                    'pooled_ok':True}, ...],
          'certainty_ranking': ['A', 'B'],
          'no_pooled': ['某臂'],
          'note': '中文一句话'
        }
    """
    rows = _arm_rows(arms or [])

    per_arm = [{
        'label': r['label'],
        'n_evidence': r['n_evidence'],
        'certainty': r['certainty'],
        'direct_ratio': round(r['direct_ratio'], 2),
        'pooled_metrics': r['pooled_metrics'],
        'pooled_ok': r['pooled_ok'],
    } for r in rows]

    # 排序：确定性从高到低；并列时证据数多的在前；再并列时保持输入顺序
    # （sorted 是稳定排序，因此不需要额外的序号兜底）。
    order = sorted(range(len(rows)),
                   key=lambda i: (-_certainty_rank(rows[i]['certainty']),
                                  -rows[i]['n_evidence']))
    ranking = [per_arm[i]['label'] for i in order]
    no_pooled = [a['label'] for a in per_arm if not a['pooled_ok']]

    if not per_arm:
        note = '未提供可对比的干预臂。'
    elif sum(a['n_evidence'] for a in per_arm) == 0:
        note = '本轮检索未获得可用于对比的证据。'
    else:
        top_rank = max(_certainty_rank(r['certainty']) for r in rows)
        tied = [r for r in rows if _certainty_rank(r['certainty']) == top_rank]
        if len(tied) == 1:
            t = tied[0]
            note = (f'共 {len(rows)} 个干预臂参与对比；{t["label"]} 的证据确定性最高'
                    f'（{t["certainty"]}，纳入 {t["n_evidence"]} 篇）。')
        else:
            note = (f'共 {len(rows)} 个干预臂参与对比；'
                    f'{_join_names([t["label"] for t in tied])} 的证据确定性相当'
                    f'（均为 {tied[0]["certainty"]}），尚不足以判断孰优孰劣。')

    return {
        'arms': per_arm,
        'certainty_ranking': ranking,
        'no_pooled': no_pooled,
        'note': note,
    }


def recommend_wording(arms: list) -> str:
    """生成一句措辞谨慎的对比结论（纯模板拼装，不调模型）。

    为什么措辞必须这么保守
    ----------------------
    1. 本模块只能看到「本轮检索范围内」的证据：检索式、数据库、时间窗、
       语言范围都会系统性地漏掉证据，所以任何"更强/更弱"的表述都只在
       本轮范围内成立。
    2. 证据确定性与临床获益不是一回事：GRADE 高确定性的证据也可能因为
       效应量很小而没有临床意义。
    3. 因此这里刻意不写「推荐」「应当使用」「首选」这类临床决策表述，
       只描述证据强度，并在句尾声明"不等同于临床推荐"，把决策权留给
       具备完整信息的临床使用者。
    """
    rows = _arm_rows(arms or [])
    if not rows:
        return '本轮检索未获得可用于对比的证据。'
    if sum(r['n_evidence'] for r in rows) == 0:
        return '本轮检索未获得可用于对比的证据。'

    top_rank = max(_certainty_rank(r['certainty']) for r in rows)
    tied = [r for r in rows if _certainty_rank(r['certainty']) == top_rank]
    if len(tied) == 1:
        t = tied[0]
        return (f'基于本轮检索，{t["label"]} 的证据确定性最高'
                f'（{t["certainty"]}，纳入 {t["n_evidence"]} 篇）；'
                f'该结论受限于本轮检索范围与语言范围，不等同于临床推荐。')
    names = _join_names([t['label'] for t in tied])
    return (f'本轮检索中 {names} 的证据确定性相当（均为 {tied[0]["certainty"]}），'
            f'现有证据不足以判断孰优孰劣；需结合效应量、适应症人群与安全性综合判断；'
            f'本对比不等同于临床推荐。')


def comparison_dir_name(question: str) -> str:
    """生成对比结果的目录名：短 slug + 4 位 md5 哈希。

    为什么要带哈希：slug 只取前 16 个字符，同一分钟内跑两个开头相同的问题
    （例如同一个课题的两个子问题）会算出同一个 slug 而互相覆盖目录。
    哈希取**完整原文**的 md5 前 4 位，因此不同问题必然落到不同目录，
    同一个问题则始终稳定（便于重跑时定位历史产物）。
    """
    text = str(question or '')
    slug = re.sub(r'[^\w\u4e00-\u9fff]+', '', text)[:16] or 'compare'
    digest = hashlib.md5(text.encode('utf-8')).hexdigest()[:4]
    return f'{slug}_{digest}'
