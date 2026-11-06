# -*- coding: utf-8 -*-
"""论文插图识别：把全文里的图片下载下来，交给多模态（VL）模型读出内容。

为什么需要这一层
----------------
循证医学里大量关键信息**只存在于图里**：meta 分析的森林图（效应量与
95%CI）、生存曲线（HR 与置信区间）、ROC 曲线（AUC）、亚组分析森林图、
主要不良事件时间线。只读正文/摘要会把这些全部漏掉——而这些恰恰是
"这个干预到底有多大效果"的答案所在。

所以本模块做三件事：
    1. 从全文结果里列出图片（extract_figures）
    2. 把图片下载下来并**内联成 base64 data URL**（FigureReader.read_all）
    3. 让 VL 模型输出结构化 JSON：图的类型 / 中文描述 / 图上真实出现的关键数值

三个硬性设计取舍（改代码前请先读完）
------------------------------------
1. **图片内联 base64，而不是把远程 URL 塞进 image_url**。
   出版商/PMC 的图床普遍有防盗链、Referer 校验与超时，模型服务端去拉
   经常 403/超时；而且很多服务端根本不接受外链 URL。内联后模型只跟
   一个字节流打交道，最稳。代价是请求体积变大，所以必须限制单图大小。

2. **绝不编造数值**。
   模型读图时很容易"看着像"给出一个 HR / 95%CI。循证场景里编造效应量
   是严重错误（比没有结果更糟：会被当成真实证据引用）。所以提示词里
   硬性要求"只抄图上真实印着的数值，看不清就写『图中未见明确标注』"，
   key_values 也被定位为"原文摘录"而不是"模型推算"。

3. **并发度封顶 + 每张图短超时 + 顺序还原**。
   本仓库实测教训：**并发打长文本会超时**。图片是 base64 进 prompt，
   体积比纯文本大得多，所以并发度不能开大（默认 4、硬上限 8），每张图
   单独限时（默认 90 秒）超时即放弃这一张。另外并发下完成顺序是乱的，
   必须按 index 收集后再排序还原成输入顺序——这是本仓库反复踩过的坑。

零第三方依赖（只用标准库）。Python 3.9+ 兼容。
"""
from __future__ import annotations

import base64
import hashlib
import inspect
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Optional

# 伪装成正常浏览器 UA：不少图床对空 UA / 默认 python-urllib 直接 403
UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/124.0 Safari/537.36')

# 默认多模态模型：Qwen3-VL-8B 是本机可用的最快的一档，够用且不慢。
# 更准的备选：Qwen/Qwen3-VL-30B-A3B-Instruct、Qwen/Qwen3-VL-32B-Instruct
DEFAULT_VL_MODEL = 'Qwen/Qwen3-VL-8B-Instruct'

# 图表类型枚举。模型只能从这十个里选，不在枚举里的一律归到 other，
# 绝不让非法值透出去（下游按 kind 分派，认不出的值会导致整条链路失效）。
FIGURE_KINDS = (
    'forest_plot',      # 森林图 / 效应量汇总图
    'survival_curve',   # 生存曲线（Kaplan-Meier）
    'roc_curve',        # ROC 曲线
    'flow_chart',       # 流程图 / 纳入排除图
    'bar_chart',        # 柱状图
    'line_chart',       # 折线图
    'scatter_plot',     # 散点图
    'table',            # 以图片形式呈现的表格
    'image',            # 照片 / 示意图 / 病理切片等非数据图
    'other',            # 以上都不像
)
_KIND_SET = frozenset(FIGURE_KINDS)

# 传给 chat_fn 的角色名。仓库的角色路由里没有专门的"看图"角色，
# 复用 reader 最贴切——读图本质上是精读的一部分。
FIGURE_ROLE = 'reader'

# 并发度：默认 4，硬上限 8。
#
# 为什么上限这么低：图片是 base64 进 prompt 的，请求体比纯文本大一个量级，
# 本仓库已经吃过"并发打长文本会超时"的亏。4 并发实测稳定，再高就换模型
# 更快（30B-A3B）更划算。
DEFAULT_WORKERS = 4
MAX_WORKERS_CAP = 8

# 下载图片时的 UA 与超时
DOWNLOAD_TIMEOUT = 30
CHUNK = 65536

# 模型输出解析失败时的统一错误文案
ERR_UNPARSED = '模型输出无法解析'


# ---------------------------------------------------------------------------
# 提示词
# ---------------------------------------------------------------------------
# 用 <<CAPTION>> 占位而不是 str.format：提示词正文里带了大段 JSON 花括号，
# 用 .format() 会被当成占位符而炸掉。占位符替换更安全。
FIGURE_PROMPT = """你是一名循证医学文献的图表解读专家。下面给你的是一篇医学论文中的一张插图（可能是森林图、生存曲线、ROC 曲线、流程图、直方图等）。

请用**中文**描述这张图，并且**只输出一个 JSON 对象**，不要输出任何 JSON 之外的解释文字。格式如下：

{
  "kind": "<图表类型>",
  "description": "<中文描述，2-4 句>",
  "key_values": "<从图中读到的关键数值>"
}

字段要求：

1. "kind" 必须从下面这个枚举里选一个，不要自创：
forest_plot（森林图/效应量汇总图）、survival_curve（生存曲线/Kaplan-Meier）、roc_curve（ROC 曲线）、flow_chart（流程图/纳入排除图）、bar_chart（柱状图）、line_chart（折线图）、scatter_plot（散点图）、table（以图片形式呈现的表格）、image（照片/示意图/病理切片等非数据图）、other（以上都不像）。

2. "description" 用中文写 2-4 句，必须交代清楚：
   - 坐标轴分别代表什么（横轴、纵轴各自的含义与单位）；
   - 各组（干预 vs 对照、各亚组等）的量级或走势差异；
   - 作者想用这张图证明什么结论。

3. "key_values" **只抄图上真实印着的数值**（效应量、HR、OR、RR、95%CI、P 值、样本量、AUC、中位生存期等），多个用分号分隔。
   **绝对禁止推测、估算或编造图上没有的数字。** 如果图上没有标注、或者看不清楚，就写「图中未见明确标注」。

原图图注（可能为空，仅用于定位这张图，准确性一律以图上实际内容为准）：
<<CAPTION>>
"""


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------
def _extract_json(text: Any) -> Optional[dict]:
    """从模型输出里抠出 JSON 对象（容忍 ```json 包裹与前后废话）。

    三层兜底，逐层放宽：
      1. 整体 json.loads——纯 JSON 输出走这条，最快。
      2. 剥掉 ```json 代码块围栏再 loads。
      3. 花括号配对扫描——处理"好的，结果如下：{...} 希望有帮助"这种
         前后夹带说明文字的情况。

    第 3 步会**逐个候选左括号重试**：模型偶尔在正文里先写一个
    "{说明}" 之类的非 JSON 花括号，扫到第一个就返回 None 会误伤后面
    真正的 JSON。所以这里扫失败就换下一个左括号继续试。

    这里刻意不复用 pipeline._json_load：本模块要能脱离 pipeline 单独导入
    与离线测试（pipeline 会连带拉起一串模块）。逻辑与 verify.py 保持一致。
    """
    if isinstance(text, dict):
        return text
    if not text:
        return None
    t = str(text).strip()
    try:
        obj = json.loads(t)
        if isinstance(obj, dict):
            return obj
        if isinstance(obj, list):
            for item in obj:
                if isinstance(item, dict):
                    return item
    except Exception:  # noqa: BLE001
        pass
    # 剥围栏后再试
    stripped = re.sub(r'^```(?:json)?\s*', '', t)
    stripped = re.sub(r'\s*```$', '', stripped).strip()
    if stripped != t:
        try:
            obj = json.loads(stripped)
            if isinstance(obj, dict):
                return obj
            if isinstance(obj, list):
                for item in obj:
                    if isinstance(item, dict):
                        return item
        except Exception:  # noqa: BLE001
            pass

    # 花括号配对扫描：逐个候选起点重试，跳过扫不成 JSON 的那个左括号
    pos = 0
    while True:
        start = t.find('{', pos)
        if start < 0:
            return None
        obj = _scan_braces(t, start)
        if isinstance(obj, dict):
            return obj
        pos = start + 1


def _scan_braces(t: str, start: int) -> Optional[dict]:
    """从 t[start]（必须是 '{'）开始做花括号配对扫描，返回解析出的对象。

    扫描时必须正确处理字符串内的花括号与转义，否则模型输出
    {"desc": "效应量 {HR} 显著"} 这种内容会被截断。
    """
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(t)):
        ch = t[i]
        if in_str:
            if esc:
                esc = False
            elif ch == '\\':
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                try:
                    obj = json.loads(t[start:i + 1])
                except Exception:  # noqa: BLE001
                    return None
                return obj if isinstance(obj, dict) else None
    return None


def _accepts_kw(fn: Callable[..., Any], name: str) -> bool:
    """探测 chat_fn 是否接受某个关键字参数。

    用 inspect 而不是"传了报 TypeError 再回头"——后者会把 chat_fn 内部
    真正的 TypeError 也误判成"不支持该参数"。与 verify._chat_accepts_model
    同一套思路。
    """
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    if name in params:
        return True
    return any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())


def _fingerprint(data: bytes) -> str:
    """图片内容指纹：SHA-256 前 16 位十六进制。

    为什么是内容指纹而不是地址：PMC 的同一张图常挂在多个地址下
    （正文里的缩略图 + 图集里的原图 + 补充材料里的副本），地址不同
    但内容一样。只看地址会重复送模型，白白消耗 token 和时间。
    """
    return hashlib.sha256(data).hexdigest()[:16]


def _absolute_url(href: str, base: str) -> str:
    """把可能是相对路径的 href 拼成绝对地址。

    fulltext 里的 href 有三种形态：
        - 完整 http(s) URL（多数情况）
        - 协议相对 //host/path
        - 相对路径 /articles/PMC123/bin/x.jpg 或 figures/x.jpg
    相对路径按全文的 url 作为 base 拼接。
    """
    h = (href or '').strip()
    if not h:
        return ''
    if h.startswith(('http://', 'https://')):
        return h
    if h.startswith('//'):
        return 'https:' + h
    b = (base or '').strip()
    if not b:
        return ''
    return urllib.parse.urljoin(b, h)


def _norm_kind(raw: Any) -> str:
    """把模型给的图类型归一化到枚举内。

    认不出一律归 'other'，**绝不把非法值原样透出去**。下游按 kind 分派，
    一个 'foo' 会让整条链路静默失效，比归到 other 更糟。
    """
    s = str(raw or '').strip().lower().replace('-', '_').replace(' ', '_')
    if s in _KIND_SET:
        return s
    # 少量常见别名，救一下模型的不规范写法
    alias = {
        'forest': 'forest_plot', 'forestplot': 'forest_plot',
        'kaplan_meier': 'survival_curve', 'km': 'survival_curve',
        'survival': 'survival_curve', 'roc': 'roc_curve',
        'flow': 'flow_chart', 'consort': 'flow_chart',
        'bar': 'bar_chart', 'line': 'line_chart',
        'scatter': 'scatter_plot', 'photograph': 'image',
        'photo': 'image', 'histogram': 'bar_chart',
    }
    return alias.get(s, 'other')


def _download_image(url: str, max_bytes: int, timeout: int) -> tuple[bytes, str]:
    """下载一张图片，返回 (字节, content-type)。

    三道防护：
      1. 只接受 image/* 的 Content-Type：出版商页面会返回 HTML，
         把它当图片喂给模型纯属浪费 token，还可能让模型对着报错页胡说。
      2. 先看 Content-Length、再流式读并累加——**任何时候都不能超限**，
         否则一张几十 MB 的高清图就能把内存撑爆。
      3. 设超时：图床慢的时候不至于卡死整轮。
    """
    req = urllib.request.Request(url, headers={
        'User-Agent': UA,
        'Accept': 'image/avif,image/webp,image/apng,image/*,*/*;q=0.8',
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        ctype = (resp.headers.get('Content-Type') or '').split(';')[0].strip().lower()
        if not ctype.startswith('image/'):
            raise ValueError('返回的不是图片（Content-Type=%s）'
                             % (ctype or '未知'))
        length = resp.headers.get('Content-Length')
        if length and str(length).isdigit() and int(length) > max_bytes:
            raise ValueError('图片过大（%s 字节，超过上限 %d）'
                             % (length, max_bytes))
        buf = bytearray()
        while True:
            chunk = resp.read(CHUNK)
            if not chunk:
                break
            buf.extend(chunk)
            if len(buf) > max_bytes:
                raise ValueError('图片过大（超过上限 %d 字节）' % max_bytes)
        return bytes(buf), ctype


# ---------------------------------------------------------------------------
# 公开 API
# ---------------------------------------------------------------------------
def extract_figures(fulltext: dict) -> list[dict]:
    """从全文结果里抽出图片清单。

    fulltext 是 pipeline 里现有的结构：
        {'source': 'europe_pmc', 'text': str, 'sections': [str],
         'figures': [{'href': str, 'caption': str}], 'url': str}
    'figures' 由别的模块提供，可能为空，也可能整个键都不存在。

    返回 [{'href': str, 'caption': str, 'index': int}]，index 从 0 开始，
    按出现顺序编号。**没有图就返回空列表，绝不抛异常**——调用方不该
    因为一篇没有插图的论文而整个检索失败。
    """
    try:
        figs = (fulltext or {}).get('figures') or []
        if not isinstance(figs, (list, tuple)):
            return []
        out: list[dict] = []
        for i, item in enumerate(figs):
            if isinstance(item, dict):
                href = str(item.get('href') or item.get('url') or '').strip()
                caption = str(item.get('caption') or item.get('title') or '').strip()
            elif isinstance(item, str):
                # 容错：只给了图注没给地址
                href, caption = '', item.strip()
            else:
                continue
            out.append({'href': href, 'caption': caption, 'index': i})
        return out
    except Exception:  # noqa: BLE001
        return []


class FigureReader:
    """用多模态模型识别全文插图。"""

    def __init__(self, chat_fn: Callable[..., dict],
                 model: str = DEFAULT_VL_MODEL,
                 max_figures: int = 6,
                 max_bytes: int = 4 * 1024 * 1024,
                 timeout: int = 90,
                 workers: int = DEFAULT_WORKERS,
                 role: str = FIGURE_ROLE,
                 downloader: Optional[Callable[[str], tuple[bytes, str]]] = None):
        """chat_fn 的签名与仓库里 verify.py / pipeline.py 用的一致：
        chat_fn(role, messages, temperature=..., max_tokens=..., model=...)
        返回 {'content': str, 'model': str, ...}

        max_figures  最多识别前 N 张图（默认 6）。超出的图仍会出现在结果里，
                     但 error 记 '超出上限未识别'——调用方能一眼看出哪些图
                     是因为上限没看，而不是因为失败没看。
        max_bytes    单张图片的字节上限（默认 4MB），超限直接跳过这张图。
        timeout      单张图的模型调用超时（默认 90 秒），超时即放弃这一张。
        workers      并发度，默认 4，硬上限 8（超过会被截断）。
        downloader   可选：自定义下载函数 (url) -> (bytes, content_type)，
                     用于离线测试与特殊站点。
        """
        self.chat_fn = chat_fn
        self.model = model or DEFAULT_VL_MODEL
        self.max_figures = max(0, int(max_figures or 0))
        self.max_bytes = max(1, int(max_bytes or 0))
        self.timeout = max(1, int(timeout or 0))
        # 并发度硬上限 8：图片请求体大，并发再高只会互相拖慢并触发超时
        self.workers = max(1, min(int(workers or DEFAULT_WORKERS), MAX_WORKERS_CAP))
        self.role = role or FIGURE_ROLE
        self.downloader = downloader

    # ---- 单张图 ----
    @staticmethod
    def _unmark_href(seen_hrefs: set, key: str, lock: threading.Lock) -> None:
        """下载失败时把地址从去重表里摘掉。

        为什么：去重表是用来防"同一张图被送模型两次"的，但如果这次
        下载是**失败**的（比如 404、图床超时），那么它根本没进过模型，
        不该占着这个坑——否则后面同一地址的图会被误判成"重复"而被
        静默跳过，把真正的下载错误掩盖掉。摘掉它，后面的图就能
        自己再试一次并报出真实原因。
        """
        with lock:
            seen_hrefs.discard(key)

    def _read_one(self, fig: dict, base: str, extra_caption: str,
                  seen_hrefs: set, seen_hashes: set, lock: threading.Lock) -> dict:
        """识别一张图。**任何异常都在这里兜住**，不让单张失败掀翻整批。

        返回字段与 read_all 的返回项同形。
        """
        out: dict[str, Any] = {
            'index': fig.get('index', 0),
            'href': fig.get('href', ''),
            'caption': fig.get('caption', ''),
            'description': '',
            'kind': '',
            'key_values': '',
            'model': self.model,
            'error': '',
        }
        href = out['href']
        if not href:
            out['error'] = '缺少图片地址'
            return out

        url = _absolute_url(href, base)
        if not url:
            out['error'] = '图片地址无法解析为绝对地址'
            return out

        # 去重（1）：地址层面。PMC 的同一张图常在多个位置出现。
        # 放在锁里做，保证并发下同一张图只有一个线程会去下载。
        key = url
        with lock:
            if key in seen_hrefs:
                out['error'] = '重复图片（与前面的图地址相同），已跳过识别'
                return out
            seen_hrefs.add(key)

        # 下载
        try:
            if self.downloader is not None:
                data, ctype = self.downloader(url)
            else:
                data, ctype = _download_image(url, self.max_bytes,
                                               min(self.timeout, DOWNLOAD_TIMEOUT))
        except urllib.error.HTTPError as e:
            out['error'] = '下载失败：HTTP %s' % e.code
            self._unmark_href(seen_hrefs, key, lock)
            return out
        except Exception as e:  # noqa: BLE001
            out['error'] = '下载失败：%s' % str(e)[:120]
            self._unmark_href(seen_hrefs, key, lock)
            return out
        if not data:
            out['error'] = '下载失败：内容为空'
            self._unmark_href(seen_hrefs, key, lock)
            return out
        # 大小与类型再校验一遍：自带 downloader（自定义站点 / 测试替身）
        # 可能绕过 _download_image 里的检查，这两道防线必须对所有路径生效。
        if len(data) > self.max_bytes:
            out['error'] = ('图片过大（%d 字节，超过上限 %d）'
                            % (len(data), self.max_bytes))
            self._unmark_href(seen_hrefs, key, lock)
            return out
        ctype = str(ctype or '').split(';')[0].strip().lower()
        if not ctype.startswith('image/'):
            out['error'] = '下载失败：返回的不是图片（Content-Type=%s）' % (
                ctype or '未知')
            self._unmark_href(seen_hrefs, key, lock)
            return out

        # 去重（2）：内容层面。同一张图可能挂在不同地址下（CDN 换域名、
        # 缩略图与原图并存），内容指纹才是可靠的判据。
        digest = _fingerprint(data)
        with lock:
            if digest in seen_hashes:
                out['error'] = '重复图片（内容指纹相同），已跳过识别'
                return out
            seen_hashes.add(digest)

        # 内联 base64：出版商图床有防盗链与超时，外链 URL 模型端经常拉不到
        b64 = base64.b64encode(data).decode('ascii')
        data_url = 'data:%s;base64,%s' % (ctype or 'image/png', b64)

        prompt = FIGURE_PROMPT.replace('<<CAPTION>>',
                                       out['caption'] or '（无）')
        if extra_caption:
            prompt += '\n补充上下文（仅供理解，不要当作图上数据引用）：%s\n' % extra_caption
        messages = [{
            'role': 'user',
            'content': [
                {'type': 'text', 'text': prompt},
                {'type': 'image_url',
                 'image_url': {'url': data_url}},
            ],
        }]

        # 调模型
        t0 = time.time()
        try:
            res = self._chat(messages)
        except Exception as e:  # noqa: BLE001
            out['error'] = '模型调用失败：%s' % str(e)[:150]
            return out
        elapsed = time.time() - t0

        content = res.get('content') if isinstance(res, dict) else res
        if isinstance(res, dict) and res.get('model'):
            out['model'] = str(res['model'])

        # 超预算的结果不采用：慢成这样的调用说明服务端已经在挣扎，
        # 收下它等于把一次"卡住"伪装成"成功"。
        if elapsed > self.timeout:
            out['error'] = '识别超时（耗时 %.0fs，超过 %ds）' % (elapsed, self.timeout)
            return out

        obj = _extract_json(content)
        if not isinstance(obj, dict):
            out['error'] = ERR_UNPARSED
            return out

        # kind 归一化：非法值绝不原样透出去（下游按 kind 分派，
        # 认不出的值会让整条链路静默失效）。
        raw_kind = str(obj.get('kind') or '').strip()
        out['kind'] = _norm_kind(raw_kind)
        if raw_kind and out['kind'] == 'other' and raw_kind.lower() not in _KIND_SET:
            # 记一笔：模型给了个枚举外的分类，说明它没看懂这张图，
            # 下游需要知道"这张图没能被正确分类"，而不只是看到 other。
            out['error'] = ('模型返回了非法图类型「%s」，已归一为 other'
                            % raw_kind[:40])
        out['description'] = str(obj.get('description') or '').strip()
        out['key_values'] = str(obj.get('key_values') or '').strip()
        return out

    # ---- 调模型 ----
    def _chat(self, messages: list[dict]) -> dict:
        """调用 chat_fn，兼容不接受 model / timeout 参数的实现。"""
        kwargs: dict[str, Any] = {'temperature': 0.1, 'max_tokens': 900}
        if _accepts_kw(self.chat_fn, 'model'):
            kwargs['model'] = self.model
        if _accepts_kw(self.chat_fn, 'timeout'):
            kwargs['timeout'] = self.timeout
        try:
            return self.chat_fn(self.role, messages, **kwargs)
        except TypeError as e:
            # chat_fn 的签名比预期更窄：退回到最小调用
            msg = str(e)
            if 'model' in kwargs and 'model' in msg:
                kwargs.pop('model')
                return self.chat_fn(self.role, messages, **kwargs)
            if 'timeout' in kwargs and 'timeout' in msg:
                kwargs.pop('timeout')
                return self.chat_fn(self.role, messages, **kwargs)
            raise

    # ---- 全部 ----
    def read_all(self, fulltext: dict, caption: str = '') -> list[dict]:
        """识别全文里的所有图。

        返回每条：
        {
          'index': int,        # 与 extract_figures 的 index 对应
          'href': str,
          'caption': str,      # 原始图注
          'description': str,  # 模型对图的描述
          'kind': str,         # 模型判定的图类型（枚举内）
          'key_values': str,   # 从图里读出的关键数值（原文摘录）
          'model': str,
          'error': str,        # 失败原因；成功为空串。
                              # 注：模型给出枚举外的图类型时，
                              # kind 会被归一为 other，同时 error 记一笔
                              # 说明"这张图没被正确分类"，好让下游能区分
                              # "确实就是 other" 与 "模型没认出来"。
        }

        **绝不抛异常**：单张图下载失败 / 解析失败 / 模型报错，都只记在那一条
        的 error 里，其余照常。返回顺序始终与输入顺序一致。
        """
        try:
            figs = extract_figures(fulltext)
        except Exception:  # noqa: BLE001
            return []
        if not figs:
            return []

        base = ''
        try:
            base = str((fulltext or {}).get('url')
                       or (fulltext or {}).get('base_url') or '')
        except Exception:  # noqa: BLE001
            base = ''

        results: dict[int, dict] = {}

        # 超出 max_figures 的图不送模型，但要在结果里体现出来：
        # 调用方需要区分"因为上限没看"和"因为失败没看"。
        for fig in figs[self.max_figures:]:
            results[fig['index']] = {
                'index': fig['index'],
                'href': fig.get('href', ''),
                'caption': fig.get('caption', ''),
                'description': '',
                'kind': '',
                'key_values': '',
                'model': self.model,
                'error': '超出上限未识别（max_figures=%d）' % self.max_figures,
            }

        todo = figs[:self.max_figures]
        if not todo:
            return [results[i] for i in sorted(results)]

        seen_hrefs: set = set()
        seen_hashes: set = set()
        lock = threading.Lock()

        # index -> 原始图信息。worker 异常兜底时要能还原 href/caption，
        # 否则调用方拿到的是一条没有出处的错误，信息等于丢了。
        meta = {f['index']: f for f in figs}

        def _blank(idx: int, error: str) -> dict:
            fig = meta.get(idx) or {}
            return {
                'index': idx,
                'href': fig.get('href', ''),
                'caption': fig.get('caption', ''),
                'description': '',
                'kind': '',
                'key_values': '',
                'model': self.model,
                'error': error,
            }

        workers = min(self.workers, len(todo))
        ex = ThreadPoolExecutor(max_workers=workers)
        futs = {}
        try:
            for fig in todo:
                futs[ex.submit(self._read_one, fig, base, caption,
                               seen_hrefs, seen_hashes, lock)] = fig['index']
            # 总时间上界：每张图限时 timeout，全部轮次 ceil(n/workers) 次
            deadline = self.timeout * ((len(todo) + workers - 1) // workers) + 5
            try:
                for fut in as_completed(futs, timeout=deadline):
                    idx = futs[fut]
                    try:
                        results[idx] = fut.result()
                    except Exception as e:  # noqa: BLE001
                        # worker 兜底：理论上 _read_one 已吞掉所有异常，
                        # 这里绝不让一张图因为并发而从结果里消失。
                        results[idx] = _blank(
                            idx, '处理失败：%s' % str(e)[:150])
            except Exception as e:  # noqa: BLE001
                # as_completed 超时：TimeoutError（py3.11+ 是 TimeoutError）
                for fut, idx in futs.items():
                    if idx not in results:
                        results[idx] = _blank(
                            idx, '识别超时：%s' % str(e)[:100])
        finally:
            # wait=False：不因为某张图卡死而拖住整轮返回
            try:
                ex.shutdown(wait=False, cancel_futures=True)
            except TypeError:  # Python < 3.9 没有 cancel_futures
                ex.shutdown(wait=False)

        # 顺序还原：并发下完成顺序是乱的，按 index 排回去
        return [results[i] for i in sorted(results)]
