# -*- coding: utf-8 -*-
"""主流程：规划 → 检索 → 扩写 → 题录 → 过滤 → 重排 → 精读 → 审查 → 成文 → 校验 → 落盘。

九段流水线的编排层。所有"机械活"（计数、过滤、排序、参考文献、校验）都在这里，
模型只负责需要判断力的四件事：怎么问、怎么抽、怎么挑矛盾、怎么写。
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import config as cfgmod
import prompts
import pubmed as pm
import sf


# ---------------- 通用工具 ----------------
def load_config(path: str | None = None) -> dict:
    """向后兼容旧调用方式。"""
    return cfgmod.load_config(path)


def _slug(text: str, n: int = 16) -> str:
    t = re.sub(r'[^\w\u4e00-\u9fff]+', '', text or '')
    return t[:n] or 'query'


def _json_load(text: str):
    """从模型输出里抠 JSON（容忍 ```json 包裹和前后噪音）。"""
    if not text:
        return None
    t = text.strip()
    t = re.sub(r'^```(?:json)?\s*', '', t)
    t = re.sub(r'\s*```$', '', t)
    try:
        return json.loads(t)
    except Exception:  # noqa: BLE001
        m = re.search(r'\{.*\}', t, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:  # noqa: BLE001
                return None
    return None


class Run:
    def __init__(self, question: str, constraints: str = '', cfg: dict | None = None,
                 cq_explicit: list[str] | None = None):
        self.cfg = cfg or cfgmod.load_config()
        self.question = (question or '').strip()
        self.constraints = (constraints or '').strip()
        self.t0 = time.time()
        self.stamp = datetime.now().strftime('%Y%m%d-%H%M')

        # ---- 输出目录 ----
        root = self.cfg['paths'].get('runs_root') or './runs'
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
        self.router = sf.ModelRouter(self.provider, self.cfg['models']['chain'])

        # ---- PubMed ----
        self.pubmed = pm.PubMed(
            api_key=self.cfg['pubmed'].get('api_key', ''),
            tool=self.cfg['pubmed'].get('tool', 'medlit-agent'),
            email=self.cfg['pubmed'].get('email', ''),
        )

        # ---- Clinical Query（仅在用户显式指定时启用）----
        self.cq_all = self._load_cq()
        self.cq_explicit = list(cq_explicit or [])
        self.cq_used: list[str] = []
        self.search_failed = False

        # ---- 状态 ----
        self.papers: dict[str, dict] = {}
        self.evidence: list[dict] = []
        self.search_log: list[dict] = []
        self.plan: dict = {}
        self.critique: dict = {}
        self.answer = ''
        self.pmid_check: dict = {}
        self.prisma = {
            'identified': 0, 'deduplicated': 0, 'screened': 0,
            'excluded_screening': 0, 'fulltext_assessed': 0,
            'included': 0, 'excluded_fulltext': 0, 'queries': 0,
        }
        self.trace = {
            'question': self.question, 'constraints': self.constraints,
            'timings': {}, 'models': {}, 'errors': [], 'counts': {},
            'provider': {'name': self.provider.name, 'base_url': self.provider.base},
            'scope_on': False,
            'scope_note': '本工具不做刊源限制，检索全 PubMed',
            'clinical_queries': {'used': list(self.cq_used)},
        }
        self._log_lines: list[str] = []

        # 全文抓取配额：并发下必须用锁，否则会超量
        self._ft_lock = threading.Lock()
        self._ft_claimed = 0

    # ---------- 配置解析 ----------
    def _load_cq(self) -> dict:
        """载入 PubMed 官方 Clinical Query 过滤器表（仅供 --cq 显式调用）。"""
        path = os.path.join(cfgmod.ROOT, 'data', 'clinical_queries.json')
        try:
            with open(path, encoding='utf-8') as f:
                data = json.load(f)
        except Exception:  # noqa: BLE001
            return {}
        return {k: v for k, v in data.items() if not k.startswith('_')}

    # ---------- 日志 ----------
    def log(self, msg: str):
        line = f'[{time.strftime("%H:%M:%S")}] {msg}'
        print(line, flush=True)
        self._log_lines.append(line)

    def err(self, stage: str, e):
        self.trace['errors'].append({'stage': stage, 'err': str(e)[:400]})
        self.log(f'!! {stage} 出错：{e}')

    # ---------- 1. 规划 ----------
    def plan_queries(self) -> dict:
        msgs = [{'role': 'system', 'content': prompts.PLANNER_SYSTEM},
                {'role': 'user', 'content': prompts.PLANNER_USER.format(
                    question=self.question,
                    constraints=self.constraints or '（无）')}]
        res = self.router.chat('planner', msgs,
                               temperature=self.cfg['runtime']['temperature_plan'],
                               max_tokens=1600)
        self.trace['timings']['plan'] = res['elapsed']
        self.trace['models']['planner'] = res['model']
        data = _json_load(res['content']) or {}
        if not data.get('queries'):
            # 兜底：把问题原样当检索式
            data = {'core_question_en': self.question,
                    'queries': [{'label': '直译', 'query': self.question}],
                    'pico': {}, 'notes': '模型未给出检索式，已回退为原问题'}
        self.plan = data
        self.log(f'① 规划完成：{len(data.get("queries", []))} 条检索式'
                 f'（{res["model"]}，{res["elapsed"]}s）')
        for q in data.get('queries', []):
            self.log(f'   · [{q.get("label")}] {q.get("query", "")[:110]}')
        return data

    # ---------- 2. 扩写（第二轮） ----------
    def expand_queries(self) -> list[dict]:
        """第一轮候选不足时，让 planner 扩写检索式提高召回。"""
        rounds = int(self.cfg['retrieval'].get('rounds', 1) or 1)
        if rounds < 2:
            return []
        need = int(self.cfg['retrieval'].get('expand_when_below',
                                             self.cfg['retrieval'].get('max_papers', 8) * 2))
        if len(self.papers) >= need:
            self.log(f'   候选已充足（{len(self.papers)} ≥ {need}），跳过扩写')
            return []
        used = '\n'.join(f'- {q.get("query", "")}' for q in self.plan.get('queries', []))
        gaps = ('（请补充缩写、旧术语、同义表述，以及该问题常用的其他 MeSH 专指词）')
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
        data = _json_load(res['content']) or {}
        qs = data.get('queries') or []
        self.log(f'   扩写轮：新增 {len(qs)} 条检索式（{res["model"]}，{res["elapsed"]}s）')
        for q in qs:
            self.log(f'   · [扩写] {q.get("query", "")[:110]}')
        return qs

    # ---------- 3. 检索 ----------
    def search(self, queries: list[dict], tag: str = 'round1',
               retmax: int = 20, min_year: str | None = None):
        # 本工具不做刊源限制：检索全 PubMed，任何期刊、任何出版社都进入候选。
        if tag == 'round1':
            self.log('   检索范围：全 PubMed（不限刊源，不限学科）')

        # Clinical Query 仅在用户显式指定时启用
        for cq_id in self.cq_explicit:
            if cq_id not in self.cq_all:
                self.log(f'   ! 未知 clinical query：{cq_id}（已忽略）')
                continue
            self.cq_used.append(cq_id)
            self.log(f'   临床查询过滤器：{cq_id}（{self.cq_all[cq_id]["label"]}）')

        for q in queries:
            term = (q.get('query') or '').strip()
            if not term:
                continue
            full = term
            for cq_id in self.cq_used:
                full = pm.apply_clinical_query(full, self.cq_all[cq_id]['query'])
            self.log(f'② 检索 [{q.get("label")}] {term[:90]}')
            try:
                r = self.pubmed.esearch(full, retmax=retmax, mindate=min_year)
            except Exception as e:  # noqa: BLE001
                self.err('esearch', e)
                continue
            # NCBI 检索后端故障时返回 HTTP 200 + ERROR 字段，
            # 必须与"真的没检索到"区分开，否则会把上游故障说成证据不足。
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
                'final_query': full[:600],
                'total_hits': r['count'], 'retrieved': len(got), 'new': len(fresh),
                'translation': r.get('translation', '')[:300],
            })
            self.log(f'   命中 {r["count"]}，取回 {len(got)}，新增 {len(fresh)}')
            # 立刻抓题录，确保本轮内去重生效
            if fresh:
                self.fetch_metadata(fresh)

    # ---------- 4. 题录 ----------
    def fetch_metadata(self, pmids: list[str]) -> int:
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
        before = len(self.papers)
        dropped = 0
        for pid in list(self.papers.keys()):
            p = self.papers[pid]
            if research_only and not pm.is_research_article(p, exclude, research_min):
                del self.papers[pid]
                dropped += 1
                continue
            if len(p.get('abstract') or '') < min_abs:
                del self.papers[pid]
                dropped += 1
        self.prisma['deduplicated'] = before
        self.prisma['screened'] = len(self.papers)
        self.prisma['excluded_screening'] = before - len(self.papers)
        self.log(f'③ 过滤后保留 {len(self.papers)} 篇（剔除 {dropped} 篇：非研究论文 / 摘要过短）')
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
        """原子地占用一个全文抓取配额。并发下避免超量抓取。"""
        limit = int(self.cfg['retrieval'].get('fulltext_limit', 0) or 0)
        with self._ft_lock:
            if self._ft_claimed < limit:
                self._ft_claimed += 1
                return True
        return False

    def read_one(self, pmid: str) -> dict | None:
        p = self.papers[pmid]
        ft = {'source': 'abstract', 'text': ''}
        if self._claim_fulltext_slot():
            try:
                ft = self.pubmed.fulltext(p)
            except Exception:  # noqa: BLE001
                pass
        if ft.get('text'):
            body = ft['text'][:26000]
            src = 'fulltext'
            fn = os.path.join(self.dir, '06_fulltext', f'PMID{pmid}.txt')
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
                    pmid=pmid,
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
                                   max_tokens=1600)
        except Exception as e:  # noqa: BLE001
            self.err(f'read {pmid}', e)
            return None
        data = _json_load(res['content'])
        if not data:
            self.err(f'read {pmid}', 'JSON 解析失败')
            return None
        data['pmid'] = pmid
        data['_text_source'] = src
        data['_journal'] = p.get('journal', '')
        data['_year'] = p.get('year', '')
        data['_title'] = p.get('title', '')
        return data

    def read_all(self, ids: list[str]):
        self.log(f'⑤ 逐篇精读（{len(ids)} 篇，并发 {self.cfg["runtime"]["workers"]}）')
        t0 = time.time()
        workers = max(1, int(self.cfg['runtime']['workers']))
        done = 0
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(self.read_one, pid): pid for pid in ids}
            for fut in as_completed(futs):
                pid = futs[fut]
                try:
                    d = fut.result()
                except Exception as e:  # noqa: BLE001
                    self.err(f'read {pid}', e)
                    d = None
                done += 1
                if d:
                    self.evidence.append(d)
                    self.log(f'   [{done}/{len(ids)}] {pid} 相关度 {d.get("relevance", "?")}'
                             f'（{d["_text_source"]}）')
                else:
                    self.log(f'   [{done}/{len(ids)}] {pid} 抽取失败')
        self.trace['timings']['read'] = round(time.time() - t0, 1)
        self.evidence.sort(key=lambda d: (-(d.get('relevance') or 0), str(d.get('_year') or '')))
        self.prisma['fulltext_assessed'] = len(ids)
        self.prisma['included'] = len(self.evidence)
        self.prisma['excluded_fulltext'] = len(ids) - len(self.evidence)
        self.log(f'   精读完成：成功 {len(self.evidence)} 篇，{self.trace["timings"]["read"]}s')

    # ---------- 8. 审查 ----------
    def critique_all(self) -> dict:
        if not self.evidence:
            return {'evidence_strength': '无证据', 'certainty': '极低',
                    'certainty_reason': '本轮未获得可用的结构化证据',
                    'consistency': '无', 'conflicts': [],
                    'gaps': ['本轮检索未获得可用的结构化证据'],
                    'warnings': ['检索过窄或全部抽取失败']}
        slim = [{k: v for k, v in d.items() if not k.startswith('_')} for d in self.evidence]
        msgs = [{'role': 'system', 'content': prompts.CRITIC_SYSTEM},
                {'role': 'user', 'content': prompts.CRITIC_USER.format(
                    question=self.plan.get('core_question_en') or self.question,
                    evidence=json.dumps(slim, ensure_ascii=False)[:14000],
                    search_summary=self.search_summary())}]
        self.log('⑥ 横向证据审查')
        try:
            res = self.router.chat('critic', msgs, temperature=0.15, max_tokens=1800)
            self.trace['timings']['critique'] = res['elapsed']
            self.trace['models']['critic'] = res['model']
            data = _json_load(res['content']) or {}
            self.log(f'   完成（{res["model"]}，{res["elapsed"]}s）'
                     f'—— 一致性：{data.get("consistency", "?")}'
                     f'｜GRADE：{data.get("certainty", "?")}')
            return data
        except Exception as e:  # noqa: BLE001
            self.err('critique', e)
            return {'evidence_strength': '审查环节失败', 'certainty': '未知',
                    'certainty_reason': '', 'consistency': '未知',
                    'conflicts': [], 'gaps': [], 'warnings': [str(e)[:200]]}

    # ---------- 9. 成文 ----------
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
            '- 检索范围：全 PubMed（不限刊源，不限学科）',
        ]
        if self.cq_used:
            lines.append('- 临床查询过滤器：' + '、'.join(self.cq_used))
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
            '| 阶段 | 数量 |',
            '|---|---|',
            f'| 数据库检出记录（{p["queries"]} 次检索，去重前） | {p["identified"]} |',
            f'| 去重后题录 | {p["deduplicated"]} |',
            f'| 题摘初筛排除 | −{p["excluded_screening"]} |',
            f'| 题摘初筛后保留 | {p["screened"]} |',
            f'| 全文/深读评估排除 | −{p["excluded_fulltext"]} |',
            f'| 最终纳入 | {p["included"]} |',
        ]
        return '\n'.join(rows)

    def evidence_table(self) -> str:
        rows = ['| # | PMID | 年份 | 期刊 | 设计 | 样本量 | 相关度 | 核心发现 |',
                '|---|------|------|------|------|--------|--------|----------|']
        for i, d in enumerate(self.evidence, 1):
            kf = '；'.join((d.get('key_findings') or [])[:2])[:100].replace('|', '/')
            rows.append(f'| {i} | {d.get("pmid", "")} | {d.get("_year", "")} | '
                        f'{(d.get("_journal") or "")[:26]} | {(d.get("study_design") or "")[:16]} | '
                        f'{str(d.get("sample_size") or "")[:14]} | {d.get("relevance", "")} | {kf} |')
        return '\n'.join(rows)

    def bibliography(self) -> str:
        """参考文献由脚本按 PubMed 原始字段生成，不经过模型。"""
        out = []
        for i, d in enumerate(self.evidence, 1):
            p = self.papers.get(d['pmid'], {})
            au = pm.fmt_authors(p.get('authors') or [], 3)
            head = f'{au}. ' if au else ''
            title = (p.get('title') or '').rstrip('.')
            journal = p.get('journal') or p.get('journal_short') or ''
            year = p.get('year') or ''
            doi = f' doi:{p["doi"]}' if p.get('doi') else ''
            out.append(f'{i}. {head}{title}. *{journal}*. {year}. PMID:{d["pmid"]}{doi}')
        return '\n'.join(out)

    def write(self) -> str:
        msgs = [{'role': 'system', 'content': prompts.WRITER_SYSTEM},
                {'role': 'user', 'content': prompts.WRITER_USER.format(
                    question=self.question,
                    constraints=self.constraints or '（无）',
                    search_summary=self.search_summary(),
                    prisma=self.prisma_summary(),
                    evidence_table=self.evidence_table(),
                    critique=json.dumps(self.critique, ensure_ascii=False)[:8000])}]
        self.log('⑦ 撰写答案')
        try:
            res = self.router.chat('writer', msgs,
                                   temperature=self.cfg['runtime']['temperature_writer'],
                                   max_tokens=self.cfg['runtime']['max_tokens'] + 1500)
            self.trace['timings']['write'] = res['elapsed']
            self.trace['models']['writer'] = res['model']
            self.log(f'   完成（{res["model"]}，{res["elapsed"]}s）')
            return res['content']
        except Exception as e:  # noqa: BLE001
            self.err('write', e)
            return '## 生成失败\n\n模型调用出错，请查看 09_log.txt。\n'

    # ---------- 10. PMID 越界校验 ----------
    def validate_pmids(self) -> dict:
        """正文里出现的每个 PMID 都必须来自候选池；否则判为越界引用。"""
        cited = set(re.findall(r'\[PMID:(\d+)\]', self.answer or ''))
        pool = set(self.papers.keys())
        orphan = sorted(cited - pool)
        check = {
            'cited_count': len(cited),
            'pool_size': len(pool),
            'orphan_pmids': orphan,
            'orphan_count': len(orphan),
            'cited_in_pool': len(cited & pool),
        }
        if self.search_failed:
            # 上游故障导致没生成答案，不做 PMID 判读
            check['skipped'] = '检索后端故障，未生成答案'
            self.pmid_check = check
            return check
        if orphan:
            msg = (f'正文引用了 {len(orphan)} 个不在候选池中的 PMID：'
                   f'{", ".join(orphan[:10])}{"…" if len(orphan) > 10 else ""}')
            self.trace['errors'].append({'stage': 'pmid_check', 'err': msg})
            self.log(f'!! PMID 校验未通过：{msg}')
        elif cited:
            self.log(f'   PMID 校验通过：正文引用 {len(cited)} 个，全部来自候选池')
        else:
            self.log('   ! 正文没有出现任何 [PMID:xxxx]，可能是证据不足或模型未遵守写作纪律')
            self.trace['errors'].append({'stage': 'pmid_check',
                                         'err': '正文中未出现任何 PMID 引用标记'})
        self.pmid_check = check
        return check

    # ---------- 11. 落盘 ----------
    def _w(self, name: str, content: str):
        with open(os.path.join(self.dir, name), 'w', encoding='utf-8') as f:
            f.write(content)

    def save(self):
        self._w('01_question.md',
                f'# 问题\n\n{self.question}\n\n## 补充限定\n\n{self.constraints or "（无）"}\n\n'
                f'## 检索范围\n\n全 PubMed（不限刊源，不限学科）\n')
        self._w('02_plan.json', json.dumps(self.plan, ensure_ascii=False, indent=2))
        self._w('03_papers.json', json.dumps(list(self.papers.values()), ensure_ascii=False, indent=1))

        clean = [{k: v for k, v in d.items() if not k.startswith('_')} for d in self.evidence]
        self._w('04_evidence.json', json.dumps(clean, ensure_ascii=False, indent=1))
        self._w('05_evidence_table.md',
                f'# 证据总表\n\n{self.search_summary()}\n\n{self.evidence_table()}\n')

        bib = self.bibliography()
        answer = self.answer
        self._w('07_answer.md', answer + ('\n\n---\n\n## 关键文献（按 PubMed 原始字段生成）\n\n'
                                         + bib + '\n' if bib else ''))
        self._w('08_bibliography.md', '# 参考文献\n\n' + (bib or '（无）') + '\n')

        self._w('11_prisma.md',
                '# PRISMA 流程\n\n' + self.prisma_summary() + '\n\n'
                + self.prisma_table() + '\n\n'
                + '## 检索式明细\n\n'
                + '\n'.join(f'### 第 {i} 次检索（{s["round"]} / {s.get("label", "")}）\n\n'
                            f'- 命中：{s["total_hits"]}\n- 取回：{s["retrieved"]}\n'
                            f'- 实际送检：\n\n```\n{s.get("final_query", s["query"])}\n```'
                            for i, s in enumerate(self.search_log, 1)) + '\n')

        self.trace['counts'] = {'papers': len(self.papers), 'evidence': len(self.evidence)}
        self.trace['prisma'] = dict(self.prisma)
        self.trace['pmid_check'] = dict(self.pmid_check)
        self.trace['elapsed_total'] = round(time.time() - self.t0, 1)
        self._w('10_trace.json', json.dumps(self.trace, ensure_ascii=False, indent=2))
        self._w('09_log.txt', '\n'.join(self._log_lines) + '\n')
        self.log(f'⑧ 已保存到 {self.dir}')
        self.log(f'   总耗时 {self.trace["elapsed_total"]}s')

    # ---------- 编排 ----------
    def execute(self, quick: bool = False):
        ret = self.cfg['retrieval']
        max_papers = int(ret['max_papers'])
        min_year = str(ret.get('min_year') or '') or None

        self.plan_queries()
        self.search(self.plan.get('queries', []), tag='round1',
                    retmax=int(ret['retmax_per_query']), min_year=min_year)

        if not self.papers:
            self.log('!! 没有检索到任何文献，尝试放宽：去掉年份限制')
            self.search(self.plan.get('queries', []), tag='round1b',
                        retmax=int(ret['retmax_per_query']), min_year=None)

        # 第二轮：扩写检索式提高召回
        extra = self.expand_queries()
        if extra:
            self.search(extra, tag='round2',
                        retmax=int(ret.get('retmax_per_query_round2', 12)), min_year=min_year)

        ids = self.filter_papers()
        if not ids:
            if self.search_failed:
                self.log('!! 全部检索均因 NCBI 后端故障失败，不是"没有相关文献"')
                self.answer = (
                    '## 一句话结论\n\n'
                    '**本轮未能完成检索：NCBI 检索后端暂时不可用**（返回 '
                    '`Search Backend failed / Search is temporarily unavailable`）。\n\n'
                    '这不是"检索后没有相关证据"，而是**上游服务故障**。'
                    '请稍后重试，或用 `--pubmed-only` 单独确认检索式是否可用。\n'
                )
            else:
                self.log('!! 过滤后为空，跳过精读')
                self.answer = '## 一句话结论\n\n本轮检索未获得可用文献，无法作答。\n'
            self.validate_pmids()
            self.save()
            return self.dir

        picked = self.rerank(ids)
        self.read_all(picked)
        self.critique = self.critique_all()
        self.answer = self.write()
        self.validate_pmids()
        self.save()
        return self.dir
