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
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request

UA = 'medlit-agent/2.2 (preprint retrieval)'

EUROPEPMC = 'https://www.ebi.ac.uk/europepmc/webservices/rest'
BIORXIV = 'https://api.biorxiv.org'

# Europe PMC 的 source 取值：PPR=预印本 MED=PubMed PAT=专利 AGR=农业书目
SOURCES = {'PPR': '预印本', 'MED': 'PubMed', 'PAT': '专利', 'AGR': 'AGR'}


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

    返回 {'count','ids','papers','error'}。失败时 error 非空、ids 为空，不抛异常。
    """
    if not term or not term.strip():
        return {'count': 0, 'ids': [], 'papers': [], 'error': '检索词为空'}
    params = {
        'query': term.strip(),
        'format': 'json',
        'pageSize': str(max(1, min(100, retmax))),
        'resultType': 'core',
    }
    if source:
        params['source'] = source
    url = f'{EUROPEPMC}/search?' + urllib.parse.urlencode(params)
    try:
        data = _get_json(url)
    except Exception as e:  # noqa: BLE001
        return {'count': 0, 'ids': [], 'papers': [], 'error': str(e)[:200]}

    result = (data.get('resultList') or {}).get('result') or []
    papers = []
    for r in result:
        rec = _europepmc_to_record(r)
        if rec:
            papers.append(rec)
    ids = [p['id'] for p in papers]
    return {'count': int(data.get('hitCount') or len(papers)),
            'ids': ids, 'papers': papers, 'error': ''}


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
def search_biorxiv(term: str, retmax: int = 20, server: str = 'biorxiv') -> dict:
    """检索 bioRxiv / medRxiv。

    官方接口**不支持关键词检索**，只能按时间倒序拉取后本地过滤，
    因此召回率低，只作 Europe PMC 不可用时的兜底。
    """
    if not term or not term.strip():
        return {'count': 0, 'ids': [], 'papers': [], 'error': '检索词为空'}
    words = [w.lower() for w in re.findall(r'[A-Za-z0-9\-]{3,}', term)]
    if not words:
        words = [term.lower()]
    url = f'{BIORXIV}/details/{server}/0/{max(1, min(100, retmax))}'
    try:
        data = _get_json(url, timeout=45, retries=1)
    except Exception as e:  # noqa: BLE001
        return {'count': 0, 'ids': [], 'papers': [], 'error': str(e)[:200]}

    papers = []
    for r in (data.get('collection') or []):
        blob = f'{r.get("title", "")} {r.get("abstract", "")}'.lower()
        if not any(w in blob for w in words):
            continue
        papers.append(_biorxiv_to_record(r, server))
    return {'count': len(papers), 'ids': [p['id'] for p in papers],
            'papers': papers, 'error': ''}


def _biorxiv_to_record(r: dict, server: str) -> dict:
    doi = (r.get('doi') or '').strip()
    authors = [a.strip() for a in str(r.get('authors') or '').split(',') if a.strip()]
    date = str(r.get('date') or '')
    year = date[:4] if len(date) >= 4 else ''
    return {
        'id': f'doi:{doi}' if doi else f'{server}:{r.get("version", "")}',
        'pmid': '',
        'doi': doi,
        'title': re.sub(r'\s+', ' ', str(r.get('title') or '')).strip(),
        'abstract': re.sub(r'\s+', ' ', str(r.get('abstract') or '')).strip(),
        'authors': authors,
        'journal': f'{server} preprint',
        'journal_short': f'{server} preprint',
        'year': year,
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
    """
    return {
        'pmid': record.get('pmid', '') or '',
        'doi': record.get('doi', ''),
        'title': record.get('title', ''),
        'abstract': record.get('abstract', ''),
        'authors': list(record.get('authors') or []),
        'journal': record.get('journal', ''),
        'journal_short': record.get('journal_short', ''),
        'year': record.get('year', ''),
        'mesh': list(record.get('mesh') or []),
        'keywords': list(record.get('keywords') or []),
        'pub_types': list(record.get('pub_types') or ['Preprint']),
        'pmcid': record.get('pmcid', '') or '',
        'source': record.get('source', 'PPR'),
        'preprint': True,
        'cited_by_count': record.get('cited_by_count', 0),
    }
