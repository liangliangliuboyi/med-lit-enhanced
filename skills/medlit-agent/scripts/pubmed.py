# -*- coding: utf-8 -*-
"""PubMed E-utilities + PMC 全文。零第三方依赖。

踩过的坑（改代码前务必看）：
1. efetch 必须 retmode=xml；公共参数里不能再塞 retmode=json，否则抓回空文档。
2. 取 DOI 只能从 PubmedData/ArticleIdList 取，不能 .//ArticleIdList ——
   参考文献列表里也有 doi，会造成覆盖。
3. 检索式里的 NOT (A OR B) 会被 PubMed 错解析（实测命中掉 85%），
   所以"排除综述"一律放在程序侧按 pub_types 过滤。
4. 检索式过长（>1800 字符）必须改 POST，否则 URL 超长被拒。
5. **检索后端故障时 NCBI 会返回 HTTP 200 + esearchresult.ERROR 字段**，
   而不是错误状态码。必须显式识别，否则会把"上游挂了"误报成"命中 0 条"。
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

EUTILS = 'https://eutils.ncbi.nlm.nih.gov/entrez/eutils'
BIOC = 'https://www.ncbi.nlm.nih.gov/research/bionlp/RESTful/pmcoa.cgi/BioC_json'
UA = 'medlit-agent/2.1 (evidence retrieval)'


class PubMed:
    def __init__(self, api_key: str = '', tool: str = 'medlit-agent',
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
            data = json.loads(txt)
        except Exception:  # noqa: BLE001
            return {'count': 0, 'ids': [], 'translation': '',
                    'error': 'NCBI 返回的不是合法 JSON（可能是 5xx 错误页）'}
        res = data.get('esearchresult', {}) or {}
        err = res.get('ERROR') or res.get('error')
        if err:
            return {'count': 0, 'ids': [], 'translation': '', 'error': str(err)[:300]}
        return {
            'count': int(res.get('count', 0) or 0),
            'ids': res.get('idlist', []) or [],
            'translation': res.get('querytranslation', ''),
            'error': '',
        }

    # ---------- elink：被引文献 / 相似文献 ----------
    def elink(self, pmids: list[str], linkname: str = 'pubmed_pubmed_citedin',
              retmax: int = 40) -> dict:
        """ELink 扩展检索。

        linkname 取值：
          pubmed_pubmed_citedin  → 引用了这些文献的（被引文献）
          pubmed_pubmed_sim      → 相似文献
        返回 {'ids': [...], 'error': ''}，失败时 error 非空、ids 为空。
        """
        if not pmids:
            return {'ids': [], 'error': ''}
        out: list[str] = []
        err = ''
        for i in range(0, len(pmids), 50):
            chunk = pmids[i:i + 50]
            params = {'dbfrom': 'pubmed', 'db': 'pubmed', 'id': ','.join(chunk),
                      'linkname': linkname, 'retmode': 'json', 'retmax': retmax}
            try:
                _, txt = self._call('elink', params)
                data = json.loads(txt)
            except Exception as e:  # noqa: BLE001
                err = str(e)[:200]
                continue
            links = data.get('linksets') or []
            for ls in links:
                for db in (ls.get('linksetdbs') or []):
                    for link in (db.get('links') or []):
                        out.extend(str(x) for x in link)
        # 去重保序
        seen, uniq = set(), []
        for x in out:
            if x not in seen:
                seen.add(x)
                uniq.append(x)
        return {'ids': uniq, 'error': err}

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

            # ---- 更正/撤稿标记（B2：引用了撤稿论文是大忌）----
            flags = []
            for cc in article.findall('CommentsCorrectionsList/CommentsCorrections'):
                ref = (cc.findtext('RefType') or '').strip().lower()
                if ref:
                    flags.append(ref.replace(' ', '-'))

            # ---- 参考文献标题（用于引文滚雪球 A3）----
            refs = []
            for ref in article.findall('ReferenceList/Reference'):
                t = self._text(ref.find('ArticleTitle'))
                if t:
                    t = re.sub(r'\s+', ' ', t).rstrip('.')
                    refs.append(t)
                else:
                    art_title = self._text(ref.find('Article/ArticleTitle'))
                    if art_title:
                        refs.append(re.sub(r'\s+', ' ', art_title).rstrip('.'))

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
                'correction_flags': flags,
                'reference_titles': refs[:40],
            })
        return papers

    # ---------- 全文 ----------
    def fulltext(self, paper: dict) -> dict:
        """优先 PMC BioC；失败则返回空。"""
        pmcid = (paper.get('pmcid') or '').replace('PMC', '')
        if not pmcid:
            return {'source': '', 'text': '', 'sections': [], 'figure_captions': []}
        try:
            return self._bioc(pmcid)
        except Exception:  # noqa: BLE001
            return {'source': '', 'text': '', 'sections': [], 'figure_captions': []}

    def _bioc(self, pmcid: str) -> dict:
        url = f'{BIOC}/PMC{pmcid}/unicode'
        req = urllib.request.Request(url, headers={'User-Agent': UA})
        with urllib.request.urlopen(req, timeout=90) as r:
            raw = r.read().decode('utf-8', 'ignore')
        data = json.loads(raw)
        if isinstance(data, list):
            data = data[0] if data else {}
        docs = data.get('documents') or []
        if not docs:
            return {'source': '', 'text': '', 'sections': [], 'figure_captions': []}
        doc = docs[0]
        buf, secs, captions = [], [], []
        for psg in doc.get('passages', []):
            infons = psg.get('infons') or {}
            sec = infons.get('section_type', '') or ''
            stype = (infons.get('type') or '')
            txt = (psg.get('text') or '').strip()
            if not txt:
                continue
            # 图表说明单独收集（B7：系统评价常用森林图/漏斗图）
            if stype in ('caption', 'fig_caption', 'table_caption') or sec == 'FIG' or sec == 'TABLE':
                captions.append(txt)
                continue
            if sec and (not secs or secs[-1] != sec):
                secs.append(sec)
                buf.append(f'\n## {sec}')
            buf.append(txt)
        return {'source': 'pmc_bioc', 'text': '\n'.join(buf), 'sections': secs,
                'figure_captions': captions}


# ---------- Clinical Query 过滤器 ----------
def apply_clinical_query(term: str, cq_query: str) -> str:
    """把 PubMed Clinical Query 过滤器 AND 进检索式（仅用户显式指定时使用）。"""
    if not cq_query:
        return term
    return f'({term}) AND ({cq_query})'


# ---------- 动物/体外研究排除（A6：官方 MeSH 写法，比自由词 NOT 稳）----------
#
# ⚠️⚠️ 这里曾经写错过一次，代价很大——记下来别再改错。
#
# 错误写法：`NOT ("Animals"[MeSH] NOT "Humans"[MeSH])`
#   PubMed 会**丢弃外层的 NOT**，实际执行成 `AND ("Animals" NOT "Humans")`
#   ——语义正好反了：只留下"有动物标记、没有 Humans 标记"的**纯动物研究**，
#   把所有人体研究全滤掉了。
#   实测（2026-11，semaglutide AND obesity）：
#     裸检索式                                    3107
#     AND NOT (Animals NOT Humans)   ← 错误写法   137   （掉了 95.6%）
#     AND NOT (Animals AND NOT Humans) ← 正确     1876  （只掉 39.6%）
#
# 正确写法：NOT (A AND NOT B)  —— 排除"有动物、无人类"的纯动物研究，保留人体研究。
ANIMAL_EXCLUSION = 'NOT ("Animals"[MeSH] AND NOT "Humans"[MeSH])'


def apply_animal_exclusion(term: str, enabled: bool = True) -> str:
    """在检索式里排除纯动物实验（官方 MeSH 写法）。

    写法必须是 `NOT (A AND NOT B)`，不能写成 `NOT (A NOT B)`——
    后者会被 PubMed 丢掉外层 NOT，语义反转成"只保留纯动物研究"。

    检索式侧只是**降噪**（少抓一些无用题录），**最终把关在程序侧**：
    `is_animal_only()` 按抓回来的 MeSH 再判一次，因为只有 efetch 回来的
    MeSH 才是该文献的真实标注。
    """
    if not enabled:
        return term
    return f'({term}) AND {ANIMAL_EXCLUSION}'


# MeSH 里代表"动物"的树节点（用前缀匹配覆盖 Mice, Inbred C57BL 之类）
_ANIMAL_ROOTS = (
    'animals', 'mice', 'rats', 'swine', 'cattle', 'dogs', 'rabbits', 'chickens',
    'sheep', 'goats', 'horses', 'zebrafish', 'drosophila', 'insects', 'mollusca',
    'crayfish', 'reptiles', 'birds', 'fishes', 'primates', 'hamsters', 'guinea pig',
    'muridae', 'cricetidae', 'lepidoptera', 'diptera', 'arthropods',
)


def is_animal_only(paper: dict) -> bool:
    """判断一篇文献是否为**纯动物实验**（无人类受试者）。

    判据（用 efetch 回来的 MeSH 判定，不依赖 PubMed 的检索式行为）：
      MeSH 里出现动物族词，且**没有** Humans 标记。

    注意要精确匹配 'Humans'——像 'Antibodies, Monoclonal, Humanized'
    这种含 "Human" 的词不算人类研究标记。
    """
    mesh = [str(m).strip().lower() for m in (paper.get('mesh') or []) if str(m).strip()]
    if not mesh:
        return False
    has_human = any(m == 'humans' or m.startswith('humans ') for m in mesh)
    if has_human:
        return False
    for m in mesh:
        if m.startswith(_ANIMAL_ROOTS):
            return True
    return False


# ---------- 语种限制 ----------
def apply_language_filter(term: str, languages: list[str]) -> str:
    """按语种限制检索范围（PubMed 的 [la] 字段）。

    默认只留英文：PubMed 收录了大量非英文文献，不限制会稀释精读名额。
    传空列表即可关闭。
    """
    langs = [str(x).strip().lower() for x in (languages or []) if str(x).strip()]
    if not langs:
        return term
    parts = ' OR '.join(f'{x}[la]' for x in langs)
    return f'({term}) AND ({parts})'


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


# ---------- 撤稿/更正判定（B2） ----------
RETRACTION_TYPES = {'retracted-publication', 'retraction-of'}
ERRATUM_TYPES = {'erratum-in', 'published-erratum', 'erratum-for'}
CORRECTION_TYPES = {'corrected-and-republished-from', 'expression-of-concern-in',
                     'update-in', 'comment-in'}


def check_retraction(paper: dict) -> dict:
    """判断一篇文献是否被撤稿 / 有更正。

    返回 {'retracted': bool, 'erratum': bool, 'concern': bool, 'flags': [...], 'note': ''}
    """
    flags = [str(f).lower() for f in (paper.get('correction_flags') or [])]
    types = [str(t).lower() for t in (paper.get('pub_types') or [])]
    allf = flags + types
    retracted = any(f in RETRACTION_TYPES for f in allf)
    erratum = any(f in ERRATUM_TYPES for f in allf)
    concern = any(f in CORRECTION_TYPES for f in allf) or 'expression-of-concern' in allf
    note = ''
    if retracted:
        note = '⚠ 该文献已被撤稿（Retracted Publication），不应作为有效证据引用'
    elif erratum:
        note = '⚠ 该文献有勘误（Erratum），引用前请核对原文'
    elif concern:
        note = '⚠ 该文献有更正/关注声明，引用前请核对原文'
    return {'retracted': retracted, 'erratum': erratum, 'concern': concern,
            'flags': flags, 'note': note}


def fmt_authors(authors: list[str], limit: int = 3) -> str:
    if not authors:
        return ''
    if len(authors) <= limit:
        return ', '.join(authors)
    return ', '.join(authors[:limit]) + ', et al'
