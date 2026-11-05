# -*- coding: utf-8 -*-
"""模型调用链的超时与降级行为测试。

背景（2026-11-05 实测）：一次真实运行卡了 28 分钟没进展，进程 CPU 几乎为 0
（纯网络等待）。根因是三重放大：

    一次 chat 最坏耗时 = 降级链长度 × timeout × retries
                       = 4 个模型 × 180s × 3 次重试 = 36 分钟

而 socket 超时走的是通用 `except Exception` 分支，**和 429/5xx 一样重试**——
同样的请求再发 3 次，每次都等满一个 timeout。

这两个问题都必须有测试锁住，否则很容易被改回去。
"""

import os
import socket
import sys
import unittest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'skills', 'medlit-agent', 'scripts'))

import sf  # noqa: E402


class TestPostTimeout(unittest.TestCase):
    """`_post` 的超时处理。"""

    def _patch_urlopen(self, counter, exc):
        def fake_urlopen(req, timeout=None):
            counter.append(timeout)
            raise exc
        return fake_urlopen

    def test_socket_timeout_is_not_retried(self):
        """超时只试一次——再发大概率还是超时，却要再等一个 timeout。"""
        calls = []
        orig = sf.urllib.request.urlopen
        sf.urllib.request.urlopen = self._patch_urlopen(
            calls, socket.timeout('timed out'))
        try:
            with self.assertRaises(Exception) as ctx:
                sf._post('http://example.invalid/x', {'a': 1}, {},
                         timeout=1, retries=3)
            self.assertEqual(len(calls), 1, '超时后不该再重试')
            self.assertIn('超时', str(ctx.exception))
        finally:
            sf.urllib.request.urlopen = orig

    def test_timeout_error_is_not_retried_either(self):
        calls = []
        orig = sf.urllib.request.urlopen
        sf.urllib.request.urlopen = self._patch_urlopen(
            calls, TimeoutError('timed out'))
        try:
            with self.assertRaises(Exception):
                sf._post('http://example.invalid/x', {'a': 1}, {},
                         timeout=1, retries=3)
            self.assertEqual(len(calls), 1)
        finally:
            sf.urllib.request.urlopen = orig

    def test_server_error_still_retries(self):
        """5xx 仍然要重试——那是暂时性故障，退避后往往就好了。"""
        calls = []
        orig = sf.urllib.request.urlopen

        def fake(req, timeout=None):
            calls.append(timeout)
            raise sf.urllib.error.HTTPError(
                'http://x', 503, 'busy', {}, None)
        sf.urllib.request.urlopen = fake
        try:
            with self.assertRaises(Exception):
                sf._post('http://example.invalid/x', {'a': 1}, {},
                         timeout=1, retries=3)
            self.assertGreaterEqual(len(calls), 2, '5xx 应重试')
        finally:
            sf.urllib.request.urlopen = orig


class TestRouterBudget(unittest.TestCase):
    """降级链的总时长预算。"""

    class _FakeProvider:
        name = 'fake'
        base = 'http://fake'

        def __init__(self, behaviour):
            self.behaviour = behaviour
            self.timeout = 180
            self.calls = []

        def available_models(self):
            return []

        def chat(self, model, messages, temperature=0.2, max_tokens=4000,
                 timeout=None):
            self.calls.append((model, timeout))
            return self.behaviour(model)

    def test_budget_stops_fallback_chain(self):
        """预算耗尽就停，不再往下试模型。"""
        def behaviour(model):
            # 每个模型都"慢"：把预算吃掉
            return {'content': '', 'model': model, 'elapsed': 0, 'tokens': 0}
        prov = self._FakeProvider(behaviour)
        # 预算极小 → 第一次调用后预算就没了
        router = sf.ModelRouter(prov, {'reader': ['m1', 'm2', 'm3', 'm4']},
                                call_budget=1)
        with self.assertRaises(Exception):
            router.chat('reader', [{'role': 'user', 'content': 'x'}])
        # 关键：不能把 4 个模型全试一遍
        self.assertLessEqual(len(prov.calls), 2,
                             f'预算应限制降级次数，实际调了 {len(prov.calls)} 次')

    def test_single_model_timeout_capped_by_budget(self):
        """单次调用的超时不能超过剩余预算。"""
        prov = self._FakeProvider(lambda m: {'content': 'ok', 'model': m,
                                             'elapsed': 0, 'tokens': 1})
        router = sf.ModelRouter(prov, {'reader': ['m1']}, call_budget=30)
        router.chat('reader', [{'role': 'user', 'content': 'x'}])
        self.assertTrue(prov.calls)
        model, timeout = prov.calls[0]
        self.assertLessEqual(timeout, 30,
                             '单次超时不应超过总预算')

    def test_successful_model_short_circuits(self):
        """第一个模型成功就不该再试后面的。"""
        prov = self._FakeProvider(
            lambda m: {'content': 'ok', 'model': m, 'elapsed': 0, 'tokens': 1})
        router = sf.ModelRouter(prov, {'reader': ['m1', 'm2', 'm3']},
                                call_budget=60)
        router.chat('reader', [{'role': 'user', 'content': 'x'}])
        self.assertEqual(len(prov.calls), 1)

    def test_failed_model_falls_back_to_next(self):
        """第一个模型失败要顺延到第二个。"""
        def behaviour(model):
            if model == 'm1':
                raise RuntimeError('boom')
            return {'content': 'ok', 'model': model, 'elapsed': 0, 'tokens': 1}
        prov = self._FakeProvider(behaviour)
        router = sf.ModelRouter(prov, {'reader': ['m1', 'm2']},
                                call_budget=60)
        res = router.chat('reader', [{'role': 'user', 'content': 'x'}])
        self.assertEqual(res['model'], 'm2')
        self.assertEqual(len(prov.calls), 2)

    def test_errors_are_recorded_for_troubleshooting(self):
        """失败原因要留痕，否则用户只看到"全部失败"无从排查。"""
        prov = self._FakeProvider(
            lambda m: (_ for _ in ()).throw(RuntimeError('boom')))
        router = sf.ModelRouter(prov, {'reader': ['m1']}, call_budget=60)
        with self.assertRaises(Exception):
            router.chat('reader', [{'role': 'user', 'content': 'x'}])
        self.assertTrue(router.last_errors.get('reader'))
        self.assertIn('boom', ' '.join(router.last_errors['reader']))


class TestFailCounting(unittest.TestCase):
    """失败计数：偶发失败不能污染整个角色。

    背景（2026-11-05 实测）：精读是多线程并发的，原来「一次失败就永久拉黑
    首选模型」，导致一个线程的偶发失败让**所有后续调用**都切到降级链上
    处理长文本会超时的模型，每个调用耗满 240 秒预算，整轮检索卡死。
    """

    class _P:
        name = 'fake'
        base = 'http://fake'
        timeout = 180

        def __init__(self, fail_times=0):
            self.fail_times = fail_times
            self.calls = []

        def available_models(self):
            return []

        def chat(self, model, messages, temperature=0.2, max_tokens=4000,
                 timeout=None):
            self.calls.append(model)
            if self.fail_times > 0:
                self.fail_times -= 1
                raise RuntimeError('boom')
            return {'content': 'ok', 'model': model, 'elapsed': 0, 'tokens': 1}

    def test_one_failure_does_not_blacklist_model(self):
        """偶发失败一次后，首选模型仍在链上。"""
        prov = self._P(fail_times=1)
        router = sf.ModelRouter(prov, {'reader': ['m1', 'm2']},
                                call_budget=60, fail_threshold=2)
        # 第一次调用：m1 失败一次（计数 1），顺延到 m2 成功
        router.chat('reader', [{'role': 'user', 'content': 'x'}])
        # 第二次调用：m1 只失败过 1 次 < 阈值 2 → 仍应先用 m1
        prov.fail_times = 0
        router.chat('reader', [{'role': 'user', 'content': 'x'}])
        self.assertEqual(prov.calls[-1], 'm1',
                         '偶发失败不应把首选模型踢出降级链')

    def test_consecutive_failures_reach_threshold_and_fallback(self):
        """连续失败达到阈值后，才真正降级。"""
        prov = self._P(fail_times=0)
        router = sf.ModelRouter(prov, {'reader': ['m1', 'm2']},
                                call_budget=60, fail_threshold=2)
        prov.fail_times = 99
        for _ in range(3):
            try:
                router.chat('reader', [{'role': 'user', 'content': 'x'}])
            except Exception:
                pass
        # m1 连续失败 ≥2 次后，链上不该再有 m1
        self.assertNotIn('m1', router.chain_of('reader'))

    def test_fail_counts_are_per_role(self):
        """失败计数按角色隔离，reader 失败不影响 writer。"""
        prov = self._P(fail_times=99)
        router = sf.ModelRouter(prov, {'reader': ['m1'], 'writer': ['m1']},
                                call_budget=60, fail_threshold=2)
        for _ in range(2):
            try:
                router.chat('reader', [{'role': 'user', 'content': 'x'}])
            except Exception:
                pass
        self.assertNotIn('m1', router.chain_of('reader'))
        self.assertIn('m1', router.chain_of('writer'))

    def test_chain_never_empty_raises_clearly(self):
        """链上模型全被拉黑时要给出明确错误，而不是静默返回空。"""
        prov = self._P(fail_times=99)
        router = sf.ModelRouter(prov, {'reader': ['m1']},
                                call_budget=60, fail_threshold=1)
        with self.assertRaises(Exception):
            router.chat('reader', [{'role': 'user', 'content': 'x'}])
        with self.assertRaises(Exception):
            router.chat('reader', [{'role': 'user', 'content': 'x'}])
        self.assertEqual(router.chain_of('reader'), [])


if __name__ == '__main__':
    unittest.main()
