# -*- coding: utf-8 -*-
"""PubMed E-utilities + PMC 全文。零第三方依赖。

踩过的坑（改代码前务必看）：
1. efetch 必须 retmode=xml；公共参数里不能再塞 retmode=json，否则抓回空文档。
2. 取 DOI 只能从 PubmedData/ArticleIdList 取，不能 .//ArticleIdList ——
   参考文献列表里也有 doi，会造成覆盖。
3. 检索式里的 NOT (A OR B) 会被 PubMed 错解析（实测命中掉 85%），
   所以"排除综述"一律放在程序侧按 pub_types 过滤。
4. 检索式过长（>1800 字符）必须改 POST，否则 URL 超长被拒。
"""
from __future__ import annotations

import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

EUTILS = 'https://eutils.ncbi.nlm.nih.gov/entrez/eutils'
BIOC = 'https://www.ncbi.nlm.nih.gov/research/bionlp/RESTful/pmcoa.cgi/BioC_json'
UA = 'med-lit-enhanced/1.0 (literature retrieval)'


class PubMed:
    def __init__(self, api_key: str = '', tool: str = 'med-lit-enhanced',
                 email: str = '', min_interval: float = 0.36):
        self.api_key = api_key
        self.tool = tool
        self.email = email
        self.min_interval = min_interval
        self._last = 0.0

    # ---------- 基础请求 ----------
    def _throttle(self):
        gap = time.time() - self._last
        if gap < self.min_interval:
            time.sleep(self.min_interval - gap)
        self._last = time.time()

    def _common(self) -> dict:
        p = {'tool': self.tool}
        if self.api_key:
            p['api_key'] = self.api_key
        if self.email:
            p['email'] = self.email
        return p

    def _call(self, endpoint: str, params: dict, timeout: int = 60, retries: int = 3):
        """短查询走 GET，长查询自动改 POST。返回 (status, text)。"""
        p = dict(params)
        p.update(self._common())
        qs = urllib.parse.urlencode(p, doseq=True)
        last = None
        for attempt in range(retries):
            self._throttle()
            try:
                if len(qs) > 1800:
                    req = urllib.request.Request(
                        f'{EUTILS}/{endpoint}.fcgi',
                        data=qs.encode('utf-8'),
                        headers={'Content-Type': 'application/x-www-form-urlencoded',
                                 'User-Agent': UA},
                        method='POST')
                else:
                    req = urllib.request.Request(
                        f'{EUTILS}/{endpoint}.fcgi?{qs}',
                        headers={'User-Agent': UA})
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    return r.status, r.read().decode('utf-8', 'ignore')
            except urllib.error.HTTPError as e:
                last = e
                if e.code == 429 or 500 <= e.code < 600:
                    time.sleep(2 ** attempt * 2)
                    continue
                raise
            except Exception as e:  # noqa: BLE001
                last = e
                time.sleep(2 ** attempt)
        raise RuntimeError(f'PubMed {endpoint} 失败: {last}')

    # ---------- esearch ----------
    def esearch(self, term: str, retmax: int = 20, sort: str = 'relevance',
                mindate: str | None = None) -> dict:
        params = {'db': 'pubmed', 'term': term, 'retmax': retmax,
                  'retmode': 'json', 'sort': sort}
        if mindate:
            params['mindate'] = f'{mindate}/3000'
            params['datetype'] = 'pdat'
        _, txt = self._call('esearch', params)
        try:
            import json
            data = json.loads(txt)
        except Exception:  # noqa: BLE001
            return {'count': 0, 'ids': [], 'translation': '',
                    'error': 'NCBI 返回的不是合法 JSON（可能是 5xx 错误页）'}
        res = data.get('esearchresult', {}) or {}
        # NCBI 检索后端故障时会返回 HTTP 200 + ERROR 字段，而不是报错状态码。
        # 必须显式识别，否则会把"上游挂了"误报成"命中 0 条"。
        err = res.get('ERROR') or res.get('error')
        if err:
            return {'count': 0, 'ids': [], 'translation': '',
                    'error': str(err)[:300]}
        return {
            'count': int(res.get('count', 0) or 0),
            'ids': res.get('idlist', []) or [],
            'translation': res.get('querytranslation', ''),
            'error': '',
        }

    # ---------- efetch ----------
    def efetch(self, pmids: list[str]) -> list[dict]:
        if not pmids:
            return []
        out: list[dict] = []
        for i in range(0, len(pmids), 200):
            chunk = pmids[i:i + 200]
            params = {'db': 'pubmed', 'id': ','.join(chunk), 'retmode': 'xml'}
            _, txt = self._call('efetch', params, timeout=90)
            out.extend(self._parse_xml(txt))
        return out

    @staticmethod
    def _text(node) -> str:
        return ''.join(node.itertext()).strip() if node is not None else ''

    def _parse_xml(self, xml_text: str) -> list[dict]:
        papers = []
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError:
            return papers
        for art in root.findall('PubmedArticle'):
            mc = art.find('MedlineCitation')
            if mc is None:
                continue
            pmid = (mc.findtext('PMID') or '').strip()
            article = mc.find('Article')
            if article is None or not pmid:
                continue

            title = self._text(article.find('ArticleTitle'))
            title = re.sub(r'\s+', ' ', title)

            parts = []
            for ab in article.findall('Abstract/AbstractText'):
                lab = ab.get('Label')
                body = self._text(ab)
                parts.append(f'{lab}: {body}' if lab else body)
            abstract = '\n'.join([x for x in parts if x]).strip()

            authors = []
            for a in article.findall('AuthorList/Author'):
                ln = a.findtext('LastName')
                ini = a.findtext('Initials')
                col = a.findtext('CollectiveName')
                if ln:
                    authors.append(f'{ln} {ini}'.strip())
                elif col:
                    authors.append(col.strip())

            journal = (article.findtext('Journal/Title') or '').strip()
            jshort = (article.findtext('Journal/ISOAbbreviation') or '').strip()
            year = (article.findtext('Journal/JournalIssue/PubDate/Year') or '').strip()
            if not year:
                md = article.findtext('Journal/JournalIssue/PubDate/MedlineDate') or ''
                m = re.search(r'(19|20)\d{2}', md)
                year = m.group(0) if m else ''

            ptypes = [self._text(x) for x in article.findall('PublicationTypeList/PublicationType')]
            mesh = [x.get('DescriptorName', '') or self._text(x)
                    for x in mc.findall('MeshHeadingList/MeshHeading/DescriptorName')]
            kws = [self._text(x) for x in mc.findall('KeywordList/Keyword')]

            doi = pmcid = ''
            pd = art.find('PubmedData/ArticleIdList')
            if pd is not None:
                for aid in pd.findall('ArticleId'):
                    t = aid.get('IdType', '')
                    if t == 'doi':
                        doi = self._text(aid)
                    elif t == 'pmc':
                        pmcid = self._text(aid)

            papers.append({
                'pmid': pmid,
                'title': title,
                'abstract': abstract,
                'authors': authors,
                'journal': journal,
                'journal_short': jshort,
                'year': year,
                'doi': doi,
                'pmcid': pmcid.replace('PMC', '') if pmcid else '',
                'mesh': [m for m in mesh if m],
                'keywords': [k for k in kws if k],
                'pub_types': [p for p in ptypes if p],
            })
        return papers

    # ---------- 全文 ----------
    def fulltext(self, paper: dict) -> dict:
        """优先 PMC BioC；失败则返回空。"""
        pmcid = (paper.get('pmcid') or '').replace('PMC', '')
        if not pmcid:
            return {'source': '', 'text': '', 'sections': []}
        try:
            return self._bioc(pmcid)
        except Exception:  # noqa: BLE001
            return {'source': '', 'text': '', 'sections': []}

    def _bioc(self, pmcid: str) -> dict:
        url = f'{BIOC}/PMC{pmcid}/unicode'
        req = urllib.request.Request(url, headers={'User-Agent': UA})
        with urllib.request.urlopen(req, timeout=90) as r:
            raw = r.read().decode('utf-8', 'ignore')
        import json
        data = json.loads(raw)
        if isinstance(data, list):
            data = data[0] if data else {}
        docs = data.get('documents') or []
        if not docs:
            return {'source': '', 'text': '', 'sections': []}
        doc = docs[0]
        buf, secs = [], []
        for psg in doc.get('passages', []):
            sec = (psg.get('infons') or {}).get('section_type', '') or ''
            txt = (psg.get('text') or '').strip()
            if not txt:
                continue
            if sec and (not secs or secs[-1] != sec):
                secs.append(sec)
                buf.append(f'\n## {sec}')
            buf.append(txt)
        return {'source': 'pmc_bioc', 'text': '\n'.join(buf), 'sections': secs}


# ---------- 期刊白名单子句 ----------
def journal_clause(names: list[str], mode: str = 'ta') -> str:
    """把刊名列表拼成 PubMed 检索子句。

    mode='ta' 用 NLM ISO 缩写匹配（默认，命中率高）；mode='full' 用期刊全称。
    """
    items = [n for n in (names or []) if n]
    if not items:
        return ''
    tag = 'ta' if mode == 'ta' else 'Journal'
    parts = [f'"{n}"[{tag}]' for n in items]
    return '(' + ' OR '.join(parts) + ')'


def apply_clinical_query(term: str, cq_query: str) -> str:
    """把 PubMed Clinical Query 过滤器 AND 进检索式。"""
    if not cq_query:
        return term
    return f'({term}) AND ({cq_query})'


# ---------- 研究论文判定（程序侧，不用检索式） ----------
def is_research_article(p: dict, exclude: list[str] | None = None,
                        min_abstract: int = 300) -> bool:
    types = [t.lower() for t in (p.get('pub_types') or [])]
    if not types:
        return False
    if 'journal article' not in types and not (
            {'randomized controlled trial', 'clinical trial', 'observational study'} & set(types)):
        return False
    for bad in (exclude or []):
        if bad.lower() in types:
            return False
    if len(p.get('abstract') or '') < min_abstract:
        return False
    return True


def fmt_authors(authors: list[str], limit: int = 3) -> str:
    if not authors:
        return ''
    if len(authors) <= limit:
        return ', '.join(authors)
    return ', '.join(authors[:limit]) + ', et al'
