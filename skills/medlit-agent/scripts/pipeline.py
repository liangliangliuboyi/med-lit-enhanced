# -*- coding: utf-8 -*-
"""主流程：规划 → 检索（含预检修订）→ 链接扩展 → 题录 → 过滤 → 重排 → 精读 → 审查 → 成文 → 校验 → 落盘。

设计原则：模型只做需要判断力的事；计数、过滤、排序、拼接、校验、定级全部在程序侧。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import nullcontext
from datetime import datetime

import config as cfgmod
import grading
import prompts
import pubmed as pm
import sf

try:
    import meta as metamod
except Exception:  # noqa: BLE001
    metamod = None
try:
    import preprints as preprintmod
except Exception:  # noqa: BLE001
    preprintmod = None
try:
    import verify as verifymod          # B5 语义比对 + B8 相关度交叉验证
except Exception:  # noqa: BLE001
    verifymod = None
try:
    import compare as comparemod        # N5 干预横向对比
except Exception:  # noqa: BLE001
    comparemod = None
try:
    import cache as cachemod            # N8 检索缓存
except Exception:  # noqa: BLE001
    cachemod = None
try:
    import pricing as pricingmod        # N9 分档计价
except Exception:  # noqa: BLE001
    pricingmod = None


# ---------------- 通用工具 ----------------
def load_config(path: str | None = None) -> dict:
    return cfgmod.load_config(path)


def _slug(text: str, n: int = 16) -> str:
    """生成目录名片段。

    末尾附加短哈希：同一分钟内跑两个相似问题（例如同一个课题的不同子问题）时，
    仅靠前 n 个字符会撞目录、后者覆盖前者。
    """
    t = re.sub(r'[^\w\u4e00-\u9fff]+', '', text or '')
    t = t[:n] or 'query'
    h = hashlib.md5((text or '').encode('utf-8')).hexdigest()[:4]
    return f'{t}_{h}'


def _json_load(text: str):
    """从模型输出里抠 JSON（容忍 ```json 包裹和前后噪音）。

    注意：不能用贪婪正则 `\\{.*\\}` —— 模型若在 JSON 前后夹带含花括号的说明文字，
    贪婪匹配会吞掉整段导致解析失败。这里用括号配对扫描，能正确取出最外层结构。
    """
    if not text:
        return None
    t = text.strip()
    t = re.sub(r'^```(?:json)?\s*', '', t)
    t = re.sub(r'\s*```$', '', t)
    try:
        return json.loads(t)
    except Exception:  # noqa: BLE001
        pass
    # 括号配对扫描：找到第一个 { 或 [，配对到对应的收尾括号
    for opener, closer in (('{', '}'), ('[', ']')):
        start = t.find(opener)
        if start < 0:
            continue
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
            elif ch == opener:
                depth += 1
            elif ch == closer:
                depth -= 1
                if depth == 0:
                    chunk = t[start:i + 1]
                    try:
                        return json.loads(chunk)
                    except Exception:  # noqa: BLE001
                        break
    return None


class Run:
    def __init__(self, question: str, constraints: str = '', cfg: dict | None = None,
                 cq_explicit: list[str] | None = None,
                 history: list[dict] | None = None,
                 resume_dir: str | None = None,
                 reuse_queries: list[dict] | None = None):
        self.cfg = cfg or cfgmod.load_config()
        self.question = (question or '').strip()
        self.constraints = (constraints or '').strip()
        self.history = history or []
        # 沿用上一轮用过的检索式（连续提问模式）。
        # 追问同一课题时重新做检索，白花钱还会捞到不同的文献；
        # 沿用旧检索式 = 保留上次的召回，再叠加新问题的概念。
        self.reuse_queries = reuse_queries or []
        self.t0 = time.time()
        self.stamp = datetime.now().strftime('%Y%m%d-%H%M')

        # ---- 输出目录（断点续跑时复用原目录）----
        root = self.cfg['paths'].get('runs_root') or './runs'
        if resume_dir:
            self.dir = os.path.abspath(resume_dir)
        else:
            self.dir = os.path.join(os.path.abspath(root), f'{self.stamp}_{_slug(self.question)}')
        os.makedirs(os.path.join(self.dir, '06_fulltext'), exist_ok=True)

        # ---- 模型端点与路由 ----
        prov = self.cfg.get('provider') or {}
        self.provider = sf.Provider(
            name=prov.get('name', 'provider'),
            base_url=prov.get('base_url', ''),
            api_key=prov.get('api_key', ''),
            timeout=int(self.cfg['runtime'].get('timeout', 180)),
            retries=int(self.cfg['runtime'].get('max_retries', 3)),
        )
        self.router = sf.ModelRouter(self.provider, self.cfg['models']['chain'],
                                    call_budget=int(
                                        (self.cfg.get('runtime') or {}).get(
                                            'model_call_budget', 240)))

        # ---- PubMed ----
        self.pubmed = pm.PubMed(
            api_key=self.cfg['pubmed'].get('api_key', ''),
            tool=self.cfg['pubmed'].get('tool', 'medlit-agent'),
            email=self.cfg['pubmed'].get('email', ''),
        )

        # ---- Clinical Query（仅用户显式指定时启用）----
        self.cq_all = self._load_cq()
        self.cq_explicit = list(cq_explicit or [])
        self.cq_used: list[str] = []
        self.search_failed = False
        self._search_latency: list[float] = []

        # ---- 状态 ----
        self.papers: dict[str, dict] = {}      # PMID → PubMed 题录
        self.preprints: dict[str, dict] = {}   # 预印本 id（doi:xxx）→ 题录
        self.evidence: list[dict] = []
        self.excluded: list[dict] = []          # C6：带排除理由
        self.search_log: list[dict] = []
        self.translations: list[dict] = []   # N2：PubMed 对检索式的解读
        self.plan: dict = {}
        self.critique: dict = {}
        self.answer = ''
        self.pmid_check: dict = {}
        self.pooled: list[dict] = []            # B4：合并效应量
        self.mech_grading: dict = {}           # B1：机械定级
        self.prisma = {
            'identified': 0, 'deduplicated': 0, 'screened': 0,
            'excluded_screening': 0, 'fulltext_assessed': 0,
            'included': 0, 'excluded_fulltext': 0, 'queries': 0,
            'preprints': 0, 'linked': 0, 'snowballed': 0,
        }
        self.trace = {
            'question': self.question, 'constraints': self.constraints,
            'timings': {}, 'models': {}, 'errors': [], 'counts': {},
            'tokens': {}, 'cost_estimate': {},
            'provider': {'name': self.provider.name, 'base_url': self.provider.base},
            'scope_on': False,
            'scope_note': '本工具检索全 PubMed（英文国际文献），不限刊源、不限学科；'
                          '不检索中文数据库',
            'clinical_queries': {'used': list(self.cq_used)},
        }
        self._log_lines: list[str] = []
        self._ft_lock = threading.Lock()
        self._ft_claimed = 0
        self._token_lock = threading.Lock()
        self.cache = None
        self.cache_stats = None
        self._checkpoint: dict = {}

        if resume_dir:
            self._load_checkpoint()

    # ---------- 工具 ----------
    def _load_cq(self) -> dict:
        """载入 PubMed 官方 Clinical Query 过滤器表（仅供 --cq 显式调用）。

        读不到文件时不能静默返回空——那会让 --cq 看起来"生效了但没效果"。
        """
        path = os.path.join(cfgmod.ROOT, 'data', 'clinical_queries.json')
        try:
            with open(path, encoding='utf-8') as f:
                data = json.load(f)
        except FileNotFoundError:
            self.log(f'!! 找不到临床查询过滤器表：{path}（--cq 将不可用）')
            return {}
        except Exception as e:  # noqa: BLE001
            self.log(f'!! 临床查询过滤器表读取失败：{e}（--cq 将不可用）')
            return {}
        return {k: v for k, v in data.items() if not k.startswith('_')}

    def log(self, msg: str):
        line = f'[{time.strftime("%H:%M:%S")}] {msg}'
        print(line, flush=True)
        self._log_lines.append(line)

    def err(self, stage: str, e):
        self.trace['errors'].append({'stage': stage, 'err': str(e)[:400]})
        self.log(f'!! {stage} 出错：{e}')

    def _track_tokens(self, role: str, res: dict):
        """D3：记录每角色 token 用量与估算费用。

        精读阶段是多线程并发的，所以计数与计价都要加锁——
        不加锁可能丢计数，或者算出半成品成本。

        模型名必须在这里统一记录：漏记 reader 会让占 token 大头的精读开销
        全部算成「无单价」，成本估算直接低估一个量级（实测 76% 的 token
        因为没记模型名而没被计价）。
        """
        # 测试里常用 Run.__new__ 绕过 __init__，那时没有锁
        lock = getattr(self, '_token_lock', None)
        ctx = lock if lock is not None else nullcontext()
        with ctx:
            model = res.get('model')
            if model:
                self.trace['models'][role] = model
            n = res.get('tokens')
            if n:
                self.trace['tokens'][role] = self.trace['tokens'].get(role, 0) + int(n)
            if pricingmod:
                # N9：按模型分档计价，不同模型价格差很多
                table = (self.cfg.get('runtime') or {}).get('price_table')
                usage = dict(self.trace['tokens'])
                role_models = dict(self.trace.get('models') or {})
                try:
                    self.trace['cost_estimate'] = pricingmod.estimate_cost(
                        usage, table, role_models)
                except Exception as e:  # noqa: BLE001
                    # 计价失败不能静默吞掉——否则用户以为没花钱
                    self.trace['cost_estimate'] = {'error': str(e)[:200]}
                return
            price = (self.cfg.get('runtime') or {}).get('price_per_million_tokens')
            if price and self.trace['tokens']:
                total = sum(self.trace['tokens'].values())
                self.trace['cost_estimate'] = {
                    'total_tokens': total,
                    'estimated_cny': round(total / 1_000_000 * price, 4),
                    'price_per_million_tokens': price,
                }

    # ---------- 断点续跑（D4）----------
    def _ckpt_path(self) -> str:
        return os.path.join(self.dir, '_checkpoint.json')

    def _save_checkpoint(self):
        try:
            self._checkpoint = {
                'plan': self.plan, 'search_log': self.search_log,
                'papers': list(self.papers.values()),
                'preprints': list(self.preprints.values()),
                'excluded': self.excluded, 'prisma': self.prisma,
                'evidence': [{k: v for k, v in d.items() if not k.startswith('_')}
                             for d in self.evidence],
                'critique': self.critique, 'answer': self.answer,
                'pooled': self.pooled, 'mech_grading': self.mech_grading,
                'trace': self.trace,
            }
            with open(self._ckpt_path(), 'w', encoding='utf-8') as f:
                json.dump(self._checkpoint, f, ensure_ascii=False, indent=1)
        except Exception as e:  # noqa: BLE001
            self.err('checkpoint', e)

    def _load_checkpoint(self):
        p = self._ckpt_path()
        if not os.path.exists(p):
            self.log(f'! 未找到断点文件 {p}，从头开始')
            return
        try:
            with open(p, encoding='utf-8') as f:
                ck = json.load(f)
        except Exception as e:  # noqa: BLE001
            self.err('checkpoint-load', e)
            return
        self.plan = ck.get('plan', {})
        self.search_log = ck.get('search_log', [])
        for p2 in ck.get('papers', []):
            if p2.get('pmid'):
                self.papers[p2['pmid']] = p2
        for rec in ck.get('preprints', []):
            if rec.get('id'):
                self.preprints[rec['id']] = rec
        self.excluded = ck.get('excluded', [])
        self.prisma.update(ck.get('prisma', {}))
        self.evidence = ck.get('evidence', [])
        self.critique = ck.get('critique', {})
        self.answer = ck.get('answer', '')
        self.pooled = ck.get('pooled', [])
        self.mech_grading = ck.get('mech_grading', {})
        self.trace.update(ck.get('trace', {}))
        self.log(f'↻ 已载入断点：候选 {len(self.papers)} 篇，证据 {len(self.evidence)} 篇')

    # ---------- 1. 规划 ----------
    def plan_queries(self) -> dict:
        hist = ''
        if self.history:
            prev = '；'.join(h.get('question', '') for h in self.history[-3:])
            hist = (f'\n\n【对话历史】用户此前连续提问：{prev}\n'
                    f'本次问题若是追问，请在检索式中保留必要的前序概念，不要重复问清。')
        cq_opts = '\n'.join(f'  - {k}：{v["label"]}' for k, v in self.cq_all.items())
        msgs = [{'role': 'system', 'content': prompts.PLANNER_SYSTEM},
                {'role': 'user', 'content': prompts.PLANNER_USER.format(
                    question=self.question + hist,
                    constraints=self.constraints or '（无）',
                    cq_options=cq_opts or '  （无）')}]
        res = self.router.chat('planner', msgs,
                               temperature=self.cfg['runtime']['temperature_plan'],
                               max_tokens=1600)
        self.trace['timings']['plan'] = res['elapsed']
        self.trace['models']['planner'] = res['model']
        self._track_tokens('planner', res)
        data = _json_load(res['content']) or {}
        if not data.get('queries'):
            data = {'core_question_en': self.question,
                    'queries': [{'label': '直译', 'query': self.question}],
                    'pico': {}, 'notes': '模型未给出检索式，已回退为原问题'}
        # R12：把上一轮的检索式并进来（去重）。
        # 放在这里而不是让 planner 自己回忆，是因为 planner 看不到上一轮
        # 具体用过什么检索式——只有程序知道。
        if self.reuse_queries:
            have = {(q.get('query') or '').strip() for q in data.get('queries', [])}
            added = 0
            for q in self.reuse_queries:
                qq = (q.get('query') or '').strip()
                if qq and qq not in have:
                    data.setdefault('queries', []).append(
                        {'label': q.get('label') or '沿用', 'query': qq})
                    have.add(qq)
                    added += 1
            if added:
                self.log(f'   · 沿用上一轮的 {added} 条检索式'
                         f'（连续提问模式：保留上次的召回，再叠加新问题的概念）')
        self.plan = data
        self.log(f'① 规划完成：{len(data.get("queries", []))} 条检索式'
                 f'（{res["model"]}，{res["elapsed"]}s）')
        for q in data.get('queries', []):
            self.log(f'   · [{q.get("label")}] {q.get("query", "")[:110]}')
        # N7：planner 建议的过滤器只提示，不自动生效
        sug = data.get('suggested_cq') or ''
        if sug:
            if sug in self.cq_all:
                self.log(f'   · 建议的临床查询过滤器：{sug}（{self.cq_all[sug]["label"]}）'
                         f'—— 仅作建议，未自动生效；要用请加 --cq {sug}')
            else:
                self.log(f'   · planner 给出的过滤器 id 不认识，已忽略：{sug}')
                data['suggested_cq'] = ''
        return data

    def revise_queries(self, failed: list[dict]) -> list[dict]:
        """A1：检索式命中 0 时让 planner 换一版。"""
        used = '\n'.join(f'- {q.get("query", "")}' for q in failed)
        msgs = [{'role': 'system', 'content': prompts.PLANNER_REVISE_SYSTEM},
                {'role': 'user', 'content': prompts.PLANNER_REVISE_USER.format(
                    core_question=self.plan.get('core_question_en') or self.question,
                    failed_queries=used,
                    constraints=self.constraints or '（无）')}]
        try:
            res = self.router.chat('planner', msgs,
                                   temperature=0.3, max_tokens=1000)
        except Exception as e:  # noqa: BLE001
            self.err('revise', e)
            return []
        self._track_tokens('planner', res)
        data = _json_load(res['content']) or {}
        qs = data.get('queries') or []
        self.log(f'   修订检索式 {len(qs)} 条（{res["model"]}）：{data.get("reason", "")}')
        for q in qs:
            self.log(f'   · [修订] {q.get("query", "")[:110]}')
        return qs

    # ---------- 2. 检索 ----------
    def _esearch(self, term: str, retmax: int = 20,
                 mindate: str | None = None) -> dict:
        """N8：带缓存的检索。同一检索式重复跑不再打 NCBI 的 API。"""
        if not cachemod or (self.cfg['retrieval'] or {}).get('no_cache'):
            # --no-cache：完全不走缓存。同一课题反复跑时，缓存的 72 小时 TTL
            # 会让两次跑拿到不同结果——要复现就把它关掉。
            return self.pubmed.esearch(term, retmax=retmax, mindate=mindate)
        if self.cache is None:
            self.cache = cachemod.EsearchCache(self._cache_dir())
            self.cache_stats = cachemod.CacheStats()
        r = cachemod.cached_esearch(self.pubmed, self.cache, term,
                                    retmax=retmax, mindate=mindate,
                                    stats=self.cache_stats)
        return r

    def _cache_dir(self) -> str:
        """缓存目录放在 runs_root 的同级，不混进每次运行的产物目录。"""
        root = os.path.abspath(self.cfg['paths'].get('runs_root') or './runs')
        parent = os.path.dirname(root) or '.'
        return os.path.join(parent, '.medlit-cache')

    def search(self, queries: list[dict], tag: str = 'round1',
               retmax: int = 20, min_year: str | None = None):
        if tag == 'round1':
            self.log('   检索范围：全 PubMed（不限刊源，不限学科；仅英文国际文献）')

        for cq_id in self.cq_explicit:
            if cq_id not in self.cq_all:
                self.log(f'   ! 未知 clinical query：{cq_id}（已忽略）')
                continue
            self.cq_used.append(cq_id)
            self.log(f'   临床查询过滤器：{cq_id}（{self.cq_all[cq_id]["label"]}）')

        exclude_animals = bool((self.cfg['retrieval'] or {}).get('exclude_animals'))
        langs = (self.cfg['retrieval'] or {}).get('languages') or []
        if langs:
            self.log(f'   语种限制：{", ".join(langs)}')

        for q in queries:
            term = (q.get('query') or '').strip()
            if not term:
                continue
            full = term
            for cq_id in self.cq_used:
                full = pm.apply_clinical_query(full, self.cq_all[cq_id]['query'])
            if exclude_animals:
                full = pm.apply_animal_exclusion(full, True)
            if langs:
                full = pm.apply_language_filter(full, langs)
            self.log(f'② 检索 [{q.get("label")}] {term[:90]}')
            t0 = time.time()
            try:
                r = self._esearch(full, retmax=retmax, mindate=min_year)
            except Exception as e:  # noqa: BLE001
                self.err('esearch', e)
                continue
            self._search_latency.append(time.time() - t0)
            if r.get('error'):
                self.search_failed = True
                self.err('esearch', f"NCBI 检索后端暂不可用：{r['error']}")
                self.log(f'   ! 检索后端报错，本条跳过：{str(r["error"])[:140]}')
                continue
            got = r['ids']
            fresh = [i for i in got if i not in self.papers]
            self.prisma['identified'] += r['count']
            self.prisma['queries'] += 1
            self.search_log.append({
                'round': tag, 'label': q.get('label'), 'query': term,
                'final_query': full[:800],
                'total_hits': r['count'], 'retrieved': len(got), 'new': len(fresh),
                'translation': r.get('translation', '')[:300],
            })
            self.log(f'   命中 {r["count"]}，取回 {len(got)}，新增 {len(fresh)}')
            # N2：PubMed 会告诉我们"它自己怎么理解你的检索式"（自动 MeSH 映射、拆词等）。
            # 命中异常少时，这段解释是判断"检索式被误解"的第一手线索，必须让人看见。
            trans = (r.get('translation') or '').strip()
            if trans:
                self.translations.append({'query': term, 'translation': trans[:600]})
                if r['count'] < 5:
                    self.log(f'   ! 命中偏少，PubMed 对检索式的解读：{trans[:260]}')
                    self.log('     （若解读与你的原意不符，多半是 MeSH 自动映射或拆词出了问题）')
            if fresh:
                self.fetch_metadata(fresh, found_by=f'{tag}:{q.get("label") or ""}')
        self._save_checkpoint()

    def search_with_precheck(self, queries: list[dict], tag: str = 'round1',
                             retmax: int = 20, min_year: str | None = None):
        """A1：检索式命中为 0 时自动修订重试（最多 2 轮）。"""
        self.search(queries, tag, retmax, min_year)
        current = queries
        for attempt in range(1, int(self.cfg['retrieval'].get('max_revisions', 2)) + 1):
            # 命中统计要**把修订轮也算进来**：修订轮的标签是 '{tag}rev{n}'，
            # 只算 tag 的话，第一次修订已经命中了几百条仍会再触发一次修订
            # ——白花一次 planner 调用 + 一轮 NCBI 检索。命中为 0 恰恰是最该省钱的场景。
            hits = sum(s['total_hits'] for s in self.search_log
                       if s['round'] == tag or s['round'].startswith(tag + 'rev'))
            if hits > 0 or self.search_failed:
                break
            self.log(f'   ! 本轮命中 0，触发检索式修订（第 {attempt} 次）')
            revised = self.revise_queries(current)
            if not revised:
                break
            newtag = f'{tag}rev{attempt}'
            self.search(revised, tag=newtag, retmax=retmax, min_year=min_year)
            current = revised
        return current

    def expand_queries(self) -> list[dict]:
        """第二轮：让 planner 补充检索式（缩写/旧术语/同义表述），提高召回。"""
        rounds = int(self.cfg['retrieval'].get('rounds', 1) or 1)
        if rounds < 2:
            return []
        need = int(self.cfg['retrieval'].get('expand_when_below',
                                             self.cfg['retrieval'].get('max_papers', 8) * 2))
        if len(self.papers) >= need:
            self.log(f'   候选已充足（{len(self.papers)} ≥ {need}），跳过扩写')
            return []
        used = '\n'.join(f'- {q.get("query", "")}' for q in self.plan.get('queries', []))
        gaps = '（请补充缩写、旧术语、同义表述，以及该问题常用的其他 MeSH 专指词）'
        msgs = [{'role': 'system', 'content': prompts.PLANNER_EXPAND_SYSTEM},
                {'role': 'user', 'content': prompts.PLANNER_EXPAND_USER.format(
                    core_question=self.plan.get('core_question_en') or self.question,
                    used_queries=used or '（无）',
                    total_hits=self.prisma['identified'],
                    n_papers=len(self.papers),
                    gaps=gaps)}]
        try:
            res = self.router.chat('planner', msgs,
                                   temperature=self.cfg['runtime']['temperature_plan'],
                                   max_tokens=1000)
        except Exception as e:  # noqa: BLE001
            self.err('expand', e)
            return []
        self._track_tokens('planner', res)
        data = _json_load(res['content']) or {}
        qs = data.get('queries') or []
        self.log(f'   扩写轮：新增 {len(qs)} 条检索式（{res["model"]}，{res["elapsed"]}s）')
        for q in qs:
            self.log(f'   · [扩写] {q.get("query", "")[:110]}')
        return qs

    # ---------- 3. 链接扩展（A2）与引文滚雪球（A3） ----------
    def expand_by_links(self, max_extra: int = 15):
        """A2：被引文献 + 相似文献。"""
        seeds = list(self.papers.keys())[:8]
        if not seeds:
            return
        for linkname, label in (('pubmed_pubmed_citedin', '被引文献'),
                                ('pubmed_pubmed_sim', '相似文献')):
            r = self.pubmed.elink(seeds, linkname, retmax=max_extra)
            if r.get('error'):
                self.log(f'   ! {label}扩展失败：{r["error"][:80]}')
                continue
            new = [i for i in r['ids'] if i not in self.papers][:max_extra]
            if new:
                self.fetch_metadata(new)
                self.prisma['linked'] += len(new)
                self.log(f'   + {label}扩展：新增 {len(new)} 篇')
        self._save_checkpoint()

    def expand_by_references(self, max_titles: int = 3):
        """A3：从种子文献的参考文献标题回灌检索。"""
        titles: list[str] = []
        for p in list(self.papers.values())[:6]:
            for t in (p.get('reference_titles') or []):
                if len(t) > 25 and t not in titles:
                    titles.append(t)
        titles = titles[:max_titles]
        if not titles:
            return
        for t in titles:
            try:
                # 走缓存：同一课题重跑时，滚雪球的标题检索完全一样，
                # 不该再打一次 NCBI
                r = self._esearch(f'"{t}"[Title]', retmax=3)
            except Exception as e:  # noqa: BLE001
                self.err('snowball', e)
                continue
            if r.get('error'):
                continue
            new = [i for i in r['ids'] if i not in self.papers]
            if new:
                self.fetch_metadata(new)
                self.prisma['snowballed'] += len(new)
                self.log(f'   + 引文滚雪球："{t[:48]}…" → 新增 {len(new)} 篇')
        self._save_checkpoint()

    def search_preprints(self, term: str, retmax: int = 15):
        """A4：预印本（Europe PMC 为主源，bioRxiv/medRxiv 官方 API 兜底）。

        预印本**没有 PMID**，因此单独存一份字典，用 DOI 标识；
        它们同样会进入精读池，并用 [DOI:xxx] 的形式被正文引用。
        """
        if not preprintmod or not (self.cfg['retrieval'] or {}).get('include_preprints'):
            return
        r = preprintmod.search_preprints(term, retmax=retmax)
        papers = r.get('papers') or []
        if r.get('error') and not papers:
            self.log(f'   ! 预印本检索不可用：{r["error"][:100]}')
            return
        if not papers:
            self.log('   · 预印本检索无结果')
            return
        added = 0
        for rec in papers:
            ident = rec.get('id') or ''
            if not ident or ident in self.preprints:
                continue
            self.preprints[ident] = rec
            added += 1
        self.prisma['preprints'] += added
        self.log(f'   + 预印本（{"/".join(r.get("sources") or ["Europe PMC"])}）：'
                 f'新增 {added} 篇（无 PMID，将以 [DOI:xxx] 形式引用；'
                 f'证据等级按未同行评议下调）')
        self._save_checkpoint()

    # ---------- 4. 题录 ----------
    def fetch_metadata(self, pmids: list[str], found_by: str = '') -> int:
        try:
            papers = self.pubmed.efetch(pmids)
        except Exception as e:  # noqa: BLE001
            self.err('efetch', e)
            return 0
        keep = 0
        for p in papers:
            if not p.get('pmid'):
                continue
            if self.cfg['retrieval'].get('require_abstract') and not p.get('abstract'):
                continue
            # R14：记下这篇是被哪一轮检索捞上来的。调检索式时这是唯一的抓手——
            # 没有它就不知道"改了检索式之后少了哪些文献"。
            if found_by:
                p['found_by'] = found_by
            self.papers[p['pmid']] = p
            keep += 1
        self.log(f'   抓取题录 {keep} 篇（累计候选 {len(self.papers)}）')
        return keep

    # ---------- 5. 过滤 ----------
    def filter_papers(self) -> list[str]:
        ret = self.cfg['retrieval']
        exclude = ret.get('exclude_pub_types', [])
        research_only = ret.get('research_only', False)
        min_abs = int(ret.get('min_abstract_chars', 200))
        research_min = int(ret.get('research_min_abstract_chars', 300))
        exclude_animals = bool(ret.get('exclude_animals', True))
        before = len(self.papers)
        dropped = 0
        for pid in list(self.papers.keys()):
            p = self.papers[pid]
            if research_only and not pm.is_research_article(p, exclude, research_min):
                self.excluded.append({'pmid': pid, 'title': p.get('title', ''),
                                      'reason': '非研究论文（文献类型不符合）'})
                del self.papers[pid]
                dropped += 1
                continue
            if len(p.get('abstract') or '') < min_abs:
                self.excluded.append({'pmid': pid, 'title': p.get('title', ''),
                                      'reason': f'摘要过短（<{min_abs} 字）'})
                del self.papers[pid]
                dropped += 1
                continue
            # B2：撤稿文献不进精读池
            chk = pm.check_retraction(p)
            if chk['retracted']:
                self.excluded.append({'pmid': pid, 'title': p.get('title', ''),
                                      'reason': '已撤稿（Retracted Publication）'})
                del self.papers[pid]
                dropped += 1
                continue
            # 纯动物实验：检索式里的过滤器只降噪、不能完全把关，
            # 这里按抓回来的 MeSH 再判一次（程序侧才是最终把关）
            if exclude_animals and pm.is_animal_only(p):
                self.excluded.append({'pmid': pid, 'title': p.get('title', ''),
                                      'reason': '纯动物实验（MeSH 无 Humans 标记）'})
                del self.papers[pid]
                dropped += 1
        self.prisma['deduplicated'] = before
        self.prisma['screened'] = len(self.papers)
        self.prisma['excluded_screening'] = before - len(self.papers)
        self.log(f'③ 过滤后保留 {len(self.papers)} 篇（剔除 {dropped} 篇：'
                 f'非研究论文 / 摘要过短 / 已撤稿 / 纯动物实验）')
        self._save_checkpoint()
        return list(self.papers.keys())

    # ---------- 6. 重排 ----------
    def rerank(self, ids: list[str]) -> list[str]:
        limit = int(self.cfg['retrieval']['max_papers'])
        rr = (self.cfg.get('provider') or {}).get('rerank') or {}
        if not rr.get('enabled'):
            self.log(f'④ 跳过重排（未启用 rerank）→ 取前 {limit} 篇')
            return ids[:limit]
        if len(ids) <= limit:
            self.log(f'④ 跳过重排（候选 {len(ids)} ≤ {limit}）')
            return ids[:limit]
        q = self.plan.get('core_question_en') or self.question
        docs = [f'{self.papers[i].get("title", "")}. {self.papers[i].get("abstract", "")[:1600]}'
                for i in ids]
        model = rr.get('model', '')
        self.log(f'④ 语义重排（{model}）：{len(ids)} → {limit}')
        try:
            res = self.provider.rerank(model, q, docs, top_n=limit,
                                        endpoint=rr.get('endpoint'))
            ranked = [ids[int(r['index'])] for r in res
                      if 0 <= int(r['index']) < len(ids)]
            for r in res:
                idx = int(r['index'])
                if 0 <= idx < len(ids):
                    self.papers[ids[idx]]['rerank_score'] = r.get('relevance_score')
            self.log('   重排完成')
            return ranked[:limit]
        except Exception as e:  # noqa: BLE001
            self.log(f'   ! 重排失败，退回原始顺序：{e}')
            self.err('rerank', e)
            return ids[:limit]

    # ---------- 7. 精读 ----------
    def _claim_fulltext_slot(self) -> bool:
        limit = int(self.cfg['retrieval'].get('fulltext_limit', 0) or 0)
        with self._ft_lock:
            if self._ft_claimed < limit:
                self._ft_claimed += 1
                return True
        return False

    def _read_workers(self) -> int:
        """精读阶段的并发度。

        刻意**不参考检索延迟**：精读是纯模型调用，跟 NCBI 快慢毫无关系。
        原来的自适应逻辑在"检索慢"时把并发降下来，反而拖慢了精读本身
        ——检索慢通常意味着网络问题，不是模型服务的问题。
        上限 6：模型服务商有限流，且长文本并发容易超时（2026-11-05 实测教训）。
        """
        base = max(1, int(self.cfg['runtime'].get('workers', 3)))
        return min(6, base)

    def _adaptive_workers(self) -> int:
        """兼容旧名。旧的"按检索延迟调整"逻辑已废弃，见 `_read_workers`。"""
        return self._read_workers()

    def read_one(self, key: str, paper: dict | None = None,
                 is_preprint: bool = False) -> dict | None:
        """抽取单篇证据。

        key 是"引用标识"：PubMed 文献是 PMID，预印本是 `doi:10.1101/xxx`。
        正文里分别用 [PMID:xxx] 与 [DOI:xxx] 引用。
        """
        p = paper if paper is not None else self.papers[key]
        ft = {'source': 'abstract', 'text': '', 'figure_captions': []}
        if self._claim_fulltext_slot():
            try:
                ft = self.pubmed.fulltext(p)
            except Exception:  # noqa: BLE001
                pass
        if ft.get('text'):
            body = ft['text'][:26000]
            src = 'fulltext'
            fn = os.path.join(self.dir, '06_fulltext', f'PMID{key}.txt')
            try:
                with open(fn, 'w', encoding='utf-8') as f:
                    f.write(ft['text'])
            except Exception:  # noqa: BLE001
                pass
        else:
            body = p.get('abstract', '')[:5000]
            src = 'abstract'
        if not body:
            return None

        msgs = [{'role': 'system', 'content': prompts.READER_SYSTEM},
                {'role': 'user', 'content': prompts.READER_USER.format(
                    question=self.plan.get('core_question_en') or self.question,
                    citation_key=key,
                    is_preprint='是（预印本，未经过同行评议）' if is_preprint else '否',
                    journal=p.get('journal', ''),
                    year=p.get('year', ''),
                    title=p.get('title', ''),
                    pub_types=' / '.join(p.get('pub_types') or []),
                    mesh=', '.join((p.get('mesh') or [])[:10]),
                    source=src,
                    text=body)}]
        try:
            res = self.router.chat('reader', msgs,
                                   temperature=self.cfg['runtime']['temperature_reader'],
                                   max_tokens=2200,
                                   # 精读喂的是全文（最长 26000 字），模型处理长文本
                                   # 差异极大：实测 DeepSeek-V3.2 约 13 秒，
                                   # 而 Qwen3.5-35B-A3B 会超过 180 秒直接超时。
                                   # 超时越短，才能越快降级到真正能用的模型。
                                   timeout=int((self.cfg['runtime'] or {}).get(
                                       'reader_timeout', 90)))
        except Exception as e:  # noqa: BLE001
            self.err(f'read {key}', e)
            return None
        self._track_tokens('reader', res)
        data = _json_load(res['content'])
        if not data:
            self.err(f'read {key}', 'JSON 解析失败')
            return None
        data['pmid'] = p.get('pmid', '') or ''
        data['_citation_key'] = key
        data['_is_preprint'] = is_preprint
        data['_text_source'] = src
        data['_journal'] = p.get('journal', '')
        data['_year'] = p.get('year', '')
        data['_title'] = p.get('title', '')
        data['_doi'] = p.get('doi', '') or ''
        # B7：图表说明
        caps = (ft.get('figure_captions') or [])[:8]
        if caps:
            data['_figure_captions'] = caps
        # B2：撤稿/勘误标记
        chk = pm.check_retraction(p)
        if chk['note']:
            data['_retraction'] = chk['note']
        return data

    def read_all(self, ids: list[str]):
        """精读。ids 是 PubMed PMID 列表；预印本也会一并被精读。"""
        workers = self._read_workers()
        self.log(f'⑤ 逐篇精读（PubMed {len(ids)} 篇'
                 + (f' + 预印本 {len(self.preprints)} 篇' if self.preprints else '')
                 + f'，并发 {workers}）')
        t0 = time.time()
        done = 0
        jobs = [(k, self.papers[k], False) for k in ids]
        jobs += [(k, self.preprints[k], True) for k in self.preprints]
        total = len(jobs)
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(self.read_one, k, p, is_pp): k for k, p, is_pp in jobs}
            for fut in as_completed(futs):
                key = futs[fut]
                try:
                    d = fut.result()
                except Exception as e:  # noqa: BLE001
                    self.err(f'read {key}', e)
                    d = None
                done += 1
                if d:
                    self.evidence.append(d)
                    pp = '（预印本）' if d.get('_is_preprint') else ''
                    self.log(f'   [{done}/{total}] {key} 相关度 {d.get("relevance", "?")}'
                             f'（{d["_text_source"]}）{pp}')
                else:
                    self.prisma['excluded_fulltext'] += 1
                    self.log(f'   [{done}/{total}] {key} 抽取失败')
        self.trace['timings']['read'] = round(time.time() - t0, 1)
        self._cross_check_relevance()
        self.evidence.sort(key=lambda d: (-(d.get('relevance') or 0), str(d.get('_year') or '')))
        self.prisma['fulltext_assessed'] = total
        self.prisma['included'] = len(self.evidence)
        self.prisma['excluded_fulltext'] = max(0, total - len(self.evidence))
        self.log(f'   精读完成：成功 {len(self.evidence)} 篇，{self.trace["timings"]["read"]}s')
        self._save_checkpoint()

    def _cross_check_relevance(self):
        """B8：相关度交叉验证——两个不同模型各打一次分取平均。

        单次打分会有偶然性；两个模型分歧大时，这本身就是个信号。
        """
        if not verifymod or not self.evidence:
            return
        if not (self.cfg['retrieval'] or {}).get('cross_check_relevance', True):
            return
        self.log('   · 相关度交叉验证（两个模型各打一次分）')

        def chat_fn(role, messages, temperature=0.2, max_tokens=800, model=None):
            if model:
                # 指定模型时直接走 provider，绕开路由器的角色链
                # 短超时：交叉验证/语义比对都是短 prompt，
                # 超时越短越快降级到下一个模型，避免整轮卡住
                res = self.provider.chat(model, messages, temperature,
                                         max_tokens,
                                         timeout=int((self.cfg['runtime'] or {}).get(
                                             'reader_timeout', 90)))
                self._track_tokens('reader', res)
                return res
            return self.router.chat(role, messages, temperature, max_tokens)

        # 两个模型必须**真的不同**，否则就是自己跟自己对账，白花一倍的钱。
        chain = self.cfg['models']['chain']
        reader_chain = chain.get('reader') or []
        critic_chain = chain.get('critic') or []
        fast_chain = chain.get('fast') or []
        model_a = (reader_chain[0] if reader_chain else None)
        # 第二个模型刻意跨厂商/跨家族取，交叉验证才有意义。
        # 优先从 **fast 链**取：交叉验证只是给个相关度分，是轻量判断任务，
        # 用大模型纯属浪费。实测同一份打分 prompt：
        #   DeepSeek-V3.2 2.5s ／ Qwen3.5-35B-A3B 53.2s ／ Qwen3.5-27B 57.2s
        #   ／ Qwen3.5-9B 直接超时／Qwen3-14B 1.5s／Qwen2.5-7B 0.7s
        # 慢模型串行跑 5 篇就是好几分钟，看起来像卡死。
        model_b = None
        for cand in (fast_chain + reader_chain[2:] + critic_chain):
            if cand and cand != model_a:
                model_b = cand
                break
        if not model_b:
            model_b = reader_chain[1] if len(reader_chain) > 1 else model_a
        if model_a == model_b:
            self.log('   · 相关度交叉验证：可用模型不足两个，跳过（否则是自证）')
            return
        self.log(f'   · 相关度交叉验证：{model_a} vs {model_b}')

        t0 = time.time()
        try:
            before = len(self.evidence)
            self.evidence = verifymod.cross_check_relevance(
                self.evidence, chat_fn, model_a=model_a, model_b=model_b)
            after = self.evidence
            if len(after) != before:
                self.evidence = after
            # verify.py 把交叉分放在 relevance_cross，原分留在 relevance。
            # **必须把交叉分采纳为有效分**，否则交叉验证算了却不影响排序，等于白花一倍的钱。
            adopted = 0
            for d in self.evidence:
                cross = d.get('relevance_cross')
                if cross is None or d.get('relevance_cross_failed'):
                    continue
                try:
                    if abs(float(cross) - float(d.get('relevance', 0))) > 1e-9:
                        adopted += 1
                    d['relevance'] = cross
                except (TypeError, ValueError):
                    continue
            gain = verifymod.cross_check_gain(
                [dict(d, relevance=d.get('_relevance_original', d.get('relevance')))
                 for d in after], after)
            gain['adopted'] = adopted
            self.trace['relevance_cross_check'] = gain
            self.log(f'     交叉验证完成：{gain.get("changed", 0)} 篇分数变化'
                     f'（已采纳 {adopted} 篇），'
                     f'{gain.get("divergent", 0)} 篇两模型分歧大')
        except Exception as e:  # noqa: BLE001
            self.err('cross_check', e)
        finally:
            # B8 要跑两遍模型，是最贵的一步，耗时必须单独记
            self.trace['timings']['cross_check'] = round(time.time() - t0, 1)

    # ---------- 8. 效应量合并（B4） ----------
    def pool_effects(self):
        if not metamod:
            return []
        try:
            self.pooled = metamod.summarize_effects(self.evidence)
        except Exception as e:  # noqa: BLE001
            self.err('meta', e)
            self.pooled = []
        for p in self.pooled:
            self.log(f'   合并效应量：{p.get("metric")} = {p.get("pooled")} '
                     f'({p.get("ci_low")}–{p.get("ci_high")}), I²={p.get("i2")}%, '
                     f'k={p.get("k")}，{p.get("heterogeneity")}异质性')
        self._save_checkpoint()
        return self.pooled

    # ---------- 9. 审查 ----------
    def critique_all(self) -> dict:
        if not self.evidence:
            return {'evidence_strength': '无证据', 'certainty': '极低',
                    'certainty_reason': '本轮未获得可用的结构化证据',
                    'consistency': '无', 'conflicts': [],
                    'gaps': ['本轮检索未获得可用的结构化证据'],
                    'warnings': ['检索过窄或全部抽取失败'],
                    'population_applicability': '不适用'}
        slim = [{k: v for k, v in d.items() if not k.startswith('_')} for d in self.evidence]
        msgs = [{'role': 'system', 'content': prompts.CRITIC_SYSTEM},
                {'role': 'user', 'content': prompts.CRITIC_USER.format(
                    question=self.plan.get('core_question_en') or self.question,
                    evidence=json.dumps(slim, ensure_ascii=False)[:14000],
                    search_summary=self.search_summary(),
                    mechanical_grading=json.dumps(self.mech_grading, ensure_ascii=False),
                    # 审稿模型也要看到效应量合并结果——否则它无从判断
                    # 「合并后是否仍有异质性」「结论是否与合并效应一致」
                    pooled_effects=self.pooled_summary() or '（无）')}]
        self.log('⑥ 横向证据审查')
        try:
            res = self.router.chat('critic', msgs, temperature=0.15, max_tokens=2000)
            self.trace['timings']['critique'] = res['elapsed']
            self.trace['models']['critic'] = res['model']
            self._track_tokens('critic', res)
            data = _json_load(res['content']) or {}
            self.log(f'   完成（{res["model"]}，{res["elapsed"]}s）'
                     f'—— 一致性：{data.get("consistency", "?")}'
                     f'｜GRADE：{data.get("certainty", "?")}'
                     f'（机械定级 {self.mech_grading.get("certainty", "?")}）')
            return data
        except Exception as e:  # noqa: BLE001
            self.err('critique', e)
            return {'evidence_strength': '审查环节失败', 'certainty': '极低',
                    'certainty_reason': '审查环节失败', 'consistency': '未知',
                    'conflicts': [], 'gaps': [], 'warnings': [str(e)[:200]],
                    'population_applicability': '未知'}

    # ---------- 10. 概况与表格 ----------
    def search_summary(self) -> str:
        if not self.search_log:
            return '（无检索记录）'
        total = sum(s['total_hits'] for s in self.search_log)
        years = [int(p['year']) for p in self.papers.values()
                 if str(p.get('year', '')).isdigit()]
        designs = {}
        for d in self.evidence:
            k = (d.get('study_design') or '未标注')[:20]
            designs[k] = designs.get(k, 0) + 1
        ymin = min(years) if years else '—'
        ymax = max(years) if years else '—'
        lines = [
            f'- 执行检索 {len(self.search_log)} 次，累计命中 {total} 条记录',
            f'- 抓取题录 {len(self.papers)} 篇，逐篇精读 {len(self.evidence)} 篇',
            f'- 年份范围：{ymin}–{ymax}',
            '- 设计分布：' + '；'.join(f'{k} {v} 篇' for k, v in designs.items()),
            '- 期刊来源：' + '；'.join(sorted({p.get("journal", "") for p in self.papers.values()})[:8]),
            '- 检索范围：全 PubMed（不限刊源，不限学科；**仅英文国际文献，未检索中文数据库**）',
        ]
        if self.cq_used:
            lines.append('- 临床查询过滤器：' + '、'.join(self.cq_used))
        if self.prisma.get('linked'):
            lines.append(f'- 经被引/相似文献链接扩展补充 {self.prisma["linked"]} 篇')
        if self.prisma.get('snowballed'):
            lines.append(f'- 经引文滚雪球补充 {self.prisma["snowballed"]} 篇')
        if self.prisma.get('preprints'):
            lines.append(f'- 预印本（bioRxiv/medRxiv）{self.prisma["preprints"]} 篇')
        return '\n'.join(lines)

    def prisma_summary(self) -> str:
        p = self.prisma
        return '\n'.join([
            f'- 数据库检出记录：{p["identified"]}（{p["queries"]} 次检索）',
            f'- 去重后题录：{p["deduplicated"]} 篇',
            f'- 题摘初筛后保留：{p["screened"]} 篇（排除 {p["excluded_screening"]} 篇）',
            f'- 进入全文/深读评估：{p["fulltext_assessed"]} 篇',
            f'- 最终纳入证据：{p["included"]} 篇（排除 {p["excluded_fulltext"]} 篇）',
        ])

    def prisma_table(self) -> str:
        p = self.prisma
        rows = [
            '| 阶段 | 数量 |', '|---|---|',
            f'| 数据库检出记录（{p["queries"]} 次检索，去重前） | {p["identified"]} |',
            f'| 去重后题录 | {p["deduplicated"]} |',
            f'| 题摘初筛排除 | −{p["excluded_screening"]} |',
            f'| 题摘初筛后保留 | {p["screened"]} |',
            f'| 全文/深读评估排除 | −{p["excluded_fulltext"]} |',
            f'| 最终纳入 | {p["included"]} |',
        ]
        if p.get('linked'):
            rows.append(f'| 其中来自被引/相似文献链接扩展 | +{p["linked"]} |')
        if p.get('snowballed'):
            rows.append(f'| 其中来自引文滚雪球 | +{p["snowballed"]} |')
        if p.get('preprints'):
            rows.append(f'| 其中预印本 | +{p["preprints"]} |')
        return '\n'.join(rows)

    def exclusion_reasons(self) -> str:
        rs = grading.excluded_reason_table(self.excluded)
        return '\n'.join(f'- {r}' for r in rs) if rs else '- （无排除记录）'

    def evidence_table(self) -> str:
        """证据总表。单元格里的竖线必须全部转义，否则会把 Markdown 表格打散。"""
        def cell(v, limit=0) -> str:
            s = str(v if v is not None else '')
            s = s.replace('|', '/').replace('\n', ' ').strip()
            if limit and len(s) > limit:
                s = s[:limit]
            return s

        rows = ['| # | 引用标识 | 年份 | 期刊 | 设计 | 样本量 | PICO | 相关度 | 来源 | 核心发现 |',
                '|---|--------|------|------|------|--------|------|--------|------|----------|']
        for i, d in enumerate(self.evidence, 1):
            kf = cell('；'.join((d.get('key_findings') or [])[:2]), 100)
            flag = ' ⚠撤稿' if d.get('_retraction') else ''
            if d.get('_is_preprint'):
                flag += ' 〔预印本〕'
            # R13：标明这条证据实际读的是全文还是摘要。
            # 两者证据强度不同——只读了摘要的条目，"未提及某个结局"可能只是
            # 摘要没写，不能当成"该研究没做"。
            src = {'fulltext': '全文', 'abstract': '摘要'}.get(
                d.get('_text_source') or '', '')
            if not src and d.get('_is_preprint'):
                src = '预印本'
            rows.append(f'| {i} | {cell(d.get("_citation_key") or d.get("pmid"))}{flag} | '
                        f'{cell(d.get("_year"))} | '
                        f'{cell(d.get("_journal"), 26)} | {cell(d.get("study_design"), 16)} | '
                        f'{cell(d.get("sample_size"), 14)} | '
                        f'{cell(d.get("pico_match")) or "—"} | {cell(d.get("relevance"))} | '
                        f'{src or "—"} | {kf} |')
        return '\n'.join(rows)

    def bias_table(self) -> str:
        """B3：偏倚评估要素汇总。"""
        fields = [('randomization', '随机化'), ('blinding', '盲法'),
                  ('allocation_concealment', '分配隐藏'),
                  ('followup_duration', '随访时长'),
                  ('attrition', '失访'), ('sample_size_calc', '样本量估算')]
        rows = ['| PMID | ' + ' | '.join(lbl for _, lbl in fields) + ' |',
                '|---|' + '---|' * len(fields)]
        for d in self.evidence:
            ba = d.get('bias_assessment') or {}
            cells = []
            for key, _lbl in fields:
                v = str(ba.get(key) or '未提及')
                cells.append(v.replace('|', '/')[:22])
            rows.append(f'| {d.get("pmid", "")} | ' + ' | '.join(cells) + ' |')
        return '\n'.join(rows)

    def pooled_summary(self) -> str:
        if not self.pooled:
            return '（本轮没有可合并的效应量）'
        out = []
        for p in self.pooled:
            if p.get('k', 0) <= 0:
                out.append(f'- {p.get("metric")}：{p.get("note", "无法合并")}')
                continue
            out.append(f'- {p.get("metric")} 合并值 {p.get("pooled")} '
                       f'(95%CI {p.get("ci_low")}–{p.get("ci_high")})，'
                       f'k={p.get("k")}，I²={p.get("i2")}%，'
                       f'{p.get("model")} 模型，异质性{p.get("heterogeneity")}')
        return '\n'.join(out)

    def bibliography(self) -> str:
        """参考文献由脚本按原始字段生成，不经过模型。"""
        out = []
        for i, d in enumerate(self.evidence, 1):
            key = d.get('_citation_key') or d.get('pmid') or ''
            if d.get('_is_preprint'):
                rec = self.preprints.get(key, {})
                au = pm.fmt_authors(rec.get('authors') or [], 3)
                head = f'{au}. ' if au else ''
                title = (rec.get('title') or '').rstrip('.')
                journal = rec.get('journal') or '预印本'
                year = rec.get('year') or ''
                doi = f' doi:{rec["doi"]}' if rec.get('doi') else ''
                out.append(f'{i}. {head}{title}. *{journal}*. {year}. '
                           f'{key}{doi} [预印本，未同行评议]')
                continue
            p = self.papers.get(d['pmid'], {})
            au = pm.fmt_authors(p.get('authors') or [], 3)
            head = f'{au}. ' if au else ''
            title = (p.get('title') or '').rstrip('.')
            journal = p.get('journal') or p.get('journal_short') or ''
            year = p.get('year') or ''
            doi = f' doi:{p["doi"]}' if p.get('doi') else ''
            note = ' [已撤稿]' if d.get('_retraction') else ''
            out.append(f'{i}. {head}{title}. *{journal}*. {year}. '
                       f'PMID:{d["pmid"]}{doi}{note}')
        return '\n'.join(out)

    # ---------- 11. 成文 ----------
    REQUIRED_SECTIONS = ('一句话结论', '证据概况', '核心发现', '证据冲突与不确定性',
                         '证据空隙', '检索覆盖说明', '适用边界', '对你的具体启示')

    def check_answer_structure(self, answer: str) -> dict:
        """N4：检查答案的结构完整性。

        两件事：① 承诺的八个段落是否都在；② 关键段落里是否真的挂了引用。
        「结构不全」的答案看起来完整、实则缺斤少两，用户很难察觉，所以要程序查。
        """
        text = answer or ''
        missing = []
        for sec in self.REQUIRED_SECTIONS:
            if f'## {sec}' not in text:
                missing.append(sec)
        # 逐段看引用数
        sections = {}
        current = None
        for line in text.split('\n'):
            if line.startswith('## '):
                current = line[3:].strip()
                sections[current] = 0
            elif current and ('[PMID:' in line or '[DOI:' in line):
                sections[current] += 1
        no_cite = [k for k in ('核心发现', '证据概况', '一句话结论')
                   if k in sections and sections[k] == 0]
        return {'ok': not missing and not no_cite,
                'missing_sections': missing,
                'sections_without_citation': no_cite,
                'section_citation_counts': sections}

    def write(self) -> str:
        msgs = [{'role': 'system', 'content': prompts.WRITER_SYSTEM},
                {'role': 'user', 'content': prompts.WRITER_USER.format(
                    question=self.question,
                    constraints=self.constraints or '（无）',
                    search_summary=self.search_summary(),
                    prisma=self.prisma_summary(),
                    evidence_table=self.evidence_table(),
                    retraction_notes='\n'.join(
                        grading.retraction_notes(list(self.papers.values()))
                    ) or '（无撤稿/勘误提示）',
                    pooled_effects=self.pooled_summary(),
                    critique=json.dumps(self.critique, ensure_ascii=False)[:8000])}]
        self.log('⑦ 撰写答案')
        try:
            res = self.router.chat('writer', msgs,
                                   temperature=self.cfg['runtime']['temperature_writer'],
                                   max_tokens=self.cfg['runtime']['max_tokens'] + 2000)
            self.trace['timings']['write'] = res['elapsed']
            self.trace['models']['writer'] = res['model']
            self._track_tokens('writer', res)
            answer = res['content']
        except Exception as e:  # noqa: BLE001
            self.err('write', e)
            return '## 生成失败\n\n模型调用出错，请查看 09_log.txt。\n'

        # N4：结构校验，不合格就让模型重写一次
        check = self.check_answer_structure(answer)
        self.trace['answer_structure'] = check
        if not check['ok'] and (self.cfg['retrieval'] or {}).get(
                'rewrite_on_structure_fail', True):
            problems = []
            if check['missing_sections']:
                problems.append('缺少这些段落：' + '、'.join(check['missing_sections']))
            if check['sections_without_citation']:
                problems.append('这些段落里没有任何引用标记：'
                                + '、'.join(check['sections_without_citation']))
            self.log('   ! 结构校验未通过：' + '；'.join(problems) + ' → 让 writer 重写一次')
            retry = msgs + [
                {'role': 'assistant', 'content': answer[:4000]},
                {'role': 'user', 'content':
                    '上一版答案结构不完整：' + '；'.join(problems)
                    + '。请按要求的结构完整重写一遍，八个段落一个都不能少，'
                      '并且「一句话结论」「证据概况」「核心发现」里必须有 [PMID:xxx] 引用。'},
            ]
            try:
                res2 = self.router.chat('writer', retry,
                                        temperature=0.2,
                                        max_tokens=self.cfg['runtime']['max_tokens'] + 2000)
                self._track_tokens('writer', res2)
                # 结构重写是独立的一次模型调用，耗时必须单独记，
                # 否则「一次查询要多久」这个问题无法回答
                self.trace['timings']['write_rewrite'] = round(
                    self.trace['timings'].get('write_rewrite', 0) + res2['elapsed'], 1)
                check2 = self.check_answer_structure(res2['content'])
                self.trace['answer_structure_retry'] = check2
                if check2['ok']:
                    self.log('   ✓ 重写后结构完整')
                    answer = res2['content']
                    self.trace['answer_structure'] = check2
                else:
                    self.log('   ! 重写后仍不完整，保留原版并记入 trace')
            except Exception as e:  # noqa: BLE001
                self.err('write-retry', e)
        elif not check['ok']:
            self.log('   ! 结构校验未通过（已关闭自动重写）')
        else:
            self.log('   ✓ 结构校验通过：八个段落齐全，关键段落均有引用')
        return answer

    # ---------- 12. PMID 越界校验 ----------
    def validate_pmids(self) -> dict:
        """正文里的每一个引用标识都必须来自候选池。

        支持两种引用形式：
          `[PMID:12345678]`   → 必须在本轮 PubMed 候选池里
          `[DOI:10.1101/xxx]` → 必须在本轮预印本候选池里
        越界引用会被记进 trace['errors']——这是唯一能自动发现"模型编文献"的机制。
        """
        answer = self.answer or ''
        cited_pmids = set(re.findall(r'\[PMID:\s*(\d+)\]', answer))
        cited_dois = set(re.findall(r'\[DOI:\s*([^\]\s]+)\]', answer))

        pmid_pool = set(self.papers.keys())
        doi_pool = set()
        for k, rec in self.preprints.items():
            d = (rec.get('doi') or '').strip().lower()
            if d:
                doi_pool.add(d)
            if k.lower().startswith('doi:'):
                doi_pool.add(k[4:].lower())

        orphan_pmids = sorted(cited_pmids - pmid_pool)
        orphan_dois = sorted(d for d in cited_dois if d.lower() not in doi_pool)
        check = {
            'cited_count': len(cited_pmids) + len(cited_dois),
            'cited_pmid_count': len(cited_pmids),
            'cited_doi_count': len(cited_dois),
            'pool_size': len(pmid_pool),
            'doi_pool_size': len(doi_pool),
            'orphan_pmids': orphan_pmids,
            'orphan_dois': orphan_dois,
            'orphan_count': len(orphan_pmids) + len(orphan_dois),
            'cited_in_pool': len(cited_pmids & pmid_pool),
        }
        if self.search_failed:
            check['skipped'] = '检索后端故障，未生成答案'
            self.pmid_check = check
            return check
        if orphan_pmids or orphan_dois:
            parts = []
            if orphan_pmids:
                parts.append(f'{len(orphan_pmids)} 个不在候选池的 PMID：'
                             + ', '.join(orphan_pmids[:10])
                             + ('...' if len(orphan_pmids) > 10 else ''))
            if orphan_dois:
                parts.append(f'{len(orphan_dois)} 个不在候选池的 DOI：'
                             + ', '.join(orphan_dois[:10])
                             + ('...' if len(orphan_dois) > 10 else ''))
            msg = '正文引用了' + '；'.join(parts)
            self.trace['errors'].append({'stage': 'citation_check', 'err': msg})
            self.log(f'!! 引用校验未通过：{msg}')
        elif cited_pmids or cited_dois:
            self.log(f'   ✓ 引用校验通过：正文引用 {len(cited_pmids)} 个 PMID'
                     + (f' + {len(cited_dois)} 个 DOI' if cited_dois else '')
                     + '，全部来自候选池')
        else:
            self.log('   ! 正文没有出现任何引用标记，可能是证据不足或模型未遵守写作纪律')
            self.trace['errors'].append({'stage': 'citation_check',
                                         'err': '正文中未出现任何 [PMID:xxx] / [DOI:xxx] 引用标记'})
        self.pmid_check = check
        self._check_retraction_mentioned(cited_pmids)
        return check

    def _check_retraction_mentioned(self, cited_pmids: set[str]):
        """被引用的撤稿/勘误文献，正文里必须提到这件事。

        writer 的提示词要求它写，但提示词只是建议——没有程序侧校验，
        就可能出现"正文拿撤稿文献的结论当有效证据"的情况。
        这里做一道兜底：撤稿文献被引用了、正文却一个字没提，就报警。
        """
        flagged = [d for d in (self.evidence or [])
                   if d.get('_retraction') and d.get('pmid') in cited_pmids]
        if not flagged:
            return
        answer = self.answer or ''
        # 正文里只要出现过"撤稿/勘误/retract"任一说法，就算已经交代过
        if any(k in answer for k in ('撤稿', '勘误', 'retract', 'Retract')):
            self.log(f'   ✓ 已引用 {len(flagged)} 篇撤稿/勘误文献，正文有相应交代')
            return
        pids = '、'.join(str(d.get('pmid')) for d in flagged[:6])
        msg = (f'正文引用了 {len(flagged)} 篇撤稿/勘误文献（PMID {pids}），'
               f'但正文完全没有提及撤稿这件事——读者会把它当有效证据')
        self.trace['errors'].append({'stage': 'retraction_check', 'err': msg})
        self.log(f'!! 撤稿提示缺失：{msg}')

    def _verify_claims(self):
        """B5：正文论断与被引文献的语义比对。

        只校验"PMID 在不在候选池"是不够的——模型完全可能挂一篇真实存在但内容无关
        的文献，读者点进去才发现对不上。这里让模型逐条核对"这篇真的支持这句话吗"。
        """
        if not verifymod or not self.answer:
            return
        if not (self.cfg['retrieval'] or {}).get('verify_claims', True):
            return
        self.log('   · 论断与文献语义比对（抽查最关键的若干条）')

        def chat_fn(role, messages, temperature=0.2, max_tokens=800, model=None):
            if model:
                # 短超时：交叉验证/语义比对都是短 prompt，
                # 超时越短越快降级到下一个模型，避免整轮卡住
                res = self.provider.chat(model, messages, temperature,
                                         max_tokens,
                                         timeout=int((self.cfg['runtime'] or {}).get(
                                             'reader_timeout', 90)))
                self._track_tokens('reader', res)
                return res
            return self.router.chat(role, messages, temperature, max_tokens)

        t0 = time.time()
        try:
            res = verifymod.verify_claims(self.answer, self.evidence, chat_fn,
                                          max_checks=int(
                                              (self.cfg['retrieval'] or {}).get(
                                                  'claim_check_limit', 12)))
            self.trace['claim_verification'] = res
            self.log(f'     比对完成：核验 {res.get("checked", 0)} 条，'
                     f'不支持 {len(res.get("unsupported", []))} 条，'
                     f'部分支持 {len(res.get("partial", []))} 条')
            for item in res.get('unsupported', [])[:3]:
                self.log(f'       ⚠ 可能不支持：{str(item.get("text", ""))[:80]}'
                         f' {item.get("citations", [])}')
        except Exception as e:  # noqa: BLE001
            self.err('verify_claims', e)
        finally:
            # B5 也要跑模型，耗时必须单独记
            self.trace['timings']['verify_claims'] = round(time.time() - t0, 1)

    # ---------- 13. 落盘 ----------
    def _w(self, name: str, content: str):
        with open(os.path.join(self.dir, name), 'w', encoding='utf-8') as f:
            f.write(content)

    def save(self):
        self._w('01_question.md',
                f'# 问题\n\n{self.question}\n\n## 补充限定\n\n{self.constraints or "（无）"}\n\n'
                f'## 检索范围\n\n全 PubMed（不限刊源、不限学科；仅英文国际文献，未检索中文数据库）\n')
        plan_out = dict(self.plan)
        # N2：把 PubMed 的检索式解读一并落盘，便于事后判断检索式是否被误解
        plan_out['query_translations'] = self.translations
        # N7：过滤器只是"建议"，程序不会自动 AND 进去——是否使用由使用者决定
        if self.plan.get('suggested_cq'):
            plan_out['suggested_cq_note'] = (
                'planner 建议的临床查询过滤器（仅供参考，未自动生效；'
                '要用请显式加 --cq ' + str(self.plan['suggested_cq']))
        self._w('02_plan.json', json.dumps(plan_out, ensure_ascii=False, indent=2))
        self._w('03_papers.json', json.dumps(list(self.papers.values()), ensure_ascii=False, indent=1))
        clean = [{k: v for k, v in d.items() if not k.startswith('_')} for d in self.evidence]
        self._w('04_evidence.json', json.dumps(clean, ensure_ascii=False, indent=1))
        self._w('05_evidence_table.md',
                f'# 证据总表\n\n{self.search_summary()}\n\n{self.evidence_table()}\n\n'
                f'## 偏倚评估要素\n\n{self.bias_table()}\n\n'
                f'## 合并效应量\n\n{self.pooled_summary()}\n\n'
                f'## GRADE 机械定级\n\n'
                f'{self.mech_grading.get("reason", "（未执行）")}\n')
        self._w('05b_excluded.json', json.dumps(self.excluded, ensure_ascii=False, indent=1))

        bib = self.bibliography()
        answer = self.answer
        self._w('07_answer.md', answer + ('\n\n---\n\n## 关键文献（按 PubMed 原始字段生成）\n\n'
                                         + bib + '\n' if bib else ''))
        self._w('08_bibliography.md', '# 参考文献\n\n' + (bib or '（无）') + '\n')

        self._w('11_prisma.md',
                '# PRISMA 流程\n\n' + self.prisma_summary() + '\n\n'
                + self.prisma_table() + '\n\n'
                + '## 排除理由汇总\n\n' + self.exclusion_reasons() + '\n\n'
                + '## 检索式明细\n\n'
                + '\n'.join(f'### 第 {i} 次检索（{s["round"]} / {s.get("label", "")}）\n\n'
                            f'- 命中：{s["total_hits"]}\n- 取回：{s["retrieved"]}\n'
                            f'- 实际送检：\n\n```\n{s.get("final_query", s["query"])}\n```'
                            for i, s in enumerate(self.search_log, 1)) + '\n')

        self.trace['counts'] = {'papers': len(self.papers), 'evidence': len(self.evidence)}
        self.trace['prisma'] = dict(self.prisma)
        self.trace['pmid_check'] = dict(self.pmid_check)
        self.trace['mechanical_grading'] = dict(self.mech_grading)
        self.trace['elapsed_total'] = round(time.time() - self.t0, 1)
        self._w('10_trace.json', json.dumps(self.trace, ensure_ascii=False, indent=2))
        self._w('09_log.txt', '\n'.join(self._log_lines) + '\n')
        self.log(f'⑧ 已保存到 {self.dir}')
        self.log(f'   总耗时 {self.trace["elapsed_total"]}s'
                 + (f'｜本次 token 约 {self.trace["tokens"]}' if self.trace['tokens'] else ''))
        self._update_index()
        self._save_checkpoint()

    def _update_index(self):
        """C4：维护 runs 根目录下的汇总索引（新增的排在最前，便于按时间倒序看）。"""
        try:
            root = os.path.abspath(self.cfg['paths'].get('runs_root') or './runs')
            os.makedirs(root, exist_ok=True)
            idx = os.path.join(root, '_index.md')
            title = (self.plan.get('core_question_en') or self.question)[:80]
            grading = self.mech_grading.get('certainty', '—')
            row = (f'| {self.stamp} | {title} | {len(self.evidence)} | '
                   f'{grading} | {self.pmid_check.get("cited_count", 0)} | '
                   f'{self.prisma.get("identified", 0)} | '
                   f'{self.trace.get("elapsed_total", 0)}s |')
            if not os.path.exists(idx):
                with open(idx, 'w', encoding='utf-8') as f:
                    f.write('# 运行索引\n\n'
                            '> 最新的排在最前。GRADE 确定性由程序机械定级。\n\n'
                            '| 时间 | 问题 | 纳入 | GRADE | 引用数 | 命中 | 耗时 |\n'
                            '|---|---|---|---|---|---|---|\n')
            with open(idx, 'r', encoding='utf-8') as f:
                lines = f.read().split('\n')
            # 找到表体起始行，把新行插在它前面
            body_start = next((i for i, l in enumerate(lines)
                               if l.startswith('|---')), len(lines))
            lines.insert(body_start + 1, row)
            with open(idx, 'w', encoding='utf-8') as f:
                f.write('\n'.join(lines))
        except Exception as e:  # noqa: BLE001
            self.err('index', e)

    # ---------- 编排 ----------
    def _timed(self, key: str, fn, *args, **kwargs):
        """跑一个阶段并把耗时记进 trace。

        用 try/finally 计时，这样函数内部无论从哪个 return 出去，耗时都记得上。
        """
        t0 = time.time()
        try:
            return fn(*args, **kwargs)
        finally:
            self.trace['timings'][key] = round(
                self.trace['timings'].get(key, 0) + (time.time() - t0), 1)

    def execute(self, quick: bool = False, resume: bool = False,
                stages: str = 'full'):
        """跑检索流水线。

        `stages` 控制跑到哪一步就停：
        - `'evidence'`：只做检索 + 精读（要原始证据时用）
        - `'grade'`：再加上效应量合并与 GRADE 定级（**横向对比用这个**——
          对比只需要证据与确定性，不需要写综述正文，
          跑完整的会白花一半的钱和一半的时间）
        - `'full'`：完整流程（含审查、成文、语义比对）
        """
        ret = self.cfg['retrieval']
        self.trace['quick_mode'] = bool(quick)
        self.trace['stages'] = stages
        if quick:
            # 快速模式：关掉两项后置校验（B8 交叉验证每篇跑两遍模型、
            # B5 语义比对最多 12 条论断各跑一遍）。它们是纯成本项，
            # 不影响检索质量。不关的话「快速」模式的成本并不低。
            ret['cross_check_relevance'] = False
            ret['verify_claims'] = False
            self.log('   · 快速模式：已关闭相关度交叉验证与论断语义比对（省时省钱）')

        if resume and self.plan:
            self.log('↻ 断点续跑：跳过规划与检索阶段')
        else:
            self.plan_queries()
            # A7：年份下限自适应（planner 给出 year_floor）
            min_year = str(ret.get('min_year') or '') or None
            try:
                yf = int(self.plan.get('year_floor') or 0)
                if yf:
                    min_year = str(yf)
                    self.log(f'   年份下限（自适应）：{min_year}')
            except (TypeError, ValueError):
                pass
            self._timed('search', self.search_with_precheck,
                        self.plan.get('queries', []), 'round1',
                        int(ret['retmax_per_query']), min_year)
            if not self.papers and not self.search_failed:
                self.log('!! 没有检索到任何文献，尝试放宽：去掉年份限制')
                self._timed('search', self.search_with_precheck,
                            self.plan.get('queries', []), 'round1b',
                            int(ret['retmax_per_query']), None)

        # 第二轮：扩写检索式提高召回
        extra = self._timed('expand_queries', self.expand_queries)
        if extra:
            min_year = str(ret.get('min_year') or '') or None
            self._timed('search', self.search_with_precheck, extra, 'round2',
                        int(ret.get('retmax_per_query_round2', 12)), min_year)

        # A2 / A3 / A4 扩展
        if ret.get('expand_by_links', True):
            self._timed('expand_links', self.expand_by_links,
                        max_extra=int(ret.get('max_link_expansion', 15)))
        if ret.get('expand_by_references', True):
            self._timed('expand_snowball', self.expand_by_references,
                        max_titles=int(ret.get('max_snowball_titles', 3)))
        if ret.get('include_preprints', False):
            self._timed('preprints', self.search_preprints,
                        self.plan.get('core_question_en') or self.question,
                        retmax=int(ret.get('preprint_limit', 15)))

        ids = self._timed('filter', self.filter_papers)
        if not ids:
            if self.search_failed:
                self.log('!! 全部检索均因 NCBI 后端故障失败，不是"没有相关文献"')
                self.answer = (
                    '## 一句话结论\n\n'
                    '**本轮未能完成检索：NCBI 检索后端暂时不可用**（返回 '
                    '`Search Backend failed / Search is temporarily unavailable`）。\n\n'
                    '这不是"检索后没有相关证据"，而是**上游服务故障**。'
                    '请稍后重试，或用 `--pubmed-only` 单独确认检索式是否可用。\n')
            else:
                self.log('!! 过滤后为空，跳过精读')
                self.answer = '## 一句话结论\n\n本轮检索未获得可用文献，无法作答。\n'
            self.validate_pmids()
            self.save()
            return self.dir

        picked = self._timed('rerank', self.rerank, ids)
        self._timed('read', self.read_all, picked)

        # 只要原始证据、不写综述正文时，到此为止
        if stages == 'evidence':
            self.answer = '（本次运行止步于证据抽取，未生成综述正文）\n'
            self.save()
            return self.dir

        self.pool_effects()
        # B1：机械定级（在审查之前算好，供 critic 复核）
        self.mech_grading = grading.mechanical_grading(self.evidence, self.pooled)
        self.log(f'   GRADE 机械定级：{self.mech_grading.get("certainty")}'
                 f'（{self.mech_grading.get("reason", "")}）')

        # 横向对比只要证据与确定性：写成文、跑语义比对都是白花钱
        if stages == 'grade':
            self.answer = '（本次运行止步于证据分级，未生成综述正文）\n'
            self.save()
            return self.dir

        self.critique = self.critique_all()
        self.answer = self.write()
        self._verify_claims()
        self.validate_pmids()
        self.save()
        return self.dir
