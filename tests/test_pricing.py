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


class TestVisionModelPricing(unittest.TestCase):
    """多模态模型（图表识别用）也要计价。

    背景（2026-11-06 实测）：开启图表识别后，
    trace 里出现 `unpriced_models: ["Qwen/Qwen3-VL-8B-Instruct"]`，
    占总 token 的一半（25200 / 44186）——**成本被严重低估**。
    图片是 base64 内联进 prompt 的，输入 token 会被图片放大，
    所以单价不能按普通 8B 文本模型算。
    """

    def test_vl_model_has_a_price(self):
        for m in ('Qwen/Qwen3-VL-8B-Instruct',
                  'Qwen/Qwen3-VL-30B-A3B-Instruct',
                  'Qwen/Qwen3-VL-32B-Instruct'):
            self.assertGreater(pricing.lookup_price(m), 0.0,
                               f'{m} 应当有单价')

    def test_vl_price_is_not_zero(self):
        self.assertNotEqual(pricing.lookup_price('Qwen/Qwen3-VL-8B-Instruct'),
                            0.0)

    def test_vl_price_is_higher_than_plain_8b(self):
        """图片放大 token，VL 单价应高于同规模的纯文本模型。"""
        vl = pricing.lookup_price('Qwen/Qwen3-VL-8B-Instruct')
        text = pricing.lookup_price('Qwen/Qwen3-14B')
        self.assertGreater(vl, text)
