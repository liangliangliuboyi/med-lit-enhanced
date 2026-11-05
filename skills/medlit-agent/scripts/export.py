# -*- coding: utf-8 -*-
"""参考文献与笔记导出：BibTeX / RIS / Zotero 笔记 / 证据表 Markdown / DOCX。

设计要点
--------
1. **纯程序生成，不经过模型**。参考文献必须逐字对应 PubMed 原始字段，
   一旦让模型转写就会出现 PMID 错号、期刊名缩写错乱。所以本模块只做
   "字段搬运 + 格式排版"，不给模型任何介入机会。
2. **零第三方依赖**。DOCX 导出没有用 python-docx，而是用标准库 zipfile
   手写一份最小可用的 OOXML 包（.docx 本质上就是个 zip）。这样用户装完
   Python 就能跑，不需要 pip install 任何东西——这是本项目的硬约束。
3. **缺字段就少一行，绝不写「无」「未提及」占位**。占位符会污染下游的
   证据抽取和 Meta 分析，这里宁可留白。

对外接口
--------
- to_bibtex(papers)          -> str
- to_ris(papers)             -> str
- to_zotero_note(paper, ev)  -> str
- evidence_markdown(papers, ev) -> str
- to_docx(markdown, out_path) -> str
"""
from __future__ import annotations

import html
import os
import re
import zipfile

__all__ = ['to_bibtex', 'to_ris', 'to_zotero_note',
           'evidence_markdown', 'to_docx']

# 题录/证据里的"空"的各种写法，统一按缺失处理
_EMPTY_TOKENS = {'', '无', '未提及', '未知', 'n/a', 'na', 'none', 'null', '-'}


# ---------------- 通用小工具 ----------------
def _s(value) -> str:
    """转成去空白的字符串；None 直接变空串。"""
    if value is None:
        return ''
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def _is_empty(value) -> bool:
    """判断字段是否"缺失"：空串 / None / 未提及 / 破折号等占位写法。"""
    if value is None:
        return True
    if isinstance(value, (list, tuple, set)):
        return not [x for x in value if not _is_empty(x)]
    text = _s(value)
    return text.lower() in _EMPTY_TOKENS


def _lst(value) -> list[str]:
    """列表字段规整成 list[str]，顺手丢掉空项。"""
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        items = list(value)
    else:
        items = [value]
    return [_s(x) for x in items if not _is_empty(x)]


def _join(items, sep='；') -> str:
    return sep.join(_lst(items))


def _first(*values) -> str:
    """取第一个非空值。"""
    for v in values:
        if not _is_empty(v):
            return _s(v)
    return ''


# ---------------- BibTeX ----------------
_BIB_SPECIAL = re.compile(r'([%&_#$])')


def _bib_escape(text: str) -> str:
    """转义 BibTeX/LaTeX 特殊字符。

    必须转义 `%`：它在 .bib 里是注释符，标题里出现 "50%" 会把整条记录
    后面全部吃掉，导出的参考文献直接变残缺。
    """
    if not text:
        return ''
    return _BIB_SPECIAL.sub(r'\\\1', text)


def _bib_key(paper: dict) -> str:
    """生成引用键：firstauthorLastName_year_pmid，例如 Smith2024_12345678。

    规则：
    - 第一作者缺失 → Anon；
    - 姓名里的非字母数字清掉（Smith-Jones → SmithJones，van der Berg → vanderBerg）；
    - 年份缺失 → n.d.；PMID 缺失 → noid。
    """
    authors = _lst(paper.get('authors'))
    surname = 'Anon'
    if authors and authors[0].strip():
        # authors 已是 "Last Initials" 格式，姓氏 = 除末尾缩写外的全部部分。
        # 复姓要连写（van der Berg → vanderBerg），这是 BibTeX 的通行做法。
        parts = re.split(r'\s+', authors[0].strip())
        if len(parts) > 1 and re.fullmatch(r'[A-Z]{1,3}', parts[-1]):
            parts = parts[:-1]          # 去掉末尾的姓名缩写
        surname = ''.join(parts)
        # BibTeX 的引用键必须是纯 ASCII：部分文献管理工具遇到非 ASCII 键会报错
        surname = re.sub(r'[^A-Za-z0-9]', '', surname)
    if not surname:
        surname = 'Anon'
    year = re.sub(r'\D+', '', _s(paper.get('year'))) or 'n.d.'
    pmid = re.sub(r'\D+', '', _s(paper.get('pmid'))) or 'noid'
    return f'{surname}{year}_{pmid}'


def to_bibtex(papers: list[dict]) -> str:
    """生成 BibTeX 参考文献。

    - 条目类型：pub_types 含 'Journal Article' → @article，否则 @misc；
    - 标题大小写原样保留（不 title-case、不 lower），因为标题里的大小写
      往往是作者刻意为之（HbA1c、GLP-1、COVID-19）；
    - doi 为空则不写该字段；PMID 写进 note = {PMID: xxx}。
    """
    blocks = []
    for paper in papers or []:
        if not isinstance(paper, dict):
            continue
        types = {t.lower() for t in _lst(paper.get('pub_types'))}
        entry_type = 'article' if 'journal article' in types else 'misc'

        fields = []
        authors = _lst(paper.get('authors'))
        if authors:
            fields.append(('author', ' and '.join(authors)))
        title = _s(paper.get('title'))
        if title:
            fields.append(('title', title))
        journal = _first(paper.get('journal'), paper.get('journal_short'))
        if journal:
            fields.append(('journal', journal))
        year = _s(paper.get('year'))
        if year:
            fields.append(('year', year))
        doi = _s(paper.get('doi'))
        if doi:
            fields.append(('doi', doi))
        pmid = _s(paper.get('pmid'))
        if pmid:
            fields.append(('note', f'PMID: {pmid}'))

        body = '\n'.join(
            f'  {name} = {{{_bib_escape(value)}}}' for name, value in fields)
        blocks.append(f'@{entry_type}{{{_bib_key(paper)},\n{body}\n}}')
    return '\n\n'.join(blocks)


# ---------------- RIS ----------------
def to_ris(papers: list[dict]) -> str:
    """生成 RIS 格式（EndNote / NoteExpress / Mendeley / Zotero 都能导入）。

    字段顺序固定：TY / AU(每个作者一行) / TI / JO / PY / DO / AB / ID / ER。
    固定顺序而不是"有值才写"，是因为下游按位置解析 RIS 的脚本依赖这个约定；
    没有值时留空行即可。AU 例外——作者数不定，没有作者就不出 AU 行。

    每条记录以 `ER  -` 结尾，记录之间空一行。摘要在 RIS 里是单行字段，
    所以原文换行要压成空格。
    """
    records = []
    for paper in papers or []:
        if not isinstance(paper, dict):
            continue
        lines = ['TY  - JOUR']
        for author in _lst(paper.get('authors')):
            lines.append(f'AU  - {author}')
        lines.append(f'TI  - {_s(paper.get("title"))}')
        lines.append(f'JO  - {_first(paper.get("journal"), paper.get("journal_short"))}')
        lines.append(f'PY  - {_s(paper.get("year"))}')
        lines.append(f'DO  - {_s(paper.get("doi"))}')
        abstract = re.sub(r'\s+', ' ', _s(paper.get('abstract'))).strip()
        lines.append(f'AB  - {abstract}')
        lines.append(f'ID  - {_s(paper.get("pmid"))}')
        lines.append('ER  -')
        records.append('\n'.join(lines))
    return '\n\n'.join(records)


# ---------------- Zotero 笔记 ----------------
def _kv(label: str, value) -> str:
    """"- **标签**：值"；值为空时返回空串（调用方据此整行跳过）。"""
    if _is_empty(value):
        return ''
    if isinstance(value, (list, tuple, set)):
        text = _join(value)
    else:
        text = _s(value)
    if not text:
        return ''
    return f'- **{label}**：{text}'


def to_zotero_note(paper: dict, evidence: dict | None) -> str:
    """生成单篇文献的 Zotero 笔记（Markdown，可直接粘进 Zotero 笔记编辑器）。

    evidence 为 None 时只输出基本信息部分；有 evidence 时按
    研究设计 / 关键结果 / 效应量 / 局限性 / 偏倚评估要素 分节。
    任何缺失字段整行跳过，不留占位符。
    """
    paper = paper if isinstance(paper, dict) else {}
    evidence = evidence if isinstance(evidence, dict) else {}

    title = _first(paper.get('title'), evidence.get('_title')) or '未命名文献'
    lines = [f'## {title}', '']

    # ---- 基本信息（永远输出；evidence=None 时到此为止）----
    lines.append('### 基本信息')
    basic = [
        _kv('PMID', _first(paper.get('pmid'), evidence.get('pmid'))),
        _kv('DOI', paper.get('doi')),
        _kv('期刊', _first(paper.get('journal'), paper.get('journal_short'))),
        _kv('年份', _first(paper.get('year'), evidence.get('_year'))),
        _kv('MeSH 词', paper.get('mesh')),
        _kv('关键词', paper.get('keywords')),
        _kv('出版类型', paper.get('pub_types')),
    ]
    lines.extend(x for x in basic if x)
    lines.append('')

    if not evidence:
        return '\n'.join(lines).rstrip() + '\n'

    # ---- 研究设计 ----
    design = [
        _kv('设计', evidence.get('study_design')),
        _kv('样本量', evidence.get('sample_size')),
        _kv('人群', evidence.get('population')),
        _kv('干预', evidence.get('intervention')),
        _kv('对照', evidence.get('comparator')),
        _kv('主要结局', evidence.get('primary_outcome')),
    ]
    design = [x for x in design if x]
    if design:
        lines.append('### 研究设计')
        lines.extend(design)
        lines.append('')

    # ---- 关键结果 / 效应量 / 局限性：都是列表，一行一条 ----
    for heading, key in (('### 关键结果', 'key_findings'),
                         ('### 效应量', 'effect_sizes'),
                         ('### 局限性', 'limitations')):
        items = _lst(evidence.get(key))
        if not items:
            continue
        lines.append(heading)
        lines.extend(f'- {x}' for x in items)
        lines.append('')

    # ---- 偏倚评估要素（是 dict，按 键：值 展开）----
    bias = evidence.get('bias_assessment')
    if isinstance(bias, dict) and bias:
        bias_lines = []
        for name, value in bias.items():
            if _is_empty(value):
                continue
            if isinstance(value, (list, tuple, set)):
                text = _join(value)
            elif isinstance(value, dict):
                text = '；'.join(f'{k}：{v}' for k, v in value.items())
            else:
                text = _s(value)
            if text:
                bias_lines.append(f'- **{_s(name)}**：{text}')
        if bias_lines:
            lines.append('### 偏倚评估要素')
            lines.extend(bias_lines)
            lines.append('')

    return '\n'.join(lines).rstrip() + '\n'


# ---------------- 证据表 Markdown ----------------
def evidence_markdown(papers: list[dict], evidence: list[dict]) -> str:
    """生成人类可读的证据表（Markdown 管道表格）。

    papers 提供期刊/年份等题录字段（evidence 里可能只有 _journal/_year 这类
    内部字段），evidence 提供设计、样本量、相关度等抽取结果。
    行按 evidence 的顺序（即相关度排序后的顺序）排列。
    """
    by_pmid = {}
    for paper in papers or []:
        if isinstance(paper, dict):
            pmid = _s(paper.get('pmid'))
            if pmid:
                by_pmid[pmid] = paper

    rows = ['| # | PMID | 年份 | 期刊 | 设计 | 样本量 | PICO匹配 | 相关度 | 核心发现 |',
            '|---|------|------|------|------|--------|--------|--------|----------|']
    for i, ev in enumerate(evidence or [], 1):
        if not isinstance(ev, dict):
            continue
        pmid = _first(ev.get('pmid'))
        paper = by_pmid.get(pmid, {})
        year = _first(paper.get('year'), ev.get('_year'))
        journal = _first(paper.get('journal'), paper.get('journal_short'),
                         ev.get('_journal'))
        design = _s(ev.get('study_design'))
        sample = _s(ev.get('sample_size'))
        pico = _s(ev.get('pico_match'))
        relevance = _s(ev.get('relevance'))

        findings = _lst(ev.get('key_findings'))[:2]
        core = '；'.join(findings)[:120].replace('|', '/')

        rows.append(f'| {i} | {pmid} | {year} | {journal} | {design} | '
                    f'{sample} | {pico} | {relevance} | {core} |')
    return '\n'.join(rows)


# ---------------- DOCX（手写 OOXML，零第三方依赖）----------------
# XML 1.0 不允许的控制字符（PubMed 摘要里偶有 \x0b \x0c 之类）
_XML_ILLEGAL = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f]')
_BOLD = re.compile(r'\*\*(.+?)\*\*', re.S)

_CONTENT_TYPES = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
</Types>'''

_ROOT_RELS = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>'''

_DOC_RELS = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>'''

# 默认字体：中文用等线，西文用微软雅黑，避免在没有中文字体的机器上出方框
_STYLES = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
<w:docDefaults>
<w:rPrDefault><w:rPr>
<w:rFonts w:ascii="Microsoft YaHei" w:hAnsi="Microsoft YaHei" w:eastAsia="等线" w:cs="Microsoft YaHei"/>
<w:sz w:val="21"/><w:szCs w:val="21"/>
</w:rPr></w:rPrDefault>
<w:pPrDefault><w:pPr><w:spacing w:after="120" w:line="288" w:lineRule="auto"/></w:pPr></w:pPrDefault>
</w:docDefaults>
<w:style w:type="paragraph" w:default="1" w:styleId="Normal">
<w:name w:val="Normal"/><w:qFormat/>
</w:style>
<w:style w:type="paragraph" w:styleId="Heading1">
<w:name w:val="heading 1"/><w:basedOn w:val="Normal"/><w:next w:val="Normal"/><w:qFormat/>
<w:pPr><w:keepNext/><w:spacing w:before="240" w:after="120"/><w:outlineLvl w:val="0"/></w:pPr>
<w:rPr><w:b/><w:sz w:val="32"/><w:szCs w:val="32"/></w:rPr>
</w:style>
<w:style w:type="paragraph" w:styleId="Heading2">
<w:name w:val="heading 2"/><w:basedOn w:val="Normal"/><w:next w:val="Normal"/><w:qFormat/>
<w:pPr><w:keepNext/><w:spacing w:before="200" w:after="100"/><w:outlineLvl w:val="1"/></w:pPr>
<w:rPr><w:b/><w:sz w:val="28"/><w:szCs w:val="28"/></w:rPr>
</w:style>
<w:style w:type="paragraph" w:styleId="Heading3">
<w:name w:val="heading 3"/><w:basedOn w:val="Normal"/><w:next w:val="Normal"/><w:qFormat/>
<w:pPr><w:keepNext/><w:spacing w:before="160" w:after="80"/><w:outlineLvl w:val="2"/></w:pPr>
<w:rPr><w:b/><w:sz w:val="24"/><w:szCs w:val="24"/></w:rPr>
</w:style>
<w:style w:type="paragraph" w:styleId="ListParagraph">
<w:name w:val="List Paragraph"/><w:basedOn w:val="Normal"/><w:qFormat/>
<w:pPr><w:spacing w:after="60"/><w:ind w:left="420" w:hanging="210"/></w:pPr>
</w:style>
</w:styles>'''


def _xesc(text: str) -> str:
    """转义 XML 特殊字符 & < >，并剔除 XML 非法控制字符。"""
    if not text:
        return ''
    return html.escape(_XML_ILLEGAL.sub('', text), quote=False)


def _runs(text: str) -> str:
    """把一行文字转成 w:r 序列，并识别 **粗体**。

    只处理粗体：这是本项目自己产出的 Markdown 里唯一会用到的行内标记，
    再多就是过度设计。
    """
    out = []
    pos = 0
    for m in _BOLD.finditer(text):
        if m.start() > pos:
            out.append(f'<w:r><w:t xml:space="preserve">{_xesc(text[pos:m.start()])}</w:t></w:r>')
        out.append(f'<w:r><w:rPr><w:b/></w:rPr>'
                   f'<w:t xml:space="preserve">{_xesc(m.group(1))}</w:t></w:r>')
        pos = m.end()
    if pos < len(text):
        out.append(f'<w:r><w:t xml:space="preserve">{_xesc(text[pos:])}</w:t></w:r>')
    return ''.join(out)


def _paragraph(style: str | None, content: str) -> str:
    ppr = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ''
    return f'<w:p>{ppr}{content}</w:p>'


def _md_to_paragraphs(markdown_text: str) -> str:
    """Markdown 行 → OOXML 段落。

    映射规则：
    - `# / ## / ###` → Heading1/2/3
    - `- ` 开头 → 列表样式段落（缩进 + 项目符号字符，不依赖 numbering.xml）
    - `| a | b |` → 用制表符分隔的段落（真正的 Word 表格要 numbering/tbl 一大套，
      性价比不高；用制表位排版在 Word 里照样能对齐）
    - 其余 → 普通段落
    """
    out = []
    for raw in (markdown_text or '').split('\n'):
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped:
            continue

        heading = re.match(r'^(#{1,3})\s+(.*)$', stripped)
        if heading:
            level = len(heading.group(1))
            out.append(_paragraph(f'Heading{level}', _runs(heading.group(2))))
            continue

        bullet = re.match(r'^[-*+]\s+(.*)$', stripped)
        if bullet:
            # 用 • 字符模拟项目符号：不需要 numbering.xml，Word 照样显示成列表
            content = (f'<w:r><w:t xml:space="preserve">• </w:t></w:r>'
                       + _runs(bullet.group(1)))
            out.append(_paragraph('ListParagraph', content))
            continue

        if stripped.startswith('|') and stripped.endswith('|'):
            cells = [c.strip() for c in stripped.strip('|').split('|')]
            # 表格分隔行（|---|---|）只是 Markdown 的表头装饰，转成 Word 后没有意义
            if cells and all(re.fullmatch(r':?-{2,}:?', c) for c in cells):
                continue
            pieces = []
            for i, cell in enumerate(cells):
                if i:
                    pieces.append('<w:r><w:tab/></w:r>')
                pieces.append(_runs(cell))
            out.append(_paragraph(None, ''.join(pieces)))
            continue

        out.append(_paragraph(None, _runs(stripped)))
    return ''.join(out)


def to_docx(markdown_text: str, out_path: str) -> str:
    """把 Markdown 转成 .docx 写盘，返回 out_path。

    实现方式：**手写最小 OOXML 包**。因为本项目是零第三方依赖的，装了
    python-docx 的机器和没装的机器行为就不一致，所以这里用标准库 zipfile
    直接拼出 Word 能打开的 OOXML：
    - [Content_Types].xml  声明各部件的 MIME 类型
    - _rels/.rels           根关系，指向 word/document.xml
    - word/_rels/document.xml.rels  文档关系，指向 styles.xml
    - word/document.xml     正文
    - word/styles.xml       样式与字体（中文不乱码的关键在这里）

    写完用 ZipFile.testzip() 自检一次，损坏的包不留给用户。
    """
    body = _md_to_paragraphs(markdown_text)
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        '<w:body>' + body +
        '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/>'
        '<w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440"'
        ' w:header="851" w:footer="992" w:gutter="0"/></w:sectPr>'
        '</w:body></w:document>')

    parent = os.path.dirname(os.path.abspath(out_path))
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)

    with zipfile.ZipFile(out_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        zf.writestr('[Content_Types].xml', _CONTENT_TYPES)
        zf.writestr('_rels/.rels', _ROOT_RELS)
        zf.writestr('word/_rels/document.xml.rels', _DOC_RELS)
        zf.writestr('word/styles.xml', _STYLES)
        zf.writestr('word/document.xml', document)

    # 自检：zip 结构必须完整，否则用户打开才发现文件坏了
    with zipfile.ZipFile(out_path) as zf:
        if zf.testzip() is not None:
            raise RuntimeError(f'DOCX 写入后校验失败：{out_path}')
    return out_path
