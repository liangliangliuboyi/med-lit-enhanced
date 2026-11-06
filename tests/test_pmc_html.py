# -*- coding: utf-8 -*-
"""PMC HTML 全文源（fulltext.FullTextFetcher._try_pmc_html 一族）的离线测试。

全部离线：构造 HTML 片段喂给解析函数，不发任何网络请求。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'skills', 'medlit-agent', 'scripts'))

import fulltext as ftmod  # noqa: E402


# 一段仿 PMC 新版版式的 HTML（容器 class = "body main-article-body"）
PMC_NEW = '''<!DOCTYPE html><html><head><title>x</title></head><body>
<nav class="usa-menu"><img src="/static/img/us_flag.svg"></nav>
<section class="body main-article-body">
  <h1>Efficacy and Safety of GLP-1 RAs</h1>
  <h2>Abstract</h2>
  <p>In this systematic review of 18 randomized clinical trials involving
     1402 participants, GLP-1 RAs significantly reduced HbA1c
     (&#8722;0.44% [95% CI, &#8722;0.68% to &#8722;0.21%]).</p>
  <h2>Results</h2>
  <p>Body weight fell by 3.02 kg. See <a href="#x">Supplement 1</a>.</p>
  <figure>
    <a href="figure/poi1/"><img src="https://cdn.ncbi.nlm.nih.gov/pmc/blobs/aa/bb/xxx-g002.jpg"
       alt="Figure 2. "></a>
    <figcaption><p>Forest plot of BMI change. <b>ETD</b> &#8722;1.45.</p></figcaption>
  </figure>
  <figure>
    <img src="https://cdn.ncbi.nlm.nih.gov/pmc/blobs/cc/dd/xxx-g003.jpg">
    <figcaption><p>Second figure caption.</p></figcaption>
  </figure>
</section>
<footer><img src="/static/img/ncbi-logo.svg"></footer>
</body></html>'''

# 一段仿 PMC 旧版版式的 HTML（容器 id = "main-content"）
PMC_OLD = '''<html><body><div id="main-content">
<h2>Introduction</h2><p>Old layout body text.</p>
<figure><img src="/pmc/articles/PMC1/bin/fig1.jpg"><figcaption>Old fig.</figcaption></figure>
</div></body></html>'''

# 404 / 反爬页
PMC_ERROR = '''<html><body><div class="alert">Article not found.
<img src="/static/img/us_flag.svg"></div>
<script>var x = "recaptcha";</script></body></html>'''


class TestStripTags(unittest.TestCase):
    def test_entities_are_decoded(self):
        s = ftmod.FullTextFetcher._strip_tags(
            '<p>a &amp; b &#8722;0.44 &lt;x&gt; &nbsp;c</p>')
        self.assertIn('&', s)
        # &#8722; 是 U+2212 真减号（不是 ASCII '-'），要原样还原
        self.assertIn('\u22120.44', s)
        self.assertNotIn('&amp;', s)
        self.assertNotIn('&#', s)

    def test_script_and_style_are_dropped(self):
        s = ftmod.FullTextFetcher._strip_tags(
            '<p>keep</p><script>var evil=1;</script><style>p{}</style>')
        self.assertIn('keep', s)
        self.assertNotIn('evil', s)

    def test_whitespace_is_collapsed(self):
        s = ftmod.FullTextFetcher._strip_tags('<p>a\n\n  b\t\tc</p>')
        self.assertEqual(s, 'a b c')


class TestLooksLikeArticle(unittest.TestCase):
    def test_real_article_page_is_recognized(self):
        self.assertTrue(ftmod.FullTextFetcher._looks_like_article(PMC_NEW))

    def test_error_page_is_rejected(self):
        """404 / 反爬页不能当正文抽出来。"""
        self.assertFalse(ftmod.FullTextFetcher._looks_like_article(PMC_ERROR))
        self.assertFalse(ftmod.FullTextFetcher._looks_like_article(
            '<html><body><p>Sorry, something went wrong.</p></body></html>'))


class TestExtractArticleHtml(unittest.TestCase):
    def test_new_layout_container(self):
        out = ftmod.FullTextFetcher._extract_article_html(PMC_NEW)
        self.assertIn('GLP-1 RAs', out)
        self.assertNotIn('usa-menu', out, '导航栏不该进正文')

    def test_old_layout_container(self):
        out = ftmod.FullTextFetcher._extract_article_html(PMC_OLD)
        self.assertIn('Old layout body text', out)


class TestHtmlToText(unittest.TestCase):
    def setUp(self):
        self.f = ftmod.FullTextFetcher()
        self.container = ftmod.FullTextFetcher._extract_article_html(PMC_NEW)
        self.text, self.sections = self.f._html_to_text(self.container)

    def test_headings_become_sections(self):
        self.assertIn('Abstract', self.sections)
        self.assertIn('Results', self.sections)

    def test_paragraph_text_is_kept(self):
        self.assertIn('1402 participants', self.text)

    def test_numeric_entities_are_decoded_in_text(self):
        # 摘要里的 \u22120.44% 应该是真字符，不是 &#8722;0.44%
        self.assertIn('\u22120.44', self.text)
        self.assertNotIn('&#', self.text)

    def test_section_marker_present(self):
        self.assertIn('## Abstract', self.text)


class TestHtmlFigures(unittest.TestCase):
    def setUp(self):
        self.f = ftmod.FullTextFetcher()
        self.figs = self.f._html_figures(
            PMC_NEW, 'https://pmc.ncbi.nlm.nih.gov/articles/PMC1/')

    def test_only_figure_images_are_taken(self):
        """导航图标（us_flag.svg、ncbi-logo.svg）不该被当成论文插图。"""
        hrefs = [f['href'] for f in self.figs]
        self.assertEqual(len(hrefs), 2)
        for h in hrefs:
            self.assertNotIn('us_flag', h)
            self.assertNotIn('ncbi-logo', h)

    def test_caption_is_plain_text(self):
        self.assertTrue(self.figs)
        cap = self.figs[0]['caption']
        self.assertIn('Forest plot', cap)
        self.assertNotIn('<', cap, '图注里不该残留 HTML 标签')
        self.assertNotIn('&#', cap)

    def test_relative_src_is_made_absolute(self):
        figs = ftmod.FullTextFetcher()._html_figures(
            PMC_OLD, 'https://pmc.ncbi.nlm.nih.gov/articles/PMC1/')
        self.assertTrue(figs)
        self.assertTrue(figs[0]['href'].startswith('https://'))

    def test_data_uri_is_skipped(self):
        html = ('<section class="body main-article-body">'
                '<figure><img src="data:image/png;base64,AAAA">'
                '<figcaption>x</figcaption></figure></section>')
        figs = ftmod.FullTextFetcher()._html_figures(html, 'https://x/')
        self.assertEqual(figs, [], '内联 base64 图应跳过')

    def test_duplicate_href_is_kept_once(self):
        html = ('<section class="body main-article-body">'
                '<figure><img src="https://x/a.jpg"></figure>'
                '<figure><img src="https://x/a.jpg"></figure></section>')
        figs = ftmod.FullTextFetcher()._html_figures(html, 'https://x/')
        self.assertEqual(len(figs), 1)


class TestTryPmcHtmlOffline(unittest.TestCase):
    """把网络请求 mock 掉，验证 _try_pmc_html 的完整路径。"""

    class _FakeFetcher(ftmod.FullTextFetcher):
        def __init__(self, html, status=200):
            super().__init__()
            self._fake_html = html
            self._fake_status = status
            self.requested = []

        def _request(self, url, timeout=None, method='GET', retries=2):
            self.requested.append(url)
            if self._fake_status != 200:
                raise RuntimeError('HTTP %d' % self._fake_status)
            return self._fake_status, self._fake_html.encode('utf-8'), 'text/html'

    def test_successful_fetch(self):
        f = self._FakeFetcher(PMC_NEW)
        got = f._try_pmc_html('12439189')
        self.assertIsNotNone(got)
        self.assertEqual(got['source'], 'pmc_html')
        self.assertIn('1402 participants', got['text'])
        self.assertEqual(len(got['figures']), 2)
        self.assertIn('PMC12439189', f.requested[0])

    def test_error_page_returns_none(self):
        f = self._FakeFetcher(PMC_ERROR)
        self.assertIsNone(f._try_pmc_html('99999999'),
                          '404/反爬页必须返回 None，交给下一级')

    def test_oversized_body_returns_none(self):
        f = self._FakeFetcher('x' * (ftmod.FullTextFetcher.PMC_HTML_MAX_BYTES + 10))
        self.assertIsNone(f._try_pmc_html('1'))

    def test_network_failure_returns_none(self):
        f = self._FakeFetcher(PMC_NEW, status=500)
        self.assertIsNone(f._try_pmc_html('1'))

    def test_figures_href_is_absolute(self):
        f = self._FakeFetcher(PMC_NEW)
        got = f._try_pmc_html('1')
        for fig in got['figures']:
            self.assertTrue(fig['href'].startswith('https://'),
                            '图表识别模块要直接拿这个 URL 去下载')


if __name__ == '__main__':
    unittest.main()
