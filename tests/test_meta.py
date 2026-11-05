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


if __name__ == '__main__':
    unittest.main()
