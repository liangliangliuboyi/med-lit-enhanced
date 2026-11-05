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
python scripts/ask.py --doctor
```

它会逐项检查并给出**可执行的补救说明**（缺什么、写到哪、怎么拿）：

```
[✓] 模型服务密钥
      已配置（SILICONFLOW_API_KEY）
[·] NCBI API key（选填）
      未配置，限速 3 请求/秒（功能正常，只是稍慢）
```

**2. 填密钥。** 装好的目录里会有一个 `.env`（由 `.env.example` 生成）：

```
SILICONFLOW_API_KEY=你的模型服务密钥     # 必填
NCBI_API_KEY=你的NCBI密钥               # 选填，有则限速 3/s → 10/s
NCBI_EMAIL=你的邮箱                     # 建议填（NCBI 规定这里填开发者邮箱）
```

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
| 引用可靠性 | 靠模型自觉写引用 | **参考文献由脚本生成，模型无权写**；正文 PMID 逐条校验，越界即报警 |
| 可复现性 | 通常只给一段对话 | **11 个文件留痕**：检索式、题录、证据、审查意见、PRISMA 流程、模型命中、耗时 |
| 循证规范 | 无 | **GRADE 确定性分级（程序机械定级 + 模型复核）+ PRISMA 流程计数**，可直接用于系统评价的方法学部分 |
| 证据把关 | 无 | **撤稿文献自动拦截并标注**；**偏倚评估要素**结构化抽取；**PICO 匹配度**区分直接/间接证据 |
| 效应量 | 各说各的 | 同指标自动**合并效应量**（逆方差加权 + I² 异质性 + 随机效应敏感性） |
| 漏检 | 只做关键词检索 | **检索式命中 0 自动修订重试** + **被引/相似文献链接扩展** + **引文滚雪球** + 年份下限自适应 |
| 检索范围 | 常常默认限定某些刊 | **全 PubMed，不限刊源、不限学科**——不同专业背景都能用 |
| 安装成本 | `pip install biopython httpx ...` | **零第三方依赖**，`git clone` 就能跑 |
| 服务端点 | 绑定单一厂商 | **任意 OpenAI 兼容端点**，rerank 可选 |

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
  06_fulltext/PMID*.txt 抓到的 PMC 开放全文
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
python scripts/ask.py --resume runs/20261105-1830_xxx     # 断点续跑
python scripts/ask.py "问题" --reuse-plan runs/xxx/02_plan.json   # 复用检索式
python scripts/ask.py "问题" --cq prognosis.narrow        # 按研究设计筛
python scripts/ask.py "问题" --preprints                 # 同时检索 bioRxiv/medRxiv
python scripts/ask.py --models                            # 看模型路由链
python scripts/ask.py --pubmed-only '检索式'              # 只检索不调模型
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
