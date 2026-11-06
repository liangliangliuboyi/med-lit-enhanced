# -*- coding: utf-8 -*-
"""全文获取模块（fulltext.py）的离线单元测试。

**全程不联网。** 所有网络出口都被 monkeypatch 掉：模块内部所有出网都走
`FullTextFetcher._request`（一个统一的方法），测试把它替换掉就能做到
「零真实请求」。任何一个用例漏了这个替换就会真连出去，所以这里用一个
基类把替换做成强制动作。

要锁住的行为（每条都对应一个真实踩过的坑）：

1. `fetch_ephemeral` 成功路径**不留任何残留文件**，且 `temp_removed` 为 True；
2. `fetch_ephemeral` 下载失败路径**同样不留残留**（.part 中间态也不能留）；
3. 来源优先级：PMC 成功就**不该**再去请求 Europe PMC；PMC 失败必须退到 Europe PMC；
4. 代理确实生效：传给 ProxyHandler 的 proxy dict 符合预期；
5. `detect_network`：环境变量优先（source='env'）；都没有则 proxy 为 ''；
6. `acquisition_guide` 含 DOI/期刊名/VPN/图书馆关键词；空列表返回空串；
7. 异常不外泄：网络与解析异常全部收进 `note`，`fetch` 不抛；
8. Europe PMC XML 解析：正文、章节、图注，以及 **xlink 命名空间**下的图片 href。
"""

import os
import sys
import tempfile
import types
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'skills', 'medlit-agent', 'scripts'))

import fulltext  # noqa: E402
from fulltext import FullTextFetcher  # noqa: E402


# ---------------------------------------------------------------------
# 假数据
# ---------------------------------------------------------------------
FAKE_BIOC = {
    'documents': [{
        'passages': [
            {'infons': {'section_type': 'ABSTRACT', 'type': 'abstract'},
             'text': 'This is the abstract of the fake article.'},
            {'infons': {'section_type': 'INTRO', 'type': 'paragraph'},
             'text': 'Introduction paragraph one.'},
            {'infons': {'section_type': 'INTRO', 'type': 'paragraph'},
             'text': 'Introduction paragraph two.'},
            {'infons': {'section_type': 'METHODS', 'type': 'paragraph'},
             'text': 'We randomised 100 patients.'},
            {'infons': {'section_type': 'FIG', 'type': 'fig_caption',
                        'file': 'fake-01-g001.jpg'},
             'text': 'Figure 1. Forest plot of the meta-analysis.'},
        ],
    }],
}

# Europe PMC 风格的 JATS XML。刻意包含：
#   - 嵌套 <sec>（子章节）
#   - <graphic xlink:href="...">（xlink 命名空间）
#   - 相对路径图片名
#   - 图注里的 <p>（不能混进正文）
FAKE_EPMC_XML = """<?xml version="1.0"?>
<pmc-articleset>
 <article>
  <front>
   <article-meta>
    <article-id pub-id-type="pmcid">PMC1234567</article-id>
    <article-id pub-id-type="pmid">99999999</article-id>
    <article-id pub-id-type="doi">10.1000/fake</article-id>
    <abstract><p>Structured abstract body text.</p></abstract>
   </article-meta>
  </front>
  <body>
   <sec>
    <title>Introduction</title>
    <p>Intro body paragraph.</p>
    <sec>
     <title>Nested Subsection</title>
     <p>Nested body paragraph that must appear once.</p>
    </sec>
   </sec>
   <sec>
    <title>Results</title>
    <p>Results body paragraph.</p>
    <fig id="F1" position="float">
     <label>Figure 1</label>
     <caption><p>Figure caption text.</p></caption>
     <graphic xmlns:xlink="http://www.w3.org/1999/xlink"
              position="float" xlink:href="fake-01-g001.jpg"/>
    </fig>
   </sec>
  </body>
 </article>
</pmc-articleset>
"""

FAKE_UNPAYWALL = {
    'is_oa': True,
    'oa_status': 'gold',
    'best_oa_location': {
        'host_type': 'repository',
        'url_for_pdf': 'https://example.invalid/paper.pdf',
        'url_for_landing_page': 'https://example.invalid/paper',
    },
}

FAKE_OPENALEX = {
    'open_access': {'is_oa': True, 'oa_url': 'https://example.invalid/oa.pdf'},
    'best_oa_location': {'pdf_url': 'https://example.invalid/oa.pdf',
                         'landing_page_url': 'https://example.invalid/oa'},
}

FAKE_PDF_BYTES = b'%PDF-1.4\nfake pdf body\n%%EOF\n'


class _FakeResponse:
    """模拟 urllib 的响应对象（支持 context manager）。"""

    def __init__(self, body: bytes, status: int = 200, ctype: str = 'application/json'):
        self._body = body
        self.status = status
        self.headers = {'Content-Type': ctype}

    def read(self, *args):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Recorder:
    """记录所有被请求过的 URL，并按 URL 前缀返回预设响应。"""

    def __init__(self, routes=None, default=None):
        # routes: [(substring, body_bytes, ctype_or_None), ...]
        self.routes = list(routes or [])
        self.default = default
        self.urls: list[str] = []

    def __call__(self, url, timeout=None, method='GET', retries=2):
        self.urls.append(url)
        for needle, body, ctype in self.routes:
            if needle in url:
                if body is None:
                    raise urllib.error.HTTPError(url, 404, 'Not Found', {}, None)
                if isinstance(body, str):
                    body = body.encode('utf-8')
                ctype = ctype or 'application/json'
                return 200, body, ctype
        if self.default is None:
            raise urllib.error.HTTPError(url, 404, 'Not Found', {}, None)
        body, ctype = self.default
        if isinstance(body, str):
            body = body.encode('utf-8')
        return 200, body, ctype


class FullTextTestBase(unittest.TestCase):
    """所有用例的基类：强制 monkeypatch 网络层，杜绝真连出去。"""

    def setUp(self):
        self.orig_request = FullTextFetcher._request
        self.orig_env = {k: os.environ.get(k) for k in
                         ('HTTPS_PROXY', 'https_proxy', 'HTTP_PROXY', 'http_proxy')}
        self.recorder = None

    def tearDown(self):
        FullTextFetcher._request = self.orig_request
        for k, v in self.orig_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def patch_network(self, routes=None, default=None):
        """把 `_request` 换成假的记录器。返回它，便于断言请求了哪些 URL。"""
        rec = _Recorder(routes, default)
        self.recorder = rec

        def fake_request(self, url, timeout=None, method='GET', retries=2):
            return rec(url, timeout=timeout, method=method, retries=retries)

        FullTextFetcher._request = fake_request
        return rec

    def clear_proxy_env(self):
        for k in ('HTTPS_PROXY', 'https_proxy', 'HTTP_PROXY', 'http_proxy'):
            os.environ.pop(k, None)


# ---------------------------------------------------------------------
# 1. 不落地：零残留
# ---------------------------------------------------------------------
class TestEphemeralNoResidue(FullTextTestBase):

    def _snapshot_tmp(self):
        """记录临时目录里当前的文件列表（用于断言没留下东西）。"""
        import tempfile
        return set(os.listdir(tempfile.gettempdir()))

    def test_successful_fetch_leaves_no_files(self):
        """成功下载后，临时目录里不能留下 pdf / .part / 任何残留。"""
        self.clear_proxy_env()
        before = self._snapshot_tmp()
        self.patch_network(routes=[
            ('unpaywall', b'{"is_oa": true, "best_oa_location": '
                         b'{"url_for_pdf": "https://example.invalid/p.pdf", '
                         b'"url_for_landing_page": "https://example.invalid/p"}}',
             'application/json'),
            ('example.invalid/p.pdf', FAKE_PDF_BYTES, 'application/pdf'),
        ])

        fetcher = FullTextFetcher(email='user@example.org', timeout=5)
        res = fetcher.fetch_ephemeral({
            'pmid': '12345', 'doi': '10.1000/paywalled',
            'title': 'A paywalled paper', 'journal': 'Lancet', 'year': '2020',
        })

        self.assertTrue(res['temp_removed'], '成功路径必须确认删除')
        self.assertEqual(res['pdf_path'], '', '不落地模式下 pdf_path 恒为空')
        # 关键断言：临时目录里没有多出任何文件
        after = self._snapshot_tmp()
        leaked = {n for n in (after - before)
                  if n.startswith('medlit-ft-') or n.endswith('.pdf')
                  or n.endswith('.part')}
        self.assertEqual(leaked, set(), '留下了残留文件：%s' % sorted(leaked))

    def test_ephemeral_download_failure_leaves_no_files(self):
        """下载失败（返回的不是 PDF）同样不能留下残留。"""
        self.clear_proxy_env()
        before = self._snapshot_tmp()
        self.patch_network(routes=[
            ('unpaywall', b'{"is_oa": true, "best_oa_location": '
                         b'{"url_for_pdf": "https://example.invalid/p.pdf"}}',
             'application/json'),
            # 故意返回 HTML 而不是 PDF —— 模拟出版商落地页
            ('example.invalid/p.pdf', b'<html><body>paywall</body></html>', 'text/html'),
        ])

        fetcher = FullTextFetcher(email='user@example.org', timeout=5)
        res = fetcher.fetch_ephemeral({
            'pmid': '12345', 'doi': '10.1000/paywalled',
            'title': 'A paywalled paper', 'journal': 'Lancet', 'year': '2020',
        })

        self.assertTrue(res['temp_removed'], '失败路径也必须确认删除')
        after = self._snapshot_tmp()
        leaked = {n for n in (after - before)
                  if n.startswith('medlit-ft-') or n.endswith('.pdf')
                  or n.endswith('.part')}
        self.assertEqual(leaked, set(), '失败后留下了残留文件：%s' % sorted(leaked))

    def test_ephemeral_with_pmc_text_leaves_nothing(self):
        """走 PMC 文本路径（压根不下载 PDF）时，同样不落任何文件。"""
        self.clear_proxy_env()
        before = self._snapshot_tmp()
        self.patch_network(routes=[
            ('pmcoa.cgi', b'{"documents": [{"passages": ['
                          b'{"infons": {"section_type": "INTRO"}, "text": "hello"}]}]}',
             'application/json'),
        ])

        fetcher = FullTextFetcher(email='user@example.org', timeout=5)
        res = fetcher.fetch_ephemeral({
            'pmid': '1', 'pmcid': 'PMC999', 'doi': '10.1000/x',
            'title': 'T', 'journal': 'J', 'year': '2020',
        })

        self.assertEqual(res['source'], 'pmc_bioc')
        self.assertTrue(res['temp_removed'])
        after = self._snapshot_tmp()
        self.assertEqual(after - before, set())

    def test_temp_removed_false_is_reported_not_hidden(self):
        """删除失败必须如实报告，不能假装成功。"""
        self.clear_proxy_env()
        self.patch_network(routes=[
            ('unpaywall', b'{"best_oa_location": '
                         b'{"url_for_pdf": "https://example.invalid/p.pdf"}}',
             'application/json'),
            ('example.invalid/p.pdf', FAKE_PDF_BYTES, 'application/pdf'),
        ])

        orig_rmtree = fulltext.shutil.rmtree
        leaked: list[str] = []

        def fake_rmtree(path, ignore_errors=False):
            # 假装删不掉：什么都不做，目录会留在原地
            leaked.append(path)
            return None

        fulltext.shutil.rmtree = fake_rmtree
        try:
            fetcher = FullTextFetcher(email='user@example.org', timeout=5)
            res = fetcher.fetch_ephemeral({
                'pmid': '7', 'doi': '10.1000/x', 'title': 'T',
                'journal': 'J', 'year': '2020',
            })
        finally:
            fulltext.shutil.rmtree = orig_rmtree
            # 测试自己把「删不掉」的目录清掉，不给仓库留垃圾
            for path in leaked:
                orig_rmtree(path, ignore_errors=True)

        self.assertFalse(res['temp_removed'],
                         '删除失败时 temp_removed 必须为 False')
        self.assertIn('未能确认删除', res['note'])

    def test_download_to_dir_keeps_pdf(self):
        """keep_downloads=True + download_dir 时，PDF 应真的落盘。"""
        import tempfile
        self.clear_proxy_env()
        with tempfile.TemporaryDirectory() as d:
            self.patch_network(routes=[
                ('unpaywall', b'{"best_oa_location": '
                             b'{"url_for_pdf": "https://example.invalid/p.pdf"}}',
                 'application/json'),
                ('example.invalid/p.pdf', FAKE_PDF_BYTES, 'application/pdf'),
            ])
            fetcher = FullTextFetcher(email='user@example.org', timeout=5,
                                      download_dir=d, keep_downloads=True)
            res = fetcher.fetch({'pmid': '4242', 'doi': '10.1000/x',
                                 'title': 'T', 'journal': 'J', 'year': '2020'})
            self.assertTrue(res['pdf_path'])
            self.assertTrue(os.path.exists(res['pdf_path']))
            self.assertEqual(os.path.basename(res['pdf_path']), 'PMID4242.pdf')
            # 正文留空：PDF 不解析
            self.assertEqual(res['text'], '')


# ---------------------------------------------------------------------
# 2. 来源优先级
# ---------------------------------------------------------------------
class TestSourcePriority(FullTextTestBase):

    def test_pmc_success_does_not_query_europepmc(self):
        """PMC 成功后不得再请求 Europe PMC（白白多花一次往返）。"""
        self.clear_proxy_env()
        rec = self.patch_network(routes=[
            ('pmcoa.cgi', b'{"documents": [{"passages": ['
                          b'{"infons": {"section_type": "INTRO"}, "text": "pmc body"}]}]}',
             'application/json'),
        ])

        fetcher = FullTextFetcher(email='user@example.org', timeout=5)
        res = fetcher.fetch({'pmid': '1', 'pmcid': 'PMC123', 'doi': '10.1/x',
                             'title': 'T', 'journal': 'J', 'year': '2020'})

        self.assertEqual(res['source'], 'pmc_bioc')
        self.assertIn('pmc body', res['text'])
        self.assertFalse(any('europepmc' in u for u in rec.urls),
                         'PMC 成功后不该再请求 Europe PMC：%s' % rec.urls)
        self.assertFalse(any('unpaywall' in u for u in rec.urls))
        self.assertFalse(any('openalex' in u for u in rec.urls))

    def test_pmc_failure_falls_back_to_europepmc(self):
        """PMC 失败必须退到 Europe PMC。"""
        self.clear_proxy_env()
        rec = self.patch_network(routes=[
            ('pmcoa.cgi', None, None),          # 404
            ('europepmc', FAKE_EPMC_XML, 'application/xml'),
        ])

        fetcher = FullTextFetcher(email='user@example.org', timeout=5)
        res = fetcher.fetch({'pmid': '99999999', 'pmcid': 'PMC1234567',
                             'doi': '10.1/x', 'title': 'T',
                             'journal': 'J', 'year': '2020'})

        self.assertEqual(res['source'], 'europe_pmc')
        self.assertTrue(any('europepmc' in u for u in rec.urls))
        self.assertIn('Results body paragraph.', res['text'])

    def test_unpaywall_skipped_without_email(self):
        """没有邮箱时不得调用 Unpaywall（email 是必填参数，不能瞎编）。"""
        self.clear_proxy_env()
        rec = self.patch_network(routes=[
            ('pmcoa.cgi', None, None),
            ('europepmc', None, None),
            ('openalex', b'{"open_access": {"is_oa": true, '
                         b'"oa_url": "https://example.invalid/oa.pdf"}}',
             'application/json'),
        ])

        fetcher = FullTextFetcher(timeout=5)   # 没给 email
        res = fetcher.fetch({'pmid': '5', 'doi': '10.1/x', 'title': 'T',
                             'journal': 'J', 'year': '2020'})

        self.assertFalse(any('unpaywall' in u for u in rec.urls),
                         '没有 email 时不该请求 Unpaywall')
        self.assertEqual(res['source'], 'openalex')

    def test_unpaywall_preferred_over_openalex(self):
        """Unpaywall 在 OpenAlex 之前。"""
        self.clear_proxy_env()
        rec = self.patch_network(routes=[
            ('pmcoa.cgi', None, None),
            ('europepmc', None, None),
            ('unpaywall', b'{"best_oa_location": {"host_type": "repo", '
                         b'"url_for_landing_page": "https://example.invalid/p"}}',
             'application/json'),
        ])

        fetcher = FullTextFetcher(email='u@example.org', timeout=5)
        res = fetcher.fetch({'pmid': '6', 'doi': '10.1/x', 'title': 'T',
                             'journal': 'J', 'year': '2020'})

        self.assertEqual(res['source'], 'unpaywall')
        self.assertFalse(any('openalex' in u for u in rec.urls),
                         'Unpaywall 成功后不该再请求 OpenAlex')


class TestPmcidUrlBuilding(FullTextTestBase):
    """PMCID → URL 的拼接。

    这块出过一次**静默降级**的坑：内部 PMCID 带了 `PMC` 前缀，URL 构造处
    又补了一次，拼出 `PMCPMC5334499` —— PMC 返回 404，模块不报错，
    只是悄悄退到 Europe PMC、再退到 Unpaywall，用户拿到的是「没有正文」，
    而不是「URL 拼错了」。这类 bug 只能靠锁死 URL 格式来防。
    """

    def test_bioc_url_has_single_pmc_prefix(self):
        self.clear_proxy_env()
        rec = self.patch_network(routes=[
            ('pmcoa.cgi', b'{"documents": [{"passages": ['
                          b'{"infons": {"section_type": "INTRO"}, "text": "x"}]}]}',
             'application/json'),
        ])
        fetcher = FullTextFetcher(timeout=5)
        fetcher.fetch({'pmid': '1', 'pmcid': 'PMC5334499', 'doi': '10.1/x',
                       'title': 'T', 'journal': 'J', 'year': '2020'})

        self.assertTrue(rec.urls)
        url = rec.urls[0]
        self.assertIn('/PMC5334499/unicode', url)
        self.assertNotIn('PMCPMC', url)

    def test_europepmc_url_has_single_pmc_prefix(self):
        self.clear_proxy_env()
        rec = self.patch_network(routes=[
            ('pmcoa.cgi', None, None),
            ('europepmc', FAKE_EPMC_XML, 'application/xml'),
        ])
        fetcher = FullTextFetcher(timeout=5)
        fetcher.fetch({'pmid': '1', 'pmcid': 'PMC5334499', 'doi': '10.1/x',
                       'title': 'T', 'journal': 'J', 'year': '2020'})

        epmc_urls = [u for u in rec.urls if 'europepmc' in u]
        self.assertTrue(epmc_urls)
        self.assertIn('/PMC5334499/fullTextXML', epmc_urls[0])
        self.assertNotIn('PMCPMC', epmc_urls[0])
        # 也不能出现 restPMC 这种少斜杠的拼接
        self.assertNotIn('restPMC', epmc_urls[0])

    def test_pmc_article_url_has_single_pmc_prefix(self):
        self.clear_proxy_env()
        self.patch_network(routes=[
            ('pmcoa.cgi', b'{"documents": [{"passages": ['
                          b'{"infons": {"section_type": "INTRO"}, "text": "x"}]}]}',
             'application/json'),
        ])
        fetcher = FullTextFetcher(timeout=5)
        res = fetcher.fetch({'pmid': '1', 'pmcid': 'PMC5334499', 'doi': '10.1/x',
                             'title': 'T', 'journal': 'J', 'year': '2020'})
        self.assertIn('/articles/PMC5334499/', res['url'])
        self.assertNotIn('PMCPMC', res['url'])

    def test_figure_url_has_single_pmc_prefix(self):
        """图片 URL 同样不能出现 PMCPMC。"""
        res = FullTextFetcher(timeout=5)._parse_epmc_xml(FAKE_EPMC_XML, '5334499')
        href = res['figures'][0]['href']
        self.assertIn('/PMC5334499/bin/', href)
        self.assertNotIn('PMCPMC', href)

    def test_guide_xml_url_is_well_formed(self):
        """获取指引里的 PMC XML 链接不能少斜杠（restPMC…）。"""
        fetcher = FullTextFetcher(timeout=5)
        guide = fetcher.acquisition_guide([
            {'pmid': '1', 'pmcid': 'PMC6789094', 'title': 'T',
             'journal': 'BMJ', 'doi': '10.1136/bmj.n71', 'year': '2019'},
        ])
        self.assertIn('rest/PMC6789094/fullTextXML', guide)
        self.assertNotIn('restPMC', guide)


# ---------------------------------------------------------------------
# 3. 代理
# ---------------------------------------------------------------------
class TestProxy(FullTextTestBase):

    def test_explicit_proxy_is_passed_to_proxyhandler(self):
        """显式代理必须真的进 ProxyHandler（否则请求不走代理）。"""
        self.clear_proxy_env()
        captured: list = []
        orig_build = fulltext.urllib.request.build_opener

        def fake_build(*handlers):
            captured.extend(handlers)
            return orig_build(*handlers)

        fulltext.urllib.request.build_opener = fake_build
        try:
            fetcher = FullTextFetcher(proxy='http://127.0.0.1:9999', timeout=5)
            proxies = fetcher._proxy_map()
        finally:
            fulltext.urllib.request.build_opener = orig_build

        self.assertEqual(proxies.get('http'), 'http://127.0.0.1:9999')
        self.assertEqual(proxies.get('https'), 'http://127.0.0.1:9999')

        # 再验证真的构造出了带代理的 opener
        handlers = []
        orig_build2 = fulltext.urllib.request.build_opener

        def fake_build2(*hs):
            handlers.extend(hs)
            return orig_build2(*hs)

        fulltext.urllib.request.build_opener = fake_build2
        try:
            fetcher._opener()
        finally:
            fulltext.urllib.request.build_opener = orig_build2

        proxy_handlers = [h for h in handlers
                          if isinstance(h, fulltext.urllib.request.ProxyHandler)]
        self.assertTrue(proxy_handlers, '应当构造出 ProxyHandler')
        self.assertEqual(proxy_handlers[0].proxies.get('https'),
                         'http://127.0.0.1:9999')

    def test_proxy_without_scheme_gets_http_prefix(self):
        """注册表里常见的不带协议写法，必须自动补 `http://`。"""
        parsed = FullTextFetcher._parse_proxy_spec('127.0.0.1:17890')
        self.assertEqual(parsed['http'], 'http://127.0.0.1:17890')
        self.assertEqual(parsed['https'], 'http://127.0.0.1:17890')

    def test_proxy_per_scheme_spec_is_split(self):
        """`http=a;https=b` 这种分协议写法要拆开。"""
        parsed = FullTextFetcher._parse_proxy_spec('http=1.2.3.4:8080;https=5.6.7.8:8443')
        self.assertEqual(parsed['http'], 'http://1.2.3.4:8080')
        self.assertEqual(parsed['https'], 'http://5.6.7.8:8443')

    def test_env_proxy_used_when_no_explicit_proxy(self):
        """没显式给代理时，环境变量要生效。"""
        self.clear_proxy_env()
        os.environ['HTTPS_PROXY'] = 'http://10.0.0.1:3128'
        fetcher = FullTextFetcher(timeout=5)
        proxies = fetcher._proxy_map()
        self.assertEqual(proxies.get('https'), 'http://10.0.0.1:3128')

    def test_empty_proxy_spec_means_no_proxy(self):
        """空代理串 → 明确禁用代理（返回空 dict，而不是让 urllib 猜）。"""
        self.assertEqual(FullTextFetcher._parse_proxy_spec(''), {})


# ---------------------------------------------------------------------
# 4. detect_network
# ---------------------------------------------------------------------
class TestDetectNetwork(FullTextTestBase):

    @staticmethod
    def _no_build_opener():
        """把 build_opener 换成不联网的 opener，返回 (原函数, 恢复函数)。"""
        orig = fulltext.urllib.request.build_opener
        fulltext.urllib.request.build_opener = lambda *a, **k: _NoNetOpener()

        def restore():
            fulltext.urllib.request.build_opener = orig
        return restore

    @staticmethod
    def _registry_returns(value):
        """替换注册表代理读取，返回 (原描述符, 恢复函数)。

        注意必须用 `staticmethod(...)` 包回去：直接赋一个普通函数会让它变成
        实例方法，`self._proxy_from_registry()` 就会多传一个 self 而炸掉。
        """
        orig = FullTextFetcher.__dict__['_proxy_from_registry']
        FullTextFetcher._proxy_from_registry = staticmethod(lambda: value)

        def restore():
            FullTextFetcher._proxy_from_registry = orig
        return restore

    def test_env_proxy_is_reported_with_env_source(self):
        """环境变量里的代理 → proxy_source == 'env'。"""
        self.clear_proxy_env()
        os.environ['HTTPS_PROXY'] = 'http://127.0.0.1:1080'
        # 探测出版商时不能真联网：把 build_opener 换掉
        restore_opener = self._no_build_opener()
        try:
            fetcher = FullTextFetcher(timeout=5)
            res = fetcher.detect_network()
        finally:
            restore_opener()

        self.assertEqual(res['proxy_source'], 'env')
        self.assertEqual(res['proxy'], 'http://127.0.0.1:1080')
        self.assertIn(res['proxy_source'], ('env', 'registry', ''))

    def test_no_proxy_anywhere_returns_empty(self):
        """既没环境变量也没注册表 → proxy 为 ''、proxy_source 为 ''。"""
        self.clear_proxy_env()
        restore_opener = self._no_build_opener()
        restore_registry = self._registry_returns('')
        try:
            fetcher = FullTextFetcher(timeout=5)
            res = fetcher.detect_network()
        finally:
            restore_opener()
            restore_registry()

        self.assertEqual(res['proxy'], '')
        self.assertEqual(res['proxy_source'], '')
        self.assertIn('publisher_reachable', res)
        self.assertIsInstance(res['publisher_reachable'], bool)

    def test_registry_proxy_is_used_when_no_env(self):
        """注册表有代理、环境变量没有时 → proxy_source == 'registry'。"""
        self.clear_proxy_env()
        restore_opener = self._no_build_opener()
        restore_registry = self._registry_returns('127.0.0.1:17890')
        try:
            fetcher = FullTextFetcher(timeout=5)
            res = fetcher.detect_network()
        finally:
            restore_opener()
            restore_registry()

        self.assertEqual(res['proxy_source'], 'registry')
        self.assertEqual(res['proxy'], '127.0.0.1:17890')

    def test_publisher_probe_sends_real_request(self):
        """可达性探测必须真的发请求（HEAD），不能只看配置。"""
        self.clear_proxy_env()
        seen: list = []

        class _Opener:
            def open(self, req, timeout=None):
                seen.append((req.get_method(), req.full_url, timeout))
                return _FakeResponse(b'', 200, 'text/html')

        orig = fulltext.urllib.request.build_opener
        fulltext.urllib.request.build_opener = lambda *a, **k: _Opener()
        try:
            fetcher = FullTextFetcher(timeout=5)
            res = fetcher.detect_network()
        finally:
            fulltext.urllib.request.build_opener = orig

        self.assertTrue(seen, '必须真的发出探测请求')
        self.assertTrue(all(m == 'HEAD' for m, _, _ in seen))
        self.assertTrue(all(t is not None and t <= 10 for _, _, t in seen),
                        '探测超时必须很短，不能拖慢主流程')
        self.assertTrue(res['publisher_reachable'])

    def test_http_error_counts_as_reachable(self):
        """403 说明网络是通的（对方在挡爬虫），必须算「可达」。"""
        self.clear_proxy_env()

        class _Opener:
            def open(self, req, timeout=None):
                raise urllib.error.HTTPError(req.full_url, 403, 'Forbidden',
                                             {}, None)

        orig = fulltext.urllib.request.build_opener
        fulltext.urllib.request.build_opener = lambda *a, **k: _Opener()
        try:
            fetcher = FullTextFetcher(timeout=5)
            res = fetcher.detect_network()
        finally:
            fulltext.urllib.request.build_opener = orig

        self.assertTrue(res['publisher_reachable'],
                        '403 不等于不可达——网络其实是通的')


class _NoNetOpener:
    """一个什么都不做、也不发请求的 opener（给 detect_network 用）。"""

    def open(self, req, timeout=None):
        raise urllib.error.URLError('offline test')


# ---------------------------------------------------------------------
# 5. acquisition_guide
# ---------------------------------------------------------------------
class TestAcquisitionGuide(FullTextTestBase):

    def _papers(self):
        return [
            {'pmid': '111', 'title': 'GLP-1 and cardiovascular outcomes',
             'journal': 'New England Journal of Medicine',
             'doi': '10.1056/NEJMoa2034577', 'year': '2021'},
            {'pmid': '222', 'title': 'SGLT2 inhibitors in CKD',
             'journal': 'The Lancet', 'doi': '10.1016/S0140-6736(20)30183-5',
             'year': '2020'},
        ]

    def test_guide_contains_doi_journal_vpn_library(self):
        """指引里必须有 DOI、期刊名，以及 VPN / 图书馆这类可执行提示。"""
        fetcher = FullTextFetcher(timeout=5)
        guide = fetcher.acquisition_guide(self._papers())

        self.assertIn('10.1056/NEJMoa2034577', guide)
        self.assertIn('10.1016/S0140-6736(20)30183-5', guide)
        self.assertIn('New England Journal of Medicine', guide)
        self.assertIn('The Lancet', guide)
        self.assertIn('VPN', guide)
        self.assertIn('图书馆', guide)
        self.assertIn('Unpaywall', guide)
        self.assertIn('OpenAlex', guide)

    def test_guide_lists_tiering_and_acquisition_paths(self):
        """要分级说明（PMC 已有 / 需要额外获取）并逐条给出获取途径。"""
        fetcher = FullTextFetcher(timeout=5)
        papers = self._papers() + [
            {'pmid': '333', 'title': 'OA one', 'journal': 'BMJ',
             'doi': '10.1136/bmj.n71', 'year': '2019', 'pmcid': 'PMC6789094'},
        ]
        guide = fetcher.acquisition_guide(papers)

        self.assertIn('PMC', guide)
        self.assertIn('需要额外获取', guide)
        self.assertIn('https://doi.org/10.1056/NEJMoa2034577', guide)
        self.assertIn('https://pubmed.ncbi.nlm.nih.gov/111/', guide)
        self.assertIn('https://pmc.ncbi.nlm.nih.gov/articles/PMC6789094/', guide)

    def test_guide_for_empty_list(self):
        """空列表返回空字符串（或明确说明），不能崩。"""
        fetcher = FullTextFetcher(timeout=5)
        self.assertEqual(fetcher.acquisition_guide([]), '')

    def test_guide_does_not_fabricate_price_or_page(self):
        """不能编造价格、页码等 DOI 之外的标识。"""
        fetcher = FullTextFetcher(timeout=5)
        guide = fetcher.acquisition_guide(self._papers()).lower()
        for bad in ('￥', '元/', '$99', 'usd', 'page 123', 'pp. 123'):
            self.assertNotIn(bad, guide, '指引里不应出现编造的价格/页码：%s' % bad)

    def test_guide_survives_missing_fields(self):
        """题录字段缺失时不能抛异常。"""
        fetcher = FullTextFetcher(timeout=5)
        guide = fetcher.acquisition_guide([{'pmid': '9'}, {'title': 'x'}])
        self.assertIsInstance(guide, str)
        self.assertTrue(guide)


# ---------------------------------------------------------------------
# 6. 异常不外泄
# ---------------------------------------------------------------------
class TestNoExceptionLeaks(FullTextTestBase):

    def test_network_error_is_captured_in_note(self):
        """网络全挂时 fetch 不抛异常，失败原因写进 note。"""
        self.clear_proxy_env()

        def boom(self, url, timeout=None, method='GET', retries=2):
            raise RuntimeError('network down')

        FullTextFetcher._request = boom
        fetcher = FullTextFetcher(email='u@example.org', timeout=1)
        res = fetcher.fetch({'pmid': '1', 'pmcid': 'PMC1', 'doi': '10.1/x',
                             'title': 'T', 'journal': 'J', 'year': '2020'})

        self.assertFalse(res['ok'])
        self.assertTrue(res['note'])
        self.assertEqual(res['source'], '')
        self.assertEqual(res['text'], '')

    def test_broken_xml_is_captured_in_note(self):
        """XML 解析失败不能抛出去。"""
        self.clear_proxy_env()
        self.patch_network(routes=[
            ('pmcoa.cgi', None, None),
            ('europepmc', b'<not-closed><xml', 'application/xml'),
        ])
        fetcher = FullTextFetcher(email='u@example.org', timeout=1)
        res = fetcher.fetch({'pmid': '1', 'pmcid': 'PMC1', 'doi': '10.1/x',
                             'title': 'T', 'journal': 'J', 'year': '2020'})
        self.assertFalse(res['ok'])
        self.assertTrue(res['note'])

    def test_fetch_ephemeral_never_raises(self):
        """各种坏输入都不许抛异常。"""
        self.clear_proxy_env()

        def boom(self, url, timeout=None, method='GET', retries=2):
            raise RuntimeError('network down')

        FullTextFetcher._request = boom
        fetcher = FullTextFetcher(email='u@example.org', timeout=1)
        for bad in [{}, None, {'pmid': ''}, {'doi': ''}, {'pmcid': 'garbage'}]:
            res = fetcher.fetch_ephemeral(bad)
            self.assertFalse(res['ok'])
            self.assertTrue(res['temp_removed'])

    def test_all_sources_fail_returns_guide_like_note(self):
        """所有源都失败时，note 要给出下一步怎么办（代理/VPN/图书馆）。"""
        self.clear_proxy_env()
        self.patch_network(routes=[])
        fetcher = FullTextFetcher(email='u@example.org', timeout=1)
        res = fetcher.fetch({'pmid': '12345', 'doi': '10.1/x', 'title': 'T',
                             'journal': 'J', 'year': '2020'})
        self.assertFalse(res['ok'])
        self.assertIn('VPN', res['note'])
        self.assertIn('图书馆', res['note'])

    def test_all_sources_fail_gives_clickable_fallback_url(self):
        """全军覆没时也要给一个用户能点的入口（PubMed 页）。"""
        self.clear_proxy_env()
        self.patch_network(routes=[])
        fetcher = FullTextFetcher(email='u@example.org', timeout=1)
        res = fetcher.fetch({'pmid': '12345', 'doi': '10.1/x', 'title': 'T',
                             'journal': 'J', 'year': '2020'})
        self.assertEqual(res['url'], 'https://pubmed.ncbi.nlm.nih.gov/12345/')

    def test_note_does_not_repeat_failed_source(self):
        """同一个源不该既报「请求失败」又报「未找到全文」——note 会变得又长又乱。

        真正要锁住的不变量：**每个源最多一条**状态说明。
        """
        self.clear_proxy_env()
        self.patch_network(routes=[])
        fetcher = FullTextFetcher(email='u@example.org', timeout=1)
        res = fetcher.fetch({'pmid': '12345', 'doi': '10.1/x', 'title': 'T',
                             'journal': 'J', 'year': '2020'})
        note = res['note']
        for src in ('Unpaywall', 'OpenAlex', 'Europe PMC', 'PMC'):
            self.assertLessEqual(note.count('%s 请求失败' % src), 1)
            self.assertLessEqual(note.count('%s 未找到' % src), 1)
            self.assertFalse('%s 请求失败' % src in note
                             and '%s 未找到' % src in note,
                             '%s 同时报了「失败」和「未找到」' % src)


# ---------------------------------------------------------------------
# 7. XML 解析
# ---------------------------------------------------------------------
class TestParseEpmcXml(FullTextTestBase):

    def _parse(self, xml=None, pmcid=''):
        fetcher = FullTextFetcher(timeout=5)
        return fetcher._parse_epmc_xml(xml or FAKE_EPMC_XML, pmcid)

    def test_extracts_body_text_and_sections(self):
        """正文与章节标题都要抽出来。"""
        res = self._parse()
        self.assertIn('Intro body paragraph.', res['text'])
        self.assertIn('Results body paragraph.', res['text'])
        self.assertIn('Nested Subsection', res['sections'])
        self.assertIn('Introduction', res['sections'])
        self.assertIn('Results', res['sections'])

    def test_nested_paragraph_appears_exactly_once(self):
        """嵌套 sec 的段落不能被父 sec 重复收集。"""
        res = self._parse()
        self.assertEqual(res['text'].count('Nested body paragraph'), 1)

    def test_extracts_figure_with_xlink_href(self):
        """图片 href 要用 xlink 命名空间取到，并拼成绝对 URL。"""
        res = self._parse(pmcid='PMC1234567')
        self.assertTrue(res['figures'], '应当抽出图片')
        fig = res['figures'][0]
        self.assertEqual(fig['caption'], 'Figure caption text.')
        self.assertTrue(fig['href'].startswith('https://'), fig['href'])
        self.assertIn('fake-01-g001.jpg', fig['href'])
        self.assertIn('PMC1234567', fig['href'])

    def test_figure_caption_not_duplicated_into_body(self):
        """图注不能混进正文（否则正文里会多出一段不相干的话）。"""
        res = self._parse()
        self.assertNotIn('Figure caption text.', res['text'])

    def test_pmcid_taken_from_xml_when_not_given(self):
        """XML 里的 PMCID 要被读出来（MED 入口时事先不知道）。"""
        res = self._parse()
        self.assertTrue(res['figures'][0]['href'])
        self.assertIn('PMC1234567', res['figures'][0]['href'])

    def test_plain_href_attribute_also_supported(self):
        """没有 xlink 命名空间时，退回读普通 href 属性。"""
        xml = """<article><body><sec><title>S</title><p>body</p>
          <fig><caption><p>cap</p></caption>
          <graphic href="plain.jpg"/></fig></sec></body></article>"""
        res = self._parse(xml, pmcid='PMC555')
        self.assertIn('plain.jpg', res['figures'][0]['href'])

    def test_abstract_is_included(self):
        """结构化摘要要抽出来（判断研究设计要用）。"""
        res = self._parse()
        self.assertIn('Structured abstract body text.', res['text'])

    def test_inline_tags_do_not_glue_words_together(self):
        """相邻标签的文字不能粘成一坨。

        实测 Europe PMC 的 JATS 会把
        `<title>Abstract</title><sec><title>Background</title><p>Severe…`
        拼成 "AbstractBackgroundSevere…"——句子结构全毁。
        这类文本喂给模型质量很差，所以必须锁住。
        """
        xml = """<article><front><article-meta><abstract>
          <title>Abstract</title>
          <sec><title>Background</title><p>Severe acute syndrome here.</p></sec>
        </abstract></article-meta></front>
        <body><sec><title>Methods</title><p>Body text.</p></sec></body></article>"""
        res = self._parse(xml)
        self.assertIn('Abstract Background Severe acute syndrome here.', res['text'])
        self.assertNotIn('AbstractBackground', res['text'])
        self.assertNotIn('BackgroundSevere', res['text'])

    def test_sup_reference_marks_do_not_glue_to_previous_word(self):
        """`<sup>1</sup>` 角标前要有空格，否则变成 "globally1 since…"���"""
        xml = """<article><body><sec><title>S</title>
          <p>has affected many people<sup><xref ref-type="bibr">1</xref></sup>
          since 2020.</p></sec></body></article>"""
        res = self._parse(xml)
        self.assertIn('people 1 since 2020.', res['text'])
        self.assertNotIn('people1', res['text'])

    def test_broken_xml_raises_parse_error(self):
        """XML 坏了应该抛 ET.ParseError（由上层捕获进 note）。"""
        import xml.etree.ElementTree as ET
        with self.assertRaises(ET.ParseError):
            self._parse('<broken')


# ---------------------------------------------------------------------
# 8. BioC 解析
# ---------------------------------------------------------------------
class TestParseBioc(FullTextTestBase):

    def test_bioc_sections_and_figures(self):
        """BioC 的段落、章节、图注都要抽到。"""
        self.clear_proxy_env()
        import json
        self.patch_network(routes=[
            ('pmcoa.cgi', json.dumps(FAKE_BIOC).encode('utf-8'), 'application/json'),
        ])
        fetcher = FullTextFetcher(timeout=5)
        res = fetcher.fetch({'pmid': '1', 'pmcid': 'PMC123', 'doi': '10.1/x',
                             'title': 'T', 'journal': 'J', 'year': '2020'})
        self.assertEqual(res['source'], 'pmc_bioc')
        self.assertIn('ABSTRACT', res['sections'])
        self.assertIn('METHODS', res['sections'])
        self.assertIn('We randomised 100 patients.', res['text'])
        self.assertTrue(res['figures'])
        self.assertIn('fake-01-g001.jpg', res['figures'][0]['href'])
        self.assertIn('PMC123', res['figures'][0]['href'])


# ---------------------------------------------------------------------
# 9. 文件名与杂项
# ---------------------------------------------------------------------
class TestMisc(FullTextTestBase):

    def test_pdf_filename_prefers_pmid(self):
        name = FullTextFetcher._pdf_filename({'pmid': '123', 'doi': '10.1/x'})
        self.assertEqual(name, 'PMID123.pdf')

    def test_pdf_filename_falls_back_to_doi(self):
        name = FullTextFetcher._pdf_filename({'doi': '10.1056/NEJMoa2034577'})
        self.assertTrue(name.endswith('.pdf'))
        self.assertNotIn('/', name)

    def test_norm_pmcid_handles_prefix(self):
        """PMCID 统一成纯数字（前缀由 URL 构造处统一补，避免 PMCPMC…）。"""
        self.assertEqual(FullTextFetcher._norm_pmcid('PMC123'), '123')
        self.assertEqual(FullTextFetcher._norm_pmcid('123'), '123')
        self.assertEqual(FullTextFetcher._norm_pmcid(''), '')

    def test_norm_pmcid_is_idempotent(self):
        """归一化要幂等：已经是纯数字的再过一次还是它自己。"""
        once = FullTextFetcher._norm_pmcid('PMC5334499')
        self.assertEqual(FullTextFetcher._norm_pmcid(once), once)
        self.assertNotIn('PMCPMC', 'PMC%s' % once)

    def test_proxy_spec_ignores_socks(self):
        """SOCKS 代理 urllib 不支持，必须忽略而不是塞进去。"""
        parsed = FullTextFetcher._parse_proxy_spec('socks=127.0.0.1:1080')
        self.assertNotIn('http', parsed)


# ---------------------------------------------------------------------
# 10. PDF 正文抽取（质量闸门）
# ---------------------------------------------------------------------
class TestPdfTextExtraction(FullTextTestBase):
    """从 PDF 字节抽正文，以及「乱码必须被丢弃」这条硬规则。"""

    def test_pdf_text_extracted_from_bytes(self):
        """抽得到正文就采用（不下载到磁盘也能抽——用的是内存里的字节）。"""
        fetcher = FullTextFetcher(timeout=5)
        good = ('This is a readable body of text extracted from a PDF. ' * 8)
        fake = types.SimpleNamespace(
            extract_text=lambda raw: {'text': good, 'pages': 3, 'chars': len(good),
                                      'method': 'content_stream', 'note': ''})
        with mock.patch.dict(sys.modules, {'pdftext': fake}):
            text, note = fetcher._pdf_text(b'%PDF-1.4 fake')
        self.assertIn('readable body of text', text)
        self.assertEqual(note, '')

    def test_garbage_text_is_discarded(self):
        """乱码必须被丢弃——它比没有文本更糟（会被当正文喂给模型）。"""
        fetcher = FullTextFetcher(timeout=5)
        garbage = ''.join(chr(i % 256) for i in range(3000))
        fake = types.SimpleNamespace(
            extract_text=lambda raw: {'text': garbage, 'pages': 1,
                                      'chars': len(garbage),
                                      'method': 'content_stream', 'note': ''})
        with mock.patch.dict(sys.modules, {'pdftext': fake}):
            text, note = fetcher._pdf_text(b'%PDF-1.4 fake')
        self.assertEqual(text, '', '乱码不能被采用')
        self.assertIn('不是可读文字', note)

    def test_too_short_text_is_discarded(self):
        """太短的文字没有信息量，也要丢弃。"""
        fetcher = FullTextFetcher(timeout=5)
        fake = types.SimpleNamespace(
            extract_text=lambda raw: {'text': 'too short', 'pages': 1,
                                      'chars': 9, 'method': 'content_stream',
                                      'note': ''})
        with mock.patch.dict(sys.modules, {'pdftext': fake}):
            text, note = fetcher._pdf_text(b'%PDF-1.4 fake')
        self.assertEqual(text, '')
        self.assertIn('过短', note)

    def test_empty_extraction_is_reported(self):
        """扫描件没有文本层 → 如实说明，不报错。"""
        fetcher = FullTextFetcher(timeout=5)
        fake = types.SimpleNamespace(
            extract_text=lambda raw: {'text': '', 'pages': 0, 'chars': 0,
                                      'method': 'none', 'note': '没有文本层'})
        with mock.patch.dict(sys.modules, {'pdftext': fake}):
            text, note = fetcher._pdf_text(b'%PDF-1.4 fake')
        self.assertEqual(text, '')
        self.assertIn('文本层', note)

    def test_missing_pdftext_module_is_graceful(self):
        """没有 pdftext 模块也不能炸——只是没正文而已。"""
        fetcher = FullTextFetcher(timeout=5)
        with mock.patch.dict(sys.modules, {'pdftext': None}):
            text, note = fetcher._pdf_text(b'%PDF-1.4 fake')
        self.assertEqual(text, '')

    def test_gate_accepts_real_world_text(self):
        """闸门不能误伤真文本：英文、中文、中英混排、表格数字都要放行。"""
        samples = [
            ('english',
             'and MHRA) and editors of many of the leading medical journals. '
             'The hope is that the guidelines will better position journal '
             'editors, peer reviewers, and journal readers.'),
            ('chinese',
             '目的为评估该方案在真实世界中的有效性与安全性，研究共纳入随机对照'
             '试验十五项，涵盖三万余名受试者，结果显示显著降低主要终点事件风险。'),
            ('mixed',
             '结论：本研究纳入 15 项 RCT（n=32,410），95% CI 0.72-0.89，'
             'P<0.001；亚组分析显示获益一致。'),
            ('table',
             'TABLE 1 Baseline characteristics: Age 63.4 (11.2) 62.1 (10.8), '
             'Male 58% 61%, BMI 27.8 (4.1) 27.4 (3.9).'),
        ]
        for name, text in samples:
            self.assertTrue(FullTextFetcher._looks_like_text(text),
                            '闸门误伤了真实%s文本' % name)

    def test_gate_rejects_decoded_garbage(self):
        """闸门必须挡住 PDF 解码出来的二进制乱码。"""
        # 真实样本：pdftext 未修复时从图片流解出来的乱码
        garbage = ('\x91\x9bQ/\x97\x89Évm7ªnØM&\x8bÀ\n.\npÃn²í¸t]i'
                   '\x9d¥\x98P\x97Ç-\xad°;\x8a$Î`³hA¶á©')
        self.assertFalse(FullTextFetcher._looks_like_text(garbage))
        # 循环乱码
        cyclic = ''.join(chr(i % 256) for i in range(3000))
        self.assertFalse(FullTextFetcher._looks_like_text(cyclic))

    def test_ephemeral_reads_text_before_deleting_file(self):
        """「不落地」也要能读到正文：抽取发生在删文件之前。"""
        self.clear_proxy_env()
        good = ('Readable PDF body text for the extraction gate test. ' * 8)
        fake = types.SimpleNamespace(
            extract_text=lambda raw: {'text': good, 'pages': 2, 'chars': len(good),
                                      'method': 'content_stream', 'note': ''})
        self.patch_network(routes=[
            ('unpaywall', b'{"best_oa_location": '
                         b'{"url_for_pdf": "https://example.invalid/p.pdf"}}',
             'application/json'),
            ('example.invalid/p.pdf', FAKE_PDF_BYTES, 'application/pdf'),
        ])
        with mock.patch.dict(sys.modules, {'pdftext': fake}):
            fetcher = FullTextFetcher(email='u@example.org', timeout=5)
            res = fetcher.fetch_ephemeral({
                'pmid': '99', 'doi': '10.1000/x', 'title': 'T',
                'journal': 'J', 'year': '2020'})
        self.assertIn('Readable PDF body text', res['text'])
        self.assertEqual(res['pdf_path'], '')
        self.assertTrue(res['temp_removed'])

    def test_download_to_dir_also_extracts_text(self):
        """落盘模式下同样抽正文（不必再让用户自己想办法读 PDF）。"""
        import tempfile
        self.clear_proxy_env()
        good = ('Readable PDF body text for the on-disk extraction test. ' * 8)
        fake = types.SimpleNamespace(
            extract_text=lambda raw: {'text': good, 'pages': 2, 'chars': len(good),
                                      'method': 'content_stream', 'note': ''})
        self.patch_network(routes=[
            ('unpaywall', b'{"best_oa_location": '
                         b'{"url_for_pdf": "https://example.invalid/p.pdf"}}',
             'application/json'),
            ('example.invalid/p.pdf', FAKE_PDF_BYTES, 'application/pdf'),
        ])
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.dict(sys.modules, {'pdftext': fake}):
                fetcher = FullTextFetcher(email='u@example.org', timeout=5,
                                          download_dir=d, keep_downloads=True)
                res = fetcher.fetch({'pmid': '88', 'doi': '10.1000/x',
                                     'title': 'T', 'journal': 'J', 'year': '2020'})
            self.assertIn('Readable PDF body text', res['text'])
            self.assertTrue(res['pdf_path'])
            self.assertTrue(os.path.exists(res['pdf_path']))

    def test_no_download_dir_still_reads_text_and_leaves_nothing(self):
        """没配下载目录时：正文照样要读到，磁盘上却不能留下任何东西。

        这条很容易被实现成「没地方放就不下载」——但那样就白跑一趟，
        用户既没有正文也没有文件。
        """
        self.clear_proxy_env()
        before = set(os.listdir(tempfile.gettempdir()))
        good = ('Readable PDF body text without a configured download dir. ' * 8)
        fake = types.SimpleNamespace(
            extract_text=lambda raw: {'text': good, 'pages': 2, 'chars': len(good),
                                      'method': 'content_stream', 'note': ''})
        self.patch_network(routes=[
            ('unpaywall', b'{"best_oa_location": '
                         b'{"url_for_pdf": "https://example.invalid/p.pdf"}}',
             'application/json'),
            ('example.invalid/p.pdf', FAKE_PDF_BYTES, 'application/pdf'),
        ])
        with mock.patch.dict(sys.modules, {'pdftext': fake}):
            fetcher = FullTextFetcher(email='u@example.org', timeout=5)
            res = fetcher.fetch({'pmid': '77', 'doi': '10.1000/x', 'title': 'T',
                                 'journal': 'J', 'year': '2020'})
        self.assertIn('Readable PDF body text', res['text'])
        self.assertEqual(res['pdf_path'], '', '没配目录就不该留下 PDF 路径')
        self.assertTrue(res.get('temp_removed'), '走了临时路径就要报告删除结果')
        after = set(os.listdir(tempfile.gettempdir()))
        leaked = {n for n in (after - before)
                  if n.startswith('medlit-ft-') or n.endswith('.pdf')
                  or n.endswith('.part')}
        self.assertEqual(leaked, set(), '留下了残留文件：%s' % sorted(leaked))

    def test_fetch_result_has_no_temp_removed_when_file_kept(self):
        """PDF 被保留时，返回结构就是文档里那 8 个字段（不多带 temp_removed）。"""
        import tempfile
        self.clear_proxy_env()
        self.patch_network(routes=[
            ('unpaywall', b'{"best_oa_location": '
                         b'{"url_for_pdf": "https://example.invalid/p.pdf"}}',
             'application/json'),
            ('example.invalid/p.pdf', FAKE_PDF_BYTES, 'application/pdf'),
        ])
        with tempfile.TemporaryDirectory() as d:
            fetcher = FullTextFetcher(email='u@example.org', timeout=5,
                                      download_dir=d, keep_downloads=True)
            res = fetcher.fetch({'pmid': '66', 'doi': '10.1000/x', 'title': 'T',
                                 'journal': 'J', 'year': '2020'})
        self.assertNotIn('temp_removed', res)
        self.assertNotIn('_used_temp', res)
        for key in ('source', 'text', 'sections', 'figures', 'pdf_path',
                    'url', 'ok', 'note'):
            self.assertIn(key, res)

    def test_ephemeral_always_has_temp_removed(self):
        """fetch_ephemeral 恒带 temp_removed 字段。"""
        self.clear_proxy_env()
        self.patch_network(routes=[])
        fetcher = FullTextFetcher(email='u@example.org', timeout=1)
        res = fetcher.fetch_ephemeral({'pmid': '5', 'doi': '10.1/x', 'title': 'T',
                                       'journal': 'J', 'year': '2020'})
        self.assertIn('temp_removed', res)
        self.assertTrue(res['temp_removed'])
        self.assertNotIn('_used_temp', res)


if __name__ == '__main__':
    unittest.main()
