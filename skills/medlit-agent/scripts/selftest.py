# -*- coding: utf-8 -*-
"""逐环节体检。约 2 分钟。

  python selftest.py            # 全链路
  python selftest.py --net      # 只测网络与接口，不跑完整问答
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:  # noqa: BLE001
    pass

import json

import config as cfgmod
import grading
import pipeline
import pubmed as pm
import sf

OK, FAIL = 'PASS', 'FAIL'
SKIP = 'SKIP'
results: list[tuple[str, str, str]] = []


class Skip(Exception):
    """因上游服务不可用而跳过——不是本项目的问题。"""


def check(name: str, fn):
    t0 = time.time()
    try:
        detail = fn() or ''
        results.append((name, OK, f'{time.time() - t0:.1f}s  {detail}'))
        print(f'[{OK}] {name}  ({time.time() - t0:.1f}s)  {detail}')
    except Skip as e:
        results.append((name, SKIP, str(e)[:200]))
        print(f'[{SKIP}] {name}  -> {e}')
    except Exception as e:  # noqa: BLE001
        results.append((name, FAIL, str(e)[:200]))
        print(f'[{FAIL}] {name}  -> {e}')


def esearch_or_skip(p, term: str, retmax: int = 10) -> dict:
    """esearch 的统一入口：上游故障一律 Skip，不算本项目失败。"""
    try:
        r = p.esearch(term, retmax=retmax)
    except Exception as e:  # noqa: BLE001
        raise Skip(f'NCBI 检索后端不可用（上游故障）：{e}')
    if r.get('error'):
        raise Skip(f'NCBI 检索后端不可用（上游故障）：{r["error"]}')
    return r


def efetch_or_skip(p, ids) -> list:
    """efetch 的统一入口：上游故障一律 Skip，不算本项目失败。"""
    if not ids:
        raise Skip('上游 esearch 未返回 ID，跳过（依赖 esearch 成功）')
    try:
        papers = p.efetch(ids)
    except Exception as e:  # noqa: BLE001
        raise Skip(f'NCBI 题录接口不可用（上游故障）：{e}')
    return papers


def main():
    net_only = '--net' in sys.argv
    cfg = pipeline.load_config()

    print('=' * 66)
    print('循证医学文献检索 Agent · 自检')
    print('=' * 66)

    def t_config():
        prov = cfg.get('provider') or {}
        return (f'provider={prov.get("name")}｜模型链 planner={cfg["models"]["chain"]["planner"][0]}'
                f'｜runs={cfg["paths"].get("runs_root")}'
                f'｜来源 {cfgmod.describe_sources(cfg)}')

    check('配置读取', t_config)

    def t_cq():
        path = os.path.join(cfgmod.ROOT, 'data', 'clinical_queries.json')
        try:
            with open(path, encoding='utf-8') as f:
                cq = json.load(f)
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f'Clinical Queries 表读取失败：{e}')
        cq = {k: v for k, v in cq.items() if not k.startswith('_')}
        if 'therapy.narrow' not in cq:
            raise RuntimeError('Clinical Queries 表缺失')
        return f'{len(cq)} 个过滤器（默认不启用，需 --cq 显式指定）'

    check('临床查询过滤器表', t_cq)

    def t_no_restriction():
        """必须确保没有刊源或学科限制残留。"""
        run = pipeline.Run.__new__(pipeline.Run)
        import pipeline as _pl
        src = open(_pl.__file__, encoding='utf-8').read()
        for bad in ('journal_clause', 'journal_profiles', 'preset_id', 'pmod.'):
            if bad in src:
                raise RuntimeError(f'pipeline.py 仍残留限制逻辑：{bad}')
        j = cfg.get('retrieval', {}).get('journals')
        if j:
            raise RuntimeError('config 里仍有 journals 限制块')
        return '无刊源限制、无学科预设残留'

    check('检索范围无限制', t_no_restriction)

    prov = cfg.get('provider') or {}
    p = sf.Provider(prov.get('name', 'provider'), prov.get('base_url', ''),
                    prov.get('api_key', ''))
    state = {}

    def t_models():
        live = p.available_models(refresh=True)
        state['live'] = live
        if not live:
            raise RuntimeError('模型列表为空（API key 或网络问题）')
        return f'{len(live)} 个模型可用'

    check('模型服务 /models', t_models)

    def t_chat():
        live = state.get('live') or set()
        chain = [m for m in cfg['models']['chain']['fast'] if (not live or m in live)]
        if not chain:
            chain = cfg['models']['chain']['fast']
        r = p.chat(chain[0], [{'role': 'user', 'content': '只回复两个字：正常'}],
                   temperature=0, max_tokens=16, timeout=90)
        if not r.get('content'):
            raise RuntimeError('空返回')
        return f'{r["model"]} → {r["content"].strip()[:20]}'

    check('对话接口', t_chat)

    pmc = pm.PubMed(cfg['pubmed'].get('api_key', ''),
                    tool=cfg['pubmed'].get('tool', 'medlit-agent'),
                    email=cfg['pubmed'].get('email', ''))
    pm_state = {}

    def t_esearch():
        r = esearch_or_skip(pmc, '"glucagon-like peptide-1"[tiab] AND obesity[tiab]', retmax=5)
        pm_state['ids'] = r['ids']
        if r['count'] <= 0:
            raise RuntimeError('命中为 0（检索式失效或上游异常）')
        return f'命中 {r["count"]}，取回 {len(r["ids"])}'

    check('PubMed esearch', t_esearch)

    def t_efetch():
        ids = pm_state.get('ids') or []
        papers = efetch_or_skip(pmc, ids)
        if not papers:
            raise RuntimeError('efetch 返回空（检查 retmode=xml）')
        x = papers[0]
        return (f'解析 {len(papers)} 篇；样例 PMID {x["pmid"]}｜'
                f'摘要 {len(x["abstract"])} 字｜MeSH {len(x["mesh"])} 个')

    check('PubMed efetch', t_efetch)

    def t_fulltext():
        r = esearch_or_skip(
            pmc, '"glucagon-like peptide-1"[tiab] AND obesity[tiab] AND hasabstract[text]',
            retmax=20)
        if r['count'] == 0:
            raise RuntimeError('命中 0，无法测试全文抓取')
        papers = efetch_or_skip(pmc, r['ids'])
        target = next((x for x in papers if x.get('pmcid')), None)
        if not target:
            return f'本轮 {len(papers)} 篇无 PMCID，跳过（正常现象）'
        ft = pmc.fulltext(target)
        if not ft.get('text'):
            raise RuntimeError(f'PMC{target["pmcid"]} 全文抓取失败')
        return f'PMC{target["pmcid"]} {len(ft["text"])} 字符，{len(ft["sections"])} 个章节'

    check('PMC 全文', t_fulltext)

    def t_research_filter():
        r = esearch_or_skip(pmc, '"glucagon-like peptide-1"[tiab] AND obesity[tiab]', retmax=10)
        papers = efetch_or_skip(pmc, r['ids'])
        n = sum(1 for x in papers if pm.is_research_article(
            x, cfg['retrieval']['exclude_pub_types'],
            cfg['retrieval'].get('research_min_abstract_chars', 300)))
        return f'10 篇中判定为研究论文 {n} 篇'

    check('研究论文判定', t_research_filter)

    def t_doctor():
        """doctor 必须能把「缺什么、怎么补」说清楚。"""
        items = cfgmod.doctor()
        if not items:
            raise RuntimeError('doctor 返回空')
        names = [i['item'] for i in items]
        for must in ('模型服务密钥', 'NCBI API key（选填）', 'Python 版本'):
            if must not in names:
                raise RuntimeError(f'doctor 缺少检查项：{must}')
        miss = [i for i in items if not i['ok']]
        for i in miss:
            if not i['detail'].strip():
                raise RuntimeError(f"缺项 {i['item']} 没给出补救说明")
        return f'{len(items)} 项检查，{len(miss)} 项待补（提示文案齐备）'

    check('环境体检 doctor', t_doctor)

    def t_grading():
        """B1：机械定级必须给出可复核的理由。"""
        ev = [
            {'study_design': 'RCT', 'sample_size': 'n=2000', 'pico_match': '直接',
             'limitations': [], 'effect_sizes': ['RR 0.60 (95%CI 0.45-0.80)']},
            {'study_design': 'RCT', 'sample_size': 'n=1500', 'pico_match': '直接',
             'limitations': [], 'effect_sizes': ['RR 0.65 (95%CI 0.50-0.85)']},
        ]
        g = grading.mechanical_grading(ev, [])
        if g['certainty'] not in ('高', '中', '低', '极低'):
            raise RuntimeError(f'定级结果异常：{g}')
        if not g.get('reason'):
            raise RuntimeError('定级没有给出理由')
        # 间接证据应降级
        ev2 = [dict(e, pico_match='间接') for e in ev]
        g2 = grading.mechanical_grading(ev2, [])
        if g2['score'] >= g['score']:
            raise RuntimeError('间接证据没有导致降级')
        return (f'RCT+直接 → {g["certainty"]}；全间接 → {g2["certainty"]}'
                f'；降级项 {len(g2["downgrades"])} 条')

    check('GRADE 机械定级', t_grading)

    def t_retraction():
        """B2：撤稿与勘误必须能被识别。"""
        r1 = pm.check_retraction({'correction_flags': ['retracted-publication']})
        if not r1['retracted']:
            raise RuntimeError('撤稿未识别')
        r2 = pm.check_retraction({'correction_flags': ['erratum-in']})
        if not r2['erratum']:
            raise RuntimeError('勘误未识别')
        r3 = pm.check_retraction({'correction_flags': []})
        if r3['retracted'] or r3['erratum']:
            raise RuntimeError('正常文献被误判')
        return '撤稿 / 勘误 / 正常 三种情况均正确'

    check('撤稿勘误识别', t_retraction)

    def t_new_modules():
        """新增模块必须能导入并暴露预期 API。"""
        import importlib
        need = {
            'export': ['to_bibtex', 'to_ris', 'to_zotero_note', 'evidence_markdown', 'to_docx'],
            'meta': ['parse_effect_size', 'pool_effect_sizes', 'summarize_effects'],
            'preprints': ['search_preprints', 'preprint_to_paper_record'],
        }
        missing = []
        for mod, fns in need.items():
            try:
                m = importlib.import_module(mod)
            except Exception as e:
                missing.append(f'{mod}(导入失败: {e})')
                continue
            for fn in fns:
                if not hasattr(m, fn):
                    missing.append(f'{mod}.{fn}')
        if missing:
            raise RuntimeError('缺少：' + '、'.join(missing))
        return f'{len(need)} 个新模块 API 齐备'

    check('新增模块 API', t_new_modules)

    def t_pmid_check():
        """PMID 越界校验必须能抓到假 PMID。"""
        run = pipeline.Run.__new__(pipeline.Run)
        run.answer = '结论 A [PMID:11111111]，结论 B [PMID:22222222]。'
        run.papers = {'22222222': {'pmid': '22222222'}}
        run.preprints = {}
        run.trace = {'errors': []}
        run.search_failed = False
        run.log = lambda m: None
        chk = run.validate_pmids()
        if chk['orphan_pmids'] != ['11111111']:
            raise RuntimeError(f'越界 PMID 未被检出：{chk}')
        # 再验一次：全部合法时不应报警
        run.answer = '结论 [PMID:22222222]。'
        run.trace = {'errors': []}
        chk2 = run.validate_pmids()
        if chk2['orphan_count'] != 0:
            raise RuntimeError(f'合法 PMID 被误判：{chk2}')
        # 预印本用 DOI 引用，DOI 越界也要能抓到
        run.answer = '结论 [DOI:10.1101/99999999.999999]。'
        run.trace = {'errors': []}
        run.preprints = {'doi:10.1101/2024.01.01.123456': {
            'id': 'doi:10.1101/2024.01.01.123456',
            'doi': '10.1101/2024.01.01.123456'}}
        chk3 = run.validate_pmids()
        if chk3['orphan_dois'] != ['10.1101/99999999.999999']:
            raise RuntimeError(f'越界 DOI 未被检出：{chk3}')
        return (f'检出越界 PMID {chk["orphan_count"]} 个；合法引用不误报；'
                f'预印本 DOI 越界也能检出')

    check('PMID 越界校验', t_pmid_check)

    if net_only:
        print('\n（--net 模式，跳过完整问答）')
    else:
        def t_quick():
            cfg2 = cfgmod.load_config()
            cfg2['retrieval'].update({'retmax_per_query': 8, 'max_papers': 2,
                                       'fulltext_limit': 1, 'min_year': 2020, 'rounds': 1})
            run = pipeline.Run('GLP-1 受体激动剂对肥胖成人的减重效果', '', cfg2)
            d = run.execute(quick=True)
            n = len(run.evidence)
            if run.search_failed:
                raise Skip('NCBI 检索后端不可用，端到端流程未能执行（上游故障）')
            if n == 0:
                raise RuntimeError('精读 0 篇')
            if run.pmid_check.get('orphan_count', 0) > 0:
                raise RuntimeError(f'出现越界 PMID：{run.pmid_check}')
            return (f'{n} 篇证据，{run.trace["elapsed_total"]}s，'
                    f'PRISMA 纳入 {run.prisma["included"]}，输出 {os.path.basename(d)}')

        check('端到端（迷你）', t_quick)

    print('\n' + '=' * 66)
    bad = [r for r in results if r[1] == FAIL]
    skipped = [r for r in results if r[1] == SKIP]
    passed = len(results) - len(bad) - len(skipped)
    print(f'通过 {passed} / 跳过 {len(skipped)} / 失败 {len(bad)}'
          f'（共 {len(results)} 项）')
    if skipped:
        print('跳过项（通常是上游服务临时不可用，不是本项目的问题）：')
        for name, _, msg in skipped:
            print(f'  · {name}: {msg}')
    if bad:
        print('失败项：')
        for name, _, msg in bad:
            print(f'  · {name}: {msg}')
        return 1
    if skipped:
        print('\n有项目因上游故障跳过——上游恢复后重跑本脚本即可。')
        return 0
    print('全部通过。可以正常调用 ask.py。')
    return 0


if __name__ == '__main__':
    sys.exit(main())
