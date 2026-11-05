# -*- coding: utf-8 -*-
"""compare 模块单元测试（标准库 unittest，完全离线、不发网络请求）。

运行：
    python -m unittest discover -s tests -v
    python tests/test_compare.py
"""
from __future__ import annotations

import os
import sys
import unittest

# 把 scripts 目录挂到 sys.path，才能 import compare
# 路径由 __file__ 推导，不写死绝对路径，保证 CI 上可用。
_SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        'skills', 'medlit-agent', 'scripts')
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

import compare  # noqa: E402


def _parse_tables(md: str) -> list:
    """把 Markdown 拆成表格块，每个块是一个二维单元格列表。

    行的首尾竖线各去掉一个（而不是 strip 掉所有首尾竖线），
    否则「最后一列为空」的行会少一列，判断不出竖线污染。
    """
    blocks = []
    cur = []
    for ln in md.splitlines():
        if ln.lstrip().startswith('|'):
            cur.append(ln.strip())
        else:
            if cur:
                blocks.append(cur)
                cur = []
    if cur:
        blocks.append(cur)
    out = []
    for b in blocks:
        rows = []
        for r in b:
            body = r[1:-1] if r.startswith('|') and r.endswith('|') else r
            rows.append([c.strip() for c in body.split('|')])
        out.append(rows)
    return out


def _evidence(pmid, year, journal, design, size, pico, findings,
              effects=None, limitations=None, cite=None):
    """构造一条证据条目（字段与 pipeline 产出的结构一致）。"""
    return {
        'pmid': pmid,
        'study_design': design,
        'sample_size': size,
        'population': '2 型糖尿病合并肥胖的成人',
        'intervention': '司美格鲁肽',
        'comparator': '安慰剂',
        'primary_outcome': '体重变化',
        'key_findings': list(findings),
        'effect_sizes': list(effects or ['RR 0.65 (95%CI 0.56-0.76)']),
        'limitations': list(limitations or ['随访时长偏短']),
        'pico_match': pico,
        'relevance': 1,
        '_citation_key': cite or pmid,
        '_is_preprint': False,
        '_journal': journal,
        '_year': year,
        '_title': f'Study {pmid}',
        '_doi': '',
    }


POOLED_OK = [{
    'metric': 'RR', 'k': 3, 'pooled': 0.65, 'ci_low': 0.56, 'ci_high': 0.76,
    'se': 0.078, 'i2': 0.0, 'q': 0.64, 'df': 2, 'p_value': 0.73, 'tau2': 0.0,
    'model': 'fixed', 'heterogeneity': '低', 'skipped_no_ci': [], 'note': '',
}]

POOLED_FAIL = [{
    'metric': 'RR', 'k': 0, 'pooled': None, 'ci_low': None, 'ci_high': None,
    'se': None, 'i2': None, 'q': None, 'df': 0, 'p_value': None, 'tau2': None,
    'model': 'fixed', 'heterogeneity': '不适用', 'skipped_no_ci': [],
    'note': '可合并的效应量不足 2 个',
}]

GRADE_HIGH = {'certainty': '高', 'score': 4, 'start_score': 4, 'designs': ['RCT'],
              'downgrades': [], 'upgrades': [], 'notes': [], 'reason': 'rct',
              'note': ''}
GRADE_MID = {'certainty': '中', 'score': 3, 'start_score': 4, 'designs': ['RCT'],
             'downgrades': ['不精确性：2/2 篇样本量小于 50'], 'upgrades': [],
             'notes': [], 'reason': 'rct', 'note': ''}


def _arm_sema(label='司美格鲁肽', evidence=None, pooled=None, grading=None):
    return {
        'label': label,
        'evidence': evidence if evidence is not None else [
            _evidence('11111111', '2024', 'N Engl J Med', 'RCT', 'n=3117', '直接',
                      ['体重下降 15%', '胃肠不良反应 42%'],
                      limitations=['未报告随机化细节']),
            _evidence('22222222', '2023', 'Lancet', 'RCT', 'n=883', '直接',
                      ['体重下降 14%'],
                      limitations=['随访时长偏短']),
        ],
        'pooled': pooled if pooled is not None else POOLED_OK,
        'grading': grading if grading is not None else GRADE_HIGH,
        'query': '(semaglutide) AND (obesity)',
    }


def _arm_tirzepatide(evidence=None, pooled=None, grading=None):
    return {
        'label': '替尔泊肽',
        'evidence': evidence if evidence is not None else [
            _evidence('33333333', '2023', 'JAMA', 'RCT', 'n=2539', '直接',
                      ['体重下降 21%'],
                      limitations=['未报告随机化细节']),
        ],
        'pooled': pooled if pooled is not None else POOLED_FAIL,
        'grading': grading if grading is not None else GRADE_MID,
        'query': '(tirzepatide) AND (obesity)',
    }


class TestBuildComparison(unittest.TestCase):
    def test_two_arms_appear_in_overview_and_rows(self):
        out = compare.build_comparison([_arm_sema(), _arm_tirzepatide()],
                                       '司美格鲁肽 vs 替尔泊肽')
        self.assertIn('司美格鲁肽', out)
        self.assertIn('替尔泊肽', out)
        # 两张表的头
        self.assertIn('| 干预 | 纳入证据 | 研究设计分布 | GRADE 确定性 | 关键效应量 | 直接证据占比 |', out)
        self.assertIn('| 干预 | 引用标识 | 年份 | 期刊 | 设计 | 样本量 | PICO | 效应量 | 核心发现 |', out)
        # GRADE 列取值
        self.assertIn('高', out)
        self.assertIn('中', out)
        # 逐条证据行：PMID / 年份 / 期刊
        self.assertIn('11111111', out)
        self.assertIn('2024', out)
        self.assertIn('N Engl J Med', out)

    def test_overview_evidence_counts_are_correct(self):
        out = compare.build_comparison([_arm_sema(), _arm_tirzepatide()])
        # 第一张表是总览表：表头 + 分隔行 + 每个臂一行
        overview = _parse_tables(out)[0]
        self.assertEqual(overview[0][0], '干预')
        counts = {row[0]: int(row[1]) for row in overview[2:]
                  if row[1].isdigit()}
        self.assertEqual(counts.get('司美格鲁肽'), 2)
        self.assertEqual(counts.get('替尔泊肽'), 1)

    def test_overview_key_effect_and_direct_ratio(self):
        out = compare.build_comparison([_arm_sema(), _arm_tirzepatide()])
        self.assertIn('RR 0.65 (0.56–0.76)', out)
        self.assertIn('I²=0%', out)
        self.assertIn('100%', out)   # 司美格鲁肽两条都是「直接」
        self.assertIn('无法合并', out)  # 替尔泊肽 k=0

    def test_pipe_in_findings_is_replaced(self):
        ev = _evidence('44444444', '2022', 'BMJ', 'RCT', 'n=100', '部分',
                       ['HbA1c 下降 1.1% | 体重下降 3kg', '第二发现'])
        out = compare.build_comparison([_arm_sema(evidence=[ev]), _arm_tirzepatide()])
        # 竖线不能出现在表格单元格里（会打散 Markdown 表格）
        self.assertNotIn('HbA1c 下降 1.1% | 体重下降 3kg', out)
        self.assertIn('HbA1c 下降 1.1% / 体重下降 3kg', out)
        # 真正的判据：每张表的每一行单元格数都必须与表头一致
        for rows in _parse_tables(out):
            width = len(rows[0])
            for row in rows:
                self.assertEqual(len(row), width,
                                 msg=f'表格行列数不一致（疑似裸竖线）: {row}')

    def test_findings_truncated_to_90_chars(self):
        long_text = '很长的一句话' * 40
        ev = _evidence('55555555', '2022', 'BMJ', 'RCT', 'n=100', '部分',
                       [long_text, '第二条'])
        out = compare.build_comparison([_arm_sema(evidence=[ev]), _arm_tirzepatide()])
        # 截断后单元格长度不超过 90
        for rows in _parse_tables(out):
            for row in rows:
                if '55555555' in row:
                    self.assertLessEqual(len(row[-1]), 90)

    def test_output_is_valid_markdown_structure(self):
        out = compare.build_comparison([_arm_sema(), _arm_tirzepatide()])
        self.assertTrue(out.strip().startswith('## '))
        tables = _parse_tables(out)
        self.assertEqual(len(tables), 2)   # 总览表 + 逐条证据对照表
        for rows in tables:
            # 表头下必须紧跟一行分隔行
            sep = rows[1]
            self.assertTrue(all(set(c) <= set('-:') and c for c in sep),
                            msg=f'表格缺少分隔行: {sep}')
            # 每行列数必须与表头一致
            for row in rows:
                self.assertEqual(len(row), len(rows[0]))

    def test_difference_note_when_only_one_arm_has_pooled(self):
        out = compare.build_comparison([_arm_sema(), _arm_tirzepatide()])
        self.assertIn('差异说明', out)
        self.assertIn('替尔泊肽 的效应量不足以合并', out)
        self.assertIn('可能存在研究设计或指标口径差异', out)

    def test_difference_note_when_certainty_differs(self):
        out = compare.build_comparison([_arm_sema(grading=GRADE_HIGH),
                                        _arm_tirzepatide(grading=GRADE_MID)])
        self.assertIn('证据确定性高于', out)
        self.assertIn('高 vs 中', out)
        self.assertIn('主要差异来源', out)
        # 高确定性一侧没有降级因素时，应回落到低确定性一侧的降级因素
        self.assertIn('不精确性', out)

    def test_difference_note_uses_lower_arm_downgrades_as_fallback(self):
        """高确定性一侧没记录降级因素时，改用低确定性一侧的（那才是差距来源）。"""
        out = compare.build_comparison([_arm_sema(grading=GRADE_HIGH),
                                        _arm_tirzepatide(grading=GRADE_MID)])
        self.assertIn('不精确性：2/2 篇样本量小于 50', out)

    def test_difference_note_when_direct_ratio_low(self):
        indirect = [_evidence('66666666', '2021', 'Clin Ther', '队列', 'n=200',
                              '间接', ['体重下降 3kg'])]
        out = compare.build_comparison([_arm_sema(), _arm_tirzepatide(evidence=indirect)])
        self.assertIn('直接证据占比偏低', out)
        self.assertIn('结论外推需谨慎', out)

    def test_no_significant_difference_message(self):
        """两个臂证据结构一致、确定性相同、都能合并时不应硬造差异。"""
        a = _arm_sema(label='药A')
        b = _arm_tirzepatide()
        b['label'] = '药B'
        b['grading'] = GRADE_HIGH
        b['pooled'] = POOLED_OK
        out = compare.build_comparison([a, b])
        self.assertIn('各臂在证据数量、确定性与效应量方向上未见显著差异', out)

    def test_no_arms_does_not_claim_no_difference(self):
        """没有臂可比时不能说"未见显著差异"——那会把没数据说成没差别。"""
        out = compare.build_comparison([])
        self.assertIn('未提供可对比的干预臂', out)
        self.assertNotIn('未见显著差异', out)

    def test_all_arms_without_evidence(self):
        """所有臂都没证据时，不能说"效应量不足以合并/口径差异"。

        压根没有效应量可合，真实原因是没检索到数据；把它说成口径问题
        会把读者引向错误的排查方向。
        """
        empty = {'label': '药A', 'evidence': [], 'pooled': [], 'grading': {}}
        out = compare.build_comparison([empty, dict(empty, label='药B')])
        self.assertIn('未检索到可用证据', out)
        self.assertIn('无法比较', out)
        self.assertNotIn('效应量不足以合并', out)
        self.assertNotIn('口径差异', out)

    def test_all_arms_without_evidence_three_arms(self):
        """三个臂都没证据时，措辞用"各臂"而不是"两臂"。"""
        empty = {'label': '药A', 'evidence': [], 'pooled': [], 'grading': {}}
        out = compare.build_comparison([empty, dict(empty, label='药B'),
                                        dict(empty, label='药C')])
        self.assertIn('各臂均未检索到可用证据', out)
        self.assertNotIn('两臂', out)

    def test_mixed_arms_distinguish_no_evidence_from_unpoolable(self):
        """一臂有证据但合并不了、另一臂根本没证据：两者的措辞必须不同。

        - 有证据但合并不了 → 「效应量不足以合并，可能存在研究设计或指标口径差异」
        - 根本没证据       → 「未检索到可用证据」，不能甩锅给口径差异
        """
        with_ev = _arm_sema(label='药A')
        with_ev['pooled'] = POOLED_FAIL
        no_ev = {'label': '药B', 'evidence': [], 'pooled': [], 'grading': {}}
        out = compare.build_comparison([with_ev, no_ev])
        self.assertIn('药A 的效应量不足以合并，可能存在研究设计或指标口径差异', out)
        self.assertIn('药B 未检索到可用证据，无法参与对比', out)
        # 药B 没有证据，不该被说成"口径差异"
        self.assertNotIn('药B 的效应量不足以合并', out)

    def test_common_limitations_dedup_and_cap(self):
        evs = [_evidence('77777777', '2020', 'Lancet', 'RCT', 'n=10', '直接',
                         ['a'], limitations=['随访时长偏短', '样本量偏小']),
               _evidence('88888888', '2021', 'Lancet', 'RCT', 'n=10', '直接',
                         ['b'], limitations=['随访时长偏短', '终点定义不一致'])]
        out = compare.build_comparison([_arm_sema(evidence=evs), _arm_tirzepatide()])
        self.assertIn('共同局限', out)
        # 去重：「随访时长偏短」只出现一次
        self.assertEqual(out.count('随访时长偏短'), 1)
        self.assertIn('样本量偏小', out)
        self.assertIn('终点定义不一致', out)

    def test_output_is_valid_markdown_structure(self):
        out = compare.build_comparison([_arm_sema(), _arm_tirzepatide()])
        self.assertTrue(out.strip().startswith('## '))
        tables = _parse_tables(out)
        self.assertEqual(len(tables), 2)   # 总览表 + 逐条证据对照表
        for rows in tables:
            # 表头下必须紧跟一行分隔行
            sep = rows[1]
            self.assertTrue(all(set(c) <= set('-:') and c for c in sep),
                            msg=f'表格缺少分隔行: {sep}')
            # 每行列数必须与表头一致
            for row in rows:
                self.assertEqual(len(row), len(rows[0]))


class TestCompareSummary(unittest.TestCase):
    def test_ranking_by_certainty(self):
        s = compare.compare_summary([_arm_sema(grading=GRADE_MID),
                                     _arm_tirzepatide(grading=GRADE_HIGH)])
        self.assertEqual(s['certainty_ranking'], ['替尔泊肽', '司美格鲁肽'])

    def test_ranking_tie_broken_by_evidence_count(self):
        """确定性相同时，证据多的排前面。"""
        s = compare.compare_summary([_arm_sema(grading=GRADE_HIGH),
                                     _arm_tirzepatide(grading=GRADE_HIGH)])
        # 司美格鲁肽 2 篇 > 替尔泊肽 1 篇
        self.assertEqual(s['certainty_ranking'], ['司美格鲁肽', '替尔泊肽'])

    def test_ranking_stable_when_fully_tied(self):
        """确定性、证据数都相同时，保持输入顺序。"""
        a = _arm_sema(label='药A', grading=GRADE_HIGH)
        b = _arm_tirzepatide()
        b['label'] = '药B'
        b['grading'] = GRADE_HIGH
        b['pooled'] = POOLED_OK
        s = compare.compare_summary([a, b])
        self.assertEqual(s['certainty_ranking'], ['药A', '药B'])

    def test_arm_fields(self):
        s = compare.compare_summary([_arm_sema(), _arm_tirzepatide()])
        a0 = s['arms'][0]
        self.assertEqual(a0['label'], '司美格鲁肽')
        self.assertEqual(a0['n_evidence'], 2)
        self.assertEqual(a0['certainty'], '高')
        self.assertAlmostEqual(a0['direct_ratio'], 1.0)
        self.assertEqual(a0['pooled_metrics'], ['RR'])
        self.assertTrue(a0['pooled_ok'])
        self.assertEqual(s['arms'][1]['certainty'], '中')
        self.assertFalse(s['arms'][1]['pooled_ok'])
        self.assertEqual(s['no_pooled'], ['替尔泊肽'])
        self.assertIsInstance(s['note'], str)
        self.assertTrue(s['note'])

    def test_missing_grading_is_未评估(self):
        s = compare.compare_summary([_arm_sema(grading={}), _arm_tirzepatide()])
        self.assertEqual(s['arms'][0]['certainty'], '未评估')

    def test_empty_arms(self):
        s = compare.compare_summary([])
        self.assertEqual(s['arms'], [])
        self.assertEqual(s['certainty_ranking'], [])
        self.assertEqual(s['no_pooled'], [])
        self.assertTrue(s['note'])


class TestRecommendWording(unittest.TestCase):
    def test_unique_highest_arm(self):
        w = compare.recommend_wording([_arm_sema(grading=GRADE_HIGH),
                                        _arm_tirzepatide(grading=GRADE_MID)])
        self.assertIn('司美格鲁肽', w)
        self.assertIn('证据确定性最高', w)
        self.assertIn('高', w)
        self.assertIn('纳入 2 篇', w)
        self.assertIn('不等同于临床推荐', w)

    def test_tied_arms(self):
        w = compare.recommend_wording([_arm_sema(grading=GRADE_HIGH),
                                        _arm_tirzepatide(grading=GRADE_HIGH)])
        self.assertIn('司美格鲁肽', w)
        self.assertIn('替尔泊肽', w)
        self.assertIn('证据确定性相当', w)
        self.assertIn('均为 高', w)
        self.assertIn('不足以判断孰优孰劣', w)

    def test_all_arms_empty(self):
        """所有臂都没有证据时，措辞必须是规格给的那一句。"""
        empty = {'label': '药A', 'evidence': [], 'pooled': [], 'grading': {}}
        w = compare.recommend_wording([empty, dict(empty, label='药B')])
        self.assertIn('未获得可用于对比的证据', w)
        self.assertEqual(w, '本轮检索未获得可用于对比的证据。')

    def test_no_arms_at_all(self):
        w = compare.recommend_wording([])
        self.assertIn('未获得可用于对比的证据', w)

    def test_wording_avoids_clinical_advice(self):
        """措辞里不得出现「推荐」「应当使用」这类临床决策表述。"""
        w = compare.recommend_wording([_arm_sema(grading=GRADE_HIGH),
                                        _arm_tirzepatide(grading=GRADE_MID)])
        for bad in ('推荐使用', '应当使用', '应首选', '建议使用', '首选'):
            self.assertNotIn(bad, w)
        # 但必须显式声明不等同于临床推荐
        self.assertIn('不等同于临床推荐', w)


class TestComparisonDirName(unittest.TestCase):
    def test_different_questions_get_different_names(self):
        a = compare.comparison_dir_name('司美格鲁肽 vs 替尔泊肽')
        b = compare.comparison_dir_name('二甲双胍 vs 格列美脲')
        self.assertNotEqual(a, b)

    def test_same_question_is_stable(self):
        a = compare.comparison_dir_name('司美格鲁肽 vs 替尔泊肽')
        b = compare.comparison_dir_name('司美格鲁肽 vs 替尔泊肽')
        self.assertEqual(a, b)

    def test_chinese_is_preserved(self):
        name = compare.comparison_dir_name('司美格鲁肽 vs 替尔泊肽')
        self.assertIn('司美格鲁肽', name)
        self.assertIn('vs', name)

    def test_empty_question_falls_back(self):
        name = compare.comparison_dir_name('')
        self.assertTrue(name.startswith('compare_'))

    def test_name_shape(self):
        name = compare.comparison_dir_name('司美格鲁肽 vs 替尔泊肽')
        slug, _, digest = name.rpartition('_')
        self.assertEqual(len(digest), 4)
        self.assertTrue(all(c in '0123456789abcdef' for c in digest))
        self.assertLessEqual(len(slug), 16)


if __name__ == '__main__':
    unittest.main()
