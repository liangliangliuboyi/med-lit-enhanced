# -*- coding: utf-8 -*-
"""figures.py 的离线单元测试。

**完全离线**：不发任何网络请求。下载走注入的 downloader 替身，
模型调用走桩 chat_fn。
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

import figures  # noqa: E402


# ---------------------------------------------------------------------------
# 桩件
# ---------------------------------------------------------------------------
# 一张"图片"的内容。必须是非空字节，否则 _read_one 会判"内容为空"。
PNG_BYTES = (b'\x89PNG\r\n\x1a\n' + b'fake-image-payload-for-tests' * 8)


def _ok_json(kind='forest_plot', desc='横轴为风险比，纵轴为研究名称。',
             kv='HR 0.72 (95% CI 0.60-0.86)'):
    """造一个模型会返回的合法 JSON 字符串。"""
    import json
    return json.dumps({'kind': kind, 'description': desc,
                       'key_values': kv}, ensure_ascii=False)


class StubChat:
    """桩模型调用。

    记录调用次数与并发峰值；可以给每个调用指定延迟（用来制造乱序完成），
    也可以指定某个调用抛异常（用来测失败隔离）。
    """

    def __init__(self, content=None, delays=None, raise_on=None,
                 errors=None):
        self.content = content if content is not None else _ok_json()
        self.delays = delays or {}
        self.raise_on = raise_on or set()
        self.errors = errors or {}
        self.calls = 0
        self.lock = threading.Lock()
        self.active = 0
        self.peak = 0
        self.completed = []          # 完成顺序（用于验证乱序）
        self.seen_messages = []

    def __call__(self, role, messages, temperature=0.2, max_tokens=800,
                 model=None, **kwargs):
        with self.lock:
            self.calls += 1
            n = self.calls
            self.active += 1
            self.peak = max(self.peak, self.active)
        try:
            time.sleep(self.delays.get(n, 0))
            self.seen_messages.append(messages)
            with self.lock:
                self.completed.append(n)
            if n in self.raise_on:
                raise RuntimeError(self.errors.get(n, 'boom'))
            return {'content': self.content, 'model': 'stub-model'}
        finally:
            with self.lock:
                self.active -= 1


def _fake_downloader(payloads=None, ctype='image/png'):
    """下载替身：按 URL 返回指定字节，永不联网。

    默认**每个 URL 返回不同的字节**——真实的图各不相同；如果所有 URL
    都返回同一份内容，内容指纹去重会把后面的图全判成重复（那正是
    去重逻辑该做的事，但会掩盖其他测试）。
    """
    payloads = payloads or {}

    def _dl(url):
        for key, val in payloads.items():
            if key in url:
                if isinstance(val, tuple):
                    return val[0], val[1]
                return val, ctype
        # 按 URL 派生一份稳定且互不相同的内容
        return PNG_BYTES + url.encode('utf-8'), ctype

    return _dl


def _fulltext(n, prefix='https://example.org/pmc/article/'):
    """造一个带 n 张图的全文结构。"""
    return {
        'source': 'europe_pmc',
        'text': '正文',
        'sections': ['INTRO'],
        'figures': [
            {'href': '%sfig%d.jpg' % (prefix, i),
             'caption': '图 %d 图注' % (i + 1)}
            for i in range(n)
        ],
        'url': 'https://example.org/pmc/article/1',
    }


def _reader(chat, **kwargs):
    kwargs.setdefault('downloader', _fake_downloader())
    return figures.FigureReader(chat, **kwargs)


# ---------------------------------------------------------------------------
# 1. 顺序不变
# ---------------------------------------------------------------------------
class TestOrderPreserved(unittest.TestCase):
    """并发下完成顺序是乱的，返回顺序必须还原成输入顺序。"""

    def test_output_order_matches_input_order(self):
        # 延迟递减 → 完成顺序与提交顺序正好相反
        chat = StubChat(delays={1: 0.30, 2: 0.24, 3: 0.18, 4: 0.12, 5: 0.06})
        reader = _reader(chat)
        ft = _fulltext(5)
        out = reader.read_all(ft)
        self.assertEqual(len(out), 5)
        self.assertEqual([o['index'] for o in out], [0, 1, 2, 3, 4])
        for i, item in enumerate(out):
            self.assertEqual(item['href'], ft['figures'][i]['href'])
            self.assertEqual(item['caption'], ft['figures'][i]['caption'])

    def test_completion_order_was_actually_scrambled(self):
        """确认上一条测试真的制造出了乱序（否则顺序断言是假阳性）。"""
        chat = StubChat(delays={1: 0.30, 2: 0.24, 3: 0.18, 4: 0.12, 5: 0.06})
        out = _reader(chat).read_all(_fulltext(5))
        self.assertNotEqual(chat.completed, sorted(chat.completed))
        # 但返回顺序仍然是输入顺序
        self.assertEqual([o['index'] for o in out], [0, 1, 2, 3, 4])

    def test_single_figure_order(self):
        chat = StubChat()
        out = _reader(chat).read_all(_fulltext(1))
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]['index'], 0)


# ---------------------------------------------------------------------------
# 2. 并发确实发生，且有上限
# ---------------------------------------------------------------------------
class TestConcurrency(unittest.TestCase):
    def test_concurrency_actually_happens(self):
        chat = StubChat(delays={n: 0.15 for n in range(1, 6)})
        out = _reader(chat).read_all(_fulltext(5))
        self.assertEqual(len(out), 5)
        self.assertGreater(chat.peak, 1, '应当真的并发，而不是串行')

    def test_peak_concurrency_within_cap(self):
        """并发度硬上限 8：即使调用方要 64，也不得超过 8。"""
        chat = StubChat(delays={n: 0.15 for n in range(1, 21)})
        reader = _reader(chat, workers=64, max_figures=20)
        out = reader.read_all(_fulltext(20))
        self.assertEqual(len(out), 20)
        self.assertLessEqual(chat.peak, figures.MAX_WORKERS_CAP)
        self.assertGreater(chat.peak, 1)

    def test_workers_capped_in_constructor(self):
        reader = figures.FigureReader(lambda *a, **k: {}, workers=999)
        self.assertLessEqual(reader.workers, figures.MAX_WORKERS_CAP)
        self.assertGreaterEqual(reader.workers, 1)

    def test_default_workers_is_four(self):
        reader = figures.FigureReader(lambda *a, **k: {})
        self.assertEqual(reader.workers, 4)


# ---------------------------------------------------------------------------
# 3. 单张失败隔离
# ---------------------------------------------------------------------------
class TestFailureIsolation(unittest.TestCase):
    def test_one_failure_does_not_affect_others(self):
        """第 2 张抛异常：其余照常，失败那条 error 非空，函数不抛异常。"""
        chat = StubChat(raise_on={2}, errors={2: '模型服务 500'})
        reader = _reader(chat)
        out = reader.read_all(_fulltext(5))   # 不抛异常即为通过
        self.assertEqual(len(out), 5)
        failed = out[1]
        self.assertTrue(failed['error'])
        self.assertIn('500', failed['error'])
        self.assertEqual(failed['description'], '')
        for i in (0, 2, 3, 4):
            self.assertEqual(out[i]['error'], '', '第 %d 张不该受影响' % i)
            self.assertTrue(out[i]['description'])

    def test_all_failing_still_returns_entries(self):
        chat = StubChat(raise_on={1, 2, 3})
        out = _reader(chat).read_all(_fulltext(3))
        self.assertEqual(len(out), 3)
        for item in out:
            self.assertTrue(item['error'])

    def test_download_failure_isolated(self):
        """下载失败（自定义 downloader 抛异常）不影响其他图。"""
        def dl(url):
            if 'fig1.jpg' in url:
                raise OSError('连接超时')
            # 其余图内容各不相同，避免被内容指纹去重误判
            return PNG_BYTES + url.encode('utf-8'), 'image/png'

        chat = StubChat()
        reader = figures.FigureReader(chat, downloader=dl)
        out = reader.read_all(_fulltext(3))
        self.assertEqual(len(out), 3)
        self.assertTrue(out[1]['error'])
        self.assertIn('连接超时', out[1]['error'])
        self.assertEqual(out[0]['error'], '')
        self.assertEqual(out[2]['error'], '')

    def test_non_image_content_type_skipped(self):
        """非 image/* 的响应要跳过——出版商页面返回 HTML 时不能喂给模型。"""
        def dl(url):
            return b'<html>nope</html>', 'text/html'

        chat = StubChat()
        reader = figures.FigureReader(chat, downloader=dl)
        out = reader.read_all(_fulltext(2))
        self.assertEqual(len(out), 2)
        for item in out:
            self.assertTrue(item['error'])
            self.assertIn('不是图片', item['error'])
        self.assertEqual(chat.calls, 0, '不该把非图片送进模型')

    def test_oversize_image_skipped(self):
        def dl(url):
            return b'x' * 5000, 'image/png'

        chat = StubChat()
        reader = figures.FigureReader(chat, downloader=dl, max_bytes=100)
        out = reader.read_all(_fulltext(2))
        self.assertEqual(len(out), 2)
        for item in out:
            self.assertTrue(item['error'])
            self.assertIn('过大', item['error'])
        self.assertEqual(chat.calls, 0)


# ---------------------------------------------------------------------------
# 4. 去重
# ---------------------------------------------------------------------------
class TestDeduplication(unittest.TestCase):
    def test_failed_download_does_not_poison_dedup(self):
        """下载失败不该占住去重表——否则后面的同地址图会被误判成"重复"，
        把真正的下载错误掩盖掉。"""
        attempts = []

        def dl(url):
            attempts.append(url)
            raise OSError('图床超时')

        chat = StubChat()
        reader = figures.FigureReader(chat, downloader=dl)
        out = reader.read_all({
            'figures': [
                {'href': 'https://example.org/a.jpg', 'caption': '图1'},
                {'href': 'https://example.org/a.jpg', 'caption': '图1副本'},
            ],
            'url': 'https://example.org/',
        })
        self.assertEqual(len(out), 2)
        for item in out:
            self.assertTrue(item['error'])
            self.assertNotIn('重复', item['error'],
                             '下载失败不应被报成"重复"')
            self.assertIn('图床超时', item['error'])
        self.assertEqual(len(attempts), 2, '同地址应当各自试一次')

    def test_same_href_sent_to_model_only_once(self):
        """同一张图出现两次（href 相同）→ 只送模型一次。"""
        chat = StubChat()
        reader = _reader(chat)
        ft = {
            'figures': [
                {'href': 'https://example.org/a.jpg', 'caption': '图1'},
                {'href': 'https://example.org/a.jpg', 'caption': '图1（重复）'},
                {'href': 'https://example.org/b.jpg', 'caption': '图2'},
            ],
            'url': 'https://example.org/',
        }
        out = reader.read_all(ft)
        self.assertEqual(len(out), 3)
        self.assertEqual(chat.calls, 2, '重复图不应重复送模型')
        dup = [o for o in out if o['error'] and '重复' in o['error']]
        self.assertEqual(len(dup), 1)
        self.assertEqual(dup[0]['index'], 1)

    def test_same_content_different_href_deduplicated(self):
        """地址不同但内容相同 → 靠 SHA-256 指纹去重，只送模型一次。"""
        chat = StubChat()
        # 两个地址返回完全相同的字节
        reader = _reader(chat, downloader=_fake_downloader({
            'a.jpg': PNG_BYTES,
            'copy-of-a.jpg': PNG_BYTES,
        }))
        ft = {
            'figures': [
                {'href': 'https://example.org/a.jpg', 'caption': '图1'},
                {'href': 'https://example.org/copy-of-a.jpg', 'caption': '图1副本'},
            ],
            'url': 'https://example.org/',
        }
        out = reader.read_all(ft)
        self.assertEqual(len(out), 2)
        self.assertEqual(chat.calls, 1, '内容相同应被指纹去重')
        dup = [o for o in out if '重复' in o['error']]
        self.assertEqual(len(dup), 1)
        self.assertEqual(dup[0]['index'], 1)

    def test_fingerprint_is_sha256_prefix_16(self):
        """指纹应当是内容 SHA-256 的前 16 位十六进制。"""
        import hashlib
        expected = hashlib.sha256(PNG_BYTES).hexdigest()[:16]
        self.assertEqual(figures._fingerprint(PNG_BYTES), expected)
        self.assertEqual(len(expected), 16)
        self.assertTrue(all(c in '0123456789abcdef' for c in expected))
        # 内容不同 → 指纹不同
        self.assertNotEqual(figures._fingerprint(PNG_BYTES),
                            figures._fingerprint(PNG_BYTES + b'x'))


# ---------------------------------------------------------------------------
# 5 & 6. 模型输出解析
# ---------------------------------------------------------------------------
class TestOutputParsing(unittest.TestCase):
    def _read_one(self, content):
        chat = StubChat(content=content)
        out = _reader(chat).read_all(_fulltext(1))
        self.assertEqual(len(out), 1)
        return out[0]

    def test_plain_json(self):
        item = self._read_one(_ok_json())
        self.assertEqual(item['error'], '')
        self.assertEqual(item['kind'], 'forest_plot')
        self.assertIn('横轴', item['description'])
        self.assertIn('HR 0.72', item['key_values'])

    def test_fenced_json_block(self):
        content = '```json\n' + _ok_json(kind='survival_curve') + '\n```'
        item = self._read_one(content)
        self.assertEqual(item['error'], '')
        self.assertEqual(item['kind'], 'survival_curve')
        self.assertTrue(item['description'])

    def test_json_with_surrounding_prose(self):
        content = ('好的，我来分析这张图：\n'
                   + _ok_json(kind='roc_curve')
                   + '\n以上，希望有帮助。')
        item = self._read_one(content)
        self.assertEqual(item['error'], '')
        self.assertEqual(item['kind'], 'roc_curve')
        self.assertTrue(item['description'])

    def test_json_with_prose_containing_braces(self):
        """正文里带花括号也不能把 JSON 截断。"""
        content = ('分析如下（注意 {HR} 这个写法）：\n'
                   + _ok_json(kind='bar_chart')
                   + '\n完毕。')
        item = self._read_one(content)
        self.assertEqual(item['error'], '')
        self.assertEqual(item['kind'], 'bar_chart')

    def test_non_json_output_is_error(self):
        """喂非 JSON → 该条 error 非空，不抛异常。"""
        item = self._read_one('抱歉，我无法识别这张图片。')
        self.assertTrue(item['error'])
        self.assertEqual(item['error'], figures.ERR_UNPARSED)
        self.assertEqual(item['description'], '')

    def test_special_box_tokens_stripped(self):
        """部分模型会带 <|begin_of_box|> 之类的特殊标记（实测 GLM-4.5V）。"""
        content = ('<|begin_of_box|>' + _ok_json(kind='roc_curve')
                   + '<|end_of_box|>')
        item = self._read_one(content)
        self.assertEqual(item['error'], '')
        self.assertEqual(item['kind'], 'roc_curve')

    def test_truncated_json_is_error_not_crash(self):
        """输出被截断（JSON 没闭合）→ 记解析失败，不抛异常。"""
        item = self._read_one('{"kind": "forest_plot", "description": "很长很长')
        self.assertTrue(item['error'])
        self.assertEqual(item['error'], figures.ERR_UNPARSED)

    def test_empty_output_is_error(self):
        item = self._read_one('')
        self.assertTrue(item['error'])
        self.assertEqual(item['description'], '')

    def test_extract_json_helper_variants(self):
        """直接测解析器本身的容错。"""
        self.assertIsNone(figures._extract_json('not json at all'))
        self.assertIsNone(figures._extract_json(''))
        self.assertIsNone(figures._extract_json(None))
        obj = figures._extract_json(_ok_json())
        self.assertIsInstance(obj, dict)
        self.assertEqual(obj['kind'], 'forest_plot')

    def test_extract_json_helper_edge_cases(self):
        self.assertIsInstance(figures._extract_json('```json\n{"a": 1}\n```'), dict)
        self.assertIsInstance(figures._extract_json('前缀 {"a": 1} 后缀'), dict)
        self.assertIsInstance(figures._extract_json('{"a": {"b": 1}}'), dict)
        self.assertIsNone(figures._extract_json('{不是 JSON}'))
        # 字符串里带花括号不能把 JSON 截断
        obj = figures._extract_json('{"desc": "效应量 {HR} 显著"}')
        self.assertIsInstance(obj, dict)
        self.assertEqual(obj['desc'], '效应量 {HR} 显著')
        # 前面的非 JSON 花括号不能挡住后面的真 JSON
        obj2 = figures._extract_json('{说明} 然后 {"kind": "bar_chart"}')
        self.assertIsInstance(obj2, dict)
        self.assertEqual(obj2['kind'], 'bar_chart')


# ---------------------------------------------------------------------------
# 7. 没有图
# ---------------------------------------------------------------------------
class TestNoFigures(unittest.TestCase):
    def test_extract_figures_empty_variants(self):
        self.assertEqual(figures.extract_figures({}), [])
        self.assertEqual(figures.extract_figures({'figures': []}), [])
        self.assertEqual(figures.extract_figures(None), [])
        self.assertEqual(figures.extract_figures({'text': 'x'}), [])
        self.assertEqual(figures.extract_figures({'figures': 'not-a-list'}), [])

    def test_extract_figures_numbers_in_order(self):
        figs = figures.extract_figures(_fulltext(3))
        self.assertEqual(len(figs), 3)
        self.assertEqual([f['index'] for f in figs], [0, 1, 2])
        self.assertEqual(figs[0]['href'], 'https://example.org/pmc/article/fig0.jpg')
        self.assertEqual(figs[0]['caption'], '图 1 图注')

    def test_read_all_with_no_figures_returns_empty(self):
        chat = StubChat()
        out = _reader(chat).read_all({'text': 'x', 'sections': []})
        self.assertEqual(out, [])
        self.assertEqual(chat.calls, 0)

    def test_read_all_with_empty_fulltext_returns_empty(self):
        chat = StubChat()
        out = _reader(chat).read_all({})
        self.assertEqual(out, [])
        self.assertEqual(chat.calls, 0)

    def test_read_all_with_none_returns_empty(self):
        chat = StubChat()
        self.assertEqual(_reader(chat).read_all(None), [])


# ---------------------------------------------------------------------------
# 8. max_figures 上限
# ---------------------------------------------------------------------------
class TestMaxFigures(unittest.TestCase):
    def test_only_first_n_sent_to_model(self):
        chat = StubChat()
        reader = _reader(chat, max_figures=3)
        out = reader.read_all(_fulltext(10))
        self.assertEqual(chat.calls, 3, '只有前 3 张该送模型')
        self.assertEqual(len(out), 10, '结果里要体现全部 10 张')
        for i, item in enumerate(out):
            if i < 3:
                self.assertEqual(item['error'], '')
                self.assertTrue(item['description'])
            else:
                self.assertIn('超出上限', item['error'])
                self.assertEqual(item['description'], '')

    def test_max_figures_larger_than_available(self):
        chat = StubChat()
        reader = _reader(chat, max_figures=50)
        out = reader.read_all(_fulltext(3))
        self.assertEqual(chat.calls, 3)
        self.assertEqual(len(out), 3)
        for item in out:
            self.assertEqual(item['error'], '')

    def test_max_figures_zero_sends_nothing(self):
        chat = StubChat()
        reader = _reader(chat, max_figures=0)
        out = reader.read_all(_fulltext(4))
        self.assertEqual(chat.calls, 0)
        self.assertEqual(len(out), 4)
        for item in out:
            self.assertIn('超出上限', item['error'])


# ---------------------------------------------------------------------------
# 9. kind 必须是枚举内的值
# ---------------------------------------------------------------------------
class TestKindEnum(unittest.TestCase):
    def test_illegal_kind_is_not_passed_through(self):
        """模型返回 kind: "foo" → 不能原样透出去。"""
        chat = StubChat(content=_ok_json(kind='foo'))
        out = _reader(chat).read_all(_fulltext(1))
        item = out[0]
        self.assertNotEqual(item['kind'], 'foo')
        self.assertIn(item['kind'], figures.FIGURE_KINDS)
        # 两种处理方式任选其一：纠正成 other，或记 error
        self.assertTrue(item['kind'] == 'other' or item['error'])

    def test_illegal_kind_records_error(self):
        chat = StubChat(content=_ok_json(kind='foo'))
        item = _reader(chat).read_all(_fulltext(1))[0]
        self.assertEqual(item['kind'], 'other')
        self.assertTrue(item['error'], '非法 kind 应当留痕')
        self.assertIn('非法', item['error'])

    def test_all_enum_values_pass_through(self):
        for kind in figures.FIGURE_KINDS:
            chat = StubChat(content=_ok_json(kind=kind))
            item = _reader(chat).read_all(_fulltext(1))[0]
            self.assertEqual(item['kind'], kind)
            self.assertEqual(item['error'], '')

    def test_norm_kind_helper(self):
        self.assertEqual(figures._norm_kind('forest_plot'), 'forest_plot')
        self.assertEqual(figures._norm_kind('Forest_Plot'), 'forest_plot')
        self.assertEqual(figures._norm_kind('kaplan-meier'), 'survival_curve')
        self.assertEqual(figures._norm_kind('nonsense'), 'other')
        self.assertEqual(figures._norm_kind(''), 'other')
        self.assertEqual(figures._norm_kind(None), 'other')


# ---------------------------------------------------------------------------
# 10. 提示词本身
# ---------------------------------------------------------------------------
class TestPrompt(unittest.TestCase):
    def test_prompt_is_chinese_and_mentions_constraints(self):
        p = figures.FIGURE_PROMPT
        self.assertIn('中文', p)
        self.assertIn('JSON', p)
        for kind in figures.FIGURE_KINDS:
            self.assertIn(kind, p, '提示词里应列出 %s' % kind)
        # 禁止编造数值的要求必须在
        self.assertIn('禁止', p)
        self.assertIn('图中未见明确标注', p)

    def test_prompt_placeholder_replaced(self):
        chat = StubChat()
        _reader(chat).read_all(_fulltext(1))
        msgs = chat.seen_messages[0]
        content = msgs[0]['content']
        self.assertIsInstance(content, list)
        text = content[0]['text']
        self.assertNotIn('<<CAPTION>>', text)
        self.assertIn('图 1 图注', text)

    def test_image_is_inlined_as_base64_data_url(self):
        """图片必须内联成 data URL，不能把远程地址塞给模型。"""
        chat = StubChat()
        _reader(chat).read_all(_fulltext(1))
        msgs = chat.seen_messages[0]
        content = msgs[0]['content']
        img = content[1]
        self.assertEqual(img['type'], 'image_url')
        url = img['image_url']['url']
        self.assertTrue(url.startswith('data:image/'))
        self.assertIn(';base64,', url)
        self.assertNotIn('http', url)

    def test_caption_context_is_appended(self):
        chat = StubChat()
        _reader(chat).read_all(_fulltext(1), caption='本文研究他汀类药物')
        text = chat.seen_messages[0][0]['content'][0]['text']
        self.assertIn('本文研究他汀类药物', text)


# ---------------------------------------------------------------------------
# 11. 杂项
# ---------------------------------------------------------------------------
class TestMisc(unittest.TestCase):
    def test_relative_href_resolved_against_base(self):
        """相对路径的 href 要按全文 url 拼成绝对地址。"""
        seen = {}

        def dl(url):
            seen['url'] = url
            return PNG_BYTES, 'image/png'

        reader = figures.FigureReader(StubChat(), downloader=dl)
        out = reader.read_all({
            'figures': [{'href': '/pmc/bin/fig1.jpg', 'caption': 'c'}],
            'url': 'https://example.org/article/PMC123',
        })
        self.assertEqual(seen['url'], 'https://example.org/pmc/bin/fig1.jpg')
        self.assertEqual(out[0]['error'], '')

    def test_missing_href_reported(self):
        out = _reader(StubChat()).read_all({
            'figures': [{'href': '', 'caption': 'c'}],
            'url': 'https://example.org/',
        })
        self.assertEqual(len(out), 1)
        self.assertTrue(out[0]['error'])

    def test_chat_fn_without_model_kwarg(self):
        """chat_fn 不接受 model 参数时也不能炸。"""
        def chat(role, messages, temperature=0.2, max_tokens=800):
            return {'content': _ok_json(), 'model': 'default-model'}

        reader = figures.FigureReader(chat, downloader=_fake_downloader())
        out = reader.read_all(_fulltext(1))
        self.assertEqual(out[0]['error'], '')
        self.assertEqual(out[0]['kind'], 'forest_plot')

    def test_chat_fn_returning_plain_string(self):
        def chat(role, messages, **kwargs):
            return _ok_json()

        reader = figures.FigureReader(chat, downloader=_fake_downloader())
        out = reader.read_all(_fulltext(1))
        self.assertEqual(out[0]['kind'], 'forest_plot')
        self.assertEqual(out[0]['error'], '')

    def test_default_model(self):
        self.assertEqual(figures.DEFAULT_VL_MODEL, 'Qwen/Qwen3-VL-8B-Instruct')
        reader = figures.FigureReader(lambda *a, **k: {})
        self.assertEqual(reader.model, 'Qwen/Qwen3-VL-8B-Instruct')

    def test_never_raises_on_weird_input(self):
        """再离谱的输入也不能抛异常——这是本模块的第一原则。"""
        chat = StubChat()
        reader = _reader(chat)
        for ft in ({'figures': None}, {'figures': [None, 123]},
                   {'figures': ['just a caption']}, {'figures': {}},
                   {'figures': [{'href': None}]}):
            out = reader.read_all(ft)
            self.assertIsInstance(out, list)

    def test_timeout_does_not_hang_forever(self):
        """模型卡死时要按预算返回，不能无限等。"""
        def slow_chat(role, messages, **kw):
            time.sleep(30)
            return {'content': _ok_json()}

        reader = figures.FigureReader(slow_chat, downloader=_fake_downloader(),
                                      timeout=3, workers=4, max_figures=4)
        ft = _fulltext(4)
        t0 = time.time()
        out = reader.read_all(ft)
        dt = time.time() - t0
        self.assertEqual(len(out), 4)
        self.assertLess(dt, 25, '卡死时应在预算内返回，实际 %.1fs' % dt)
        for item in out:
            self.assertTrue(item['error'])
            self.assertIn('超时', item['error'])


if __name__ == '__main__':
    unittest.main()
