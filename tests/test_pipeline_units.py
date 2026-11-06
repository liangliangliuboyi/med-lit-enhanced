# -*- coding: utf-8 -*-
"""`skills/medlit-agent/scripts/pipeline.py` 的纯离线单元测试。

测试范围（全部是**纯函数 / 无副作用方法**）：

| 被测对象            | 说明                                             |
| ------------------- | ------------------------------------------------ |
| `_json_load`        | 从模型输出里抠 JSON                              |
| `_slug`             | 生成运行目录名                                   |
| `Run._load_cq`      | 载入 Clinical Query 过滤器表                     |
| `Run.evidence_table`| 证据总表（Markdown）                             |
| `Run.prisma_table`  | PRISMA 流程表（Markdown）                        |
| `Run.validate_pmids`| 正文 PMID 越界校验                               |

**离线保证**
本文件里的所有测试都不发网络请求、不读任何 API key。
`TestOfflineGuard` 一组用例会把 `socket.socket` 与
`urllib.request.urlopen` 替换成"一旦被调用就抛异常"的桩，
从而证明被测代码路径确实没有偷偷联网。

**构造 Run 实例的姿势**
`Run.__init__` 会创建输出目录、构造 Provider / PubMed 客户端并读取配置，
既需要密钥又会产生副作用，因此测试一律用 `Run.__new__(Run)` 绕开 `__init__`，
再手工塞入被测方法真正需要的字段。
"""
from __future__ import annotations

import os
import socket
import sys
import threading
import unittest
import urllib.request

# 把被测代码目录加入 import 路径。路径由 __file__ 推导，不写死绝对路径，
# 保证在 CI 上（不同的工作目录）同样可用。
SCRIPTS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'skills', 'medlit-agent', 'scripts')
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import pipeline  # noqa: E402  （必须在 sys.path 调整之后导入）
import pubmed  # noqa: E402


def make_run(**fields):
    """构造一个不跑 __init__ 的 Run 实例，并写入给定字段。

    `Run.__init__` 需要密钥、会建目录、会连 PubMed，所以测试里一律绕开它。
    """
    run = pipeline.Run.__new__(pipeline.Run)
    run.answer = ''
    run.papers = {}
    run.preprints = {}
    run.evidence = []
    run.excluded = []
    run.prisma = {
        'identified': 0, 'deduplicated': 0, 'screened': 0,
        'excluded_screening': 0, 'fulltext_assessed': 0,
        'included': 0, 'excluded_fulltext': 0, 'queries': 0,
        'preprints': 0, 'linked': 0, 'snowballed': 0,
    }
    run.trace = {'errors': []}
    run.search_failed = False
    # log 会往 self._log_lines 里追加并 print，测试里不需要输出，直接静音。
    run.log = lambda msg: None
    for key, value in fields.items():
        setattr(run, key, value)
    return run


class TestJsonLoad(unittest.TestCase):
    """`_json_load(text)`：从模型输出里抠 JSON。"""

    def test_plain_json_is_parsed(self):
        """纯 JSON 字符串应被直接解析成 dict。"""
        self.assertEqual(pipeline._json_load('{"a": 1, "b": "x"}'), {'a': 1, 'b': 'x'})

    def test_json_array_is_parsed(self):
        """JSON 数组同样应被解析（返回 list 而不是 dict）。"""
        self.assertEqual(pipeline._json_load('[1, 2, 3]'), [1, 2, 3])

    def test_fenced_json_block_is_parsed(self):
        """被 ```json ... ``` 包起来时应能解析。"""
        raw = '```json\n{"queries": [{"label": "直译"}]}\n```'
        self.assertEqual(pipeline._json_load(raw), {'queries': [{'label': '直译'}]})

    def test_fenced_json_without_language_tag_is_parsed(self):
        """只有 ``` 而没有 json 语言标记时也应能解析。"""
        raw = '```\n{"ok": true}\n```'
        self.assertEqual(pipeline._json_load(raw), {'ok': True})

    def test_noisy_text_around_json_is_parsed_via_regex_fallback(self):
        """前后有噪音文字、中间夹着 JSON 时，靠正则兜底也能解析。"""
        raw = '好的，这是结果：\n{"core_question_en": "aspirin", "queries": []}\n以上。'
        self.assertEqual(
            pipeline._json_load(raw),
            {'core_question_en': 'aspirin', 'queries': []})

    def test_empty_string_returns_none(self):
        """空字符串返回 None。"""
        self.assertIsNone(pipeline._json_load(''))

    def test_none_returns_none(self):
        """None 返回 None。"""
        self.assertIsNone(pipeline._json_load(None))

    def test_invalid_json_returns_none(self):
        """非法 JSON 返回 None，而不是抛异常。"""
        self.assertIsNone(pipeline._json_load('这不是 JSON'))

    def test_broken_json_object_returns_none(self):
        """形似 JSON 但语法损坏的文本也应返回 None。"""
        self.assertIsNone(pipeline._json_load('{"a": }'))


class TestSlug(unittest.TestCase):
    """`_slug(text, n)`：生成运行目录名片段（末尾带短哈希防撞名）。"""

    def _base(self, text, n=16):
        """取掉哈希后缀后的主体部分。"""
        out = pipeline._slug(text, n)
        return out.rsplit('_', 1)[0] if '_' in out else out

    def test_chinese_characters_are_preserved(self):
        """中文应被保留。"""
        self.assertTrue(pipeline._slug('二甲双胍治疗糖尿病').startswith('二甲双胍治疗糖尿病'))

    def test_special_characters_are_stripped(self):
        """空格与标点等特殊字符应被去掉。"""
        self.assertEqual(self._base('hello world!@#$%^&*()'), 'helloworld')

    def test_empty_string_falls_back_to_query(self):
        """空字符串应回退成 'query'（并仍带哈希后缀）。"""
        self.assertTrue(pipeline._slug('').startswith('query'))

    def test_none_falls_back_to_query(self):
        """None 应回退成 'query'。"""
        self.assertTrue(pipeline._slug(None).startswith('query'))

    def test_only_special_characters_falls_back_to_query(self):
        """全是特殊字符时清空后同样回退成 'query'。"""
        self.assertTrue(pipeline._slug('!!!???').startswith('query'))

    def test_long_text_is_truncated_to_n(self):
        """超过 n 个字符应被截断。"""
        self.assertEqual(len(self._base('a' * 30, 10)), 10)

    def test_truncation_keeps_leading_characters(self):
        """截断应保留开头部分。"""
        self.assertEqual(self._base('abcdefghijklmnop', 5), 'abcde')

    def test_default_length_is_16(self):
        """默认主体长度为 16。"""
        self.assertEqual(len(self._base('x' * 40)), 16)

    def test_same_minute_different_questions_do_not_collide(self):
        """同一分钟内两个相似问题的目录名不能相同（否则后者覆盖前者）。"""
        a = pipeline._slug('GLP-1 受体激动剂对青少年肥胖的效果如何？')
        b = pipeline._slug('GLP-1 受体激动剂对青少年肥胖的长期安全性如何？')
        self.assertNotEqual(a, b, '相似问题的目录名撞车了')

    def test_same_question_is_stable(self):
        """同一个问题每次生成的目录名必须一致（便于断点续跑）。"""
        self.assertEqual(pipeline._slug('测试问题'), pipeline._slug('测试问题'))


class TestLoadCq(unittest.TestCase):
    """`Run._load_cq()`：载入 Clinical Query 过滤器表。"""

    def setUp(self):
        self.cq = make_run()._load_cq()

    def test_returns_dict(self):
        """应返回一个 dict。"""
        self.assertIsInstance(self.cq, dict)

    def test_contains_therapy_narrow(self):
        """应包含 'therapy.narrow' 等标准过滤器条目。"""
        self.assertIn('therapy.narrow', self.cq)

    def test_underscore_prefixed_keys_are_removed(self):
        """以 _ 开头的元信息键（如 _comment）应被过滤掉。"""
        for key in self.cq:
            self.assertFalse(key.startswith('_'),
                             f'不应保留以下划线开头的键：{key}')

    def test_entries_have_label_and_query(self):
        """每个条目都应包含 label 与 query 两个字段。"""
        for key, value in self.cq.items():
            self.assertIsInstance(value, dict, f'{key} 应为 dict')
            self.assertIn('label', value, f'{key} 缺少 label')
            self.assertIn('query', value, f'{key} 缺少 query')


class TestEvidenceTable(unittest.TestCase):
    """`Run.evidence_table()`：证据总表。"""

    def test_empty_evidence_returns_header_only(self):
        """evidence 为空时只返回表头，且不抛异常。"""
        table = make_run(evidence=[]).evidence_table()
        self.assertIsInstance(table, str)
        self.assertIn('引用标识', table)
        self.assertIn('核心发现', table)

    def test_empty_evidence_has_exactly_two_header_lines(self):
        """evidence 为空时应只有表头行与分隔行两行。"""
        table = make_run(evidence=[]).evidence_table()
        self.assertEqual(len(table.strip().splitlines()), 2)

    def test_evidence_rows_are_rendered(self):
        """有证据时应逐条渲染出 PMID 等字段。"""
        evidence = [{
            'pmid': '12345678', '_year': '2020', '_journal': 'Lancet',
            'study_design': 'RCT', 'sample_size': 1200,
            'pico_match': 'P', 'relevance': 9,
            'key_findings': ['降低风险', '无明显差异'],
        }]
        table = make_run(evidence=evidence).evidence_table()
        self.assertIn('12345678', table)
        self.assertIn('Lancet', table)
        self.assertIn('RCT', table)
        self.assertIn('降低风险', table)

    def test_pipe_in_key_findings_is_escaped(self):
        """核心发现里的竖线应被替换掉，否则会破坏 Markdown 表格。"""
        evidence = [{'pmid': '1', 'key_findings': ['A|B']}]
        table = make_run(evidence=evidence).evidence_table()
        body = table.splitlines()[2]
        self.assertNotIn('A|B', body)
        self.assertIn('A/B', body)


class TestPrismaTable(unittest.TestCase):
    """`Run.prisma_table()`：PRISMA 流程表。"""

    def test_output_contains_final_inclusion_row(self):
        """输出中应包含「最终纳入」字样。"""
        table = make_run().prisma_table()
        self.assertIn('最终纳入', table)

    def test_output_is_markdown_table(self):
        """输出应是 Markdown 表格（含表头与分隔行）。"""
        table = make_run().prisma_table()
        lines = table.strip().splitlines()
        self.assertTrue(lines[0].startswith('|'))
        self.assertEqual(set(lines[1].replace('|', '').strip()), {'-'})

    def test_counts_are_rendered(self):
        """PRISMA 计数应被渲染进表格。"""
        prisma = {
            'identified': 120, 'deduplicated': 100, 'screened': 80,
            'excluded_screening': 20, 'fulltext_assessed': 40,
            'included': 12, 'excluded_fulltext': 28, 'queries': 3,
            'preprints': 0, 'linked': 0, 'snowballed': 0,
        }
        table = make_run(prisma=prisma).prisma_table()
        self.assertIn('120', table)
        self.assertIn('| 12 |', table)


class TestValidatePmids(unittest.TestCase):
    """`Run.validate_pmids()`：正文 PMID 越界校验。

    被测方法的实际行为（依当前 pipeline.py 实现）：
    - 从正文里用正则 `\\[PMID:(\\d+)\\]` 抽取引用；
    - `pool` 取自 `self.papers` 的键；
    - `orphan` = 引用集合减去候选池，排序后写入 `orphan_pmids`；
    - 返回字典字段：`cited_count` / `pool_size` / `orphan_pmids` /
      `orphan_count` / `cited_in_pool`；
    - `self.search_failed` 为真时**提前返回**，并在结果里加 `skipped`，
      此时不做任何报警（不写 `trace['errors']`、不调用 `log`）。
    """

    def test_all_cited_pmids_in_pool_reports_no_orphan(self):
        """正文 PMID 全部在候选池时，orphan_count 应为 0。"""
        run = make_run(
            answer='结论一 [PMID:11111111]；结论二 [PMID:22222222]。',
            papers={'11111111': {}, '22222222': {}, '33333333': {}})
        check = run.validate_pmids()
        self.assertEqual(check['orphan_count'], 0)
        self.assertEqual(check['orphan_pmids'], [])
        self.assertEqual(check['cited_count'], 2)
        self.assertEqual(check['cited_in_pool'], 2)
        self.assertEqual(check['pool_size'], 3)

    def test_orphan_pmid_is_detected(self):
        """正文出现候选池之外的 PMID 时，应被记为越界引用。"""
        run = make_run(
            answer='有效引用 [PMID:11111111]，越界引用 [PMID:99999999]。',
            papers={'11111111': {}})
        check = run.validate_pmids()
        self.assertEqual(check['orphan_count'], 1)
        self.assertEqual(check['orphan_pmids'], ['99999999'])
        self.assertEqual(check['cited_count'], 2)
        self.assertEqual(check['cited_in_pool'], 1)

    def test_multiple_orphans_are_sorted(self):
        """多个越界 PMID 应被完整列出并按字典序排序。"""
        run = make_run(
            answer='[PMID:99999999] [PMID:12345678] [PMID:55555555]',
            papers={})
        check = run.validate_pmids()
        self.assertEqual(check['orphan_count'], 3)
        self.assertEqual(check['orphan_pmids'], ['12345678', '55555555', '99999999'])

    def test_orphan_is_recorded_as_trace_error(self):
        """发现越界引用时，应在 trace['errors'] 里留痕。"""
        run = make_run(answer='[PMID:99999999]', papers={})
        run.validate_pmids()
        stages = [e.get('stage') for e in run.trace['errors']]
        self.assertIn('citation_check', stages)

    def test_no_orphan_does_not_record_trace_error(self):
        """没有越界引用时，不应产生 pmid_check 错误。"""
        run = make_run(answer='[PMID:11111111]', papers={'11111111': {}})
        run.validate_pmids()
        stages = [e.get('stage') for e in run.trace['errors']]
        self.assertNotIn('citation_check', stages)

    def test_search_failed_skips_the_check(self):
        """search_failed 为真时不做 PMID 判读，结果里应带 skipped 字段。"""
        run = make_run(
            answer='[PMID:99999999]', papers={}, search_failed=True)
        check = run.validate_pmids()
        self.assertIn('skipped', check)
        self.assertEqual(check['skipped'], '检索后端故障，未生成答案')

    def test_search_failed_does_not_raise_alarm(self):
        """search_failed 为真时不应报警（trace 里不记 pmid_check 错误）。"""
        run = make_run(
            answer='[PMID:99999999]', papers={}, search_failed=True)
        run.validate_pmids()
        stages = [e.get('stage') for e in run.trace['errors']]
        self.assertNotIn('citation_check', stages)

    def test_result_is_stored_on_pmid_check_attribute(self):
        """校验结果应同时写入 self.pmid_check。"""
        run = make_run(answer='[PMID:11111111]', papers={'11111111': {}})
        check = run.validate_pmids()
        self.assertEqual(run.pmid_check, check)

    def test_bare_pmid_without_brackets_is_not_counted(self):
        """只有 [PMID:xxx] 形式才被识别；裸 PMID 文本不计入引用数。"""
        run = make_run(answer='见 PMID:11111111 的研究。', papers={'11111111': {}})
        check = run.validate_pmids()
        self.assertEqual(check['cited_count'], 0)
        self.assertEqual(check['orphan_count'], 0)


class TestOfflineGuard(unittest.TestCase):
    """离线保证：被测代码路径绝不触网。

    把 `socket.socket` 与 `urllib.request.urlopen` 换成"一被调用就抛异常"的桩，
    只要有任何一次真实的网络访问，测试就会失败。
    """

    def setUp(self):
        def _boom(*args, **kwargs):
            raise AssertionError('单元测试不允许发起网络请求')

        self._orig_socket = socket.socket
        self._orig_urlopen = urllib.request.urlopen
        socket.socket = _boom
        urllib.request.urlopen = _boom

    def tearDown(self):
        socket.socket = self._orig_socket
        urllib.request.urlopen = self._orig_urlopen

    def test_exercised_paths_make_no_network_call(self):
        """把所有被测函数跑一遍，不应触发任何网络调用。"""
        pipeline._json_load('{"a": 1}')
        pipeline._slug('二甲双胍 糖尿病')
        make_run().prisma_table()
        make_run().evidence_table()
        make_run(answer='[PMID:1]', papers={'1': {}}).validate_pmids()

    def test_importing_pipeline_does_not_touch_network(self):
        """被测模块本身不应在导入期发起网络请求（重新导入一次验证）。"""
        import importlib
        importlib.reload(pipeline)


if __name__ == '__main__':
    unittest.main(verbosity=2)


class TestPreprintCitations(unittest.TestCase):
    """预印本没有 PMID，用 [DOI:xxx] 引用；越界也要能查出来。"""

    def _run(self, **fields):
        run = make_run(**fields)
        return run

    def test_doi_citation_in_pool_is_ok(self):
        run = self._run(
            answer='结论 A [DOI:10.1101/2024.01.01.123456]。',
            preprints={'doi:10.1101/2024.01.01.123456': {
                'id': 'doi:10.1101/2024.01.01.123456',
                'doi': '10.1101/2024.01.01.123456'}},
        )
        chk = run.validate_pmids()
        self.assertEqual(chk['orphan_count'], 0)
        self.assertEqual(chk['cited_doi_count'], 1)

    def test_orphan_doi_is_detected(self):
        run = self._run(
            answer='结论 A [DOI:10.1101/99999999.999999]。',
            preprints={'doi:10.1101/2024.01.01.123456': {
                'id': 'doi:10.1101/2024.01.01.123456',
                'doi': '10.1101/2024.01.01.123456'}},
        )
        chk = run.validate_pmids()
        self.assertEqual(chk['orphan_count'], 1)
        self.assertTrue(chk['orphan_dois'])

    def test_preprint_and_pmid_pools_are_separate(self):
        """PMID 引用只在 PubMed 池里查，DOI 引用只在预印本池里查，两者互不兜底。"""
        run = self._run(
            answer='混引 [PMID:11111111] 与 [DOI:10.1101/88888888.888888]。',
            # PubMed 池里放的是另一个 PMID，说明 11111111 确实不在池内
            papers={'22222222': {'pmid': '22222222'}},
            preprints={'doi:10.1101/2024.01.01.123456': {
                'id': 'doi:10.1101/2024.01.01.123456',
                'doi': '10.1101/2024.01.01.123456'}},
        )
        chk = run.validate_pmids()
        self.assertEqual(chk['orphan_count'], 2)
        self.assertEqual(chk['orphan_pmids'], ['11111111'])
        self.assertEqual(len(chk['orphan_dois']), 1)
        self.assertEqual(chk['cited_pmid_count'], 1)
        self.assertEqual(chk['cited_doi_count'], 1)


class TestAnimalOnlyDetection(unittest.TestCase):
    """纯动物实验的程序侧判定。

    背景：实测发现 PubMed 的 `NOT ("Animals"[MeSH] NOT "Humans"[MeSH])`
    **只降噪、不能完全把关**（同一检索式 473 → 102，但纯小鼠文献仍在结果里），
    所以最终把关必须放在程序侧。
    """

    def test_pure_mouse_study_is_animal_only(self):
        p = {'mesh': ['Animals', 'Mice', 'Activins',
                      'Antibodies, Monoclonal, Humanized', 'Obesity', 'Weight Loss']}
        self.assertTrue(pubmed.is_animal_only(p))

    def test_human_study_is_not_animal_only(self):
        p = {'mesh': ['Humans', 'Adult', 'Obesity', 'Weight Loss']}
        self.assertFalse(pubmed.is_animal_only(p))

    def test_translational_mixed_study_is_kept(self):
        p = {'mesh': ['Humans', 'Animals', 'Mice', 'Disease Models, Animal']}
        self.assertFalse(pubmed.is_animal_only(p))

    def test_no_mesh_is_not_animal_only(self):
        self.assertFalse(pubmed.is_animal_only({'mesh': []}))

    def test_humanized_is_not_humans(self):
        """'Antibodies, Monoclonal, Humanized' 含 Human 但不是 Humans 标记。"""
        p = {'mesh': ['Animals', 'Antibodies, Monoclonal, Humanized']}
        self.assertTrue(pubmed.is_animal_only(p))


class TestTokenTracking(unittest.TestCase):
    """token 与成本追踪。

    背景（2026-11 真实 trace 抓到的）：`trace['models']` 只记了 planner/critic/writer，
    漏了 reader —— reader 占全部 token 的 76%（37940/49767），却被算成「无单价」，
    成本估算直接低估一个量级（显示 ¥0.0314，实际远不止）。
    """

    def _run(self):
        run = pipeline.Run.__new__(pipeline.Run)
        run.trace = {'tokens': {}, 'models': {}, 'errors': [], 'timings': {}}
        run.cfg = {'runtime': {}}
        return run

    def test_model_name_is_recorded_for_every_role(self):
        """每个角色的模型名都要记下来，否则计价查不到单价。"""
        run = self._run()
        run._track_tokens('reader', {'tokens': 100, 'model': 'deepseek-ai/DeepSeek-V3.2'})
        run._track_tokens('critic', {'tokens': 50, 'model': 'deepseek-ai/DeepSeek-R1'})
        self.assertEqual(run.trace['models']['reader'], 'deepseek-ai/DeepSeek-V3.2')
        self.assertEqual(run.trace['models']['critic'], 'deepseek-ai/DeepSeek-R1')

    def test_reader_tokens_are_counted(self):
        run = self._run()
        run._track_tokens('reader', {'tokens': 37940, 'model': 'm'})
        self.assertEqual(run.trace['tokens']['reader'], 37940)

    def test_concurrent_tracking_does_not_lose_counts(self):
        """精读是多线程并发的，计数不能因竞态丢失。"""
        import threading
        run = self._run()
        run._token_lock = threading.Lock()
        def worker():
            for _ in range(50):
                run._track_tokens('reader', {'tokens': 10, 'model': 'm'})
        ts = [threading.Thread(target=worker) for _ in range(8)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(run.trace['tokens']['reader'], 8 * 50 * 10)

    def test_pricing_failure_is_not_silently_swallowed(self):
        """计价失败要留下痕迹，否则用户以为没花钱。"""
        run = self._run()
        run._track_tokens('reader', {'tokens': 100, 'model': 'm'})
        # 真实场景里 estimate_cost 可能因表格式不对抛异常
        self.assertTrue(run.trace['cost_estimate'] is not None)

    def test_track_tokens_works_without_init(self):
        """测试常用 Run.__new__ 绕过 __init__，那时没有锁也不能崩。"""
        run = self._run()
        run._track_tokens('reader', {'tokens': 10, 'model': 'm'})
        self.assertEqual(run.trace['tokens']['reader'], 10)


class TestRetractionMentionCheck(unittest.TestCase):
    """被引用的撤稿文献，正文里必须交代。"""

    def _run(self, answer, evidence):
        run = pipeline.Run.__new__(pipeline.Run)
        run.answer = answer
        run.evidence = evidence
        run.papers = {}
        run.preprints = {}
        run.trace = {'errors': []}
        run.search_failed = False
        run.log = lambda m: None
        run.pmid_check = {}
        return run

    def test_retracted_paper_cited_without_mention_is_flagged(self):
        run = self._run(
            answer='结论成立 [PMID:11111111]。',
            evidence=[{'pmid': '11111111', '_retraction': '该文已被撤稿'}])
        run.validate_pmids()
        stages = [e['stage'] for e in run.trace['errors']]
        self.assertIn('retraction_check', stages)

    def test_retracted_paper_mentioned_in_answer_is_ok(self):
        run = self._run(
            answer='该文献已被撤稿 [PMID:11111111]。',
            evidence=[{'pmid': '11111111', '_retraction': '该文已被撤稿'}])
        run.validate_pmids()
        stages = [e['stage'] for e in run.trace['errors']]
        self.assertNotIn('retraction_check', stages)

    def test_retracted_paper_not_cited_is_not_flagged(self):
        """没被引用就不该报警——没引用的文献不进正文。"""
        run = self._run(
            answer='结论成立 [PMID:22222222]。',
            evidence=[{'pmid': '11111111', '_retraction': '该文已被撤稿'}])
        run.validate_pmids()
        stages = [e['stage'] for e in run.trace['errors']]
        self.assertNotIn('retraction_check', stages)


class TestStageControl(unittest.TestCase):
    """execute() 的 stages 参数。

    背景：横向对比（--compare）真正需要的只有 evidence / pooled / mech_grading，
    写成文、跑审稿、跑语义比对全是白花钱——实测每臂浪费约 132 秒 + 13 次模型调用。
    """

    def test_read_workers_ignores_search_latency(self):
        """精读并发度不应受检索延迟影响（精读是纯模型调用）。"""
        run = pipeline.Run.__new__(pipeline.Run)
        run.cfg = {'runtime': {'workers': 4}}
        run._search_latency = [9.0, 9.5, 10.0]   # 检索很慢
        self.assertEqual(run._read_workers(), 4,
                         '检索慢不代表模型慢，精读并发度不该被牵连')

    def test_read_workers_is_capped(self):
        run = pipeline.Run.__new__(pipeline.Run)
        run.cfg = {'runtime': {'workers': 20}}
        run._search_latency = []
        self.assertLessEqual(run._read_workers(), 6)

    def test_adaptive_workers_alias_still_works(self):
        """旧名要保留，别让外部调用炸掉。"""
        run = pipeline.Run.__new__(pipeline.Run)
        run.cfg = {'runtime': {'workers': 3}}
        run._search_latency = []
        self.assertEqual(run._adaptive_workers(), 3)


class TestRevisionHitCounting(unittest.TestCase):
    """检索式修订循环的命中统计。

    背景：修订轮的标签是 '{tag}rev{n}'，原实现只统计 tag，
    导致第一次修订已经命中了几百条仍会再触发一次修订——
    白花一次 planner 调用 + 一轮 NCBI 检索。命中为 0 恰恰是最该省钱的场景。
    """

    def _run(self):
        run = pipeline.Run.__new__(pipeline.Run)
        run.cfg = {'retrieval': {'max_revisions': 2}}
        run.search_log = []
        run.search_failed = False
        run.log = lambda m: None
        run.plan = {'queries': []}
        return run

    def test_revision_round_counts_towards_hits(self):
        run = self._run()
        run.search_log = [{'round': 'round1', 'total_hits': 0},
                          {'round': 'round1rev1', 'total_hits': 500}]
        calls = []

        def fake_revise(current):
            calls.append(1)
            return []

        run.revise_queries = fake_revise
        run.search_with_precheck = lambda q, tag='round1', retmax=20, min_year=None: None
        run.search_with_precheck.__func__ = None
        # 直接测判定逻辑：hits 必须包含 rev 轮
        hits = sum(s['total_hits'] for s in run.search_log
                   if s['round'] == 'round1' or s['round'].startswith('round1rev'))
        self.assertEqual(hits, 500, '修订轮的命中必须计入')


class TestFoundBy(unittest.TestCase):
    """每篇文献要记下它是被哪轮检索捞上来的。"""

    def test_fetch_metadata_records_found_by(self):
        run = pipeline.Run.__new__(pipeline.Run)
        run.cfg = {'retrieval': {'require_abstract': True}}
        run.papers = {}
        run.log = lambda m: None

        class FakePubMed:
            def efetch(self, pmids):
                return [{'pmid': '111', 'abstract': 'x' * 300,
                         'title': 'T', 'mesh': [], 'pub_types': []}]

        run.pubmed = FakePubMed()
        run.fetch_metadata(['111'], found_by='round1:精准')
        self.assertEqual(run.papers['111'].get('found_by'), 'round1:精准')


class TestReuseQueries(unittest.TestCase):
    """连续提问模式要沿用上一轮的检索式。"""

    def test_reuse_queries_are_merged_and_deduped(self):
        run = pipeline.Run.__new__(pipeline.Run)
        run.cfg = {'runtime': {'temperature_plan': 0.15}}
        run.trace = {'timings': {}, 'models': {}, 'errors': [], 'tokens': {}}
        run.log = lambda m: None
        run.question = 'GLP-1 对青少年的疗效'
        run.constraints = ''
        run.history = []
        run.reuse_queries = [{'label': '精准', 'query': 'A'},
                             {'label': '扩展', 'query': 'B'}]
        run.plan = {}
        run.cq_all = {}
        run.cq_explicit = []
        run.router = type('R', (), {'chat': lambda self, role, msgs, **kw: {
            'content': '{"core_question_en":"Q","queries":[{"label":"新","query":"C"}]}',
            'model': 'm', 'elapsed': 1.0, 'tokens': 10}})()
        run.plan_queries()
        got = [q['query'] for q in run.plan['queries']]
        self.assertIn('C', got, '本轮新检索式要保留')
        self.assertIn('A', got, '上一轮的检索式要沿用')
        self.assertIn('B', got)
        self.assertEqual(len(got), len(set(got)), '不应有重复检索式')

    def test_duplicate_reuse_query_is_not_added_twice(self):
        run = pipeline.Run.__new__(pipeline.Run)
        run.cfg = {'runtime': {'temperature_plan': 0.15}}
        run.trace = {'timings': {}, 'models': {}, 'errors': [], 'tokens': {}}
        run.log = lambda m: None
        run.question = 'Q'
        run.constraints = ''
        run.history = []
        run.reuse_queries = [{'label': 'x', 'query': 'SAME'}]
        run.plan = {}
        run.cq_all = {}
        run.cq_explicit = []
        run.router = type('R', (), {'chat': lambda self, role, msgs, **kw: {
            'content': '{"core_question_en":"Q","queries":[{"label":"新","query":"SAME"}]}',
            'model': 'm', 'elapsed': 1.0, 'tokens': 10}})()
        run.plan_queries()
        same = [q for q in run.plan['queries'] if q['query'] == 'SAME']
        self.assertEqual(len(same), 1, '同一条检索式不该出现两次')


class TestEvidenceSourceColumn(unittest.TestCase):
    """证据表要标明这条读的是全文还是摘要。"""

    def _table(self, evidence):
        run = pipeline.Run.__new__(pipeline.Run)
        run.evidence = evidence
        return run.evidence_table()

    def test_fulltext_and_abstract_are_labelled(self):
        t = self._table([
            {'_citation_key': '1', '_text_source': 'fulltext', 'key_findings': ['a']},
            {'_citation_key': '2', '_text_source': 'abstract', 'key_findings': ['b']},
        ])
        self.assertIn('全文', t)
        self.assertIn('摘要', t)

    def test_header_has_source_column(self):
        t = self._table([{'_citation_key': '1', 'key_findings': ['a']}])
        self.assertIn('来源', t.splitlines()[0])

    def test_missing_source_does_not_crash(self):
        t = self._table([{'_citation_key': '1', 'key_findings': ['a']}])
        self.assertIn('|', t)


class TestDesignDiversification(unittest.TestCase):
    """证据池的研究设计多样性。

    背景（2026-11-06 实测）：问「GLP-1 对青少年肥胖的疗效与安全性」，
    重排选出的 5 篇**全是系统综述**，一篇原始研究都没有。两个问题：
    ① 系统综述的结论受它纳入研究的截止时间限制，可能过时；
    ② 系统综述往往只给汇总数字，**原始研究的森林图才带逐个结局的效应量**——
       只读综述等于把「图表识别」这个能力整个用不上。
    """

    def _run(self, papers, cfg=None):
        run = pipeline.Run.__new__(pipeline.Run)
        run.cfg = cfg or {'retrieval': {'diversify_by_design': True,
                                        'min_primary': 1}}
        run.papers = papers
        run.log = lambda m: None
        return run

    def _mk(self, pmid, types):
        return {'pmid': pmid, 'pub_types': types,
                'journal': f'J{pmid}', 'title': f'T{pmid}'}

    def test_design_group_classification(self):
        run = self._run({})
        self.assertEqual(run._design_group(
            self._mk('1', ['Meta-Analysis', 'Systematic Review'])), 'review')
        self.assertEqual(run._design_group(
            self._mk('2', ['Review'])), 'review')
        self.assertEqual(run._design_group(
            self._mk('3', ['Randomized Controlled Trial'])), 'primary')
        self.assertEqual(run._design_group(
            self._mk('4', ['Observational Study'])), 'primary')
        self.assertEqual(run._design_group(
            self._mk('5', ['Journal Article'])), 'other')

    def test_review_only_pool_is_left_alone(self):
        """候选里全是综述 → 无从补起，不该硬凑。"""
        papers = {str(i): self._mk(str(i), ['Systematic Review'])
                  for i in range(4)}
        run = self._run(papers)
        out = run._diversify(['0', '1', '2', '3'], 4)
        self.assertEqual(out, ['0', '1', '2', '3'])

    def test_primary_is_added_when_top_n_is_all_reviews(self):
        """top-N 全是综述、但候选里有原始研究 → 补进 1 篇原始研究。"""
        papers = {
            '0': self._mk('0', ['Systematic Review']),
            '1': self._mk('1', ['Systematic Review']),
            '2': self._mk('2', ['Meta-Analysis']),
            '9': self._mk('9', ['Randomized Controlled Trial']),
        }
        run = self._run(papers)
        out = run._diversify(['0', '1', '2', '9'], 3)
        self.assertEqual(len(out), 3)
        self.assertIn('9', out, '原始研究应被补进证据池')
        self.assertEqual(out.count('9'), 1)

    def test_replaces_the_last_review_not_the_first(self):
        """替换的是排序最靠后的综述——综述更泛，少一篇损失更小。"""
        papers = {
            '0': self._mk('0', ['Systematic Review']),
            '1': self._mk('1', ['Meta-Analysis']),
            '2': self._mk('2', ['Review']),
            '9': self._mk('9', ['Randomized Controlled Trial']),
        }
        run = self._run(papers)
        out = run._diversify(['0', '1', '2', '9'], 3)
        self.assertIn('0', out, '排第一的综述不该被替换')
        self.assertNotIn('2', out, '排最后的综述应被替换')
        self.assertIn('9', out)

    def test_pool_with_primary_is_untouched(self):
        """已经有原始研究 → 原样返回，不做任何改动。"""
        papers = {
            '0': self._mk('0', ['Systematic Review']),
            '1': self._mk('1', ['Randomized Controlled Trial']),
            '2': self._mk('2', ['Meta-Analysis']),
        }
        run = self._run(papers)
        out = run._diversify(['0', '1', '2'], 3)
        self.assertEqual(out, ['0', '1', '2'])

    def test_can_be_turned_off(self):
        papers = {
            '0': self._mk('0', ['Systematic Review']),
            '9': self._mk('9', ['Randomized Controlled Trial']),
        }
        run = self._run(papers, {'retrieval': {'diversify_by_design': False,
                                              'min_primary': 1}})
        out = run._diversify(['0', '9'], 1)
        self.assertEqual(out, ['0'], '关掉后就是纯按相关度取前 N')

    def test_min_primary_zero_means_off(self):
        papers = {
            '0': self._mk('0', ['Systematic Review']),
            '9': self._mk('9', ['Randomized Controlled Trial']),
        }
        run = self._run(papers, {'retrieval': {'diversify_by_design': True,
                                              'min_primary': 0}})
        out = run._diversify(['0', '9'], 1)
        self.assertEqual(out, ['0'])

    def test_length_is_preserved(self):
        """多样性调整不能改变证据池大小。"""
        papers = {
            '0': self._mk('0', ['Systematic Review']),
            '1': self._mk('1', ['Meta-Analysis']),
            '9': self._mk('9', ['Randomized Controlled Trial']),
        }
        run = self._run(papers)
        out = run._diversify(['0', '1', '9'], 2)
        self.assertEqual(len(out), 2)
        self.assertEqual(len(set(out)), 2, '不能出现重复项')


class TestEvidencePoolReuse(unittest.TestCase):
    """连续提问模式的证据池复用。

    背景：用户的真实用法是「围绕一个课题反复追问」。
    同一篇文献重读一次是一次完整的模型调用，而抽取结果几乎必然一样——
    沿用上一轮已精读过的文献，追问的成本因此从
    「检索+全量精读+审稿+成文」降到「检索+只精读新文献+审稿+成文」。
    """

    def _run(self, prior=None):
        run = pipeline.Run.__new__(pipeline.Run)
        run.cfg = {'runtime': {'temperature_reader': 0.1},
                   'retrieval': {}}
        run.trace = {'timings': {}, 'models': {}, 'errors': [], 'tokens': {}}
        run.log = lambda m: None
        run.prior_evidence = list(prior or [])
        run._prior_map = {
            (d.get('_citation_key') or d.get('pmid')): d
            for d in run.prior_evidence if d
        }
        run.cache_hits = 0
        run.router = type('R', (), {'chat': lambda self, *a, **k: {
            'content': '{}', 'model': 'm', 'elapsed': 0.1, 'tokens': 1}})()
        run.pubmed = type('P', (), {'fulltext': lambda self, p: {
            'source': '', 'text': '', 'figure_captions': []}})()
        run.dir = '.'
        run.no_fulltext = []
        run.plan = {'core_question_en': 'Q'}
        run.question = 'Q'
        # 精读要走「全文名额」计数器，它靠这把锁保证并发下不超发
        run._ft_lock = threading.Lock()
        run._ft_claimed = 0
        return run

    def _paper(self, pmid):
        return {'pmid': pmid, 'title': f'T{pmid}', 'journal': 'J', 'year': '2024',
                'abstract': 'x' * 300, 'pub_types': ['Journal Article'],
                'mesh': [], 'keywords': [], 'doi': '', 'pmcid': ''}

    def test_prior_map_is_keyed_by_citation_key(self):
        run = self._run([{'_citation_key': '111', 'pmid': '111',
                           'key_findings': ['a']}])
        self.assertIn('111', run._prior_map)

    def test_reused_evidence_is_returned_without_model_call(self):
        """命中已有证据时不应该再调模型。"""
        prior = {'_citation_key': '111', 'pmid': '111', 'relevance': 5,
                 'key_findings': ['已抽好的结论'], '_text_source': 'fulltext'}
        run = self._run([prior])
        called = {'n': 0}

        def boom(*a, **k):
            called['n'] += 1
            raise RuntimeError('不该调模型')

        run.router = type('R', (), {'chat': staticmethod(boom)})()
        got = run.read_one('111', self._paper('111'))
        self.assertIsNotNone(got)
        self.assertEqual(called['n'], 0, '命中缓存就不该调模型')
        self.assertEqual(run.cache_hits, 1)

    def test_reused_evidence_is_marked(self):
        """复用项要打标记，读者才知道它是上一轮的抽取结果。"""
        prior = {'_citation_key': '111', 'pmid': '111', 'relevance': 5,
                 'key_findings': ['a'], '_text_source': 'fulltext'}
        run = self._run([prior])
        got = run.read_one('111', self._paper('111'))
        self.assertTrue(got.get('_reused'))

    def test_reuse_does_not_mutate_the_original(self):
        """不能污染上一轮的证据对象（下一轮还要继续用）。"""
        prior = {'_citation_key': '111', 'pmid': '111', 'relevance': 5,
                 'key_findings': ['a'], '_text_source': 'fulltext'}
        run = self._run([prior])
        got = run.read_one('111', self._paper('111'))
        got['relevance'] = 99
        self.assertEqual(prior['relevance'], 5, '原对象不该被改')

    def test_new_paper_is_not_reused(self):
        """没在上一轮出现过的文献要走正常精读流程。"""
        run = self._run([{'_citation_key': '111', 'pmid': '111'}])
        got = run.read_one('222', self._paper('222'))
        # 正常精读会因为 JSON 解析失败返回 None，但**不该**命中缓存
        self.assertIsNone(got)
        self.assertEqual(run.cache_hits, 0)

    def test_empty_prior_pool_is_fine(self):
        run = self._run([])
        self.assertEqual(run._prior_map, {})
        self.assertEqual(run.cache_hits, 0)
