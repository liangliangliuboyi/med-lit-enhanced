# -*- coding: utf-8 -*-
"""按模型分档的成本估算。

为什么需要这个模块
------------------
原先成本估算只有一个全局单价（`runtime.price_per_million_tokens`），
但本项目默认的模型链横跨 DeepSeek-V3.2 / R1、Qwen3.5-35B-A3B、Qwen3-14B 等，
**不同模型的单价能差 5 倍以上**（一个 9B 小模型和一个 70B 大模型不是一个量级）。
用全局单价算出来的成本要么严重高估、要么严重低估，用户没法据此判断
"这次跑下来大概要花多少钱、值不值得把检索式调得更细"。

因此这里改成**按模型分档计价**：
- 价格表按模型名索引，价格单位是"人民币 / 每百万 token"；
- 匹配顺序是"精确 → 前缀"（`deepseek-ai/DeepSeek-V3.2` 能覆盖带日期后缀的
  `-0728` 变体），匹配时大小写不敏感；
- 查不到价格的模型**按 0 处理**（"未知 / 不计价"），而不是报错。
  宁可少算也不要让用户看到一个凭空的数字——错误的成本比没有成本更糟。

注意：价格随时会变，这里只是占位；使用者应按自己服务商的实时价格改这张表。
"""
from __future__ import annotations

# ---------------- 价格表 ----------------
# 单位：人民币 / 每百万 token（输入输出合并的粗略均价，仅用于量级估算）。
# 价格为 0 表示"未知 / 不计价"——宁可不算也不要报错。
#
# 价格随时会变，这里只是占位；使用者应按自己服务商的实时价格改这张表。
DEFAULT_PRICE_TABLE: dict[str, float] = {
    # ---- DeepSeek 系列 ----
    'deepseek-ai/DeepSeek-V3.2': 2.0,
    'deepseek-ai/DeepSeek-V3.1-Terminus': 2.0,
    'deepseek-ai/DeepSeek-R1': 4.0,
    # ---- Qwen3.5 系列 ----
    'Qwen/Qwen3.5-35B-A3B': 0.5,
    'Qwen/Qwen3.5-27B': 0.4,
    'Qwen/Qwen3.5-9B': 0.15,
    # ---- Qwen3 系列 ----
    'Qwen/Qwen3-32B': 0.8,
    'Qwen/Qwen3-14B': 0.4,
    # 重排模型按"次"计费，不按 token 计费，这里不计价。
    'Qwen/Qwen3-Reranker-8B': 0.0,
    # ---- Qwen2.5 系列 ----
    'Qwen/Qwen2.5-72B-Instruct': 2.4,
    'Qwen/Qwen2.5-7B-Instruct': 0.3,
    # ---- 多模态（图表识别用）----
    # 图片是 base64 内联进 prompt 的，**输入 token 会被图片放大**
    # （一张森林图的 base64 大约折合上千 token），所以单价要按"贵"估，
    # 不能当成普通 8B 文本模型算。
    'Qwen/Qwen3-VL-8B-Instruct': 1.5,
    'Qwen/Qwen3-VL-8B-Thinking': 1.5,
    'Qwen/Qwen3-VL-30B-A3B-Instruct': 2.0,
    'Qwen/Qwen3-VL-30B-A3B-Thinking': 2.0,
    'Qwen/Qwen3-VL-32B-Instruct': 2.5,
    'Qwen/Qwen3-VL-32B-Thinking': 2.5,
}


# ---------------- 查价 ----------------
def lookup_price(model: str, table: dict | None = None) -> float:
    """查某个模型的单价（人民币 / 每百万 token）；查不到返回 0.0。

    匹配顺序：精确匹配 → 前缀匹配（取**最长**的匹配前缀，最具体的优先）。
    大小写不敏感——不同平台给同一个模型的大小写写法并不统一。

    前缀匹配是必需的：平台上的模型名常带日期/量化后缀
    （`deepseek-ai/DeepSeek-V3.2-0728`），只做精确匹配会大量漏查。
    """
    if not model:
        return 0.0
    tbl = table if table else DEFAULT_PRICE_TABLE
    if not tbl:
        return 0.0

    name = str(model).strip().lower()
    if not name:
        return 0.0

    # 精确匹配：即使价格是 0（表示"这张表里明确标记为不计价"）也直接返回，
    # 不再退去做前缀匹配——否则一个不计价的模型可能被另一条规则误配到价格。
    for key, price in tbl.items():
        if str(key).strip().lower() == name:
            try:
                return float(price)
            except (TypeError, ValueError):
                return 0.0

    # 前缀匹配：多个前缀同时命中时取最长的那个。
    best_key = ''
    best_price = 0.0
    for key, price in tbl.items():
        k = str(key).strip().lower()
        if not k or not name.startswith(k):
            continue
        if len(k) > len(best_key):
            best_key = k
            try:
                best_price = float(price)
            except (TypeError, ValueError):
                best_price = 0.0
    return best_price if best_key else 0.0


def _short_model(model: str) -> str:
    """把 `deepseek-ai/DeepSeek-V3.2` 缩成 `DeepSeek-V3.2`（去掉厂商前缀）。

    只用于一行式摘要里给人看；计算一律用完整模型名。
    """
    name = str(model or '').strip()
    if '/' in name:
        name = name.rsplit('/', 1)[-1]
    return name


# ---------------- 估算 ----------------
# usage 里携带角色→模型映射的保留键。
# 角色名（planner/reader/…）本身不是模型名，查价必须用真实模型名，
# 所以主流程把 {角色: 模型名} 挂在 usage 的这个保留键下一起传进来。
MODELS_KEY = '_models'


def estimate_cost(usage: dict[str, int], table: dict | None = None,
                  role_models: dict[str, str] | None = None) -> dict:
    """按各角色消耗的 token 估算成本。

    参数
    ----
    usage:
        ``{角色名: token 数}``。主流程按角色累计 token（例如
        ``{'planner': 1200, 'reader': 8000}``）。
        也支持直接用模型名当键（``{'deepseek-ai/DeepSeek-V3.2': 8000}``）。
    table:
        价格表，缺省用 `DEFAULT_PRICE_TABLE`。
    role_models:
        ``{角色名: 模型名}``。角色名不是模型名，查价必须用真实模型名——
        没有这张映射时，usage 的键会被当作模型名处理（角色名自然查不到价）。

    角色→模型的映射有两种给法，效果相同，都支持：
    1. 显式传 `role_models` 参数；
    2. 在 usage 里放保留键 `'_models'`（主流程用的就是这种，省一个参数）。

    返回
    ----
    dict:
        ``total_tokens``    所有角色的 token 总量；
        ``priced_tokens``   能查到价格的 token 量；
        ``unpriced_models`` 查不到价格的**模型名**（去重）；
        ``estimated_cny``   估算总花费（人民币，保留 4 位小数）；
        ``breakdown``       按角色汇总的明细：
        ``{角色: {'tokens': n, 'cny': x, 'priced': bool, 'model': 模型名}}``。
        每个角色都记下自己用的模型名——只有角色名是查不到价的。

    查不到价格的 token 计入 total_tokens 但不计入 priced_tokens，费用记 0——
    这样"这次花了多少钱"和"有多少 token 没算钱"两件事都能看清楚，
    不会出现一个被悄悄摊薄的总数。
    """
    tbl = table if table else DEFAULT_PRICE_TABLE
    usage = usage or {}

    # 合并两处角色→模型映射：显式参数优先，usage 里的保留键作为补充。
    models_map: dict[str, str] = {}
    inline = usage.get(MODELS_KEY)
    if isinstance(inline, dict):
        models_map.update({str(k): str(v) for k, v in inline.items() if v})
    if role_models:
        models_map.update({str(k): str(v) for k, v in role_models.items() if v})

    breakdown: dict[str, dict] = {}
    unpriced_models: list[str] = []
    total_tokens = 0
    priced_tokens = 0
    total_cny = 0.0

    for label, raw_tokens in usage.items():
        if label == MODELS_KEY:
            # 这是角色→模型的映射，不是某个角色的 token 数。
            continue
        try:
            tokens = int(raw_tokens or 0)
        except (TypeError, ValueError):
            tokens = 0
        if tokens <= 0:
            continue

        model = str(models_map.get(label, label) or '').strip()
        price = lookup_price(model, tbl)
        priced = price > 0

        cny = tokens / 1_000_000 * price if priced else 0.0
        total_tokens += tokens
        total_cny += cny
        if priced:
            priced_tokens += tokens
        else:
            if model not in unpriced_models:
                unpriced_models.append(model)

        breakdown[label] = {
            'tokens': tokens,
            'cny': round(cny, 4),
            'priced': priced,
            'model': model,
        }

    return {
        'total_tokens': total_tokens,
        'priced_tokens': priced_tokens,
        'unpriced_models': unpriced_models,
        'estimated_cny': round(total_cny, 4),
        'breakdown': breakdown,
    }


def format_cost_line(estimate: dict) -> str:
    """把估算结果压成一行中文摘要（可直接写进日志或报告）。

    有价格时形如：
        本次消耗约 12,345 tokens，估算成本 ¥0.0123（DeepSeek-V3.2 等 3 个模型）
    消耗了 token 但一个都算不出价格时：
        本次消耗约 12,345 tokens，成本未知（未配置单价）

    空估算也会给出一句可读的话（"消耗约 0 tokens"）而不是空串——
    日志里直接拼接时不必再判断有没有内容。
    """
    est = estimate or {}
    total = int(est.get('total_tokens') or 0)
    priced_tokens = int(est.get('priced_tokens') or 0)

    if priced_tokens <= 0:
        return f'本次消耗约 {total:,} tokens，成本未知（未配置单价）'

    cny = float(est.get('estimated_cny') or 0.0)
    # 已计价的模型名从 breakdown 里现算（每个角色都记了自己用的模型名）。
    models: list[str] = []
    for label, item in (est.get('breakdown') or {}).items():
        if (item or {}).get('priced'):
            model = (item or {}).get('model') or label
            if model and model not in models:
                models.append(model)

    if not models:
        tail = ''
    elif len(models) == 1:
        tail = f'（{_short_model(models[0])}）'
    else:
        tail = f'（{_short_model(models[0])} 等 {len(models)} 个模型）'

    return f'本次消耗约 {total:,} tokens，估算成本 ¥{cny:.4f}{tail}'
