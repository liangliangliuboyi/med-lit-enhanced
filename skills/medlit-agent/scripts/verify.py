# -*- coding: utf-8 -*-
"""证据可信度校验：B5「论断—文献语义比对」+ B8「相关度交叉验证」。

为什么需要这个模块
------------------
本仓库原本只做**引用越界校验**：正文里出现的每个 `[PMID:xxx]` / `[DOI:xxx]`
是否来自本轮候选池（见 pipeline.validate_pmids）。这只能抓住"凭空编的标识"，
抓不住"真实存在但内容对不上"的引用——模型完全可以挂一篇真实 PMID、内容却与
那句话无关的文献，读者点进去才发现对不上。这类错误是循证答复里最伤信誉的一类，
因为它披着"可核查"的外衣。

于是补上两层校验：

B5 论断—文献语义比对
    把答案正文切成带引用的论断，对每条论断，让 reader 模型只看"被引文献自己
    抽取出来的结构化信息"，判断它是否真的支持这句话，输出
    支持 / 部分支持 / 不支持 / 无法判断。
    关键约束：**只喂给模型该文献的摘要级信息**（标题、设计、样本量、关键结果、
    效应量），不给全文、也不让模型联网——这样模型只能凭"文献说了什么"来判断，
    而不是凭"题目像不像"。判定是模型辅助的，程序侧只做机械归类。

B8 相关度交叉验证
    原来的 relevance（1-5）是 reader 模型一次打分给出的，偶然性大。改成两个
    不同模型各打一次、取平均；两者差 ≥2 分时标记为分歧（提示这条文献的价值
    对判断者而言不稳定，需要人工看一眼）。单个模型失败时退化为另一个模型的结果，
    两个都失败则保留原分并标记失败——**绝不因为交叉验证失败就把文献丢掉**。

两条设计原则
------------
1. **可离线测试**：模型调用一律通过注入的 `chat_fn` 回调获得，本模块自己不 new
   Provider、不发任何网络请求。单元测试用假回调跑。
2. **失败不致命**：单条核对失败（JSON 解析不了、模型异常）只记进 failed 列表，
   不影响其它条目，也不向上抛异常——校验是"体检"，不是"门禁"。
"""
from __future__ import annotations

import inspect
import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Optional

__all__ = [
    'parse_claims',
    'verify_claims',
    'cross_check_relevance',
    'cross_check_gain',
    'CLAIM_VERIFY_SYSTEM',
    'CLAIM_VERIFY_USER',
    'RELEVANCE_SCORE_SYSTEM',
    'RELEVANCE_SCORE_USER',
]


# ============================================================================
# 判定口径与提示词（放模块顶部，主流程与测试都可复用/覆写）
# ============================================================================
CLAIM_VERIFY_SYSTEM = """你是循证医学审稿人，负责判断**答案里的一句论断**是否真的被**它所引用的那几篇文献**支持。

规则：
1. 只依据下面给出的「文献信息卡」判断，不要引入外部知识，不要脑补原文没写的内容。
2. 判定只能用这四个值之一：
   - "支持"：文献研究的问题与论断在人群/干预/对照/结局上对得上，文献报告的结果
     方向与数值直接支撑这句话的表述；
   - "部分支持"：方向一致但只覆盖论断的一部分——例如只支持某个亚组、只支持方向
     不支持具体数值、或论断的外推超出了文献结论的范围；
   - "不支持"：文献研究的问题与论断不是一回事（人群、干预、对照、结局对不上），
     或文献结论与论断相反；
   - "无法判断"：文献信息不足（缺关键结果、缺效应量、只有摘要片段、只有会议摘要），
     不足以判断支不支持。
3. 判定的是**内容是否对应**，不评价研究质量。设计粗糙但结论对得上，仍算"支持"；
   设计漂亮但结论对不上，算"不支持"。
4. 若文献标注为撤稿或勘误，一律判"不支持"，并在理由里点明撤稿/勘误。
5. 理由用中文一句话，不超过 40 字，必须指出依据来自文献的哪一处（标题/设计/关键结果）。
6. 只输出 JSON，不要任何解释文字。

输出格式：
{"verdict": "支持 / 部分支持 / 不支持 / 无法判断", "reason": "一句话理由"}"""

CLAIM_VERIFY_USER = """待核对论断（来自答案正文）：
{claim}

被引用的文献信息卡：
{cards}

请判断这些文献是否支持上面这句论断，只输出 JSON。"""

RELEVANCE_SCORE_SYSTEM = """你是医学文献筛选专家，负责给一篇文献「对回答核心问题的价值」打 1-5 分。

规则：
1. 只依据下面给出的「文献信息卡」判断，不要引入外部知识。
2. 打分口径（与本系统其它环节一致）：
   - 5 = 直接回答核心问题（人群、干预、对照、结局基本对得上）；
   - 4 = 与核心问题高度相关，但结局或人群有一处不完全吻合；
   - 3 = 间接相关（同一大领域，关键要素不覆盖）；
   - 2 = 边缘相关（只涉及问题的一个侧面）；
   - 1 = 几乎无关。
3. **独立打分**：不要参考任何已有的相关性评分，也不要猜测别人会给几分。
4. 理由用中文一句话，不超过 40 字。
5. 只输出 JSON，不要解释文字。

输出格式：
{"relevance": 1, "reason": "一句话理由"}"""

RELEVANCE_SCORE_USER = """核心问题：
{question}

文献信息卡：
{card}

请给出 1-5 的相关性评分，只输出 JSON。"""


# ============================================================================
# 内部常量
# ============================================================================
CITATION_RE = re.compile(r'\[(PMID|DOI)\s*:\s*([^\]\s]+)\]', re.IGNORECASE)

# 句子切分：中文句号/问号/叹号/分号/换行直接断；英文句号在"后面跟空白或结尾"
# 时断——这样小数（1.24）不会被撕开（. 后面跟的是数字，不是空白）。
_SPLIT_RE = re.compile(r'[。！？；\n]+|\.(?=\s|$)')

# 英文缩写里的点不是句号：切分前先把它们的点换成哨兵，切完再还原。
# 否则 "et al. 2020" 会被切成两半，"e.g." 会把句子从中间劈开。
_ABBREV_RE = re.compile(
    r'\b(e\.g|i\.e|et\s+al|al|vs|Fig|Figs|No|Nos|Ref|Refs|approx|Ca|Cf|St|Dr|Mr|Mrs|Ms|'
    r'Jr|Sr|Inc|Ltd|Co|Dept|Univ|Prof|et)\.(?=\s|$)', re.IGNORECASE)

# Markdown 有序列表的 "1. " 里的点也不是句号：不保护的话，一整条列表项
# 会被切成 "1" 和正文两半，引用就落到没有正文的那半边去了。
_LIST_DOT_RE = re.compile(r'(?m)^(\s*\d+)\.(?=\s)')

_ABBR_SENTINEL = '\x00'

# 引用标记在切分前先掩码成不含标点的私有区字符，避免 DOI 里的 "10.1101" 被
# 当成句号切碎，也避免引用标记里的冒号影响切分。
_MASK_START = '\ue000'
_MASK_END = '\ue001'
_MASK_RE = re.compile(_MASK_START + r'(\d+)' + _MASK_END)

# Markdown 列表符号 / 强调符，切句后清理
_BULLET_RE = re.compile(r'^\s*(?:[-*+]\s+|\d+[.)]\s+)')

_VERDICT_UNSUPPORTED = '不支持'
_VERDICT_PARTIAL = '部分支持'
_VERDICT_SUPPORTED = '支持'
_VERDICT_UNCERTAIN = '无法判断'
_VERDICT_FAILED = '核对失败'
_VERDICT_UNMAPPED = '无法核对'

_MAX_CLAIM_TEXT = 300

# ============================================================================
# 并发度
# ============================================================================
# 为什么这里能并发、并发多少
# ------------------------
# B8 交叉验证是整个流水线里最慢的一步：N 篇文献 × 2 个模型 = 2N 次模型调用。
# 原来全串行，5 篇就要 458 秒（实测 trace）。而这两次调用的 prompt 是**短**的
# （只喂结构化字段，不喂全文），实测单次 0.7~2.5 秒——同仓库的 pipeline.read_all
# 用同样的 ThreadPoolExecutor 并发跑精读，5 篇只要 33 秒。
#
# 但并发度不能开大：
#   1. 模型服务商会限流（429），并发太高会成片失败；
#   2. 本仓库吃过教训——「并发打长文本会超时」。这里 prompt 短，所以 4~6 安全，
#      但仍留出上限，不让调用方无脑拉到几十。
#
# 因此默认 4，且做成可配置（模块常量 + 关键字参数），上限硬性钉在 _WORKERS_CAP。
CROSS_CHECK_WORKERS = 4        # B8 交叉验证默认并发度
CLAIM_CHECK_WORKERS = 4        # B5 论断比对默认并发度
_WORKERS_CAP = 8               # 硬上限：无论调用方怎么传，都不超过这个数

NOTE_TEXT = ('本结果由模型辅助产出，用于提示「引用」与「文献内容」可能对不上，'
             '不构成人工审阅，也不评价研究质量。')


# ============================================================================
# 工具函数
# ============================================================================
def _norm_doi(value: Any) -> str:
    """DOI 归一化：去空白、转小写、去掉 `doi:` 前缀。"""
    s = str(value or '').strip().lower()
    if s.startswith('doi:'):
        s = s[4:].strip()
    return s


def _split_segments(text: str) -> list[str]:
    """把正文切成句/段片段。

    先保护两类"不是句号的点"——英文缩写（et al. / e.g.）与 Markdown 有序列表
    编号（"1. "），再按中文标点、换行、英文句号切分，最后还原。
    """
    protected = _ABBREV_RE.sub(lambda m: m.group(1) + _ABBR_SENTINEL, text)
    protected = _LIST_DOT_RE.sub(lambda m: m.group(1) + _ABBR_SENTINEL, protected)
    parts = [p for p in _SPLIT_RE.split(protected) if p and p.strip()]
    return [p.replace(_ABBR_SENTINEL, '.') for p in parts]


def _extract_json(text: Any) -> Optional[dict]:
    """从模型输出里抠出 JSON 对象（容忍 ```json 包裹与前后噪音）。

    这里没有复用 pipeline._json_load：verify.py 需要能脱离 pipeline 单独导入
    与离线测试，而 pipeline 会连带拉起一串模块。逻辑保持一致——用括号配对扫描
    而不是贪婪正则，避免模型在 JSON 前后夹带含花括号的说明文字导致解析失败。
    """
    if isinstance(text, dict):
        return text
    if not text:
        return None
    t = str(text).strip()
    t = re.sub(r'^```(?:json)?\s*', '', t)
    t = re.sub(r'\s*```$', '', t)
    try:
        obj = json.loads(t)
        if isinstance(obj, dict):
            return obj
    except Exception:  # noqa: BLE001
        pass
    start = t.find('{')
    if start < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(t)):
        ch = t[i]
        if in_str:
            if esc:
                esc = False
            elif ch == '\\':
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                try:
                    obj = json.loads(t[start:i + 1])
                except Exception:  # noqa: BLE001
                    return None
                return obj if isinstance(obj, dict) else None
    return None


def _norm_verdict(raw: Any) -> Optional[str]:
    """把模型给的判定值归一化成四个标准值之一；认不出返回 None。

    顺序很重要："部分支持" 里也含"支持"，所以先判"不支持"、再判"部分"、
    最后才判"支持"。
    """
    s = str(raw or '').strip()
    if not s:
        return None
    low = s.lower()
    if '不支持' in s or 'unsupported' in low or 'not support' in low:
        return _VERDICT_UNSUPPORTED
    if '部分' in s or 'partial' in low:
        return _VERDICT_PARTIAL
    if '无法' in s or '不确定' in low or 'unable' in low or 'cannot' in low:
        return _VERDICT_UNCERTAIN
    if '支持' in s or 'support' in low:
        return _VERDICT_SUPPORTED
    return None


def _to_score(raw: Any) -> Optional[float]:
    """把模型给的相关度分转成 1-5 之间的数；认不出返回 None。

    **越界不算"勉强能用"，算无效**：模型给出 0 或 7，说明它没按 1–5 的
    量表打分（或者在胡编）。把它夹到 1 或 5 会**把无效输出伪装成合法分数**，
    然后这个分数还会被采纳为有效 relevance 覆盖掉原来的评分。
    宁可判失败，也不要污染分数。
    """
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return None
    if val != val:          # NaN
        return None
    if val < 1 or val > 5:
        return None
    return val


def _fmt_list(value: Any, limit: int = 6) -> str:
    """把可能是字符串/列表的字段压成一行文本。"""
    if value is None:
        return ''
    if isinstance(value, str):
        items = [value]
    elif isinstance(value, (list, tuple)):
        items = list(value)
    else:
        items = [value]
    parts = [str(x).strip() for x in items if str(x).strip()]
    return '；'.join(parts[:limit])


def _evidence_card(ev: dict, max_chars: int = 3000) -> str:
    """把一条证据压成给模型看的「文献信息卡」。

    注意：**不输出 relevance**。交叉验证要的是两个模型各自独立判断，把原有分数
    喂进去会造成锚定，第二个模型只会跟着第一个走。

    `max_chars` 是**总长度上限**：单条证据的 key_findings / effect_sizes /
    population 都可能很长，全塞进去会让 prompt 变长。
    本仓库吃过教训——"并发打长文本会超时"（Qwen3.5 系列 26000 字就超时），
    所以这里必须给个硬上限，不能指望每个字段都短。
    """
    rows: list[str] = []
    # 内部字段（_title / _journal / _year）是精读阶段写进去的，
    # 但**必须留兜底**：证据条目可能来自断点恢复或外部构造，那时只有
    # 普通字段。没有兜底的话标题会整个丢失（信息卡变成"引用标识 + 研究设计"）。
    title = ev.get('_title') or ev.get('title') or ''
    journal = ev.get('_journal') or ev.get('journal') or ''
    year = ev.get('_year') or ev.get('year') or ''
    cite_key = ev.get('_citation_key') or ev.get('pmid') or ''

    rows.append('引用标识: ' + str(cite_key))
    if title:
        rows.append('标题: ' + str(title))
    meta = ' / '.join(str(x) for x in (journal, year) if x and str(x) != 'None')
    if meta:
        rows.append('期刊与年份: ' + meta)
    if ev.get('study_design'):
        rows.append('研究设计: ' + str(ev['study_design']))
    if ev.get('sample_size'):
        rows.append('样本量: ' + str(ev['sample_size']))
    for label, key in (('人群', 'population'), ('干预/暴露', 'intervention'),
                       ('对照', 'comparator'), ('主要结局', 'primary_outcome')):
        if ev.get(key):
            rows.append(label + ': ' + _fmt_list(ev[key], limit=3))
    if ev.get('key_findings'):
        rows.append('关键结果: ' + _fmt_list(ev['key_findings']))
    if ev.get('effect_sizes'):
        rows.append('效应量: ' + _fmt_list(ev['effect_sizes']))
    if ev.get('pico_match'):
        rows.append('PICO 匹配: ' + str(ev['pico_match']))
    if ev.get('_text_source'):
        rows.append('文本来源: ' + str(ev['_text_source']))
    if ev.get('_retraction'):
        rows.append('撤稿/勘误提示: ' + str(ev['_retraction']))
    text = '\n'.join(rows)
    if max_chars and len(text) > max_chars:
        # 截断时保留开头（引用标识、标题、设计、样本量在前面，信息密度最高）
        text = text[:max_chars].rstrip() + '…（内容过长已截断）'
    return text


def _build_index(evidence: list[dict]) -> tuple[dict[str, dict], dict[str, dict]]:
    """建 PMID / DOI 两个查表索引。"""
    by_pmid: dict[str, dict] = {}
    by_doi: dict[str, dict] = {}
    for ev in evidence or []:
        if not isinstance(ev, dict):
            continue
        pmid = ev.get('pmid')
        if pmid not in (None, ''):
            by_pmid.setdefault(str(pmid).strip(), ev)
        ckey = ev.get('_citation_key')
        if ckey not in (None, ''):
            ck = str(ckey).strip()
            if ck.lower().startswith('doi:'):
                by_doi.setdefault(_norm_doi(ck), ev)
            else:
                by_pmid.setdefault(ck, ev)
        doi = ev.get('_doi')
        if doi not in (None, ''):
            by_doi.setdefault(_norm_doi(doi), ev)
    return by_pmid, by_doi


def _lookup_citation(cit: dict, by_pmid: dict, by_doi: dict) -> Optional[dict]:
    """按引用类型在索引里找证据条目；找不到返回 None。"""
    ctype = str(cit.get('type') or '').lower()
    value = str(cit.get('value') or '').strip()
    if not value:
        return None
    if ctype == 'pmid':
        return by_pmid.get(value)
    if ctype == 'doi':
        return by_doi.get(_norm_doi(value))
    return None


# ============================================================================
# 公开 API 1：解析论断
# ============================================================================
def parse_claims(answer: str) -> list[dict]:
    """把答案正文拆成「论断 + 引用」清单。

    一条论断 = 一个句/段片段里出现的引用标记。切分按中文句号/问号/叹号/分号/
    换行，英文句号只在后面跟空白或结尾时断（否则 "RR 1.24" 会被撕开），
    英文缩写（et al. / e.g.）里的点会被保护，不当句号处理。

    返回每条：{'text': 去标记后的正文（≤300 字）, 'citations': [...], 'index': 序号}。
    没有引用标记的片段不返回——只关心带引用的论断。
    """
    if not answer or not isinstance(answer, str):
        return []

    # 引用标记先掩码，避免 DOI 里的点被当成句号、把句子切碎
    cites: list[dict] = []
    masked_parts: list[str] = []
    last = 0
    for m in CITATION_RE.finditer(answer):
        masked_parts.append(answer[last:m.start()])
        idx = len(cites)
        cites.append({'type': m.group(1).lower(), 'value': m.group(2).strip()})
        masked_parts.append(_MASK_START + str(idx) + _MASK_END)
        last = m.end()
    masked_parts.append(answer[last:])
    masked = ''.join(masked_parts)

    claims: list[dict] = []
    for seg in _split_segments(masked):
        masks = _MASK_RE.findall(seg)
        if not masks:
            continue
        text = _MASK_RE.sub('', seg)
        text = _BULLET_RE.sub('', text)
        text = re.sub(r'\s+', ' ', text).strip()
        text = text.strip(' \t`*_#')
        if not text:
            # 片段里只有引用标记、没有可核对的文字（例如孤零零的一个 [PMID:x]），
            # 跳过：没有论断内容就无从比对。
            continue
        claims.append({
            'text': text[:_MAX_CLAIM_TEXT],
            'citations': [cites[int(i)] for i in masks],
            'index': len(claims) + 1,
        })
    return claims


# ============================================================================
# 公开 API 2：论断—文献语义比对（B5）
# ============================================================================
def _verify_one(chat_fn: Callable[..., dict], claim: dict,
                matched: list[dict], unmatched: list[dict]) -> dict:
    """核对单条论断，返回一条记录。

    任何异常都在这里就地兜住 → 该条记为「核对失败」，**不向上抛**：
    校验是"体检"不是"门禁"，而且并发时一条坏数据不能牵连其它条。
    """
    record: dict[str, Any] = {
        'index': claim['index'],
        'text': claim['text'],
        'citations': claim['citations'],
        'verdict': '',
        'reason': '',
    }
    try:
        cards = '\n\n'.join(
            ('文献 ' + str(i + 1) + '\n' + _evidence_card(ev))
            for i, ev in enumerate(matched))
        msgs = [
            {'role': 'system', 'content': CLAIM_VERIFY_SYSTEM},
            {'role': 'user', 'content': CLAIM_VERIFY_USER.format(
                claim=claim['text'], cards=cards)},
        ]
        res = chat_fn('reader', msgs, temperature=0.1, max_tokens=600)
        content = res.get('content') if isinstance(res, dict) else res
        data = _extract_json(content)
        if not isinstance(data, dict):
            raise ValueError('模型返回内容不是合法 JSON')
        verdict = _norm_verdict(
            data.get('verdict', data.get('judgement', data.get('judgment'))))
        if not verdict:
            raise ValueError('模型返回的判定值无法识别: '
                             + str(data.get('verdict'))[:60])
        reason = str(data.get('reason', data.get('explanation', '')) or '').strip()
        if unmatched:
            reason = (reason + '（另有 ' + str(len(unmatched))
                      + ' 条引用未在证据表中找到，未纳入判断）').strip()
        record['verdict'] = verdict
        record['reason'] = reason[:300]
    except Exception as e:  # noqa: BLE001
        record['verdict'] = _VERDICT_FAILED
        record['reason'] = str(e)[:150]
    return record


def verify_claims(answer: str, evidence: list[dict],
                  chat_fn: Callable[..., dict], *, max_checks: int = 12,
                  max_workers: Optional[int] = None) -> dict:
    """逐条核对答案里的论断是否真的被它引用的文献支持。

    参数
    ----
    answer : str            答案正文
    evidence : list[dict]   证据条目（reader 抽取的结构化结果）
    chat_fn : callable      模型调用回调，形如 `router.chat`；
                            由调用方注入，本模块不自己建连接。
    max_checks : int        最多核验多少条论断（按正文出现顺序），控制成本。
    max_workers : int|None  并发度上限（同时在跑的模型调用数）。None 用默认值
                            CLAIM_CHECK_WORKERS；实际值被硬性夹在 [1, _WORKERS_CAP]。

    返回
    ----
    {'checked', 'total_claims', 'unsupported', 'partial', 'uncertain', 'failed',
     'summary', 'note'}

    另附 `supported` 列表（判为"支持"的论断）——判定为"支持"既不是缺陷也不是
    不确定，单列出来才能在报告里说清"核了几条、其中几条站得住"，否则读者只能
    靠减法反推。

    约定
    ----
    - 引用映射不到证据 → 判「无法核对」并记入 uncertain，不算 unsupported，
      也不抛异常（可能是预印本没进池子）。
    - 单条失败（模型异常 / 返回值不是合法 JSON）→ 记入 failed，继续下一条。
    - `checked` 是**实际发起过模型调用**的条数（含调用失败的）。
    - 各判定列表里的条目**按论断在正文中出现的顺序排列**（并发下完成顺序是
      乱的，所以先按 index 收集、再排序还原）。

    并发说明
    --------
    每条论断的核对彼此独立（互不影响、互不依赖），所以改成并发执行。
    并发结束后必须**按论断在正文中的位置重新排序**再分桶——否则结果列表的
    顺序会随网络快慢乱掉，报告里"第 3 条"可能变成别的论断。
    """
    result: dict[str, Any] = {
        'checked': 0,
        'total_claims': 0,
        'supported': [],
        'unsupported': [],
        'partial': [],
        'uncertain': [],
        'failed': [],
        'summary': '',
        'note': NOTE_TEXT,
    }

    claims = parse_claims(answer)
    result['total_claims'] = len(claims)
    if not claims:
        result['summary'] = '正文未出现带引用标记的论断，未做语义核对。'
        return result

    by_pmid, by_doi = _build_index(evidence)

    # 先做纯本地的映射（不花钱、不调模型）：分出"可核对"与"无法核对"。
    checkable: list[tuple[dict, list[dict], list[dict]]] = []
    unmapped: list[dict] = []
    for claim in claims:
        matched: list[dict] = []
        unmatched: list[dict] = []
        for cit in claim['citations']:
            ev = _lookup_citation(cit, by_pmid, by_doi)
            if ev is None:
                unmatched.append(cit)
            else:
                matched.append(ev)
        if not matched:
            unmapped.append({
                'index': claim['index'],
                'text': claim['text'],
                'citations': claim['citations'],
                'verdict': _VERDICT_UNMAPPED,
                'reason': '引用的文献不在本轮证据表中，无法核对内容对应关系。',
            })
            continue
        checkable.append((claim, matched, unmatched))

    budget = max(0, int(max_checks))
    todo = checkable[:budget]

    # 并发核对：每条论断一个任务。完成后按 index 收集——顺序要还原，
    # 不能按完成顺序直接 append（并发下完成顺序是乱的）。
    workers = _resolve_workers(max_workers, CLAIM_CHECK_WORKERS)
    records: dict[int, dict] = {}
    if todo:
        with ThreadPoolExecutor(max_workers=min(workers, len(todo))) as ex:
            futs = {}
            for claim, matched, unmatched in todo:
                idx = int(claim['index'])
                futs[ex.submit(_verify_one, chat_fn, claim, matched, unmatched)] = (idx, claim)
            for fut in as_completed(futs):
                idx, claim = futs[fut]
                try:
                    records[idx] = fut.result()
                except Exception as e:  # noqa: BLE001
                    # _verify_one 内部已兜底，这里是最后一道防线
                    records[idx] = {
                        'index': idx,
                        'text': claim['text'],
                        'citations': claim['citations'],
                        'verdict': _VERDICT_FAILED,
                        'reason': str(e)[:150],
                    }

    # 按论断在正文中的位置排序后分桶：并发打乱了完成顺序，
    # 报告里引用的"第几条"必须仍然对应正文里的第几条。
    ordered = sorted(records.values(), key=lambda r: r.get('index') or 0)
    for rec in ordered:
        verdict = rec.get('verdict')
        if verdict == _VERDICT_SUPPORTED:
            result['supported'].append(rec)
        elif verdict == _VERDICT_UNSUPPORTED:
            result['unsupported'].append(rec)
        elif verdict == _VERDICT_PARTIAL:
            result['partial'].append(rec)
        elif verdict == _VERDICT_FAILED:
            result['failed'].append(rec)
        else:
            result['uncertain'].append(rec)

    # "引用映射不到证据"的条目同样按论断位置混进 uncertain，
    # 让 uncertain 整体保持正文顺序。
    for rec in sorted(unmapped, key=lambda r: r.get('index') or 0):
        result['uncertain'].append(rec)
    result['uncertain'].sort(key=lambda r: r.get('index') or 0)

    result['checked'] = len(ordered)

    skipped = len(checkable) - min(len(checkable), budget)
    result['summary'] = (
        '共 {total} 条带引用论断，实际核验 {checked} 条：支持 {sup} 条、'
        '不支持 {uns} 条、部分支持 {par} 条、无法判断或无法核对 {unc} 条、'
        '核对失败 {fail} 条'
        + ('，另有 {skip} 条因超过核验上限未核验。'.format(skip=skipped)
           if skipped else '。')
    ).format(total=result['total_claims'], checked=result['checked'],
             sup=len(result['supported']), uns=len(result['unsupported']),
             par=len(result['partial']),
             unc=len(result['uncertain']), fail=len(result['failed']))
    return result


# ============================================================================
# 公开 API 3：相关度交叉验证（B8）
# ============================================================================
def _chat_accepts_model(chat_fn: Callable[..., dict]) -> bool:
    """探测 chat_fn 是否接受 model 关键字参数。

    用 inspect 而不是"传了 model 报 TypeError 再回头"——后者会把 chat_fn 内部
    真正的 TypeError 也误判成"不支持 model"，进而悄悄退化成同一个模型打两次分，
    交叉验证就白做了。
    """
    try:
        params = inspect.signature(chat_fn).parameters
    except (TypeError, ValueError):
        return False
    if 'model' in params:
        return True
    return any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())


def _score_once(chat_fn: Callable[..., dict], role: str, msgs: list[dict],
                model: Optional[str]) -> dict:
    """让一个模型打一次分。返回 {'score', 'reason', 'model', 'error'}。"""
    out: dict[str, Any] = {'score': None, 'reason': '', 'model': None,
                           'error': None, 'model_ignored': False}
    try:
        kwargs: dict[str, Any] = {'temperature': 0.1, 'max_tokens': 400}
        if model:
            if _chat_accepts_model(chat_fn):
                kwargs['model'] = model
            else:
                # 调用方的 chat_fn 不接受 model 参数（如直接传 ModelRouter.chat），
                # 指定的模型无法生效。这里不静默吞掉——记下来，交给上层判断
                # 这次"交叉验证"是不是退化成了同一个模型打两次分。
                out['error'] = ('chat_fn 不支持 model 参数，无法指定模型 '
                                 + str(model) + '（已退化为按角色路由）')
                # 专门标记：这才是"模型没生效"。别的失败（网络超时、返回解析不了）
                # 不该被算到这一类里——两种失败的含义完全不同。
                out['model_ignored'] = True
        res = chat_fn(role, msgs, **kwargs)
        content = res.get('content') if isinstance(res, dict) else res
        if isinstance(res, dict) and res.get('model'):
            out['model'] = str(res['model'])
        data = _extract_json(content)
        if not isinstance(data, dict):
            raise ValueError('模型返回内容不是合法 JSON')
        raw = data.get('relevance', data.get('score', data.get('rating')))
        score = _to_score(raw)
        if score is None:
            raise ValueError('模型未给出可识别的相关性评分: ' + str(raw)[:60])
        out['score'] = score
        out['reason'] = str(data.get('reason', data.get('explanation', '')) or '').strip()[:200]
    except Exception as e:  # noqa: BLE001
        out['error'] = str(e)[:200]
        out['score'] = None
    return out


def _failed_side(error: Any) -> dict:
    """构造一次"打分失败"的侧结果，形状与 _score_once 的返回值一致。"""
    return {'score': None, 'reason': '', 'model': None, 'error': str(error)[:200]}


def _score_side(chat_fn: Callable[..., dict], role: str, msgs: list[dict],
                model: Optional[str]) -> dict:
    """并发 worker：跑一次打分，并把 worker 自身的异常也兜住。

    兜异常的理由：并发下任何一次 worker 抛异常都不能掀翻整个函数，更不能
    让这篇文献从结果里消失。这里返回与 _score_once 同形的失败结果，并打上
    `worker_failed` 标记——上层据此把该篇当作"两个模型都失败"处理
    （保留原 relevance + 标记 relevance_cross_failed）。
    """
    try:
        return _score_once(chat_fn, role, msgs, model)
    except Exception as e:  # noqa: BLE001
        out = _failed_side(e)
        out['worker_failed'] = True
        return out


def _resolve_workers(requested: Optional[int], default: int) -> int:
    """把调用方给的并发度规整成 [1, _WORKERS_CAP] 之间的整数。

    上限是硬性的：模型服务商会限流，而且本仓库吃过教训——"并发打长文本
    会超时"。这里 prompt 短（只喂结构化字段），4~6 并发实测没问题，
    但也不能让调用方一把拉到几十把服务端打挂。
    """
    try:
        n = int(requested) if requested is not None else int(default)
    except (TypeError, ValueError):
        n = int(default)
    return max(1, min(n, _WORKERS_CAP))


def cross_check_relevance(evidence: list[dict],
                           chat_fn: Callable[..., dict],
                           model_a: Optional[str] = None,
                           model_b: Optional[str] = None,
                           *,
                           question: str = '',
                           role_a: str = 'reader',
                           role_b: str = 'critic',
                           max_workers: Optional[int] = None) -> list[dict]:
    """用两个不同模型各打一次相关性分，取平均。

    参数
    ----
    evidence : list[dict]    证据条目（不会被原地修改）
    chat_fn : callable       模型调用回调；**需要支持 model= 参数**。
                             签名形如 chat_fn(role, messages, temperature,
                             max_tokens, model=None)。
    model_a / model_b : str  指定两次打分分别用哪个模型。都为 None 时由调用方在
                             chat_fn 内部决定（此时默认走 reader / critic 两个角色，
                             对应两条不同的模型链）。
    question : str           核心问题，用于相关性打分。
    role_a / role_b : str    两次打分使用的路由角色。
    max_workers : int | None 并发度上限（同时在跑的模型调用数）。None 用默认值
                             CROSS_CHECK_WORKERS；实际值被硬性夹在 [1, _WORKERS_CAP]。

    返回**新的** evidence 列表，每条新增：
        relevance_cross       两模型平均分（保留 1 位小数；单边成功时为该侧分数）
        relevance_by_model    {'modelA': 分, 'modelB': 分}（失败侧为 None）
        relevance_divergent   两模型差 ≥2 分
        relevance_cross_failed 两个模型都失败
        _relevance_original   原来的 relevance
    额外记录（便于审计）：
        relevance_models      两次实际使用的模型名
        relevance_same_model  两侧实际用了同一个模型（交叉验证形同虚设）
        relevance_model_ignored  调用方指定的模型没能生效（chat_fn 不支持 model）

    并发说明
    --------
    这一步原来是最慢的一环（N 篇 × 2 个模型 = 2N 次调用，全串行，实测 5 篇
    要 458 秒），现在改成内部并行。

    **同一篇文献的两次打分之间没有依赖**：`_score_once` 只吃
    (chat_fn, role, msgs, model)，返回 score/reason/model/error，不读对方的
    结果；两侧喂进去的是**同一份** msgs，且 `_evidence_card` 刻意不输出
    relevance、系统提示也写明"独立打分"——就是为了避免第二个模型跟着第一个
    走。所以 2N 次调用可以全部铺平进同一个线程池，不必"一篇一个任务、篇内
    串行两次"（后者在 N 为奇数时会多空转一轮）。

    **返回顺序必须与输入顺序一致**：并发下完成顺序是乱的（谁先返回取决于
    网络），所以结果按 (篇序号, 打分侧) 收进字典，再按篇序号顺序组装——
    上游 pipeline 依赖这个顺序做后续排序与展示。
    """
    if not evidence or not isinstance(evidence, (list, tuple)):
        return []

    # 先在主线程把 prompt 建好（纯本地计算，不涉及模型调用）。
    # 同时筛掉非 dict 条目——与原串行版的过滤规则保持一致。
    items: list[tuple[int, dict, list[dict]]] = []
    for ev in evidence:
        if not isinstance(ev, dict):
            continue
        card = _evidence_card(ev)
        msgs = [
            {'role': 'system', 'content': RELEVANCE_SCORE_SYSTEM},
            {'role': 'user', 'content': RELEVANCE_SCORE_USER.format(
                question=question or '（未提供核心问题，请依据文献信息卡本身判断其对'
                                     '一个循证医学问题的相关性）',
                card=card)},
        ]
        items.append((len(items), ev, msgs))

    if not items:
        return []

    workers = _resolve_workers(max_workers, CROSS_CHECK_WORKERS)

    # 把 2N 次打分全部摊平成任务：A、B 两侧互不依赖，可以真正并发。
    # 每侧一个任务 → 负载更均衡（N 为奇数时不会多空转一轮）。
    tasks: list[tuple[int, str]] = []
    for pos, _ev, _msgs in items:
        tasks.append((pos, 'a'))
        tasks.append((pos, 'b'))

    # 结果按 (篇序号, 侧) 收集，最后按篇序号顺序组装——并发下完成顺序是乱的，
    # 直接按完成顺序 append 会让返回列表的顺序乱掉，上游依赖顺序做后续处理。
    scores: dict[tuple[int, str], dict] = {}
    by_pos = {pos: msgs for pos, _ev, msgs in items}
    with ThreadPoolExecutor(max_workers=min(workers, len(tasks))) as ex:
        futs = {}
        for pos, side in tasks:
            role = role_a if side == 'a' else role_b
            model = model_a if side == 'a' else model_b
            # 给每个任务一份独立的 messages 列表副本：并发下不想让两个
            # 任务共用同一个 list 对象（万一 chat_fn 内部动了它）。
            futs[ex.submit(_score_side, chat_fn, role, list(by_pos[pos]), model)] = (pos, side)
        for fut in as_completed(futs):
            key = futs[fut]
            try:
                scores[key] = fut.result()
            except Exception as e:  # noqa: BLE001
                # worker 兜底：理论上 _score_side 已经吞掉了异常，这里是
                # 最后一道防线——绝不让一篇文献因为并发而整个消失。
                failed = _failed_side(e)
                failed['worker_failed'] = True
                scores[key] = failed

    out: list[dict] = []
    for pos, ev, _msgs in items:
        a = scores.get((pos, 'a'), _failed_side('未执行'))
        b = scores.get((pos, 'b'), _failed_side('未执行'))

        new = dict(ev)
        original = ev.get('relevance')
        new['_relevance_original'] = original

        # worker 自身抛异常（不是模型调用失败）时，按"该篇两个模型都失败"处理：
        # 宁可保守，也不能因为并发把半成品结果当成可信分数用出去。
        if a.get('worker_failed') or b.get('worker_failed'):
            a = _failed_side(a.get('error') or 'worker 执行失败')
            b = _failed_side(b.get('error') or 'worker 执行失败')

        new['relevance_by_model'] = {'modelA': a['score'], 'modelB': b['score']}
        new['relevance_models'] = {'modelA': a['model'], 'modelB': b['model']}
        new['relevance_same_model'] = bool(
            a['model'] and b['model'] and a['model'] == b['model'])
        # 调用方没能指定模型（例如 chat_fn 不接受 model 参数）→ 两次打分可能
        # 来自同一个模型，交叉验证就退化成了"同一个人打两遍分"。记下来，
        # 让上层能判断这次交叉验证还有没有意义。
        new['relevance_model_ignored'] = bool(a.get('model_ignored')
                                               or b.get('model_ignored'))
        # 某一侧调用失败（网络、超时、返回解析不了）——与"模型没生效"是两回事，
        # 单独记一个字段，别混在一起让人误判。
        new['relevance_side_failed'] = bool(a['error'] or b['error'])

        if a['score'] is not None and b['score'] is not None:
            new['relevance_cross'] = round((a['score'] + b['score']) / 2.0, 1)
            new['relevance_divergent'] = abs(a['score'] - b['score']) >= 2
            new['relevance_cross_failed'] = False
        elif a['score'] is not None:
            # 单边失败 → 用另一边的结果，不整条作废
            new['relevance_cross'] = round(a['score'], 1)
            new['relevance_divergent'] = False
            new['relevance_cross_failed'] = False
        elif b['score'] is not None:
            new['relevance_cross'] = round(b['score'], 1)
            new['relevance_divergent'] = False
            new['relevance_cross_failed'] = False
        else:
            # 两边都失败 → 保留原分并标记失败（绝不因此丢掉这篇文献）
            new['relevance_cross'] = original
            new['relevance_divergent'] = False
            new['relevance_cross_failed'] = True
        out.append(new)
    return out


# ============================================================================
# 公开 API 4：交叉验证带来的信息量
# ============================================================================
def cross_check_gain(evidence_before: list[dict],
                     evidence_after: list[dict]) -> dict:
    """统计交叉验证相对原始评分带来的变化，用于写进 trace。

    changed    : 交叉分与原分不同的条目数
    avg_delta  : 各条目 (交叉分 - 原分) 的平均绝对值（保留 2 位小数），
                 只对**交叉成功**的条目求平均（两模型都失败的条目原分照旧，
                 计入 failed 而不计入均值）
    divergent  : 两模型分歧（差 ≥2 分）的条目数
    failed     : 两个模型都失败的条目数
    """
    before_by_key: dict[str, dict] = {}
    for ev in evidence_before or []:
        if not isinstance(ev, dict):
            continue
        key = str(ev.get('_citation_key') or ev.get('pmid') or ev.get('_doi') or '')
        if key:
            before_by_key[key] = ev

    changed = 0
    divergent = 0
    failed = 0
    deltas: list[float] = []

    for ev in evidence_after or []:
        if not isinstance(ev, dict):
            continue
        if ev.get('relevance_divergent'):
            divergent += 1
        if ev.get('relevance_cross_failed'):
            failed += 1
            continue
        cross = ev.get('relevance_cross')
        if cross is None:
            continue
        original = ev.get('_relevance_original')
        if original is None:
            key = str(ev.get('_citation_key') or ev.get('pmid') or ev.get('_doi') or '')
            prev = before_by_key.get(key)
            original = prev.get('relevance') if prev else ev.get('relevance')
        try:
            delta = abs(float(cross) - float(original))
        except (TypeError, ValueError):
            continue
        deltas.append(delta)
        if delta >= 0.05:
            changed += 1

    avg = round(sum(deltas) / len(deltas), 2) if deltas else 0.0
    return {'changed': changed, 'avg_delta': avg,
            'divergent': divergent, 'failed': failed}
