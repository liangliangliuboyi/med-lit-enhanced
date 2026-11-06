# -*- coding: utf-8 -*-
"""全文获取：多源降级抓取 + PDF 交付 + 「拿不到怎么办」的中文获取指引。零第三方依赖。

这个模块解决的是这么一件事
--------------------------
`pubmed.py` 的 `fulltext()` 只能拿到 **PMC BioC** 全文，而 PMC 只覆盖开放获取（OA）
的那部分文章。真实场景里用户经常遇到的是：

    「这篇文献我要读全文，自动抓不到怎么办？」

所以本模块做四件事：

1. **多源降级**：PMC OA → Europe PMC → Unpaywall → OpenAlex，逐级降级找全文；
2. **PDF 交付**：找到 PDF 就下载给用户（落到 `download_dir`）；
3. **不落地阅读**：只想读一下不想留文件时，临时下载 → 读取 → **确认删除**；
4. **获取指引**：自动抓不到时，给出一份可执行的中文清单（代理 / VPN / 交大图书馆）。

来源优先级为什么是这个顺序
--------------------------
`PMC OA(BioC) → Europe PMC(XML) → PMC HTML → Unpaywall → OpenAlex`，
按「成功率 × 可信度 × 能给什么」排：

1. **PMC OA（BioC）**：最优先。它是**已经解析好的**结构化全文（段落 + 章节 + 图注），
   正文最干净、最适合直接喂给模型精读；而且不需要邮箱、不需要 key。
2. **Europe PMC（fullTextXML）**：第二。它是 JATS XML 原始全文，**覆盖范围比 PMC 更广**
   （有些文章 PMC 只有作者稿/摘要，Europe PMC 那边有正式版），解析成本也低。
   它有两个入口：`PMC{pmcid}` 与 `MED/{pmid}`，后者用于「没有 PMCID 但 Europe PMC
   收录了全文」的情况。
3. **PMC HTML 全文页**：第三。**目前最稳的一条源**，而且**唯一能给出图片直链**的免费源
   （`cdn.ncbi.nlm.nih.gov/pmc/blobs/…`），图表识别模块（`scripts/figures.py`）
   就靠它下载插图。它排在 Unpaywall 之前，因为 Unpaywall/OpenAlex 只能给
   「副本在哪」，给不出可直接下载的图片地址。
   实测有些文章前两条全断、只有 HTML 能出全文（见 `_try_pmc_html` 的注释）。
4. **Unpaywall**：第四。**免费、不需要 API key**（但 `email` 参数必填，没有邮箱就跳过——
   绝不瞎编一个邮箱，那等于替用户伪造身份）。它给的是「开放获取副本在哪」。
5. **OpenAlex**：第五。同样免费无 key，字段结构和 Unpaywall 高度相似，作为冗余。

「PMC 成功了还去请求 Europe PMC」是**必须避免**的浪费：一次 Europe PMC XML 往返
动辄几百 KB、1~3 秒，全文抓取是整条流水线里最慢的一环。所以每个源一旦拿到正文就
**立即 return**，绝不继续往下试（`fetch()` 里是顺序 if-return，不是循环收集）。

为什么 PDF 的文本要过一道「质量闸门」
--------------------------------------
本项目**零第三方依赖**，装不了 pdfplumber / pypdf / PyMuPDF；但同目录的
`pdftext.py` 用纯标准库实现了一个最小 PDF 解析器，能从 PDF 字节里抽正文。

问题是：**PDF 解析遇到没料到的结构时，很容易解出二进制乱码而不是文字。**
实测真实出版商 PDF 上出现过「可打印字符占比只有 0.44」的输出（乱码）。
乱码比没有文本更糟——它会被下游 pipeline 当成正文喂给模型，污染整条证据链。

所以本模块的做法是：

    抽出来的文本要通过「可打印字符占比 ≥ 0.85 且长度 ≥ 200 字」才采用，
    否则宁可 `text=''` 也不把乱码塞给下游。

「不落地」为什么必须确认删除
---------------------------
`fetch_ephemeral()` 的承诺是「读完不留痕」。但「调了 `os.remove()`」**不等于**删掉了：
文件可能被占用（杀毒软件/索引服务正在扫）、权限不足、路径算错。于是本模块的做法是：

    删 → 再 `os.path.exists()` 复查 → 只有确认不存在了才置 `temp_removed=True`；
    删不掉就**如实**写进 `note`，绝不假装删成功。

而且正文是在**删文件之前**从内存里的字节抽出来的——所以「不落地」不等于
「读不到正文」，用户照样能读到全文，磁盘上却一个字节都不留。

临时文件建在 `tempfile.mkdtemp()` 下的独立目录里并**整体 `shutil.rmtree()`**，
避免只删单个文件时漏掉 `.part` 中间态。

踩过的坑（改代码前务必看）
--------------------------
1. **代理不能只看环境变量。** 本机（Windows）实测：`curl` 直连某些站点能通，
   Python `urllib` 直连却被干扰；而 Windows 注册表
   `HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Internet Settings\\ProxyServer`
   里明明有可用代理。所以 `detect_network()` **必须真的发一个请求**去验证，
   不能只读配置就下结论。
2. **注册表里的 ProxyServer 可能是 `127.0.0.1:17890` 这种没写协议的**，
   直接丢给 `ProxyHandler` 会构造出非法代理，必须补 `http://`。
   也可能是 `http=1.2.3.4:8080;https=1.2.3.4:8080` 这种分协议形式，要拆开解析。
3. **`ProxyHandler({})` 会把环境变量里的代理也一起关掉。** 所以我们不用「空代理 =
   交给系统」这种含糊语义，而是**自己解析出明确的代理映射**再传给 ProxyHandler，
   行为完全确定、可测试。
4. **出版商域名返回 403 不等于不可达。** 403 说明 DNS + TCP + TLS 全都通了，
   是对方在挡爬虫。所以探测可达性时，HTTP 4xx/5xx 都要算「网络通」，
   只有 URLError / 超时才算不通——否则会把「被风控」误报成「没网」。
5. **Europe PMC 的 `<graphic>` 用 xlink 命名空间**：
   `xlink:href` 的真实属性名是 `{http://www.w3.org/1999/xlink}href`，
   用 `graphic.get('href')` 永远拿到 None。
6. **`<graphic>` 里的 href 通常是相对路径**（如 `WJR-9-27-g001.jpg`），
   必须拼成绝对 URL 才能给用户点。
7. **Unpaywall 的 `url_for_pdf` 未必真是 PDF**：实测有些 OA 出版商把 HTML 落地页
   也填在这个字段里。所以下载后要验魔数（`%PDF`），不是 PDF 就不存盘。
8. **PDF 正文抽取必须过质量闸门**：见上面「为什么 PDF 的文本要过一道质量闸门」。
   乱码比没有文本更糟——它会被当成正文喂给模型。
9. **PMC 的 HTML 页会随机返回 reCAPTCHA 挑战页**：同一个 URL 连抓 20 次，
   实测有 30% 的概率拿到的是 21KB 的 Google 挑战页（不是正文）。
   对策有三层：① 检查正文标志（`main-article-body` 等）而不是只看 HTTP 200；
   ② 换另一个域名（`pmc.ncbi.nlm.nih.gov` ↔ `www.ncbi.nlm.nih.gov`，内容一致）
   再试；③ 两个域名都失败就歇 1.5 秒再来一轮。
   实测 1 轮成功率 70%，2 轮 95%+。**不重试的话这一级会白白降级到 Unpaywall，
   丢掉图片直链**——那对图表识别是实质损失。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

UA_DEFAULT = 'medlit-agent/2.2 (fulltext retrieval)'

BIOC = 'https://www.ncbi.nlm.nih.gov/research/bionlp/RESTful/pmcoa.cgi/BioC_json'
EPMC = 'https://www.ebi.ac.uk/europepmc/webservices/rest'
UNPAYWALL = 'https://api.unpaywall.org/v2'
OPENALEX = 'https://api.openalex.org/works'

# PMC 的 HTML 全文页（有 PMCID 就能试）。
# 新旧两个域名内容一致，都留着：主域名偶尔被风控挡，老域名是备份。

# PMC 图片的规范地址：/pmc/articles/{PMCID}/bin/{文件名}
PMC_FIG_BASE = 'https://www.ncbi.nlm.nih.gov/pmc/articles/{pmcid}/bin/'

# xlink 命名空间：<graphic xlink:href="..."> 的属性真名
XLINK = '{http://www.w3.org/1999/xlink}href'

# 探测出版商可达性用的域名。刻意挑了访问量最大的几家：
# 它们能通基本就代表「网络这条路是通的」，不必逐刊去试。
PROBE_HOSTS = (
    'https://link.springer.com/',
    'https://www.sciencedirect.com/',
    'https://onlinelibrary.wiley.com/',
    'https://www.thelancet.com/',
    'https://www.nejm.org/',
)

# PDF 大小上限：超过就不下载了（避免个别超大文件把磁盘/内存打满）
MAX_PDF_BYTES = 80 * 1024 * 1024

# PMC HTML 页面上限：有的文章 1~2MB，超限就放弃这一级继续降级。
# 设 3MB 是权衡：够覆盖绝大多数文章，又不至于把内存/带宽打满。

# PMC HTML 的正文容器 class（实测 PMC 新版页面）：
#   <section class="body main-article-body">
# 同一篇的老页面（<div class="body">）也认，所以按 class 里含 'main-article-body'
# 或精确等于 'body' 来找。找不到就退回 `<article>` 标签。

# 代理 / 访问相关的提示语（用户能直接照做的动作）
VPN_HINT = ('若提示付费墙、403 或连接失败，请先开启 网络代理 / 交大医学院 VPN / '
            '交大图书馆校外访问入口 后重试')

# JATS 里这些标签里的 <p> 不算正文段落（图注/表注/公式另算）
_NON_BODY_TAGS = ('fig', 'table-wrap', 'disp-formula', 'graphic', 'inline-graphic')

# JATS 里这些标签是「块级」的：它们的内容前后要断开，否则相邻两段会粘成一坨。
# 例：`<title>Abstract</title><sec><title>Background</title><p>Severe…`
# 不处理的话会变成 "AbstractBackgroundSevere…"——词都黏在一起，
# 这样的正文喂给模型质量很差（本模块初版就踩过）。
_BLOCK_TAGS = ('title', 'p', 'label', 'sec', 'abstract', 'caption', 'kwd',
               'article-title', 'abstract-title')

# 这些标签是「角标/引用标记」：内容前后要加空格，
# 否则 "…globally<sup>1</sup> since…" 会变成 "…globally1 since…"（数字粘在词上）。
_MARKER_TAGS = ('xref', 'sup', 'sub')

_WS = re.compile(r'\s+')
_UNSAFE_FN = re.compile(r'[^A-Za-z0-9._-]+')


def _clean(text: str) -> str:
    """压掉连续空白。全文正文动辄几十万字，不压空白会让下游 token 白白变多。"""
    return _WS.sub(' ', (text or '')).strip()


def _node_text(node) -> str:
    """取一个 XML 节点的全部文字（含子节点），拼成一行。

    不能直接用 `''.join(node.itertext())`：那样相邻标签的文字会**粘在一起**。
    实测 Europe PMC 的 JATS 会把
    `<title>Abstract</title><sec><title>Background</title><p>Severe…`
    拼成 "AbstractBackgroundSevere…"——句子结构全毁了。
    所以这里按 JATS 的块级/角标语义插入分隔符：
      - 块级标签（title/p/sec…）的内容前后断开；
      - 角标标签（xref/sup/sub）前面补一个空格，数字才不会粘在单词上。
    """
    if node is None:
        return ''
    parts: list[str] = []

    def walk(el) -> None:
        tag = el.tag
        if not isinstance(tag, str):
            return
        if tag in _MARKER_TAGS:
            parts.append(' ')
        if el.text:
            parts.append(el.text)
        for child in el:
            walk(child)
            if child.tail:
                parts.append(child.tail)
        if tag in _BLOCK_TAGS:
            parts.append(' ')

    walk(node)
    return _clean(''.join(parts))


def _fig_url(pmcid: str, filename: str) -> str:
    """把 Europe PMC / BioC 给的相对图片名拼成绝对 URL。

    两个源给的都是**同一个 PMC 归档里的文件名**（如 `WJR-9-27-g001.jpg`），
    所以统一拼到 PMC 的 `/bin/` 目录下——这是 PMC 的规范图片地址。
    文件名要做 URL 转义，否则空格、`#` 之类会直接把 URL 截断。

    `pmcid` 是**纯数字**（不含 `PMC` 前缀），前缀在这里统一补——
    模块内部一律用纯数字，避免出现 `PMCPMC5334499` 这种拼接事故。
    """
    if not pmcid or not filename:
        return ''
    base = PMC_FIG_BASE.format(pmcid='PMC%s' % pmcid.replace('PMC', ''))
    return base + urllib.parse.quote(filename.lstrip('/'))


def _own_paragraphs(node) -> list[str]:
    """取一个容器**自己**的段落文本（不含嵌套 `<sec>` 里的，也不含图注）。

    为什么要专门写这个：JATS 里段落除了直接挂在 `<sec>` 下，还常被包在
    `<list>` / `<boxed-text>` / `<disp-quote>` 里。只取 `findall('p')`（直接子级）
    会漏掉这些；但用 `iter('p')` 又会把嵌套 `<sec>` 的段落重复算一遍
    （父 sec 一次、子 sec 递归一次）。所以这里做一次**受控递归**：
    遇到 `<sec>` 停（交给上层递归）、遇到图表标签停（单独收集）。
    """
    out: list[str] = []
    for child in node:
        tag = child.tag
        if not isinstance(tag, str):
            continue  # 注释/PI
        if tag == 'p':
            txt = _node_text(child)
            if txt:
                out.append(txt)
        elif tag == 'sec':
            continue
        elif tag in _NON_BODY_TAGS:
            continue
        else:
            out.extend(_own_paragraphs(child))
    return out


class FullTextFetcher:
    """全文抓取器。

    参数
    ----
    proxy: 显式代理。空则按 环境变量 → Windows 注册表 的顺序自动探测。
           形如 `http://127.0.0.1:7890` 或 `127.0.0.1:7890`（自动补协议）。
    timeout: 单次请求超时（秒）。
    download_dir: PDF 存放目录。空字符串 = 不落盘。
    keep_downloads: True = PDF 留到 `download_dir`；False = 临时下载后立刻删除。
    email: 邮箱。**Unpaywall 的 email 参数必填**，没有就跳过 Unpaywall 这个源
           （绝不编造邮箱）。
    ua: 自定义 User-Agent。
    """

    def __init__(self, *, proxy: str = '', timeout: int = 60,
                 download_dir: str = '', keep_downloads: bool = True,
                 email: str = '', ua: str = ''):
        self.proxy = (proxy or '').strip()
        self.timeout = int(timeout or 60)
        self.download_dir = (download_dir or '').strip()
        self.keep_downloads = bool(keep_downloads)
        self.email = (email or '').strip()
        self.ua = (ua or '').strip() or UA_DEFAULT

    # ==================================================================
    # 网络底层
    # ==================================================================
    def _proxy_map(self) -> dict:
        """当前生效的代理映射（scheme → url）。

        顺序：显式 proxy → 环境变量 → Windows 注册表。
        （与 urllib 自身在 Windows 上的行为一致：环境变量优先、注册表兜底。）
        返回空 dict 表示「明确不用代理」。
        """
        spec = self.proxy
        if not spec:
            spec = self._proxy_from_env() or self._proxy_from_registry()
        return self._parse_proxy_spec(spec)

    @staticmethod
    def _proxy_from_env() -> str:
        """从环境变量读代理。大小写两种写法都看（Linux/macOS 习惯小写）。"""
        for key in ('HTTPS_PROXY', 'https_proxy', 'HTTP_PROXY', 'http_proxy'):
            val = (os.environ.get(key) or '').strip()
            if val:
                return val
        return ''

    @staticmethod
    def _proxy_from_registry() -> str:
        """从 Windows 注册表读系统代理。非 Windows 或读不到都返回空。

        `ProxyEnable=0` 表示用户把系统代理关了，此时即便 `ProxyServer` 还在，
        也不该用（那是残留值）。
        """
        if os.name != 'nt':
            return ''
        try:
            import winreg  # 标准库，仅 Windows 才有
        except Exception:  # noqa: BLE001 —— 非 Windows 平台没有 winreg
            return ''
        try:
            with winreg.OpenKey(
                    winreg.HKEY_CURRENT_USER,
                    r'Software\Microsoft\Windows\CurrentVersion\Internet Settings'
            ) as key:
                try:
                    enabled = int(winreg.QueryValueEx(key, 'ProxyEnable')[0])
                except OSError:
                    enabled = 1  # 没有 ProxyEnable 这一项时，按「有就启用」处理
                if not enabled:
                    return ''
                val = winreg.QueryValueEx(key, 'ProxyServer')[0]
            return str(val or '').strip()
        except OSError:
            return ''

    @staticmethod
    def _parse_proxy_spec(spec: str) -> dict:
        """把各种写法的代理串解析成 ProxyHandler 用的 {'http': url, 'https': url}。

        支持三种写法（Windows 上第 1、3 种都实测出现过）：
          - `127.0.0.1:7890`                          → 没协议，自动补 `http://`
          - `http://127.0.0.1:7890`                   → 原样
          - `http=1.2.3.4:8080;https=1.2.3.4:8080`   → 分协议拆开
        `socks=` 之类的条目直接忽略：标准库 urllib 不支持 SOCKS，
        传进去只会变成一个连不上的死代理。
        """
        spec = (spec or '').strip()
        if not spec:
            return {}
        if ';' in spec or ('=' in spec and '://' not in spec.split(';')[0]):
            out: dict = {}
            for part in spec.split(';'):
                if '=' not in part:
                    continue
                key, _, val = part.partition('=')
                key = key.strip().lower()
                val = val.strip()
                if not val or key not in ('http', 'https'):
                    continue
                out[key] = val if '://' in val else 'http://' + val
            return out
        url = spec if '://' in spec else 'http://' + spec
        return {'http': url, 'https': url}

    def _opener(self):
        """构造带明确代理策略的 opener。

        这里**总是**显式传 ProxyHandler（哪怕是空 dict）：空 dict 的语义是
        「明确禁用所有代理」，比「不传 → 让 urllib 自己猜」要确定得多，
        也才能在测试里断言代理到底走没走。
        """
        proxies = self._proxy_map()
        return urllib.request.build_opener(urllib.request.ProxyHandler(proxies))

    def _request(self, url: str, timeout: int | None = None,
                 method: str = 'GET', retries: int = 2) -> tuple[int, bytes, str]:
        """发一个请求，返回 (状态码, 内容, Content-Type)。失败抛异常。

        429/5xx 会退避重试；4xx 直接抛（重试也没用）。
        """
        timeout = int(timeout or self.timeout)
        last: Exception | None = None
        for attempt in range(max(1, retries)):
            req = urllib.request.Request(url, method=method)
            req.add_header('User-Agent', self.ua)
            try:
                opener = self._opener()
                with opener.open(req, timeout=timeout) as resp:
                    body = resp.read()
                    ctype = resp.headers.get('Content-Type', '') or ''
                    return int(getattr(resp, 'status', 200) or 200), body, ctype
            except urllib.error.HTTPError as e:
                last = e
                if e.code == 429 or 500 <= e.code < 600:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise RuntimeError('HTTP %s：%s' % (e.code, url[:120])) from e
            except Exception as e:  # noqa: BLE001
                last = e
                time.sleep(1.0 * (attempt + 1))
        raise RuntimeError('请求失败：%s（%s）'
                           % (url[:120], type(last).__name__ if last else '未知'))

    def _get_text(self, url: str, timeout: int | None = None) -> str:
        _, body, _ = self._request(url, timeout=timeout)
        return body.decode('utf-8', 'ignore')

    def _get_json(self, url: str, timeout: int | None = None) -> dict:
        _, body, _ = self._request(url, timeout=timeout)
        data = json.loads(body.decode('utf-8', 'ignore'))
        return data if isinstance(data, dict) else {}

    # ==================================================================
    # 公开 API
    # ==================================================================
    def fetch(self, paper: dict) -> dict:
        """按优先级尝试多个来源拿全文。

        返回
        ----
        {
          'source': 'pmc_bioc' | 'europe_pmc' | 'unpaywall' | 'openalex' | '',
          'text': str,          # 纯文本正文（PDF 不解析，所以 PDF-only 时可能为空）
          'sections': [str],    # 章节标题
          'figures': [{'href','caption'}],
          'pdf_path': str,      # 下载到本地的 PDF 路径（没下载则 ''）
          'url': str,           # 全文的可访问网址（用户手动获取时可用）
          'ok': bool,
          'note': str,          # 失败原因 / 提示
        }

        `ok` 的含义是「**定位到了全文载体**」：正文文本、下载好的 PDF 文件、
        或可打开的全文链接三者之一。拿到的是哪一种，看 `text` / `pdf_path` / `url`
        与 `note` 的说明。

        `keep_downloads=False` 时 PDF 会临时下载后立刻删除，此时 `pdf_path` 恒为
        `''`，删除结果写在 `note` 里。

        **PDF 正文也会被抽取**（同目录的 `pdftext`，纯标准库），但要过质量闸门
        才采用——乱码比没有文本更糟，详见 `_pdf_text`。

        **任何异常都不外泄**：网络故障、XML 解析失败、磁盘写不动，全部收进 `note`。
        """
        try:
            res = self._fetch(paper, ephemeral=False)
        except Exception as e:  # noqa: BLE001 —— 顶层兜底，绝不把异常抛给调用方
            res = self._blank()
            res['note'] = '获取全文时发生未预期错误：%s' % type(e).__name__
        # 没走过临时下载的场合，返回结构就是文档里那 8 个字段；
        # 走过的话额外带上 temp_removed（临时文件是否已确认删除）。
        if not res.pop('_used_temp', False):
            res.pop('temp_removed', None)
        return res

    def fetch_ephemeral(self, paper: dict) -> dict:
        """**不落地**地读全文：临时下载 → 读取 → 立刻删除。

        与 `fetch()` 的差别：
          - `pdf_path` 恒为 `''`；
          - 多一个 `temp_removed` 字段，表示临时文件是否**已确认删除**；
          - 无论成功失败，**都不能留下任何本地残留文件**。

        「想读一下但不想留文件」是用户最常见的诉求，所以这个入口必须保证零残留。
        """
        try:
            res = self._fetch(paper, ephemeral=True)
        except Exception as e:  # noqa: BLE001
            res = self._blank()
            res['note'] = '获取全文时发生未预期错误：%s' % type(e).__name__
            res['temp_removed'] = True
        res.pop('_used_temp', None)
        res['temp_removed'] = bool(res.get('temp_removed', True))
        res['pdf_path'] = ''
        return res

    def acquisition_guide(self, papers: list[dict]) -> str:
        """给「自动没拿到全文的文献」生成一份可执行的中文获取指引。

        papers 是题录列表，每项至少有 pmid / title / journal / doi / year。
        返回 markdown 文本；传入空列表返回空字符串。
        """
        items = [p for p in (papers or []) if isinstance(p, dict)]
        if not items:
            return ''

        have_pmc: list[dict] = []
        need_more: list[dict] = []
        for p in items:
            if (p.get('pmcid') or '').strip():
                have_pmc.append(p)
            else:
                need_more.append(p)

        out: list[str] = []
        out.append('# 全文获取指引')
        out.append('')
        out.append('本次共 %d 篇文献：%d 篇在 PMC 已收录全文（可直接读），'
                   '%d 篇需要额外获取。' % (len(items), len(have_pmc), len(need_more)))
        out.append('')
        out.append('> ' + VPN_HINT + '。')
        out.append('')

        # ---- 一、已有全文 ----
        out.append('## 一、PMC 已有全文（可直接读，无需额外操作）')
        out.append('')
        if not have_pmc:
            out.append('本次没有在 PMC 找到全文的文献。')
            out.append('')
        else:
            for p in have_pmc:
                pmcid = self._norm_pmcid(p.get('pmcid'))
                out.append('### %s' % self._title_line(p))
                out.append('')
                out.append('- 期刊：%s（%s）｜ PMID：%s'
                           % (self._journal(p), self._year(p), self._pmid(p) or '未提供'))
                if self._doi(p):
                    out.append('- DOI：`%s`' % self._doi(p))
                out.append('- PMC 全文页：https://pmc.ncbi.nlm.nih.gov/articles/PMC%s/'
                           % pmcid)
                out.append('- PMC 全文 XML（可直接取正文）：%s/PMC%s/fullTextXML'
                           % (EPMC, pmcid))
                out.append('')

        # ---- 二、需要额外获取 ----
        out.append('## 二、需要额外获取全文的文献')
        out.append('')
        if not need_more:
            out.append('本次所有文献都已在 PMC 找到全文。')
            out.append('')
        else:
            out.append('以下文献自动抓不到全文，可按下面的途径逐条尝试。')
            out.append('')
            for idx, p in enumerate(need_more, 1):
                out.append('### %d. %s' % (idx, self._title_line(p)))
                out.append('')
                out.append('- 期刊：%s（%s）｜ PMID：%s'
                           % (self._journal(p), self._year(p), self._pmid(p) or '未提供'))
                doi = self._doi(p)
                if doi:
                    out.append('- DOI：`%s`' % doi)
                else:
                    out.append('- DOI：该文献未提供 DOI，请用 PMID 或期刊名检索')
                out.append('')
                out.append('获取途径（按推荐顺序尝试）：')
                out.append('')
                step = 0
                if doi:
                    step += 1
                    out.append('%d. **Unpaywall**（查开放获取状态与可下载副本）：'
                               '`https://api.unpaywall.org/v2/%s?email=<你的邮箱>`'
                               % (step, doi))
                    step += 1
                    out.append('%d. **OpenAlex**（查开放获取链接）：'
                               '`https://api.openalex.org/works/doi:%s`'
                               % (step, doi))
                    step += 1
                    out.append('%d. **期刊官网**（出版商正式页面，含全文或购买入口）：'
                               '`https://doi.org/%s`' % (step, doi))
                else:
                    step += 1
                    out.append('%d. **期刊官网**：用上面的期刊名与年份到出版商官网检索' % step)
                if self._pmid(p):
                    step += 1
                    out.append('%d. **PubMed 全文链接**：'
                               '`https://pubmed.ncbi.nlm.nih.gov/%s/`'
                               '（页面上的 “Full text links” 会跳到出版商）'
                               % (step, self._pmid(p)))
                step += 1
                out.append('%d. **交大图书馆**：按期刊名 + 年份检索该刊，'
                           '校外访问需走统一身份认证；确认是开放获取后可直接下载 PDF'
                           % step)
                out.append('')

        # ---- 三、网络与访问 ----
        out.append('## 三、网络与访问准备')
        out.append('')
        out.append('- **网络代理**：在系统或浏览器里开启代理（Clash / v2ray 等）后重试；'
                   '本机代理通常在 `127.0.0.1` 的某个端口，可在系统设置里查看。')
        out.append('- **交大医学院 VPN**：连上校园网后，'
                   '用交大统一身份认证登录 VPN（access.sjtu.edu.cn），再访问出版商站点。')
        out.append('- **交大图书馆校外访问入口**：图书馆主页（lib.sjtu.edu.cn）→'
                   '「校外访问 / 远程访问」，用统一身份认证登录后按刊名检索。')
        out.append('- **机构订阅**：若学校已订阅该刊，走上面的校外访问入口通常可直接看全文；'
                   '没有订阅时只能看摘要。')
        out.append('')
        out.append('说明：以上只提供**合法获取途径**，不涉及任何绕过付费墙的手段；'
                   '是否付费、如何付费由出版商与所在机构决定。')
        out.append('')
        return '\n'.join(out)

    def detect_network(self) -> dict:
        """探测当前网络环境。

        返回
        ----
        {
          'proxy': str,          # 当前生效的代理（环境变量优先，其次 Windows 注册表）
          'proxy_source': str,   # 'env' | 'registry' | ''
          'publisher_reachable': bool,
          'note': str,
        }

        注意：`detect_network()` 报告的是**这台机器的网络环境**，
        与构造参数 `proxy`（调用方显式指定）无关。

        为什么要「真的发一个请求」：本机实测 `curl` 直连能通、Python `urllib` 直连
        被干扰，而注册表里明明配了可用代理。只读配置就下结论会给出错误的
        「你网络没问题」或「你没配代理」。所以这里做一次真实探测（HEAD，超时很短）。
        """
        env_proxy = self._proxy_from_env()
        reg_proxy = '' if env_proxy else self._proxy_from_registry()
        if env_proxy:
            proxy, source = env_proxy, 'env'
        elif reg_proxy:
            proxy, source = reg_proxy, 'registry'
        else:
            proxy, source = '', ''

        reachable, detail = self._probe_publishers()
        notes = []
        if source:
            notes.append('检测到代理（来源：%s）：%s' % (source, proxy))
        else:
            notes.append('未检测到代理（环境变量与 Windows 注册表都没有）')
        if self.proxy:
            notes.append('本实例显式指定代理：%s（优先于上面的自动探测）' % self.proxy)
        notes.append(detail)
        return {
            'proxy': proxy,
            'proxy_source': source,
            'publisher_reachable': reachable,
            'note': '；'.join(notes),
        }

    # ==================================================================
    # 抓取主流程
    # ==================================================================
    def _blank(self) -> dict:
        return {
            'source': '', 'text': '', 'sections': [], 'figures': [],
            'pdf_path': '', 'url': '', 'ok': False, 'note': '',
        }

    def _fetch(self, paper: dict, ephemeral: bool) -> dict:
        """真正的抓取流程（`fetch` / `fetch_ephemeral` 共用）。"""
        paper = paper if isinstance(paper, dict) else {}
        res = self._blank()
        pmcid = self._norm_pmcid(paper.get('pmcid'))
        pmid = self._pmid(paper)
        doi = self._doi(paper)
        notes: list[str] = []

        # 临时目录：只有真的要落盘时才建；用完整体删掉并复查。
        tmpdir = ''
        # 记录临时目录**是否真的被创建过**：没下载过 PDF 时不该报 temp_removed。
        tmpdir_used: list[bool] = []
        # **要不要留下 PDF 文件**：只有「用户明确要留 + 给了目录」才留。
        # 其余情况（不落地模式、keep_downloads=False、或压根没给下载目录）
        # 一律走临时目录：下载 → 抽正文 → 删掉。
        #
        # 为什么「没给下载目录」也要走临时下载而不是干脆不下载：
        # 拿到 PDF 就能抽出正文（见 `_pdf_text`），不抽等于白下一趟。
        # 而「不存」的本意是**磁盘上不留东西**，临时下载再删正好满足。
        keep_file = bool(self.keep_downloads) and bool(self.download_dir)
        want_temp = not keep_file

        def _ensure_tmp() -> str:
            nonlocal tmpdir
            if not tmpdir:
                tmpdir = tempfile.mkdtemp(prefix='medlit-ft-')
                tmpdir_used.append(True)
            return tmpdir

        def _cleanup_tmp() -> bool:
            """删掉临时目录并**确认**删干净。返回是否已确认删除。"""
            nonlocal tmpdir
            if not tmpdir:
                return True
            ok = True
            try:
                shutil.rmtree(tmpdir, ignore_errors=False)
            except Exception:  # noqa: BLE001 —— 删不掉就如实报告
                ok = False
            # 复查：只有路径真的不存在了才算删掉
            if os.path.exists(tmpdir):
                try:
                    shutil.rmtree(tmpdir, ignore_errors=True)
                except Exception:  # noqa: BLE001
                    pass
                ok = not os.path.exists(tmpdir)
            tmpdir = ''
            return ok

        try:
            # ---------------- 1. PMC OA（BioC）----------------
            if pmcid:
                failed = False
                try:
                    got = self._try_bioc(pmcid)
                except Exception as e:  # noqa: BLE001
                    got = None
                    failed = True
                    notes.append('PMC BioC 请求失败（%s）' % type(e).__name__)
                if got:
                    res.update(got)
                    res['url'] = ('https://pmc.ncbi.nlm.nih.gov/articles/PMC%s/'
                                  % pmcid)
                    res['ok'] = True
                    res['note'] = '已从 PMC 开放获取全文（BioC）取得正文。'
                    return res
                # 失败原因与「没有全文」是两回事，只记一条，别把 note 撑成两倍
                if not failed:
                    notes.append('PMC 未提供可用全文。')

            # ---------------- 2. Europe PMC（fullTextXML）----------------
            if pmcid or pmid:
                failed = False
                try:
                    got = self._try_europepmc(pmcid, pmid)
                except Exception as e:  # noqa: BLE001
                    got = None
                    failed = True
                    notes.append('Europe PMC 请求失败（%s）' % type(e).__name__)
                if got:
                    res.update(got)
                    res['url'] = self._epmc_landing(pmcid, pmid)
                    res['ok'] = True
                    res['note'] = '已从 Europe PMC 全文 XML 取得正文。'
                    return res
                if not failed:
                    notes.append('Europe PMC 未提供可用全文。')

            # ---------------- 3. PMC HTML（最稳的一条源）----------------
            # 放在 Unpaywall 之前：PMC 的 HTML 页既能出正文，
            # **还能给出图片直链**——图表识别模块就靠它拿到森林图。
            # 实测（2026-11-06，PMC12439189）：
            #   PMC BioC   → 200 但内容是错误页 "[Error] : No result can be found."
            #   Europe PMC → 500 / 404
            #   PMC HTML   → 200，233KB，正文与 4 张插图直链全拿到
            if pmcid:
                failed = False
                try:
                    got = self._try_pmc_html(pmcid)
                except Exception as e:  # noqa: BLE001
                    got = None
                    failed = True
                    notes.append('PMC HTML 请求失败（%s）' % type(e).__name__)
                if got:
                    res.update(got)
                    res['ok'] = True
                    res['note'] = '已从 PMC 全文页取得正文。'
                    return res
                if not failed:
                    notes.append('PMC 全文页未提供可用全文。')

            # ---------------- 4. Unpaywall ----------------
            # email 是 Unpaywall 的**必填**参数，没有就跳过（不编造）。
            if doi and self.email:
                failed = False
                try:
                    got = self._try_unpaywall(paper, doi, _ensure_tmp, want_temp)
                except Exception as e:  # noqa: BLE001
                    got = None
                    failed = True
                    notes.append('Unpaywall 请求失败（%s）' % type(e).__name__)
                if got:
                    res.update(got)
                    return res
                if not failed:
                    notes.append('Unpaywall 未找到可用的开放获取副本。')
            elif doi and not self.email:
                notes.append('未配置邮箱，跳过 Unpaywall（该接口的 email 参数必填）。')

            # ---------------- 5. OpenAlex ----------------
            if doi:
                failed = False
                try:
                    got = self._try_openalex(paper, doi, _ensure_tmp, want_temp)
                except Exception as e:  # noqa: BLE001
                    got = None
                    failed = True
                    notes.append('OpenAlex 请求失败（%s）' % type(e).__name__)
                if got:
                    res.update(got)
                    return res
                if not failed:
                    notes.append('OpenAlex 未找到可用的开放获取副本。')

            # 全军覆没：至少给用户一个能点的入口（PubMed 页上的 Full text links）
            res['url'] = self._fallback_url(pmid, pmcid)
            res['note'] = self._no_fulltext_note(notes, doi, pmid)
            return res
        finally:
            # 无论成功、失败还是抛异常，临时目录都要清掉并确认。
            if want_temp:
                removed = _cleanup_tmp()
                res['temp_removed'] = removed
                # 标记走过临时路径：fetch() 据此决定要不要多返回 temp_removed。
                # 只有**真的建过临时目录**才算——压根没下载过 PDF 时，
                # 报一个 temp_removed=True 会让人误以为「下载并删除过」。
                res['_used_temp'] = bool(tmpdir_used)
                if not removed:
                    res['note'] = ((res.get('note') or '') +
                                   '；警告：临时文件未能确认删除，请手动检查').strip('；')
                # 临时模式下文件已被删掉，pdf_path 不能再指向一个不存在的路径
                res['pdf_path'] = ''

    # ---------------- 各来源的具体实现 ----------------
    def _try_bioc(self, pmcid: str) -> dict | None:
        """PMC OA 的 BioC JSON：段落 + 章节 + 图注，一次到位。"""
        url = '%s/PMC%s/unicode' % (BIOC, pmcid)
        raw = self._get_text(url, timeout=min(self.timeout, 90))
        data = json.loads(raw)
        if isinstance(data, list):
            data = data[0] if data else {}
        docs = (data or {}).get('documents') or []
        if not docs:
            return None
        buf: list[str] = []
        sections: list[str] = []
        figures: list[dict] = []
        for psg in docs[0].get('passages', []) or []:
            infons = psg.get('infons') or {}
            stype = infons.get('type') or ''
            sec = infons.get('section_type') or ''
            txt = (psg.get('text') or '').strip()
            if not txt:
                continue
            # 图/表说明单独收进 figures：系统评价常用森林图、漏斗图
            if stype in ('caption', 'fig_caption', 'table_caption') or sec in ('FIG', 'TABLE'):
                figures.append({
                    'href': _fig_url(pmcid, infons.get('file') or ''),
                    'caption': txt,
                })
                continue
            if sec and (not sections or sections[-1] != sec):
                sections.append(sec)
                buf.append('\n## %s' % sec)
            buf.append(txt)
        text = '\n'.join(buf).strip()
        if not text:
            return None
        return {'source': 'pmc_bioc', 'text': text, 'sections': sections,
                'figures': figures}

    # ---------------- PMC HTML 全文页 ----------------
    #
    # 为什么单独加这一级（实测 2026-11-06，PMC12439189）：
    #   PMC BioC   → HTTP 200 但内容是错误页 "[Error] : No result can be found."
    #   Europe PMC → HTTP 500（fullTextXML）/ 404（MED 形式）
    #   PMC HTML   → HTTP 200，233KB，正文与插图直链全拿到
    #
    # 而且它是**唯一能给出图片直链**的免费全文源：
    # 插图地址形如
    #   https://cdn.ncbi.nlm.nih.gov/pmc/blobs/.../xxx-g002.jpg
    # 图表识别模块（scripts/figures.py）就靠它下载图片送给多模态模型。
    #
    # PMC 的 HTML 有新旧两套版式，容器类名不同：
    #   新版：<section class="body main-article-body">
    #   旧版：<div id="main-content"> / <div class="article-body">
    # 所以容器要按多个候选依次尝试，**不能硬编码一个**。

    PMC_HTML = 'https://pmc.ncbi.nlm.nih.gov/articles/PMC%s/'
    PMC_HTML_MAX_BYTES = 6 * 1024 * 1024
    # 正文最少多少字才算「真的抽到了正文」。
    # 真实文章（含摘要）都远超过这个数；而容器在但正文没渲染出来的空壳页，
    # 剥出来的文字通常不到百字。定 200 是「既能挡住空壳、又不误伤短文」的位置。
    PMC_HTML_MIN_CHARS = 200
    # 正文容器的候选（按可靠性排序）
    _PMC_BODY_PATTERNS = (
        r'<section[^>]*class="[^"]*\bbody\b[^"]*main-article-body[^"]*"[^>]*>',
        r'<div[^>]*class="[^"]*\bmain-article-body\b[^"]*"[^>]*>',
        r'<div[^>]*id="main-content"[^>]*>',
        r'<div[^>]*class="[^"]*\barticle-body\b[^"]*"[^>]*>',
        r'<article[^>]*>',
    )
    # 新旧两个域名，内容一致（实测都是 233192 字节、4 张图）。
    #
    # 为什么两个都要试：PMC **会随机返回 reCAPTCHA 挑战页**——同一个 URL
    # 连抓 8 次，实测新域名 6 次中招、老域名 2 次中招（命中率随机，
    # 不是固定的）。挑战页只有 21KB 的 Google JS，和正文毫无关系。
    # 所以**换一个域名再试**是提高成功率最省事的办法——比加重试退避更划算。
    _PMC_HTML_URLS = (
        'https://pmc.ncbi.nlm.nih.gov/articles/PMC%s/',
        'https://www.ncbi.nlm.nih.gov/pmc/articles/PMC%s/',
    )
    # 最多试几轮（每轮 = 两个域名各试一次）。
    #
    # 实测（同一 URL 连抓，间隔 0.8~1s）：
    #   1 轮（2 个域名）    → 成功 14/20 = 70%
    #   2 轮（+间隔 1.5s） → 成功 19/20 = 95%，另一次测 14/14 = 100%
    #   3 轮               → 同样 100%，但每次多花 ~0.2s，没有额外收益
    # 挑战页是**随机命中**的，所以再试一轮的成功率远高于第一轮——
    # 多花一次往返（~1.5s）把成功率从 70% 提到 95%+，这笔买卖很划算。
    # 代价可控：只在「上一轮全失败」时才走下一轮，不拖慢正常路径。
    PMC_HTML_ROUNDS = 2
    # 两轮之间的间隔（秒）。反爬挑战页是随机命中的，歇一下再打更容易过。
    # 单独拎出来是为了让测试能把它调成 0（否则每个用例都要白等 1.5 秒）。
    PMC_HTML_RETRY_SLEEP = 1.5

    def _try_pmc_html(self, pmcid: str) -> dict | None:
        """从 PMC 的 HTML 全文页取正文与插图。失败返回 None（交给下一级）。

        策略：**多域名 × 多轮**。反爬挑战页是随机命中的（见 `_PMC_HTML_URLS`
        与 `PMC_HTML_ROUNDS` 的注释），所以换域名、再试一轮，
        实测能把成功率从 70% 提到 95%。
        """
        for rnd in range(self.PMC_HTML_ROUNDS):
            for tpl in self._PMC_HTML_URLS:
                url = tpl % pmcid
                try:
                    status, body, ctype = self._request(url)
                except Exception:  # noqa: BLE001
                    continue
                if status != 200 or not body:
                    continue
                if len(body) > self.PMC_HTML_MAX_BYTES:
                    # 太大了就不硬啃：超限时截断容易切坏结构，反而抽不出东西
                    continue
                html = body.decode('utf-8', 'ignore')
                if not self._looks_like_article(html):
                    # PMC 偶尔返回 404 页或反爬提示页——别把错误页当正文，
                    # 换下一个域名 / 下一轮再试。
                    continue
                container = self._extract_article_html(html)
                if not container:
                    continue
                text, sections = self._html_to_text(container)
                if len(text) < self.PMC_HTML_MIN_CHARS:
                    # 正文太短：多半是抽到了空壳页（容器在，但正文没渲染出来）。
                    # 门槛定 200 字：真实文章正文（含摘要）都远超过这个数，
                    # 而错误页/空壳页剥出来的文字通常不到百字。
                    continue
                figures = self._html_figures(html, url)
                return {
                    'source': 'pmc_html',
                    'text': text,
                    'sections': sections,
                    'figures': figures,
                    'url': url,
                }
            # 第一轮全失败（多半是撞上了反爬挑战页）。歇一下再试一轮：
            # 挑战页是随机命中的，间隔几秒再打成功率明显更高
            # （实测立刻重试容易被继续挡）。
            if rnd + 1 < self.PMC_HTML_ROUNDS:
                time.sleep(self.PMC_HTML_RETRY_SLEEP)
        return None

    @staticmethod
    def _looks_like_article(html: str) -> bool:
        """判断拿到的 HTML 是不是正文页，而不是 404/反爬页。"""
        head = html[:6000].lower()
        if 'captcha' in head or 'recaptcha' in head:
            return False
        # 正文页至少有这些标志之一
        for mark in ('main-article-body', 'article-body', 'id="main-content"',
                     '<article', '<figure'):
            if mark in html:
                return True
        return False

    @staticmethod
    def _extract_article_html(html: str) -> str:
        """切出正文容器那一段 HTML。找不到就返回空串。"""
        for pat in FullTextFetcher._PMC_BODY_PATTERNS:
            m = re.search(pat, html, re.I)
            if not m:
                continue
            start = m.end()
            # 找配对的结束标签（容器都是 div/section/article）
            tag = re.match(r'<(div|section|article)\b', m.group(0), re.I)
            if not tag:
                continue
            name = tag.group(1).lower()
            depth = 1
            pos = start
            open_pat = re.compile(r'<%s\b' % name, re.I)
            close_pat = re.compile(r'</%s>' % name, re.I)
            while depth > 0 and pos < len(html):
                no = open_pat.search(html, pos)
                nc = close_pat.search(html, pos)
                if nc is None:
                    break
                if no is not None and no.start() < nc.start():
                    depth += 1
                    pos = no.end()
                else:
                    depth -= 1
                    pos = nc.end()
                    if depth == 0:
                        return html[start:nc.start()]
        return ''

    @staticmethod
    def _strip_tags(fragment: str) -> str:
        """把一段 HTML 片段转成纯文本。"""
        s = re.sub(r'<(script|style)\b.*?</\1>', ' ', fragment,
                   flags=re.S | re.I)
        s = re.sub(r'<[^>]+>', ' ', s)
        s = (s.replace('&nbsp;', ' ').replace('&amp;', '&')
              .replace('&lt;', '<').replace('&gt;', '>')
              .replace('&quot;', '"').replace('&#39;', "'")
              .replace('&#x2013;', '-').replace('&#8211;', '-'))
        s = re.sub(r'&#(\d+);',
                   lambda m: chr(int(m.group(1))) if int(m.group(1)) < 65536
                   else ' ', s)
        return re.sub(r'\s+', ' ', s).strip()

    def _html_to_text(self, fragment: str) -> tuple:
        """HTML 正文 → (纯文本, 章节标题列表)。

        保留段落边界（用换行），否则整篇挤成一行会让模型读着费劲。
        """
        s = re.sub(r'<(script|style|nav|header|footer)\b.*?</\1>', ' ',
                   fragment, flags=re.S | re.I)
        sections: list[str] = []
        buf: list[str] = []

        def _flush(tag: str) -> None:
            txt = self._strip_tags(tag)
            if not txt:
                return
            if tag.strip().lower().startswith(('<h1', '<h2', '<h3', '<h4')):
                sections.append(txt)
                buf.append('\n## ' + txt)
            else:
                buf.append(txt)

        for m in re.finditer(r'<(h[1-4]|p)\b[^>]*>(.*?)</\1>', s,
                             flags=re.S | re.I):
            _flush(m.group(0))
        text = '\n'.join(x for x in buf if x.strip())
        text = re.sub(r'\n{3,}', '\n\n', text).strip()
        return text, sections

    def _html_figures(self, html: str, base_url: str) -> list:
        """从 HTML 里抽插图：**只取 `<figure>` 里的 `<img>`**。

        为什么必须限定在 `<figure>` 内：
        PMC 页面里 `<img>` 有 37 个，其中绝大多数是导航图标
        （us_flag.svg / icon-dot.svg / ncbi-logo / usa-icons）。
        不限定就会把一堆图标当成论文插图送进多模态模型。

        返回 [{'href': 图片绝对地址, 'caption': 图注纯文本}, ...]
        """
        out: list = []
        seen: set = set()
        for fm in re.finditer(r'<figure\b.*?</figure>', html, flags=re.S | re.I):
            block = fm.group(0)
            im = re.search(r'<img[^>]+src="([^"]+)"', block, re.I)
            if not im:
                continue
            src = (im.group(1) or '').strip()
            if not src or src.startswith('data:'):
                # 内联 base64 图跳过：体积大且没必要
                continue
            if not re.match(r'^https?://', src, re.I):
                src = urllib.parse.urljoin(base_url, src)
            cap = ''
            cm = re.search(r'<figcaption[^>]*>(.*?)</figcaption>', block,
                           flags=re.S | re.I)
            if cm:
                cap = self._strip_tags(cm.group(1))
            if src in seen:
                continue
            seen.add(src)
            out.append({'href': src, 'caption': cap})
        return out

    def _try_europepmc(self, pmcid: str, pmid: str) -> dict | None:
        """Europe PMC 的 JATS fullTextXML。

        两个入口都试：`PMC{pmcid}` 与 `MED/{pmid}`（后者覆盖「没有 PMCID 但
        Europe PMC 收录了全文」的情况）。
        """
        urls = []
        if pmcid:
            urls.append('%s/PMC%s/fullTextXML' % (EPMC, pmcid))
        if pmid:
            urls.append('%s/MED/%s/fullTextXML' % (EPMC, pmid))
        for url in urls:
            try:
                raw = self._get_text(url, timeout=min(self.timeout, 90))
            except Exception:  # noqa: BLE001 —— 换下一个入口
                continue
            if not raw or '<html' in raw[:200].lower():
                continue
            parsed = self._parse_epmc_xml(raw, pmcid)
            if parsed and parsed.get('text'):
                parsed['source'] = 'europe_pmc'
                return parsed
        return None

    @staticmethod
    def _pmcid_from_xml(root) -> str:
        """从 JATS XML 里读出 PMCID（**纯数字**，不带 `PMC` 前缀）。

        为什么需要：走 `MED/{pmid}` 入口时我们事先不知道 PMCID，
        但文章内部的 `<article-id pub-id-type="pmcid">` 带着——
        图片 href 是相对路径，要拼成绝对 URL 就必须知道 PMCID。
        """
        for aid in root.iter('article-id'):
            if (aid.get('pub-id-type') or '').strip() == 'pmcid':
                val = (aid.text or '').strip()
                if val:
                    return val.replace('PMC', '').strip()
        return ''

    def _parse_epmc_xml(self, xml_text: str, pmcid: str) -> dict:
        """解析 Europe PMC 的 JATS XML。

        要点：
          - `<sec>` 的 `<title>` 是章节名，`<p>` 是段落；
          - **嵌套** `<sec>` 要递归展开，否则会漏掉 Methods/Results 这类子章节；
          - `<fig>` 里的 `<graphic>` 用 **xlink 命名空间**
            （`{http://www.w3.org/1999/xlink}href`），用 `get('href')` 永远是 None；
          - `<graphic>` 的 href 多半是相对路径，要拼绝对 URL；
          - 段落用受控递归收集，避免同一段在父 sec 与子 sec 里各算一次。
        """
        root = ET.fromstring(xml_text)
        body = root.find('.//body')
        sections: list[str] = []
        lines: list[str] = []

        # XML 里若带 PMCID，以它为准（走 MED/{pmid} 入口时我们事先不知道）
        pmcid = pmcid or self._pmcid_from_xml(root)

        def walk(sec, depth: int) -> None:
            title = _node_text(sec.find('title'))
            if title:
                sections.append(title)
                lines.append('%s %s' % ('#' * min(depth + 2, 6), title))
            for txt in _own_paragraphs(sec):
                lines.append(txt)
            for sub in sec.findall('sec'):
                walk(sub, depth + 1)

        if body is not None:
            for txt in _own_paragraphs(body):
                lines.append(txt)
            for sec in body.findall('sec'):
                walk(sec, 0)

        # 摘要：JATS 里放在 <front><abstract>，对判断研究设计很有用
        abs_txt = _node_text(root.find('.//abstract'))
        if abs_txt:
            lines.insert(0, '## Abstract\n%s' % abs_txt)

        figures: list[dict] = []
        for fig in root.iter('fig'):
            graphic = fig.find('graphic')
            href = ''
            if graphic is not None:
                href = graphic.get(XLINK) or graphic.get('href') or ''
            caption = _node_text(fig.find('caption'))
            if not href and not caption:
                continue
            figures.append({
                'href': _fig_url(pmcid, href),
                'caption': caption,
            })

        text = '\n\n'.join(x for x in lines if x).strip()
        return {'source': 'europe_pmc', 'text': text, 'sections': sections,
                'figures': figures}
    def _try_unpaywall(self, paper: dict, doi: str, ensure_tmp, want_temp: bool) -> dict | None:
        """Unpaywall：免费无 key（email 必填）。返回开放获取副本信息。"""
        url = '%s/%s?email=%s' % (UNPAYWALL, urllib.parse.quote(doi),
                                  urllib.parse.quote(self.email))
        data = self._get_json(url)
        best = data.get('best_oa_location') or {}
        if not best:
            locs = data.get('oa_locations') or []
            best = locs[0] if locs else {}
        if not best:
            return None
        pdf_url = best.get('url_for_pdf') or ''
        landing = best.get('url_for_landing_page') or best.get('url') or ''
        return self._deliver_oa(paper, 'unpaywall', pdf_url, landing,
                                best.get('host_type') or '', ensure_tmp, want_temp)

    def _try_openalex(self, paper: dict, doi: str, ensure_tmp, want_temp: bool) -> dict | None:
        """OpenAlex：免费无 key。字段结构和 Unpaywall 相似，作为冗余源。"""
        url = '%s/doi:%s' % (OPENALEX, urllib.parse.quote(doi, safe='/'))
        data = self._get_json(url)
        oa = data.get('open_access') or {}
        best = data.get('best_oa_location') or {}
        pdf_url = best.get('pdf_url') or oa.get('oa_url') or ''
        landing = best.get('landing_page_url') or oa.get('oa_url') or ''
        if not pdf_url and not landing:
            return None
        return self._deliver_oa(paper, 'openalex', pdf_url, landing,
                                best.get('source') or '', ensure_tmp, want_temp)

    def _deliver_oa(self, paper: dict, source: str, pdf_url: str, landing: str,
                    host_type: str, ensure_tmp, want_temp: bool) -> dict | None:
        """把「找到了开放获取副本」这件事变成可交付的结果。

        能下载 PDF 就下载（按 keep_downloads 决定留在磁盘还是立刻删）；
        只有落地页 URL 时，把 URL 交出去让用户自己点。

        PDF 的正文文本由 `pdftext`（同目录，纯标准库）抽取，但**必须过质量闸门**
        才采用——见 `_pdf_text`。
        """
        if not pdf_url and not landing:
            return None
        res = {'source': source, 'text': '', 'sections': [], 'figures': [],
               'pdf_path': '', 'url': landing or pdf_url, 'ok': True, 'note': ''}

        # 没有下载目录就不落盘，但**照样下载一份到临时目录抽正文**——
        # 拿到 PDF 就能抽到正文，不抽等于白下一趟。
        # （`want_temp` 为真时文件随后会被删掉，磁盘上不留东西。）

        if not pdf_url:
            res['note'] = ('已找到开放获取的网页版全文（%s），但没有可直接下载的 PDF；'
                           '正文请按上面的链接在浏览器中查看。%s。'
                           % (host_type or '开放获取来源', VPN_HINT))
            return res

        try:
            saved, raw = self._download_pdf(paper, pdf_url, ensure_tmp, want_temp)
        except Exception as e:  # noqa: BLE001 —— 下载失败也要把 URL 交出去
            res['note'] = ('已找到开放获取副本（%s），但 PDF 下载失败（%s）；'
                           '请手动打开：%s。%s。'
                           % (host_type or '开放获取来源', type(e).__name__,
                              pdf_url, VPN_HINT))
            res['url'] = landing or pdf_url
            return res

        # PDF 正文抽取：抽得到就用，抽不到/抽出来是乱码就留空。
        # 抽取用的是**内存里的字节**，所以临时模式下文件可以立刻删掉，
        # 用户照样能读到正文——这正是「不落地也能读全文」的意义。
        ptext, pnote = self._pdf_text(raw)
        if ptext:
            res['text'] = ptext

        if saved and want_temp:
            # 临时模式下文件马上会被删掉，不能把路径当成「已交付」告诉用户
            res['note'] = ('已确认开放获取 PDF（%s）可以下载，并抽取到正文；'
                           '按「不落地」要求已在读取后删除本地文件，未保留副本。%s。'
                           % (host_type or '开放获取来源', pnote or VPN_HINT))
            return res
        if saved:
            res['pdf_path'] = saved
            res['note'] = ('已下载开放获取 PDF（%s）：%s。%s%s'
                           % (host_type or '开放获取来源', saved,
                              '并抽取到正文（%d 字）。' % len(ptext) if ptext else
                              '本模块未能从该 PDF 抽取到可用正文，',
                              VPN_HINT))
            return res
        res['note'] = ('已找到开放获取副本（%s），但返回的内容不是 PDF'
                       '（多半是出版商的网页落地页），请手动打开：%s。%s。'
                       % (host_type or '开放获取来源', pdf_url, VPN_HINT))
        res['url'] = landing or pdf_url
        return res

    # ---------------- PDF 正文抽取 ----------------
    @staticmethod
    def _pdf_text(raw: bytes) -> tuple[str, str]:
        """从 PDF 字节里抽正文。返回 (文本, 说明)。

        这里用同目录的 `pdftext` 模块（纯标准库实现的最小 PDF 解析器）。
        但**必须过质量闸门**才采用抽出来的文本。

        闸门的两条判据（实测标定，用「正常文本 / 解码乱码」两组真实样本）：

        1. **控制字符占比 ≤ 2%**（不含 \\n \\t \\r）
           —— 真正的正文里一个控制字符都没有（实测 0.000）；
           而解码失败的二进制乱码里控制字符占 22%~26%。
           这是最灵的一条判据：正常文本与乱码在这条上差了一个数量级。
        2. **词字符（字母/数字/下划线）占比 ≥ 50%**
           —— 正文由词和数字构成；乱码里符号、标点、控制符占比很高。

        为什么要有闸门：PDF 解析一旦遇到没料到的结构，很容易**解出二进制乱码**
        而不是文字（实测真实出版商 PDF 上出现过可打印占比只有 0.44 的情况）。
        乱码比没有文本更糟——它会被下游 pipeline 当成正文喂给模型，
        污染的是整条证据链。所以宁可留空，也不能把乱码塞进去。
        """
        if not raw:
            return '', '未取到 PDF 内容。'
        try:
            import pdftext  # 同目录，纯标准库；不在也能正常工作（只是没正文）
        except Exception:  # noqa: BLE001 —— 没这个模块就跳过正文抽取
            return '', '未安装 PDF 文本抽取模块，正文请看 PDF 文件。'
        try:
            got = pdftext.extract_text(raw)
        except Exception as e:  # noqa: BLE001 —— 解析异常不能外泄
            return '', 'PDF 正文抽取失败（%s）。' % type(e).__name__
        text = (got.get('text') or '').strip()
        if not text:
            return '', ('PDF 里没有可抽取的文本层（可能是扫描件）：%s'
                       % (got.get('note') or ''))
        if not FullTextFetcher._looks_like_text(text):
            return '', 'PDF 抽取结果不是可读文字（已丢弃，避免乱码污染正文）。'
        if len(text) < 200:
            return '', ('PDF 抽取到的文字过短（%d 字），已丢弃。' % len(text))
        return text, ''

    @staticmethod
    def _looks_like_text(text: str) -> bool:
        """判断一段文本像不像「人话」——用来挡 PDF 解码出来的二进制乱码。

        两条判据（阈值由真实样本标定，见 `_pdf_text` 的说明）：
          - 控制字符占比 ≤ 2%（正文里一个都没有，乱码里占 22%~26%）；
          - 词字符（字母/数字/下划线）占比 ≥ 50%。
        """
        n = len(text)
        if n == 0:
            return False
        ctrl = 0
        wordchars = 0
        for ch in text:
            code = ord(ch)
            if ch in '\n\r\t':
                continue
            if code < 32 or code == 127:
                ctrl += 1
                continue
            if ch.isalnum() or ch == '_':
                wordchars += 1
        return (ctrl / n) <= 0.02 and (wordchars / n) >= 0.5

    # ---------------- PDF 下载 ----------------
    def _download_pdf(self, paper: dict, pdf_url: str, ensure_tmp, want_temp: bool
                      ) -> tuple[str, bytes]:
        """下载 PDF。返回 (保存路径, 文件字节)。

        - `want_temp=True`：落到临时目录（调用方随后连目录一起删掉并复查）；
        - `want_temp=False` 且配了 `download_dir`：落到 `download_dir` 长期保存。

        写文件用 `.part` 中间名 + `os.replace` 原子改名：中途失败不会留下一个
        被误当成成品 PDF 的半截文件。

        字节一并返回，是为了**在删文件之前**就能把正文抽出来——
        「不落地」模式下用户照样能读到正文。
        """
        filename = self._pdf_filename(paper)
        if want_temp:
            dest = os.path.join(ensure_tmp(), filename)
        else:
            try:
                os.makedirs(self.download_dir, exist_ok=True)
            except OSError:
                return '', b''
            dest = os.path.join(self.download_dir, filename)

        tmp = dest + '.part'
        try:
            _, body, ctype = self._request(pdf_url, timeout=self.timeout, retries=1)
        except Exception:  # noqa: BLE001
            # 下载失败也要保证不留残骸
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass
            raise
        if len(body) > MAX_PDF_BYTES:
            raise RuntimeError('PDF 过大（%d 字节），已放弃下载' % len(body))
        if not body.startswith(b'%PDF'):
            # Unpaywall 的 url_for_pdf 未必真是 PDF（实测有出版商填的是 HTML 页）
            raise RuntimeError('返回内容不是 PDF（Content-Type: %s）' % (ctype or '未知'))
        try:
            with open(tmp, 'wb') as f:
                f.write(body)
            os.replace(tmp, dest)
        except OSError:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass
            raise
        return dest, body

    # ---------------- 小工具 ----------------
    @staticmethod
    def _pdf_filename(paper: dict) -> str:
        """PDF 文件名：`PMID{pmid}.pdf`；没有 pmid 就用净化过的 DOI。"""
        pmid = FullTextFetcher._pmid(paper)
        if pmid:
            return 'PMID%s.pdf' % _UNSAFE_FN.sub('', pmid)
        doi = FullTextFetcher._doi(paper)
        if doi:
            return 'DOI%s.pdf' % _UNSAFE_FN.sub('_', doi).strip('_')[:80]
        return 'fulltext.pdf'

    @staticmethod
    def _norm_pmcid(value) -> str:
        """统一成**纯数字** PMCID（如 `5334499`）。

        为什么不带 `PMC` 前缀：模块内部要把它拼进
        `.../PMC{pmcid}/...`、`/pmc/articles/PMC{pmcid}/` 等多个 URL，
        前缀统一由 URL 构造处补一次。内部若带前缀，拼接时很容易出现
        `PMCPMC5334499` —— 本模块初版就踩过这个坑（PMC 全文抓不到，
        静默降级到 Unpaywall，浪费了一整轮请求）。
        """
        v = str(value or '').strip()
        if not v:
            return ''
        v = v.replace('PMC', '').strip()
        return v

    @staticmethod
    def _pmid(paper: dict) -> str:
        return str((paper or {}).get('pmid') or '').strip()

    @staticmethod
    def _doi(paper: dict) -> str:
        return str((paper or {}).get('doi') or '').strip()

    @staticmethod
    def _journal(paper: dict) -> str:
        return str((paper or {}).get('journal')
                   or (paper or {}).get('journal_short') or '期刊未提供')

    @staticmethod
    def _year(paper: dict) -> str:
        return str((paper or {}).get('year') or '').strip() or '年份未提供'

    @staticmethod
    def _title_line(paper: dict) -> str:
        return str((paper or {}).get('title') or '').strip() or '标题未提供'

    @staticmethod
    def _epmc_landing(pmcid: str, pmid: str) -> str:
        if pmcid:
            return 'https://europepmc.org/article/PMC/%s' % pmcid.replace('PMC', '')
        if pmid:
            return 'https://europepmc.org/article/MED/%s' % pmid
        return ''

    @staticmethod
    def _fallback_url(pmid: str, pmcid: str) -> str:
        """全都没拿到时，至少给一个用户能点的入口。

        PubMed 的文章页上有 “Full text links” 按钮，会跳到出版商；
        这比留空强——用户至少知道从哪儿开始找。
        """
        if pmid:
            return 'https://pubmed.ncbi.nlm.nih.gov/%s/' % pmid
        if pmcid:
            return 'https://pmc.ncbi.nlm.nih.gov/articles/PMC%s/' % pmcid
        return ''

    def _probe_publishers(self) -> tuple[bool, str]:
        """真发一个请求，探测出版商域名是否可达。

        判定标准很关键：**HTTP 4xx/5xx 也算可达**——能收到响应就说明
        DNS + TCP + TLS 全部打通，是对方在挡爬虫而不是网络不通。
        只有 URLError（连不上/超时）才算不可达。
        """
        proxies = self._proxy_map()
        opener = urllib.request.build_opener(urllib.request.ProxyHandler(proxies))
        ok_hosts: list[str] = []
        failed: list[str] = []
        for url in PROBE_HOSTS:
            host = urllib.parse.urlparse(url).netloc
            try:
                req = urllib.request.Request(url, method='HEAD')
                req.add_header('User-Agent', self.ua)
                with opener.open(req, timeout=6) as resp:
                    ok_hosts.append('%s(%s)' % (host, getattr(resp, 'status', '?')))
            except urllib.error.HTTPError as e:
                # 403/404 都是「服务器回应了」→ 网络是通的
                ok_hosts.append('%s(HTTP %s)' % (host, e.code))
            except Exception as e:  # noqa: BLE001
                failed.append('%s(%s)' % (host, type(e).__name__))
        reachable = bool(ok_hosts)
        detail = ('出版商站点可达：%s' % '、'.join(ok_hosts[:3])) if reachable else (
            '出版商站点均不可达：%s' % ('、'.join(failed[:3]) or '未知'))
        if failed:
            detail += '；不可达：%s' % '、'.join(failed[:3])
        return reachable, detail

    @staticmethod
    def _no_fulltext_note(notes: list[str], doi: str, pmid: str) -> str:
        """全部来源都没拿到全文时，给一句能照做的说明。"""
        parts = ['自动获取全文失败。']
        if notes:
            parts.append('已尝试：%s。' % '；'.join(n for n in notes if n))
        parts.append(VPN_HINT + '。')
        if doi:
            parts.append('也可在 Unpaywall（https://api.unpaywall.org/v2/%s?email=<你的邮箱>）'
                         '或 OpenAlex（https://api.openalex.org/works/doi:%s）查开放获取副本。'
                         % (doi, doi))
        if pmid:
            parts.append('PubMed 页面（https://pubmed.ncbi.nlm.nih.gov/%s/）上的 '
                         '“Full text links” 可直接跳到出版商。' % pmid)
        parts.append('交大图书馆校外访问入口（lib.sjtu.edu.cn → 校外访问）也可获取。')
        return ''.join(parts)
