# -*- coding: utf-8 -*-
"""命令行入口。

常用：
  python ask.py "GLP-1 受体激动剂对青少年肥胖的效果如何？"
  python ask.py "问题" --constraints "只关注中国人群，2020 年后"
  python ask.py --quick "问题"                 # 快速模式，约 3 分钟
  python ask.py --setup                       # 首次配置向导（缺什么会主动提示）
  python ask.py --doctor                      # 环境体检：缺什么、怎么补
  python ask.py --chat                        # 连续提问模式
  python ask.py --cq therapy.narrow "问题"    # 套用 PubMed 官方临床查询过滤器（可选）
  python ask.py --export bib "问题"           # 顺带导出 BibTeX / RIS / docx
  python ask.py --models                      # 看模型路由与实际可用情况
  python ask.py --pubmed-only "检索式"        # 只检索，不调模型
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:  # noqa: BLE001
    pass

import config as cfgmod
import pipeline
import pubmed as pm
import sf

try:
    import export as exportmod
except Exception:  # noqa: BLE001
    exportmod = None


# ---------------- 子命令实现 ----------------
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


def cmd_doctor(as_json: bool = False):
    """环境体检：把「缺什么、怎么补」一次说清楚。"""
    items = cfgmod.doctor()
    if as_json:
        print(json.dumps({'items': items,
                          'missing_required': cfgmod.missing_user_items()},
                         ensure_ascii=False, indent=2))
        return 0
    print('=' * 66)
    print('环境体检')
    print('=' * 66)
    missing = []
    for it in items:
        mark = '✓' if it['ok'] else ('!' if it.get('required') else '·')
        print(f'[{mark}] {it["item"]}')
        detail = str(it['detail'])
        if it['ok']:
            print(f'      {detail}')
        else:
            print(detail)
            if it.get('required'):
                missing.append(it)
    print('=' * 66)
    if missing:
        print(f'有 {len(missing)} 项必须由你自己提供（工具不会替你填写）：')
        for it in missing:
            print(f'  · {it["item"]}')
            if it.get('ask'):
                print(f'    该怎么问：{it["ask"]}')
        print('\n运行 `python scripts/ask.py --setup` 可以由向导采集并写入 .env。')
        return 1
    print('环境齐备，可以正常使用。')
    return 0


def first_run_notice():
    """首次使用提示：把「必须由使用者自己提供」的信息列在最显眼的位置。

    这一段会出现在每次运行的输出开头——智能体读到它就知道该先去问用户要什么，
    而不是自己编一个值填进去。
    """
    miss = cfgmod.missing_user_items()
    if not miss:
        return ''
    lines = ['', '=' * 66,
             '⚠️  首次使用提示：以下信息需要你自己提供，工具不会替你填写',
             '=' * 66]
    for i, it in enumerate(miss, 1):
        lines.append(f'{i}. {it["item"]}')
        if it.get('ask'):
            lines.append(f'   {it["ask"]}')
        lines.append(f'   填写方式：{it.get("how", "")}'.rstrip())
    lines.append('')
    lines.append('   或者运行 `python scripts/ask.py --setup` 由向导采集并自动写入 .env。')
    lines.append('=' * 66)
    lines.append('')
    return '\n'.join(lines)


def cmd_setup():
    """首次配置向导：只问必要的问题，其余用默认值。"""
    print('=' * 66)
    print('medlit-agent 配置向导')
    print('=' * 66)
    print('只需要三样东西。随时可以跳过，选填项留空即可。\n')

    endpoints = [
        ('siliconflow', '硅基流动（推荐，国内直连，开箱即用）'),
        ('deepseek', 'DeepSeek（便宜）'),
        ('kimi', 'Kimi / 月之暗面'),
        ('openai', 'OpenAI（国际用户）'),
        ('ollama', '本地 Ollama（完全离线）'),
    ]
    print('① 选择模型服务：')
    for i, (_key, label) in enumerate(endpoints, 1):
        print(f'   {i}. {label}')
    idx = _ask_int('序号（默认 1）', 1)
    key, _label = endpoints[max(0, min(len(endpoints) - 1, idx - 1))]
    base = {
        'siliconflow': 'https://api.siliconflow.cn/v1',
        'deepseek': 'https://api.deepseek.com/v1',
        'kimi': 'https://api.moonshot.cn/v1',
        'openai': 'https://api.openai.com/v1',
        'ollama': 'http://127.0.0.1:11434/v1',
    }[key]

    print('\n② 模型服务密钥（必填）')
    print(f'   端点：{base}')
    api_key = _ask('   粘贴你的 API Key（输入不回显）', default='', secret=True)
    if not api_key:
        print('   ✗ 没有填密钥，无法继续。稍后可以随时运行 --setup 补上。')
        return 1

    print('\n③ 你的邮箱（必填，必须是你自己的）')
    print('   NCBI 要求登记使用者的邮箱：万一有人违规使用这个工具导致你的 IP 被 NCBI 限流，')
    print('   NCBI 需要能联系到你。工具不会替你填写，也不会填别人的邮箱。')
    email = _ask('   你的邮箱', default='')
    if not email:
        print('   ✗ 没填邮箱。首次使用必须填自己的邮箱，稍后可以随时补。')
        return 1

    print('\n④ NCBI API key（选填）')
    print('   没有也能用，只是限速 3 请求/秒；申请：'
          'https://www.ncbi.nlm.nih.gov/account/settings/')
    ncbi_key = _ask('   NCBI API Key（可留空）', default='')

    env_name = 'SILICONFLOW_API_KEY' if key == 'siliconflow' else 'OPENAI_COMPATIBLE_API_KEY'
    values = {env_name: api_key}
    if ncbi_key:
        values['NCBI_API_KEY'] = ncbi_key
    if email:
        values['NCBI_EMAIL'] = email

    path = cfgmod.save_env(values)
    print(f'\n✓ 已写入 {path}')

    # 非默认端点要落到 config.json
    if key != 'siliconflow':
        _write_provider_config(key, base, env_name)
        print(f'✓ 已把端点写入 config.json（provider = {key}）')

    print('\n正在自检……')
    try:
        cfg = cfgmod.load_config()
        p = sf.Provider(cfg['provider']['name'], cfg['provider']['base_url'],
                        cfg['provider']['api_key'])
        live = p.available_models(refresh=True)
        if not live:
            print('  ! 模型端点连不上或密钥无效，请检查。')
            return 1
        print(f'  ✓ 模型端点可用（{len(live)} 个模型）')
    except Exception as e:  # noqa: BLE001
        print(f'  ! 自检失败：{e}')
        return 1
    print('\n配置完成，现在可以提问了：'
          '\n  python scripts/ask.py "你的问题"')
    return 0


def _write_provider_config(key: str, base: str, env_name: str):
    import copy
    src = cfgmod.EXAMPLE_CONFIG
    # 优先以**已有的 config.local.json** 为基底，只改 provider 段。
    # 否则会用模板整体覆盖，把使用者在这个文件里改过的其他设置全丢掉。
    data = {}
    try:
        with open(cfgmod.LOCAL_CONFIG, encoding='utf-8') as f:
            data = json.load(f)
    except Exception:  # noqa: BLE001
        data = {}
    if not data:
        try:
            with open(src, encoding='utf-8') as f:
                data = json.load(f)
        except Exception:  # noqa: BLE001
            data = {}
    prov = data.setdefault('provider', {})
    prov['name'] = key
    prov['base_url'] = base
    prov['api_key_env'] = env_name
    rr = prov.setdefault('rerank', {})
    rr['enabled'] = False
    rr['endpoint'] = ''
    out = cfgmod.LOCAL_CONFIG
    with open(out, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _ask(prompt: str, default: str = '', secret: bool = False) -> str:
    import getpass
    try:
        if secret:
            v = getpass.getpass(prompt + ': ')
        else:
            v = input(prompt + ': ').strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return default
    return (v or '').strip() or default


def _ask_int(prompt: str, default: int) -> int:
    v = _ask(prompt)
    try:
        return int(v)
    except ValueError:
        return default


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


def cmd_list_cq():
    path = os.path.join(cfgmod.ROOT, 'data', 'clinical_queries.json')
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
    except Exception as e:  # noqa: BLE001
        print(f'读取失败：{e}')
        return
    print('可用的 PubMed 官方 Clinical Query 过滤器（用 --cq <id> 启用）：\n')
    for k, v in data.items():
        if k.startswith('_'):
            continue
        print(f'  {k:22s} {v["label"]}')
    print('\n这些过滤器默认不启用。用 --cq therapy.narrow 等显式指定才会生效。')


def cmd_chat(cfg, args):
    """E2：连续提问模式，上下文自动带。

    追问时会**沿用上一轮的检索式**：重新检索既白花钱，
    又可能捞到和上次不一样的文献，连着看很别扭。
    """
    history: list[dict] = []
    prev_queries: list[dict] = []
    print('连续提问模式。输入问题，Enter 提交；输入 quit / exit 退出。')
    print('（每次提问都会跑一次检索，但会沿用上一轮的检索式，请稍等）\n')
    while True:
        try:
            q = input('问题> ').strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not q:
            continue
        if q.lower() in ('quit', 'exit', ':q', '退出'):
            break
        run = pipeline.Run(q, args.constraints, cfg, cq_explicit=_cq_list(args),
                           history=list(history),
                           reuse_queries=list(prev_queries))
        out_dir = run.execute(quick=args.quick)
        history.append({'question': q,
                        'core_question_en': run.plan.get('core_question_en', '')})
        # 记下本轮实际用的检索式（含沿用的），下一轮继续沿用。
        # 上限 6 条：再多检索式会明显拖慢，而更早的那些通常已经被覆盖了。
        prev_queries = (run.plan.get('queries') or [])[-6:]
        print('\n' + '=' * 70)
        print(run.answer[:2000])
        print(f'\n（完整答案见 {out_dir}/07_answer.md）\n')
    return 0


def cmd_compare(cfg, args):
    """N5：两个干预的横向对比。

    分别跑两轮完整检索，然后并排给出：证据量、研究设计、GRADE 确定性、
    关键效应量、直接证据占比，以及差异说明与措辞谨慎的结论。
    """
    import compare as comparemod

    arms = []
    for label in args.compare:
        print(f'\n===== 检索「{label}」 =====')
        q = f'{args.question} {label}' if args.question else label
        run = pipeline.Run(q, args.constraints, cfg, cq_explicit=_cq_list(args))
        # stages='grade'：只跑到证据分级。横向对比真正需要的只有
        # evidence / pooled / mech_grading 三样，成文、审稿、语义比对全是白花钱。
        out_dir = run.execute(quick=args.quick, stages='grade')
        arms.append({
            'label': label,
            'evidence': run.evidence,
            'pooled': run.pooled,
            'grading': run.mech_grading,
            'query': run.plan.get('core_question_en', ''),
            'dir': out_dir,
            # 表格要在这里就取好：写文件时循环变量 run 指向的是最后一个 arm，
            # 直接在循环里调 run.evidence_table() 会让所有 arm 的文件内容都是最后一个的
            'evidence_table': run.evidence_table(),
        })
        print(f'  → 纳入 {len(run.evidence)} 篇，'
              f'GRADE {run.mech_grading.get("certainty", "未评估")}')

    table = comparemod.build_comparison(arms, args.question or '')
    summary = comparemod.compare_summary(arms)
    wording = comparemod.recommend_wording(arms)

    out_dir = os.path.join(os.path.abspath(
        cfg['paths'].get('runs_root') or './runs'),
        comparemod.comparison_dir_name(args.question or ' '.join(args.compare)))
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, 'comparison.md'), 'w', encoding='utf-8') as f:
        f.write('# 干预横向对比\n\n')
        f.write(table + '\n\n')
        f.write('## 结论措辞\n\n' + wording + '\n')
    for arm in arms:
        if arm.get('dir'):
            # label 是用户输入的中文干预名，可能带 / \ : * 等非法文件名字符
            safe = re.sub(r'[\\/:*?"<>|]+', '_', str(arm['label']))[:40] or 'arm'
            with open(os.path.join(out_dir, f'evidence_{safe}.md'),
                      'w', encoding='utf-8') as f:
                f.write(arm.get('evidence_table', '') + '\n')
    with open(os.path.join(out_dir, 'comparison.json'), 'w', encoding='utf-8') as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    # 对比模式跑的是 N 份「检索 + 精读 + 分级」流程，
    # 成本与耗时约为单次查询的 N 倍——必须告诉使用者，否则容易意外超支
    n = len(arms)
    print('\n' + '=' * 70)
    print(f'对比结果：{out_dir}')
    print(f'提示：本次共跑了 {n} 份检索与证据分级流程'
          f'（已跳过综述成文与语义比对，那部分对横向对比没有用），'
          f'模型调用约为单次完整查询的 {n} 倍。')
    print('=' * 70)
    print(table)
    print('\n结论措辞：\n' + wording)
    return 0


def _cq_list(args):
    if not args.cq:
        return []
    return [x.strip() for x in args.cq.replace('，', ',').split(',') if x.strip()]


def _do_exports(run, out_dir, fmt: str):
    """C1/C2/C3：导出参考文献与笔记。"""
    if not exportmod:
        print('  ! 导出模块不可用')
        return
    papers = [run.papers.get(d['pmid'], {}) for d in run.evidence]
    papers = [p for p in papers if p]
    try:
        if fmt == 'bib':
            path = os.path.join(out_dir, 'references.bib')
            with open(path, 'w', encoding='utf-8') as f:
                f.write(exportmod.to_bibtex(papers))
            print(f'  ✓ BibTeX：{path}')
        elif fmt == 'ris':
            path = os.path.join(out_dir, 'references.ris')
            with open(path, 'w', encoding='utf-8') as f:
                f.write(exportmod.to_ris(papers))
            print(f'  ✓ RIS：{path}')
        elif fmt == 'notes':
            path = os.path.join(out_dir, 'zotero_notes.md')
            with open(path, 'w', encoding='utf-8') as f:
                for d in run.evidence:
                    p = run.papers.get(d['pmid'], {})
                    f.write(exportmod.to_zotero_note(p, d) + '\n\n---\n\n')
            print(f'  ✓ Zotero 笔记：{path}')
        elif fmt == 'docx':
            md = (f'# {run.plan.get("core_question_en") or run.question}\n\n'
                  f'{run.answer}\n\n---\n\n## 关键文献\n\n{run.bibliography()}\n')
            path = os.path.join(out_dir, 'answer.docx')
            exportmod.to_docx(md, path)
            print(f'  ✓ Word 文档：{path}')
    except Exception as e:  # noqa: BLE001
        print(f'  ! 导出失败：{e}')


# ---------------- 主入口 ----------------
def main():
    ap = argparse.ArgumentParser(
        description='循证医学文献检索 Agent',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='示例：\n'
               '  python ask.py "GLP-1 受体激动剂对青少年肥胖的效果如何？"\n'
               '  python ask.py "TIR 与糖尿病并发症风险" --cq prognosis.narrow\n'
               '  python ask.py "问题" --constraints "2020 年后，中国人群" --quick\n'
               '  python ask.py --setup        # 首次配置\n')
    ap.add_argument('question', nargs='?', help='你的问题（中文即可）')
    ap.add_argument('--constraints', default='', help='补充限定，如人群/年份/地区')
    ap.add_argument('--quick', action='store_true', help='快速模式：单轮、少读几篇')
    ap.add_argument('--max-papers', type=int, help='精读篇数上限')
    ap.add_argument('--fulltext-limit', type=int, help='尝试抓全文的篇数')
    ap.add_argument('--research-only', action='store_true', help='只保留研究论文')
    ap.add_argument('--cq', metavar='ID[,ID]', help='PubMed 官方临床查询过滤器 id（默认不启用）')
    ap.add_argument('--rounds', type=int, help='检索轮数（2 表示必要时做一轮扩写）')
    ap.add_argument('--no-animal-exclusion', action='store_true',
                    help='不排除纯动物实验（默认排除）')
    ap.add_argument('--no-links', action='store_true', help='不做被引/相似文献扩展')
    ap.add_argument('--no-snowball', action='store_true', help='不做引文滚雪球扩展')
    ap.add_argument('--no-cache', action='store_true',
                    help='不用检索缓存（要可复现、或怀疑缓存过期时用）')
    ap.add_argument('--preprints', action='store_true', help='同时检索 bioRxiv/medRxiv 预印本')
    ap.add_argument('--export', choices=['bib', 'ris', 'notes', 'docx'],
                    help='跑完后额外导出参考文献或笔记')
    ap.add_argument('--compare', nargs=2, metavar=('A', 'B'),
                    help='横向对比两个干预：分别检索后输出对照表与各自的 GRADE')
    ap.add_argument('--reuse-plan', metavar='FILE',
                    help='复用上次保存的检索式（02_plan.json），不重新规划')
    ap.add_argument('--resume', metavar='DIR', help='从上次运行的目录断点续跑')
    ap.add_argument('--chat', action='store_true', help='连续提问模式')
    ap.add_argument('--setup', action='store_true', help='首次配置向导')
    ap.add_argument('--doctor', action='store_true', help='环境体检：缺什么、怎么补')
    ap.add_argument('--doctor-json', action='store_true',
                    help='环境体检的 JSON 输出（供智能体解析后逐项向用户询问）')
    ap.add_argument('--models', action='store_true', help='打印模型路由链')
    ap.add_argument('--list-cq', action='store_true', help='列出可用的临床查询过滤器')
    ap.add_argument('--pubmed-only', metavar='TERM', help='只检索 PubMed，不调模型')
    ap.add_argument('--retmax', type=int, default=10, help='--pubmed-only 的取回条数')
    ap.add_argument('--json', action='store_true', help='以 JSON 输出结果摘要')
    ap.add_argument('--config', help='配置文件路径')
    args = ap.parse_args()

    if args.setup:
        return cmd_setup()
    if args.doctor_json:
        return cmd_doctor(as_json=True)
    if args.doctor:
        return cmd_doctor()
    if args.list_cq:
        cmd_list_cq()
        return 0

    # --models / --pubmed-only 不需要模型密钥也能跑
    cfg = pipeline.load_config(args.config) if args.models else cfgmod.load_config(args.config)

    if args.models:
        cmd_models(cfg)
        return 0
    if args.pubmed_only:
        cmd_pubmed_only(cfg, args.pubmed_only, args.retmax)
        return 0

    if args.chat:
        return cmd_chat(cfg, args)

    if args.compare:
        if not args.question:
            args.question = '比较两种干预的疗效与安全性证据'
        return cmd_compare(cfg, args)

    if not args.question:
        ap.print_help()
        return 2

    # ---- 首次使用提示：必须由使用者自己提供的信息，放在输出最显眼处 ----
    notice = first_run_notice()
    if notice:
        print(notice)

    # ---- 参数覆盖配置 ----
    if args.quick:
        # N11：快速模式只关掉「第二轮扩写」与两种链接扩展，
        # **保留检索式质量闸门**（max_revisions）——检索式写歪了才是最大的坑，
        # 快速模式同样要能自我修正。
        #
        # 另外关掉两项**后置校验**：B8 相关度交叉验证（每篇文献跑两遍模型）
        # 与 B5 论断语义比对（最多 12 条论断各跑一遍模型）。这两项是纯成本项，
        # 不影响检索与写作质量，留给完整模式即可。
        cfg['retrieval'].update({'rounds': 1, 'retmax_per_query': 12,
                                 'max_papers': 4, 'fulltext_limit': 2,
                                 'expand_by_links': False, 'expand_by_references': False,
                                 'cross_check_relevance': False,
                                 'verify_claims': False})
    if args.no_cache:
        cfg['retrieval']['no_cache'] = True
    if args.max_papers:
        cfg['retrieval']['max_papers'] = args.max_papers
    if args.fulltext_limit is not None:
        cfg['retrieval']['fulltext_limit'] = args.fulltext_limit
    if args.research_only:
        cfg['retrieval']['research_only'] = True
    if args.rounds is not None:
        cfg['retrieval']['rounds'] = args.rounds
    if args.no_animal_exclusion:
        cfg['retrieval']['exclude_animals'] = False
    if args.no_links:
        cfg['retrieval']['expand_by_links'] = False
    if args.no_snowball:
        cfg['retrieval']['expand_by_references'] = False
    if args.preprints:
        cfg['retrieval']['include_preprints'] = True

    run = pipeline.Run(args.question, args.constraints, cfg,
                       cq_explicit=_cq_list(args), resume_dir=args.resume)

    # A8：复用检索式
    if args.reuse_plan:
        try:
            with open(args.reuse_plan, encoding='utf-8') as f:
                run.plan = json.load(f)
            run.log(f'↻ 已复用检索式：{args.reuse_plan}')
        except Exception as e:  # noqa: BLE001
            print(f'✗ 读取检索式失败：{e}')
            return 2

    out_dir = run.execute(quick=args.quick, resume=bool(args.resume))

    if args.export:
        _do_exports(run, out_dir, args.export)

    if args.json:
        print(json.dumps({'dir': out_dir,
                          'answer': run.answer,
                          'counts': run.trace.get('counts', {}),
                          'prisma': run.trace.get('prisma', {}),
                          'pmid_check': run.trace.get('pmid_check', {}),
                          'grading': run.trace.get('mechanical_grading', {}),
                          'pooled': run.pooled,
                          'models': run.trace.get('models', {}),
                          'tokens': run.trace.get('tokens', {}),
                          'cost': run.trace.get('cost_estimate', {}),
                          'scope': run.trace.get('scope_note', ''),
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
    try:
        sys.exit(main())
    except cfgmod.MissingSecretError as e:
        print('\n✗ 配置不完整\n')
        print(str(e))
        print('\n补齐后重试，或运行 `python scripts/ask.py --setup` 由向导代填。')
        sys.exit(2)
