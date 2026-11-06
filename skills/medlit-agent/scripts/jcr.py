# -*- coding: utf-8 -*-
"""JCR 影响因子 / 分区查询：把期刊名（或 ISSN）解析成期刊元数据。

为什么需要这一层
----------------
检索结果要能告诉用户「这是 Lancet，IF 109，JCR Q1，中科院 1 区」，
否则「分区」对读者没有意义（尤其在国内评价体系里，JCR 分位与中科院大区
是两套东西，混在一列是最常见的误读来源）。

对齐靠什么
----------
**首选 ISSN，不是刊名**。PubMed 的 `Journal/Title` 是官方全称，
和 JCR 导出表的写法经常对不上：

    PubMed: "JAMA"                JCR: "JAMA-JOURNAL OF THE AMERICAN MEDICAL ASSOCIATION"
    PubMed: "N Engl J Med"        JCR: "NEW ENGLAND JOURNAL OF MEDICINE"
    PubMed: "N Engl J Med"        JCR 缩写: "NEW ENGL J MED"

刊名只能靠模糊匹配，命中率不可靠；ISSN 是唯一稳定的连接键。
匹配顺序（先准后糊，宁可查不到也不给错的）：

    1. ISSN 精确         —— 最可靠
    2. 刊名规范化精确     —— 大小写/括号/标点差异
    3. 缩写刊名映射       —— MedlineTA ≈ ISO 缩写
    4. 前缀包含（唯一命中）—— "JAMA" ⊂ "JAMA-journal of ..."；多命中视为歧义，放弃
    5. 模糊相似度         —— difflib + 分词重合，需过阈值

失败时返回 `{'matched': False}`，**绝不猜**。分区猜错比查不到危害大得多：
用户会据此判断证据质量。

数据来源
--------
assets/jcr_2026_index.json 等，由 scripts/build_jcr_index.py 从
JCR 2026 导出表 + 中科院分区表生成。索引文件缺失时本模块静默降级
（检索照常跑，只是没有分区标注）。
"""
from __future__ import annotations

import difflib
import json
import os
import re
import threading

__all__ = ['JcrIndex', 'get_index', 'lookup', 'format_label', 'norm_title']

_ASSETS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'assets')
_INDEX_FILE = os.path.join(_ASSETS, 'jcr_2026_index.json')
_ISSN_FILE = os.path.join(_ASSETS, 'jcr_2026_index_issn_map.json')
_ABBREV_FILE = os.path.join(_ASSETS, 'jcr_2026_index_abbrev_map.json')

# 模糊匹配的相似度门槛。调低会开始出现「张冠李戴」——
# 阈值宁可保守，查不到比给错强。
_FUZZY_CUTOFF = 0.86
# 前缀包含匹配时，短的那一方至少要有这么多个字符才算数
_PREFIX_MIN_LEN = 5

_lock = threading.Lock()
_singleton: 'JcrIndex | None' = None


# ---------------- 名称规范化 ----------------
_PAREN = re.compile(r'\([^)]*\)')
_BRACKET = re.compile(r'\[[^\]]*\]')
_NONALNUM = re.compile(r'[^0-9a-z& ]+')
_WS = re.compile(r'\s+')


def norm_title(s: str) -> str:
    """刊名规范化：去括号内容、去标点、小写、& 统一成 and、剥开头冠词。

    两边（索引键与查询值）走同一个函数，保证一致。

    **剥冠词是必须的**：PubMed 的 `Journal/Title` 保留英文冠词，
    而 JCR 导出表不带：

        PubMed: "The Journal of clinical endocrinology and metabolism"
        JCR:    "JOURNAL OF CLINICAL ENDOCRINOLOGY AND METABOLISM"

    不剥的话这两种写法差一个 "the "，精确匹配必然落空。
    """
    s = str(s or '').strip().lower()
    s = _PAREN.sub(' ', s)
    s = _BRACKET.sub(' ', s)
    s = s.replace('&', ' and ')
    s = _NONALNUM.sub(' ', s)
    s = _WS.sub(' ', s).strip()
    # 剥开头的冠词（最多剥一层，"The the X" 不会有）
    for art in ('the ', 'a ', 'an '):
        if s.startswith(art):
            s = s[len(art):].strip()
            break
    return s


def norm_issn(s: str) -> str:
    """ISSN 规范化：去掉连字符与空格，统一大写。"""
    return re.sub(r'[^0-9Xx]', '', str(s or '')).upper()


def _tokens(norm: str) -> set:
    """分词（去掉英文停用词），用于模糊匹配时的重合度计算。"""
    stop = {'the', 'of', 'and', 'a', 'an', 'for', 'in', 'on', 'to',
            'journal', 'jnl', 'zeitschrift', 'revue'}
    return {w for w in norm.split() if w and w not in stop}


# ---------------- 索引 ----------------
class JcrIndex:
    """期刊元数据索引。加载一次，之后所有查询走内存。"""

    def __init__(self, index_path: str = '', issn_path: str = '',
                 abbrev_path: str = ''):
        self.index_path = index_path or _INDEX_FILE
        self.issn_path = issn_path or _ISSN_FILE
        self.abbrev_path = abbrev_path or _ABBREV_FILE
        self.raw: dict = {}
        self.meta: dict = {}
        self.by_norm: dict = {}
        self.by_issn: dict = {}
        self.by_abbrev: dict = {}
        self.by_base: dict = {}
        self._prefix_cache: dict = {}
        self._abbrev_keys: list = []
        self._loaded = False
        self.load()

    # ---- 加载 ----
    def load(self) -> bool:
        if self._loaded:
            return bool(self.raw)
        self._loaded = True
        data = _read_json(self.index_path)
        self.meta = data.get('_meta') or {}
        # `_meta` 不是期刊条目，加载时要跳过
        self.raw = {k: v for k, v in data.items()
                    if not k.startswith('_') and isinstance(v, dict)}
        self.by_norm = {}
        for name, rec in self.raw.items():
            key = norm_title(name)
            if key and key not in self.by_norm:
                self.by_norm[key] = (name, rec)
        # ISSN 与缩写刊名**折在条目里**，一次遍历建完映射。
        # （早先是两个独立的映射文件，加起来比主索引还大。）
        self.by_issn = {}
        self.by_abbrev = {}
        for name, rec in self.raw.items():
            for issn in (rec.get('issn'), rec.get('issn_e')):
                k = norm_issn(issn or '')
                if k:
                    self.by_issn.setdefault(k, name)
            abbr = norm_title(rec.get('abbr') or '')
            if abbr:
                self.by_abbrev.setdefault(abbr, name)
        self._abbrev_keys = list(self.by_abbrev.keys())
        self.by_base = self._build_base_index()
        return bool(self.raw)

    def _build_base_index(self) -> dict:
        """主刊名索引：JCR 把「主刊名-副标题」写成一条，中间是连字符。

            "JAMA-JOURNAL OF THE AMERICAN MEDICAL ASSOCIATION"
            "BMJ-British Medical Journal"

        而 PubMed 的刊名往往只有主刊名（"JAMA"、"BMJ"），
        精确匹配必然落空。按主刊名建一份索引就能救回来。

        歧义处理：
        - 同一主刊名下只有一本  → 直接用
        - 有多本，但其中有一本**刊名本身就等于主刊名**（无副标题）→ 用它
          （例如 "Radiology" 与 "Radiology-Artificial Intelligence" 同族，
            前者才是正解）
        - 否则判歧义，放弃（"JACC" 下面挂着 8 本子刊，猜不得）
        """
        cands: dict = {}
        plain: dict = {}
        for name in self.raw:
            if '-' in name:
                base = name.split('-', 1)[0].strip().lower()
                cands.setdefault(base, []).append(name)
            key = norm_title(name)
            if key and '-' not in name:
                plain.setdefault(key, name)
        out: dict = {}
        for base, names in cands.items():
            if len(names) == 1:
                out[base] = names[0]
            elif base in plain and plain[base] in names:
                out[base] = plain[base]
            elif base in plain:
                out[base] = plain[base]
        return out

    @property
    def available(self) -> bool:
        return bool(self.raw)

    # ---- 查询 ----
    def lookup(self, journal: str = '', issn: str = '',
               issn_e: str = '') -> dict:
        """查一个期刊。返回 dict，**查不到时 `matched` 为 False**。

        返回字段：if / quartile / cas_zone / cas_top / categories /
        journal（索引里的规范刊名）/ match（匹配方式）/ matched
        """
        miss = {'matched': False, 'journal': str(journal or '').strip(),
                'match': 'none', 'if': None, 'quartile': None,
                'cas_zone': None, 'cas_top': None, 'categories': []}
        if not self.raw:
            return miss

        # 1) ISSN 精确
        for cand in (issn, issn_e):
            key = norm_issn(cand)
            if not key:
                continue
            name = self.by_issn.get(key)
            if name and name in self.raw:
                return self._hit(name, self.raw[name], 'issn')

        norm = norm_title(journal)
        if not norm:
            return miss

        # 2) 刊名规范化精确
        hit = self.by_norm.get(norm)
        if hit:
            return self._hit(hit[0], hit[1], 'title')

        # 3) 缩写刊名映射
        abbr = self.by_abbrev.get(norm)
        if abbr and abbr in self.raw:
            return self._hit(abbr, self.raw[abbr], 'abbrev')

        # 3.5) 主刊名（JCR 的「主刊名-副标题」写法 vs PubMed 的短刊名）
        base = self.by_base.get(norm)
        if base and base in self.raw:
            return self._hit(base, self.raw[base], 'base')

        # 4) 前缀包含（必须唯一命中，否则视为歧义）
        cands = self._prefix_candidates(norm)
        if len(cands) == 1:
            name = cands[0]
            return self._hit(name, self.raw[name], 'prefix')
        if len(cands) > 1:
            # 歧义：不再往下猜，宁可查不到
            return miss

        # 5) 模糊
        best_name, best_score = self._fuzzy(norm)
        if best_name and best_score >= _FUZZY_CUTOFF:
            return self._hit(best_name, self.raw[best_name], 'fuzzy')
        # 5.5) 缩写刊名模糊（PubMed 的 MedlineTA 与 JCR 的 ISO 缩写风格不同：
        #      "N Engl J Med" vs "NEW ENGL J MED"）
        abbr_name, abbr_score = self._fuzzy_abbrev(norm)
        if abbr_name and abbr_score >= _FUZZY_CUTOFF:
            return self._hit(abbr_name, self.raw[abbr_name], 'abbrev_fuzzy')
        return miss

    # ---- 内部 ----
    def _hit(self, name: str, rec: dict, how: str) -> dict:
        return {
            'matched': True,
            'journal': name,
            'match': how,
            'if': rec.get('if'),
            'quartile': rec.get('quartile'),
            'cas_zone': rec.get('cas_zone'),
            'cas_top': rec.get('cas_top'),
            'categories': rec.get('categories') or [],
            'source': self.meta.get('source', ''),
        }

    def _prefix_candidates(self, norm: str) -> list:
        """找出规范刊名里以 norm 开头的那些（或以 norm 为前缀的那些）。

        只在「norm 至少 _PREFIX_MIN_LEN 个字符」时才考虑，
        否则 "Cancer" 会命中几十本刊。
        """
        if norm in self._prefix_cache:
            return self._prefix_cache[norm]
        out: list = []
        if len(norm) >= _PREFIX_MIN_LEN:
            for key, (name, _rec) in self.by_norm.items():
                if key.startswith(norm + ' '):
                    out.append(name)
        self._prefix_cache[norm] = out
        return out

    def _fuzzy(self, norm: str) -> tuple:
        """模糊匹配：difflib 序列相似度 + 分词重合度，取两者较优。"""
        qtok = _tokens(norm)
        best_name, best_score = '', 0.0
        # 先用首词缩小候选，避免对 2 万多条做全量 difflib（太慢）
        head = norm.split()[0] if norm.split() else ''
        candidates = list(self.by_norm.items())
        if head:
            narrowed = [(k, v) for k, v in candidates if k.startswith(head)]
            if len(narrowed) >= 3:
                candidates = narrowed
        for key, (name, _rec) in candidates:
            score = difflib.SequenceMatcher(None, norm, key).ratio()
            if score < 0.6:
                ktok = _tokens(key)
                if qtok and ktok:
                    score = len(qtok & ktok) / len(qtok | ktok)
            if score > best_score:
                best_name, best_score = name, score
        return best_name, best_score

    def _fuzzy_abbrev(self, norm: str) -> tuple:
        """在缩写刊名表里做模糊匹配。

        PubMed 的 MedlineTA 与 JCR 的 ISO 缩写风格不同：
        "N Engl J Med" vs "NEW ENGL J MED"。精确匹配救不了，只能靠相似度。
        """
        best_name, best_score = '', 0.0
        qtok = _tokens(norm)
        for key in self._abbrev_keys:
            score = difflib.SequenceMatcher(None, norm, key).ratio()
            if score < 0.6:
                ktok = _tokens(key)
                if qtok and ktok:
                    score = len(qtok & ktok) / len(qtok | ktok)
            if score > best_score:
                best_name, best_score = self.by_abbrev[key], score
        return best_name, best_score


def _read_json(path: str) -> dict:
    try:
        with open(path, 'r', encoding='utf-8') as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def get_index() -> JcrIndex:
    """全局单例（线程安全）。首次调用时才真正读盘。"""
    global _singleton
    if _singleton is None:
        with _lock:
            if _singleton is None:
                _singleton = JcrIndex()
    return _singleton


def lookup(journal: str = '', issn: str = '', issn_e: str = '') -> dict:
    """便捷函数：查一个期刊。"""
    return get_index().lookup(journal=journal, issn=issn, issn_e=issn_e)


# ---------------- 展示 ----------------
def format_label(info: dict) -> str:
    """把查询结果拼成一句人话，用于证据表与答案。

    例：「IF 109.0 · JCR Q1 · 中科院 1 区」
    """
    if not info or not info.get('matched'):
        return ''
    bits = []
    jif = info.get('if')
    if jif is not None:
        bits.append(f'IF {jif}')
    q = info.get('quartile')
    if q:
        bits.append(f'JCR {q}')
    z = info.get('cas_zone')
    if z:
        top = 'Top' if info.get('cas_top') else ''
        bits.append(f'中科院 {z} 区{top}')
    return ' · '.join(bits)


if __name__ == '__main__':
    import sys
    idx = get_index()
    print(f'索引条目 {len(idx.raw)}｜ISSN 映射 {len(idx.by_issn)}｜'
          f'缩写映射 {len(idx.by_abbrev)}')
    for q in sys.argv[1:]:
        info = idx.lookup(journal=q)
        print(f'  {q!r:45s} → {info.get("matched")} '
              f'{format_label(info) or "-"} [{info.get("match")}]')
