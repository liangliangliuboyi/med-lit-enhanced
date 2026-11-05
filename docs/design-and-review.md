# `med-lit-enhanced` 技能完整拆解报告

> 拆解对象：`C:\Users\LZY13\.workbuddy\skills\med-lit-enhanced\`
> 拆解日期：2026-11-05 ｜ 方法沉淀见 `E:\workcreate\.workbuddy\memory\topics\skill-reverse-engineering.md`
> 口径：**SKILL.md 是作者的"宣称"，源码是"事实"**，本报告以源码为准，并单列两者不一致之处。

---

## 一、一句话定位

把"提一个医学问题"变成"一份每条论断都挂着 PMID 的可回查循证答案"——
靠**多角色大模型流水线（规划→检索→重排→精读→审查→成文）** 驱动，
用**程序侧过滤**代替检索式 NOT，用**脚本生成参考文献**代替模型写参考文献。

---

## 二、文件清单与职责

| 文件 | 体积 | 角色 | 一句话职责 |
|---|---|---|---|
| `SKILL.md` | 6.0 KB | 说明书 | 给 Agent 看的使用文档（何时用、怎么调、参数表、常见问题） |
| `scripts/ask.py` | 5.8 KB | CLI 入口 | argparse 收参 → 覆盖 config → 调 `pipeline.Run` |
| `scripts/config.json` | 2.4 KB | 配置 | 密钥、模型路由链、检索参数、运行参数、输出根目录 |
| `scripts/pipeline.py` | 20.0 KB | **主流程** | 九阶段编排、日志、落盘、全部核心业务逻辑 |
| `scripts/prompts.py` | 5.4 KB | 提示词库 | 4 个 system prompt + 4 个 user prompt 模板 |
| `scripts/pubmed.py` | 10.3 KB | PubMed 客户端 | E-utilities 检索/取题录/PMC 全文，零第三方依赖 |
| `scripts/sf.py` | 6.3 KB | 模型客户端 | 硅基流动 chat/rerank/embed + 按角色降级路由 |
| `scripts/selftest.py` | 5.2 KB | 自检 | 7 项逐环节体检，含一次端到端迷你跑 |

**技术底座**：纯标准库（`urllib` + `xml.etree.ElementTree` + `json` + `re` + `concurrent.futures`），
**零第三方依赖**。这正好对上了本机的现实约束——WorkBuddy 托管的裸 Python 装不了第三方库，所以这套设计能直接跑起来。

---

## 三、工作原理（机制层）

### 3.1 三条设计纪律（作者的立身之本）

1. **只基于检索到的真实文献作答**，每条实质性论断后必须挂 `[PMID:xxxxxxxx]`。
2. **参考文献由脚本生成**，取自 PubMed 原始字段；模型只写正文，禁止自己写期刊名/卷期/作者。
3. **检索到的如果不够，就说不够**，不许用常识补空。

第 2 条是这个技能最聪明的地方——大模型写参考文献时最容易编错年份和刊名，
所以干脆**不让模型碰这一段**，`bibliography()` 直接从 `self.papers` 原始字段拼字符串。

### 3.2 四个关键机制

**机制 A · 角色化模型路由（`sf.ModelRouter`）**
每个角色在 `config.json` 里配一条候选链（planner / reader / critic / writer / fast）。
路由器先调 `/models` 探测平台存活列表，取链上**第一个可用**的模型；
调用抛异常或返回空 → 自动降级到链上下一个，并把失败的模型记入 `_fail` 集合，**后续不再重试它**。
角色之间可以用不同模型：critic 用 DeepSeek-R1（推理型），reader 用 DeepSeek-V3.2（长上下文）。

**机制 B · 检索式交给模型，过滤交给程序**
PubMed 有一条血泪教训（写进 `pubmed.py` 文件头注释）：检索式里的 `NOT (A OR B)` 会被 PubMed **错解析**，
实测命中量掉 85%。所以：
- PLANNER 提示词第 4 条明令**不许写 NOT 子句**；
- 排除综述/病例报告改在程序侧做 —— `pubmed.is_research_article()` 按 `pub_types` 白名单+黑名单判定。

**机制 C · JSON 容错解析 + 全阶段兜底**
`_json_load()` 先剥 ```` ```json ```` 包裹，再用正则 `\{.*\}` 兜底抽 JSON，仍失败返回 `None`。
每个阶段拿到 `None` 都有对应降级路径（见 4.7），**任何一环失败都不会让整条流水线崩掉**。

**机制 D · 全量留痕**
每跑一次在磁盘上生成 10 个文件，把规划、题录、证据、批评、答案、参考文献、日志、trace 全落盘。
`10_trace.json` 里记录每阶段耗时、**实际命中的模型名**、错误表、以及**本次是否限刊**（`scope_on` / `scope_mode` / `scope_names`），供事后回查。

---

## 四、工作流程（九阶段）

```
用户问题(中文)
   │
① 规划      planner 模型 → 英文核心问题 + PICO + 2~3 条互补检索式
   │
② 检索      esearch × N 条（带年份下限，可选刊源子句）
   │         每条检索式后立刻 efetch → 保证轮内去重
   │         └─ 命中 0 → 再来一轮「去掉年份限制」（round1b 兜底）
③ 题录      efetch 解析：标题/摘要/MeSH/关键词/pub_types/作者/年份/DOI/PMCID
   │
④ 过滤      摘要非空 + ≥200 字（可选 --research-only 剔综述等）
   │
⑤ 重排      Qwen3-Reranker-8B 语义重排 → 截到 max_papers(默认 8)
   │         （候选数 ≤ 上限时跳过重排）
⑥ 精读      3 线程并发：前 N 篇优先抓 PMC 全文，其余用摘要
   │         reader 模型逐篇抽结构化证据（PICO/样本量/效应量/局限/相关度1-5）
   │         按 相关度↓ → 年份↓ 排序
⑦ 审查      critic 模型横向评估：一致性 / 冲突(带PMID对照) / 证据空隙 / 方法学警告
   │
⑧ 成文      writer 模型按固定 6 段结构写，每句挂 PMID
   │
⑨ 落盘      10 个文件 + trace + log
```

### 4.1 阶段细则

**① 规划**（`pipeline.plan_queries`）
调 planner，temperature 0.15，max_tokens 1200。
模型必须输出英文检索式，拆成 2–3 条互补：**一条偏精准**（核心概念 AND）、**一条偏召回**（同义词 OR 展开），
每条 ≤12 个概念块，用括号正确分组，MeSH 词 + 自由词 `[tiab]` 组合。
**兜底**：模型没给出可用检索式 → 把原问题直译当检索式，日志注明"模型未给出检索式，已回退"。

**② 检索**（`pipeline.search`）
对每条检索式调 `esearch`：`sort=relevance`，`retmax=retmax_per_query`（默认 20），
带 `mindate=2015/3000` + `datetype=pdat`。
**刊源子句**（可选）：`( "Nature"[ta] OR "Cell"[ta] ... ) AND 你的检索式`，`ta`=缩写、`full`=全称。
**去重**：维护 `self.papers` 字典，命中后立刻 efetch 落库，后续检索式只处理新增 ID。

**③ 题录**（`pipeline.fetch_metadata` + `pubmed._parse_xml`）
efetch 分批 200 篇/次，`retmode=xml`。
解析出的字段：`pmid / title / abstract（含分段标签）/ authors / journal / journal_short / year / doi / pmcid / mesh / keywords / pub_types`。
两个关键细节：
- **DOI 只能从 `PubmedData/ArticleIdList` 取**。用 `.//ArticleIdList` 会把参考文献列表里的 doi 也抓进来，造成覆盖。
- 年份缺失时从 `MedlineDate` 里正则抠 `(19|20)\d{2}`。

**④ 过滤**（`pipeline.filter_papers`）
两道门槛：
- `require_abstract` → 丢弃无摘要的；
- `min_abstract_chars`（默认 200）→ 丢弃摘要过短的。
`--research-only` 开启时额外走 `is_research_article()`：要求 pub_types 含 "Journal Article"
或 RCT/Clinical Trial/Observational Study，且不含排除名单（Review / Systematic Review / Meta-Analysis /
Case Reports / Editorial / Comment / Letter / News / Erratum / Retraction），且摘要 ≥400 字。

**⑤ 语义重排**（`pipeline.rerank`）
用 `Qwen/Qwen3-Reranker-8B`，query = 英文核心问题，document = 标题 + 摘要前 1600 字，top_n = max_papers。
每个文献的 `relevance_score` 回写到题录。
**兜底**：重排失败 → 退回原始顺序。

**⑥ 逐篇精读**（`pipeline.read_one` / `read_all`）
`ThreadPoolExecutor(max_workers=3)` 并发。
每篇的正文来源优先级：**PMC 开放全文（前 fulltext_limit 篇）→ 摘要（截 5000 字）**；
全文截 26000 字，命中 `## 章节` 结构，落盘到 `06_fulltext/PMID<id>.txt`。
reader 模型抽取固定 9 字段：
`pmid / study_design / sample_size / population / intervention / comparator / primary_outcome / key_findings[] / effect_sizes[] / limitations[] / relevance(1-5)`。
**核心纪律**：只依据给定文本，文本里没有的写「未提及」，绝不推测。

**⑦ 横向审查**（`pipeline.critique_all`）
把所有证据条目（去掉内部字段后）打包给 critic，输出：
`evidence_strength / consistency / conflicts[{claim, pmids[], note}] / gaps[] / warnings[]`。
作用是**给成文阶段一个"冲突对照表"**，让 writer 无法装作没看见矛盾。

**⑧ 成文**（`pipeline.write`）
writer 拿 4 样东西：问题、补充限定、检索概况（脚本算的，不让它自己数）、证据表、审查意见 JSON。
固定 6 段结构：
1. 一句话结论（2-3 句，含最关键量化结果）
2. 证据概况（检索次数/命中数/年份范围/设计分布/期刊来源——**以"检索概况"为准**）
3. 核心发现（3-6 个要点，句末带 PMID）
4. 证据冲突与不确定性
5. 证据空隙（引用审查给出的 gaps）
6. 对你的具体启示（3-5 条可操作建议）
**第 7 条纪律**：明确禁止写"参考文献"或"关键文献清单"章节。

**⑨ 落盘**（`pipeline.save`）
写出 10 个文件，`07_answer.md` = 模型正文 + 脚本生成的参考文献（用 `\n\n---\n\n## 关键文献` 拼接）。

### 4.2 落盘产物

```
E:/workbuddyplace/med-lit-runs/20261027-1530_GLP1受体激动剂对肥胖成人减重/
  01_question.md        问题与限定
  02_plan.json          检索计划（英文核心问题 + PICO + 检索式）
  03_papers.json        全部候选题录（摘要/MeSH/关键词/PMCID）
  04_evidence.json      精读抽取的结构化证据
  05_evidence_table.md  证据总表 + 检索概况
  06_fulltext/PMID*.txt 抓到的 PMC 开放全文
  07_answer.md          ★ 答案正文（带 PMID）+ 关键文献
  08_bibliography.md    参考文献（脚本生成）
  09_log.txt            带时间戳的执行日志
  10_trace.json         模型命中 / 各阶段耗时 / 错误表 / 是否限刊
```
读答案只看 `07_answer.md` 和 `05_evidence_table.md`，其余是过程留痕。

### 4.3 模型路由表（`config.json`）

| 角色 | 路由链（取第一个可用） | 温度 | max_tokens |
|---|---|---|---|
| planner | DeepSeek-V3.2 → V3.1-Terminus → Qwen3.5-35B-A3B → Qwen3-32B | 0.15 | 1200 |
| reader | DeepSeek-V3.2 → V3.1-Terminus → Qwen3.5-35B-A3B → Qwen2.5-72B-Instruct | 0.10 | 1600 |
| critic | **DeepSeek-R1** → DeepSeek-V3.2 → Qwen3.5-27B | 0.15 | 1800 |
| writer | DeepSeek-V3.2 → V3.1-Terminus → Qwen3.5-35B-A3B | 0.30 | 5500 |
| fast（仅自检用） | Qwen3.5-9B → Qwen3-14B → Qwen2.5-7B-Instruct | — | — |

独立模型：`Qwen/Qwen3-Reranker-8B`（重排）、`Qwen/Qwen3-Embedding-8B`（嵌入，**已配置但从未被调用**）。

### 4.4 CLI 参数全表

| 参数 | 作用 | 默认 |
|---|---|---|
| `question` | 位置参数，中文问题 | 必填 |
| `--constraints` | 补充限定（人群/年份/地区） | 空 |
| `--quick` | 快速模式：少读几篇、约 3 分钟 | 关 |
| `--max-papers N` | 精读篇数上限 | 8 |
| `--fulltext-limit N` | 尝试抓全文的篇数 | 4 |
| `--research-only` | 只保留研究论文 | **关**（默认保留综述，因为系统综述/网状 Meta 对"效果如何"类问题价值最高） |
| `--journals` | 限定到内置 26 本 CNS 及大子刊 | **关（默认全 PubMed）** |
| `--journal-list "Nature,Nat Med"` | 临时指定刊名，自动开启限定并覆盖内置表 | — |
| `--journal-mode ta\|full` | 刊名匹配方式 | ta |
| `--models` | 打印路由链与平台可用性 | — |
| `--pubmed-only TERM` | 只检索不调模型 | — |
| `--retmax N` | `--pubmed-only` 取回条数 | 10 |
| `--json` | 输出 JSON 摘要（不含答案正文） | — |
| `--config PATH` | 指定配置文件 | 内置 |

### 4.5 自检（`selftest.py`，约 2 分钟）

7 项逐环节体检：配置读取 → 硅基流动 `/models` → 对话接口 → PubMed esearch → PubMed efetch → PMC 全文 → 研究论文判定
→ （可选）端到端迷你跑（`--quick` + max_papers=2 + min_year=2020）。
加 `--net` 只测网络与接口，不跑完整问答。

### 4.6 网络层细节（`pubmed.py` / `sf.py`）

- **节流**：PubMed 最小请求间隔 0.36 s（≈2.8 req/s，无 key 时的安全线）。
- **重试**：429 与 5xx 指数退避（`2^attempt * 2`），其他 HTTP 错误直接抛出。
- **长查询自动切 POST**：URL 编码后 >1800 字符改用 POST 表单，否则 NCBI 拒收。
- **代理**：`urllib` 会读环境变量 `HTTP_PROXY/HTTPS_PROXY` 和 Windows 注册表代理。
  本机代理是 `127.0.0.1:17890`（CokeCloud），**终端程序不读系统代理**，跑这个技能前建议显式设
  `$env:HTTPS_PROXY="http://127.0.0.1:17890"`。
- **全文来源**：PMC BioC JSON
  `https://www.ncbi.nlm.nih.gov/research/bionlp/RESTful/pmcoa.cgi/BioC_json/PMC<id>/unicode`，
  按 `section_type` 拼成带 `## 章节` 的 markdown。

### 4.7 全部降级路径（鲁棒性设计）

| 出问题的环节 | 降级行为 |
|---|---|
| planner 没吐出检索式 | 用原问题直译当检索式 |
| esearch 抛异常 | 记 `errors`，跳过该条检索式继续下一条 |
| 检索命中 0 | 去掉年份限制再搜一轮（round1b） |
| 某轮候选为空 | 直接写"本轮检索未获得可用文献，无法作答"并落盘 |
| efetch 解析失败 | 丢弃该篇，不影响其他 |
| 重排接口失败 | 退回原始顺序，取前 N |
| 某篇精读失败 | 记 `errors`，其余照常，evidence 里就没有它 |
| reader 返回非 JSON | 记 `errors`（"JSON 解析失败"），该篇丢弃 |
| critic 失败 | 返回固定兜底（"审查环节失败"），writer 仍能写 |
| writer 失败 | 正文写"## 生成失败\n\n模型调用出错，请查看 09_log.txt" |
| 模型调用异常 | 路由到链上下一个模型；全链失败才抛 |

**这套"任何一环失败都不崩"的韧性设计，是这个技能工程质量最高的部分。**

---

## 五、工作要点（值得抄走的六条）

1. **能不交给模型的事，就不交给模型。** 参考文献 → 脚本拼；检索概况的计数 → 脚本算；
   摘要长度与文献类型过滤 → 程序判。只有"需要判断力"的部分才给模型。
2. **把检索式的痛点写进提示词。** PubMed 的 NOT 错解析不是 bug 修复，是**写进 PLANNER 提示词的禁令**，
   并在 `pubmed.py` 文件头把四个坑注释出来，让下一个改代码的人先看注释。
3. **角色分离。** 规划、精读、审查、成文用不同温度、不同模型：
   规划要稳（0.15）、精读要冷（0.10，不许脑补）、审查要能推理（R1）、成文可以放开（0.30）。
4. **每篇文献一个 1-5 的相关度，并用它排序。** 精读产物按 `相关度↓ → 年份↓` 排，
   写手看到的证据表天然是按价值排序的。
5. **让模型"显式承认不知道"。** READER 规定"文本里没有就写未提及"；
   WRITER 规定"没检索到就写『本轮检索未覆盖』"；CRITIC 专门产出 `gaps` 段落。
   三个环节共同把"沉默的幻觉"逼成"显式的空白"。
6. **过程留痕到能审计。** 日志带时间戳、trace 记录实际命中的模型和每阶段耗时、还记了本次是否限刊——
   事后想知道"当时到底用了哪个模型、限没限刊"，不用复现。

---

## 六、已实现功能清单

**输入输出**
- ✅ 中文提问 → 自动译成英文核心问题 + PICO + 2–3 条互补检索式
- ✅ `--constraints` 补充人群/年份/地区限定
- ✅ `--quick` 快速模式

**检索**
- ✅ PubMed E-utilities 检索（relevance 排序、年份下限）
- ✅ 默认**检索全 PubMed 不限刊**；可选 26 本 CNS 白名单（`--journals`）或任意临时名单（`--journal-list`）
- ✅ 命中 0 自动放宽年份重搜
- ✅ 轮内即时去重

**筛选**
- ✅ 摘要存在性 + 长度门槛
- ✅ 文献类型门槛（可选剔除综述/病例报告/评论等）
- ✅ PMC 开放全文优先（仅 OA 有全文，其余用摘要）
- ✅ 语义重排截断到 Top-N

**证据抽取**
- ✅ 并发（3 线程）逐篇抽取 11 字段结构化证据
- ✅ 相关度评分 + 排序
- ✅ 效应量、CI、样本量、设计类型提取
- ✅ 作者自述局限提取

**审查与成文**
- ✅ 一致性判定、冲突对照（含双方 PMID + 差异可能来源）
- ✅ 证据空隙、方法学风险
- ✅ 6 段固定结构成文，每条论断挂 `[PMID:xxxx]`
- ✅ 参考文献脚本生成（作者 ≤3 + et al + 期刊 + 年份 + PMID + DOI）

**工程能力**
- ✅ 全阶段降级不崩
- ✅ 模型按角色路由 + 失败自动顺延 + 失败模型拉黑
- ✅ 10 文件留痕 + 时间戳日志 + trace（含是否限刊）
- ✅ 7 项自检脚本，支持 `--net` 快速体检
- ✅ `--models` 打印路由链与平台实际可用性
- ✅ `--pubmed-only` 只检索不调模型（用于核对检索式）
- ✅ `--json` 输出机器可读摘要
- ✅ 零第三方依赖

---

## 七、缺陷与风险（源码级审查）

### 🔴 高优先级

**1. 明文密钥硬编码在 `config.json`**
`siliconflow.api_key`（`sk-jvcf...`）与 `pubmed.api_key`（`30c81e0d...`）直接写在文件里。
SKILL.md 有警告"不要提交到公开仓库"，但文件就躺在用户级 skills 目录，
**打包分享、同步到 git、或者你哪天手滑把技能目录发给别人，密钥即泄露**。
👉 建议改读环境变量（`os.environ.get('SILICONFLOW_API_KEY')`），配置文件只留 base_url。

**2. 跨工作空间写入**
`paths.runs_root = "E:/workbuddyplace/med-lit-runs"` —— 输出落在**另一个工作空间**里，
与本工作空间 `E:\workcreate` 无关。技能在哪个空间跑，产物就散在哪个地方，隔离性被破坏。
👉 建议改成 `E:/workcreate/med-lit-runs`。

**3. SKILL.md 承诺的「PMID 越界报警」在代码里不存在**
SKILL.md 原文：「回答里出现没见过的文献……若正文引用了不在候选池里的 PMID，会在日志里报警（`10_trace.json` 的 `errors`）」。
**核对结果：`pipeline.py` 从未对 writer 的输出做过任何 PMID 校验**，
`trace['errors']` 只收集异常/降级信息，不收集越界引用。这条文档承诺是**假的**。
👉 建议在 `save()` 前加一步：正则抽出正文所有 `[PMID:\d+]`，
与 `self.papers` 的 key 求差集，差集非空则写进 `trace['errors']` 并在日志打印告警。

### 🟠 中优先级

**4. `fulltext_limit` 在并发下有竞态**
`read_one` 里判断 `len(self.evidence) < fulltext_limit`，
但 `self.evidence` 只在 `read_all` 主线程里 future 完成后才 append。
3 个并发线程可能同时看到 `len == 0` → **全部去抓 PMC 全文**，既超限又浪费带宽和配额。
👉 用 `threading.Lock` + 独立计数器，或先同步算出要抓全文的 PMID 集合再分派。

**5. `retrieval.rounds` 是死配置**
`config.json` 写了 `"rounds": 2`，`--quick` 还在改它（`rounds: 1`），
但 `execute()` **从头到尾没读过这个键**。真正的"多轮"其实是 planner 给的 2–3 条检索式在同一轮里依次检索。
👉 要么删掉这个键，要么真按 rounds 做二轮检索式扩写。

**6. `--research-only` 会让摘要门槛突然变严**
`filter_papers` 里 research_only 分支用**硬编码 400**，而 config 的 `min_abstract_chars` 是 200。
开了这个开关后，大量 200–399 字的摘要会被误杀，用户以为只是"剔综述"，实际门槛也变了。
👉 统一读 config。

### 🟡 低优先级 / 观察项

**7.** `embed_model`（Qwen3-Embedding-8B）配了但 pipeline 从未调用 embedding 接口——要么接上做向量召回，要么删。
**8.** `writer` 的 `max_tokens = runtime.max_tokens + 1500 = 5500`，长答案可能被静默截断，**没有截断检测**。
**9.** `selftest.py` 依赖固定检索式 `"glucagon-like peptide-1" AND obesity`，
PubMed 收录会随时间变化，未来某天命中数下降会误报失败。
**10.** 输出目录名 = 时间戳 + 问题前 16 字，**同一分钟跑两个相似问题会撞目录**（后者覆盖前者）。
**11.** `--json` 只输出 `dir/counts/models/elapsed`，**不含答案正文**，脚本化消费时还得去读目录。
**12.** planner 生成的检索式**没有语法校验**，唯一反馈是 esearch 返回 0；建议加一次 esearch 预检。
**13.** `pubmed.email` 留空——NCBI 建议配 email 以获得更宽松的限流配额。

---

## 八、维护与扩展

| 想改什么 | 改哪里 | 要不要动代码 |
|---|---|---|
| 换 API key | `config.json` → `siliconflow.api_key` / `pubmed.api_key` | 否（建议改环境变量，见缺陷 1） |
| 换模型 | `config.json` → `models.chain.<角色>` | 否 |
| 改检索范围 | `config.json` → `retrieval.*`（年份下限、篇数、摘要门槛、排除类型） | 否 |
| 改输出目录 | `config.json` → `paths.runs_root` | 否 |
| 改写作风格 | `prompts.py` → `WRITER_SYSTEM` / `WRITER_USER` | 否 |
| 改抽取字段 | `prompts.py` → `READER_SYSTEM` 的输出格式 + `pipeline.read_one` 的字段透传 | 是 |
| 改章节结构 | `prompts.py` → `WRITER_SYSTEM` 的输出结构 | 否 |
| 加一个环节 | `pipeline.py` → `Run.execute()` 串一个新方法 | 是 |
| 自检 | `python scripts/selftest.py`（约 2 分钟） | — |

**调用示例**
```bash
C:/Users/LZY13/.workbuddy/binaries/python/versions/3.13.12/python.exe \
  "C:/Users/LZY13/.workbuddy/skills/med-lit-enhanced/scripts/ask.py" \
  "GLP-1 受体激动剂对青少年肥胖的效果如何？" \
  --constraints "只关注 RCT，2020 年后" --max-papers 8
```

---

## 九、复盘要点（我的整体评价）

**做得好的三件事**
1. **职责边界划得极清楚**——模型只做判断力工作，机械活全给程序。这是可维护性的根源。
2. **鲁棒性设计到位**——11 条降级路径，环节之间用 try/except 兜死，任何单点失败都不影响产出可读答案。
3. **提示词里写进了"不许编造"的机制约束**，而不是只写"请准确"——用可检查的输出格式和禁令代替空洞要求。

**最需要补的三件事**
1. **密钥环境变量化**（安全底线）。
2. **PMID 越界校验补上**——这是它自己写在文档里的承诺，也是唯一能自动发现"模型编文献"的机制。
3. **并发计数器加锁** + `rounds` 死配置清理。

**一句话总评**
> 这是一个**工程成熟度明显高于提示词技巧**的技能：真正的创新不在提示词写得多好，
> 而在于把「检索、过滤、重排、计数、参考文献」这些**不该让模型做的事**牢牢摁在程序侧，
> 只把「怎么问、怎么抽、怎么挑矛盾、怎么写」这四件真正需要判断力的事交给模型。
> 它的三个缺陷（密钥硬编码、跨空间写入、承诺的 PMID 校验未实现）都属于**收尾没做完**，
> 而非架构问题——补齐之后就是一个可以直接长期信赖的循证检索工具。

---

*本报告由源码逐行通读得出，与 SKILL.md 宣称不一致之处已在第七节逐条标出。*
