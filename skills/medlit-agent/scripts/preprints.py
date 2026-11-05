# -*- coding: utf-8 -*-
"""预印本与补充语料检索。

为什么要分两个源
----------------
- **Europe PMC**（主源）：收录 bioRxiv/medRxiv 预印本、专利、以及 PubMed 之外的部分期刊，
  **并且支持真正的关键词检索**。查询语法与 PubMed 类似（`TITLE:"..."`、`AND`/`OR`）。
- **bioRxiv / medRxiv 官方 API**（备选）：接口稳定但**不支持关键词检索**，
  只能按时间倒序拉取再本地过滤，召回率低。仅在 Europe PMC 不可用时兜底。

两个源都不返回 PMID（预印本没有 PubMed 编号），因此**不伪造 PMID**，
统一用 `doi:` 前缀标识，论文记录里 `pmid` 留空。

踩过的坑（改代码前务必看）：
1. **Europe PMC 的 source 必须写进检索式，不能当 URL 参数传。**
   `?source=PPR` 会被静默忽略，返回的是 PubMed 收录的正式论文（实测 48144 条），
   而不是预印本——而且这些记录带真 PMID，会被当成"有 PMID 的预印本"混进证据池。
   正确写法是 `query=SRC:PPR AND (关键词)`，这样才真的只返回预印本（实测 573 条）。
2. **bioRxiv 官方 API 的 URL 形式变过。**
   早年的 `https://api.biorxiv.org/details/{server}/{start}/{count}` 现在会返回
   `{"messages":[{"status":"Both dates must be in yyyy-mm-dd format"}],"collection":[]}`
   ——HTTP 200 但一条数据都没有，且**不报错**，看起来像"检索到 0 篇"。
   当前可用形式是 `https://api.biorxiv.org/details/{server}/{YYYY-MM-DD}/{YYYY-MM-DD}/{cursor}`。
3. **HTTP 200 不代表成功。** 上游故障时返回 200 + `messages[0].status != 'ok'`，
   必须显式判断 status，否则会把"上游挂了"误报成"命中 0 篇"。
4. **cursor 是记录偏移量，不是页码**，每页固定 30 条，且窗口内按日期**升序**排列。
   所以最新的记录在**末尾**：`cursor = total - 30` 才是最新的一批。
5. **只扫最新的一批召回很差。** 实测某主题在"最新 240 条"里命中 0 篇，
   同样 240 条分层抽样能命中 3~8 篇。所以扫描策略是一半取最新（保时效）、
   一半在窗口内等距分层（保覆盖）。
6. **同一 DOI 会有多个版本**（v1/v2/v3 各占一条记录），需按 DOI 去重、保留最新版。
7. **单次请求要 2~4 秒**，串行翻页很慢，所以用线程池并发抓取。
8. **作者串是分号分隔的**："Harvey, D. Y.; Stoll, H."。按逗号切会切成
   ['Harvey', 'D. Y.', 'Stoll', 'H.']，必须先按分号切再处理每人的姓/名逗号。
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

UA = 'medlit-agent/2.2 (preprint retrieval)'

EUROPEPMC = 'https://www.ebi.ac.uk/europepmc/webservices/rest'
BIORXIV = 'https://api.biorxiv.org'

# Europe PMC 的 source 取值：PPR=预印本 MED=PubMed PAT=专利 AGR=农业书目
SOURCES = {'PPR': '预印本', 'MED': 'PubMed', 'PAT': '专利', 'AGR': 'AGR'}

# bioRxiv 官方 API 的抓取参数
BIORXIV_PAGE = 30          # 每页固定 30 条
BIORXIV_WINDOW_DAYS = 365  # 时间窗口：往前推多久
BIORXIV_SCAN_PAGES = 16    # 每个站点最多扫描多少页（16 页 = 480 条）
BIORXIV_WORKERS = 12       # 并发抓页的线程数

# 关键词匹配的停用词：太常见，命中它们等于没过滤
_STOPWORDS = {
    'and', 'the', 'for', 'with', 'from', 'that', 'this', 'are', 'was',
    'were', 'not', 'but', 'has', 'have', 'had', 'its', 'our', 'their',
    'which', 'when', 'what', 'who', 'how', 'why', 'all', 'any', 'can',
    'may', 'one', 'two', 'new', 'use', 'used', 'using', 'via', 'per',
}


def _get_json(url: str, timeout: int = 45, retries: int = 2) -> dict:
    """带一次重试的 JSON GET。失败抛异常，由调用方转成 error 字段。"""
    last = None
    for attempt in range(retries):
        req = urllib.request.Request(url, headers={'User-Agent': UA})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode('utf-8', 'ignore'))
        except urllib.error.HTTPError as e:
            last = RuntimeError(f'HTTP {e.code}')
            if e.code == 429 or 500 <= e.code < 600:
                continue
            raise last
        except Exception as e:  # noqa: BLE001
            last = e
    raise last if last else RuntimeError('未知错误')


# ---------------------------------------------------------------------------
# Europe PMC
# ---------------------------------------------------------------------------
def search_europepmc(term: str, retmax: int = 25, source: str = 'PPR') -> dict:
    """检索 Europe PMC。

    返回 {'count','ids','papers','error','hit_count'}。失败时 error 非空、ids 为空，
    不抛异常。

    注意：source 是**写进检索式**的（SRC:PPR），不是 URL 参数。
    当成 URL 参数传会被静默忽略，返回 PubMed 正式论文而不是预印本。
    """
    if not term or not term.strip():
        return {'count': 0, 'ids': [], 'papers': [], 'error': '检索词为空',
                'hit_count': 0}
    query = term.strip()
    if source:
        query = f'SRC:{source} AND ({query})'
    params = {
        'query': query,
        'format': 'json',
        'pageSize': str(max(1, min(100, retmax))),
        'resultType': 'core',
    }
    url = f'{EUROPEPMC}/search?' + urllib.parse.urlencode(params)
    try:
        data = _get_json(url)
    except Exception as e:  # noqa: BLE001
        return {'count': 0, 'ids': [], 'papers': [], 'error': str(e)[:200],
                'hit_count': 0}

    result = (data.get('resultList') or {}).get('result') or []
    papers = []
    for r in result:
        rec = _europepmc_to_record(r)
        if rec:
            papers.append(rec)
    ids = [p['id'] for p in papers]
    # count 与 ids 长度保持一致（hitCount 是上游总命中数，另存字段）
    return {'count': len(papers), 'ids': ids, 'papers': papers, 'error': '',
            'hit_count': int(data.get('hitCount') or 0)}


def _europepmc_to_record(r: dict) -> dict | None:
    """Europe PMC 原始记录 → 内部统一的 paper record。"""
    title = (r.get('title') or '').strip()
    if not title:
        return None
    doi = (r.get('doi') or '').strip()
    pmid = (r.get('pmid') or '').strip()
    src = (r.get('source') or '').strip()
    # 标识：预印本没有 PMID，用 doi 兜底
    ident = pmid or (f'doi:{doi}' if doi else f'epmc:{r.get("id", "")}')
    authors = []
    for a in ((r.get('authorList') or {}).get('author') or []):
        last = (a.get('lastName') or '').strip()
        initials = (a.get('initials') or '').strip()
        if last:
            authors.append(f'{last} {initials}'.strip())
        elif (a.get('fullName') or '').strip():
            authors.append(a['fullName'].strip())
    abstract = (r.get('abstractText') or '').strip()
    journal = ''
    ji = r.get('journalInfo') or {}
    if isinstance(ji, dict):
        journal = ((ji.get('journal') or {}) or {}).get('title', '') or ''
        if not journal:
            journal = (ji.get('journal') or {}).get('medlineAbbreviation', '') or ''
    year = str(r.get('pubYear') or '').strip()
    # P7：部分预印本平台（如 Research Square）的 pubYear 不准，
    # firstPublicationDate 更接近真实首次公开时间，优先用它。
    fpd = str(r.get('firstPublicationDate') or '').strip()
    if len(fpd) >= 4 and fpd[:4].isdigit():
        year = fpd[:4]
    ptypes = [t for t in (r.get('pubTypeList') or {}).get('pubType', []) if t]
    return {
        'id': ident,
        'pmid': pmid,
        'doi': doi,
        'title': re.sub(r'\s+', ' ', title),
        'abstract': abstract,
        'authors': authors,
        'journal': journal or f'{"Europe PMC " + SOURCES.get(src, src)}',
        'journal_short': journal,
        'year': year,
        'mesh': [m for m in (r.get('meshHeadingList') or {}).get('meshHeading', []) if m],
        'keywords': [k for k in (r.get('keywordList') or {}).get('keyword', []) if k],
        'pub_types': ptypes or ['Preprint'],
        'pmcid': '',
        'source': src or 'PPR',
        'preprint': src == 'PPR',
        'is_open_access': r.get('isOpenAccess') == 'Y',
        'cited_by_count': r.get('citedByCount', 0),
    }


# ---------------------------------------------------------------------------
# bioRxiv / medRxiv（备选）
# ---------------------------------------------------------------------------
def _tokenize(term: str) -> list[str]:
    """把检索词拆成用于本地匹配的关键词列表。

    - 英文/数字按连字符保留（"GLP-1" 整体保留，另补一个 "glp1" 变体）；
    - 中文按连续汉字成词；
    - 长度 < 3 的词丢弃（"or"、"1" 这类只会制造假命中）；
    - 常见停用词丢弃。
    """
    if not isinstance(term, str) or not term.strip():
        return []
    toks: list[str] = []
    for raw in re.findall(r'[A-Za-z0-9][A-Za-z0-9\-]*|[\u4e00-\u9fff]+', term.lower()):
        t = raw.strip('-')
        if len(t) < 3 or t in _STOPWORDS:
            continue
        toks.append(t)
        if '-' in t:  # "GLP-1" 同时匹配 "GLP1" 写法
            flat = t.replace('-', '')
            if len(flat) >= 3 and flat not in _STOPWORDS:
                toks.append(flat)
    seen: set[str] = set()
    out: list[str] = []
    for t in toks:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def _matches(record: dict, tokens: list[str]) -> bool:
    """标题/摘要里包含任一关键词即命中（不区分大小写）。"""
    if not tokens:
        return True
    hay = ' '.join([str(record.get('title') or ''),
                    str(record.get('abstract') or '')]).lower()
    if not hay.strip():
        return False
    return any(tok in hay for tok in tokens)


def _split_authors(raw) -> list[str]:
    """把作者串转成 PubMed 风格的作者列表。

    bioRxiv 的作者串是分号分隔、每人内部逗号分隔："Harvey, D. Y.; Stoll, H."。
    PubMed 侧是 "Harvey DY" 这种 "姓 + 首字母" 的写法，这样上层就能把
    预印本和 PubMed 文献放进同一个列表处理。

    也接受已经是 list 的输入（Europe PMC 侧已经是 list）。
    """
    if isinstance(raw, (list, tuple)):
        return [str(a).strip() for a in raw if str(a).strip()]
    if not raw:
        return []
    text = str(raw).strip()
    if not text:
        return []

    parts = [p.strip() for p in text.split(';') if p.strip()]
    if len(parts) == 1:
        only = parts[0]
        if ',' in only:
            last, _, rest = only.partition(',')
            toks = rest.replace('.', ' ').split()
            # 逗号后是首字母缩写 → 这是"姓, 名"的单人写法
            if toks and all(len(t) == 1 and t.isalpha() for t in toks):
                return [f'{last.strip()} {" ".join(toks)}']
        # 否则整体按逗号切（形如 "Smith J, Doe A"）
        parts = [p.strip() for p in only.split(',') if p.strip()]

    out: list[str] = []
    for p in parts:
        if ',' in p:
            last, _, rest = p.partition(',')
            initials = ' '.join(rest.replace('.', ' ').split())
            if last.strip() and initials:
                out.append(f'{last.strip()} {initials}')
                continue
        out.append(p)
    return out


def _biorxiv_window() -> tuple[str, str]:
    """返回 (起始日期, 结束日期) 的 ISO 字符串。"""
    today = date.today()
    return ((today - timedelta(days=BIORXIV_WINDOW_DAYS)).isoformat(),
            today.isoformat())


def _biorxiv_status(data) -> str:
    """取 API 返回的 status 字段（成功时是 'ok'）。"""
    if not isinstance(data, dict):
        return ''
    msgs = data.get('messages') or []
    return str((msgs[0] if msgs else {}).get('status') or '')


def _biorxiv_fetch_page(server: str, start: str, end: str, cursor: int):
    """取回指定游标的一页记录。返回 (records, error)。"""
    url = f'{BIORXIV}/details/{server}/{start}/{end}/{int(cursor)}'
    try:
        data = _get_json(url, timeout=45, retries=1)
    except Exception as e:  # noqa: BLE001
        return [], str(e)[:200]
    status = _biorxiv_status(data)
    if status and status != 'ok':
        # 游标越界返回 'no posts found'，属于正常情况而非故障
        if 'no posts' in status.lower():
            return [], ''
        return [], f'上游返回异常状态：{status}'
    return list((data.get('collection') or [])), ''


def _biorxiv_total(server: str, start: str, end: str) -> tuple[int, str]:
    """取回窗口内的总条数。返回 (total, error)。

    必须先知道 total：cursor 是记录偏移量，不先知道总数就不知道最新的记录在哪。
    """
    url = f'{BIORXIV}/details/{server}/{start}/{end}/0'
    try:
        data = _get_json(url, timeout=45, retries=1)
    except Exception as e:  # noqa: BLE001
        return 0, str(e)[:200]
    status = _biorxiv_status(data)
    if status and status != 'ok':
        return 0, f'上游返回异常状态：{status}'
    msgs = (data.get('messages') or []) if isinstance(data, dict) else []
    try:
        return int((msgs[0] if msgs else {}).get('total') or 0), ''
    except (TypeError, ValueError):
        return 0, ''


def _biorxiv_plan_cursors(total: int, pages: int) -> list[int]:
    """规划要抓哪些游标。

    一半取最新（保时效），一半在窗口内等距分层（保覆盖）。
    只取最新的方案实测召回很差——某主题在"最新 240 条"里 0 命中，
    同样 240 条分层抽样能命中 3~8 篇。
    """
    if total <= 0 or pages <= 0:
        return []
    newest = max(0, total - BIORXIV_PAGE)
    recent = [max(0, newest - i * BIORXIV_PAGE) for i in range(pages // 2)]
    rest = pages - len(recent)
    span = max(1, newest - (pages // 2) * BIORXIV_PAGE)
    spread = [span * i // max(1, rest) for i in range(rest)] if rest > 0 else []
    return sorted({c for c in recent + spread if 0 <= c < total}, reverse=True)


def _biorxiv_scan(server: str, tokens: list[str], retmax: int) -> tuple[list, str]:
    """扫描单个站点的预印本，返回 (records, error)。"""
    start, end = _biorxiv_window()

    total, err = _biorxiv_total(server, start, end)
    if err:
        return [], err
    if total <= 0:
        # 窗口内没有记录：不是故障，返回空结果
        return [], ''

    cursors = _biorxiv_plan_cursors(total, BIORXIV_SCAN_PAGES)
    scanned: list[dict] = []
    errors: list[str] = []

    def _pull(cursor: int):
        return _biorxiv_fetch_page(server, start, end, cursor)

    # 单次请求 2~4 秒，并发拉取把 16 页压到一次往返的耗时
    with ThreadPoolExecutor(max_workers=min(BIORXIV_WORKERS, len(cursors) or 1)) as ex:
        for page, perr in ex.map(_pull, cursors):
            if perr:
                errors.append(perr)
                continue
            if page:
                scanned.extend(page)

    if not scanned:
        return [], ('；'.join(dict.fromkeys(errors))[:200]
                    or '没有取到任何预印本记录')

    # 去重：同一 DOI 的多个版本只保留最新版
    best: dict[str, tuple[int, dict]] = {}
    for rec in scanned:
        if not isinstance(rec, dict):
            continue
        doi = str(rec.get('doi') or '').strip()
        if not doi:
            continue
        try:
            ver = int(str(rec.get('version') or '0'))
        except (TypeError, ValueError):
            ver = 0
        prev = best.get(doi)
        if prev is None or ver >= prev[0]:
            best[doi] = (ver, rec)

    records = [_biorxiv_to_record(rec, server) for _, rec in best.values()]
    records.sort(key=lambda r: r.get('date') or '', reverse=True)  # 时间倒序
    err_text = '；'.join(dict.fromkeys(errors))[:200] if errors else ''
    return [r for r in records if _matches(r, tokens)][:retmax], err_text


def search_biorxiv(term: str, retmax: int = 20, server: str = 'biorxiv') -> dict:
    """检索 bioRxiv / medRxiv。

    官方接口**不支持关键词检索**，只能按时间倒序拉取后本地过滤，
    因此召回率低，只作 Europe PMC 不可用时的兜底。
    """
    if not term or not term.strip():
        return {'count': 0, 'ids': [], 'papers': [], 'error': '检索词为空'}
    retmax = max(1, min(100, int(retmax)))
    tokens = _tokenize(term)
    papers, error = _biorxiv_scan(server, tokens, retmax)
    return {'count': len(papers), 'ids': [p['id'] for p in papers],
            'papers': papers, 'error': error}


def _biorxiv_to_record(r: dict, server: str) -> dict:
    """bioRxiv 原始记录 → 内部统一的 paper record。"""
    doi = (r.get('doi') or '').strip()
    date_str = str(r.get('date') or '')
    year = date_str[:4] if len(date_str) >= 4 else ''
    return {
        'id': f'doi:{doi}' if doi else f'{server}:{r.get("version", "")}',
        'pmid': '',
        'doi': doi,
        'title': re.sub(r'\s+', ' ', str(r.get('title') or '')).strip(),
        'abstract': re.sub(r'\s+', ' ', str(r.get('abstract') or '')).strip(),
        'authors': _split_authors(r.get('authors') or ''),
        'journal': f'{server} preprint',
        'journal_short': f'{server} preprint',
        'year': year,
        'date': date_str,
        'mesh': [],
        'keywords': [],
        'pub_types': ['Preprint'],
        'pmcid': '',
        'source': server,
        'preprint': True,
        'is_open_access': True,
        'cited_by_count': 0,
    }


# ---------------------------------------------------------------------------
# 统一入口
# ---------------------------------------------------------------------------
def search_preprints(term: str, retmax: int = 15) -> dict:
    """检索预印本：优先 Europe PMC，不可用时回落 bioRxiv + medRxiv。

    返回 {'count','ids','papers','error','sources'}。
    """
    papers: list[dict] = []
    seen: set[str] = set()
    errors: list[str] = []
    used: list[str] = []

    ep = search_europepmc(term, retmax=retmax, source='PPR')
    if ep['papers']:
        used.append('Europe PMC')
        for p in ep['papers']:
            if p['id'] in seen:
                continue
            seen.add(p['id'])
            papers.append(p)
    else:
        errors.append('Europe PMC：' + (ep['error'] or '无结果'))

    # Europe PMC 没结果或不可用时，用官方 API 兜底
    if not papers:
        for server in ('biorxiv', 'medrxiv'):
            r = search_biorxiv(term, retmax=retmax, server=server)
            if r['papers']:
                used.append(server)
                for p in r['papers']:
                    if p['id'] in seen:
                        continue
                    seen.add(p['id'])
                    papers.append(p)
            elif r['error']:
                errors.append(f'{server}：{r["error"]}')

    return {'count': len(papers), 'ids': [p['id'] for p in papers],
            'papers': papers, 'error': '；'.join(errors) if errors else '',
            'sources': used}


def preprint_to_paper_record(record: dict) -> dict:
    """预印本记录 → 与 PubMed 题录同构的 dict。

    这样上层可以把它和 PubMed 文献放在同一个列表里处理，
    同时用 `preprint` 与 `source` 字段区分证据等级。

    预印本没有 PMID，`pmid` 一律留空——宁可留空，也不能编造一个 PMID 出来。
    """
    if not isinstance(record, dict):
        record = {}
    source = str(record.get('source') or '').strip()
    server = source.lower() if source.lower() in ('biorxiv', 'medrxiv') else ''
    return {
        'pmid': str(record.get('pmid') or ''),
        'doi': record.get('doi', ''),
        'title': record.get('title', ''),
        'abstract': record.get('abstract', ''),
        'authors': _split_authors(record.get('authors') or []),
        'journal': record.get('journal', '') or (f'{server} preprint' if server else ''),
        'journal_short': record.get('journal_short', '') or (
            f'{server} preprint' if server else ''),
        'year': record.get('year', ''),
        'mesh': list(record.get('mesh') or []),
        'keywords': list(record.get('keywords') or []),
        'pub_types': list(record.get('pub_types') or ['Preprint']),
        'pmcid': record.get('pmcid', '') or '',
        'source': source or 'PPR',
        'preprint': True,
        'cited_by_count': record.get('cited_by_count', 0),
    }
