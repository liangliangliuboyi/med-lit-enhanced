# medlit-agent · 循证医学文献检索技能

> 给 AI 智能体用的循证医学文献检索技能（Agent Skill）。
> 你用中文提一个临床或公卫问题，它检索 PubMed、逐篇精读、挑出证据冲突，
> 最后写出一份**每句话都挂 `[PMID:xxxx]`**、能点回原文的循证答复。

[English](#english) | MIT 许可 | Python 3.9+ | **零第三方依赖**

---

## 安装

### 方式 A：交给智能体装

把仓库地址丢给智能体，让它帮你装：

```
https://github.com/liangliangliuboyi/medlit-agent
```

智能体读到这个仓库的 `skills/medlit-agent/SKILL.md`，里面写了完整的安装步骤。

### 方式 B：一行命令

```bash
npx -y skills add liangliangliuboyi/medlit-agent --skill medlit-agent
```

`skills` 是以 GitHub 为 registry 的技能包管理器（<https://skills.sh>），
支持 Claude Code / Cursor / Codex / Windsurf 等，装到各自的 skills 目录。

### 方式 C：手动拷贝

```bash
git clone https://github.com/liangliangliuboyi/medlit-agent.git
cd medlit-agent
.\install\install.ps1 workbuddy      # Windows
./install/install.sh workbuddy       # macOS / Linux
```

手动的话，把仓库里的 `skills/medlit-agent/` 整个目录复制到目标位置：

| Agent | 目标目录 |
|---|---|
| WorkBuddy | `~/.workbuddy/skills/medlit-agent/` |
| Claude Code | `~/.claude/skills/medlit-agent/` |
| Codex / Cursor 等 | `~/.agents/skills/medlit-agent/` |

### 装完两件事

**1. 体检（缺什么会主动提示你）。**

```bash
cd <装好的目录>
python scripts/ask.py --doctor          # 人看的报告
python scripts/ask.py --doctor-json     # 机器读的结构化清单
```

这个工具需要几项**只有你本人能提供**的信息。体检会逐项检查，对必填项还会附一句"该怎么问"：

```
[✓] 模型服务密钥（必填）
      已配置（SILICONFLOW_API_KEY）
[!] NCBI 邮箱（必填，须填你自己的）
      缺少 NCBI 邮箱（首次使用必须填你自己的）：…
      该怎么问：请提供你自己的邮箱。它会写进 NCBI 请求里…
```

**2. 填密钥。** 装好的目录里会有一个 `.env`（由 `.env.example` 生成）：

```
SILICONFLOW_API_KEY=你的模型服务密钥     # 必填
NCBI_API_KEY=你的NCBI密钥               # 选填，有则限速 3/s → 10/s
NCBI_EMAIL=你的邮箱                     # 必填，必须是你自己的
```

> NCBI 要求登记使用者的邮箱：万一有人违规使用这个工具导致你的 IP 被限流，
> NCBI 需要能联系到你本人。**工具不会替你填写，也不要填别人的邮箱。**

或者让向导代填：`python scripts/ask.py --setup`（采集并写入 `.env`，自动自检）。

模型服务默认用**硅基流动**（国内直连、开箱即用）。任何 OpenAI 兼容端点都能换——
改 `config.json` 的 `provider.base_url` 与 `provider.api_key_env` 就行，
可换 DeepSeek / Kimi / OpenAI / 本地 Ollama。

**自检**：

```bash
python scripts/selftest.py          # 逐环节体检（上游故障显示 SKIP 而非 FAIL）
python ../../tests/run_tests.py      # 离线单元测试，不需要密钥
```

## 用起来

```bash
python scripts/ask.py "GLP-1 受体激动剂对青少年肥胖的效果如何？"
```

```
① 规划      模型把中文问题翻成英文检索式（2-3 条互补）
② 检索      PubMed E-utilities，去重、落题录
③ 筛选      摘要长度门槛 + 文献类型门槛 + 语义重排（可选）
④ 精读      逐篇抽取 PICO/样本量/效应量/局限，PMC 开放全文优先
⑤ 审查      横向找冲突、找空隙、给 GRADE 确定性分级
⑥ 成文      固定六段式，每条论断挂 PMID；参考文献由脚本拼
   └─ 落盘 11 个文件，全程可审计
```

## 它和别的 PubMed 工具不一样

| | 大多数同类工具 | 本项目 |
|---|---|---|
| 语言 | 英文提问英文答 | **中文提问中文答** |
| 引用可靠性 | 靠模型自觉写引用 | **参考文献由脚本生成，模型无权写**；正文引用逐条校验（PubMed 用 `[PMID:xxx]`、预印本用 `[DOI:xxx]`），越界即报警 |
| 可复现性 | 通常只给一段对话 | **11 个文件留痕**：检索式、题录、证据、审查意见、PRISMA 流程、模型命中、耗时 |
| 循证规范 | 无 | **GRADE 确定性分级（程序机械定级 + 模型复核）+ PRISMA 流程计数**，可直接用于系统评价的方法学部分 |
| 引用是否名副其实 | 只查 PMID 在不在池里 | **逐条核对"这篇真的支持这句话吗"**（语义比对，不支持的论断单独列出）；相关度由**两个模型交叉打分**取平均，分歧大的单独标记 |
| 结论完整性 | 靠模型自觉 | 答案八段结构**程序校验**，缺段或关键段无引用会**自动重写**；正文还必须交代"检索覆盖了什么、没覆盖什么" |
| 证据把关 | 无 | **撤稿文献自动拦截并标注**；**偏倚评估要素**结构化抽取；**PICO 匹配度**区分直接/间接证据；**预印本单独标记且证据等级下调** |
| 效应量 | 各说各的 | 同指标自动**合并效应量**（逆方差加权 + I² 异质性 + 随机效应敏感性） |
| 横向对比 | 跑两次人工对比 | `**--compare "A" "B"**` 并排给出证据量、GRADE、效应量、直接证据占比 |
| 漏检 | 只做关键词检索 | **检索式命中 0 自动修订重试** + **被引/相似文献链接扩展** + **引文滚雪球** + 年份下限自适应 + **Europe PMC 预印本**（`--preprints`） |
| 检索范围 | 常常默认限定某些刊 | **全 PubMed，不限刊源、不限学科**——不同专业背景都能用 |
| 成本 | 不知道花了多少 | 按模型分档计价，每次运行输出 token 与估算费用；检索结果**本地缓存**，重复检索不重复打 API |
| 安装成本 | `pip install biopython httpx ...` | **零第三方依赖**，`git clone` 就能跑 |
| 服务端点 | 绑定单一厂商 | **任意 OpenAI 兼容端点**，rerank 可选 |

## 面向交医用户的三个专门能力

### 1. 期刊评价：影响因子 + JCR 分区 + 中科院大区

每条证据都会标注它发表的刊物的量级：

| 期刊 | IF | JCR 分区 | 中科院大区 |
|---|---|---|---|
| N Engl J Med | 84.5 | Q1 | 1 区 |
| Lancet | 109 | Q1 | 1 区 |
| JAMA | 65.4 | Q1 | 1 区 |
| PLoS One | 2.8 | Q2 | 3 区 |

- 数据来自 **JCR 2026**（22,643 本刊）与**中科院分区表**，由
  `scripts/build_jcr_index.py` 合并成一份索引（`assets/jcr_2026_index.json`）。
- **JCR 分位（Q1–Q4）与中科院大区（1–4）是两套体系**，索引里分列两个字段，
  绝不混在一列——那是国内学术评价里最常见的误读来源。
- 对齐靠 **ISSN**（唯一稳定的连接键），刊名匹配只作兜底；查不到就留空，**绝不猜**。
- 实测真实 PubMed 题录 59 篇样本，命中 54 篇（91%），全部走 ISSN。

### 2. 全文获取：拿不到就告诉你去哪儿拿

自动降级链：**PMC 开放全文 → Europe PMC → Unpaywall → OpenAlex**。

- **能下载就下载**：OA 论文的 PDF 会存到本次运行目录的 `07_fulltext_pdf/`。
- **只想读不想留文件**：`--no-download` 让全文只在临时目录待一下，读完立刻删除
  （并确认删除成功，不留任何残留）。
- **拿不到就给可执行的指引**：答案里单列一节，逐条给出题名、DOI、
  PubMed / PMC 链接，并提醒——
  > 若提示付费墙或连接失败，请开启 网络代理 / 交大医学院 VPN，
  > 或通过交大图书馆校外访问入口获取。

代理自动探测：环境变量优先，其次 Windows 注册表的系统代理；也可用
`--fulltext-proxy http://127.0.0.1:7890` 手动指定。

### 3. 图表识别：把只存在于图里的效应量读出来

森林图上的 HR/OR/RR 与 95%CI、生存曲线、ROC 曲线——这些数字**常常只存在于图里**，
摘要和正文都没写，只看摘要会漏掉。

- 开启：`--figures`（默认关，因为每张图是一次多模态模型调用）
- 模型：默认 `Qwen/Qwen3-VL-8B-Instruct`（可 `--vl-model` 换成
  `Qwen/Qwen3-VL-30B-A3B-Instruct` 等更强的）
- 提示词明确要求「只抄图上真实出现的数值，读不清就写未见明确标注」——
  循证场景里编造效应量是严重错误
- 结果进证据表的「图表要点」列，完整内容另存 `05c_figures.md`

### 4. 连续提问：文献池是累积的

`--chat` 模式下追问同一课题时：

- 沿用上一轮的**检索式**（不重新从零检索）
- 沿用上一轮**已精读过的文献**（同一篇重读一次是一次完整的模型调用，
  而抽取结果几乎必然一样）——证据表里标 `↺沿用`
- 成本因此从「检索 + 全量精读 + 审稿 + 成文」降到
  「检索 + 只精读新文献 + 审稿 + 成文」

## 工具自己会做的检索增强

不需要用户要求，每次运行自动执行：

| 能力 | 行为 |
|---|---|
| **检索式质量闸门** | 命中 0 时自动修订检索式并重试（默认最多 2 轮） |
| **年份自适应** | "最新进展"放宽到近 3-5 年，"经典/机制"放宽到早年，一般疗效诊断近 10 年 |
| **被引/相似文献** | ELink 拉引用了种子文献的新文献与相似文献 |
| **引文滚雪球** | 从种子文献的参考文献标题回灌检索 |
| **排除纯动物实验** | 用官方 MeSH 写法，不会像自由词 `NOT` 那样被 PubMed 错解析 |
| **撤稿拦截** | 撤稿文献在进入精读前剔除，证据表标注 ⚠ |
| **第二轮扩写** | 候选不足时补充缩写/旧术语/同义表述再搜一轮 |

## 产物

每次运行生成一个目录：

```
runs/20261105-1830_GLP1受体激动剂对青少年肥胖/
  01_question.md        问题与限定
  02_plan.json          检索计划（核心问题 + PICO + 检索式 + 年份下限）
  03_papers.json        全部候选题录
  04_evidence.json      抽取的结构化证据（含偏倚要素、PICO 匹配度）
  05_evidence_table.md  ★ 证据总表 + 偏倚要素表 + 合并效应量 + GRADE 机械定级
  05b_excluded.json     被排除文献及排除理由
  05c_figures.md        图表识别结果（模型从插图里读出的内容与数值）
  06_fulltext/PMID*.txt 抓到的 PMC 开放全文
  06b_fulltext_guide.md ★ 全文获取指引（哪些只读了摘要、去哪儿拿全文）
  07_fulltext_pdf/      下载到本地的开放获取 PDF（--no-download 时不会有）
  07_answer.md          ★ 答案正文（每条论断挂 PMID）+ 关键文献
  08_bibliography.md    参考文献（脚本生成，非模型）
  09_log.txt            带时间戳的执行日志
  10_trace.json         模型命中 / 耗时 / 错误表 / PMID 校验 / token 与成本
  11_prisma.md          PRISMA 流程计数 + 排除理由汇总 + 全部检索式明细
```

**看答案只需 `07_answer.md` 和 `05_evidence_table.md`**；其余是过程留痕，供审稿与复现。
`runs/_index.md` 汇总所有历史运行。

## 常用命令

```bash
python scripts/ask.py "你的问题"                          # 基本用法
python scripts/ask.py --doctor                            # 缺什么、怎么补
python scripts/ask.py --setup                             # 配置向导
python scripts/ask.py --quick "你的问题"                  # 快速模式（约 3 分钟）
python scripts/ask.py --chat                              # 连续提问，上下文自动带
python scripts/ask.py "问题" --export bib                 # 顺带导出 BibTeX
python scripts/ask.py "问题" --export ris                 # 顺带导出 RIS（EndNote/Zotero 可导入）
python scripts/ask.py "问题" --export docx                # 顺带导出 Word
python scripts/ask.py "问题" --export notes               # 顺带导出 Zotero 笔记
python scripts/ask.py --compare "司美格鲁肽" "替尔泊肽"   # 两个干预横向对比
python scripts/ask.py --resume runs/20261105-1830_xxx     # 断点续跑
python scripts/ask.py "问题" --reuse-plan runs/xxx/02_plan.json   # 复用检索式
python scripts/ask.py "问题" --cq prognosis.narrow        # 按研究设计筛
python scripts/ask.py "问题" --preprints                 # 同时检索 bioRxiv/medRxiv
python scripts/ask.py "问题" --no-cache                  # 不用检索缓存（要可复现时）
python scripts/ask.py "问题" --no-animal-exclusion       # 不排除纯动物实验
python scripts/ask.py --models                            # 看模型路由链
python scripts/ask.py --pubmed-only '检索式'              # 只检索不调模型
python scripts/ask.py "问题" --figures                   # 开启图表识别（读图里的效应量）
python scripts/ask.py "问题" --no-download               # 全文读完即删，本地不留副本
python scripts/ask.py "问题" --fulltext-proxy http://127.0.0.1:7890   # 指定抓全文的代理
```

### 几个容易被忽略的行为

| 行为 | 说明 |
|---|---|
| `--quick` 到底快在哪 | 关掉第二轮扩写、两种链接扩展、**相关度交叉验证、论断语义比对**。**保留**检索式质量闸门——检索式写歪才是最大的坑 |
| `--compare` 的成本 | 会跑 N 份「检索 + 精读 + 分级」，但**跳过综述成文与语义比对**（对比用不上）。耗时与成本约为单次完整查询的 N 倍 |
| `--chat` 会沿用上一次的检索式 | 追问同一课题时保留上次的召回，再叠加新问题的概念；不会每次都从零重检 |
| `--no-cache` 什么时候用 | 检索结果缓存 72 小时。同一课题两次跑可能拿到不同结果，**要复现就加这个** |
| 证据表里的「来源」列 | 标明这条证据读的是**全文**还是**摘要**。只读了摘要时，"该研究未报告某结局"可能只是摘要没写 |
python scripts/ask.py --json "问题"                       # 机器可读输出（含答案与成本）
```

## 设计要点

- **模型只做需要判断力的四件事**：怎么问、怎么抽、怎么挑矛盾、怎么写。
  计数、过滤、排序、拼参考文献、校验 PMID —— 全部在程序侧。
- **参考文献不经模型**：大模型最容易编错年份和刊名，所以这一段干脆不让它碰。
- **检索式交给模型，过滤交给程序**：PubMed 会错解析 `NOT (A OR B)`（实测命中掉 85%），
  所以提示词明令不许写 NOT，排除综述改在程序侧按 `pub_types` 判。
- **PRISMA 计数是算出来的**：检出、去重、初筛、评估、纳入各环节的数字全部由程序统计，
  模型不允许自己数。
- **任何一环失败都不崩**：模型失败自动降级到链上下一个模型，检索式失败回退原问题，
  零命中放宽年份，重排失败退回原始顺序，**上游故障与"真的没检索到"严格区分**。
- **全程可审计**：`10_trace.json` 记录实际命中的模型、每阶段耗时、错误表、
  本次检索范围、以及正文 PMID 是否全部来自候选池。

## 常见问题

**国内访问 NCBI 报错**：NCBI 需要境外直连或代理
（PowerShell：`$env:HTTPS_PROXY="http://127.0.0.1:7890"`，换成你自己的代理端口）。
访问硅基流动**不需要**代理。

**提示 "Search Backend failed"**：这是 NCBI 检索后端的临时故障（常见于访问高峰），
不是"没有相关文献"。等一会儿重试即可。

**没有 rerank 端点的服务（DeepSeek / OpenAI）**：把 `config.json` 里
`provider.rerank.enabled` 改成 `false`，流程照常，只是排序退回 PubMed 原始相关度。

## 合规与声明

- 本工具经由 **NCBI E-utilities** 获取题录与摘要。**摘要的版权属于出版商或作者**，
  本项目不随仓库分发任何 PubMed 内容（全部运行时下载），仅在本地临时目录使用。
  请遵守 <https://www.ncbi.nlm.nih.gov/home/about/policies.shtml> 与
  <https://www.ncbi.nlm.nih.gov/about/disclaimer.html>。
- 本工具输出的是**文献证据综述**，**不构成医疗建议**。涉及具体患者的诊疗决策请咨询医生。
- 本项目为**个人开发的开源工具，非任何机构官方产品**。

## License

MIT © 详见 [LICENSE](LICENSE)。

---

<a name="english"></a>

## English

**medlit-agent** — an Agent Skill that turns a clinical or public-health question
into an auditable, evidence-based answer where **every substantive claim carries a
`[PMID:xxxx]` tag** and the reference list is generated by code, never by the model.

**Install**

```bash
npx -y skills add liangliangliuboyi/medlit-agent --skill medlit-agent
```

Or copy `skills/medlit-agent/` into your agent's skills directory
(`~/.workbuddy/skills/`, `~/.claude/skills/`, `~/.agents/skills/`), then fill in
`.env` with `SILICONFLOW_API_KEY` (required) and optionally `NCBI_API_KEY`, and run
`python scripts/selftest.py`.

**Highlights**

- Chinese-first: ask in Chinese, read in Chinese.
- **Zero third-party dependencies** — Python 3.9+ stdlib only.
- Any **OpenAI-compatible** endpoint (SiliconFlow / DeepSeek / Kimi / OpenAI / Ollama);
  reranking optional.
- Searches **all of PubMed** — no journal whitelist, no topic restriction.
- Emits a **PRISMA flow count** and a **GRADE certainty rating** alongside the narrative,
  plus 11 audit-trail files per run.
- **PMID boundary validation**: any citation in the answer that is not in the retrieved
  candidate pool is flagged as an error.

Data comes from **NCBI E-utilities**. Abstract copyrights belong to publishers and authors;
no PubMed content is redistributed in this repository. This tool summarizes literature and
**does not constitute medical advice**. Not an official product of any institution.
