# medlit-agent · 循证医学文献检索 Agent

> 把"提一个问题"变成"一份可回查的循证答案"。
> 每条实质性论断都挂 `[PMID:xxxx]`，参考文献由脚本从 PubMed 原始字段生成，**模型碰不到期刊名和年份**。

[English](#english-readme) | 许可证：MIT | Python 3.9+ | **零第三方依赖**

---

## 这是什么

一个给医学生、临床医生与公共卫生研究者用的 **Agent 技能（Agent Skill）**：
你用中文提出一个临床或公卫问题，它自己规划检索式、检索 PubMed、逐篇精读、
挑出证据冲突、最后写出一份**每句话都能点回原文**的循证答复。

```
用户问题（中文）
   │
① 规划      模型把中文问题翻成英文检索式（2-3 条互补），可套用 PubMed 官方 Clinical Query
   │
② 检索      PubMed E-utilities，去重、落题录
   │
③ 筛选      摘要长度门槛 + 文献类型门槛 + 语义重排（可选）
   │
④ 精读      逐篇抽取 PICO/样本量/效应量/局限，PMC 开放全文优先
   │
⑤ 审查      横向找冲突、找空隙、给 GRADE 确定性分级
   │
⑥ 成文      固定六段式，每条论断挂 PMID；参考文献由脚本拼
   │
   └─ 落盘 11 个文件，全程可审计
```

## 为什么和别的 PubMed 工具不一样

| | 大多数同类工具 | 本项目 |
|---|---|---|
| 语言 | 英文提问英文答 | **中文提问中文答** |
| 引用可靠性 | 靠模型自觉写引用 | **参考文献由脚本生成，模型无权写**；正文 PMID 逐条校验，越界即报警 |
| 可复现性 | 通常只给一段对话 | **11 个文件留痕**：检索式、题录、证据、审查意见、PRISMA 流程、模型命中、耗时 |
| 循证规范 | 无 | **GRADE 确定性分级 + PRISMA 流程计数**，直接可用进系统评价 |
| 学科适配 | 通用 | **通用底座 + 可插拔域预设**（内置糖尿病代谢、慢病基层、循证医学、公共卫生政策四个） |
| 安装成本 | `pip install biopython httpx ...` | **零第三方依赖**，`git clone` 就能跑 |
| 服务端点 | 绑定单一厂商 | **任意 OpenAI 兼容端点**，rerank 可选 |

## 快速开始

```bash
git clone https://github.com/<你的用户名>/medlit-agent.git
cd medlit-agent

# 1) 配置密钥（推荐用 .env；也可直接设系统环境变量）
cp .env.example .env      # Windows PowerShell: copy .env.example .env
# 编辑 .env，填入 SILICONFLOW_API_KEY

# 2) 自检（约 2 分钟，逐环节体检）
python scripts/selftest.py

# 3) 提问
python scripts/ask.py "GLP-1 受体激动剂对青少年肥胖的效果如何？"
```

> 国内用户注意：访问 NCBI 可能需要代理
> （PowerShell：`$env:HTTPS_PROXY="http://127.0.0.1:7890"`）。访问硅基流动**不需要**代理。

## 配置

### 模型服务端点（必填）

默认是**硅基流动**（国内直连、开箱即用）。任何 OpenAI 兼容端点都能换：

```jsonc
// config.json
{
  "provider": {
    "name": "siliconflow",                  // 自己起个名
    "base_url": "https://api.siliconflow.cn/v1",
    "api_key_env": "SILICONFLOW_API_KEY",   // 只声明"去哪读"，不存 key
    "rerank": {
      "enabled": true,
      "model": "Qwen/Qwen3-Reranker-8B",
      "endpoint": "https://api.siliconflow.cn/v1/rerank"
    }
  }
}
```

| 端点 | `base_url` | 备注 |
|---|---|---|
| 硅基流动 | `https://api.siliconflow.cn/v1` | 默认；国内直连；模型池丰富（99 个） |
| DeepSeek | `https://api.deepseek.com/v1` | 便宜；**无 rerank 端点**，关掉 `rerank.enabled` 即可 |
| Kimi | `https://api.moonshot.cn/v1` | 长上下文强 |
| OpenAI | `https://api.openai.com/v1` | 国际用户；**无 rerank 端点** |
| 本地 Ollama | `http://127.0.0.1:11434/v1` | 完全离线（需自备够用的模型） |

> **rerank 是可选增强。** 关掉后流程照常运行，只是排序退回"PubMed 相关度原始顺序"，精读篇数不变。

### NCBI API key（选填但强烈建议）

- 无 key：限速 **3 请求/秒**（本项目默认节流 2.8/s，够用）
- 有 key：限速 **10 请求/秒**
- 申请：<https://www.ncbi.nlm.nih.gov/account/settings/> → API Key Management

### NCBI `tool` / `email`（分布式软件必填）

NCBI 要求分布式软件登记 `tool` 与 `email`，否则违规 IP 会被封禁：

```jsonc
"pubmed": {
  "tool": "medlit-agent",          // 项目级标识，不要用个人标识
  "email": "you@example.com",      // 软件开发者邮箱（你自己的）
  "api_key_env": "NCBI_API_KEY"
}
```

## 命令行用法

```bash
# 基础提问
python scripts/ask.py "二甲双胍对青少年肥胖的疗效如何？"

# 指定学科域预设 + 限定临床核心刊 + 套用官方临床查询过滤器
python scripts/ask.py "TIR 与糖尿病微血管并发症风险" \
  --preset diabetes-metabolism --journals clinical --cq prognosis.narrow

# 快速模式（约 3 分钟）
python scripts/ask.py "问题" --quick

# 只检索不调模型（核对检索式用）
python scripts/ask.py --pubmed-only '("Diabetes Mellitus, Type 2"[MeSH] AND obesity[tiab])'

# 看模型路由链与平台实际可用模型
python scripts/ask.py --models

# 列出所有学科域预设
python scripts/ask.py --list-presets
```

### 参数表

| 参数 | 作用 | 默认 |
|---|---|---|
| `--constraints` | 补充限定（人群/年份/地区） | 空 |
| `--quick` | 快速模式：单轮、少读几篇 | 关 |
| `--max-papers N` | 精读篇数上限 | 8 |
| `--fulltext-limit N` | 尝试抓 PMC 全文的篇数 | 4 |
| `--research-only` | 只保留研究论文 | 关（默认保留综述，系统综述对"效果如何"类问题价值最高） |
| `--journals [画像]` | 限定期刊；不给值用预设画像 | 关（默认**检索全 PubMed**） |
| `--journal-list "A,B"` | 临时指定刊名，自动开启限定 | — |
| `--journal-mode ta\|full` | 刊名匹配方式 | ta（NLM 缩写） |
| `--preset ID` | 指定学科域预设 | 自动识别 |
| `--no-preset` | 关闭预设 | 关 |
| `--cq ID[,ID]` | PubMed 官方 Clinical Query 过滤器 | 由模型按问题类型选 |
| `--rounds N` | 检索轮数（2 = 必要时做一轮扩写） | 2 |
| `--models` | 打印模型路由链 | — |
| `--pubmed-only TERM` | 只检索不调模型 | — |
| `--json` | 输出机器可读摘要 | — |

## 学科域预设

内置四个（`data/presets.json`），**你可以直接改，也可以把自己的 JSON 丢进 `presets/` 目录，无需改代码**：

| id | 名称 | 期刊画像 | 适用 |
|---|---|---|---|
| `diabetes-metabolism` | 糖尿病 · 肥胖 · 代谢病 | clinical | 2 型糖尿病、肥胖、胰岛素抵抗、降糖减重药、CGM/TIR |
| `chronic-care-primary` | 慢病管理 · 基层防治 | public_health | 慢病防控、基层/家庭医生、分级诊疗、依从性 |
| `evidence-medicine` | 循证医学 · 卫生技术评估 | clinical | 系统评价、GRADE、指南制定、诊断准确性、HTA |
| `public-health-policy` | 公共卫生 · 卫生政策 | public_health | 疾病负担、健康公平、政策评估、环境与职业健康 |

不指定 `--preset` 时会按问题关键词自动识别；识别不到就走通用底座。

### 期刊画像

`clinical`（临床核心刊）/ `basic_science`（Nature-Cell 系）/ `public_health`（公卫与政策）。
每个画像同时提供 NLM 缩写（`ta`）与全称（`full`）两套匹配串。

## 产物

每次运行生成一个目录：

```
runs/20261105-1830_GLP1受体激动剂对青少年肥胖/
  01_question.md        问题、限定、所用预设
  02_plan.json          检索计划（英文核心问题 + PICO + 检索式 + 所选过滤器）
  03_papers.json        全部候选题录（摘要/MeSH/关键词/PMCID）
  04_evidence.json      逐篇抽取的结构化证据
  05_evidence_table.md  ★ 证据总表 + 检索概况
  06_fulltext/PMID*.txt 抓到的 PMC 开放全文
  07_answer.md          ★ 答案正文（每条论断挂 PMID）+ 关键文献
  08_bibliography.md    参考文献（脚本生成，非模型）
  09_log.txt            带时间戳的执行日志
  10_trace.json         模型命中 / 各阶段耗时 / 错误表 / PMID 校验结果
  11_prisma.md          PRISMA 流程计数 + 全部检索式明细
```

**看答案只需 `07_answer.md` 和 `05_evidence_table.md`**；其余是过程留痕，供审稿与复现。

## 设计要点

- **模型只做需要判断力的四件事**：怎么问、怎么抽、怎么挑矛盾、怎么写。
  计数、过滤、排序、拼参考文献、校验 PMID —— 全部在程序侧。
- **参考文献不经模型**：大模型最容易编错年份和刊名，所以这一段干脆不让它碰。
- **检索式交给模型，过滤交给程序**：PubMed 会错解析 `NOT (A OR B)`（实测命中掉 85%），
  所以提示词明令不许写 NOT，排除综述改在程序侧按 `pub_types` 判。
- **PRISMA 计数是算出来的**：检出、去重、初筛、评估、纳入各环节的数字全部由程序统计，
  模型不允许自己数。
- **任何一环失败都不崩**：11 条降级路径，模型失败自动降级到链上下一个模型，
  检索式失败回退原问题，零命中放宽年份，重排失败退回原始顺序。
- **全程可审计**：`10_trace.json` 记录实际命中的模型、每阶段耗时、错误表、
  **本次是否限刊**、以及正文 PMID 是否全部来自候选池。

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

<a name="english-readme"></a>

# English README

**medlit-agent** — an Agent Skill that turns a clinical or public-health question
into an auditable, evidence-based answer where **every substantive claim carries a
`[PMID:xxxx]` tag** and the reference list is generated by code, never by the model.

- Chinese-first: ask in Chinese, read in Chinese.
- **Zero third-party dependencies** — Python 3.9+ stdlib only.
- Any **OpenAI-compatible** model endpoint (SiliconFlow / DeepSeek / Kimi / OpenAI / Ollama);
  reranking is optional.
- Pluggable **discipline presets** (diabetes & metabolism, chronic-care & primary care,
  evidence-based medicine, public health & policy).
- Emits a **PRISMA flow count** and a **GRADE certainty rating** alongside the narrative,
  plus 11 audit-trail files per run.
- Out-of-the-box **PMID boundary validation**: any citation in the answer that is not in
  the retrieved candidate pool is flagged as an error.

```bash
git clone https://github.com/<your-id>/medlit-agent.git
cd medlit-agent
cp .env.example .env      # fill in SILICONFLOW_API_KEY
python scripts/selftest.py
python scripts/ask.py "What is the efficacy of GLP-1 receptor agonists for adolescent obesity?"
```

Data comes from **NCBI E-utilities**. Abstract copyrights belong to publishers and authors;
no PubMed content is redistributed in this repository. This tool summarizes literature and
**does not constitute medical advice**. Not an official product of any institution.
