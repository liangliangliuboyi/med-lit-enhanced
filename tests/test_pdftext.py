# -*- coding: utf-8 -*-
"""`pdftext.py` 的离线单元测试。

全部用例都是**自己构造 PDF 字节**，不联网、不下载任何东西。
"""
from __future__ import annotations

import os
import sys
import unittest
import zlib

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'skills', 'medlit-agent', 'scripts'))

import pdftext  # noqa: E402


def build_pdf(objects, trailer_extra=b'', root=b''):
    """拼一个最小可用的 PDF。

    objects: [(objnum, body_bytes), ...]，body 里放字典和流。
    """
    out = bytearray(b'%PDF-1.4\n')
    for num, body in objects:
        out += b'%d 0 obj\n' % num
        out += body
        out += b'\nendobj\n'
    out += b'trailer\n<< /Size %d ' % (len(objects) + 1)
    out += root
    out += trailer_extra
    out += b' >>\n%%EOF\n'
    return bytes(out)


def stream_obj(content, compress=False, extra=b''):
    """生成一个流对象体（带正确的 /Length）。"""
    if compress:
        data = zlib.compress(content)
        filt = b' /Filter /FlateDecode'
    else:
        data = content
        filt = b''
    head = b'<< /Length %d' % len(data) + filt + extra + b' >>\nstream\n'
    return head + data + b'\nendstream'


class TestIsPdf(unittest.TestCase):
    def test_real_header_is_pdf(self):
        self.assertTrue(pdftext.is_pdf(b'%PDF-1.7\n%\xe2\xe3\xcf\xd3\n'))

    def test_random_bytes_is_not_pdf(self):
        self.assertFalse(pdftext.is_pdf(b'not a pdf at all'))
        self.assertFalse(pdftext.is_pdf(b''))
        self.assertFalse(pdftext.is_pdf(os.urandom(512)))

    def test_non_bytes_is_not_pdf(self):
        self.assertFalse(pdftext.is_pdf('a string'))
        self.assertFalse(pdftext.is_pdf(None))


class TestPlainStream(unittest.TestCase):
    """未压缩的最小内容流。"""

    def _pdf(self, content):
        return build_pdf([(1, stream_obj(content))])

    def test_extracts_hello_world(self):
        pdf = self._pdf(b'BT (Hello World) Tj ET')
        res = pdftext.extract_text(pdf)
        self.assertIn('Hello World', res['text'])
        self.assertEqual(res['method'], 'content_stream')
        self.assertEqual(res['chars'], len(res['text']))

    def test_result_shape(self):
        pdf = self._pdf(b'BT (Hello World) Tj ET')
        res = pdftext.extract_text(pdf)
        for key in ('text', 'pages', 'chars', 'method', 'note'):
            self.assertIn(key, res)
        self.assertIsInstance(res['text'], str)
        self.assertIsInstance(res['pages'], int)

    def test_flate_decode(self):
        pdf = self._pdf(b'BT (Hello World) Tj ET')
        compressed = build_pdf([
            (1, stream_obj(b'BT (Hello World) Tj ET', compress=True))])
        self.assertIn('Hello World', pdftext.extract_text(compressed)['text'])

    def test_string_escapes(self):
        # PDF 字面字符串转义：\( \) \\ \n 以及八进制 \101
        content = (b'BT (A\\(ok\\)B) Tj (back\\\\slash) Tj '
                   b'(new\\nline) Tj (oct\\101) Tj ET')
        text = pdftext.extract_text(self._pdf(content))['text']
        self.assertIn('A(ok)B', text)
        self.assertIn('back\\slash', text)
        self.assertIn('new\nline', text)
        self.assertIn('octA', text)

    def test_tj_array_spacing(self):
        # TJ 数组里 -300 的字间距表示这里要断开 -> 补一个空格
        text = pdftext.extract_text(
            self._pdf(b'BT [(A) -300 (B)] TJ ET'))['text']
        self.assertIn('A B', text)

    def test_tj_small_kerning_keeps_word_together(self):
        # -30 这种小字距是 kerning，不该插入空格
        text = pdftext.extract_text(
            self._pdf(b'BT [(A) -30 (B)] TJ ET'))['text']
        self.assertNotIn('A B', text)

    def test_positive_tj_is_kerning_not_a_space(self):
        # PDF 规范：TJ 里的正数是把后面的字往左挤（kerning），
        # 不是词间空格。LaTeX 排版 "You" 时 Y 与 ou 之间是 +111，
        # 如果按绝对值判断就会得到 "Y ou"。
        text = pdftext.extract_text(
            self._pdf(b'BT [(All)-250(Y)111(ou)-250(Need)] TJ ET'))['text']
        self.assertIn('All You Need', text)
        self.assertNotIn('Y ou', text)

    def test_negative_tj_above_threshold_is_a_space(self):
        text = pdftext.extract_text(
            self._pdf(b'BT [(Attention)-250(Is)-250(All)] TJ ET'))['text']
        self.assertIn('Attention Is All', text)

    def test_quotes_operator(self):
        text = pdftext.extract_text(
            self._pdf(b'BT (line one) \' (line two) \' ET'))['text']
        self.assertIn('line one', text)
        self.assertIn('line two', text)

    def test_hex_string_show(self):
        text = pdftext.extract_text(
            self._pdf(b'BT <48656C6C6F> Tj ET'))['text']
        self.assertIn('Hello', text)


class TestToUnicode(unittest.TestCase):
    """ToUnicode CMap：现代 PDF（含中文文献）的必备能力。"""

    CMAP = (b'/CIDInit /ProcSet findresource begin\n'
            b'12 dict begin\nbegincmap\n'
            b'1 begincodespacerange\n<00> <FF>\nendcodespacerange\n'
            b'2 beginbfchar\n<01> <4E2D>\n<02> <6587>\nendbfchar\n'
            b'endcmap\nend\n')

    def _pdf(self):
        return build_pdf([
            (1, b'<< /Type /Font /Subtype /TrueType /BaseFont /Foo '
                b'/ToUnicode 3 0 R >>'),
            (2, b'<< /Type /Page /Parent 4 0 R /MediaBox [0 0 612 792] '
                b'/Resources << /Font << /F1 1 0 R >> >> /Contents 5 0 R >>'),
            (3, stream_obj(self.CMAP)),
            (4, b'<< /Type /Pages /Kids [2 0 R] /Count 1 >>'),
            (5, stream_obj(b'BT /F1 12 Tf <0102> Tj ET')),
        ])

    def test_bfchar_maps_to_chinese(self):
        res = pdftext.extract_text(self._pdf())
        self.assertIn('中文', res['text'])

    def test_page_count_detected(self):
        res = pdftext.extract_text(self._pdf())
        self.assertEqual(res['pages'], 1)

    def test_cmap_stream_not_mistaken_for_content(self):
        # CMap 里也有 'endstream' 之类的结构，不能被当成正文抽出来
        text = pdftext.extract_text(self._pdf())['text']
        self.assertNotIn('begincmap', text)
        self.assertNotIn('codespacerange', text)

    def test_bfrange(self):
        cmap = (b'begincmap\n'
                b'1 begincodespacerange\n<0000> <FFFF>\nendcodespacerange\n'
                b'1 beginbfrange\n<0020> <0022> <0041>\nendbfrange\n'
                b'endcmap\n')
        pdf = build_pdf([
            (1, b'<< /Type /Font /Subtype /Type0 /BaseFont /Foo '
                b'/ToUnicode 3 0 R >>'),
            (2, b'<< /Type /Page /Parent 4 0 R '
                b'/Resources << /Font << /F1 1 0 R >> >> /Contents 5 0 R >>'),
            (3, stream_obj(cmap)),
            (4, b'<< /Type /Pages /Kids [2 0 R] /Count 1 >>'),
            (5, stream_obj(b'BT /F1 12 Tf <0020> Tj <0021> Tj <0022> Tj ET')),
        ])
        text = pdftext.extract_text(pdf)['text']
        self.assertIn('ABC', text)

    def test_fallback_to_global_cmap_when_page_resources_missing(self):
        # 页面对象损坏（没有 /Resources）：应该退回全局合并的 CMap，
        # 而不是整篇抽不出字。
        pdf = build_pdf([
            (1, b'<< /Type /Font /Subtype /TrueType /BaseFont /Foo '
                b'/ToUnicode 3 0 R >>'),
            (2, b'<< /Type /Page /Contents 5 0 R >>'),
            (3, stream_obj(self.CMAP)),
            (5, stream_obj(b'BT /F1 12 Tf <0102> Tj ET')),
        ])
        text = pdftext.extract_text(pdf)['text']
        self.assertIn('中文', text)

    def test_multiple_cmaps_merged(self):
        cmap_a = (b'begincmap\n1 beginbfchar\n<01> <4E2D>\nendbfchar\n'
                  b'endcmap\n')
        cmap_b = (b'begincmap\n1 beginbfchar\n<02> <6587>\nendbfchar\n'
                  b'endcmap\n')
        pdf = build_pdf([
            (1, b'<< /Type /Font /Subtype /TrueType /BaseFont /A '
                b'/ToUnicode 3 0 R >>'),
            (2, b'<< /Type /Font /Subtype /TrueType /BaseFont /B '
                b'/ToUnicode 4 0 R >>'),
            (3, stream_obj(cmap_a)),
            (4, stream_obj(cmap_b)),
            (5, stream_obj(b'BT /FA 12 Tf <01> Tj /FB 12 Tf <02> Tj ET')),
        ])
        text = pdftext.extract_text(pdf)['text']
        self.assertIn('中', text)
        self.assertIn('文', text)


class TestBrokenInput(unittest.TestCase):
    """坏输入一律不抛异常。"""

    def test_not_a_pdf(self):
        res = pdftext.extract_text(b'not a pdf at all')
        self.assertEqual(res['text'], '')
        self.assertNotEqual(res['note'], '')
        self.assertNotEqual(res['method'], 'error')

    def test_empty_input(self):
        res = pdftext.extract_text(b'')
        self.assertEqual(res['text'], '')
        self.assertNotEqual(res['note'], '')

    def test_truncated_pdf(self):
        pdf = build_pdf([(1, stream_obj(b'BT (Hello World) Tj ET'))])
        res = pdftext.extract_text(pdf[:len(pdf) // 2])
        self.assertIsInstance(res['text'], str)
        self.assertIn(res['method'], ('content_stream', 'none', 'error'))

    def test_truncated_inside_stream(self):
        pdf = build_pdf([(1, stream_obj(b'BT (Hello World) Tj ET'))])
        cut = pdf.index(b'stream') + 20
        res = pdftext.extract_text(pdf[:cut])
        self.assertIsInstance(res['text'], str)

    def test_encrypted_marker(self):
        pdf = build_pdf(
            [(1, stream_obj(b'BT (Hello World) Tj ET'))],
            trailer_extra=b'/Encrypt 2 0 R')
        res = pdftext.extract_text(pdf)
        self.assertIn('加密', res['note'])
        self.assertIn('Encrypt', res['note'])

    def test_encrypted_without_content(self):
        pdf = build_pdf(
            [(1, b'<< /Type /Catalog >>')],
            trailer_extra=b'/Encrypt 2 0 R')
        res = pdftext.extract_text(pdf)
        self.assertIn('加密', res['note'])

    def test_scanned_pdf_without_text_layer(self):
        # 只有图片、没有文本算子：应当是空文本 + 说明，而不是报错
        pdf = build_pdf([(1, stream_obj(b'\x00\x01\x02binaryimage'))])
        res = pdftext.extract_text(pdf)
        self.assertEqual(res['text'], '')
        self.assertEqual(res['method'], 'none')
        self.assertNotEqual(res['note'], '')

    def test_bad_flate_stream_is_skipped(self):
        # /Filter 说 FlateDecode 但数据不是 zlib：跳过并计数，不抛异常
        body = (b'<< /Length 10 /Filter /FlateDecode >>\nstream\n'
                b'notzlib!!\nendstream')
        pdf = build_pdf([(1, body)])
        res = pdftext.extract_text(pdf)
        self.assertIsInstance(res['text'], str)
        self.assertNotEqual(res['method'], 'error')

    def test_non_bytes_input(self):
        res = pdftext.extract_text('not bytes')
        self.assertEqual(res['text'], '')
        self.assertNotEqual(res['note'], '')


class TestMaxChars(unittest.TestCase):
    def test_truncates_to_max_chars(self):
        long_text = b'ABCDEFGHIJ' * 100
        pdf = build_pdf([(1, stream_obj(b'BT (' + long_text + b') Tj ET'))])
        res = pdftext.extract_text(pdf, max_chars=50)
        self.assertLessEqual(len(res['text']), 50)
        self.assertEqual(res['chars'], len(res['text']))
        self.assertIn('截断', res['note'])

    def test_short_text_not_truncated(self):
        pdf = build_pdf([(1, stream_obj(b'BT (short) Tj ET'))])
        res = pdftext.extract_text(pdf, max_chars=1000)
        self.assertIn('short', res['text'])
        self.assertNotIn('截断', res['note'])


class TestPredefinedCMap(unittest.TestCase):
    """预定义 CMap（GBK-EUC-H 之类）：老排版/OCR 生成的中文 PDF。

    这类 PDF 没有 /ToUnicode，字符码本身就是 GBK 字节，
    只能靠 Python 标准库的 gbk codec 解。
    """

    def _pdf(self, encoding=b'GBK-EUC-H'):
        # 「中国科学院」的 GBK 编码
        content = (b'BT /F0 12 Tf 1 0 0 1 100 700 Tm '
                   b'(\xd6\xd0\xb9\xfa\xbf\xc6\xd1\xa7\xd4\xba) Tj ET')
        return build_pdf([
            (1, b'<< /Type /Font /Subtype /Type0 '
                b'/BaseFont /STSong-Light-' + encoding +
                b' /Encoding /' + encoding + b' >>'),
            (2, b'<< /Type /Page /Parent 3 0 R /Resources '
                b'<< /Font << /F0 1 0 R >> >> /Contents 4 0 R >>'),
            (3, b'<< /Type /Pages /Kids [2 0 R] /Count 1 >>'),
            (4, stream_obj(content)),
        ])

    def test_gbk_decodes_to_chinese(self):
        text = pdftext.extract_text(self._pdf())['text']
        self.assertIn('中国科学院', text)

    def test_no_space_between_cjk_glyphs(self):
        # 中日韩排版词间没有空格；OCR 出来中文 PDF 常常给每个字
        # 单独一个 Tm 定位，不能因为横向挪位就补空格。
        content = (b'BT /F0 12 Tf 1 0 0 1 100 700 Tm (\xd6\xd0) Tj '
                   b'1 0 0 1 112 700 Tm (\xb9\xfa) Tj ET')
        pdf = build_pdf([
            (1, b'<< /Type /Font /Subtype /Type0 /BaseFont /STSong-Light '
                b'/Encoding /GBK-EUC-H >>'),
            (2, b'<< /Type /Page /Resources << /Font << /F0 1 0 R >> >> '
                b'/Contents 3 0 R >>'),
            (3, stream_obj(content)),
        ])
        text = pdftext.extract_text(pdf)['text']
        self.assertIn('中国', text)
        self.assertNotIn('中 国', text)

    def test_latin_in_cjk_font_still_decodes(self):
        content = b'BT /F0 12 Tf (Hello) Tj ET'
        pdf = build_pdf([
            (1, b'<< /Type /Font /Subtype /Type0 /BaseFont /STSong-Light '
                b'/Encoding /GBK-EUC-H >>'),
            (2, b'<< /Type /Page /Resources << /Font << /F0 1 0 R >> >> '
                b'/Contents 3 0 R >>'),
            (3, stream_obj(content)),
        ])
        self.assertIn('Hello', pdftext.extract_text(pdf)['text'])


class TestObjectStream(unittest.TestCase):
    """PDF 1.5+ 的对象流（/Type /ObjStm）。

    大量对象（字体字典、页面对象）被压缩进对象流，文件里看不到
    `N 0 obj`。不解开就等于这些对象不存在。
    """

    def _build_with_objstm(self, extra_objs):
        """把 extra_objs 放进一个 ObjStm，返回整个 PDF 字节。"""
        members = b''
        header = b''
        offsets = []
        for num, body in extra_objs:
            offsets.append((num, len(members)))
            members += body + b' '
        for num, off in offsets:
            header += b'%d %d ' % (num, off)
        payload = header + members
        first = len(header)
        out = bytearray(b'%PDF-1.5\n')
        out += b'1 0 obj\n' + (b'<< /Type /ObjStm /N %d /First %d /Length %d >>\n'
                               % (len(extra_objs), first, len(payload)))
        out += b'stream\n' + payload + b'\nendstream\nendobj\n'
        out += b'trailer\n<< /Size 2 >>\n%%EOF\n'
        return bytes(out)

    def test_object_in_objstm_is_found(self):
        pdf = self._build_with_objstm([
            (7, b'<< /Type /Font /Subtype /TrueType /BaseFont /Foo >>'),
        ])
        objs = pdftext._scan_objects(pdf)
        by_num = {o.num: o for o in objs}
        pdftext._expand_object_streams(pdf, objs, by_num)
        self.assertIn(7, by_num)
        self.assertEqual(by_num[7].dict.get('Type'), ('name', 'Font'))

    def test_font_inside_objstm_is_used(self):
        # 页面资源指向的字体对象藏在对象流里：必须能拿到它的 ToUnicode
        cmap = (b'begincmap\n1 beginbfchar\n<01> <4E2D>\n<02> <6587>\n'
                b'endbfchar\nendcmap\n')
        # ObjStm 成员不能是流（PDF 规定对象流里不含流），
        # 所以 CMap 用普通流对象放在外面。
        out = bytearray(b'%PDF-1.5\n')
        out += b'1 0 obj\n' + stream_obj(cmap) + b'\nendobj\n'
        out += b'2 0 obj\n' + stream_obj(
            b'BT /F1 12 Tf <0102> Tj ET') + b'\nendobj\n'
        members = [
            (3, b'<< /Type /Page /Parent 4 0 R /Resources '
                b'<< /Font << /F1 5 0 R >> >> /Contents 2 0 R >>'),
            (4, b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>'),
            (5, b'<< /Type /Font /Subtype /TrueType /BaseFont /Foo '
                b'/ToUnicode 1 0 R >>'),
        ]
        payload = b''
        header = b''
        for num, body in members:
            header += b'%d %d ' % (num, len(payload))
            payload += body + b' '
        first = len(header)
        blob = header + payload
        out += b'6 0 obj\n<< /Type /ObjStm /N %d /First %d /Length %d >>\n' \
               % (len(members), first, len(blob))
        out += b'stream\n' + blob + b'\nendstream\nendobj\n'
        out += b'trailer\n<< /Size 7 >>\n%%EOF\n'
        res = pdftext.extract_text(bytes(out))
        self.assertIn('中文', res['text'])
        self.assertEqual(res['pages'], 1)


class TestFontIsolation(unittest.TestCase):
    """不同字体的字符码不能互相污染。"""

    def test_font_without_tounicode_uses_latin1_not_other_cmap(self):
        # 两个子集字体把同一个字符码映射成不同的字；
        # 没有 ToUnicode 的那个字体必须按 latin-1 解，
        # 不能套用别的字体的映射表。
        cmap_a = b'begincmap\n1 beginbfchar\n<41> <4E2D>\nendbfchar\nendcmap\n'
        pdf = build_pdf([
            (1, b'<< /Type /Font /Subtype /TrueType /BaseFont /A '
                b'/ToUnicode 3 0 R >>'),
            (2, b'<< /Type /Font /Subtype /TrueType /BaseFont /B '
                b'/Encoding /WinAnsiEncoding >>'),
            (3, stream_obj(cmap_a)),
            (4, b'<< /Type /Page /Resources << /Font << /FA 1 0 R /FB 2 0 R >> >> '
                b'/Contents 5 0 R >>'),
            (5, stream_obj(b'BT /FA 12 Tf (A) Tj /FB 12 Tf (A) Tj ET')),
        ])
        text = pdftext.extract_text(pdf)['text']
        self.assertIn('中', text)
        # 第二个 A 用的是 WinAnsi 字体，应该是字母 A 而不是「中」
        self.assertIn('A', text)
        self.assertEqual(text.count('中'), 1)


class TestRealWorldShapes(unittest.TestCase):
    """接近真实排版的结构。"""

    def test_multi_page_ordering(self):
        pdf = build_pdf([
            (1, b'<< /Type /Catalog /Pages 6 0 R >>'),
            (2, b'<< /Type /Page /Parent 6 0 R /Resources << /Font '
                b'<< /F1 5 0 R >> >> /Contents 3 0 R >>'),
            (3, stream_obj(b'BT /F1 12 Tf (PageOne) Tj ET')),
            (4, b'<< /Type /Page /Parent 6 0 R /Resources << /Font '
                b'<< /F1 5 0 R >> >> /Contents 7 0 R >>'),
            (5, b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>'),
            (6, b'<< /Type /Pages /Kids [2 0 R 4 0 R] /Count 2 >>'),
            (7, stream_obj(b'BT /F1 12 Tf (PageTwo) Tj ET')),
        ])
        res = pdftext.extract_text(pdf)
        self.assertEqual(res['pages'], 2)
        self.assertIn('PageOne', res['text'])
        self.assertIn('PageTwo', res['text'])
        self.assertLess(res['text'].index('PageOne'),
                        res['text'].index('PageTwo'))

    def test_incremental_update_uses_latest_object(self):
        # 同一个 objnum 出现两次（增量更新），应取最后一次
        out = bytearray(build_pdf([
            (1, b'<< /Type /Catalog /Pages 2 0 R >>'),
            (2, b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>'),
            (3, stream_obj(b'BT (OLD) Tj ET')),
        ]))
        out += b'3 0 obj\n' + stream_obj(b'BT (NEW) Tj ET') + b'\nendobj\n'
        res = pdftext.extract_text(bytes(out))
        self.assertIn('NEW', res['text'])
        self.assertNotIn('OLD', res['text'])

    def test_ascii85_filter(self):
        import base64 as _b64
        content = b'BT (Hello World) Tj ET'
        data = _b64.a85encode(content) + b'~>'
        body = (b'<< /Length %d /Filter /ASCII85Decode >>\nstream\n'
                % len(data)) + data + b'\nendstream'
        pdf = build_pdf([(1, body)])
        res = pdftext.extract_text(pdf)
        self.assertIn('Hello World', res['text'])

    def test_latin1_high_bytes(self):
        # 老文档常见的 WinAnsi/Latin-1 编码：字节直接按 latin-1 解
        pdf = build_pdf([(1, stream_obj(b'BT (caf\xe9 r\xe9sum\xe9) Tj ET'))])
        text = pdftext.extract_text(pdf)['text']
        self.assertIn('caf\xe9', text)

    def test_td_vertical_move_makes_newline(self):
        content = b'BT 10 700 Td (line1) Tj 0 -14 Td (line2) Tj ET'
        text = pdftext.extract_text(
            build_pdf([(1, stream_obj(content))]))['text']
        self.assertIn('line1', text)
        self.assertIn('line2', text)
        self.assertLess(text.index('line1'), text.index('line2'))

    def test_xobject_image_is_not_extracted(self):
        pdf = build_pdf([
            (1, b'<< /Type /XObject /Subtype /Image /Width 10 /Height 10 '
                b'/Length 6 >>\nstream\n\x00\x01\x02\x03\x04\x05\nendstream'),
            (2, stream_obj(b'BT (RealText) Tj ET')),
        ])
        text = pdftext.extract_text(pdf)['text']
        self.assertIn('RealText', text)


if __name__ == '__main__':
    unittest.main()
