# -*- coding: utf-8 -*-
"""export 模块单元测试（标准库 unittest，不依赖网络）。

运行：
    python -m unittest discover -s tests -v
    python tests/test_export.py
"""
from __future__ import annotations

import os
import re
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile

# 把 scripts 目录挂到 sys.path，才能 import export
_SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        'skills', 'medlit-agent', 'scripts')
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

import export  # noqa: E402


PAPER = {
    'pmid': '12345678',
    'title': 'Glucagon-like Peptide-1 Receptor Agonists for Obesity in Adults.',
    'abstract': 'Background\nObjective\nResults\nConclusions',
    'authors': ['Smith J', 'Doe AB'],
    'journal': 'N Engl J Med',
    'journal_short': 'N Engl J Med',
    'year': '2024',
    'doi': '10.1056/NEJMoa12345678',
    'pmcid': '12345678',
    'mesh': ['Diabetes Mellitus, Type 2'],
    'keywords': ['obesity', 'GLP-1'],
    'pub_types': ['Randomized Controlled Trial', 'Journal Article'],
}

EVIDENCE = {
    'pmid': '12345678',
    'study_design': 'RCT',
    'sample_size': 'n=3117',
    'population': 'Adults with obesity',
    'intervention': 'Semaglutide 2.4 mg weekly',
    'comparator': 'Placebo',
    'primary_outcome': 'Change in body weight at 68 weeks',
    'key_findings': ['Mean weight loss 14.9% vs 2.4%',
                     'Adverse events 57.5% vs 31.7%',
                     'Third finding not shown'],
    'effect_sizes': ['RR 1.24 (95%CI 1.10-1.40)'],
    'limitations': ['Open-label, no blinding'],
    'relevance': 5,
    'bias_assessment': {'随机化': '正确', '分配隐藏': '不充分'},
    'pico_match': '直接',
    '_journal': 'N Engl J Med',
    '_year': '2024',
    '_title': 'Glucagon-like Peptide-1 Receptor Agonists for Obesity in Adults.',
}


class TestBibtex(unittest.TestCase):
    def test_key_format(self):
        bib = export.to_bibtex([PAPER])
        self.assertIn('@article{Smith2024_12345678,', bib)

    def test_key_missing_author_is_anon(self):
        p = dict(PAPER)
        p['authors'] = []
        self.assertIn('@article{Anon2024_12345678,', export.to_bibtex([p]))

    def test_key_strips_non_alnum_from_surname(self):
        p = dict(PAPER)
        p['authors'] = ["O'Brien-Müller J"]
        self.assertIn('@article{OBrienMller2024_12345678,', export.to_bibtex([p]))

    def test_key_uses_lastname_first_token(self):
        p = dict(PAPER)
        p['authors'] = ['van der Berg JJ']
        self.assertIn('@article{vanderBerg2024_12345678,', export.to_bibtex([p]))

    def test_article_type_from_pub_types(self):
        self.assertIn('@article{', export.to_bibtex([PAPER]))

    def test_misc_type_when_not_journal_article(self):
        p = dict(PAPER)
        p['pub_types'] = ['Review']
        self.assertIn('@misc{', export.to_bibtex([p]))

    def test_fields(self):
        bib = export.to_bibtex([PAPER])
        self.assertIn('author = {Smith J and Doe AB}', bib)
        self.assertIn(f'title = {{{PAPER["title"]}}}', bib)
        self.assertIn('journal = {N Engl J Med}', bib)
        self.assertIn('year = {2024}', bib)
        self.assertIn('doi = {10.1056/NEJMoa12345678}', bib)
        self.assertIn('note = {PMID: 12345678}', bib)

    def test_title_case_preserved(self):
        """标题大小写必须原样保留——GLP-1、GLP-1 这类不能被转成小写。"""
        bib = export.to_bibtex([PAPER])
        self.assertIn('Glucagon-like Peptide-1 Receptor Agonists', bib)
        self.assertNotIn('glucagon-like peptide-1', bib)

    def test_doi_omitted_when_empty(self):
        p = dict(PAPER)
        p['doi'] = ''
        bib = export.to_bibtex([p])
        self.assertNotIn('doi =', bib)

    def test_multiple_papers_separated(self):
        p2 = dict(PAPER, pmid='87654321', year='2023')
        bib = export.to_bibtex([PAPER, p2])
        self.assertIn('@article{Smith2024_12345678,', bib)
        self.assertIn('@article{Smith2023_87654321,', bib)

    def test_empty_input(self):
        self.assertEqual(export.to_bibtex([]), '')


class TestRis(unittest.TestCase):
    def test_field_order(self):
        ris = export.to_ris([PAPER])
        lines = [ln for ln in ris.split('\n') if ln.strip()]
        # RIS 的每行形如 "TY  - JOUR"，标签是两个大写字母
        tags = [re.match(r'^([A-Z]{2})', ln).group(1) for ln in lines]
        # 期望顺序：TY, AU, AU, TI, JO, PY, DO, AB, ID, ER
        self.assertEqual(tags, ['TY', 'AU', 'AU', 'TI', 'JO', 'PY', 'DO', 'AB', 'ID', 'ER'])

    def test_ends_with_er(self):
        ris = export.to_ris([PAPER])
        self.assertTrue(ris.rstrip('\n').endswith('ER  -'))

    def test_blank_line_between_records(self):
        p2 = dict(PAPER, pmid='87654321')
        ris = export.to_ris([PAPER, p2])
        self.assertIn('ER  -\n\nTY  - JOUR', ris)
        self.assertEqual(ris.count('TY  - JOUR'), 2)

    def test_abstract_newlines_flattened(self):
        ris = export.to_ris([PAPER])
        for line in ris.split('\n'):
            if line.startswith('AB  - '):
                self.assertNotIn('\n', line)
                self.assertIn('Background Objective Results Conclusions', line)
                break
        else:
            self.fail('没有找到 AB 行')

    def test_author_one_per_line(self):
        ris = export.to_ris([PAPER])
        self.assertIn('AU  - Smith J', ris)
        self.assertIn('AU  - Doe AB', ris)

    def test_field_order_stable_when_fields_missing(self):
        """字段顺序必须固定：缺 DOI 时 DO 行仍然要在，位置不变。"""
        p = dict(PAPER)
        p['doi'] = ''
        ris = export.to_ris([p])
        lines = [ln for ln in ris.split('\n') if ln.strip()]
        tags = [re.match(r'^([A-Z]{2})', ln).group(1) for ln in lines]
        self.assertEqual(tags, ['TY', 'AU', 'AU', 'TI', 'JO', 'PY', 'DO', 'AB', 'ID', 'ER'])

    def test_empty_input(self):
        self.assertEqual(export.to_ris([]), '')


class TestZoteroNote(unittest.TestCase):
    def test_none_evidence_does_not_crash(self):
        note = export.to_zotero_note(PAPER, None)
        self.assertIsInstance(note, str)
        self.assertIn('### 基本信息', note)
        self.assertNotIn('### 研究设计', note)
        self.assertNotIn('### 关键结果', note)

    def test_none_evidence_has_basic_info(self):
        note = export.to_zotero_note(PAPER, None)
        self.assertIn('PMID', note)
        self.assertIn('10.1056/NEJMoa12345678', note)
        self.assertIn('N Engl J Med', note)
        self.assertIn('2024', note)

    def test_title_first_line(self):
        note = export.to_zotero_note(PAPER, None)
        self.assertTrue(note.startswith('## '))
        self.assertIn(PAPER['title'], note.split('\n')[0])

    def test_sections_present_with_evidence(self):
        note = export.to_zotero_note(PAPER, EVIDENCE)
        for head in ('### 基本信息', '### 研究设计', '### 关键结果',
                     '### 效应量', '### 局限性', '### 偏倚评估要素'):
            self.assertIn(head, note)

    def test_missing_field_skipped_not_placeholder(self):
        """缺失字段整行跳过，不允许出现「无」「未提及」这类占位。"""
        ev = dict(EVIDENCE)
        del ev['sample_size']
        del ev['limitations']
        note = export.to_zotero_note(PAPER, ev)
        self.assertNotIn('样本量', note)
        self.assertNotIn('未提及', note)
        self.assertNotIn('局限性', note)

    def test_internal_underscore_fields_not_leaked(self):
        note = export.to_zotero_note(PAPER, EVIDENCE)
        self.assertNotIn('_journal', note)
        self.assertNotIn('_year', note)
        self.assertNotIn('_title', note)

    def test_empty_paper_does_not_crash(self):
        note = export.to_zotero_note({}, None)
        self.assertIsInstance(note, str)
        self.assertIn('### 基本信息', note)


class TestEvidenceMarkdown(unittest.TestCase):
    def test_header_and_separator(self):
        md = export.evidence_markdown([PAPER], [EVIDENCE])
        lines = md.split('\n')
        self.assertTrue(lines[0].startswith('| # | PMID | 年份 | 期刊 | 设计 | 样本量 |'))
        self.assertIn('PICO匹配', lines[0])
        self.assertIn('相关度', lines[0])
        self.assertIn('核心发现', lines[0])
        self.assertTrue(lines[1].startswith('|---'))

    def test_pipe_in_findings_replaced(self):
        """发现里的 | 必须换成 /，否则会撑破 Markdown 表格。"""
        ev = dict(EVIDENCE)
        ev['key_findings'] = ['A | B', 'C | D']
        md = export.evidence_markdown([PAPER], [ev])
        body = md.split('\n')[2]
        self.assertNotIn('| B', body)
        self.assertIn('A / B', body)
        self.assertIn('C / D', body)
        # 每一行的竖线数量必须一致（表头 9 列）
        self.assertEqual(body.count('|'), 10)

    def test_only_first_two_findings(self):
        md = export.evidence_markdown([PAPER], [EVIDENCE])
        self.assertIn('Mean weight loss 14.9% vs 2.4%', md)
        self.assertIn('Adverse events 57.5% vs 31.7%', md)
        self.assertNotIn('Third finding not shown', md)

    def test_findings_truncated_to_120(self):
        ev = dict(EVIDENCE)
        ev['key_findings'] = ['x' * 200]
        md = export.evidence_markdown([PAPER], [ev])
        cell = md.split('\n')[2].split('|')[-2].strip()
        self.assertEqual(len(cell), 120)

    def test_pico_match_column(self):
        md = export.evidence_markdown([PAPER], [EVIDENCE])
        self.assertIn('直接', md)

    def test_pico_match_empty_when_absent(self):
        ev = dict(EVIDENCE)
        del ev['pico_match']
        md = export.evidence_markdown([PAPER], [ev])
        cells = md.split('\n')[2].split('|')
        # 列序：#, PMID, 年份, 期刊, 设计, 样本量, PICO匹配, 相关度, 核心发现
        self.assertEqual(cells[7].strip(), '')

    def test_row_numbering(self):
        p2 = dict(PAPER, pmid='87654321')
        ev2 = dict(EVIDENCE, pmid='87654321')
        md = export.evidence_markdown([PAPER, p2], [EVIDENCE, ev2])
        self.assertIn('| 1 | 12345678 |', md)
        self.assertIn('| 2 | 87654321 |', md)

    def test_empty_evidence(self):
        md = export.evidence_markdown([PAPER], [])
        self.assertEqual(len(md.split('\n')), 2)


class TestDocx(unittest.TestCase):
    def test_creates_valid_zip(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, 'out.docx')
            result = export.to_docx('# 标题\n\n正文一段。', out)
            self.assertEqual(result, out)
            self.assertTrue(os.path.exists(out))
            self.assertTrue(zipfile.is_zipfile(out))
            with zipfile.ZipFile(out) as zf:
                self.assertIsNone(zf.testzip())

    def test_required_parts_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, 'parts.docx')
            export.to_docx('hello', out)
            with zipfile.ZipFile(out) as zf:
                names = set(zf.namelist())
            for part in ('[Content_Types].xml', '_rels/.rels', 'word/document.xml',
                         'word/styles.xml'):
                self.assertIn(part, names)

    def test_all_xml_parts_well_formed(self):
        """每个 XML 部件都必须能被解析器读懂——testzip() 只能验字节，验不了标签。"""
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, 'wf.docx')
            export.to_docx('# 标题\n\n正文 & <标签>\n\n- 条目\n', out)
            with zipfile.ZipFile(out) as zf:
                for name in zf.namelist():
                    if name.endswith('.xml') or name.endswith('.rels'):
                        try:
                            ET.fromstring(zf.read(name))
                        except ET.ParseError as e:
                            self.fail(f'{name} 不是合法 XML：{e}')

    def test_content_types_declares_parts(self):
        """[Content_Types].xml 必须声明 document.xml 与 styles.xml，否则 Word 打不开。"""
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, 'ct.docx')
            export.to_docx('x', out)
            with zipfile.ZipFile(out) as zf:
                ct = zf.read('[Content_Types].xml').decode('utf-8')
        self.assertIn('/word/document.xml', ct)
        self.assertIn('/word/styles.xml', ct)
        self.assertIn('Extension="rels"', ct)

    def test_headings_map_to_styles(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, 'h.docx')
            export.to_docx('# H1\n## H2\n### H3\n', out)
            with zipfile.ZipFile(out) as zf:
                doc = zf.read('word/document.xml').decode('utf-8')
        self.assertIn('Heading1', doc)
        self.assertIn('Heading2', doc)
        self.assertIn('Heading3', doc)

    def test_special_chars_escaped(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, 'esc.docx')
            export.to_docx('a & b < c > d', out)
            with zipfile.ZipFile(out) as zf:
                doc = zf.read('word/document.xml').decode('utf-8')
        self.assertIn('&amp;', doc)
        self.assertIn('&lt;', doc)
        self.assertIn('&gt;', doc)
        self.assertNotIn('< c >', doc)

    def test_chinese_font_in_styles(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, 'font.docx')
            export.to_docx('测试', out)
            with zipfile.ZipFile(out) as zf:
                styles = zf.read('word/styles.xml').decode('utf-8')
        self.assertIn('Microsoft YaHei', styles)
        self.assertIn('等线', styles)

    def test_list_and_table_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, 'lt.docx')
            export.to_docx('- 条目一\n- 条目二\n\n| a | b |\n', out)
            with zipfile.ZipFile(out) as zf:
                doc = zf.read('word/document.xml').decode('utf-8')
        self.assertIn('ListParagraph', doc)
        self.assertIn('<w:tab/>', doc)

    def test_empty_markdown_still_valid(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, 'empty.docx')
            export.to_docx('', out)
            with zipfile.ZipFile(out) as zf:
                self.assertIsNone(zf.testzip())

    def test_creates_parent_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, 'sub', 'dir', 'o.docx')
            export.to_docx('x', out)
            self.assertTrue(os.path.exists(out))


if __name__ == '__main__':
    unittest.main(verbosity=2)
