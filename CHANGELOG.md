# 更新日志

## v2.1.0 —— 2026-11-05

**这一版的核心是"做减法"：删掉一切会替用户做决定的限制，并把技能改造成可以直接从地址安装的样子。**

### 移除（重要）

- **完全删除刊源限制**。原先内建 26 本 CNS 期刊白名单 + 可切换的期刊画像，
  现在**恒定检索全 PubMed**。使用者来自不同专业背景（临床、公卫、药学、流行病学……），
  工具不应该替他们决定"该看哪些刊"。
- **完全删除学科域预设**。原先内置糖尿病代谢 / 慢病基层 / 循证医学 / 公卫政策四个预设，
  会把特定学科的 MeSH 词与研究设计偏好硬塞进检索式。**全部移除**，检索式只围绕问题本身。
- **删除相关 CLI 参数**：`--journals`、`--journal-list`、`--journal-mode`、
  `--preset`、`--no-preset`、`--list-presets`。
- **删除相关文件**：`scripts/presets.py`、`data/presets.json`、`data/journal_profiles.json`。
- **Clinical Query 过滤器改为默认关闭**。原先由 planner 自动挑选并 AND 进检索式，
  现在只有用户显式 `--cq <id>` 时才生效。
- **planner 提示词新增禁令**：不得限定学科、主题或期刊。

### 新增

- **标准 `skills/` 布局**：技能位于 `skills/medlit-agent/`，符合
  [Agent Skills](https://skills.sh) 生态约定，`npx -y skills add owner/repo --skill medlit-agent`
  可直接安装。
- **安装脚本** `install/install.ps1`（Windows）与 `install/install.sh`（macOS/Linux），
  自动识别目标 agent、复制技能目录、生成 `.env` 模板、打印后续步骤。
- **SKILL.md 新增「零、安装」章节**：智能体读到仓库地址后，可以照着文件自己完成安装。
- **自检新增两项**：临床查询过滤器表完整性、**检索范围无限制**（扫描源码确认没有
  刊源/预设逻辑残留，config 里也不允许再有 `journals` 块）。
- **`--list-cq`**：列出可用的 Clinical Query 过滤器。
- **功能改进清单** `docs/功能改进清单.md`：40+ 条待裁决的改进项，供后续迭代使用。

### 修复（沿用 v2.0.0）

- 密钥全部走 `api_key_env` → `.env`/环境变量，仓库零明文密钥。
- **PMID 越界校验**：正文每个 `[PMID:]` 必须来自候选池，越界写进 `trace.errors` 并告警。
- NCBI 返回 200 + `ERROR` 时不再误报成"命中 0 条"，严格区分上游故障与真的零命中。
- `fulltext_limit` 并发竞态（加锁 + 原子计数）。
- `retrieval.rounds` 由死配置改为真实的第二轮扩写。
- `--research-only` 摘要门槛改读配置 `research_min_abstract_chars`。

### 保留

- **GRADE 确定性分级**、**PRISMA 流程计数**（`11_prisma.md`）：输出增强，不构成限制。
- **provider 抽象**：任意 OpenAI 兼容端点可用；`rerank` 可选。
- **11 个留痕文件**：方法学可审计。

## v2.0.0 —— 2026-11-05

开源化重构：密钥环境变量化、NCBI 免责声明、PMID 越界校验、GRADE、PRISMA、
provider 抽象、11 项自检。（v2.1.0 已按反馈撤销其中的学科预设与期刊画像。）

## v1.0 —— 2026-10-27

初版：规划 → 检索 → 重排 → 精读 → 审查 → 成文，六段流水线，10 个留痕文件。
