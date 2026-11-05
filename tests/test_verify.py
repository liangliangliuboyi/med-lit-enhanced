# -*- coding: utf-8 -*-
"""verify.py 的离线单元测试（不联网）。

全部模型调用都走假的 chat_fn 桩，返回预置 JSON——本文件不发任何网络请求。
"""
from __future__ import annotations

import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'skills', 'medlit-agent', 'scripts'))

import verify  # noqa: E402


# ---------------------------------------------------------------------------
# 测试夹具
# ---------------------------------------------------------------------------
def make_evidence(pmid='12345678', relevance=3, doi='', title='某随机对照试验',
                  key_findings=None, is_preprint=False):
    """造一条 reader 风格的证据条目。"""
    ev = {
        'pmid': pmid,
        'study_design': 'RCT',
        'sample_size': 'n=3117',
        'population': '成人 2 型糖尿病患者',
        'intervention': '二甲双胍',
        'comparator': '安慰剂',
        'primary_outcome': '主要心血管复合终点',
        'key_findings': key_findings or ['干预组主要终点发生率低于对照组'],
        'effect_sizes': ['RR 0.88 (95%CI 0.78-0.99)'],
        'limitations': ['随访 2 年'],
        'bias_assessment': {'randomization': '随机', 'blinding': '双盲'},
        'pico_match': '直接',
        'relevance': relevance,
        '_citation_key': ('doi:' + doi) if (is_preprint and doi) else pmid,
        '_is_preprint': is_preprint,
        '_journal': 'Lancet',
        '_year': '2024',
        '_title': title,
        '_doi': doi,
        '_text_source': 'fulltext',
    }
    return ev


def fake_chat(payload, *, role='reader', record=None):
    """造一个返回固定 JSON 的 chat_fn 桩。

    payload 可以是 dict（直接作为返回体）或 callable（收到 messages 后返回内容）。
    """
    def _fn(_role, messages, temperature=0.2, max_tokens=4000, model=None):
        if record is not None:
            record.append({'role': _role, 'messages': messages,
                           'temperature': temperature, 'max_tokens': max_tokens,
                           'model': model})
        content = payload(messages) if callable(payload) else payload
        return {'content': content, 'model': model or 'fake-model', 'elapsed': 0.1}
    return _fn


# ===========================================================================
# parse_claims
# ===========================================================================
class TestParseClaims(unittest.TestCase):
    def test_mixed_pmid_and_doi(self):
        answer = (
            '一项大型 RCT 显示该干预可降低主要终点风险 [PMID:12345678]。'
            '预印本报告了类似的趋势 [DOI:10.1101/2024.01.01.123456]。'
        )
        claims = verify.parse_claims(answer)
        self.assertEqual(len(claims), 2)
        self.assertEqual(claims[0]['citations'],
                         [{'type': 'pmid', 'value': '12345678'}])
        self.assertEqual(claims[1]['citations'],
                         [{'type': 'doi', 'value': '10.1101/2024.01.01.123456'}])
        self.assertEqual(claims[0]['index'], 1)
        self.assertEqual(claims[1]['index'], 2)
        # 正文里不应残留引用标记本身
        self.assertNotIn('[PMID:', claims[0]['text'])
        self.assertNotIn('[DOI:', claims[1]['text'])
        self.assertIn('RCT', claims[0]['text'])

    def test_two_citations_in_one_sentence(self):
        answer = '该结论在两项研究中均被观察到 [PMID:111] [PMID:222]。'
        claims = verify.parse_claims(answer)
        self.assertEqual(len(claims), 1)
        self.assertEqual(len(claims[0]['citations']), 2)
        self.assertEqual([c['value'] for c in claims[0]['citations']], ['111', '222'])

    def test_no_citation_paragraph_not_returned(self):
        answer = ('## 一句话结论\n\n'
                  '这是纯文字结论，没有任何引用标记。\n\n'
                  '这是另一段没有引用的文字。\n')
        self.assertEqual(verify.parse_claims(answer), [])

    def test_empty_answer(self):
        self.assertEqual(verify.parse_claims(''), [])
        self.assertEqual(verify.parse_claims(None), [])

    def test_text_truncated_to_300(self):
        long_text = '结论' * 400
        claims = verify.parse_claims(long_text + ' [PMID:123]')
        self.assertEqual(len(claims), 1)
        self.assertLessEqual(len(claims[0]['text']), 300)

    def test_decimal_not_split(self):
        """小数点不能被当成句号，否则 'RR 1.24' 会被撕成两半。"""
        answer = '风险比 RR 1.24 (95%CI 1.10-1.40) 提示获益 [PMID:123]。'
        claims = verify.parse_claims(answer)
        self.assertEqual(len(claims), 1)
        self.assertIn('1.24', claims[0]['text'])

    def test_only_citation_no_text_skipped(self):
        self.assertEqual(verify.parse_claims('[PMID:123]'), [])

    def test_ordered_list_item_not_split(self):
        """Markdown 有序列表的 "1. " 不是句号，不应把列表项劈成两半。"""
        answer = ('1. 第一条要点，说明干预有效 [PMID:12345678]。\n'
                  '2. 第二条要点，说明安全性可接受 [PMID:87654321]。\n')
        claims = verify.parse_claims(answer)
        self.assertEqual(len(claims), 2)
        self.assertIn('第一条要点', claims[0]['text'])
        self.assertIn('第二条要点', claims[1]['text'])

    def test_abbreviation_dot_not_split(self):
        """et al. / e.g. 里的点不是句号。"""
        answer = ('Smith et al. 报告了该结果 [PMID:12345678]。'
                  '结论与既往一致 e.g. 无明显异质性 [PMID:87654321]。')
        claims = verify.parse_claims(answer)
        self.assertEqual(len(claims), 2)
        self.assertIn('et al.', claims[0]['text'])
        self.assertIn('e.g.', claims[1]['text'])


# ===========================================================================
# verify_claims
# ===========================================================================
class TestVerifyClaims(unittest.TestCase):
    def test_supported_claim_not_flagged(self):
        answer = '该干预降低了主要终点风险 [PMID:12345678]。'
        chat = fake_chat({'verdict': '支持', 'reason': 'RCT 结果方向一致'})
        res = verify.verify_claims(answer, [make_evidence()], chat)
        self.assertEqual(res['unsupported'], [])
        self.assertEqual(res['partial'], [])
        self.assertEqual(res['failed'], [])
        # 判为"支持"的论断不算缺陷，但不能混进 uncertain —— 要单列出来
        self.assertEqual(res['uncertain'], [])
        self.assertEqual(len(res['supported']), 1)
        self.assertEqual(res['supported'][0]['verdict'], '支持')
        self.assertEqual(res['checked'], 1)
        self.assertEqual(res['total_claims'], 1)
        self.assertTrue(res['summary'])
        self.assertTrue(res['note'])

    def test_unsupported_claim_flagged(self):
        answer = '该干预降低了主要终点风险 [PMID:12345678]。'
        chat = fake_chat({'verdict': '不支持', 'reason': '文献终点与论断不一致'})
        res = verify.verify_claims(answer, [make_evidence()], chat)
        self.assertEqual(len(res['unsupported']), 1)
        rec = res['unsupported'][0]
        self.assertEqual(rec['verdict'], '不支持')
        self.assertIn('12345678', rec['citations'][0]['value'])
        self.assertIn('该干预', rec['text'])
        self.assertTrue(rec['reason'])
        self.assertEqual(res['supported'], [])

    def test_partial_claim_goes_to_partial(self):
        answer = '该干预降低了主要终点风险 [PMID:12345678]。'
        chat = fake_chat({'verdict': '部分支持', 'reason': '只支持方向'})
        res = verify.verify_claims(answer, [make_evidence()], chat)
        self.assertEqual(len(res['partial']), 1)
        self.assertEqual(res['unsupported'], [])
        self.assertEqual(res['supported'], [])

    def test_uncertain_claim_goes_to_uncertain(self):
        answer = '该干预降低了主要终点风险 [PMID:12345678]。'
        chat = fake_chat({'verdict': '无法判断', 'reason': '信息不足'})
        res = verify.verify_claims(answer, [make_evidence()], chat)
        self.assertEqual(len(res['uncertain']), 1)
        self.assertEqual(res['unsupported'], [])
        self.assertEqual(res['supported'], [])

    def test_chat_exception_recorded_as_failed_not_raised(self):
        answer = '该干预降低了主要终点风险 [PMID:12345678]。'

        def boom(*_a, **_kw):
            raise RuntimeError('网络超时')

        res = verify.verify_claims(answer, [make_evidence()], boom)
        self.assertEqual(len(res['failed']), 1)
        self.assertEqual(res['failed'][0]['verdict'], '核对失败')
        self.assertIn('网络超时', res['failed'][0]['reason'])
        self.assertEqual(res['unsupported'], [])

    def test_invalid_json_recorded_as_failed(self):
        answer = '该干预降低了主要终点风险 [PMID:12345678]。'
        res = verify.verify_claims(answer, [make_evidence()],
                                   fake_chat('这不是 JSON，只是一段解释。'))
        self.assertEqual(len(res['failed']), 1)
        self.assertEqual(res['failed'][0]['verdict'], '核对失败')

    def test_unmapped_citation_no_error_not_unsupported(self):
        """引用映射不到证据 → 不报错、不计入 unsupported。"""
        answer = '一个查不到的引用 [PMID:99999999]。'
        chat = fake_chat({'verdict': '支持', 'reason': '不该被调用'})
        res = verify.verify_claims(answer, [make_evidence()], chat)
        self.assertEqual(res['unsupported'], [])
        self.assertEqual(res['supported'], [])
        self.assertEqual(res['failed'], [])
        self.assertEqual(res['checked'], 0)
        self.assertEqual(res['total_claims'], 1)
        self.assertEqual(len(res['uncertain']), 1)
        self.assertEqual(res['uncertain'][0]['verdict'], '无法核对')

    def test_doi_citation_maps_to_preprint(self):
        answer = '预印本报告了类似趋势 [DOI:10.1101/2024.01.01.123456]。'
        ev = make_evidence(pmid='', relevance=2, doi='10.1101/2024.01.01.123456',
                           is_preprint=True)
        chat = fake_chat({'verdict': '支持', 'reason': '一致'})
        res = verify.verify_claims(answer, [ev], chat)
        self.assertEqual(res['checked'], 1)
        self.assertEqual(res['unsupported'], [])

    def test_max_checks_limits_work(self):
        answer = ''.join('结论 %d [PMID:12345678]。' % i for i in range(1, 6))
        calls = []
        chat = fake_chat({'verdict': '支持', 'reason': 'ok'}, record=calls)
        res = verify.verify_claims(answer, [make_evidence()], chat, max_checks=2)
        self.assertEqual(res['total_claims'], 5)
        self.assertEqual(res['checked'], 2)
        self.assertEqual(len(calls), 2)

    def test_role_is_reader(self):
        answer = '结论 [PMID:12345678]。'
        calls = []
        chat = fake_chat({'verdict': '支持', 'reason': 'ok'}, record=calls)
        verify.verify_claims(answer, [make_evidence()], chat)
        self.assertTrue(calls)
        self.assertEqual(calls[0]['role'], 'reader')

    def test_empty_answer_returns_zero(self):
        res = verify.verify_claims('', [make_evidence()], fake_chat({}))
        self.assertEqual(res['checked'], 0)
        self.assertEqual(res['total_claims'], 0)
        self.assertTrue(res['summary'])


# ===========================================================================
# cross_check_relevance
# ===========================================================================
def two_model_chat(score_a, score_b, *, fail_a=False, fail_b=False):
    """按 model 参数分别返回 score_a / score_b 的 chat_fn 桩。"""
    def _fn(_role, messages, temperature=0.2, max_tokens=4000, model=None):
        if model == 'model-A':
            if fail_a:
                raise RuntimeError('模型 A 挂了')
            return {'content': '{"relevance": %s, "reason": "A 的理由"}' % score_a,
                    'model': 'model-A'}
        if model == 'model-B':
            if fail_b:
                raise RuntimeError('模型 B 挂了')
            return {'content': '{"relevance": %s, "reason": "B 的理由"}' % score_b,
                    'model': 'model-B'}
        raise RuntimeError('未指定 model')
    return _fn


class TestCrossCheckRelevance(unittest.TestCase):
    def test_average_and_divergence(self):
        ev = make_evidence(relevance=4)
        chat = two_model_chat(5, 3)
        out = verify.cross_check_relevance([ev], chat, 'model-A', 'model-B')
        self.assertEqual(len(out), 1)
        self.assertAlmostEqual(out[0]['relevance_cross'], 4.0)
        self.assertTrue(out[0]['relevance_divergent'])
        self.assertFalse(out[0]['relevance_cross_failed'])
        self.assertEqual(out[0]['relevance_by_model'], {'modelA': 5.0, 'modelB': 3.0})
        self.assertEqual(out[0]['_relevance_original'], 4)

    def test_no_divergence_when_close(self):
        ev = make_evidence(relevance=4)
        out = verify.cross_check_relevance([ev], two_model_chat(5, 4), 'model-A', 'model-B')
        self.assertAlmostEqual(out[0]['relevance_cross'], 4.5)
        self.assertFalse(out[0]['relevance_divergent'])

    def test_both_models_fail_keeps_original(self):
        ev = make_evidence(relevance=3)
        chat = two_model_chat(5, 3, fail_a=True, fail_b=True)
        out = verify.cross_check_relevance([ev], chat, 'model-A', 'model-B')
        self.assertTrue(out[0]['relevance_cross_failed'])
        self.assertEqual(out[0]['relevance_cross'], 3)
        self.assertEqual(out[0]['_relevance_original'], 3)
        self.assertFalse(out[0]['relevance_divergent'])

    def test_single_model_failure_uses_other(self):
        ev = make_evidence(relevance=3)
        chat = two_model_chat(5, 3, fail_a=True)
        out = verify.cross_check_relevance([ev], chat, 'model-A', 'model-B')
        self.assertFalse(out[0]['relevance_cross_failed'])
        self.assertAlmostEqual(out[0]['relevance_cross'], 3.0)
        self.assertIsNone(out[0]['relevance_by_model']['modelA'])
        self.assertAlmostEqual(out[0]['relevance_by_model']['modelB'], 3.0)

    def test_returns_new_list_original_untouched(self):
        original = make_evidence(relevance=2)
        before = dict(original)
        out = verify.cross_check_relevance([original], two_model_chat(5, 5),
                                           'model-A', 'model-B')
        self.assertIsNot(out, [original])
        self.assertIsNot(out[0], original)
        # 原对象不能被写脏
        self.assertEqual(original, before)
        self.assertNotIn('relevance_cross', original)
        # 原列表本身也不能变长
        src = [original]
        verify.cross_check_relevance(src, two_model_chat(5, 5), 'model-A', 'model-B')
        self.assertEqual(len(src), 1)

    def test_invalid_json_scores_as_failure(self):
        ev = make_evidence(relevance=3)
        out = verify.cross_check_relevance([ev], fake_chat('不是 JSON'),
                                           'model-A', 'model-B')
        self.assertTrue(out[0]['relevance_cross_failed'])
        self.assertEqual(out[0]['relevance_cross'], 3)

    def test_chat_fn_without_model_kwarg_falls_back_to_role(self):
        """调用方的 chat_fn 不接受 model 参数时，应退化为按角色路由而不是崩掉。"""
        ev = make_evidence(relevance=3)
        seen_roles = []

        def no_model_kwarg(role, messages, temperature=0.2, max_tokens=4000):
            seen_roles.append(role)
            return {'content': '{"relevance": 4, "reason": "ok"}', 'model': role}

        out = verify.cross_check_relevance([ev], no_model_kwarg, 'model-A', 'model-B')
        self.assertAlmostEqual(out[0]['relevance_cross'], 4.0)
        self.assertEqual(seen_roles, ['reader', 'critic'])
        # 指定的模型没能生效 → 必须留痕，否则这只是一次"同一个模型打两遍分"
        self.assertTrue(out[0]['relevance_model_ignored'])

    def test_model_ignored_false_when_model_honored(self):
        ev = make_evidence(relevance=3)
        out = verify.cross_check_relevance([ev], two_model_chat(5, 3), 'model-A', 'model-B')
        self.assertFalse(out[0]['relevance_model_ignored'])
        self.assertFalse(out[0]['relevance_same_model'])

    def test_empty_evidence_returns_empty_list(self):
        self.assertEqual(verify.cross_check_relevance([], two_model_chat(5, 5)), [])


# ===========================================================================
# cross_check_gain
# ===========================================================================
class TestCrossCheckGain(unittest.TestCase):
    def test_changed_and_avg_delta(self):
        before = [make_evidence(pmid='1', relevance=3),
                  make_evidence(pmid='2', relevance=4),
                  make_evidence(pmid='3', relevance=5)]
        after = [
            dict(before[0], relevance_cross=4.0, relevance_divergent=False,
                 relevance_cross_failed=False),
            dict(before[1], relevance_cross=4.0, relevance_divergent=False,
                 relevance_cross_failed=False),
            dict(before[2], relevance_cross=5.0, relevance_divergent=False,
                 relevance_cross_failed=False),
        ]
        gain = verify.cross_check_gain(before, after)
        # 第 1 条 3→4 变了，第 2 条 4→4 没变，第 3 条 5→5 没变
        self.assertEqual(gain['changed'], 1)
        # 平均绝对变化 = (1 + 0 + 0) / 3
        self.assertAlmostEqual(gain['avg_delta'], 0.33, places=2)
        self.assertEqual(gain['divergent'], 0)
        self.assertEqual(gain['failed'], 0)

    def test_divergent_and_failed_counts(self):
        before = [make_evidence(pmid='1', relevance=3),
                  make_evidence(pmid='2', relevance=3)]
        after = [
            dict(before[0], relevance_cross=4.0, relevance_divergent=True,
                 relevance_cross_failed=False),
            dict(before[1], relevance_cross=3, relevance_divergent=False,
                 relevance_cross_failed=True),
        ]
        gain = verify.cross_check_gain(before, after)
        self.assertEqual(gain['divergent'], 1)
        self.assertEqual(gain['failed'], 1)
        # 失败条目不计入均值
        self.assertAlmostEqual(gain['avg_delta'], 1.0, places=2)

    def test_empty_inputs(self):
        gain = verify.cross_check_gain([], [])
        self.assertEqual(gain['changed'], 0)
        self.assertAlmostEqual(gain['avg_delta'], 0.0)
        self.assertEqual(gain['divergent'], 0)
        self.assertEqual(gain['failed'], 0)


class TestParallelExecution(unittest.TestCase):
    """并发执行的行为约束。

    背景（2026-11-05 实测）：B8 相关度交叉验证原来是**串行**的，
    5 篇文献 × 2 个模型 = 10 次调用一次跑，实测 458.8 秒，
    占全文模型调用数的 40%。改成并发后实测加速比 3.31 倍。
    """

    def _evidence(self, n=5):
        return [{'pmid': str(i), 'relevance': 3, 'title': f'T{i}',
                 'key_findings': ['x'], 'study_design': 'RCT'}
                for i in range(n)]

    def _tracker(self, delay=0.03):
        """返回 (chat_fn, 状态字典)。状态里有峰值并发数。"""
        state = {'inflight': 0, 'peak': 0, 'calls': 0}
        lock = threading.Lock()

        def chat_fn(role, messages, temperature=0.2, max_tokens=800, model=None):
            with lock:
                state['inflight'] += 1
                state['peak'] = max(state['peak'], state['inflight'])
                state['calls'] += 1
            try:
                time.sleep(delay)
                return {'content': '{"relevance": 4, "reason": "ok"}',
                        'model': model or role, 'tokens': 10}
            finally:
                with lock:
                    state['inflight'] -= 1
        return chat_fn, state

    def test_return_order_matches_input_order(self):
        """并发下完成顺序是乱的，返回顺序必须按输入还原。"""
        ev = self._evidence(6)
        # 让不同文献的延迟不同，制造乱序完成
        delays = [0.06, 0.01, 0.05, 0.02, 0.04, 0.03]
        call_n = {'i': 0}
        lock = threading.Lock()

        def chat_fn(role, messages, temperature=0.2, max_tokens=800, model=None):
            with lock:
                i = call_n['i']
                call_n['i'] += 1
            time.sleep(delays[i % len(delays)])
            return {'content': '{"relevance": 4, "reason": "ok"}',
                    'model': model or role, 'tokens': 10}

        out = verify.cross_check_relevance(ev, chat_fn, model_a='A', model_b='B')
        self.assertEqual([d['pmid'] for d in out], [e['pmid'] for e in ev],
                         '并发后返回顺序必须与输入一致')

    def test_concurrency_actually_happens(self):
        """峰值并发必须 > 1，否则等于没并行。"""
        chat_fn, state = self._tracker()
        ev = self._evidence(6)
        verify.cross_check_relevance(ev, chat_fn, model_a='A', model_b='B')
        self.assertGreater(state['peak'], 1,
                           f'峰值并发只有 {state["peak"]}，并行没生效')

    def test_parallel_is_faster_than_serial(self):
        """并发必须真的比串行快。"""
        ev = self._evidence(5)
        chat_fn, _ = self._tracker(delay=0.05)
        t0 = time.time()
        verify.cross_check_relevance(ev, chat_fn, model_a='A', model_b='B')
        par = time.time() - t0

        chat_fn2, _ = self._tracker(delay=0.05)
        t0 = time.time()
        verify.cross_check_relevance(ev, chat_fn2, model_a='A', model_b='B',
                                     max_workers=1)
        ser = time.time() - t0
        self.assertLess(par, ser, f'并行 {par:.3f}s 不应慢于串行 {ser:.3f}s')

    def test_one_side_failure_does_not_lose_papers(self):
        """某一侧打分失败不能导致整篇文献丢失。"""
        ev = self._evidence(4)
        calls = {'n': 0}
        lock = threading.Lock()

        def chat_fn(role, messages, temperature=0.2, max_tokens=800, model=None):
            with lock:
                calls['n'] += 1
                n = calls['n']
            if n % 3 == 0:
                raise RuntimeError('boom')
            return {'content': '{"relevance": 5, "reason": "ok"}',
                    'model': model or role, 'tokens': 10}

        out = verify.cross_check_relevance(ev, chat_fn, model_a='A', model_b='B')
        self.assertEqual(len(out), len(ev), '并发失败不能让文献变少')
        for d in out:
            self.assertIn('relevance_cross', d)
            self.assertIn('relevance_cross_failed', d)

    def test_original_evidence_not_mutated(self):
        ev = self._evidence(3)
        chat_fn, _ = self._tracker()
        verify.cross_check_relevance(ev, chat_fn, model_a='A', model_b='B')
        for e in ev:
            self.assertNotIn('relevance_cross', e,
                             '不能原地改调用方传入的 evidence')

    def test_result_fields_unchanged(self):
        """返回结构不能变（上游 pipeline 依赖这些字段）。"""
        ev = self._evidence(2)
        chat_fn, _ = self._tracker()
        out = verify.cross_check_relevance(ev, chat_fn, model_a='A', model_b='B')
        for d in out:
            for k in ('relevance_cross', 'relevance_by_model',
                      'relevance_divergent', 'relevance_cross_failed',
                      '_relevance_original', 'relevance_models'):
                self.assertIn(k, d, f'缺少字段 {k}')

    def test_verify_claims_order_is_preserved(self):
        """论断列表的顺序要按正文出现顺序，不能被并发打乱。"""
        answer = ('结论一 [PMID:11111111]。\n'
                  '结论二 [PMID:22222222]。\n'
                  '结论三 [PMID:33333333]。\n'
                  '结论四 [PMID:44444444]。')
        ev = [{'pmid': p, 'title': 'T', 'study_design': 'RCT',
               'key_findings': ['x'], 'relevance': 4}
              for p in ('11111111', '22222222', '33333333', '44444444')]
        delays = [0.05, 0.01, 0.04, 0.02]
        call_n = {'i': 0}
        lock = threading.Lock()

        def chat_fn(role, messages, temperature=0.2, max_tokens=800, model=None):
            with lock:
                i = call_n['i']
                call_n['i'] += 1
            time.sleep(delays[i % len(delays)])
            return {'content': '{"verdict": "支持", "reason": "ok"}',
                    'model': 'm', 'tokens': 10}

        res = verify.verify_claims(answer, ev, chat_fn, max_checks=10)
        self.assertEqual(res['checked'], 4)
        self.assertEqual(len(res['supported']), 4)
        texts = [s['text'] for s in res['supported']]
        self.assertTrue(texts[0].startswith('结论一'), texts[:1])
        self.assertTrue(texts[-1].startswith('结论四'), texts[-1:])

    def test_verify_claims_failure_is_isolated(self):
        """一条核验失败不影响其他条，也不能抛异常。"""
        answer = ('甲 [PMID:11111111]。\n'
                  '乙 [PMID:22222222]。\n'
                  '丙 [PMID:33333333]。')
        ev = [{'pmid': p, 'title': 'T', 'key_findings': ['x'], 'relevance': 4}
              for p in ('11111111', '22222222', '33333333')]
        calls = {'n': 0}
        lock = threading.Lock()

        def chat_fn(role, messages, temperature=0.2, max_tokens=800, model=None):
            with lock:
                calls['n'] += 1
                n = calls['n']
            if n == 2:
                raise RuntimeError('boom')
            return {'content': '{"verdict": "支持", "reason": "ok"}',
                    'model': 'm', 'tokens': 10}

        res = verify.verify_claims(answer, ev, chat_fn, max_checks=10)
        self.assertEqual(res['checked'], 3)
        self.assertEqual(len(res['failed']), 1, '失败的应单独记进 failed')
        self.assertGreaterEqual(len(res['supported']), 1)

    def test_verify_claims_result_keys_unchanged(self):
        answer = '甲 [PMID:11111111]。'
        ev = [{'pmid': '11111111', 'title': 'T', 'key_findings': ['x'],
               'relevance': 4}]
        chat_fn, _ = self._tracker()
        res = verify.verify_claims(answer, ev, chat_fn, max_checks=10)
        for k in ('checked', 'total_claims', 'unsupported', 'partial',
                  'uncertain', 'failed', 'summary', 'note'):
            self.assertIn(k, res, f'缺少键 {k}')


if __name__ == '__main__':
    unittest.main()
