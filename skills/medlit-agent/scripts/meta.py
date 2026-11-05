# -*- coding: utf-8 -*-
"""效应量解析与合并（meta-analysis 的最小可用实现）。零第三方依赖。

为什么需要这个模块：
逐篇抽取阶段 reader 模型会把每篇文献的效应量写成自由文本列表，例如
    ['RR 1.24 (95%CI 1.10-1.40)', 'HR 0.78 (95%CI 0.65-0.93)']
这些字符串没法直接做定量综合。本模块把它们解析成结构化数据，
再按指标族做逆方差加权合并，输出合并点估计、置信区间与异质性指标（Q/I²/τ²）。

四条踩坑经验（改代码前先读）：
1. **对数尺度只用于比率型指标**（RR/HR/IRR/OR）。对 RR 取 log 之后，
   SE = (log(hi)-log(lo))/(2*1.96)；对 MD/SMD 必须用原尺度，
   SE = (hi-lo)/(2*1.96)。搞混了会让合并结果完全失真。
2. **CI 上限可能小于下限**（模型抽取时经常发生）。必须纠正而不是直接算，
   否则 SE 变成负数、权重变成负数，Q 检验结果毫无意义。
3. **SE 可能为 0**（lo == hi）。此时权重是无穷大，必须丢弃该条而不是除零。
4. **OR 不能与 RR/HR 混池**。RR 可折叠（collapsible）而 OR 不可，
   两者混算出来的合并值没有意义，所以 OR 自成一族。
   同理 MD（原始单位）与 SMD（标准差单位）单位不同，也不混池。

指标别名映射：risk ratio/relative risk→RR，hazard ratio→HR，odds ratio→OR，
incidence rate ratio→IRR，mean difference→MD，standardized mean difference→SMD。
"""
from __future__ import annotations

import math
import re

__all__ = [
    'parse_effect_size', 'pool_effect_sizes', 'summarize_effects',
    'chi2_sf', 'METRIC_ALIASES', 'RATIO_METRICS', 'DIFF_METRICS',
    'pool_group', 'POOL_GROUPS',
]

# 95% 置信区间对应的正态分位数
Z95 = 1.959963984540054

# 指标别名 → 标准指标名
METRIC_ALIASES: dict[str, str] = {
    'risk ratio': 'RR',
    'relative risk': 'RR',
    'hazard ratio': 'HR',
    'odds ratio': 'OR',
    'incidence rate ratio': 'IRR',
    'mean difference': 'MD',
    'standardized mean difference': 'SMD',
    'standardised mean difference': 'SMD',
}

# 缩写形式（需带词边界，避免 "OR" 命中 "order"、"MD" 命中 "SMD" 的残片）
_ABBREV: dict[str, str] = {'RR': 'RR', 'HR': 'HR', 'OR': 'OR',
                            'IRR': 'IRR', 'MD': 'MD', 'SMD': 'SMD'}

# 比率型（走对数变换）与差值型（走原尺度）
RATIO_METRICS = ('RR', 'HR', 'IRR', 'OR')
DIFF_METRICS = ('MD', 'SMD')

# 可合并的指标族。RR/HR/IRR 同族可合并（都是 log 尺度的率比，方向可比）；
# OR 自成一族；MD 与 SMD 单位不同（原始单位 vs 标准差单位），也各自成族。
POOL_GROUPS: dict[str, tuple[str, ...]] = {
    'RATIO': ('RR', 'HR', 'IRR'),
    'OR': ('OR',),
    'MD': ('MD',),
    'SMD': ('SMD',),
}
_GROUP_ORDER = ('RATIO', 'OR', 'MD', 'SMD')

# 中文别名，reader 模型偶尔会输出中文指标名
_ZH_ALIASES: dict[str, str] = {
    '风险比': 'RR',
    '相对危险度': 'RR',
    '相对风险': 'RR',
    '危险比': 'HR',
    '优势比': 'OR',
    '比值比': 'OR',
    '发病比': 'IRR',
    '发病率比': 'IRR',
    '均数差': 'MD',
    '平均差': 'MD',
    '标准化均数差': 'SMD',
}

# 数字：允许负号、小数、指数
_NUM = r'[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?'
# CI 分隔符：减号 / 连字符变体 / 波浪号 / 至 / 到 / to
# 必须用非捕获组包起来：否则 "| to | 至" 会把整个正则切成"或"分支，
# 导致只有第一个分支同时绑定 lo 和 hi。
_DASH = r'(?:[-‐‑‒–—―~～]|to|至|到)'

# 匹配 "95%CI 1.10-1.40" / "95% CI: 1.10 to 1.40" / "95% confidence interval ..."
_CI_RE = re.compile(
    r'95\s*%\s*(?:CI|confidence\s+interval)?\s*[:：]?\s*'
    r'(?P<lo>' + _NUM + r')\s*' + _DASH + r'\s*(?P<hi>' + _NUM + r')',
    re.IGNORECASE,
)

# 指标名匹配表。长别名排在前面，但真正决定结果的是"最靠前出现"，
# 所以 "standardized mean difference" 不会被 "mean difference" 抢走。
_METRIC_RES: list[tuple[re.Pattern, str]] = []
for _zh, _std in sorted(_ZH_ALIASES.items(), key=lambda kv: -len(kv[0])):
    _METRIC_RES.append((re.compile(re.escape(_zh), re.IGNORECASE), _std))
for _en, _std in sorted(METRIC_ALIASES.items(), key=lambda kv: -len(kv[0])):
    _METRIC_RES.append((re.compile(r'(?<![A-Za-z])' + re.escape(_en) + r'(?![A-Za-z])',
                                   re.IGNORECASE), _std))
# 缩写大小写不敏感（reader 可能输出 "rr 1.24"），靠词边界避免误命中
for _ab, _std in sorted(_ABBREV.items(), key=lambda kv: -len(kv[0])):
    _METRIC_RES.append((re.compile(r'(?<![A-Za-z])' + _ab + r'(?![A-Za-z])',
                                   re.IGNORECASE), _std))

# 形如 "3 or 4" 的英文连词场景：把 "or" 当成 OR 会造出假效应量。
# 规则：若 "or" 前面紧邻的是数字（忽略空格），判定为英文连词而非指标。
_OR_CONJ_RE = re.compile(r'\d\s+or$', re.IGNORECASE)


def pool_group(metric: str) -> str:
    """返回指标所属的可合并族名；未知指标返回空串。"""
    m = (metric or '').upper()
    for group, members in POOL_GROUPS.items():
        if m in members:
            return group
    return ''


def _metric_of(text: str) -> tuple[str, int] | None:
    """在文本中找指标名。返回 (指标, 指标名结束位置)；找不到返回 None。

    多个别名可能指向同一指标（如 "relative risk" 与 "risk ratio" 都是 RR），
    取最靠前出现的那个，保证同一文本的解析结果稳定。
    """
    best: tuple[str, int, int] | None = None  # (指标, 起始位置, 结束位置)
    for rx, std in _METRIC_RES:
        for m in rx.finditer(text):
            if std == 'OR' and _OR_CONJ_RE.search(text[:m.start()]):
                continue  # 英文连词，不是 OR
            if best is None or m.start() < best[1]:
                best = (std, m.start(), m.end())
            break
    if best is None:
        return None
    return best[0], best[2]


def parse_effect_size(text: str) -> dict | None:
    """把一条自由文本效应量解析成结构化 dict；解析不出来返回 None。

    返回字段：
        metric    指标类型：RR/HR/OR/IRR/MD/SMD
        estimate  点估计值
        ci_low    CI 下限（无 CI 则为 None）
        ci_high   CI 上限（无 CI 则为 None）
        has_ci    是否带 CI
        valid     是否可用于合并（比率型要求点估计为正；CI 必须自洽）
        text      原始文本

    例子：
        'RR 1.24 (95%CI 1.10-1.40)'            → RR 1.24 [1.10, 1.40]
        'RR 1.24 (95% CI 1.10–1.40)'           → RR 1.24 [1.10, 1.40]
        'hazard ratio 0.78 (95%CI 0.65-0.93)'  → HR 0.78 [0.65, 0.93]
        'odds ratio 2.10 (95%CI 1.50-2.90)'    → OR 2.10 [1.50, 2.90]
        'mean difference 3.2 (95%CI 1.1-5.3)'  → MD 3.2 [1.1, 5.3]
        'RR 1.24'                               → RR 1.24，ci_low/ci_high 为 None
        '完全无关的文本'                        → None
    """
    if not isinstance(text, str):
        return None
    s = text.strip()
    if not s:
        return None

    found = _metric_of(s)
    if not found:
        return None
    metric, after = found

    # 点估计：取指标名之后的第一个数字
    m_est = re.search(_NUM, s[after:])
    if not m_est:
        return None
    try:
        estimate = float(m_est.group(0))
    except (TypeError, ValueError):
        return None

    # CI：在点估计之后的剩余文本里找（避免把点估计自身误当作 CI 端点）
    ci_low = ci_high = None
    rest = s[after + m_est.end():]
    m_ci = _CI_RE.search(rest)
    if m_ci:
        try:
            lo = float(m_ci.group('lo'))
            hi = float(m_ci.group('hi'))
        except (TypeError, ValueError):
            lo = hi = None
        if lo is not None and hi is not None:
            # CI 上限小于下限是模型抽取常见错误，必须纠正
            if lo > hi:
                lo, hi = hi, lo
            ci_low, ci_high = lo, hi

    has_ci = ci_low is not None and ci_high is not None

    # 有效性：比率型取 log 的前提是点估计 > 0；差值型（MD/SMD）允许负值，
    # 因为负的均数差是完全合法的方向。CI 宽度为 0 则该条无法参与加权。
    valid = True
    if metric in RATIO_METRICS and estimate <= 0:
        valid = False
    if has_ci and not (ci_high > ci_low):
        valid = False

    return {
        'metric': metric,
        'estimate': estimate,
        'ci_low': ci_low,
        'ci_high': ci_high,
        'has_ci': has_ci,
        'valid': valid,
        'text': text.strip(),
    }


def chi2_sf(x: float, df: int) -> float:
    """卡方分布的生存函数 P(X > x)，即 Q 检验的 p 值。

    没有 scipy，用正则化不完全伽马函数 Q(df/2, x/2) 近似。
    df <= 0 时返回 1.0（无法判定）。
    """
    try:
        x = float(x)
        df = int(df)
    except (TypeError, ValueError):
        return 1.0
    if df <= 0 or x <= 0 or not math.isfinite(x):
        return 1.0
    a = df / 2.0
    xx = x / 2.0
    try:
        if xx < a + 1.0:
            # P(a,x) 级数展开 → Q = 1 - P
            ap = a
            s = 1.0 / a
            delta = s
            for _ in range(1000):
                ap += 1.0
                delta *= xx / ap
                s += delta
                if abs(delta) < abs(s) * 1e-15:
                    break
            p = s * math.exp(-xx + a * math.log(xx) - math.lgamma(a))
            return max(0.0, min(1.0, 1.0 - p))
        # Q(a,x) 连分式（Lentz 算法）
        b = xx + 1.0 - a
        c = 1.0 / 1e-300
        d = 1.0 / b
        h = d
        for i in range(1, 1000):
            an = -i * (i - a)
            b += 2.0
            d = an * d + b
            if abs(d) < 1e-300:
                d = 1e-300
            c = b + an / c
            if abs(c) < 1e-300:
                c = 1e-300
            d = 1.0 / d
            delta = d * c
            h *= delta
            if abs(delta - 1.0) < 1e-15:
                break
        q = math.exp(-xx + a * math.log(xx) - math.lgamma(a)) * h
        return max(0.0, min(1.0, q))
    except (ValueError, OverflowError, ZeroDivisionError):
        return 1.0


def _yi(ratio: bool, est: float) -> float:
    """合并用的变换尺度：比率型取 log，差值型取原值。"""
    return math.log(est) if ratio else est


def _se_from_ci(ratio: bool, lo: float, hi: float) -> float:
    """由 95%CI 反推标准误。返回 nan 表示无法计算。"""
    if ratio:
        if lo <= 0 or hi <= 0:
            return float('nan')
        return (math.log(hi) - math.log(lo)) / (2.0 * Z95)
    return (hi - lo) / (2.0 * Z95)


def pool_effect_sizes(items: list[dict]) -> dict:
    """把同族、方向可比的效应量做逆方差加权合并。

    参数
        items  parse_effect_size 的结果列表（可混有不同指标，会自动分族）

    返回
        含 metric / k / pooled / ci_low / ci_high / se / i2 / q / p_value /
        tau2 / df / model / heterogeneity / skipped_no_ci / note 的 dict。

    固定效应：y_bar = Σ(w·y)/Σ(w)，w = 1/SE²
    随机效应（DerSimonian-Laird）：τ² = max(0, (Q-df)/(Σw - Σw²/Σw))，w* = 1/(SE²+τ²)

    缺 95%CI、标准误为 0 或不合法的条目进 skipped_no_ci，不参与合并但要报告。
    k < 2 时不合并，返回 k=0。

    注意：se 是"合并所用尺度"上的标准误——比率型指标为 log 尺度，差值型为原尺度。
    """
    if not items:
        return {'metric': '', 'k': 0, 'note': '没有可合并的效应量'}

    # ---- 分族：OR 不与 RR/HR/IRR 混；MD 与 SMD 单位不同也不混 ----
    buckets: dict[str, list[dict]] = {}
    for it in items:
        if not isinstance(it, dict):
            continue
        metric = str(it.get('metric') or '').upper()
        group = pool_group(metric)
        if not group:
            continue
        buckets.setdefault(group, []).append(it)

    if not buckets:
        return {'metric': '', 'k': 0, 'note': '没有可合并的效应量'}

    def _usable_count(group: str) -> int:
        n = 0
        for it in buckets[group]:
            if not it.get('valid', True):
                continue
            if it.get('estimate') is None or it.get('ci_low') is None \
                    or it.get('ci_high') is None:
                continue
            n += 1
        return n

    # 选主族：取可用条目最多的族；并列时按 RATIO→OR→MD→SMD 的固定顺序。
    group = max(_GROUP_ORDER,
                key=lambda g: (_usable_count(g) if g in buckets else -1,
                               -_GROUP_ORDER.index(g)))
    pool = buckets.get(group, [])
    if not pool:
        return {'metric': '', 'k': 0, 'note': '没有可合并的效应量'}

    # 主族里可能混了多个指标名（RR/HR/IRR 同族），但它们同为比率型，尺度一致。
    # metric 取本族内出现最多的指标名（并列时按固定顺序），metrics 列出全部。
    metrics_here = [str(it.get('metric') or '').upper() for it in pool]
    metrics_list = [m for m in ('RR', 'HR', 'IRR', 'OR', 'MD', 'SMD')
                    if m in metrics_here]
    metric_label = max(metrics_list, key=lambda m: (metrics_here.count(m),
                                                     -metrics_list.index(m)))
    ratio = group in ('RATIO', 'OR')

    skipped_no_ci: list[dict] = []
    usable: list[dict] = []
    for it in pool:
        if not it.get('valid', True):
            skipped_no_ci.append(it)
            continue
        est, lo, hi = it.get('estimate'), it.get('ci_low'), it.get('ci_high')
        if est is None or lo is None or hi is None:
            skipped_no_ci.append(it)
            continue
        try:
            est_f, lo_f, hi_f = float(est), float(lo), float(hi)
        except (TypeError, ValueError):
            skipped_no_ci.append(it)
            continue
        se = _se_from_ci(ratio, lo_f, hi_f)
        if not math.isfinite(se) or se <= 0:
            # SE=0 或非法 CI：权重会是无穷大，必须丢弃
            skipped_no_ci.append(it)
            continue
        usable.append({'y': _yi(ratio, est_f), 'se': se,
                       'est': est_f, 'lo': lo_f, 'hi': hi_f})

    k = len(usable)
    if k < 2:
        note = (f'可合并的效应量不足 2 个'
                f'（{metric_label} 有 {k} 个具备完整 95%CI 且方向可比）')
        return {
            'metric': metric_label, 'metrics': metrics_list,
            'k': 0, 'pooled': None, 'ci_low': None,
            'ci_high': None, 'se': None, 'i2': None, 'q': None, 'p_value': None,
            'tau2': None, 'df': 0, 'model': 'fixed', 'heterogeneity': '不适用',
            'skipped_no_ci': skipped_no_ci, 'note': note,
        }

    # ---- 固定效应 ----
    weights = [1.0 / (u['se'] ** 2) for u in usable]
    sw = sum(weights)
    y_bar = sum(w * u['y'] for w, u in zip(weights, usable)) / sw
    se_bar = math.sqrt(1.0 / sw)

    # ---- 异质性：Q 检验 + I² ----
    q = sum(w * (u['y'] - y_bar) ** 2 for w, u in zip(weights, usable))
    df = k - 1
    p_value = chi2_sf(q, df)
    i2 = max(0.0, (q - df) / q * 100.0) if q > 0 else 0.0

    # ---- DerSimonian-Laird τ² 与随机效应 ----
    sum_w = sw
    sum_w2 = sum(w * w for w in weights)
    denom = sum_w - sum_w2 / sum_w if sum_w > 0 else 0.0
    tau2 = max(0.0, (q - df) / denom) if denom > 0 else 0.0
    re_weights = [1.0 / (u['se'] ** 2 + tau2) for u in usable]
    re_sw = sum(re_weights)
    re_y = sum(w * u['y'] for w, u in zip(re_weights, usable)) / re_sw
    re_se = math.sqrt(1.0 / re_sw)

    # I² > 50% 时改用随机效应（异质性太大，固定效应会低估不确定性）
    model = 'random' if i2 > 50 else 'fixed'
    y_used, se_used = (re_y, re_se) if model == 'random' else (y_bar, se_bar)

    def _back(y: float) -> float:
        return math.exp(y) if ratio else y

    pooled = _back(y_used)
    ci_low = _back(y_used - Z95 * se_used)
    ci_high = _back(y_used + Z95 * se_used)

    if i2 < 25:
        heterogeneity = '低'
    elif i2 <= 75:
        heterogeneity = '中'
    else:
        heterogeneity = '高'

    note_parts = [f'逆方差加权合并 {k} 篇文献的 {metric_label}'
                  f'（{"随机效应" if model == "random" else "固定效应"}模型）']
    if model == 'random':
        note_parts.append(f'I²={i2:.1f}% > 50%，异质性较大，已改用随机效应模型'
                          f'（τ²={tau2:.4f}，固定效应结果为 {_back(y_bar):.3f}）')
    else:
        note_parts.append(f'I²={i2:.1f}%，异质性{heterogeneity}'
                          f'（Q={q:.2f}，p={p_value:.3f}）')
    if skipped_no_ci:
        note_parts.append(f'{len(skipped_no_ci)} 条效应量缺少可用 95%CI 或标准误为 0，'
                          f'未纳入合并')

    # 方向冲突提示：同一族里既有"偏大"又有"偏小"的估计时提醒下游复核
    null = 1.0 if ratio else 0.0
    up = sum(1 for u in usable if u['est'] > null)
    down = sum(1 for u in usable if u['est'] < null)
    if up and down:
        note_parts.append(f'各研究效应方向不一致（{up} 条偏大、{down} 条偏小），'
                          f'合并结果需谨慎解读')

    other_metrics: list[str] = []
    for g in buckets:
        if g == group:
            continue
        for it in buckets[g]:
            mm = str(it.get('metric') or '').upper()
            if mm and mm not in other_metrics:
                other_metrics.append(mm)
    if other_metrics:
        note_parts.append(f'另有 {"/".join(other_metrics)} 指标，因方向或尺度不可比'
                          f'未与 {metric_label} 合并')

    return {
        'metric': metric_label,
        'metrics': metrics_list,
        'k': k,
        'pooled': pooled,
        'ci_low': ci_low,
        'ci_high': ci_high,
        'se': se_used,
        'i2': i2,
        'q': q,
        'df': df,
        'p_value': p_value,
        'tau2': tau2,
        'model': model,
        'heterogeneity': heterogeneity,
        'skipped_no_ci': skipped_no_ci,
        'note': '；'.join(note_parts),
    }


def summarize_effects(evidence: list[dict]) -> list[dict]:
    """从证据列表里汇总所有效应量，按可合并族分组合并。

    evidence 每项应是 reader 抽取的证据 dict，可能含 'effect_sizes' 字符串列表。
    缺该键或该键不是列表时安全跳过。

    返回每族一个合并结果的列表（可能为空列表）。
    """
    if not evidence or not isinstance(evidence, (list, tuple)):
        return []

    parsed: list[dict] = []
    for ev in evidence:
        if not isinstance(ev, dict):
            continue
        raw = ev.get('effect_sizes')
        if not raw or not isinstance(raw, (list, tuple)):
            continue
        for txt in raw:
            if not isinstance(txt, str):
                continue
            item = parse_effect_size(txt)
            if item:
                parsed.append(item)

    if not parsed:
        return []

    # 按可合并族分组：RR/HR/IRR 一组，OR 一组，MD 一组，SMD 一组
    by_group: dict[str, list[dict]] = {}
    for it in parsed:
        by_group.setdefault(pool_group(it['metric']), []).append(it)

    results: list[dict] = []
    for group in _GROUP_ORDER:
        if group in by_group:
            results.append(pool_effect_sizes(by_group[group]))
    return results
