# -*- coding: utf-8 -*-
"""PDF 文本抽取：纯标准库实现的最小 PDF 解析器。

背景
----
`fulltext.py` 能下载 OA 论文的 PDF，但本项目零第三方依赖，装不了
pdfplumber / pypdf / PyMuPDF，于是「只拿到 PDF」时全文长度为 0，
用户读不到正文——而循证场景里正文恰恰是核心。

本模块补上这一段：从 PDF 字节里抽出纯文本，供 pipeline 做全文精读。

只做「最小可用集合」的实现取舍
------------------------------
1. **扫 obj 标记，不解析 xref 表**。
   PDF 的交叉引用表（xref）经常是坏的：线性化 PDF、增量更新（同一个
   objnum 在文件里出现多次）、交叉引用流（/Type /XRef）、以及被人手改
   过的文件，都会让 xref 偏移量对不上。反过来，「文件里出现
   `N G obj` 就说明这里有一个间接对象」是**自洽**的：即使 xref 全坏，
   也能把对象全找出来。所以这里用正则扫全文切对象，容错性远好于解析
   xref（增量更新时取最后一次出现的版本，正好也是正确版本）。

2. **必须支持 ToUnicode CMap**。
   PDF 里的字符串字节**不等于** Unicode：现代 PDF（尤其是中文文献，
   以及 LaTeX / Word 生成的 Type0 字体）用的是 CID 编码，字节本身没有
   文本含义，全靠字体对象里的 /ToUnicode CMap 才能还原成字符。
   不解析 CMap，中文文献抽出来就是一堆乱码（本任务的核心验收点）。

3. **找不到「页面-字体」精确对应时，退回全局合并 CMap**。
   理想做法是按页面 /Resources → /Font 精确绑定字体；现实里 PDF 常年
   把资源继承到 /Pages 节点、字体名在不同页复用、页面对象本身损坏。
   此时**退化**成「把文件里所有 CMap 合并成一张全局表」，对「同一篇
   论文字体种类不多」的场景正确率不低，而且绝不会因为找不到页面资源
   而整篇失败。

4. **不追求 100% 完美**：不还原版式（位置、字号、旋转），不处理 Type3
   字体位图，不做 OCR（扫描件就是没有文本层）。能读出可读的正文就够。

5. **任何异常都不外泄**：解压失败、对象损坏、加密文件全部收进 note，
   让上层继续跑，而不是让整条 pipeline 挂掉。

零第三方依赖（只用标准库）。Python 3.9+ 兼容。
"""
from __future__ import annotations

import base64
import re
import sys
import time
import zlib

# PDF 魔数
PDF_MAGIC = b'%PDF-'

# TJ 数组里的字间距阈值（单位：1/1000 em）。
# PDF 的 TJ 数组里夹着「字距调整量」：负数表示把后面的字往左拉。
# 经验值：正常的字距微调在 -10 ~ -80 之间（西文 kerning），
# 而真正的词间空隙一般在 -250 ~ -600。取 100 作为分界：
# 大于 100 就当成「这里断词了」，补一个空格；小于 100 认为是 kerning，
# 什么都不加。这个值偏小（宁可多补空格，也不要把两个词粘成一个词）。
TJ_SPACE_THRESHOLD = 100

# 单个流解压后的最大字节数：防御畸形 PDF 里的压缩炸弹
MAX_STREAM_BYTES = 8 * 1024 * 1024

# 「探测型」内容流的最大个数：只有当页面 /Contents 解析不出来时才会用到
MAX_PROBE_STREAMS = 64

# 空白字符集（PDF 规定为 0x00 0x09 0x0A 0x0C 0x0D 0x20）
_WS = frozenset(b'\x00\x09\x0a\x0c\x0d\x20')
# 分隔符
_DELIM = frozenset(b'()<>[]{}/%')
# 数字可能出现的字符
_NUMCHARS = frozenset(b'0123456789+-.')
# 数字与运算符之间的分界（用于切出裸操作符）
_OPCHARS = frozenset(b'0123456789abcdefghijklmnopqrstuvwxyz'
                     b'ABCDEFGHIJKLMNOPQRSTUVWXYZ*\'"')

# 间接对象头：`N G obj`
_OBJ_RE = re.compile(rb'(?<![0-9])(\d{1,10})[\x00\t\n\x0c\r ]+(\d{1,5})'
                     rb'[\x00\t\n\x0c\r ]+obj(?![A-Za-z0-9])')
# 流关键字（排除 endstream）
_STREAM_RE = re.compile(rb'(?<![A-Za-z0-9])stream(?![A-Za-z0-9])')
# 内容流里是否出现过文本块的粗判
_BT_RE = re.compile(rb'(?<![A-Za-z0-9])BT(?![A-Za-z0-9])')
_SHOW_RE = re.compile(rb'(?<![A-Za-z0-9])(?:Tj|TJ)(?![A-Za-z0-9])')
# CMap 段落
_BFCHAR_RE = re.compile(rb'beginbfchar(.*?)endbfchar', re.S)
_BFRANGE_RE = re.compile(rb'beginbfrange(.*?)endbfrange', re.S)


def is_pdf(data) -> bool:
    """判断字节是不是 PDF（看 %PDF- 魔数）。

    少数 PDF 会在文件头前面带一点垃圾字节（甚至 BOM），所以在前 1KB 内
    找一次魔数就够了；随机字节几乎不可能命中。
    """
    if isinstance(data, (bytearray, memoryview)):
        data = bytes(data)
    if not isinstance(data, bytes):
        return False
    if data[:5] == PDF_MAGIC:
        return True
    return PDF_MAGIC in data[:1024]


def extract_text(pdf_bytes: bytes, max_chars: int = 200000) -> dict:
    """从一个 PDF 的字节里抽文本。

    返回:
    {
      'text': str,        # 抽取到的文本（可能为空）
      'pages': int,       # 解析出的页数（尽力而为；解析不出来就是 0）
      'chars': int,       # 文本长度
      'method': str,      # 'content_stream' | 'none' | 'error'
      'note': str,        # 失败原因/说明（成功则可能为空串）
    }

    任何异常都不外泄——全部收进 note。
    """
    try:
        return _extract_text(pdf_bytes, max_chars)
    except Exception as exc:  # noqa: BLE001 —— 兜底，绝不让异常外泄
        return _result('', 0, 'error',
                       'PDF 解析异常：%s: %s' % (type(exc).__name__, exc))


# ----------------------------------------------------------------------
# 内部实现
# ----------------------------------------------------------------------

def _result(text: str, pages: int, method: str, note: str) -> dict:
    return {'text': text, 'pages': pages, 'chars': len(text),
            'method': method, 'note': note}


def _extract_text(pdf_bytes, max_chars) -> dict:
    if isinstance(pdf_bytes, (bytearray, memoryview)):
        pdf_bytes = bytes(pdf_bytes)
    if not isinstance(pdf_bytes, bytes):
        return _result('', 0, 'none', '输入不是 bytes 类型，无法解析 PDF')
    if not pdf_bytes:
        return _result('', 0, 'none', '输入为空，没有可解析的内容')
    if not is_pdf(pdf_bytes):
        return _result('', 0, 'none', '不是 PDF 文件：缺少 %PDF- 魔数')

    # /Encrypt 只出现在 trailer 里；加密 PDF 的内容流本身是密文，
    # 我们没有口令，只能识别出「这是加密文件」并如实说明。
    encrypted = b'/Encrypt' in pdf_bytes

    objects = _scan_objects(pdf_bytes)
    if not objects:
        return _result('', 0, 'none', 'PDF 结构异常：没有找到任何间接对象')

    # 同一个 objnum 可能出现多次（增量更新），后面的版本才是当前生效的版本
    by_num = {}
    for obj in objects:
        by_num[obj.num] = obj
    uniq = [by_num[n] for n in sorted(by_num)]

    pages = _count_pages(uniq)
    global_cmap, page_fonts, page_contents = _build_font_maps(uniq, by_num)

    chunks = []
    skipped = 0
    probed = 0
    for obj in uniq:
        if not obj.has_stream:
            continue
        fonts = None
        if obj.num in page_contents:
            fonts = page_fonts.get(obj.num)
        else:
            # 页面 /Contents 没解析出来时的兜底：解码后看看是不是内容流
            if probed >= MAX_PROBE_STREAMS or not _probe_candidate(obj):
                continue
            decoded = _decode_stream(obj)
            if decoded is None:
                skipped += 1
                continue
            if not (_BT_RE.search(decoded) and _SHOW_RE.search(decoded)):
                continue
            probed += 1
        decoded = _decode_stream(obj)
        if decoded is None:
            skipped += 1
            continue
        text = _extract_content_text(decoded, fonts, global_cmap)
        if text.strip():
            chunks.append(text)

    if not chunks:
        if encrypted:
            note = ('PDF 含 /Encrypt 加密标记，内容流需要口令才能解密，'
                    '本次未取到文本层（可能是扫描件）')
        else:
            note = '内容流里没有文本算子，可能是扫描件（无文本层）'
        return _result('', pages, 'none', note)

    text = _tidy('\n\n'.join(chunks))
    truncated = False
    if isinstance(max_chars, int) and max_chars >= 0 and len(text) > max_chars:
        text = text[:max_chars]
        truncated = True

    notes = []
    if skipped:
        notes.append('有 %d 个流解压失败，已跳过' % skipped)
    if probed:
        notes.append('有 %d 个内容流靠探测识别' % probed)
    if encrypted:
        notes.append('PDF 含 /Encrypt 加密标记，部分内容可能未解出')
    if truncated:
        notes.append('文本超过 %d 字，已截断' % max_chars)
    return _result(text, pages, 'content_stream', '；'.join(notes))


# ----------------------------------------------------------------------
# 对象扫描
# ----------------------------------------------------------------------

class _Obj:
    """一个间接对象：字典部分 + 可选的流数据。"""

    __slots__ = ('num', 'gen', 'end', 'dict', 'stream_raw', 'has_stream')

    def __init__(self, num, gen, end, dct, stream_raw, has_stream):
        self.num = num
        self.gen = gen
        self.end = end
        self.dict = dct
        self.stream_raw = stream_raw
        self.has_stream = has_stream


def _scan_objects(data: bytes):
    """扫全文找 `N G obj`，把每个对象切出来。

    为什么不用 xref 表：xref 的偏移量在增量更新、线性化 PDF、交叉引用流
    里都容易对不上，而「出现 obj 标记就是一个对象」永远成立。
    """
    objs = []
    pos = 0
    n = len(data)
    while pos < n:
        m = _OBJ_RE.search(data, pos)
        if not m:
            break
        obj = _parse_object(data, m.start(), int(m.group(1)), int(m.group(2)),
                            m.end())
        if obj is None:
            pos = m.end()
            continue
        objs.append(obj)
        # 跳过整个对象：既避免把流里的二进制误认成新对象头，
        # 也让 objnum 顺序天然接近阅读顺序。
        pos = obj.end if obj.end > m.end() else m.end()
    return objs


def _parse_object(data: bytes, start: int, num: int, gen: int, body_start: int):
    """切出 [body_start, end) 这段对象体，并解析出字典与流数据。"""
    end_obj = data.find(b'endobj', body_start)
    sm = _STREAM_RE.search(data, body_start)
    has_stream = bool(sm) and (end_obj < 0 or sm.start() < end_obj)

    if not has_stream:
        body_end = end_obj if end_obj >= 0 else len(data)
        dct = _parse_dict(data[body_start:body_end])
        obj_end = (end_obj + 6) if end_obj >= 0 else len(data)
        return _Obj(num, gen, obj_end, dct, b'', False)

    s_pos = sm.start()
    dct = _parse_dict(data[body_start:s_pos])
    # 'stream' 关键字后必须跟一个 EOL
    data_start = s_pos + len(b'stream')
    if data[data_start:data_start + 2] == b'\r\n':
        data_start += 2
    elif data[data_start:data_start + 1] in (b'\n', b'\r'):
        data_start += 1

    end_stream = data.find(b'endstream', data_start)
    length = dct.get('/Length')
    raw = None
    data_end = -1
    # 优先用 /Length 定位：流数据里偶尔会出现 'endstream' 字样，
    # 按长度切才不会被误导。
    if length is not None and length[0] == 'num':
        cand = data_start + int(length[1])
        if 0 <= int(length[1]) <= len(data) - data_start:
            tail = data[cand:cand + 24]
            if (tail.startswith(b'\r\n') or tail.startswith(b'\n')
                    or tail.startswith(b'\r')
                    or tail.lstrip()[:9] == b'endstream'
                    or b'endstream' in tail):
                data_end = cand
    if data_end < 0:
        if end_stream < 0:
            data_end = len(data)          # 文件被截断
        else:
            data_end = end_stream
            # 'endstream' 前的 EOL 不算数据的一部分
            if data[data_end - 2:data_end] == b'\r\n':
                data_end -= 2
            elif data[data_end - 1:data_end] in (b'\n', b'\r'):
                data_end -= 1
    raw = data[data_start:max(data_start, data_end)]

    if end_stream < 0:
        obj_end = len(data)
    else:
        after = data.find(b'endobj', end_stream)
        obj_end = (after + 6) if after >= 0 else len(data)
    return _Obj(num, gen, obj_end, dct, raw, True)


# ----------------------------------------------------------------------
# 字典 / 数组 / 记号
# ----------------------------------------------------------------------

def _tokens(data: bytes):
    """把一段 PDF 数据切成记号序列。

    记号类型：'name' / 'num' / 'str' / 'hex' / 'arr_open' / 'arr_close' /
    'dict_open' / 'dict_close' / 'op'。
    """
    i = 0
    n = len(data)
    while i < n:
        c = data[i]
        if c in _WS:
            i += 1
            continue
        if c == 0x25:  # '%' 注释
            j = data.find(b'\n', i)
            i = n if j < 0 else j + 1
            continue
        if c == 0x28:  # '(' 字面字符串
            s, i = _read_literal(data, i)
            yield ('str', s)
            continue
        if c == 0x3c:  # '<'
            if data[i:i + 2] == b'<<':
                yield ('dict_open', '')
                i += 2
                continue
            j = data.find(b'>', i)
            if j < 0:
                yield ('hex', data[i + 1:].decode('ascii', 'ignore'))
                i = n
                continue
            yield ('hex', data[i + 1:j].decode('ascii', 'ignore'))
            i = j + 1
            continue
        if c == 0x3e:  # '>'
            if data[i:i + 2] == b'>>':
                yield ('dict_close', '')
                i += 2
            else:
                i += 1
            continue
        if c == 0x5b:  # '['
            yield ('arr_open', '')
            i += 1
            continue
        if c == 0x5d:  # ']'
            yield ('arr_close', '')
            i += 1
            continue
        if c in (0x7b, 0x7d):  # '{' '}'（PDF 1.2 的 PostScript 函数体）
            i += 1
            continue
        if c == 0x2f:  # '/' 名字
            j = i + 1
            while j < n and data[j] not in _WS and data[j] not in _DELIM:
                j += 1
            yield ('name', data[i + 1:j].decode('latin-1'))
            i = j
            continue
        if c in (0x30, 0x31, 0x32, 0x33, 0x34, 0x35, 0x36, 0x37, 0x38, 0x39,
                 0x2b, 0x2d, 0x2e):  # 数字
            j = i
            while j < n and data[j] in _NUMCHARS:
                j += 1
            if j == i:
                j = i + 1
            try:
                yield ('num', float(data[i:j]))
            except ValueError:
                pass
            i = j
            continue
        # 裸操作符
        j = i
        while j < n and data[j] not in _WS and data[j] not in _DELIM \
                and data[j] in _OPCHARS:
            j += 1
        if j == i:
            j = i + 1
        op = data[i:j].decode('latin-1')
        yield ('op', op)
        i = j
        if op == 'BI':
            # 内联图片（BI ... ID <二进制> EI）：二进制里可能混着 '(' 之类
            # 的字符，会被误当成字符串。直接跳到 EI 之后更安全。
            j = data.find(b'EI', i)
            if j < 0:
                i = n
            else:
                i = j + 2


def _read_literal(data: bytes, i: int):
    """读一个 `(...)` 字面字符串，返回 (字节内容, 新位置)。"""
    depth = 1
    i += 1
    out = bytearray()
    n = len(data)
    while i < n:
        c = data[i]
        if c == 0x5c:  # 反斜杠转义
            i += 1
            if i >= n:
                break
            e = data[i]
            if e == 0x6e:      # \n
                out.append(0x0a)
                i += 1
            elif e == 0x72:    # \r
                out.append(0x0d)
                i += 1
            elif e == 0x74:    # \t
                out.append(0x09)
                i += 1
            elif e == 0x62:    # \b
                out.append(0x08)
                i += 1
            elif e == 0x66:    # \f
                out.append(0x0c)
                i += 1
            elif 0x30 <= e <= 0x37:  # \ddd 八进制
                val = 0
                cnt = 0
                while i < n and cnt < 3 and 0x30 <= data[i] <= 0x37:
                    val = val * 8 + (data[i] - 0x30)
                    i += 1
                    cnt += 1
                out.append(val & 0xFF)
            elif e == 0x0d:     # 续行
                i += 1
                if i < n and data[i] == 0x0a:
                    i += 1
            elif e == 0x0a:
                i += 1
            else:              # \( \) \\ 等：转义符本身
                out.append(e)
                i += 1
            continue
        if c == 0x28:  # '('
            depth += 1
            out.append(c)
            i += 1
            continue
        if c == 0x29:  # ')'
            depth -= 1
            i += 1
            if depth == 0:
                break
            out.append(c)
            continue
        if c == 0x0d:  # 字符串里的裸换行：PDF 规定 CRLF / CR 都算 LF
            out.append(0x0a)
            i += 1
            if i < n and data[i] == 0x0a:
                i += 1
            continue
        out.append(c)
        i += 1
    return bytes(out), i


def _parse_dict(raw: bytes) -> dict:
    """解析 PDF 字典，返回 {key: value}。

    value 统一表示成元组：('name', str) / ('num', float) / ('ref', int) /
    ('dict', dict) / ('arr', list) / ('str', bytes) / ('hex', str)。
    """
    toks = list(_tokens(raw))
    if not toks or toks[0][0] != 'dict_open':
        return {}
    d, _ = _parse_dict_tokens(toks, 0)
    return d


def _parse_dict_tokens(toks, i):
    """从 toks[i]（dict_open）开始解析字典，返回 (dict, 下一个位置)。"""
    d = {}
    i += 1
    n = len(toks)
    while i < n:
        kind, val = toks[i]
        if kind == 'dict_close':
            return d, i + 1
        if kind != 'name':
            i += 1
            continue
        key = val
        i += 1
        if i >= n:
            break
        k2, v2 = toks[i]
        if k2 == 'name':
            d[key] = ('name', v2)
            i += 1
        elif k2 == 'num':
            # `12 0 R` 是间接引用
            if i + 2 < n and toks[i + 1][0] == 'num' \
                    and toks[i + 2][0] == 'op' and toks[i + 2][1] == 'R':
                d[key] = ('ref', int(v2))
                i += 3
            else:
                d[key] = ('num', v2)
                i += 1
        elif k2 == 'str':
            d[key] = ('str', v2)
            i += 1
        elif k2 == 'hex':
            d[key] = ('hex', v2)
            i += 1
        elif k2 == 'dict_open':
            sub, i = _parse_dict_tokens(toks, i)
            d[key] = ('dict', sub)
        elif k2 == 'arr_open':
            arr, i = _parse_array_tokens(toks, i)
            d[key] = ('arr', arr)
        else:
            i += 1
    return d, i


def _parse_array_tokens(toks, i):
    """从 toks[i]（arr_open）开始解析数组，返回 (list, 下一个位置)。"""
    arr = []
    i += 1
    n = len(toks)
    while i < n:
        kind, val = toks[i]
        if kind == 'arr_close':
            return arr, i + 1
        if kind == 'num':
            if i + 2 < n and toks[i + 1][0] == 'num' \
                    and toks[i + 2][0] == 'op' and toks[i + 2][1] == 'R':
                arr.append(('ref', int(val)))
                i += 3
                continue
            arr.append(('num', val))
            i += 1
            continue
        if kind in ('name', 'str', 'hex'):
            arr.append((kind, val))
            i += 1
            continue
        if kind == 'dict_open':
            sub, i = _parse_dict_tokens(toks, i)
            arr.append(('dict', sub))
            continue
        if kind == 'arr_open':
            sub, i = _parse_array_tokens(toks, i)
            arr.append(('arr', sub))
            continue
        i += 1
    return arr, i


# ----------------------------------------------------------------------
# 流解码
# ----------------------------------------------------------------------

def _decode_stream(obj: _Obj):
    """按 /Filter 链解码一个流；失败返回 None（调用方负责跳过并计数）。"""
    try:
        filters = _filters_of(obj.dict)
        data = obj.stream_raw
        for f in filters:
            if f == 'FlateDecode':
                data = _inflate(data)
            elif f == 'ASCII85Decode':
                data = _ascii85(data)
            elif f == 'ASCIIHexDecode':
                data = _asciihex(data)
            elif f == 'RunLengthDecode':
                data = _runlength(data)
            else:
                # DCTDecode / JPXDecode / CCITTFaxDecode 等是图像或未实现的
                # 滤镜：内容流不会用它们，直接放弃这个流。
                return None
            if data is None:
                return None
        return data
    except Exception:  # noqa: BLE001 —— 解码失败只是「跳过这个流」
        return None


def _filters_of(dct: dict):
    """从字典里取出 /Filter 滤镜名列表（按应用顺序）。"""
    val = dct.get('/Filter')
    if val is None:
        return []
    if val[0] == 'name':
        return [val[1]]
    if val[0] == 'arr':
        out = []
        for item in val[1]:
            if item[0] == 'name':
                out.append(item[1])
        return out
    return []


def _inflate(data: bytes):
    """zlib 解压；兼容截断流与缺 zlib 头的裸 deflate。"""
    if not data:
        return b''
    try:
        return zlib.decompress(data)
    except zlib.error:
        pass
    try:
        d = zlib.decompressobj()
        out = d.decompress(data, MAX_STREAM_BYTES)
        try:
            out += d.flush()
        except zlib.error:
            pass
        if out:
            return out
    except zlib.error:
        pass
    try:
        return zlib.decompress(data, -15)
    except zlib.error:
        return None


def _ascii85(data: bytes):
    """ASCII85Decode：LaTeX / distiller 生成的 PDF 非常常见。"""
    if not data:
        return b''
    body = data.strip()
    if body.startswith(b'<~'):
        body = body[2:]
    if body.endswith(b'~>'):
        body = body[:-2]
    try:
        return base64.a85decode(body, adobe=False)
    except Exception:  # noqa: BLE001
        return None


def _asciihex(data: bytes):
    """ASCIIHexDecode：'>' 之前是十六进制文本。"""
    body = data.split(b'>')[0]
    cleaned = re.sub(rb'[^0-9A-Fa-f]', b'', body)
    if len(cleaned) % 2:
        cleaned += b'0'
    try:
        return bytes.fromhex(cleaned.decode('ascii'))
    except ValueError:
        return None


def _runlength(data: bytes):
    """RunLengthDecode：长度字节 + 重复/字面量。"""
    out = bytearray()
    i = 0
    n = len(data)
    while i < n:
        ln = data[i]
        i += 1
        if ln == 128:
            break
        if ln < 128:
            out.extend(data[i:i + ln + 1])
            i += ln + 1
        else:
            if i < n:
                out.extend(bytes([data[i]]) * (257 - ln))
                i += 1
    return bytes(out)


def _probe_candidate(obj: _Obj) -> bool:
    """判断一个流「有可能」是内容流（避免把字体/图片也解开）。"""
    dct = obj.dict
    t = dct.get('/Type')
    if t is not None and t[0] == 'name' and t[1] in (
            'Font', 'FontDescriptor', 'XObject', 'Metadata', 'Catalog',
            'Pages', 'Page', 'Annot', 'Filespec'):
        return False
    st = dct.get('/Subtype')
    if st is not None and st[0] == 'name' and st[1] in (
            'Image', 'Type1C', 'CIDFontType0C', 'Type1', 'TrueType',
            'OpenType', 'CIDFontType0', 'CIDFontType2', 'Form', 'PDF'):
        return False
    if any(k in dct for k in ('/Length1', '/Length2', '/Length3',
                              '/FontFile', '/FontFile2', '/FontFile3')):
        return False
    if obj.stream_raw and len(obj.stream_raw) > 4 * 1024 * 1024:
        return False
    return True


# ----------------------------------------------------------------------
# 页数、字体与 CMap
# ----------------------------------------------------------------------

def _count_pages(objects) -> int:
    """页数：数 /Type /Page 对象，并和 /Type /Pages 的 /Count 取大者。"""
    pages = 0
    tree_count = 0
    for obj in objects:
        t = obj.dict.get('/Type')
        if t is None or t[0] != 'name':
            continue
        if t[1] == 'Page':
            pages += 1
        elif t[1] == 'Pages':
            c = obj.dict.get('/Count')
            if c is not None and c[0] == 'num':
                tree_count = max(tree_count, int(c[1]))
    return max(pages, tree_count)


def _resolve_dict(val, by_num):
    """把 ('dict', d) 或 ('ref', n) 统一解析成字典。"""
    if val is None:
        return None
    if val[0] == 'dict':
        return val[1]
    if val[0] == 'ref':
        obj = by_num.get(val[1])
        if obj is None:
            return None
        return obj.dict
    return None


def _build_font_maps(objects, by_num):
    """建立 CMap 表。

    返回 (global_cmap, page_fonts, page_contents)：
      - global_cmap：文件里所有 ToUnicode CMap 合并后的全局表（兜底用）
      - page_fonts：内容流 objnum -> {字体名: (cmap, 每字符字节数)}
      - page_contents：页面 /Contents 引用到的内容流 objnum 集合
    """
    global_cmap = {}
    # 先把所有字体对象的 /ToUnicode 收进全局表（后出现的覆盖先出现的）
    for obj in objects:
        dct = obj.dict
        tu = dct.get('/ToUnicode')
        if tu is None:
            continue
        cmap = _cmap_of_ref(tu, by_num)
        if cmap:
            global_cmap.update(cmap)

    page_fonts = {}
    page_contents = set()
    for obj in objects:
        if not _is_page(obj):
            continue
        contents = _page_contents(obj, by_num)
        page_contents.update(contents)
        fonts = _page_fonts(obj, by_num)
        if not fonts:
            continue
        for cnum in contents:
            page_fonts.setdefault(cnum, fonts)
    return global_cmap, page_fonts, page_contents


def _is_page(obj: _Obj) -> bool:
    t = obj.dict.get('/Type')
    return bool(t and t[0] == 'name' and t[1] == 'Page')


def _page_contents(obj: _Obj, by_num):
    """页面的 /Contents：可能是单个引用，也可能是引用数组。"""
    val = obj.dict.get('/Contents')
    if val is None:
        return []
    if val[0] == 'ref':
        return [val[1]]
    if val[0] == 'arr':
        out = []
        for item in val[1]:
            if item[0] == 'ref':
                out.append(item[1])
        return out
    return []


def _resolve_resources(obj: _Obj, by_num, depth: int = 0):
    """取页面的 /Resources；页面没写就沿 /Parent 往 /Pages 继承。"""
    if obj is None or depth > 8:
        return None
    res = obj.dict.get('/Resources')
    if res is not None:
        d = _resolve_dict(res, by_num)
        if d is not None:
            return d
    parent = obj.dict.get('/Parent')
    if parent is not None and parent[0] == 'ref':
        pobj = by_num.get(parent[1])
        if pobj is not None and pobj is not obj:
            return _resolve_resources(pobj, by_num, depth + 1)
    return None


def _page_fonts(obj: _Obj, by_num):
    """页面字体表：{字体名: (cmap, 每字符字节数)}。"""
    res = _resolve_resources(obj, by_num)
    if not res:
        return {}
    fref = res.get('/Font')
    fdict = _resolve_dict(fref, by_num)
    if not fdict:
        return {}
    fonts = {}
    for name, val in fdict.items():
        fd = _resolve_dict(val, by_num)
        if fd is None:
            continue
        tu = fd.get('/ToUnicode')
        cmap = _cmap_of_ref(tu, by_num) or {}
        fonts[name] = (cmap, _code_width(fd))
    return fonts


def _code_width(fdict: dict) -> int:
    """每个字符码占几个字节：Type0 / Identity-H 是 2 字节，其余 1 字节。"""
    st = fdict.get('/Subtype')
    if st is not None and st[0] == 'name' and st[1] == 'Type0':
        return 2
    enc = fdict.get('/Encoding')
    if enc is not None and enc[0] == 'name' and enc[1] in (
            'Identity-H', 'Identity-V'):
        return 2
    return 1


def _cmap_of_ref(val, by_num):
    """按 /ToUnicode 引用找到 CMap 流并解析。"""
    if val is None:
        return None
    if val[0] == 'ref':
        obj = by_num.get(val[1])
        if obj is None or not obj.has_stream:
            return None
        data = _decode_stream(obj)
        if not data:
            return None
        return _parse_cmap(data)
    return None


def _parse_cmap(data: bytes) -> dict:
    """解析 ToUnicode CMap，返回 {字符码字节: 文本}。

    支持的段落：
      beginbfchar   <src> <dst>            字符码 -> Unicode
      beginbfrange  <lo> <hi> <dst>        连续区间（dst 递增）
                    <lo> <hi> [<d1> <d2>..]  区间 + 数组
                    <lo> <hi> <cid>        区间 + 增量数字
      begincidchar / begincidrange         CID 版（少见，兼容处理）
    """
    mapping = {}
    for m in _BFCHAR_RE.finditer(data):
        body = m.group(1)
        pairs = re.findall(rb'<([0-9A-Fa-f\s]*)>\s*<([0-9A-Fa-f\s]*)>', body)
        for src_hex, dst_hex in pairs:
            src = _hex_bytes(src_hex)
            dst = _hex_bytes(dst_hex)
            if not src:
                continue
            mapping[src] = _utf16_text(dst)
    for m in _BFRANGE_RE.finditer(data):
        _parse_bfrange(m.group(1), mapping)
    # CID 版段落：字符码 -> CID -> Unicode（把 CID 当码点近似处理）
    for m in re.finditer(rb'begincidchar(.*?)endcidchar', data, re.S):
        pairs = re.findall(rb'<([0-9A-Fa-f\s]*)>\s*(\d+)', m.group(1))
        for src_hex, cid in pairs:
            src = _hex_bytes(src_hex)
            if src:
                mapping[src] = _utf16_text(_hex_bytes(b'%04X' % int(cid)))
    for m in re.finditer(rb'begincidrange(.*?)endcidrange', data, re.S):
        body = m.group(1)
        for lo_hex, hi_hex, cid in re.findall(
                rb'<([0-9A-Fa-f\s]*)>\s*<([0-9A-Fa-f\s]*)>\s*(\d+)', body):
            lo = _hex_bytes(lo_hex)
            hi = _hex_bytes(hi_hex)
            if not lo or not hi:
                continue
            lo_i = int.from_bytes(lo, 'big')
            hi_i = int.from_bytes(hi, 'big')
            for k in range(min(hi_i - lo_i + 1, 65536)):
                mapping[(lo_i + k).to_bytes(len(lo), 'big')] = \
                    _utf16_text(_hex_bytes(b'%04X' % (int(cid) + k)))
    return mapping


def _parse_bfrange(body: bytes, mapping: dict):
    """解析一个 beginbfrange 段落。"""
    toks = list(_tokens(body))
    i = 0
    n = len(toks)
    while i < n:
        kind, val = toks[i]
        if kind != 'hex':
            i += 1
            continue
        lo_hex = val
        if i + 1 >= n or toks[i + 1][0] != 'hex':
            i += 1
            continue
        hi_hex = toks[i + 1][1]
        i += 2
        if i >= n:
            break
        k2, v2 = toks[i]
        if k2 == 'hex':
            _add_range(mapping, lo_hex, hi_hex, _hex_bytes(v2))
            i += 1
        elif k2 == 'arr_open':
            arr, i = _parse_array_tokens(toks, i)
            lo_i = int.from_bytes(_hex_bytes(lo_hex), 'big')
            for k, item in enumerate(arr):
                if item[0] != 'hex':
                    continue
                _add_range(mapping, lo_hex, hi_hex, _hex_bytes(item[1]),
                           exact_offset=k, base=lo_i)
        elif k2 == 'num':
            # <lo> <hi> <cid> 增量形式：把 cid 当作码点近似
            lo_i = int.from_bytes(_hex_bytes(lo_hex), 'big')
            hi_i = int.from_bytes(_hex_bytes(hi_hex), 'big')
            for k in range(min(hi_i - lo_i + 1, 65536)):
                mapping[(lo_i + k).to_bytes(max(1, len(lo_hex.strip()) // 2),
                                           'big')] = \
                    _utf16_text(_hex_bytes(b'%04X' % (int(v2) + k)))
            i += 1
        else:
            i += 1


def _add_range(mapping: dict, lo_hex: str, hi_hex: str, dst: bytes,
               exact_offset: int = 0, base: int = 0):
    """把 <lo> <hi> <dst> 展开成逐个字符码的映射。

    dst 是 UTF-16BE 码点序列；区间内第 k 个字符码对应 dst 加上 k。
    """
    lo_bytes = _hex_bytes(lo_hex)
    hi_bytes = _hex_bytes(hi_hex)
    if not lo_bytes or not hi_bytes:
        return
    lo_i = int.from_bytes(lo_bytes, 'big')
    hi_i = int.from_bytes(hi_bytes, 'big')
    if hi_i < lo_i:
        lo_i, hi_i = hi_i, lo_i
    if hi_i - lo_i > 65535:
        hi_i = lo_i + 65535
    nb = max(1, len(lo_bytes))
    if not dst:
        return
    units = [int.from_bytes(dst[k:k + 2], 'big')
             for k in range(0, len(dst) - 1, 2)]
    if not units:
        units = [dst[0]]
    for k in range(hi_i - lo_i + 1):
        if exact_offset:
            if k != exact_offset:
                continue
            u = units
            code = base + exact_offset
        else:
            u = list(units)
            carry = k
            for idx in range(len(u) - 1, -1, -1):
                v = u[idx] + carry
                u[idx] = v & 0xFFFF
                carry = v >> 16
                if not carry:
                    break
            code = lo_i + k
        raw = b''.join(x.to_bytes(2, 'big') for x in u)
        mapping[code.to_bytes(nb, 'big')] = _utf16_text(raw)


def _hex_bytes(hexdigits) -> bytes:
    """十六进制字符串 -> 字节（忽略空白，奇数位补 0）。"""
    if isinstance(hexdigits, bytes):
        hexdigits = hexdigits.decode('ascii', 'ignore')
    cleaned = re.sub(r'\s+', '', hexdigits or '')
    if len(cleaned) % 2:
        cleaned += '0'
    try:
        return bytes.fromhex(cleaned)
    except ValueError:
        return b''


def _utf16_text(raw: bytes) -> str:
    """CMap 里的目标值 -> 文本（按 UTF-16BE 解）。"""
    if not raw:
        return ''
    if len(raw) == 1:
        return chr(raw[0])
    if len(raw) % 2:
        raw = raw + b'\x00'
    try:
        return raw.decode('utf-16-be', 'ignore')
    except Exception:  # noqa: BLE001
        return raw.decode('latin-1', 'ignore')


# ----------------------------------------------------------------------
# 文本算子
# ----------------------------------------------------------------------

def _extract_content_text(content: bytes, fonts, global_cmap: dict) -> str:
    """从一个内容流里抽文本。

    fonts：{字体名: (cmap, 每字符字节数)}，来自页面 /Resources；
    找不到时退回 global_cmap（所有 CMap 合并的全局表）。
    """
    out = []
    stack = []
    cur_font = None
    last_tm = None

    def resolve(raw: bytes) -> str:
        info = fonts.get(cur_font) if fonts else None
        if info is None:
            return _decode_bytes(raw, {}, 0, global_cmap)
        return _decode_bytes(raw, info[0], info[1], global_cmap)

    def push(item):
        # 数组里的元素要进数组，不能直接进操作数栈
        if stack and stack[-1][0] == 'arr':
            stack[-1][1].append(item)
        else:
            stack.append(item)

    for kind, val in _tokens(content):
        if kind == 'name':
            push(('name', val))
            continue
        if kind == 'num':
            push(('num', val))
            continue
        if kind == 'str':
            push(('str', val))
            continue
        if kind == 'hex':
            push(('str', _hex_bytes(val)))
            continue
        if kind == 'arr_open':
            stack.append(('arr', []))
            continue
        if kind == 'arr_close':
            if stack and stack[-1][0] == 'arr':
                arr = stack.pop()[1]
                stack.append(('arr', arr))
            continue
        if kind in ('dict_open', 'dict_close'):
            continue

        op = val
        if op == 'Tf':
            if len(stack) >= 2 and stack[-2][0] == 'name':
                cur_font = stack[-2][1]
            stack = []
        elif op == 'Tj':
            if stack and stack[-1][0] == 'str':
                out.append(resolve(stack[-1][1]))
            stack = []
        elif op == 'TJ':
            if stack and stack[-1][0] == 'arr':
                for item in stack[-1][1]:
                    if item[0] == 'str':
                        out.append(resolve(item[1]))
                    elif item[0] == 'num':
                        # 字间距调整量：绝对值够大就说明这里该断词了
                        if abs(item[1]) >= TJ_SPACE_THRESHOLD:
                            out.append(' ')
            stack = []
        elif op in ("'", '"'):
            if stack and stack[-1][0] == 'str':
                out.append(resolve(stack[-1][1]))
                out.append('\n')
            stack = []
        elif op in ('Td', 'TD'):
            # 竖直位移 = 换行；纯横向位移 = 同一行里的空隙
            ty = 0.0
            if stack and stack[-1][0] == 'num':
                ty = stack[-1][1]
            out.append('\n' if abs(ty) > 1e-6 else ' ')
            stack = []
        elif op == 'T*':
            out.append('\n')
            stack = []
        elif op == 'Tm':
            nums = [x[1] for x in stack if x[0] == 'num']
            if len(nums) >= 6:
                x, y = nums[4], nums[5]
                if last_tm is not None:
                    if abs(y - last_tm[1]) > 0.5:
                        out.append('\n')
                    elif abs(x - last_tm[0]) > 0.5:
                        out.append(' ')
                last_tm = (x, y)
            stack = []
        elif op == 'ET':
            out.append('\n')
            stack = []
        elif op == 'BT':
            stack = []
        else:
            # 其余操作符（q/Q/cm/gs/Do/re/...）：清掉操作数栈
            stack = []
    return ''.join(out)


def _decode_bytes(raw: bytes, cmap: dict, width: int, global_cmap: dict) -> str:
    """把内容流里的字节串解成文本。

    width：每个字符码几个字节（1 或 2）；0 表示不确定，自适应尝试。
    """
    if not raw:
        return ''
    out = []
    i = 0
    n = len(raw)
    while i < n:
        step = 1
        if width in (1, 2):
            code = raw[i:i + width]
            if len(code) < width:
                code = raw[i:]
                width = len(code)
            text = _lookup(cmap, global_cmap, code)
            if text is None and width == 2 and len(code) == 2:
                # 双字节字体里偶尔混进单字节码，退一步试试
                text = _lookup(cmap, global_cmap, code[:1])
                if text is not None:
                    width = 1
            if text is None:
                text = _fallback_char(code)
            step = max(1, len(code))
        else:
            code1 = raw[i:i + 1]
            text = _lookup(cmap, global_cmap, code1)
            if text is not None:
                step = 1
            else:
                code2 = raw[i:i + 2]
                text = _lookup(cmap, global_cmap, code2)
                if text is not None:
                    step = 2
                else:
                    text = _fallback_char(code1)
                    step = 1
        out.append(text)
        i += step
    return ''.join(out)


def _lookup(cmap: dict, global_cmap: dict, code: bytes):
    """在字体 CMap 里找，找不到就退回全局合并表。"""
    if not code:
        return None
    if cmap:
        v = cmap.get(code)
        if v is not None:
            return v
    if global_cmap:
        v = global_cmap.get(code)
        if v is not None:
            return v
    return None


def _fallback_char(code: bytes) -> str:
    """没有 CMap 时的兜底解码：单字节按 latin-1，双字节按 UTF-16BE。"""
    if not code:
        return ''
    if len(code) == 1:
        b = code[0]
        if b in (0x0a, 0x0d, 0x09):
            return '\n' if b != 0x09 else '\t'
        if b < 0x20:
            return ''
        try:
            return bytes([b]).decode('latin-1')
        except Exception:  # noqa: BLE001
            return ''
    try:
        s = code.decode('utf-16-be')
    except Exception:  # noqa: BLE001
        s = ''
    if s and s.isprintable():
        return s
    v = int.from_bytes(code, 'big')
    if 0xD800 <= v <= 0xDFFF:
        return ''
    try:
        return chr(v)
    except ValueError:
        return ''


def _tidy(text: str) -> str:
    """清理抽取结果：去掉控制字符、压缩多余空行。"""
    if not text:
        return ''
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    text = re.sub(r'[^\x09\x0a\x20-\x7e\xa0-￿'
                  r'　-〿＀-￯]', '', text)
    text = '\n'.join(line.rstrip() for line in text.split('\n'))
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


# ----------------------------------------------------------------------
# 命令行（方便手工验证真实 PDF）
# ----------------------------------------------------------------------

def _main(argv) -> int:
    if len(argv) < 2:
        sys.stdout.write('用法: python pdftext.py <file.pdf> [max_chars]\n')
        return 2
    path = argv[1]
    limit = int(argv[2]) if len(argv) > 2 else 200000
    try:
        with open(path, 'rb') as fh:
            data = fh.read()
    except OSError as exc:
        sys.stdout.write('读文件失败: %s\n' % exc)
        return 1
    t0 = time.time()
    res = extract_text(data, max_chars=limit)
    dt = time.time() - t0
    sys.stdout.write('文件大小: %d 字节\n' % len(data))
    sys.stdout.write('耗时: %.3f 秒\n' % dt)
    sys.stdout.write('页数: %d\n' % res['pages'])
    sys.stdout.write('字符数: %d\n' % res['chars'])
    sys.stdout.write('方法: %s\n' % res['method'])
    sys.stdout.write('说明: %s\n' % res['note'])
    sys.stdout.write('--- 前 300 字 ---\n%s\n' % res['text'][:300])
    return 0


if __name__ == '__main__':
    sys.exit(_main(sys.argv))
