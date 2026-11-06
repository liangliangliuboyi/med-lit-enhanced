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
   xref（增量更新时取最后一次出现的版本，正好也是当前生效的版本）。

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
import struct
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

# 滤镜名缩写 -> 全名。LaTeX / Word 生成的 PDF 经常用缩写。
_FILTER_ALIASES = {
    'Fl': 'FlateDecode',
    'FlA': 'FlateDecode',
    'AHx': 'ASCIIHexDecode',
    'A85': 'ASCII85Decode',
    'RL': 'RunLengthDecode',
}

# 预定义 CMap（Adobe CJK 集合）-> Python 编解码器。
# 中文文献 PDF 有两种常见形态：
#   1) 字体带 /ToUnicode（现代排版软件生成）—— 走 CMap 解析
#   2) 字体用 /Encoding /GBK-EUC-H 这类「预定义 CMap」（老排版/OCR 生成）——
#      此时字符码**本身就是** GBK/Big5 编码的字节，可以直接用标准库的
#      codec 解出来。不支持这一类的话，中文 PDF 会整篇变成乱码。
_PREDEFINED_CMAP_CODEC = {
    'GBK-EUC-H': 'gbk', 'GBK-EUC-V': 'gbk',
    'GB-EUC-H': 'gbk', 'GB-EUC-V': 'gbk',
    'GBpc-EUC-H': 'gbk', 'GBpc-EUC-V': 'gbk',
    'GBKp-EUC-H': 'gbk', 'GBKp-EUC-V': 'gbk',
    'GBK-V': 'gbk',
    'B5pc-H': 'big5', 'B5pc-V': 'big5',
    'ETen-B5-H': 'big5', 'ETen-B5-V': 'big5',
    'CNS-EUC-H': 'big5', 'CNS-EUC-V': 'big5',
    '90ms-RKSJ-H': 'shift_jis', '90ms-RKSJ-V': 'shift_jis',
    '90msp-RKSJ-H': 'shift_jis', '90msp-RKSJ-V': 'shift_jis',
    'EUC-H': 'euc_jp', 'EUC-V': 'euc_jp',
    'KSC-EUC-H': 'euc_kr', 'KSC-EUC-V': 'euc_kr',
    'KSCms-UHC-H': 'euc_kr', 'KSCms-UHC-V': 'euc_kr',
    'UniGB-UCS2-H': 'utf-16-be', 'UniGB-UCS2-V': 'utf-16-be',
    'UniCNS-UCS2-H': 'utf-16-be', 'UniCNS-UCS2-V': 'utf-16-be',
    'UniJIS-UCS2-H': 'utf-16-be', 'UniJIS-UCS2-V': 'utf-16-be',
}

# 会产生中日韩文字的编码：这些字体排出来的文本里词间没有空格，
# 不能因为「横向挪了位置」就补空格（否则每个汉字后面都跟一个空格）。
_CJK_CODECS = frozenset((
    'gbk', 'gb2312', 'big5', 'big5hkscs', 'shift_jis', 'euc_jp',
    'euc_kr', 'cp949', 'gb18030',
))

# 词法记号。正则切分比逐字符扫描快一个量级（内容流动辄上百 KB）。
_TOKEN_RE = re.compile(rb"""
      (?P<ws>[\x00\t\n\x0c\r ]+)
    | (?P<comment>%[^\r\n]*)
    | (?P<dict_open><<)
    | (?P<dict_close>>>)
    | (?P<hexstr><[0-9A-Fa-f\s]*>)
    | (?P<arr_open>\[)
    | (?P<arr_close>\])
    | (?P<name>/[^\s\(\)\[\]<>/%]*)
    | (?P<num>[+-]?(?:\d+\.?\d*|\.\d+))
    | (?P<op>[A-Za-z*'"]+)
    | (?P<skip>[{}=>])
""", re.X)

# 间接对象头：`N G obj`
_OBJ_RE = re.compile(rb'(?<![0-9])(\d{1,10})[\x00\t\n\x0c\r ]+(\d{1,5})'
                     rb'[\x00\t\n\x0c\r ]+obj(?![A-Za-z0-9])')
# 流关键字。必须紧跟 EOL（PDF 规范要求），这样才不会把字符串里的
# "stream" 这个词误认成流关键字。
_STREAM_RE = re.compile(rb'(?<![A-Za-z0-9])stream(?![A-Za-z0-9])'
                         rb'(?:\r\n|\n|\r)')
# 内容流里是否出现过文本块的粗判
_BT_RE = re.compile(rb'(?<![A-Za-z0-9])BT(?![A-Za-z0-9])')
# 显示文本的算子有四种：Tj / TJ / ' / "，探测时都要认。
_SHOW_RE = re.compile(rb'(?<![A-Za-z0-9])(?:Tj|TJ)(?![A-Za-z0-9])'
                      rb"|(?<![A-Za-z0-9])[\'\"]")
# CMap 段落
_BFCHAR_RE = re.compile(rb'beginbfchar(.*?)endbfchar', re.S)
_BFRANGE_RE = re.compile(rb'beginbfrange(.*?)endbfrange', re.S)
_CIDCHAR_RE = re.compile(rb'begincidchar(.*?)endcidchar', re.S)
_CIDRANGE_RE = re.compile(rb'begincidrange(.*?)endcidrange', re.S)
_CODESPACE_RE = re.compile(rb'begincodespacerange(.*?)endcodespacerange', re.S)
# 十六进制串
_HEX_PAIR_RE = re.compile(rb'<([0-9A-Fa-f\s]*)>\s*<([0-9A-Fa-f\s]*)>')


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


def extract_images(pdf_bytes: bytes, min_bytes: int = 3000,
                   max_images: int = 20,
                   max_pixels: int = 4000000,
                   max_bytes: int = 32 * 1024 * 1024) -> list:
    """从 PDF 字节里抠出内嵌图片。

    返回 [{'data': bytes, 'ext': 'jpg'|'png', 'width': int, 'height': int,
           'index': int}, ...]

    为什么需要它：现在的链路是「HTML/XML 全文 -> 找 <img> -> 下载 -> 送 VL
    识图」。但很多开放获取论文**只有 PDF**（Europe PMC 给不出 XML），
    这时拿不到图，图表识别功能直接失效，而 PDF 里的内嵌图是可以直接抠的。

    两条路径：
      - /DCTDecode（JPEG）：流数据**原样就是完整 JPEG**，直接截出来即可，
        零解码成本。这类占绝大多数。
      - /FlateDecode（zlib 压缩的裸位图）：解压后自己包一层 PNG。

    过滤规则（否则会抠出一堆期刊 logo、页眉横线）：
      - 字节数 < min_bytes：装饰性小图标、线条
      - 单张 > max_bytes：超大图跳过（防御畸形 PDF 里的巨图）
      - width*height < 10000 或 > max_pixels：期刊 logo、分子结构示意图
      - 宽高比 > 8:1 或 < 1:8：分隔线
      - 最多返回 max_images 张

    任何异常都不外泄——最坏情况返回空列表；单张图出错只跳过它，
    不会中断整篇。
    """
    try:
        return _extract_images(pdf_bytes, min_bytes, max_images, max_pixels,
                               max_bytes)
    except Exception:  # noqa: BLE001 —— 兜底，绝不让异常外泄
        return []


def _extract_images(pdf_bytes, min_bytes, max_images, max_pixels, max_bytes):
    if isinstance(pdf_bytes, (bytearray, memoryview)):
        pdf_bytes = bytes(pdf_bytes)
    if not isinstance(pdf_bytes, bytes) or not pdf_bytes:
        return []
    if not is_pdf(pdf_bytes):
        return []

    objects = _scan_objects(pdf_bytes)
    if not objects:
        return []
    by_num = {}
    for obj in objects:
        by_num[obj.num] = obj
    _expand_object_streams(pdf_bytes, objects, by_num)

    images = []
    for obj in by_num.values():
        if len(images) >= max_images:
            break
        if not obj.has_stream:
            continue
        # 超大流直接跳过：既省内存，也不该让整篇提取失败
        if len(obj.stream_raw) > max_bytes:
            continue
        if not _is_image_xobject(obj):
            continue
        width = _int_of(obj.dict.get('Width'), 0)
        height = _int_of(obj.dict.get('Height'), 0)
        if width <= 0 or height <= 0:
            continue
        # 像素数过滤：太小是图标/logo，太大是没必要的巨图
        pixels = width * height
        if pixels < 10000 or pixels > max_pixels:
            continue
        # 宽高比过滤：极端比例基本是分隔线或页眉横条
        ratio = width / float(height)
        if ratio > 8.0 or ratio < 0.125:
            continue

        try:
            data = _image_payload(obj, width, height)
        except Exception:  # noqa: BLE001 —— 单张图失败只跳过它
            continue
        if data is None:
            continue
        if len(data) < min_bytes:
            continue
        images.append({
            'data': data,
            'ext': 'jpg' if data[:2] == b'\xff\xd8' else 'png',
            'width': width,
            'height': height,
            'index': len(images),
        })
    return images


def _is_image_xobject(obj: _Obj) -> bool:
    """这个对象是不是图片 XObject（/Type /XObject /Subtype /Image）。"""
    dct = obj.dict
    t = dct.get('Type')
    st = dct.get('Subtype')
    if not (t and t[0] == 'name' and t[1] == 'XObject'):
        return False
    return bool(st and st[0] == 'name' and st[1] == 'Image')


def _int_of(val, default: int = 0) -> int:
    """从 ('num', x) 里取整数。"""
    if val is None or val[0] != 'num':
        return default
    try:
        return int(val[1])
    except (TypeError, ValueError):
        return default


def _image_payload(obj: _Obj, width: int, height: int):
    """取出图片的字节数据与格式（jpg / png）。

    DCTDecode：流数据原样就是 JPEG，直接返回。
    FlateDecode：解压后是裸像素，自己包一层 PNG。
    """
    filters = _filters_of(obj.dict)
    if 'DCTDecode' in filters:
        # JPEG：原样就是完整 JPEG 文件
        data = obj.stream_raw
        # 去掉流数据末尾多出来的 EOL（PDF 里 stream...endstream 之间
        # 常见一个换行，不去掉会让某些 JPEG 解码器报错）
        if data.endswith(b'\r\n'):
            data = data[:-2]
        elif data.endswith(b'\n') or data.endswith(b'\r'):
            data = data[:-1]
        return data if data[:2] == b'\xff\xd8' else None

    if 'FlateDecode' in filters:
        raw = _inflate(obj.stream_raw)
        if not raw:
            return None
        return _pack_png(raw, width, height, obj.dict)
    return None


def _pack_png(raw: bytes, width: int, height: int, dct: dict):
    """把裸像素数据包成 PNG。

    PNG = 签名 + IHDR + IDAT(zlib) + IEND，每块都要算 CRC32。
    只支持 8 位深度的 DeviceGray / DeviceRGB / DeviceCMYK / Indexed，
    以及带 /DecodeParms /Predictor 的 PNG 预测器（部分 PDF 用它压缩位图）。
    """
    bits = _int_of(dct.get('BitsPerComponent'), 8)
    if bits != 8:
        # 16 位/1 位/4 位的位图打包很麻烦，且论文里的图基本都是 8 位
        return None
    colors = _color_components(dct.get('ColorSpace'))
    if not colors:
        return None
    expected = width * height * colors
    if len(raw) < expected:
        return None
    pixels = raw[:expected]

    # /DecodeParms /Predictor 12（PNG 预测器）：每行前面有一个过滤类型字节
    predictor = _predictor_of(dct)
    row_len = width * colors
    if predictor >= 10:
        pixels = _undo_png_predictor(pixels, width, height, colors)
        if pixels is None:
            return None
        row_len = width * colors + 1

    # PNG 的每行前面要加一个「过滤类型」字节（0 = None）
    rows = []
    for r in range(height):
        start = r * row_len
        end = start + row_len
        if end > len(pixels):
            break
        rows.append(b'\x00' + pixels[start:end])
    if len(rows) != height:
        return None
    body = zlib.compress(b''.join(rows), 6)

    color_type = {1: 0, 3: 2, 4: 6}.get(colors)
    if color_type is None:
        return None
    ihdr = _png_chunk(b'IHDR', struct.pack('>IIBBBBB', width, height,
                                           8, color_type, 0, 0, 0))
    return (b'\x89PNG\r\n\x1a\n' + ihdr
            + _png_chunk(b'IDAT', body) + _png_chunk(b'IEND', b''))


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    """拼一个 PNG 块：长度 + 类型 + 数据 + CRC32。"""
    return (struct.pack('>I', len(data)) + kind + data
            + struct.pack('>I', zlib.crc32(kind + data) & 0xFFFFFFFF))


def _color_components(colorspace):
    """从 /ColorSpace 推出每像素的字节数（1=灰度 3=RGB 4=CMYK）。"""
    if colorspace is None:
        return None
    if colorspace[0] == 'name':
        name = colorspace[1]
        if name in ('DeviceGray', 'CalGray', 'G'):
            return 1
        if name in ('DeviceRGB', 'CalRGB', 'RGB'):
            return 3
        if name == 'DeviceCMYK':
            return 4
        return None
    if colorspace[0] == 'array':
        items = colorspace[1]
        if not items:
            return None
        first = items[0]
        if first[0] == 'name':
            name = first[1]
            if name in ('DeviceGray', 'CalGray', 'G'):
                return 1
            if name in ('DeviceRGB', 'CalRGB', 'RGB'):
                return 3
            if name == 'DeviceCMYK':
                return 4
            return None
        if first[0] == 'ref':
            # ICCBased：要看被引用流字典里的 /N
            return 3      # ICCBased 最常见是 3 通道
        if first[0] == 'dict':
            # Indexed：调色板，1 通道
            return 1
    return None


def _predictor_of(dct: dict) -> int:
    """读 /DecodeParms 里的 /Predictor（没有就是 1）。"""
    parms = dct.get('DecodeParms')
    if parms is None:
        return 1
    if parms[0] == 'array' and parms[1]:
        parms = parms[1][0]
    if parms[0] != 'dict':
        return 1
    return _int_of(parms[1].get('Predictor'), 1)


def _undo_png_predictor(data: bytes, width: int, height: int, colors: int):
    """还原 PNG 预测器（/Predictor 10~15，常见 12=PNG Up）。

    每行开头有一个过滤类型字节，之后按类型做加减。
    """
    bpp = colors
    row_len = width * colors
    out = bytearray()
    prev = bytearray(row_len)
    pos = 0
    for _ in range(height):
        if pos >= len(data):
            return None
        ft = data[pos]
        pos += 1
        row = bytearray(data[pos:pos + row_len])
        if len(row) < row_len:
            return None
        pos += row_len
        if ft == 1:      # Sub
            for i in range(bpp, row_len):
                row[i] = (row[i] + row[i - bpp]) & 0xFF
        elif ft == 2:    # Up
            for i in range(row_len):
                row[i] = (row[i] + prev[i]) & 0xFF
        elif ft == 3:    # Average
            for i in range(row_len):
                left = row[i - bpp] if i >= bpp else 0
                row[i] = (row[i] + ((left + prev[i]) >> 1)) & 0xFF
        elif ft == 4:    # Paeth
            for i in range(row_len):
                a = row[i - bpp] if i >= bpp else 0
                b = prev[i]
                c = prev[i - bpp] if i >= bpp else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                if pa <= pb and pa <= pc:
                    pr = a
                elif pb <= pc:
                    pr = b
                else:
                    pr = c
                row[i] = (row[i] + pr) & 0xFF
        elif ft != 0:
            return None
        out.extend(row)
        prev = row
    return bytes(out)


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
    # PDF 1.5+ 把大量对象压进「对象流」，先展开它们
    _expand_object_streams(pdf_bytes, objects, by_num)
    uniq = [by_num[n] for n in sorted(by_num)]

    pages = _count_pages(uniq)
    global_info, page_fonts, page_contents, cmap_nums = \
        _build_font_maps(uniq, by_num)

    chunks = []
    skipped = 0
    probed = 0
    for obj in uniq:
        if not obj.has_stream:
            continue
        fonts = None
        decoded = None
        if obj.num in page_contents:
            fonts = page_fonts.get(obj.num)
            decoded = _decode_stream(obj)
            if decoded is None:
                skipped += 1
                continue
        else:
            # 页面 /Contents 没解析出来时的兜底：解码后看看是不是内容流
            if probed >= MAX_PROBE_STREAMS \
                    or not _probe_candidate(obj, cmap_nums):
                continue
            decoded = _decode_stream(obj)
            if decoded is None:
                skipped += 1
                continue
            if not (_BT_RE.search(decoded) and _SHOW_RE.search(decoded)):
                continue
            probed += 1
        text = _extract_content_text(decoded, fonts, global_info)
        if text.strip():
            chunks.append(text)

    if not chunks:
        # 区分「流解压失败」和「确实没有文本层」——这两种情况的原因
        # 完全不同，上游要能据此判断是该重试还是该走 OCR。
        notes = []
        if skipped:
            notes.append('有 %d 个流解压失败，已跳过' % skipped)
        if encrypted:
            notes.append('PDF 含 /Encrypt 加密标记，内容流需要口令才能解密')
        if notes:
            notes.append('未取到文本层（也可能是扫描件）')
            note = '；'.join(notes)
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
    # 预先算出所有 endobj / endstream / stream 关键字的位置，避免每个对象
    # 都从头扫一遍全文（10MB 的文件有上千个对象时，那是 O(n*k) 的浪费，
    # 实测一篇 2MB 的 arXiv 论文光这一项就要 5.9 秒）。
    endobj_pos = [m.start() for m in re.finditer(rb'endobj', data)]
    endstream_pos = [m.start() for m in re.finditer(rb'endstream', data)]
    stream_pos = [m.start() for m in _STREAM_RE.finditer(data)]

    objs = []
    pos = 0
    n = len(data)
    while pos < n:
        m = _OBJ_RE.search(data, pos)
        if not m:
            break
        obj = _parse_object(data, int(m.group(1)), int(m.group(2)),
                            m.end(), endobj_pos, endstream_pos, stream_pos)
        if obj is None:
            pos = m.end()
            continue
        objs.append(obj)
        # 跳过整个对象：既避免把流里的二进制误认成新对象头，
        # 也让 objnum 顺序天然接近阅读顺序。
        pos = obj.end if obj.end > m.end() else m.end()
    return objs


def _next_pos(positions, start):
    """positions 里的第一个 >= start 的位置（找不到返回 -1）。"""
    lo, hi = 0, len(positions)
    while lo < hi:
        mid = (lo + hi) // 2
        if positions[mid] < start:
            lo = mid + 1
        else:
            hi = mid
    return positions[lo] if lo < len(positions) else -1


def _parse_object(data: bytes, num: int, gen: int, body_start: int,
                  endobj_pos, endstream_pos, stream_pos):
    """切出对象体，并解析出字典与流数据。"""
    end_obj = _next_pos(endobj_pos, body_start)
    s_pos = _next_pos(stream_pos, body_start)
    has_stream = s_pos >= 0 and (end_obj < 0 or s_pos < end_obj)

    if not has_stream:
        body_end = end_obj if end_obj >= 0 else len(data)
        dct = _parse_dict(data[body_start:body_end])
        obj_end = (end_obj + 6) if end_obj >= 0 else len(data)
        return _Obj(num, gen, obj_end, dct, b'', False)

    dct = _parse_dict(data[body_start:s_pos])
    # 'stream' 关键字后必须跟一个 EOL
    data_start = s_pos + len(b'stream')
    if data[data_start:data_start + 2] == b'\r\n':
        data_start += 2
    elif data[data_start:data_start + 1] in (b'\n', b'\r'):
        data_start += 1

    end_stream = _next_pos(endstream_pos, data_start)
    length = dct.get('Length')
    data_end = -1
    # 优先用 /Length 定位：流数据里偶尔会出现 'endstream' 字样，
    # 按长度切才不会被误导。
    if length is not None and length[0] == 'num':
        cand_len = int(length[1])
        if 0 <= cand_len <= len(data) - data_start:
            cand = data_start + cand_len
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
        after = _next_pos(endobj_pos, end_stream)
        obj_end = (after + 6) if after >= 0 else len(data)
    return _Obj(num, gen, obj_end, dct, raw, True)


def _parse_objstm_member(num: int, body: bytes):
    """解析对象流里的一个成员对象。

    对象流里的成员**没有** `N 0 obj` 外壳，直接就是对象体。
    """
    sm = _STREAM_RE.search(body)
    if not sm:
        return _Obj(num, 0, 0, _parse_dict(body), b'', False)
    dct = _parse_dict(body[:sm.start()])
    data_start = sm.end()
    length = dct.get('Length')
    raw = b''
    if length is not None and length[0] == 'num':
        raw = body[data_start:data_start + int(length[1])]
    else:
        end = body.find(b'endstream', data_start)
        raw = body[data_start:end if end >= 0 else len(body)]
        if raw.endswith(b'\r\n'):
            raw = raw[:-2]
        elif raw.endswith(b'\n') or raw.endswith(b'\r'):
            raw = raw[:-1]
    return _Obj(num, 0, 0, dct, raw, True)


def _expand_object_streams(data: bytes, objects, by_num):
    """把 /Type /ObjStm（对象流）里压缩的对象展开出来。

    PDF 1.5 起有大量对象（字体字典、页面对象、页面树……）被压缩进
    「对象流」，文件里根本看不到 `N 0 obj`。不解开对象流，这些对象
    对我们等于不存在——字体映射拿不到、页面数也数不对。
    """
    for obj in list(objects):
        t = obj.dict.get('Type')
        if not (t and t[0] == 'name' and t[1] == 'ObjStm'):
            continue
        if not obj.has_stream:
            continue
        payload = _decode_stream(obj)
        if not payload:
            continue
        n_val = obj.dict.get('N')
        first_val = obj.dict.get('First')
        if not (n_val and n_val[0] == 'num' and first_val
                and first_val[0] == 'num'):
            continue
        count = int(n_val[1])
        first = int(first_val[1])
        # 头部是 count 组 (对象号, 相对偏移) 的十进制整数
        nums = [int(x) for x in re.findall(rb'\d+', payload[:first])]
        if len(nums) < count * 2:
            continue
        for i in range(count):
            onum = nums[2 * i]
            offset = nums[2 * i + 1]
            end = (nums[2 * i + 3] if i + 1 < count
                   else len(payload) - first)
            start = first + offset
            if start < 0 or end < 0 or end <= start:
                continue
            if onum in by_num:
                # 文件里已经有这个对象的直接版本，直接版本优先
                continue
            member = _parse_objstm_member(onum, payload[start:first + end])
            if member is not None:
                objects.append(member)
                by_num[onum] = member


# ----------------------------------------------------------------------
# 字典 / 数组 / 记号
# ----------------------------------------------------------------------

def _tokens(data: bytes):
    """把一段 PDF 数据切成记号序列。

    记号类型：'name' / 'num' / 'str' / 'hex' / 'arr_open' / 'arr_close' /
    'dict_open' / 'dict_close' / 'op'。

    字典的键统一**不带**前导斜杠（'Type' / 'Length'），与 PDF 里
    `/Type` / `/Length` 一一对应，方便到处用。
    """
    i = 0
    n = len(data)
    while i < n:
        if data[i] == 0x28:  # '(' 字面字符串
            s, i = _read_literal(data, i)
            yield ('str', s)
            continue
        m = _TOKEN_RE.match(data, i)
        if not m:
            i += 1
            continue
        kind = m.lastgroup
        if kind in ('ws', 'comment', 'skip'):
            i = m.end()
            continue
        if kind == 'name':
            yield ('name', m.group()[1:].decode('latin-1'))
        elif kind == 'num':
            try:
                yield ('num', float(m.group()))
            except ValueError:
                pass
        elif kind == 'hexstr':
            yield ('hex', m.group()[1:-1].decode('ascii', 'ignore'))
        elif kind == 'arr_open':
            yield ('arr_open', '')
        elif kind == 'arr_close':
            yield ('arr_close', '')
        elif kind == 'dict_open':
            yield ('dict_open', '')
        elif kind == 'dict_close':
            yield ('dict_close', '')
        else:  # op
            op = m.group().decode('latin-1')
            yield ('op', op)
            i = m.end()
            if op == 'BI':
                # 内联图片（BI ... ID <二进制> EI）：二进制里可能混着 '('
                # 之类字符，会被误当成字符串。直接跳到 EI 之后更安全。
                j = data.find(b'EI', i)
                i = n if j < 0 else j + 2
            continue
        i = m.end()


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
    # 找第一个 dict_open：从这里开始解析，容忍对象体前面有杂字符
    idx = -1
    for i, (kind, _val) in enumerate(toks):
        if kind == 'dict_open':
            idx = i
            break
    if idx < 0:
        return {}
    d, _ = _parse_dict_tokens(toks, idx)
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
        data = obj.stream_raw
        for f in _filters_of(obj.dict):
            if f == 'FlateDecode':
                data = _inflate(data)
            elif f == 'ASCII85Decode':
                data = _ascii85(data)
            elif f == 'ASCIIHexDecode':
                data = _asciihex(data)
            elif f == 'RunLengthDecode':
                data = _runlength(data)
            else:
                # DCTDecode / JPXDecode / CCITTFaxDecode / LZWDecode 等是
                # 图像或未实现的滤镜：内容流不会用它们，直接放弃这个流。
                return None
            if data is None:
                return None
        return data
    except Exception:  # noqa: BLE001 —— 解码失败只是「跳过这个流」
        return None


def _filters_of(dct: dict):
    """从字典里取出 /Filter 滤镜名列表（按应用顺序）。"""
    val = dct.get('Filter')
    if val is None:
        return []
    names = []
    if val[0] == 'name':
        names = [val[1]]
    elif val[0] == 'arr':
        for item in val[1]:
            if item[0] == 'name':
                names.append(item[1])
    return [_FILTER_ALIASES.get(n, n) for n in names]


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


def _probe_candidate(obj: _Obj, cmap_stream_nums) -> bool:
    """判断一个流「有可能」是内容流（避免把字体/图片也解开）。"""
    if obj.num in cmap_stream_nums:
        return False
    dct = obj.dict
    t = dct.get('Type')
    if t is not None and t[0] == 'name' and t[1] in (
            'Font', 'FontDescriptor', 'XObject', 'Metadata', 'Catalog',
            'Pages', 'Page', 'Annot', 'Filespec'):
        return False
    st = dct.get('Subtype')
    if st is not None and st[0] == 'name' and st[1] in (
            'Image', 'Type1C', 'CIDFontType0C', 'Type1', 'TrueType',
            'OpenType', 'CIDFontType0', 'CIDFontType2', 'Form', 'PDF'):
        return False
    if any(k in dct for k in ('Length1', 'Length2', 'Length3',
                              'FontFile', 'FontFile2', 'FontFile3')):
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
        t = obj.dict.get('Type')
        if t is None or t[0] != 'name':
            continue
        if t[1] == 'Page':
            pages += 1
        elif t[1] == 'Pages':
            c = obj.dict.get('Count')
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

    返回 (global_cmap, global_width, page_fonts, page_contents)：
      - global_cmap：文件里所有 ToUnicode CMap 合并后的全局表（兜底用）
      - global_width：全局表里字符码的字节数（0 表示长度不一致）
      - page_fonts：内容流 objnum -> {字体名: (cmap, 每字符字节数)}
      - page_contents：页面 /Contents 引用到的内容流 objnum 集合
    """
    global_cmap = {}
    widths = set()
    cmap_stream_nums = set()
    codec_votes = {}
    # 先把所有字体对象的 /ToUnicode 收进全局表（后出现的覆盖先出现的）
    for obj in objects:
        tu = obj.dict.get('ToUnicode')
        if tu is None:
            continue
        if tu[0] == 'ref':
            cmap_stream_nums.add(tu[1])
        cmap = _cmap_of_ref(tu, by_num)
        if cmap:
            mapping, width = cmap
            global_cmap.update(mapping)
            if width:
                widths.add(width)
        else:
            # 没有 ToUnicode，但字体可能用预定义 CMap（如 GBK-EUC-H）。
            # 这类字体的字符码本身就是 GBK 字节，可以直接用 codec 解。
            codec = _font_codec(obj.dict)
            if codec:
                codec_votes[codec] = codec_votes.get(codec, 0) + 1
    global_width = widths.pop() if len(widths) == 1 else 0
    # 全局兜底用的 codec：取用得最多的那个；票数并列就不猜了
    global_codec = None
    if codec_votes:
        best = max(codec_votes.values())
        winners = [c for c, n in codec_votes.items() if n == best]
        if len(winners) == 1:
            global_codec = winners[0]
    global_info = (global_cmap, global_width, global_codec)

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
    return (global_info, page_fonts, page_contents, cmap_stream_nums)


def _is_page(obj: _Obj) -> bool:
    t = obj.dict.get('Type')
    return bool(t and t[0] == 'name' and t[1] == 'Page')


def _page_contents(obj: _Obj, by_num):
    """页面的 /Contents：可能是单个引用，也可能是引用数组。"""
    val = obj.dict.get('Contents')
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
    res = obj.dict.get('Resources')
    if res is not None:
        d = _resolve_dict(res, by_num)
        if d is not None:
            return d
    parent = obj.dict.get('Parent')
    if parent is not None and parent[0] == 'ref':
        pobj = by_num.get(parent[1])
        if pobj is not None and pobj is not obj:
            return _resolve_resources(pobj, by_num, depth + 1)
    return None


def _page_fonts(obj: _Obj, by_num):
    """页面字体表：{字体名: (cmap, 每字符字节数, 预定义CMap的codec)}。"""
    res = _resolve_resources(obj, by_num)
    if not res:
        return {}
    fdict = _resolve_dict(res.get('Font'), by_num)
    if not fdict:
        return {}
    fonts = {}
    for name, val in fdict.items():
        fd = _resolve_dict(val, by_num)
        if fd is None:
            continue
        tu = fd.get('ToUnicode')
        cmap = _cmap_of_ref(tu, by_num)
        if cmap:
            mapping, width = cmap
            codec = None
        else:
            # 没有 ToUnicode：看看 /Encoding 是不是预定义 CMap
            # （中文 PDF 常见 GBK-EUC-H / ETen-B5-H 等）
            mapping, width = {}, 0
            codec = _font_codec(fd)
        if not width:
            width = _code_width(fd)
        fonts[name] = (mapping, width, codec)
    return fonts


def _font_codec(fdict: dict):
    """从字体的 /Encoding 推出 Python codec 名（预定义 CMap）。

    返回 None 表示这个字体不是预定义 CMap（要么是 Identity-H 这种
    没有内置编码表的，要么编码表在文件里、但我们没能力反查 CID）。
    """
    enc = fdict.get('Encoding')
    if enc is None or enc[0] != 'name':
        return None
    return _PREDEFINED_CMAP_CODEC.get(enc[1])


def _code_width(fdict: dict) -> int:
    """每个字符码占几个字节：Type0 / Identity-H 是 2 字节，其余 1 字节。"""
    st = fdict.get('Subtype')
    if st is not None and st[0] == 'name' and st[1] == 'Type0':
        return 2
    enc = fdict.get('Encoding')
    if enc is not None and enc[0] == 'name' and enc[1] in (
            'Identity-H', 'Identity-V'):
        return 2
    return 1


def _cmap_of_ref(val, by_num):
    """按 /ToUnicode 引用找到 CMap 流并解析。

    返回 (mapping, code_width)；mapping 是 {字符码字节: 文本}，
    code_width 是字符码的字节数（0 表示推断不出来）。
    """
    if val is None or val[0] != 'ref':
        return None
    obj = by_num.get(val[1])
    if obj is None or not obj.has_stream:
        return None
    data = _decode_stream(obj)
    if not data:
        return None
    return _parse_cmap(data)


def _parse_cmap(data: bytes):
    """解析 ToUnicode CMap。

    返回 (mapping, code_width)：
      mapping     = {字符码字节: 文本}
      code_width  = 字符码占几个字节（1 或 2）

    支持的段落：
      beginbfchar   <src> <dst>              字符码 -> Unicode
      beginbfrange  <lo> <hi> <dst>          连续区间（dst 递增）
                    <lo> <hi> [<d1> <d2>..]  区间 + 数组
                    <lo> <hi> <cid>          区间 + 增量数字
      begincidchar / begincidrange           CID 版（少见，兼容处理）
    """
    mapping = {}
    widths = set()

    # 1) 码空间区间最权威：<0000> <FFFF> 说明字符码是 2 字节
    for m in _CODESPACE_RE.finditer(data):
        for lo_hex in re.findall(rb'<([0-9A-Fa-f\s]*)>', m.group(1)):
            cleaned = re.sub(rb'\s', b'', lo_hex)
            if cleaned:
                widths.add(max(1, len(cleaned) // 2))

    # 2) bfchar：<src> <dst>
    for m in _BFCHAR_RE.finditer(data):
        for src_hex, dst_hex in _HEX_PAIR_RE.findall(m.group(1)):
            src = _hex_bytes(src_hex)
            if not src:
                continue
            widths.add(len(src))
            mapping[src] = _utf16_text(_hex_bytes(dst_hex))

    # 3) bfrange：三种形式
    for m in _BFRANGE_RE.finditer(data):
        _parse_bfrange(m.group(1), mapping, widths)

    # 4) CID 版段落：字符码 -> CID -> Unicode（把 CID 当码点近似处理）
    for m in _CIDCHAR_RE.finditer(data):
        for src_hex, cid in re.findall(rb'<([0-9A-Fa-f\s]*)>\s*(\d+)',
                                       m.group(1)):
            src = _hex_bytes(src_hex)
            if src:
                widths.add(len(src))
                mapping[src] = _utf16_text(_hex_bytes(b'%04X' % int(cid)))
    for m in _CIDRANGE_RE.finditer(data):
        for lo_hex, hi_hex, cid in re.findall(
                rb'<([0-9A-Fa-f\s]*)>\s*<([0-9A-Fa-f\s]*)>\s*(\d+)',
                m.group(1)):
            lo = _hex_bytes(lo_hex)
            hi = _hex_bytes(hi_hex)
            if not lo or not hi:
                continue
            widths.add(len(lo))
            lo_i = int.from_bytes(lo, 'big')
            hi_i = int.from_bytes(hi, 'big')
            nb = max(1, len(lo))
            for k in range(min(hi_i - lo_i + 1, 65536)):
                mapping[(lo_i + k).to_bytes(nb, 'big')] = \
                    _utf16_text(_hex_bytes(b'%04X' % (int(cid) + k)))

    width = 0
    if len(widths) == 1:
        width = widths.pop()
    elif widths:
        # 宽度不一致（混合字体）：取最常见的那个，另一个靠自适应解码兜底
        width = max(widths)
    return mapping, width


def _parse_bfrange(body: bytes, mapping: dict, widths):
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
            # <lo> <hi> <dst>
            _add_range(mapping, lo_hex, hi_hex, _hex_bytes(v2), widths)
            i += 1
        elif k2 == 'arr_open':
            # <lo> <hi> [<d1> <d2> ...]：数组第 k 个元素对应 lo+k
            arr, i = _parse_array_tokens(toks, i)
            lo_i = int.from_bytes(_hex_bytes(lo_hex), 'big')
            for k, item in enumerate(arr):
                if item[0] != 'hex':
                    continue
                _add_range(mapping, lo_hex, hi_hex, _hex_bytes(item[1]),
                           widths, only_offset=k, base=lo_i)
        elif k2 == 'num':
            # <lo> <hi> <cid>：CID 增量形式（ToUnicode 里少见）
            lo_i = int.from_bytes(_hex_bytes(lo_hex), 'big')
            hi_i = int.from_bytes(_hex_bytes(hi_hex), 'big')
            nb = max(1, len(_hex_bytes(lo_hex)))
            widths.add(nb)
            for k in range(min(hi_i - lo_i + 1, 65536)):
                mapping[(lo_i + k).to_bytes(nb, 'big')] = \
                    _utf16_text(_hex_bytes(b'%04X' % (int(v2) + k)))
            i += 1
        else:
            i += 1


def _add_range(mapping: dict, lo_hex: str, hi_hex: str, dst: bytes, widths,
               only_offset: int = -1, base: int = 0):
    """把 <lo> <hi> <dst> 展开成逐个字符码的映射。

    dst 是 UTF-16BE 码点序列；区间内第 k 个字符码对应 dst 加上 k。
    only_offset >= 0 时只写入那一个字符码（数组形式用）。
    """
    lo_bytes = _hex_bytes(lo_hex)
    hi_bytes = _hex_bytes(hi_hex)
    if not lo_bytes or not hi_bytes or not dst:
        return
    lo_i = int.from_bytes(lo_bytes, 'big')
    hi_i = int.from_bytes(hi_bytes, 'big')
    if hi_i < lo_i:
        lo_i, hi_i = hi_i, lo_i
    if hi_i - lo_i > 65535:
        hi_i = lo_i + 65535
    nb = max(1, len(lo_bytes))
    widths.add(nb)
    units = [int.from_bytes(dst[k:k + 2], 'big')
             for k in range(0, len(dst) - 1, 2)]
    if not units:
        units = [dst[0]]
    for k in range(hi_i - lo_i + 1):
        if only_offset >= 0:
            if k != only_offset:
                continue
            u = list(units)
            code = base + only_offset
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

def _extract_content_text(content: bytes, fonts, global_info) -> str:
    """从一个内容流里抽文本。

    fonts：{字体名: (cmap, 每字符字节数, 预定义CMap的codec)}，来自页面
    /Resources；找不到时退回 global_info（所有 CMap 合并的全局表）。
    """
    out = []
    stack = []        # 操作数栈
    arrays = []       # 正在构造的数组（嵌套用）
    cur_font = None
    # 当前行起点（文本空间坐标）。Tm 绝对设置，TD/Td/T* 相对累加。
    line_x = None
    line_y = None
    leading = 0.0

    def resolve(raw: bytes) -> str:
        """把一段字符码解成文本。

        三条路径（优先级从高到低）：
        1. 页面 /Resources 里精确找到这个字体 -> 只用它自己的 CMap /
           预定义 CMap；两者都没有就是普通字体（WinAnsi 等），按
           latin-1 解。
        2. 找不到页面-字体对应 -> 退回全局合并 CMap（见模块说明）。
        """
        info = fonts.get(cur_font) if fonts else None
        if info is None:
            # 没有精确对应：用全局合并表兜底（正确率不如精确匹配，
            # 但不会因为找不到页面资源而整篇抽不出字）
            gmap, gwidth, _gcodec = global_info
            return _decode_bytes(raw, gmap, gwidth, gmap, gwidth)
        cmap, width, codec = info
        if codec:
            # 预定义 CMap（GBK/Big5/Shift-JIS 等）：字符码是变长的多字节
            # 序列，必须整串解码，逐字节解会把汉字劈成两半。
            return _decode_with_codec(raw, codec)
        if cmap:
            # 有自己的 ToUnicode：命中不了就按 latin-1 兜底。
            # 绝不能在这里套用全局表——同一篇 PDF 里多个子集字体会把
            # 相同的字符码映射成不同的字，全局表会把正确的字覆盖成别的字。
            return _decode_bytes(raw, cmap, width, None, 0)
        # 普通字体（WinAnsi/MacRoman/Standard 编码）：直接按 latin-1 解
        return _decode_bytes(raw, {}, width, None, 0)

    def is_cjk_font() -> bool:
        """当前字体是不是中日韩字体。

        中文排版里词与词之间**没有空格**（"中国科学院" 不是 "中 国 科 学
        院"），而且 OCR 出来的中文 PDF 常常给每个字单独一个 Tm 定位。
        所以遇到中日韩字体时，绝不能因为「横向挪了位置」就补空格，
        否则会得到每个字都带空格的烂文本。
        """
        info = fonts.get(cur_font) if fonts else None
        if info is None:
            info = global_info
        codec = info[2]
        if codec and codec in _CJK_CODECS:
            return True
        return _is_cjk_context(out)

    def push(item):
        # 数组里的元素要进数组，不能直接进操作数栈
        if arrays:
            arrays[-1].append(item)
        else:
            stack.append(item)

    def _set_line(x, y):
        """Tm：绝对设置行起点。"""
        nonlocal line_x, line_y
        if line_y is not None:
            if abs(y - line_y) > 0.5:
                out.append('\n')
            elif abs(x - line_x) > 0.5:
                # 横向挪了位置：多半是换了个词。但中日韩排版里词间本来
                # 就没有空格（每个字还可能单独定位），所以中日韩字体不补空格。
                if not is_cjk_font():
                    out.append(' ')
        line_x, line_y = x, y

    def _move_line(dx, dy):
        """Td/TD/T*：相对当前行起点位移。"""
        nonlocal line_x, line_y
        if line_x is None:
            # BT 之后的第一个 Td：此时文本矩阵是单位矩阵，
            # 所以这个位移就是绝对坐标。
            line_x, line_y = dx, dy
            return
        if abs(dy) > 1e-6:
            out.append('\n')
        elif abs(dx) > 0.5 and not is_cjk_font():
            out.append(' ')
        line_x += dx
        line_y += dy

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
            arrays.append([])
            continue
        if kind == 'arr_close':
            if arrays:
                arr = arrays.pop()
                push(('arr', arr))
            continue
        if kind in ('dict_open', 'dict_close'):
            continue

        op = val
        if op == 'Tf':
            # 操作数是 <字体名> <字号>
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
                        # 字间距调整量。PDF 规范里这个数是「往回拉的量」：
                        # **负数** = 把后面的字往右推 = 这里原本有个空隙
                        # （LaTeX 排版时一个词间空格正好是 -250）；
                        # **正数** = 把后面的字往左挤 = 字距微调（kerning），
                        # 例如 "Y" 与 "ou" 之间的 111。
                        # 所以只有负数才代表词间空格，正数不能补空格，
                        # 否则会把 "You" 拆成 "Y ou"。
                        if item[1] <= -TJ_SPACE_THRESHOLD:
                            out.append(' ')
            stack = []
        elif op in ("'", '"'):
            if stack and stack[-1][0] == 'str':
                out.append(resolve(stack[-1][1]))
                out.append('\n')
            stack = []
        elif op in ('Td', 'TD'):
            # Td/TD 是相对**当前行起点**的位移。行起点由 Tm 绝对设置，
            # 由 Td/TD 累加（BT 之后第一个 Td 是相对单位矩阵的绝对值）。
            tx = ty = 0.0
            if len(stack) >= 2 and stack[-1][0] == 'num' \
                    and stack[-2][0] == 'num':
                tx, ty = stack[-2][1], stack[-1][1]
            if op == 'TD':
                leading = -ty
            _move_line(tx, ty)
            stack = []
        elif op == 'TL':
            # 设置行距：T* 用它往下走一行
            if stack and stack[-1][0] == 'num':
                leading = stack[-1][1]
            stack = []
        elif op == 'T*':
            # 换到下一行
            _move_line(0.0, -leading)
            stack = []
        elif op == 'Tm':
            nums = [x[1] for x in stack if x[0] == 'num']
            if len(nums) >= 6:
                _set_line(nums[4], nums[5])
            stack = []
        elif op == 'ET':
            out.append('\n')
            stack = []
        elif op == 'BT':
            # 新段落：行起点重置，下一个 Tm/Td 会重新定位
            line_x = None
            line_y = None
            stack = []
        else:
            # 其余操作符（q/Q/cm/gs/Do/re/...）：清掉操作数栈
            stack = []
    return ''.join(out)


def _is_cjk_context(out) -> bool:
    """判断已输出的文字是不是中日韩上下文。

    中文 PDF（尤其是 OCR 出来的）每个汉字都会用单独的 Tm 定位，
    横向位置一直在变。如果不加区分地按「横向挪位 = 换词」处理，
    就会得到「中 国 科 学 院」这种每个字都带空格的烂文本。
    这里看已输出内容的末尾是不是中日韩字符：是就不补空格。
    """
    for chunk in reversed(out):
        if not chunk:
            continue
        for ch in reversed(chunk):
            if ch.isspace():
                continue
            return _is_cjk_char(ch)
    return False


def _is_cjk_char(ch: str) -> bool:
    """是不是中日韩表意文字（含中日韩标点与全角符号）。"""
    if not ch:
        return False
    code = ord(ch[0])
    return (
        0x4E00 <= code <= 0x9FFF      # CJK 统一表意文字
        or 0x3400 <= code <= 0x4DBF   # 扩展 A
        or 0xF900 <= code <= 0xFAFF   # 兼容表意文字
        or 0x3000 <= code <= 0x303F   # CJK 标点
        or 0xFF00 <= code <= 0xFFEF   # 全角形式
        or 0x20000 <= code <= 0x2A6DF  # 扩展 B
    )


def _decode_with_codec(raw: bytes, codec: str) -> str:
    """用 Python codec 解一段字符码（预定义 CMap 路径）。

    解不出来时退回 latin-1：宁可给出可辨认的字节，也不要整段丢掉。
    """
    if not raw:
        return ''
    try:
        return raw.decode(codec, errors='replace')
    except (LookupError, UnicodeDecodeError):
        try:
            return raw.decode(codec, errors='ignore')
        except Exception:  # noqa: BLE001
            return raw.decode('latin-1', 'replace')


def _decode_bytes(raw: bytes, cmap: dict, width: int, global_cmap: dict,
                  global_width: int) -> str:
    """把内容流里的字节串解成文本。

    width：当前字体每个字符码几个字节（1 或 2）；0 表示不确定，自适应尝试。
    """
    if not raw:
        return ''
    out = []
    i = 0
    n = len(raw)
    while i < n:
        step = 1
        if width in (1, 2):
            w = width
            code = raw[i:i + w]
            if len(code) < w:
                w = len(code)
                code = raw[i:]
            text = _lookup(cmap, global_cmap, code)
            if text is None and w == 2 and len(code) == 2:
                # 双字节字体里偶尔混进单字节码，退一步试试
                text = _lookup(cmap, global_cmap, code[:1])
                if text is not None:
                    w = 1
            if text is None and global_width in (1, 2) and global_width != w:
                # 当前字体的 CMap 没命中，换全局表的字符码宽度再试一次
                gw = global_width
                gcode = raw[i:i + gw]
                text = _lookup({}, global_cmap, gcode)
                if text is not None:
                    w = gw
            if text is None:
                text = _fallback_char(code)
            step = max(1, w)
        else:
            # 宽度未知：先按 1 字节试，再按 2 字节试
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
        if b in (0x0a, 0x0d):
            return '\n'
        if b == 0x09:
            return '\t'
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
    text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', text)
    text = '\n'.join(line.strip() for line in text.split('\n'))
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
