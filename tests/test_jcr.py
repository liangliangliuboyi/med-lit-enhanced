# -*- coding: utf-8 -*-
"""jcr.py 的离线单元测试。

全部离线：不发任何网络请求。索引用**合成数据**测逻辑，
真实索引文件只在「存在时」做几条集成校验（不存在就跳过，不能让测试挂掉）。
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'skills', 'medlit-agent', 'scripts'))

import jcr  # noqa: E402


def _write(path, obj):
    with open(path, 'w', encoding='utf-8') as fh:
        json.dump(obj, fh, ensure_ascii=False)


class TestNormalize(unittest.TestCase):
    """刊名/ISSN 规范化。"""

    def test_strips_leading_article(self):
        # PubMed 的刊名保留冠词，JCR 表不带——不剥就永远对不上
        self.assertEqual(
            jcr.norm_title('The Journal of clinical endocrinology and metabolism'),
            'journal of clinical endocrinology and metabolism')
        self.assertEqual(jcr.norm_title('A Journal of X'), 'journal of x')

    def test_strips_parenthetical_place(self):
        self.assertEqual(jcr.norm_title('Advances in nutrition (Bethesda, Md.)'),
                         'advances in nutrition')

    def test_ampersand_and_punctuation_unified(self):
        a = jcr.norm_title('JOURNAL OF CLINICAL ENDOCRINOLOGY & METABOLISM')
        b = jcr.norm_title('Journal of Clinical Endocrinology and Metabolism')
        self.assertEqual(a, b)

    def test_case_and_whitespace_folded(self):
        self.assertEqual(jcr.norm_title('  LANCET   '), 'lancet')
        self.assertEqual(jcr.norm_title('Lancet'), 'lancet')

    def test_empty_input_is_safe(self):
        self.assertEqual(jcr.norm_title(''), '')
        self.assertEqual(jcr.norm_title(None), '')

    def test_issn_normalization(self):
        self.assertEqual(jcr.norm_issn('1945-7197'), '19457197')
        self.assertEqual(jcr.norm_issn(' 0021-972X '), '0021972X')
        self.assertEqual(jcr.norm_issn(''), '')


class TestLookupWithSyntheticIndex(unittest.TestCase):
    """用合成索引测匹配逻辑，不依赖真实数据文件。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='jcr_test_')
        # 新结构：ISSN 与缩写刊名**折在条目里**，只有一个索引文件
        index = {
            '_meta': {'source': 'test', 'entries': 9},
            'LANCET': {'if': 109, 'quartile': 'Q1', 'cas_zone': 1,
                       'cas_top': None, 'categories': ['MEDICINE'],
                       'issn': '0140-6736', 'issn_e': '1474-547X',
                       'abbr': 'LANCET'},
            'JAMA-JOURNAL OF THE AMERICAN MEDICAL ASSOCIATION': {
                'if': 65.4, 'quartile': 'Q1', 'cas_zone': 1, 'cas_top': None,
                'categories': ['MEDICINE'], 'issn': '0098-7484',
                'issn_e': '1538-3598', 'abbr': 'JAMA'},
            'NEW ENGLAND JOURNAL OF MEDICINE': {
                'if': 84.5, 'quartile': 'Q1', 'cas_zone': 1, 'cas_top': None,
                'categories': ['MEDICINE'], 'issn': '0028-4793',
                'issn_e': '1533-4406', 'abbr': 'NEW ENGL J MED'},
            'JACC-Journal of the American College of Cardiology': {
                'if': 39, 'quartile': 'Q1', 'cas_zone': 1, 'cas_top': None,
                'categories': ['CARDIOLOGY'], 'issn': '0735-1097',
                'issn_e': '1558-3597', 'abbr': 'J AM COLL CARDIOL'},
            'JACC-Cardiovascular Interventions': {
                'if': 8, 'quartile': 'Q1', 'cas_zone': 2, 'cas_top': None,
                'categories': ['CARDIOLOGY'], 'issn': '1941-4084',
                'issn_e': '1876-??', 'abbr': 'JACC CARDIOVASC INTERV'},
            'RADIOLOGY': {'if': 17.6, 'quartile': 'Q1', 'cas_zone': 1,
                          'cas_top': None, 'categories': ['RADIOLOGY'],
                          'issn': '0033-8419', 'issn_e': '1538-8419',
                          'abbr': 'RADIOLOGY'},
            'Radiology-Artificial Intelligence': {
                'if': 12, 'quartile': 'Q1', 'cas_zone': 2, 'cas_top': None,
                'categories': ['RADIOLOGY'], 'issn': '2638-6100',
                'issn_e': '2638-6119', 'abbr': 'RAD ARTIF INTELL'},
            'FOLK MUSIC JOURNAL': {'if': 0.0, 'quartile': None, 'cas_zone': None,
                                   'cas_top': None, 'categories': [],
                                   'issn': '', 'issn_e': '', 'abbr': ''},
            'NO IMPACT FACTOR JOURNAL': {'if': None, 'quartile': None,
                                         'cas_zone': None, 'cas_top': None,
                                         'categories': [], 'issn': '1938-3207',
                                         'issn_e': '', 'abbr': ''},
            # 用来测「纯 base 路径」：主刊名 TEST 与缩写 T J TEST 不同
            'TEST-JOURNAL OF TESTING': {'if': 3.3, 'quartile': 'Q3',
                                        'cas_zone': 4, 'cas_top': None,
                                        'categories': ['TESTING'],
                                        'issn': '9999-1111', 'issn_e': '',
                                        'abbr': 'T J TEST'},
        }
        self.index_path = os.path.join(self.tmp, 'index.json')
        _write(self.index_path, index)
        self.idx = jcr.JcrIndex(self.index_path)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- 匹配方式 ----
    def test_match_by_issn(self):
        info = self.idx.lookup(journal='乱七八糟的刊名', issn='0028-4793')
        self.assertTrue(info['matched'])
        self.assertEqual(info['match'], 'issn')
        self.assertEqual(info['if'], 84.5)

    def test_issn_is_preferred_over_title(self):
        """ISSN 是最可靠的钥匙，优先于刊名。"""
        info = self.idx.lookup(journal='LANCET', issn='0028-4793')
        self.assertEqual(info['match'], 'issn')
        self.assertEqual(info['journal'], 'NEW ENGLAND JOURNAL OF MEDICINE')

    def test_issn_normalization_applies_to_both_sides(self):
        """查询值去连字符、映射表键也去连字符——两边必须一致。"""
        info = self.idx.lookup(journal='X', issn='0028-4793')
        self.assertTrue(info['matched'])
        self.assertEqual(info['if'], 84.5)

    def test_match_by_title(self):
        info = self.idx.lookup(journal='Lancet')
        self.assertTrue(info['matched'])
        self.assertEqual(info['match'], 'title')

    def test_match_by_title_strips_article(self):
        info = self.idx.lookup(journal='The Lancet')
        self.assertTrue(info['matched'])

    def test_match_by_base_name(self):
        """JCR 写「主刊名-副标题」，PubMed 只有主刊名。

        注意：走 abbrev 或 base 哪条路都对——两条都指向同一本刊。
        缩写匹配更可靠，所以优先级更高。
        """
        info = self.idx.lookup(journal='JAMA')
        self.assertTrue(info['matched'])
        self.assertIn(info['match'], ('base', 'abbrev'))
        self.assertTrue(info['journal'].startswith('JAMA'))
        self.assertEqual(info['if'], 65.4)

    def test_match_by_base_name_only(self):
        """缩写对不上时，靠主刊名也能找到。"""
        info = self.idx.lookup(journal='TEST')
        self.assertTrue(info['matched'])
        self.assertEqual(info['match'], 'base')
        self.assertEqual(info['if'], 3.3)

    def test_match_by_abbrev(self):
        # 合成索引的缩写表里有 'NEW ENGL J MED'
        info = self.idx.lookup(journal='New Engl J Med')
        self.assertTrue(info['matched'])
        self.assertEqual(info['match'], 'abbrev')

    def test_match_by_abbrev_fuzzy(self):
        """PubMed 的 MedlineTA 与 JCR 的 ISO 缩写风格不同。"""
        info = self.idx.lookup(journal='N Engl J Med')
        self.assertTrue(info['matched'])
        self.assertEqual(info['journal'], 'NEW ENGLAND JOURNAL OF MEDICINE')

    def test_ambiguous_base_is_not_guessed(self):
        """JACC 挂着 8 本子刊，猜不得——宁可查不到。"""
        info = self.idx.lookup(journal='JACC')
        self.assertFalse(info['matched'])
        self.assertEqual(info['match'], 'none')

    def test_exact_name_beats_sibling_with_same_base(self):
        """Radiology 与 Radiology-Artificial Intelligence 同族，
        精确匹配必须优先拿到 Radiology 本身。"""
        info = self.idx.lookup(journal='Radiology')
        self.assertTrue(info['matched'])
        self.assertEqual(info['journal'], 'RADIOLOGY')

    def test_nonexistent_journal_is_not_matched(self):
        info = self.idx.lookup(journal='Foo Bar Nonexistent Journal Xyz')
        self.assertFalse(info['matched'])
        self.assertIsNone(info['if'])

    def test_empty_journal_is_not_matched(self):
        self.assertFalse(self.idx.lookup(journal='')['matched'])
        self.assertFalse(self.idx.lookup()['matched'])

    # ---- 取值语义 ----
    def test_zero_if_and_null_if_are_different(self):
        """'<0.1' 归一为 0.0，'N/A' 归一为 null——语义不同，不能混。"""
        zero = self.idx.lookup(journal='FOLK MUSIC JOURNAL')
        self.assertTrue(zero['matched'])
        self.assertEqual(zero['if'], 0.0)
        null = self.idx.lookup(journal='NO IMPACT FACTOR JOURNAL')
        self.assertTrue(null['matched'])
        self.assertIsNone(null['if'])

    def test_no_quartile_is_none_not_empty_string(self):
        info = self.idx.lookup(journal='FOLK MUSIC JOURNAL')
        self.assertIsNone(info['quartile'])


class TestFormatLabel(unittest.TestCase):
    def test_full_label(self):
        self.assertEqual(
            jcr.format_label({'matched': True, 'if': 109, 'quartile': 'Q1',
                              'cas_zone': 1, 'cas_top': None}),
            'IF 109 · JCR Q1 · 中科院 1 区')

    def test_cas_top_suffix(self):
        self.assertIn('Top', jcr.format_label(
            {'matched': True, 'if': 50, 'quartile': 'Q1',
             'cas_zone': 1, 'cas_top': True}))

    def test_partial_info(self):
        self.assertEqual(
            jcr.format_label({'matched': True, 'if': 3.2, 'quartile': 'Q3',
                              'cas_zone': None}),
            'IF 3.2 · JCR Q3')

    def test_unmatched_returns_empty_string(self):
        self.assertEqual(jcr.format_label({'matched': False}), '')
        self.assertEqual(jcr.format_label({}), '')
        self.assertEqual(jcr.format_label(None), '')


class TestGracefulDegradation(unittest.TestCase):
    """索引文件缺失时要静默降级，不能让检索流程崩掉。"""

    def test_missing_index_returns_no_match(self):
        idx = jcr.JcrIndex(os.path.join(tempfile.gettempdir(), 'no_such_jcr.json'))
        self.assertFalse(idx.available)
        info = idx.lookup(journal='Lancet')
        self.assertFalse(info['matched'])
        self.assertIsNone(info['if'])

    def test_corrupt_index_returns_no_match(self):
        tmp = tempfile.mkdtemp(prefix='jcr_bad_')
        path = os.path.join(tmp, 'bad.json')
        with open(path, 'w', encoding='utf-8') as fh:
            fh.write('{ not json')
        idx = jcr.JcrIndex(path)
        self.assertFalse(idx.available)
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


class TestRealIndexIntegration(unittest.TestCase):
    """真实索引文件的集成校验——文件不存在就跳过。"""

    def setUp(self):
        self.idx = jcr.get_index()
        if not self.idx.available:
            self.skipTest('真实 JCR 索引文件未构建')

    def test_known_journals_resolve(self):
        for name in ('Lancet', 'N Engl J Med', 'BMJ', 'Cell'):
            info = self.idx.lookup(journal=name)
            self.assertTrue(info['matched'], f'{name} 应能查到')
            self.assertIsNotNone(info['if'])

    def test_issn_lookup_prefers_issn(self):
        info = self.idx.lookup(journal='完全不存在的刊名', issn='0028-4793')
        self.assertTrue(info['matched'])
        self.assertEqual(info['journal'], 'NEW ENGLAND JOURNAL OF MEDICINE')

    def test_index_has_both_quartile_systems(self):
        """JCR 分位与中科院大区是**两套体系**，必须分开存在。"""
        info = self.idx.lookup(journal='Lancet')
        self.assertTrue(info['matched'])
        self.assertIn(info['quartile'], ('Q1', 'Q2', 'Q3', 'Q4'))
        self.assertIn(info['cas_zone'], (1, 2, 3, 4))


if __name__ == '__main__':
    unittest.main()
