# -*- coding: utf-8 -*-
"""cache.py 的离线单元测试（绝不在真实目录里写缓存）。"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'skills', 'medlit-agent', 'scripts'))

import cache  # noqa: E402
import pricing  # noqa: E402


class FakePubMed:
    """假 PubMed：记录被调用了几次，返回固定结果。"""

    def __init__(self, result=None, exc=None):
        self.calls = 0
        self.result = result if result is not None else {
            'count': 3, 'ids': ['1', '2', '3'], 'translation': 'x[tiab]', 'error': ''}
        self.exc = exc

    def esearch(self, term, retmax=20, sort='relevance', mindate=None):
        self.calls += 1
        if self.exc:
            raise self.exc
        return dict(self.result)


class TempCacheCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='medlit-cache-test-')

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestCacheKey(TempCacheCase):
    def test_same_key_for_case_and_whitespace_differences(self):
        a = cache.cache_key('Diabetes  Mellitus', retmax=20)
        b = cache.cache_key('diabetes mellitus', retmax=20)
        self.assertEqual(a, b)

    def test_different_retmax_gives_different_key(self):
        self.assertNotEqual(cache.cache_key('x', retmax=20),
                            cache.cache_key('x', retmax=30))

    def test_none_and_empty_mindate_are_same(self):
        self.assertEqual(cache.cache_key('x', mindate=None),
                         cache.cache_key('x', mindate=''))

    def test_key_is_hex_and_stable(self):
        k = cache.cache_key('某个中文检索词')
        self.assertEqual(k, cache.cache_key('某个中文检索词'))
        self.assertRegex(k, r'^[0-9a-f]+$')


class TestEsearchCache(TempCacheCase):
    def test_set_then_get(self):
        c = cache.EsearchCache(self.tmp)
        payload = {'count': 2, 'ids': ['1', '2'], 'translation': 't', 'error': ''}
        c.set('k1', payload)
        got = c.get('k1')
        self.assertIsNotNone(got)
        self.assertEqual(got['ids'], ['1', '2'])

    def test_miss_returns_none(self):
        c = cache.EsearchCache(self.tmp)
        self.assertIsNone(c.get('nope'))

    def test_error_result_is_not_cached(self):
        c = cache.EsearchCache(self.tmp)
        c.set('k', {'count': 0, 'ids': [], 'translation': '', 'error': '上游故障'})
        self.assertIsNone(c.get('k'))

    def test_empty_result_not_cached(self):
        c = cache.EsearchCache(self.tmp)
        c.set('k', {'count': 0, 'ids': [], 'translation': '', 'error': ''})
        self.assertIsNone(c.get('k'))

    def test_expired_entry_returns_none(self):
        c = cache.EsearchCache(self.tmp, ttl_hours=0)
        c.set('k', {'count': 1, 'ids': ['1'], 'translation': '', 'error': ''})
        time.sleep(0.01)
        self.assertIsNone(c.get('k'))

    def test_forged_old_timestamp_is_expired(self):
        """把 saved_at 改老，条目必须立刻过期。

        只靠 TTL=0 测不出"按 saved_at 判断过期"这件事——
        这里直接伪造一个很久以前的时间戳来验证。
        """
        c = cache.EsearchCache(self.tmp, ttl_hours=72)
        c.set('k', {'count': 1, 'ids': ['1'], 'translation': '', 'error': ''})
        path = os.path.join(self.tmp, 'k.json')
        with open(path, 'r', encoding='utf-8') as f:
            rec = json.load(f)
        rec['saved_at'] = time.time() - 3600 * 100      # 100 小时前
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(rec, f)
        self.assertIsNone(c.get('k'))

    def test_missing_saved_at_is_expired(self):
        """saved_at 缺失/坏掉一律当过期——宁可多打一次 NCBI。"""
        c = cache.EsearchCache(self.tmp, ttl_hours=72)
        path = os.path.join(self.tmp, 'k.json')
        with open(path, 'w', encoding='utf-8') as f:
            json.dump({'payload': {'count': 1, 'ids': ['1'],
                                   'translation': '', 'error': ''}}, f)
        self.assertIsNone(c.get('k'))

    def test_get_never_writes_to_disk(self):
        """get 是只读操作：未命中不该创建目录或文件。"""
        c = cache.EsearchCache(os.path.join(self.tmp, 'sub'))
        self.assertIsNone(c.get('nope'))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, 'sub')))

    def test_path_traversal_key_is_rejected(self):
        """键会拼进文件名，含 ../ 的键必须被拒绝，不能写到缓存目录外面去。"""
        c = cache.EsearchCache(self.tmp)
        payload = {'count': 1, 'ids': ['1'], 'translation': '', 'error': ''}
        c.set('../escaped', payload)
        self.assertIsNone(c.get('../escaped'))
        self.assertFalse(os.path.exists(os.path.join(os.path.dirname(self.tmp),
                                                     'escaped.json')))

    def test_corrupted_file_returns_none_without_raising(self):
        c = cache.EsearchCache(self.tmp)
        with open(os.path.join(self.tmp, 'bad.json'), 'w', encoding='utf-8') as f:
            f.write('{ 这不是合法 JSON')
        self.assertIsNone(c.get('bad'))

    def test_clear_returns_removed_count(self):
        c = cache.EsearchCache(self.tmp)
        c.set('a', {'count': 1, 'ids': ['1'], 'translation': '', 'error': ''})
        c.set('b', {'count': 1, 'ids': ['2'], 'translation': '', 'error': ''})
        n = c.clear()
        self.assertEqual(n, 2)
        self.assertIsNone(c.get('a'))

    def test_write_is_atomic_no_temp_left(self):
        c = cache.EsearchCache(self.tmp)
        c.set('k', {'count': 1, 'ids': ['1'], 'translation': '', 'error': ''})
        leftovers = [f for f in os.listdir(self.tmp) if f.endswith('.tmp')]
        self.assertEqual(leftovers, [])


class TestCachedEsearch(TempCacheCase):
    def test_second_call_uses_cache(self):
        pm = FakePubMed()
        c = cache.EsearchCache(self.tmp)
        stats = cache.CacheStats()
        r1 = cache.cached_esearch(pm, c, 'diabetes', stats=stats)
        r2 = cache.cached_esearch(pm, c, 'diabetes', stats=stats)
        self.assertEqual(pm.calls, 1)
        self.assertEqual(r1['ids'], r2['ids'])
        self.assertGreaterEqual(stats.as_dict()['hit'], 1)

    def test_exception_propagates(self):
        pm = FakePubMed(exc=RuntimeError('上游挂了'))
        c = cache.EsearchCache(self.tmp)
        with self.assertRaises(RuntimeError):
            cache.cached_esearch(pm, c, 'x')

    def test_failure_result_not_cached(self):
        pm = FakePubMed(result={'count': 0, 'ids': [], 'translation': '',
                                 'error': 'Search Backend failed'})
        c = cache.EsearchCache(self.tmp)
        cache.cached_esearch(pm, c, 'x')
        cache.cached_esearch(pm, c, 'x')
        self.assertEqual(pm.calls, 2)   # 故障不缓存，会真的再打一次


class TestCacheStats(TempCacheCase):
    def test_counters(self):
        s = cache.CacheStats()
        s.hit()
        s.hit()
        s.miss()
        s.record_write()
        d = s.as_dict()
        self.assertEqual(d['hit'], 2)
        self.assertEqual(d['miss'], 1)
        self.assertEqual(d['write'], 1)


class TestNoRepoPollution(unittest.TestCase):
    """测试期间绝不能在仓库里留下缓存文件。"""

    def test_cache_dir_is_not_inside_runs_root(self):
        """缓存目录必须在 runs_root 的**同级**，不能混进运行产物目录。"""
        runs_root = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), 'runs')
        cache_dir = cache.default_cache_dir(runs_root)
        self.assertFalse(os.path.abspath(cache_dir).startswith(
            os.path.abspath(runs_root) + os.sep))
        self.assertTrue(cache_dir.endswith('.medlit-cache'))

    def test_repo_has_no_cache_artifacts(self):
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        stray = [n for n in os.listdir(repo) if n.endswith('.medlit-cache')]
        self.assertEqual(stray, [], f'仓库里出现了缓存目录：{stray}')


class TestPricing(unittest.TestCase):
    """pricing.py 的计价逻辑（与 tests/test_pricing.py 互补，覆盖集成细节）。"""

    def test_lookup_price_exact_and_prefix(self):
        self.assertGreater(pricing.lookup_price('deepseek-ai/DeepSeek-V3.2'), 0)
        # 带日期后缀的变体也应命中前缀
        self.assertGreater(pricing.lookup_price('deepseek-ai/DeepSeek-V3.2-0728'), 0)

    def test_lookup_price_unknown_is_zero(self):
        self.assertEqual(pricing.lookup_price('some/unknown-model'), 0.0)
        self.assertEqual(pricing.lookup_price(''), 0.0)

    def test_price_table_covers_config_models(self):
        """config.example.json 里用到的模型都应该在价格表里。"""
        for m in ('deepseek-ai/DeepSeek-V3.2', 'deepseek-ai/DeepSeek-R1',
                  'Qwen/Qwen3.5-35B-A3B', 'Qwen/Qwen3-32B', 'Qwen/Qwen3-14B',
                  'Qwen/Qwen2.5-72B-Instruct', 'Qwen/Qwen2.5-7B-Instruct',
                  'Qwen/Qwen3-Reranker-8B'):
            self.assertIn(m, pricing.DEFAULT_PRICE_TABLE, m)

    def test_estimate_cost_totals(self):
        usage = {'reader': 1_000_000, 'critic': 100_000}
        role_models = {'reader': 'deepseek-ai/DeepSeek-V3.2',
                       'critic': 'unknown/model'}
        r = pricing.estimate_cost(usage, None, role_models)
        self.assertEqual(r['total_tokens'], 1_100_000)
        self.assertEqual(r['priced_tokens'], 1_000_000)
        self.assertEqual(r['unpriced_models'], ['unknown/model'])
        self.assertTrue(r['breakdown']['reader']['priced'])
        self.assertFalse(r['breakdown']['critic']['priced'])

    def test_estimate_cost_models_subdict_form(self):
        """主流程用的是 usage 里挂 '_models' 的写法，也要能用。"""
        usage = {'reader': 1_000_000,
                 '_models': {'reader': 'deepseek-ai/DeepSeek-V3.2'}}
        r = pricing.estimate_cost(usage)
        self.assertEqual(r['priced_tokens'], 1_000_000)
        self.assertNotIn('_models', r['breakdown'])

    def test_estimate_cost_returns_expected_keys(self):
        """返回结构固定：下游 trace 直接把它写进产物，键不能变。"""
        r = pricing.estimate_cost({'reader': 100})
        self.assertEqual(set(r.keys()),
                         {'total_tokens', 'priced_tokens', 'unpriced_models',
                          'estimated_cny', 'breakdown'})

    def test_breakdown_records_model_per_role(self):
        """每个角色要记下自己用的模型名——只有角色名是查不到价的。"""
        usage = {'reader': 1000, 'critic': 2000,
                 '_models': {'reader': 'deepseek-ai/DeepSeek-V3.2',
                             'critic': 'unknown/model'}}
        r = pricing.estimate_cost(usage)
        self.assertEqual(r['breakdown']['reader']['model'],
                         'deepseek-ai/DeepSeek-V3.2')
        self.assertEqual(r['breakdown']['critic']['model'], 'unknown/model')
        # breakdown 的键是角色名，不是模型名
        self.assertNotIn('deepseek-ai/DeepSeek-V3.2', r['breakdown'])

    def test_models_key_is_not_counted_as_role(self):
        """'_models' 是映射，不是某个角色的 token 数。"""
        r = pricing.estimate_cost({'reader': 100,
                                   '_models': {'reader': 'x/y'}})
        self.assertEqual(r['total_tokens'], 100)
        self.assertNotIn('_models', r['breakdown'])

    def test_estimate_cost_cny_math(self):
        """费用 = token / 1e6 * 单价。"""
        r = pricing.estimate_cost(
            {'x': 1_000_000}, None, {'x': 'deepseek-ai/DeepSeek-V3.2'})
        expected = 1_000_000 / 1_000_000 * pricing.lookup_price(
            'deepseek-ai/DeepSeek-V3.2')
        self.assertAlmostEqual(r['estimated_cny'], expected, places=4)

    def test_format_cost_line_contains_numbers(self):
        r = pricing.estimate_cost(
            {'x': 1_000_000}, None, {'x': 'deepseek-ai/DeepSeek-V3.2'})
        line = pricing.format_cost_line(r)
        self.assertIn('1,000,000', line)
        self.assertIn('¥', line)

    def test_format_cost_line_unknown(self):
        r = pricing.estimate_cost({'x': 500}, None, {'x': 'unknown/model'})
        self.assertIn('成本未知', pricing.format_cost_line(r))


if __name__ == '__main__':
    unittest.main()
