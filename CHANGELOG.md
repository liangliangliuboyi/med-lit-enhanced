# 更新日志

## v2.2.0 —— 2026-11-05

**把功能改进清单的第一批（A~H 组 40+ 项）全部实现。** 这一版是"能力大跃"，不是修修补补。

### 检索覆盖

- **A1 检索式质量闸门**：命中 0 时自动把原检索式回喂给 planner 修订并重试（默认 2 轮）。
- **A2 被引/相似文献扩展**：ELink 拉 `citedin` 与 `sim`，补关键词检索的漏检。
- **A3 引文滚雪球**：efetch 时解析 `ReferenceList` 标题，回灌 PubMed 检索。
- **A4 预印本**：新增 `scripts/preprints.py`。**Europe PMC 为主源**（支持真正的关键词检索，
  收录 bioRxiv/medRxiv/专利），bioRxiv 官方 API 作兜底。**不伪造 PMID**。
- **A5 中文文献边界声明**：writer 提示词明令不得声称检索了中文文献；检索概况与产物里标注
  "仅英文国际文献"。真要接中文库需要万方/CNKI 的可用授权。
- **A6 排除动物实验**：用官方 MeSH 写法（`NOT "Animals"[MeSH] NOT "Humans"[MeSH]`），
  避免自由词 `NOT` 被 PubMed 错解析。`--no-animal-exclusion` 可关。
- **A7 年份下限自适应**：planner 输出 `year_floor`（"最新进展"→近 3-5 年，"经典"→早年）。
- **A8 复用检索式**：`--reuse-plan <02_plan.json>`。

### 证据质量

- **B1 GRADE 机械定级**（新增 `scripts/grading.py`）：研究设计→起始等级，
  偏倚/不一致/不精确/间接四类降级，效应量与剂量反应升级。
  **模型在 critic 阶段复核并说明与机械等级是否一致**。
  发表偏倚因程序无法自动检测，只作备注不降级（证据少时降级是过度惩罚）。
- **B2 撤稿与勘误**：efetch 解析 `CommentsCorrections`；撤稿文献在过滤阶段**剔除**，
  勘误在证据表与正文标注 ⚠。
- **B3 偏倚评估要素**：reader 抽取 6 个结构化字段，`05_evidence_table.md` 出偏倚要素表。
- **B4 效应量合并**（新增 `scripts/meta.py`）：逆方差加权固定效应 + Q 检验 + I² +
  DerSimonian-Laird 随机效应；**OR 与 RR/HR 不混池**（不可折叠），MD 与 SMD 不混池。
- **B6 PICO 匹配度**：reader 输出「直接/部分/间接」；writer 要求间接证据单独成段；
  机械定级对间接证据降级。
- **B7 图表说明**：PMC BioC 解析时单独收集 figure captions。

### 输出可用性

- **C1/C2/C3 导出**（新增 `scripts/export.py`）：BibTeX（纯 ASCII 引用键、复姓连写）、
  RIS（EndNote/Zotero 可导入）、**Word**（纯标准库手写 OOXML，零第三方依赖，已用真实 Word 验证可打开）、
  Zotero 笔记。`--export bib|ris|notes|docx`。
- **C4 运行索引**：`runs/_index.md` 自动汇总。
- **C5 适用边界**：writer 输出新增「## 适用边界」段；critic 增加 `population_applicability`。
- **C6 排除理由**：新增 `05b_excluded.json` 与 PRISMA 里的排除理由汇总。

### 工程与维护

- **D1 单元测试**：新增 `tests/`，**107 个离线用例**（unittest，无需密钥）。
- **D2 CI**：`.github/workflows/ci.yml`，Python 3.9–3.12，windows 单独跑语法检查。
- **D3 token 成本**：按角色累计 token，填单价即出成本估算。
- **D4 断点续跑**：各阶段落 `_checkpoint.json`，`--resume <目录>`。
- **D5 并发自适应**：按实测检索延迟在 1–6 之间调并发度。
- **D6 `--json` 含正文**：答案、PRISMA、GRADE、合并效应量、token 与成本全在里面。
- **D7 中文报错**：密钥缺失、NCBI 上游故障、HTTP 错误全部中文并给补救方式。

### 安装与体验

- **E1 配置向导 + 主动提示**：`ask.py --setup` 交互向导；`ask.py --doctor` 环境体检。
  **缺密钥时报错信息本身就是一份可执行清单**；SKILL.md 明确要求智能体先体检、
  缺什么就主动问用户要什么。
- **E2 连续提问**：`ask.py --chat`，历史带入 planner 上下文，支持追问。

### 修掉的既有缺陷

| # | 问题 | 修复 |
|---|---|---|
| 1 | `_json_load` 用贪婪正则 `\{.*\}`，模型在 JSON 前后夹带含花括号的文字时会解析失败 | 改为括号配对扫描（正确处理嵌套与字符串内的括号） |
| 2 | PMID 正则不容许空格，`[PMID: 12345678]` 会被漏掉 | 改为 `\[PMID:\s*(\d+)\]` |
| 3 | 证据表只转义 `key_findings` 的竖线，其他单元格的 `\|` 会打散 Markdown 表格 | 统一走 `cell()` 转义 |
| 4 | 同一分钟跑两个相似问题会撞目录、后者覆盖前者 | 目录名末尾加 4 位哈希后缀 |
| 5 | `_load_cq` 静默返回空 dict，`--cq` 会"看起来生效但没效果" | 读不到文件时明确报错 |
| 6 | `expand_queries` 方法在重构中丢失，端到端直接 `AttributeError` | 补回 |
| 7 | 证据数少时机械定级因"无法评估发表偏倚"而降级，把高质量 RCT 误判成"低确定性" | 改为只记备注不降级 |
| 8 | BibTeX 引用键没取完整复姓（`van der Berg` → `van`），且允许非 ASCII | 取完整姓氏去空格连写 + 限定纯 ASCII |
| 9 | `pubmed.email` 按 NCBI 规定应填**开发者**邮箱，模板里空着 | 模板填开发者邮箱并注明使用者可覆盖 |

### 保留

GRADE 分级 ✅ ｜ PRISMA 计数 ✅ ｜ `--cq`（默认关）✅ ｜ provider 抽象 ✅ ｜
语义重排（可选）✅ ｜ 11 个留痕文件 ✅ ｜ 第二轮扩写 ✅ ｜ 检索范围不限制 ✅

## v2.1.0 —— 2026-11-05

**做减法**：删净刊源限制与学科域预设，改造为 Agent Skills 标准 `skills/` 布局，
支持 `npx -y skills add owner/repo --skill medlit-agent`，新增一键安装脚本。

## v2.0.0 —— 2026-11-05

开源化重构：密钥环境变量化、NCBI 免责声明、PMID 越界校验、provider 抽象、11 项自检。
（其中 v2.1.0 已按反馈撤销学科预设与期刊画像。）

## v1.0 —— 2026-10-27

初版：规划 → 检索 → 重排 → 精读 → 审查 → 成文，六段流水线，10 个留痕文件。
