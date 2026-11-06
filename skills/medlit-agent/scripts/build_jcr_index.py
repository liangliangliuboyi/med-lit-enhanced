# -*- coding: utf-8 -*-
"""JCR 2026 + 中科院分区 标准化期刊索引构建器。

为什么要有这个脚本
------------------
检索结果要标注「影响因子 / 分区」，就需要一份机器可读的期刊索引。
索引必须**由程序生成**：22643 条记录让模型手写必然出错（期刊名缩写错乱、
影响因子串行、漏条），这与本项目「参考文献由脚本生成」的纪律完全一致。

输入（三份 xlsx）
------------------
1. 2026JCR.xlsx                    sheet: Journals
   Rank | Journal name | Abbreviated journal | Publisher | ISSN | eISSN |
   Categories | Editions | 2025 JIF | 5-year JIF
   → 提供 期刊名 / 影响因子 / 学科分类 / 收录体系
2. 2026年JCR期刊分区信息.xlsx        sheet: 2025年影响因子分区
   期刊名称 | ISSN | EISSN | 分区 | IF | 5 Year JIF | 5 Year JIF Quartile
   → 提供 JCR 当年分区
3. 中科院分区.xlsx（可选）           sheet: Sheet1
   Journal | 分区 | Top | Open Access
   → 提供中科院**大区**（1/2/3/4），与 JCR 分位（Q1-Q4）是两套体系

输出
----
assets/jcr_2026_index.json
    {"<期刊名>": {"if": <数字|null>, "quartile": "Q1|Q2|Q3|Q4|null",
                  "cas_zone": <1|2|3|4|null>, "cas_top": <bool|null>,
                  "categories": ["分类", ...], "source": "<文件名>"}}
assets/jcr_2026_issn_map.json
    {"<ISSN>": "<期刊名>"}     ← PubMed 题录对齐用 ISSN 比名字可靠得多

口径决定（改代码前先读这里）
--------------------------
1. **主键用表 A 的 Journal name**（全称）。两表 22643 条按「去空白+忽略大小写」
   一一对应，0 条落单、0 条重名，所以可以安全地按名字合并。
2. **影响因子**：表 A 的 `2025 JIF` 是**文本**，三种形态：
   - "56.1"      → 数字 56.1（整数写成 56，不写 56.0）
   - "<0.1"      → 0.0。JCR 从不发布真正的 0.0（实测源表里 "0.0" 出现 0 次），
                   所以索引里的 0.0 一定来自 "<0.1"，不会与真实 0.0 混淆。
   - "N/A" / 空 → null，表示「没有影响因子」，与 0.0 语义不同。
3. **分区**：表 B 的 `分区`（当年 JCR 分区）。不用 `5 Year JIF Quartile`
   （两者有 4038 本不同），后者适合做趋势分析，不适合做单刊标签。
4. **中科院大区**单独存 `cas_zone`，**绝不与 JCR 分区混在一列**——
   两套体系混读是国内学术评价里最常见的误读来源。
5. **categories**：表 A 的 `Categories`。该列是**单个**分类，逗号是分类名本身的
   一部分（"MEDICINE, GENERAL & INTERNAL"），**不能按逗号切**；
   字面值 "Multiple" 表示"属于多个分类但源表未展开"，原样保留，绝不编造。
6. `source`：一条记录由多份表共同构成，写成 "<表A>+<表B>[+<表C>]"。

零第三方依赖
------------
只用标准库（zipfile + xml.etree.ElementTree）解析 xlsx。和 export.py 手写 OOXML
同一个思路：装了 openpyxl 的机器和没装的机器行为必须一致。

用法
----
    python scripts/build_jcr_index.py \
        --jif   "C:/Users/LZY13/Desktop/2026JCR.xlsx" \
        --quart "C:/Users/LZY13/Desktop/2026年JCR期刊分区信息.xlsx" \
        --cas   "C:/Users/LZY13/Desktop/中科院分区.xlsx" \
        --out   assets/jcr_2026_index.json
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import zipfile
import xml.etree.ElementTree as ET

__all__ = ['build_index', 'load_index', 'main']

_NS = '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'
_RNS = '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}'
QUARTILES = ('Q1', 'Q2', 'Q3', 'Q4')


# ---------------- xlsx 读取（纯标准库）----------------
def _col_index(ref: str) -> int:
    """'BC12' -> 54（0 基列号）。"""
    m = re.match(r'([A-Za-z]+)', ref or '')
    if not m:
        return -1
    n = 0
    for ch in m.group(1).upper():
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def _read_shared_strings(zf: zipfile.ZipFile) -> list[str]:
    try:
        raw = zf.read('xl/sharedStrings.xml')
    except KeyError:
        return []
    out = []
    for si in ET.fromstring(raw).findall(f'{_NS}si'):
        out.append(''.join(t.text or '' for t in si.iter(f'{_NS}t')))
    return out


def _sheet_paths(zf: zipfile.ZipFile) -> dict[str, str]:
    """{工作表名: zip 内路径}。"""
    wb = ET.fromstring(zf.read('xl/workbook.xml'))
    rels = ET.fromstring(zf.read('xl/_rels/workbook.xml.rels'))
    rid2target = {}
    for rel in rels:
        rid, target = rel.get('Id'), rel.get('Target')
        if not rid or not target:
            continue
        if target.startswith('/'):
            target = target.lstrip('/')
        elif not target.startswith('xl/'):
            target = 'xl/' + target
        rid2target[rid] = target
    out = {}
    for sh in ET.fromstring(zf.read('xl/workbook.xml')).find(f'{_NS}sheets'):
        name, rid = sh.get('name'), sh.get(f'{_RNS}id')
        if name and rid in rid2target:
            out[name] = rid2target[rid]
    return out


def _read_sheet(zf: zipfile.ZipFile, path: str, shared: list[str]) -> list[list[str]]:
    """读一个工作表，返回 0 基的 list[list[str]]（缺格是 ''，全表等宽）。"""
    root = ET.fromstring(zf.read(path))
    sd = root.find(f'{_NS}sheetData')
    if sd is None:
        return []
    by_row: dict[int, dict[int, str]] = {}
    max_col, fallback = -1, 0
    for row in sd.findall(f'{_NS}row'):
        r_attr = row.get('r')
        ridx = int(r_attr) - 1 if (r_attr or '').isdigit() else fallback
        fallback = ridx + 1
        cells: dict[int, str] = {}
        for c in row.findall(f'{_NS}c'):
            idx = _col_index(c.get('r') or '')
            if idx < 0:
                continue
            t = c.get('t')
            v = c.find(f'{_NS}v')
            if t == 's':
                try:
                    val = shared[int(v.text)] if (v is not None and v.text) else ''
                except (ValueError, IndexError):
                    val = ''
            elif t == 'inlineStr':
                is_el = c.find(f'{_NS}is')
                val = (''.join(x.text or '' for x in is_el.iter(f'{_NS}t'))
                       if is_el is not None else '')
            elif t == 'b':
                val = 'TRUE' if (v is not None and v.text == '1') else 'FALSE'
            else:
                val = (v.text or '') if v is not None else ''
            cells[idx] = val
            max_col = max(max_col, idx)
        by_row[ridx] = cells
    if not by_row:
        return []
    n_rows = max(by_row) + 1
    return [[by_row.get(r, {}).get(c, '') for c in range(max_col + 1)]
            for r in range(n_rows)]


def _load_table(path: str, sheet_name: str):
    """读一张表，返回 (表头, 数据行)。"""
    if not os.path.isfile(path):
        raise SystemExit(f'找不到文件：{path}')
    with zipfile.ZipFile(path) as zf:
        shared = _read_shared_strings(zf)
        paths = _sheet_paths(zf)
        target = paths.get(sheet_name)
        if target is None:
            raise SystemExit(
                f'在 {path} 里找不到工作表「{sheet_name}」；现有：{list(paths)}')
        rows = _read_sheet(zf, target, shared)
    if not rows:
        raise SystemExit(f'工作表「{sheet_name}」是空的：{path}')
    return [str(h).strip() for h in rows[0]], rows[1:]


def _col(header: list[str], *names: str) -> int:
    """按列名取列号（忽略大小写），取不到直接报错——绝不猜列号。"""
    low = [h.lower() for h in header]
    for n in names:
        if n.lower() in low:
            return low.index(n.lower())
    raise SystemExit(f'列不存在：{names}；现有列：{header}')


# ---------------- 取值规整 ----------------
def _norm(text) -> str:
    return str(text or '').strip().lower()


def _parse_jif(text):
    """JCR 的影响因子文本 → 数字；没有则 None。

    "<0.1" → 0.0（JCR 从不发布真正的 0.0，所以 0.0 一定来自 "<0.1"）
    "N/A" / 空 → None（表示「没有影响因子」，与 0.0 语义不同）
    """
    s = str(text or '').strip()
    if not s or s.upper() == 'N/A':
        return None
    if s.startswith('<'):
        return 0.0
    try:
        v = float(s)
    except ValueError:
        return None
    return int(v) if v.is_integer() else v


# ---------------- 主逻辑 ----------------
def build_index(jif_path: str, quart_path: str, cas_path: str = ''):
    """合并源表，返回 (索引 dict, 统计 dict, ISSN 映射 dict)。"""
    a_header, a_rows = _load_table(jif_path, 'Journals')
    b_header, b_rows = _load_table(quart_path, '2025年影响因子分区')

    a_name = _col(a_header, 'Journal name')
    a_cats = _col(a_header, 'Categories')
    a_jif = _col(a_header, '2025 JIF', 'JIF', 'IF')
    a_issn = _col(a_header, 'ISSN')
    a_eissn = _col(a_header, 'eISSN')
    a_abbr = _col(a_header, 'Abbreviated journal')
    b_name = _col(b_header, '期刊名称', 'Journal name')
    b_quart = _col(b_header, '分区', 'Quartile')
    b_issn = _col(b_header, 'ISSN')
    b_eissn = _col(b_header, 'EISSN', 'eISSN')

    # ---- 表 B：当年 JCR 分区 ----
    quart: dict[str, str | None] = {}
    for row in b_rows:
        name = str(row[b_name] or '').strip()
        if not name:
            continue
        q = str(row[b_quart] or '').strip().upper()
        quart.setdefault(_norm(name), q if q in QUARTILES else None)

    # ---- 表 C（可选）：中科院大区 ----
    cas: dict[str, dict] = {}
    cas_path_used = ''
    if cas_path and os.path.isfile(cas_path):
        c_header, c_rows = _load_table(cas_path, 'Sheet1')
        c_name = _col(c_header, 'Journal', '期刊名称', 'Journal name')
        c_zone = _col(c_header, '分区', 'Zone')
        c_top = _col(c_header, 'Top') if any(
            h.lower() == 'top' for h in c_header) else None
        for row in c_rows:
            name = str(row[c_name] or '').strip()
            if not name:
                continue
            z = str(row[c_zone] or '').strip()
            try:
                zv = int(float(z)) if z else None
            except ValueError:
                zv = None
            top = None
            if c_top is not None:
                tv = str(row[c_top] or '').strip().upper()
                top = True if tv in ('TRUE', '1', 'YES') else (
                    False if tv in ('FALSE', '0', 'NO') else None)
            cas.setdefault(_norm(name), {'zone': zv, 'top': top})
        cas_path_used = os.path.basename(cas_path)

    # ---- 表 A：影响因子 + 学科分类 + ISSN ----
    parts = [os.path.basename(jif_path), os.path.basename(quart_path)]
    if cas_path_used:
        parts.append(cas_path_used)
    source = '+'.join(parts)
    index: dict[str, dict] = {}
    issn_map: dict[str, str] = {}
    abbrev_map: dict[str, str] = {}
    dup = 0
    for row in a_rows:
        name = str(row[a_name] or '').strip()
        if not name:
            continue
        key = _norm(name)
        if key in index:
            dup += 1
            continue
        cats_raw = str(row[a_cats] or '').strip()
        # 该列是单个分类，逗号属于分类名本身；只有分号才是多分类分隔符
        cats = [c.strip() for c in cats_raw.split(';') if c.strip()] if cats_raw else []
        c = cas.get(key, {})
        abbr = str(row[a_abbr] or '').strip()
        issns = [str(row[a_issn] or '').strip(), str(row[a_eissn] or '').strip()]
        if b_issn < len(row):
            issns.append(str(row[b_issn] or '').strip())
        if b_eissn < len(row):
            issns.append(str(row[b_eissn] or '').strip())
        issns = [i for i in issns if i and i.upper() != 'N/A']
        index[name] = {
            'if': _parse_jif(row[a_jif]),
            'quartile': quart.get(key),
            'cas_zone': c.get('zone'),
            'cas_top': c.get('top'),
            'categories': cats,
            # ISSN 与缩写刊名**折进条目本身**：
            # 单独存映射表会多出两个几 MB 的文件，而运行时反正要把它们
            # 建成字典——直接放这里，运行时一次遍历就建完。
            'issn': issns[0] if issns else '',
            'issn_e': issns[1] if len(issns) > 1 else '',
            'abbr': abbr,
        }
        # 缩写刊名单独存映射（运行时做兜底匹配用）
        if abbr and _norm(abbr) != key:
            abbrev_map.setdefault(_norm(abbr), name)
        for i in issns:
            issn_map.setdefault(i, name)

    stats = {
        'entries': len(index),
        'dup_names_dropped': dup,
        'with_if': sum(1 for v in index.values() if v.get('if') is not None),
        'with_quartile': sum(1 for v in index.values()
                              if v.get('quartile') is not None),
        'with_cas_zone': sum(1 for v in index.values()
                              if v.get('cas_zone') is not None),
        'with_both': sum(1 for v in index.values()
                         if v.get('if') is not None and v.get('quartile') is not None),
        'with_neither': sum(1 for v in index.values()
                            if v.get('if') is None and v.get('quartile') is None),
        'unmatched_in_quart_table': sum(
            1 for k in quart if k not in {_norm(n) for n in index}),
        'source': source,
    }
    # `_meta` 放在最前面：来源字符串在每条里重复 22643 次会白白多占几百 KB，
    # 提到顶层就没有这个问题了。
    meta = {'source': source, 'entries': len(index), 'built_from': parts}
    out = {'_meta': meta}
    out.update(index)
    return out, stats, issn_map, abbrev_map


def load_index(path: str) -> dict:
    """运行时读索引。缺文件时返回空 dict（不抛异常，让检索照常跑）。"""
    try:
        with open(path, 'r', encoding='utf-8') as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description='构建 JCR 2026 标准化期刊索引 JSON')
    ap.add_argument('--jif', required=True, help='2026JCR.xlsx 路径')
    ap.add_argument('--quart', required=True, help='2026年JCR期刊分区信息.xlsx 路径')
    ap.add_argument('--cas', default='', help='中科院分区.xlsx 路径（可选）')
    ap.add_argument('--out', required=True, help='输出 JSON 路径')
    ap.add_argument('--with-issn-map', action='store_true',
                    help='同时输出<out 同名>_issn_map.json（ISSN → 期刊名）')
    args = ap.parse_args(argv)

    index, stats, issn_map, abbrev_map = build_index(
        args.jif, args.quart, args.cas)

    parent = os.path.dirname(os.path.abspath(args.out))
    if parent:
        os.makedirs(parent, exist_ok=True)
    # 紧凑写入：这个文件是**生成物**，不需要人读 diff，
    # 缩进 2 会白白多占一倍体积。
    with open(args.out, 'w', encoding='utf-8') as fh:
        json.dump(index, fh, ensure_ascii=False, separators=(',', ':'))

    size = os.path.getsize(args.out)
    print(f'[OK] {args.out}')
    print(f'     条目数 {stats["entries"]}｜文件 {size / 1048576:.2f} MB')
    print(f'     有 IF {stats["with_if"]}｜有 JCR 分区 {stats["with_quartile"]}'
          f'｜有中科院大区 {stats["with_cas_zone"]}')
    print(f'     IF+分区都有 {stats["with_both"]}｜两者都无 {stats["with_neither"]}')
    if stats['dup_names_dropped'] or stats['unmatched_in_quart_table']:
        print(f'     [注意] 合并时丢弃重名 {stats["dup_names_dropped"]} 条，'
              f'分区表落单 {stats["unmatched_in_quart_table"]} 条')

    if args.with_issn_map:
        # 单独的映射文件不再需要——ISSN 与缩写已经折进主索引。
        # 保留这个开关只为兼容旧调用；写出来的是同一份数据的另一种视图。
        base = os.path.splitext(args.out)[0]
        map_path = base + '_issn_map.json'
        with open(map_path, 'w', encoding='utf-8') as fh:
            json.dump(issn_map, fh, ensure_ascii=False, separators=(',', ':'))
        print(f'[OK] {map_path}（{len(issn_map)} 条，可选视图）')
        abbr_path = base + '_abbrev_map.json'
        with open(abbr_path, 'w', encoding='utf-8') as fh:
            json.dump(abbrev_map, fh, ensure_ascii=False, separators=(',', ':'))
        print(f'[OK] {abbr_path}（{len(abbrev_map)} 条，可选视图）')

    # 抽样回读，确认落盘内容与预期一致
    with open(args.out, 'r', encoding='utf-8') as fh:
        back = json.load(fh)
    n_back = len(back) - (1 if '_meta' in back else 0)
    assert n_back == stats['entries'], '落盘后条目数与构建时不一致'
    print(f'[CHECK] 回读校验通过，{n_back} 条')
    for name in list(k for k in back if not k.startswith('_'))[:6]:
        print('       ', json.dumps({name: back[name]}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    sys.exit(main())
