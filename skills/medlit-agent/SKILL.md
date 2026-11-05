---
name: medlit-agent
version: 2.1.0
description: >
  循证医学文献检索。当用户提出医学、临床、公共卫生、流行病学、药物疗效、诊断准确性、
  指南依据等需要循证依据的问题，或明确要求"查文献 / 找证据 / 做文献综述 / 系统评价 /
  PubMed 检索"时使用。自动把中文问题翻译成英文检索式，调用 PubMed E-utilities 取真实题录与
  摘要（有 PMC 开放全文则抓全文），逐篇抽取结构化证据、横向审查冲突与证据空隙、给出 GRADE
  确定性分级与 PRISMA 流程计数，最后生成每条论断都挂 PMID 的答案。检索范围为全 PubMed，
  不限刊源、不限学科，适配不同专业背景的使用者。适用于"我需要知道某个临床问题现在有什么证据"
  这类问题，不适用于纯概念解释、不需要文献支撑的闲聊，以及涉及具体患者诊疗决策的场景
  （应提示就医）。
repo: https://github.com/liangliangliuboyi/medlit-agent
---

# 循证医学文献检索

把"提一个问题"变成"一份可回查的循证答案"。核心是三条纪律：

1. **只基于检索到的真实文献作答**，每条实质性论断后面必须挂 `[PMID:xxxxxxxx]`
2. **参考文献由脚本生成**，取自 PubMed 原始字段；模型只负责写正文，禁止自己写期刊名、卷期、作者（这是最容易出错的地方）
3. **检索到的如果不够，就说不够**，不要用常识补空

---

## ⚠️ 合规声明（必读，不可删）

- 本工具经由 **NCBI E-utilities** 获取题录与摘要。**摘要的版权属于出版商或作者**。
  遵守 <https://www.ncbi.nlm.nih.gov/home/about/policies.shtml> 与
  <https://www.ncbi.nlm.nih.gov/about/disclaimer.html>。
- **本工具输出的是文献证据综述，不构成医疗建议。涉及具体患者的诊疗决策必须提示就医。**
- 分布式软件应向 NCBI 登记 `tool` 与 `email`（`vog.hin.mln.ibcn@seitilitue`），
  以便上游因故封禁 IP 时能联系到开发者。

---

## 零、安装（用户要求"安装这个技能"时照此执行）

**仓库地址：<https://github.com/liangliangliuboyi/medlit-agent>**

### 方式 A：让智能体装（推荐）

把仓库地址给智能体，它会读本文件并按下面的步骤完成安装。

### 方式 B：一行命令（任意 agent 通用）

```bash
npx -y skills add liangliangliuboyi/medlit-agent --skill medlit-agent
```

`skills` CLI 是 GitHub 作为 registry 的技能包管理器（<https://skills.sh>），
支持 Claude Code / Cursor / Codex 等，装到 `.claude/skills/`、`.agents/skills/` 等目录。

### 方式 C：手动拷贝（WorkBuddy 等自建 skills 目录的 agent）

```bash
git clone https://github.com/liangliangliuboyi/medlit-agent.git
cd medlit-agent
./install/install.sh workbuddy        # Windows: .\install\install.ps1 workbuddy
```

或者手动：把仓库里的 `skills/medlit-agent/` 整个目录复制到

| Agent | 目标目录 |
|---|---|
| WorkBuddy | `~/.workbuddy/skills/medlit-agent/` |
| Claude Code | `~/.claude/skills/medlit-agent/` |
| Codex / Cursor 等 | `~/.agents/skills/medlit-agent/` |

### 装完必做的两件事

**1. 填密钥。** 复制 `.env.example` 为 `.env`（安装脚本会自动生成），填入：

```
SILICONFLOW_API_KEY=你的模型服务密钥     # 必填
NCBI_API_KEY=你的NCBI密钥               # 选填，有则限速 3/s → 10/s
NCBI_EMAIL=你的邮箱                     # 建议填
```

**2. 自检。** 在技能目录下跑：

```bash
python scripts/selftest.py
```

自检 12 项：配置读取、临床查询表、检索范围无限制、模型服务、对话接口、PubMed 检索、
题录解析、PMC 全文、研究论文判定、PMID 越界校验、端到端迷你跑。
**遇到 NCBI 上游故障时相关项显示 `SKIP` 而不是 `FAIL`**——那不是安装问题，等上游恢复重跑即可。

### 关于模型服务

默认用**硅基流动**（国内直连、开箱即用）。任何 OpenAI 兼容端点都能换，改
`config.json` 的 `provider.base_url` 与 `provider.api_key_env` 即可：

| 端点 | `base_url` | 备注 |
|---|---|---|
| 硅基流动 | `https://api.siliconflow.cn/v1` | 默认，国内直连 |
| DeepSeek | `https://api.deepseek.com/v1` | 便宜，**无 rerank 端点** |
| Kimi | `https://api.moonshot.cn/v1` | 长上下文强 |
| OpenAI | `https://api.openai.com/v1` | 国际用户，**无 rerank 端点** |
| 本地 Ollama | `http://127.0.0.1:11434/v1` | 完全离线 |

**rerank 是可选增强**：没有 `/rerank` 端点时把 `provider.rerank.enabled` 设为 `false`，
流程照常运行，只是排序退回 PubMed 原始相关度。

---

## 一、什么时候用

**适用**：疗效比较、诊断准确性、危险因素、预后、指南依据、某方向近年进展、某药物安全性、系统评价素材。

**不适用**：名词解释、机制科普、不需要文献的操作性问题、涉及具体患者诊疗决策（应提示就医）。

## 二、怎么调用

在技能目录下执行（把引号里的内容换成用户的问题）：

```bash
python scripts/ask.py "问题原文" --constraints "人群/年份/地区等限定"
```

常用参数：

| 参数 | 作用 |
|---|---|
| `--quick` | 快速模式：单轮检索、少读几篇，约 3 分钟 |
| `--max-papers 8` | 精读篇数上限（默认 8） |
| `--fulltext-limit 4` | 尝试抓全文的篇数（默认 4） |
| `--research-only` | 只保留研究论文，剔除综述/病例报告/评论（默认**不**剔除，因为系统综述/网状 Meta 对"效果如何"类问题价值最高） |
| `--cq therapy.narrow` | 套用 PubMed 官方临床查询过滤器（**默认不启用**，按研究设计筛，如 therapy/diagnosis/prognosis/rct/review） |
| `--list-cq` | 列出所有可用过滤器 |
| `--rounds 2` | 检索轮数（2 = 候选不足时自动扩写一轮） |
| `--models` | 打印模型路由链与平台可用性 |
| `--pubmed-only "检索式"` | 只检索不调模型，用于核对检索式 |
| `--json` | 输出机器可读摘要 |

**调用顺序建议**：先跑一次 `--quick` 确认命中和方向对不对，再跑完整版。

## 三、检索范围

> **本工具检索全 PubMed，不限刊源、不限学科、不限主题。**
> 任何期刊、任何出版社、任何专业方向的研究都会进入候选。
> 这是刻意的设计——使用者来自不同专业背景（临床、公卫、药学、流行病学……），
> 工具不应该替他们决定"该看哪些刊"。
> 每次运行会在 `09_log.txt` 第一屏和 `10_trace.json` 的 `scope_on` 字段里写明本次范围，便于回查。

## 四、输出在哪

每次运行在 `paths.runs_root` 下新建一个目录（默认 `./runs/`）：

```
20261105-1830_GLP1受体激动剂对青少年肥胖/
  01_question.md        问题与限定
  02_plan.json          检索计划（英文核心问题 + PICO + 检索式）
  03_papers.json        全部候选题录（含摘要、MeSH、关键词、PMCID）
  04_evidence.json      精读抽取的结构化证据
  05_evidence_table.md  ★ 证据总表（一眼看完所有文献的设计/样本/结论）
  06_fulltext/          抓到的开放全文（若有）
  07_answer.md          ★ 答案正文（每条论断挂 PMID）
  08_bibliography.md    参考文献（脚本生成，权威）
  09_log.txt            执行日志
  10_trace.json         模型调用、耗时、错误表、PMID 校验结果
  11_prisma.md          PRISMA 流程计数 + 全部检索式明细
```

**读答案只看 `07_answer.md` 和 `05_evidence_table.md`**，其余是过程留痕。

## 五、拿到结果之后怎么用

1. 读 `07_answer.md`，核对每段是否都有 PMID
2. 读 `05_evidence_table.md`，确认关键数字与文献对得上
3. 读 `11_prisma.md`，PRISMA 计数可直接用于系统评价的方法学部分
4. 按用户关注点重写：该压缩的压缩、该补背景的补背景
5. **区分三件事**：文献说了什么 / 证据有多强（看 GRADE）/ 我的推断是什么
6. 如果某一节文献不足，明确告诉用户"这块证据薄"，不要糊过去

## 六、模型怎么选

`config.json` 的 `models.chain` 按角色配置了路由链，脚本会先探测平台可用模型，按顺序取第一个可用的：

- **planner**（规划检索式 + 第二轮扩写）→ DeepSeek-V3.2
- **reader**（逐篇抽取证据）→ DeepSeek-V3.2（长上下文）
- **critic**（挑方法学缺陷、判冲突、给 GRADE 分级）→ DeepSeek-R1
- **writer**（成文）→ DeepSeek-V3.2
- **rerank**（语义重排，可选）→ Qwen3-Reranker-8B

改模型只改 `config.json`，不用动代码。`--models` 可以看实际命中的是哪几个。

## 七、常见问题

**命中太少**：说明检索式过窄。把问题拆成 2–3 个同义词组重跑，或去掉 `--research-only` 看是不是被过滤光了。

**命中太多但都不相关**：加 `--constraints "2020 年后"` 收紧时间，或用 `--cq` 套用更窄的研究设计过滤器。

**抓不到全文**：正常。PubMed 只有开放获取（PMC）才有全文，多数临床刊只有摘要。摘要在多数情况下够用；不够时答案里会注明"仅摘要"。

**回答里出现没见过的文献**：不应该发生。参考文献由脚本按 PMID 回查生成；若正文引用了不在候选池里的 PMID，`10_trace.json` 的 `errors` 与 `pmid_check` 字段会报警。

**报 "Search Backend failed / Search is temporarily unavailable"**：这是 **NCBI 检索后端临时故障**，不是"没有相关文献"。等一会儿重试即可，工具已把这种情况与真正的零命中区分开。

**国内访问 NCBI 报错**：NCBI 需要境外直连或代理。PowerShell 里设 `$env:HTTPS_PROXY="http://127.0.0.1:7890"`（换成你自己的代理端口）。访问硅基流动**不需要**代理。

## 八、维护

- 换 API key：改 `.env`（或系统环境变量），**不要写进 config 文件**
- 换模型 / 换端点：改 `config.json` 的 `provider` 与 `models.chain`
- 改输出目录：改 `config.json` 的 `paths.runs_root`
- 加 Clinical Query 过滤器：改 `data/clinical_queries.json`
- 改写作风格：`scripts/prompts.py` 的 `WRITER_SYSTEM`
- 自检：`python scripts/selftest.py`

⚠️ 仓库里**不得出现任何明文密钥**。`.env` / `config.json` / `config.local.json` 都已在 `.gitignore` 中。
