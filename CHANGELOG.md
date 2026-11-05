# 更新日志

## v2.0.0 —— 2026-11-05

开源化重构。这是本项目的第一个公开版本，也是内部完善版的第一次结构化整理。

### 新增

- **学科域预设系统**：通用底座 + 可插拔域预设。内置 `diabetes-metabolism`、
  `chronic-care-primary`、`evidence-medicine`、`public-health-policy` 四个。
  按问题关键词自动识别，也可用 `--preset` 显式指定、`--no-preset` 关闭。
  用户往 `presets/` 目录丢 JSON 即可扩展，**不需要改代码**。
- **期刊画像**（`data/journal_profiles.json`）：`clinical`（临床核心刊 51 本）/
  `basic_science`（Nature-Cell 系 26 本）/ `public_health`（公卫与政策 31 本）。
  每套同时提供 NLM 缩写（`ta`）与全称（`full`）两种匹配串。
- **PubMed 官方 Clinical Query 过滤器**（`data/clinical_queries.json`）：11 个，
  含 therapy / diagnosis / prognosis / etiology / rct / review / observational / guideline。
  由 planner 按问题类型自动选择，也可用 `--cq` 强制指定。
- **GRADE 确定性分级**：critic 阶段输出 `certainty`（高/中/低/极低）与定级依据。
- **PRISMA 流程计数**：新增 `11_prisma.md`，含检出、去重、初筛、评估、纳入各环节数字
  与全部检索式明细。**计数全部由程序统计，模型不允许自己数。**
- **PMID 越界校验**：正文出现的每个 PMID 必须来自候选池，越界即写入
  `10_trace.json` 的 `errors` 并在日志报警。这是唯一能自动发现"模型编文献"的机制。
- **第二轮检索式扩写**：`rounds: 2` 时，候选不足会由 planner 扩写 1-2 条检索式提高召回。
- **provider 抽象**：任何 OpenAI 兼容端点（硅基流动 / DeepSeek / Kimi / OpenAI / Ollama）都能用；
  `rerank` 变为可选增强，无该端点时自动退回原始顺序。
- **`--list-presets`**：列出所有学科域预设。

### 修复

- **明文密钥**：`config.json` 里的 API key 全部改为环境变量 / `.env` 读取，
  仓库不再含任何密钥。`.env` / `config.json` / `config.local.json` 已 gitignore。
- **NCBI 400 误报**：上游故障时返回 HTTP 200 + `ERROR` 字段，旧代码会当成"命中 0 条"，
  现在显式识别并区分"上游挂了"与"真的没检索到"。
- **`fulltext_limit` 并发竞态**：改用锁 + 原子计数，避免并发下超量抓取 PMC 全文。
- **`retrieval.rounds` 死配置**：原先配置里有但编排逻辑从不读取，现已实现为真实的扩写轮。
- **`--research-only` 硬编码阈值**：摘要门槛原来写死 400 字，现改为读配置
  `research_min_abstract_chars`（默认 300），与 `min_abstract_chars`（默认 200）区分开。
- **自检区分上游故障**：NCBI 不可用时相关项显示 `SKIP` 而非 `FAIL`。

### 变更

- 产物从 10 个文件增加到 **11 个**（新增 `11_prisma.md`）。
- 输出目录从 `E:/workbuddyplace/med-lit-runs` 改为可配置（默认 `./runs`），
  不再绑定任何特定工作空间。
- 项目名 `med-lit-enhanced` → `medlit-agent`，`tool` 标识改为 `medlit-agent`。
- 提示词全面重构：planner 增加 `clinical_query` 输出字段；critic 增加 GRADE；
  writer 的「证据概况」段落同时接收检索概况与 PRISMA 计数。

### 合规

- 补入 NCBI 免责声明与版权声明（`SKILL.md` 首屏 + `README.md` + `LICENSE`）。
- `pubmed.email` 改为配置项，需使用者填自己的开发者邮箱，并按 NCBI 要求登记
  `tool` + `email`。
