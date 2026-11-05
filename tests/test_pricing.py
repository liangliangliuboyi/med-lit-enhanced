# -*- coding: utf-8 -*-
"""pricing.py 的离线单元测试。"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'skills', 'medlit-agent', 'scripts'))

import pricing  # noqa: E402


class TestLookupPrice(unittest.TestCase):
    def test_exact_match(self):
        self.assertGreater(pricing.lookup_price('deepseek-ai/DeepSeek-V3.2'), 0)

    def test_case_insensitive(self):
        self.assertEqual(pricing.lookup_price('DEEPSEEK-AI/DEEPSEEK-V3.2'),
                         pricing.lookup_price('deepseek-ai/DeepSeek-V3.2'))

    def test_prefix_match_with_date_suffix(self):
        """平台上的模型名常带日期后缀，前缀匹配是必需的。"""
        self.assertGreater(pricing.lookup_price('deepseek-ai/DeepSeek-V3.2-0728'), 0)

    def test_unknown_returns_zero(self):
        self.assertEqual(pricing.lookup_price('some/unknown-model'), 0.0)
        self.assertEqual(pricing.lookup_price(''), 0.0)

    def test_custom_table_overrides(self):
        tbl = {'my/model': 9.9}
        self.assertEqual(pricing.lookup_price('my/model', tbl), 9.9)
        self.assertEqual(pricing.lookup_price('my/model-2024', tbl), 9.9)


class TestEstimateCost(unittest.TestCase):
    def test_empty_usage(self):
        r = pricing.estimate_cost({})
        self.assertEqual(r['total_tokens'], 0)
        self.assertEqual(r['estimated_cny'], 0.0)

    def test_role_models_mapping_is_used(self):
        """角色名不是模型名，必须靠 role_models 映射才能查到价。"""
        usage = {'reader': 1_000_000, 'critic': 100_000}
        role_models = {'reader': 'deepseek-ai/DeepSeek-V3.2',
                       'critic': 'unknown/model'}
        r = pricing.estimate_cost(usage, None, role_models)
        self.assertEqual(r['total_tokens'], 1_100_000)
        self.assertEqual(r['priced_tokens'], 1_000_000)
        self.assertIn('unknown/model', r['unpriced_models'])
        self.assertGreater(r['estimated_cny'], 0)
        self.assertTrue(r['breakdown']['reader']['priced'])
        self.assertFalse(r['breakdown']['critic']['priced'])
        self.assertEqual(r['breakdown']['critic']['model'], 'unknown/model')

    def test_model_name_as_key(self):
        """也支持直接用模型名当键。"""
        r = pricing.estimate_cost({'deepseek-ai/DeepSeek-V3.2': 1_000_000})
        self.assertGreater(r['priced_tokens'], 0)

    def test_non_numeric_tokens_ignored(self):
        r = pricing.estimate_cost({'reader': 'abc'})
        self.assertEqual(r['total_tokens'], 0)

    def test_zero_tokens_skipped(self):
        r = pricing.estimate_cost({'reader': 0})
        self.assertEqual(r['total_tokens'], 0)


class TestFormatCostLine(unittest.TestCase):
    def test_known_cost(self):
        line = pricing.format_cost_line(pricing.estimate_cost(
            {'reader': 1_000_000},
            None, {'reader': 'deepseek-ai/DeepSeek-V3.2'}))
        self.assertIn('tokens', line)
        self.assertIn('¥', line)

    def test_unknown_cost(self):
        line = pricing.format_cost_line(pricing.estimate_cost(
            {'reader': 1000}, None, {'reader': 'unknown/model'}))
        self.assertIn('成本未知', line)

    def test_empty_estimate(self):
        # 空估算也要给出一句可读的话（而不是空串），便于直接写进日志
        line = pricing.format_cost_line({})
        self.assertIn('tokens', line)


if __name__ == '__main__':
    unittest.main()
