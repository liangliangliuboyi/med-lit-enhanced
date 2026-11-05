---
name: medlit-agent
version: 2.2.0
description: >
  循证医学文献检索。当用户提出医学、临床、公共卫生、流行病学、药物疗效、诊断准确性、
  指南依据等需要循证依据的问题，或明确要求"查文献 / 找证据 / 做文献综述 / 系统评价 /
  PubMed 检索"时使用。自动把中文问题翻译成英文检索式（命中为零会自动修订重试），调用
  PubMed E-utilities 取真实题录与摘要（有 PMC 开放全文则抓全文），并用被引/相似文献链接与
  引文滚雪球补漏；逐篇抽取结构化证据与偏倚评估要素，横向审查冲突与证据空隙，给出 GRADE
  确定性分级（程序机械定级 + 模型复核）与 PRISMA 流程计数，最后生成每条论断都挂 PMID 的答案。
  检索范围为全 PubMed，不限刊源、不限学科，适配不同专业背景的使用者。不适用于纯概念解释、
  不需要文献支撑的闲聊，以及涉及具体患者诊疗决策的场景（应提示就医）。
repo: https://github.com/liangliangliuboyi/medlit-agent
---

# 循证医学文献检索

把"提一个问题"变成"一份可回查的循证答案"。三条纪律：

1. **只基于检索到的真实文献作答**，每条实质性论断后面必须挂 `[PMID:xxxxxxxx]`
2. **参考文献由脚本生成**，取自 PubMed 原始字段；模型只负责写正文，禁止自己写期刊名、卷期、作者
3. **检索到的如果不够，就说不够**，不要用常识补空

---

## ⚠️ 合规声明（必读，不可删）

- 本工具经由 **NCBI E-utilities** 获取题录与摘要。**摘要的版权属于出版商或作者**。
  遵守 <https://www.ncbi.nlm.nih.gov/home/about/policies.shtml> 与
  <https://www.ncbi.nlm.nih.gov/about/disclaimer.html>。
- **本工具输出的是文献证据综述，不构成医疗建议。涉及具体患者的诊疗决策必须提示就医。**
- **本工具只检索 PubMed（英文国际文献），不检索中文数据库。**
  当问题涉及中文指南、中文共识或中文期刊研究时，答案里必须明确写
  「本轮检索未覆盖中文文献（仅检索了 PubMed 国际文献）」，**不要含糊带过，也不要编造中文文献**。
- `pubmed.tool` 是项目级标识；`pubmed.email` 按 NCBI 规定填**软件开发者**的邮箱
  （不是终端使用者的），使用者可在自己的 `config.json` 里改成自己的。

---

## 零、安装（用户要求"安装这个技能"时照此执行）

**仓库地址：<https://github.com/liangliangliuboyi/medlit-agent>**

### 方式 A：让智能体装（推荐）

把仓库地址给智能体，它会读本文件并按下面步骤完成安装。

### 方式 B：一行命令（任意 agent 通用）

```bash
npx -y skills add liangliangliuboyi/medlit-agent --skill medlit-agent
```

`skills` CLI 以 GitHub 为 registry（<https://skills.sh>），支持 Claude Code / Cursor / Codex 等。

### 方式 C：手动拷贝

```bash
git clone https://github.com/liangliangliuboyi/medlit-agent.git
cd medlit-agent
./install/install.sh workbuddy        # Windows: .\install\install.ps1 workbuddy
```

或手动把仓库里的 `skills/medlit-agent/` 整个目录复制到：

| Agent | 目标目录 |
|---|---|
| WorkBuddy | `~/.workbuddy/skills/medlit-agent/` |
| Claude Code | `~/.claude/skills/medlit-agent/` |
| Codex / Cursor 等 | `~/.agents/skills/medlit-agent/` |

---

## 一、装完必做：**先体检，缺什么就主动问用户**

> **这是智能体拿到技能后的第一步，也是最重要的一步。**
> 不要假设用户配好了密钥。先跑体检，把缺的东西一次性说清楚，让用户补齐或代填。

```bash
cd <装好的技能目录>
python scripts/ask.py --doctor
```

体检会逐项检查并给出**可执行的补救说明**（缺什么、写到哪、怎么拿）：

```
[✓] 模型服务密钥
      已配置（SILICONFLOW_API_KEY）
[·] NCBI API key（选填）
      未配置，限速 3 请求/秒（功能正常，只是稍慢）
[·] NCBI 开发者邮箱（建议）
      建议填写 NCBI 开发者邮箱（可选项）：...
```

**如果体检发现缺项，你要做的是：**

1. 把缺的项目和补救方式**原样转述给用户**，用中文说清楚缺什么、为什么需要、不补会有什么影响。
2. **主动问用户要**（不要自己瞎填）：模型服务密钥必填；NCBI key 和邮箱是选填，
   但要说明"不填也能用，只是限速低一点"。
3. 用户愿意让你代填时，运行 `python scripts/ask.py --setup` 由向导采集并写入 `.env`。
4. 补完再跑一次 `--doctor`，确认全绿，再开始正式检索。

如果用户明确说"先不配，直接跑"，那就直接跑——缺模型密钥会报明确错误并给出补救说明，
**不要因为缺 NCBI key（选填）就拒绝工作**。

---

## 二、怎么调用

```bash
python scripts/ask.py "问题原文" --constraints "人群/年份/地区等限定"
```

### 常用参数

| 参数 | 作用 |
|---|---|
| `--setup` | 首次配置向导（采集密钥写入 `.env`，并自动自检） |
| `--doctor` | 环境体检：缺什么、怎么补 |
| `--quick` | 快速模式：单轮检索、少读几篇，约 3 分钟 |
| `--max-papers 8` | 精读篇数上限（默认 8） |
| `--fulltext-limit 4` | 尝试抓 PMC 全文的篇数（默认 4） |
| `--research-only` | 只保留研究论文（默认**不**剔除，系统综述对"效果如何"类问题价值最高） |
| `--cq therapy.narrow` | 套用 PubMed 官方临床查询过滤器（**默认不启用**） |
| `--list-cq` | 列出所有可用过滤器 |
| `--no-animal-exclusion` | 不排除纯动物实验（默认排除） |
| `--no-links` / `--no-snowball` | 关闭被引/相似文献扩展 / 引文滚雪球 |
| `--preprints` | 同时检索 bioRxiv / medRxiv 预印本（默认关） |
| `--export bib\|ris\|notes\|docx` | 跑完后额外导出 BibTeX / RIS / Zotero 笔记 / Word |
| `--reuse-plan FILE` | 复用上次的 `02_plan.json`，不重新规划检索式 |
| `--resume DIR` | 从上次运行的目录断点续跑 |
| `--chat` | 连续提问模式，上下文自动带 |
| `--rounds 2` | 检索轮数（2 = 候选不足时自动扩写一轮） |
| `--models` | 打印模型路由链与平台可用性 |
| `--pubmed-only "检索式"` | 只检索不调模型，用于核对检索式 |
| `--json` | 输出机器可读摘要（含答案正文、PRISMA、GRADE、token 成本） |

**建议顺序**：先 `--doctor` → 再 `--quick` 确认命中方向 → 再跑完整版。

## 三、检索范围（重要）

> **本工具检索全 PubMed，不限刊源、不限学科、不限主题。**
> 任何期刊、任何出版社、任何专业方向的研究都会进入候选。
> 使用者来自不同专业背景，工具不应该替他们决定"该看哪些刊"。
>
> **但只有英文国际文献。** 中文文献不在覆盖范围内，答案里要如实说明。

## 四、工具自己会做的检索增强（不需要用户要求）

| 能力 | 行为 |
|---|---|
| **检索式质量闸门** | 检索命中 0 时自动让模型修订检索式并重试（默认最多 2 轮），仍为 0 才降级 |
| **年份自适应** | "最新进展"类问题放宽到近 3-5 年；"经典/机制"类放宽到早年；一般疗效诊断问题近 10 年 |
| **被引/相似文献** | 用 ELink 拉取引用了种子文献的新文献与相似文献，补关键词检索的漏检 |
| **引文滚雪球** | 从种子文献的参考文献标题回灌检索 |
| **排除纯动物实验** | 用官方 MeSH 写法（`NOT "Animals"[MeSH] NOT "Humans"[MeSH]`），比自由词 NOT 稳 |
| **撤稿拦截** | 撤稿文献在进入精读前被剔除，并在证据表标注 ⚠ |
| **预印本** | `--preprints` 开启后检索 Europe PMC 的预印本（bioRxiv/medRxiv）。预印本没有 PMID，用 `[DOI:10.1101/xxx]` 引用，证据等级按未同行评议下调 |
| **第二轮扩写** | 候选不足时让模型补充缩写/旧术语/同义表述再搜一轮 |

## 五、输出在哪

每次运行在 `paths.runs_root` 下新建一个目录（默认 `./runs/`）：

```
20261105-1830_GLP1受体激动剂对青少年肥胖/
  01_question.md        问题与限定
  02_plan.json          检索计划（核心问题 + PICO + 检索式 + 年份下限）
  03_papers.json        全部候选题录
  04_evidence.json      精读抽取的结构化证据（含偏倚评估要素、PICO 匹配度）
  05_evidence_table.md  ★ 证据总表 + 偏倚要素表 + 合并效应量 + GRADE 机械定级
  05b_excluded.json     被排除文献及排除理由
  06_fulltext/PMID*.txt 抓到的 PMC 开放全文
  07_answer.md          ★ 答案正文（每条论断挂 PMID）
  08_bibliography.md    参考文献（脚本生成，非模型）
  09_log.txt            执行日志
  10_trace.json         模型命中 / 耗时 / 错误表 / PMID 校验 / token 与成本
  11_prisma.md          PRISMA 流程计数 + 排除理由汇总 + 检索式明细
  _index.md（在上层目录）所有历史运行的汇总索引
```

**看答案只看 `07_answer.md` 和 `05_evidence_table.md`**，其余是过程留痕。

## 六、拿到结果之后怎么用

1. 读 `07_answer.md`，核对每段是否都有 PMID
2. 读 `05_evidence_table.md`，确认关键数字与文献对得上
3. 读 `11_prisma.md`，PRISMA 计数与排除理由可直接用于系统评价的方法学部分
4. 按用户关注点重写：该压缩的压缩、该补背景的补背景
5. **区分三件事**：文献说了什么 / 证据有多强（看 GRADE）/ 我的推断是什么
6. 某节文献不足就明确说"这块证据薄"，不要糊过去

## 七、模型怎么选

`config.json` 的 `models.chain` 按角色配置路由链，脚本先探测平台可用模型再取第一个可用的：

- **planner**（规划检索式 + 修订 + 扩写）→ DeepSeek-V3.2
- **reader**（逐篇抽取证据 + 偏倚要素）→ DeepSeek-V3.2（长上下文）
- **critic**（挑方法学缺陷、判冲突、复核 GRADE）→ DeepSeek-R1
- **writer**（成文）→ DeepSeek-V3.2
- **rerank**（语义重排，可选）→ Qwen3-Reranker-8B

**端点可换**：任何 OpenAI 兼容端点（硅基流动 / DeepSeek / Kimi / OpenAI / 本地 Ollama）。
只有提供 `/rerank` 的服务能做语义重排；没有就关掉 `provider.rerank.enabled`，流程照常。

## 八、常见问题

**命中太少**：检索式命中 0 会自动修订重试；仍不行说明问题本身太窄，可加 `--constraints` 放宽或换问法。

**命中太多但不相关**：加 `--constraints "2020 年后"`，或用 `--cq` 套用更窄的研究设计过滤器。

**抓不到全文**：正常。PubMed 只有开放获取（PMC）才有全文，多数临床刊只有摘要。答案里会注明"仅摘要"。

**回答里出现没见过的文献**：不应该发生。参考文献由脚本按原始字段生成；正文若引用了不在候选池里的 `[PMID:xxx]`（PubMed 文献）或 `[DOI:xxx]`（预印本），`10_trace.json` 的 `errors` 与 `pmid_check` 会报警。

**报 "Search Backend failed / Search is temporarily unavailable"**：**NCBI 检索后端临时故障**，不是"没有相关文献"。等一会儿重试，工具已把这两种情况严格区分。

**国内访问 NCBI 报错**：NCBI 需要境外直连或代理（PowerShell：`$env:HTTPS_PROXY="http://127.0.0.1:7890"`，换成你自己的代理端口）。访问硅基流动**不需要**代理。

## 九、维护

- 换 API key：改 `.env`（或系统环境变量），**不要写进 config 文件**
- 换模型 / 换端点：改 `config.json` 的 `provider` 与 `models.chain`
- 改输出目录：改 `config.json` 的 `paths.runs_root`
- 加 Clinical Query 过滤器：改 `data/clinical_queries.json`
- 改写作风格：`scripts/prompts.py` 的 `WRITER_SYSTEM`
- 改定级规则：`scripts/grading.py`
- 自检：`python scripts/selftest.py`（逐环节体检；上游故障显示 SKIP 而非 FAIL）
- 单元测试：`python tests/run_tests.py`（离线，不需密钥）

⚠️ 仓库里**不得出现任何明文密钥**。`.env` / `config.json` / `config.local.json` 都已在 `.gitignore` 中。
