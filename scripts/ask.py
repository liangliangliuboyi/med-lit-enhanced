# -*- coding: utf-8 -*-
"""命令行入口。

常用：
  python ask.py "GLP-1 受体激动剂对青少年肥胖的效果如何？"
  python ask.py "问题" --constraints "只关注中国人群，2020 年后"
  python ask.py --quick "问题"                 # 快速模式，约 3 分钟
  python ask.py --preset diabetes-metabolism "问题"   # 指定学科域预设
  python ask.py --journals clinical "问题"    # 限定临床核心期刊
  python ask.py --cq therapy.narrow "问题"    # 套用 PubMed 官方临床查询过滤器
  python ask.py --models                      # 看模型路由与实际可用情况
  python ask.py --pubmed-only "检索式"        # 只检索，不调模型
  python ask.py --list-presets                # 列出所有学科域预设
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:  # noqa: BLE001
    pass

import config as cfgmod
import pipeline
import presets as pmod
import pubmed as pm
import sf


def cmd_models(cfg):
    prov = cfg.get('provider') or {}
    print(f'服务端点：{prov.get("name", "provider")}  {prov.get("base_url", "")}')
    print(f'配置来源：{cfgmod.describe_sources(cfg)}\n')
    p = sf.Provider(prov.get('name', 'provider'), prov.get('base_url', ''),
                    prov.get('api_key', ''))
    live = p.available_models(refresh=True)
    print(f'平台可用模型总数：{len(live)}\n')
    router = sf.ModelRouter(p, cfg['models']['chain'])
    print('角色路由链（OK=可用 / --=未出现在 /models，仍会尝试）')
    for role in ('planner', 'reader', 'critic', 'writer', 'fast'):
        print(f'\n[{role}]')
        for i, m in enumerate(router.chain_of(role)[:6], 1):
            mark = 'OK' if (not live or m in live) else '--'
            print(f'  {i}. [{mark}] {m}')
    rr = (cfg.get('provider') or {}).get('rerank') or {}
    print(f"\nrerank: {rr.get('model', '-')} "
          f"{'OK' if rr.get('enabled') and rr.get('model') in live else '--'}"
          f"（enabled={rr.get('enabled')}）")


def cmd_pubmed_only(cfg, term, retmax):
    p = pm.PubMed(cfg['pubmed'].get('api_key', ''),
                  tool=cfg['pubmed'].get('tool', 'medlit-agent'),
                  email=cfg['pubmed'].get('email', ''))
    try:
        r = p.esearch(term, retmax=retmax)
    except Exception as e:  # noqa: BLE001
        print(f'✗ PubMed 检索失败：{e}\n'
              f'  若提示 500 / "Search Backend failed" / "Cannot connect to SOLR"，\n'
              f'  说明是 NCBI 检索后端临时故障，与检索式无关，稍后重试即可。')
        return
    if r.get('error'):
        print(f'✗ PubMed 检索后端暂不可用：{r["error"]}\n'
              f'  这是 NCBI 上游故障（常见于访问高峰），不是检索式的问题，稍后重试即可。')
        return
    papers = p.efetch(r['ids']) if r['ids'] else []
    print(f'命中 {r["count"]} 条，取回 {len(papers)} 条\n')
    if r.get('translation'):
        print(f'PubMed 理解成：{r["translation"]}\n')
    for i, x in enumerate(papers, 1):
        print(f'[{i}] PMID {x["pmid"]} | {x["year"]} | {x["journal"]}')
        print(f'    {x["title"]}')
        print(f'    类型: {", ".join(x["pub_types"][:4])}')
        print(f'    MeSH: {", ".join(x["mesh"][:6])}')
        print(f'    关键词: {", ".join(x["keywords"][:8])}')
        print(f'    摘要: {(x["abstract"] or "")[:360]}…\n')


def cmd_list_presets():
    print('可用的学科域预设：\n')
    for p in pmod.list_presets():
        print(f'  {p["id"]}')
        print(f'    {p["name"]}｜期刊画像 {p["journal_profile"]}｜'
              f'Clinical Query: {", ".join(p["cq"]) or "（无）"}')
        if p['description']:
            print(f'    {p["description"]}')
        print()
    print('用法：--preset <id>  显式指定；不指定则按问题关键词自动识别；'
          '--no-preset 关闭。')
    print('自定义：把自己的 JSON 放进 presets/ 目录即可生效，无需改代码。')


def main():
    ap = argparse.ArgumentParser(
        description='循证医学文献检索 Agent',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='示例：\n'
               '  python ask.py "二甲双胍对青少年肥胖的疗效" --preset diabetes-metabolism\n'
               '  python ask.py "TIR 与糖尿病并发症风险" --journals clinical --cq therapy.broad\n')
    ap.add_argument('question', nargs='?', help='你的问题（中文即可）')
    ap.add_argument('--constraints', default='', help='补充限定，如人群/年份/地区')
    ap.add_argument('--quick', action='store_true', help='快速模式：单轮、少读几篇')
    ap.add_argument('--max-papers', type=int, help='精读篇数上限')
    ap.add_argument('--fulltext-limit', type=int, help='尝试全文的篇数')
    ap.add_argument('--research-only', action='store_true', help='只保留研究论文')
    ap.add_argument('--journals', nargs='?', const='__auto__', default=None,
                    help='限定期刊：不给值用预设画像（默认 clinical），'
                         '也可给 profile 名（clinical/basic_science/public_health）')
    ap.add_argument('--journal-list', metavar='N1,N2,...',
                    help='临时指定期刊名单（逗号分隔，自动开启限定并覆盖画像）')
    ap.add_argument('--journal-mode', choices=['ta', 'full'], default=None,
                    help='ta=缩写精确匹配（默认）；full=全称匹配')
    ap.add_argument('--preset', metavar='ID', help='指定学科域预设')
    ap.add_argument('--no-preset', action='store_true', help='关闭学科域预设')
    ap.add_argument('--list-presets', action='store_true', help='列出所有预设')
    ap.add_argument('--cq', metavar='ID[,ID]', help='PubMed 官方临床查询过滤器 id')
    ap.add_argument('--rounds', type=int, help='检索轮数（2 表示必要时做一轮扩写）')
    ap.add_argument('--models', action='store_true', help='打印模型路由链')
    ap.add_argument('--pubmed-only', metavar='TERM', help='只检索 PubMed，不调模型')
    ap.add_argument('--retmax', type=int, default=10, help='--pubmed-only 的取回条数')
    ap.add_argument('--json', action='store_true', help='以 JSON 输出结果摘要')
    ap.add_argument('--config', help='配置文件路径')
    args = ap.parse_args()

    if args.list_presets:
        cmd_list_presets()
        return 0

    # --models / --pubmed-only 不需要模型密钥也能跑
    cfg = pipeline.load_config(args.config) if args.models else cfgmod.load_config(args.config)

    if args.models:
        cmd_models(cfg)
        return 0

    if args.pubmed_only:
        cmd_pubmed_only(cfg, args.pubmed_only, args.retmax)
        return 0

    if not args.question:
        ap.print_help()
        return 2

    # ---- 参数覆盖配置 ----
    if args.quick:
        cfg['retrieval'].update({'rounds': 1, 'retmax_per_query': 12,
                                 'max_papers': 4, 'fulltext_limit': 2})
    if args.max_papers:
        cfg['retrieval']['max_papers'] = args.max_papers
    if args.fulltext_limit is not None:
        cfg['retrieval']['fulltext_limit'] = args.fulltext_limit
    if args.research_only:
        cfg['retrieval']['research_only'] = True
    if args.rounds is not None:
        cfg['retrieval']['rounds'] = args.rounds

    jcfg = cfg['retrieval'].setdefault('journals', {})
    if args.journals is not None:
        jcfg['enabled'] = True
        if args.journals != '__auto__':
            jcfg['profile'] = args.journals
        elif not jcfg.get('profile'):
            jcfg['profile'] = 'clinical'
    if args.journal_list:
        names = [x.strip() for x in args.journal_list.replace('，', ',').split(',') if x.strip()]
        if names:
            jcfg['list'] = names
            jcfg['enabled'] = True
    if args.journal_mode:
        jcfg['mode'] = args.journal_mode

    cq_list = []
    if args.cq:
        cq_list = [x.strip() for x in args.cq.replace('，', ',').split(',') if x.strip()]

    try:
        run = pipeline.Run(args.question, args.constraints, cfg,
                           preset_id=args.preset, no_preset=args.no_preset,
                           cq_explicit=cq_list)
    except KeyError as e:
        print(f'预设错误：{e}')
        return 2

    out_dir = run.execute(quick=args.quick)

    if args.json:
        print(json.dumps({'dir': out_dir,
                          'counts': run.trace.get('counts', {}),
                          'prisma': run.trace.get('prisma', {}),
                          'pmid_check': run.trace.get('pmid_check', {}),
                          'models': run.trace.get('models', {}),
                          'preset': run.trace.get('preset', {}),
                          'elapsed': run.trace.get('elapsed_total')},
                         ensure_ascii=False, indent=2))
    else:
        print('\n' + '=' * 70)
        print('答案目录：', out_dir)
        print('=' * 70)
        print(run.answer[:2000])
        print('\n（完整答案见 07_answer.md，证据总表见 05_evidence_table.md，'
              'PRISMA 流程见 11_prisma.md，参考文献见 08_bibliography.md）')
    return 0


if __name__ == '__main__':
    sys.exit(main())
