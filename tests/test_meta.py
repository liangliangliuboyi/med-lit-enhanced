# -*- coding: utf-8 -*-
"""meta.py 与 preprints.py 的离线单元测试（不联网）。"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'skills', 'medlit-agent', 'scripts'))

import meta  # noqa: E402
import preprints  # noqa: E402


class TestParseEffectSize(unittest.TestCase):
    def test_rr_with_ci(self):
        r = meta.parse_effect_size('RR 1.24 (95%CI 1.10-1.40)')
        self.assertEqual(r['metric'], 'RR')
        self.assertAlmostEqual(r['estimate'], 1.24)
        self.assertAlmostEqual(r['ci_low'], 1.10)
        self.assertAlmostEqual(r['ci_high'], 1.40)
        self.assertTrue(r['has_ci'])

    def test_rr_with_spaces_and_en_dash(self):
        r = meta.parse_effect_size('RR 1.24 (95% CI 1.10–1.40)')
        self.assertEqual(r['metric'], 'RR')
        self.assertAlmostEqual(r['ci_low'], 1.10)
        self.assertAlmostEqual(r['ci_high'], 1.40)

    def test_hazard_ratio_long_form(self):
        r = meta.parse_effect_size('hazard ratio 0.78 (95%CI 0.65-0.93)')
        self.assertEqual(r['metric'], 'HR')
        self.assertAlmostEqual(r['estimate'], 0.78)

    def test_odds_ratio_long_form(self):
        r = meta.parse_effect_size('odds ratio 2.10 (95%CI 1.50-2.90)')
        self.assertEqual(r['metric'], 'OR')

    def test_mean_difference(self):
        r = meta.parse_effect_size('mean difference 3.2 (95%CI 1.1-5.3)')
        self.assertEqual(r['metric'], 'MD')
        self.assertEqual(r['estimate'], 3.2)

    def test_without_ci(self):
        r = meta.parse_effect_size('RR 1.24')
        self.assertEqual(r['metric'], 'RR')
        self.assertFalse(r['has_ci'])
        self.assertIsNone(r['ci_low'])

    def test_chinese_metric(self):
        r = meta.parse_effect_size('风险比 1.24（95%CI 1.10-1.40）')
        self.assertEqual(r['metric'], 'RR')

    def test_unrelated_text_returns_none(self):
        self.assertIsNone(meta.parse_effect_size('完全无关的文本'))
        self.assertIsNone(meta.parse_effect_size(''))
        self.assertIsNone(meta.parse_effect_size(None))


class TestPoolEffectSizes(unittest.TestCase):
    def test_identical_effects_pool_to_themselves(self):
        """两个完全相同的效应量：合并点估计不变，置信区间应当**变窄**。

        注意：合并两篇独立研究后 CI 一定比单篇更窄（SE 减半的平方根），
        所以不能断言 CI 与原文一致——那是我的错误期望。
        """
        items = [meta.parse_effect_size('RR 1.24 (95%CI 1.10-1.40)')] * 2
        r = meta.pool_effect_sizes(items)
        self.assertEqual(r['k'], 2)
        self.assertAlmostEqual(r['pooled'], 1.24, places=3)
        # 合并两篇独立研究 → 置信区间收窄：下界抬升、上界降低，但都仍包住点估计
        self.assertGreater(r['ci_low'], 1.10)
        self.assertLess(r['ci_low'], r['pooled'])
        self.assertLess(r['ci_high'], 1.40)
        self.assertGreater(r['ci_high'], r['pooled'])
        self.assertGreater(r['ci_low'], 1.0)     # 仍应覆盖无效线 1.0
        self.assertEqual(r['i2'], 0.0)           # 完全一致 → 无异质性

    def test_heterogeneity_detected(self):
        items = [meta.parse_effect_size('RR 1.00 (95%CI 0.90-1.11)'),
                 meta.parse_effect_size('RR 5.00 (95%CI 4.00-6.25)')]
        r = meta.pool_effect_sizes(items)
        self.assertGreater(r['i2'], 0)
        self.assertGreater(r['q'], 0)

    def test_fewer_than_two_returns_zero(self):
        items = [meta.parse_effect_size('RR 1.24 (95%CI 1.10-1.40)')]
        r = meta.pool_effect_sizes(items)
        self.assertEqual(r['k'], 0)
        self.assertTrue(r['note'])

    def test_missing_ci_is_skipped_not_dropped_silently(self):
        """缺 CI 的效应量不应被静默丢弃——必须出现在 skipped 列表里。"""
        items = [meta.parse_effect_size('RR 1.24 (95%CI 1.10-1.40)'),
                 meta.parse_effect_size('RR 1.30')]
        r = meta.pool_effect_sizes(items)
        self.assertEqual(r['k'], 0)   # 只有 1 个带 CI，不足以合并
        self.assertTrue(r.get('skipped_no_ci'), '缺 CI 的条目没有被记录')

    def test_empty_input(self):
        r = meta.pool_effect_sizes([])
        self.assertEqual(r['k'], 0)

    def test_ratio_with_nonpositive_ci_does_not_crash(self):
        items = [meta.parse_effect_size('RR 1.24 (95%CI 0.00-1.40)'),
                 meta.parse_effect_size('RR 1.30 (95%CI 1.10-1.50)')]
        r = meta.pool_effect_sizes(items)
        self.assertIsInstance(r, dict)

    def test_chi2_sf_monotonic(self):
        # 卡方生存函数应随 x 单调递减
        v1 = meta.chi2_sf(1.0, 2)
        v2 = meta.chi2_sf(5.0, 2)
        self.assertGreater(v1, v2)
        self.assertTrue(0 <= v1 <= 1)
        self.assertTrue(0 <= v2 <= 1)


class TestSummarizeEffects(unittest.TestCase):
    def test_empty_list(self):
        self.assertEqual(meta.summarize_effects([]), [])
        self.assertEqual(meta.summarize_effects(None), [])

    def test_groups_by_metric(self):
        ev = [{'effect_sizes': ['RR 1.24 (95%CI 1.10-1.40)',
                                 'RR 1.30 (95%CI 1.15-1.47)',
                                 'OR 2.0 (95%CI 1.5-2.7)',
                                 'OR 2.2 (95%CI 1.6-3.0)']}]
        out = meta.summarize_effects(ev)
        metrics = {o['metric'] for o in out}
        self.assertEqual(metrics, {'RR', 'OR'})

    def test_missing_effect_sizes_key(self):
        out = meta.summarize_effects([{'pmid': '1'}, {}])
        self.assertEqual(out, [])


class TestPreprintRecord(unittest.TestCase):
    def test_preprint_to_paper_record_fields(self):
        rec = {
            'id': 'doi:10.1101/2024.01.01.123456', 'pmid': '', 'doi': '10.1101/2024.01.01.123456',
            'title': 'A preprint title', 'abstract': 'Some abstract',
            'authors': ['Smith J', 'Doe AB'],
            'journal': 'bioRxiv preprint', 'journal_short': 'bioRxiv preprint',
            'year': '2024', 'mesh': [], 'keywords': [],
            'pub_types': ['Preprint'], 'pmcid': '', 'source': 'biorxiv',
        }
        p = preprints.preprint_to_paper_record(rec)
        self.assertEqual(p['pmid'], '')          # 预印本没有 PMID，不能伪造
        self.assertEqual(p['title'], 'A preprint title')
        self.assertEqual(p['year'], '2024')
        self.assertTrue(p['preprint'])
        self.assertEqual(p['source'], 'biorxiv')
        self.assertEqual(p['pub_types'], ['Preprint'])

    def test_europepmc_record_conversion(self):
        raw = {
            'id': '4000000', 'source': 'PPR', 'pmid': '', 'doi': '10.1101/x',
            'title': 'Preprint title', 'abstract': 'Abstract text',
            'authorList': {'author': [{'lastName': 'Zhang', 'initials': 'L'},
                                      {'fullName': 'Group Consortium'}]},
            'journalInfo': {'journal': {'title': 'bioRxiv'}},
            'pubYear': '2025', 'pubTypeList': {'pubType': ['Preprint']},
            'isOpenAccess': 'Y', 'citedByCount': 3,
        }
        rec = preprints._europepmc_to_record(raw)
        self.assertIsNotNone(rec)
        self.assertEqual(rec['id'], 'doi:10.1101/x')
        self.assertEqual(rec['year'], '2025')
        self.assertTrue(rec['preprint'])
        self.assertEqual(rec['cited_by_count'], 3)
        self.assertIn('Zhang L', rec['authors'])

    def test_record_without_title_is_skipped(self):
        self.assertIsNone(preprints._europepmc_to_record({'id': '1'}))


class TestSearchGuards(unittest.TestCase):
    def test_empty_term_returns_error_not_crash(self):
        r = preprints.search_europepmc('', retmax=5)
        self.assertEqual(r['count'], 0)
        self.assertTrue(r['error'])

        r2 = preprints.search_biorxiv('', retmax=5)
        self.assertEqual(r2['count'], 0)
        self.assertTrue(r2['error'])

    def test_search_preprints_never_raises(self):
        """即使网络全挂，也要返回结构完整的 error 而不是抛异常。"""
        try:
            r = preprints.search_preprints('some term', retmax=3)
            self.assertIn('count', r)
            self.assertIn('papers', r)
            self.assertIn('error', r)
        except Exception as e:  # noqa: BLE001
            self.fail(f'search_preprints 抛出了异常：{e}')


# ---------------------------------------------------------------------------
# 以下用例全部离线：用假的 _get_json 替换真实网络请求
# ---------------------------------------------------------------------------
class _FakeHttp:
    """替换 preprints._get_json，记录请求过的 URL 并返回预置响应。"""

    def __init__(self, responder):
        self.responder = responder
        self.urls: list[str] = []

    def __call__(self, url: str, timeout: int = 45, retries: int = 2):
        self.urls.append(url)
        return self.responder(url)


class TestEuropePmcSourceFilter(unittest.TestCase):
    """Europe PMC 的 source 必须写进检索式，不能当 URL 参数传。

    当成 URL 参数传会被静默忽略，返回 PubMed 正式论文（带真 PMID），
    而不是预印本——这会让正式论文混进预印本证据池。
    """

    def test_source_is_part_of_query_not_url_param(self):
        seen = {}

        def responder(url):
            seen['url'] = url
            return {'hitCount': 1, 'resultList': {'result': [{
                'id': 'PPR1', 'source': 'PPR', 'title': 'A preprint',
                'doi': '10.1101/x', 'pubYear': '2026',
            }]}}

        fake = _FakeHttp(responder)
        orig = preprints._get_json
        preprints._get_json = fake
        try:
            r = preprints.search_europepmc('glp-1', retmax=5, source='PPR')
        finally:
            preprints._get_json = orig

        self.assertEqual(r['count'], 1)
        # source 必须在 query 里
        self.assertIn('SRC%3APPR', seen['url'])
        self.assertNotIn('source=', seen['url'])
        # 记录必须被标成预印本，且没有 PMID
        self.assertEqual(r['papers'][0]['source'], 'PPR')
        self.assertTrue(r['papers'][0]['preprint'])
        self.assertEqual(r['papers'][0]['pmid'], '')

    def test_non_preprint_record_is_not_marked_preprint(self):
        """上游返回 MED 记录时，preprint 标志必须为 False。"""
        raw = {'id': '1', 'source': 'MED', 'pmid': '123', 'title': 'A paper',
               'pubYear': '2026'}
        rec = preprints._europepmc_to_record(raw)
        self.assertFalse(rec['preprint'])
        self.assertEqual(rec['pmid'], '123')


class TestBiorxivEndpoint(unittest.TestCase):
    """bioRxiv 官方 API 的 URL 形式与游标语义。

    旧形式 /details/{server}/{start}/{count} 现在返回 HTTP 200 + 空 collection，
    看起来像"检索到 0 篇"；现行形式需要日期区间 + 游标。
    """

    def test_uses_date_range_endpoint_not_legacy_form(self):
        def responder(url):
            return {'messages': [{'status': 'ok', 'total': '100'}],
                    'collection': []}

        fake = _FakeHttp(responder)
        orig = preprints._get_json
        preprints._get_json = fake
        try:
            preprints.search_biorxiv('obesity', retmax=10, server='biorxiv')
        finally:
            preprints._get_json = orig

        self.assertTrue(fake.urls, '应当发起过请求')
        for u in fake.urls:
            # 旧形式是 /details/biorxiv/0/20；现行形式是 /details/biorxiv/DATE/DATE/CURSOR
            self.assertNotRegex(u, r'/details/biorxiv/\d+/\d+$')
            self.assertRegex(u, r'/details/biorxiv/\d{4}-\d{2}-\d{2}/')

    def test_cursors_include_newest_and_are_within_range(self):
        """最新记录在窗口末尾，cursor = total - 30。"""
        cursors = preprints._biorxiv_plan_cursors(total=1000, pages=8)
        self.assertTrue(cursors)
        self.assertLessEqual(max(cursors), 1000)
        self.assertGreaterEqual(min(cursors), 0)
        self.assertIn(970, cursors)   # total - PAGE_SIZE
        # 一半分层，覆盖窗口前段
        self.assertLess(min(cursors), 900)

    def test_plan_cursors_handles_edge_cases(self):
        self.assertEqual(preprints._biorxiv_plan_cursors(0, 8), [])
        self.assertEqual(preprints._biorxiv_plan_cursors(100, 0), [])
        # 总数小于一页时，cursor 只能是 0
        self.assertEqual(preprints._biorxiv_plan_cursors(10, 8), [0])

    def test_keeps_latest_version_per_doi(self):
        """同一 DOI 的 v1/v2 只保留最新版。

        故意让 v2 先出现、v1 后出现：只按"最后一条覆盖"去重的实现会留下 v1，
        因此这个用例能区分"取最新版"和"随便留一条"。
        """
        def responder(url):
            return {'messages': [{'status': 'ok', 'total': '60'}], 'collection': [
                {'doi': '10.1101/a', 'title': 'New', 'version': '2',
                 'date': '2026-01-02', 'abstract': 'obesity'},
                {'doi': '10.1101/a', 'title': 'Old', 'version': '1',
                 'date': '2026-01-01', 'abstract': 'obesity'},
                {'doi': '10.1101/b', 'title': 'Other', 'version': '1',
                 'date': '2026-01-03', 'abstract': 'obesity'},
            ]}

        fake = _FakeHttp(responder)
        orig = preprints._get_json
        preprints._get_json = fake
        try:
            r = preprints.search_biorxiv('obesity', retmax=10, server='biorxiv')
        finally:
            preprints._get_json = orig

        dois = [p['doi'] for p in r['papers']]
        self.assertEqual(len(dois), len(set(dois)), '同一 DOI 不应重复出现')
        titles = [p['title'] for p in r['papers']]
        self.assertIn('New', titles)
        self.assertNotIn('Old', titles)

    def test_upstream_error_status_is_reported_not_swallowed(self):
        """上游 200 + status != ok 必须报错，不能当成"命中 0 篇"。

        刻意让一部分页成功、一部分页失败：这样"有记录但也有故障"时，
        error 字段必须非空。如果翻页环节不检查 status，错误会被静默吞掉，
        看起来就是"一切正常，只是命中少了一点"。
        """
        def responder(url):
            cursor = url.rsplit('/', 1)[-1]
            if cursor == '0':
                return {'messages': [{'status': 'ok', 'total': '1000'}], 'collection': [
                    {'doi': '10.1101/ok', 'title': 'Obesity', 'version': '1',
                     'date': '2026-01-01', 'abstract': 'obesity'},
                ]}
            return {'messages': [{'status': 'Server not recognized'}],
                    'collection': []}

        fake = _FakeHttp(responder)
        orig = preprints._get_json
        preprints._get_json = fake
        try:
            r = preprints.search_biorxiv('obesity', retmax=10, server='biorxiv')
        finally:
            preprints._get_json = orig

        self.assertTrue(r['error'],
                        '部分页失败时 error 必须非空（翻页环节要检查上游 status）')
        self.assertGreaterEqual(r['count'], 1)

    def test_network_failure_returns_error_not_exception(self):
        def boom(url, timeout=45, retries=2):
            raise RuntimeError('network down')

        orig = preprints._get_json
        preprints._get_json = boom
        try:
            r = preprints.search_biorxiv('obesity', retmax=10, server='biorxiv')
            r2 = preprints.search_preprints('obesity', retmax=10)
        finally:
            preprints._get_json = orig

        self.assertEqual(r['count'], 0)
        self.assertTrue(r['error'])
        self.assertEqual(r2['count'], 0)
        self.assertTrue(r2['error'])
        self.assertEqual(r2['papers'], [])

    def test_preprints_have_no_pmid(self):
        """预印本没有 PMID——ids 用 doi: 前缀，绝不伪造 PMID。"""
        def responder(url):
            return {'messages': [{'status': 'ok', 'total': '60'}], 'collection': [
                {'doi': '10.1101/x', 'title': 'Obesity study', 'version': '1',
                 'date': '2026-01-01', 'abstract': 'obesity and weight',
                 'authors': 'Harvey, D. Y.; Stoll, H.'},
            ]}

        fake = _FakeHttp(responder)
        orig = preprints._get_json
        preprints._get_json = fake
        try:
            r = preprints.search_biorxiv('obesity', retmax=10, server='biorxiv')
        finally:
            preprints._get_json = orig

        self.assertEqual(r['ids'], ['doi:10.1101/x'])
        self.assertEqual(r['papers'][0]['pmid'], '')
        self.assertEqual(r['papers'][0]['authors'], ['Harvey D Y', 'Stoll H'])


class TestAuthorSplitting(unittest.TestCase):
    def test_semicolon_separated_authors(self):
        self.assertEqual(
            preprints._split_authors('Harvey, D. Y.; Stoll, H.; Hamilton, R. H.'),
            ['Harvey D Y', 'Stoll H', 'Hamilton R H'])

    def test_single_author_with_initials(self):
        self.assertEqual(preprints._split_authors('Smith, J.'), ['Smith J'])

    def test_plain_comma_separated_names(self):
        self.assertEqual(preprints._split_authors('Smith J, Doe A'),
                         ['Smith J', 'Doe A'])

    def test_list_input_passthrough(self):
        self.assertEqual(preprints._split_authors(['Smith J', 'Doe A']),
                         ['Smith J', 'Doe A'])

    def test_empty_input(self):
        self.assertEqual(preprints._split_authors(''), [])
        self.assertEqual(preprints._split_authors(None), [])


class TestTokenizer(unittest.TestCase):
    def test_drops_stopwords_and_short_tokens(self):
        toks = preprints._tokenize('the and of GLP-1 obesity')
        self.assertIn('obesity', toks)
        self.assertNotIn('the', toks)
        self.assertNotIn('and', toks)

    def test_keeps_hyphenated_variants(self):
        toks = preprints._tokenize('GLP-1 receptor')
        self.assertIn('glp-1', toks)
        self.assertIn('glp1', toks)
        self.assertIn('receptor', toks)

    def test_chinese_tokens(self):
        toks = preprints._tokenize('肥胖症 糖尿病')
        self.assertIn('肥胖症', toks)

    def test_empty_term_yields_no_tokens(self):
        self.assertEqual(preprints._tokenize(''), [])
        self.assertEqual(preprints._tokenize('   '), [])


if __name__ == '__main__':
    unittest.main()
