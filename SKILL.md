---
name: medlit-agent
description: 循证医学文献检索。当用户提出医学、临床、公共卫生、流行病学、药物疗效、诊断准确性、指南依据等需要循证依据的问题，或明确要求"查文献 / 找证据 / 做文献综述 / 系统评价 / PubMed 检索 / GRADE"时使用。自动从模型池选最优模型并按角色分工，规划中英文检索式，调用 PubMed E-utilities 取真实题录与摘要（有 PMC 开放全文则抓全文），逐篇抽取结构化证据、横向审查冲突与证据空隙、给出 GRADE 确定性分级与 PRISMA 流程计数，最后生成每条论断都挂 PMID 的答案。内置糖尿病代谢、慢病基层、循证医学、公共卫生政策四个学科域预设。适用于"我需要知道某个临床问题现在有什么证据"这类问题，不适用于纯概念解释、不需要文献支撑的闲聊，以及涉及具体患者诊疗决策的场景（应提示就医）。
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
- 分布式软件必须向 NCBI 登记 `tool` 与 `email`（`vog.hin.mln.ibcn@seitilitue`），
  否则违规 IP 会被封禁。仓库里的 `tool` 是项目级标识，`email` 请填开发者自己的邮箱。

## 一、什么时候用

**适用**：疗效比较、诊断准确性、危险因素、预后、指南依据、某方向近三年进展、某药物安全性、系统评价素材。

**不适用**：名词解释、机制科普、不需要文献的操作性问题、涉及具体患者诊疗决策（应提示就医）。

## 二、怎么调用

在工作区内执行（把引号里的内容换成用户的问题）：

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
| `--journals [画像]` | 限定期刊：`clinical`（默认，临床核心刊）/ `basic_science`（Nature-Cell 系）/ `public_health`；不给值则用预设画像 |
| `--journal-list "Nature,Nat Med"` | 临时指定期刊名单，自动开启限定并覆盖画像 |
| `--journal-mode ta\|full` | 刊名匹配方式：`ta` 缩写精确匹配（默认）／`full` 全称匹配 |
| `--preset ID` | 指定学科域预设；不给则按问题关键词自动识别 |
| `--no-preset` | 关闭学科域预设 |
| `--cq ID[,ID]` | 套用 PubMed 官方 Clinical Query 过滤器（如 `therapy.narrow`、`diagnosis.broad`） |
| `--rounds 2` | 检索轮数（2 = 候选不足时自动扩写一轮） |
| `--models` | 打印模型路由链与平台可用性 |
| `--pubmed-only "检索式"` | 只检索不调模型，用于核对检索式 |
| `--list-presets` | 列出所有学科域预设 |

**调用顺序建议**：先跑一次 `--quick` 确认命中和方向对不对，再跑完整版。

## 三、检索范围

> **默认检索全 PubMed，不限制期刊**，任何刊、任何出版社都会进入候选。
> 只有显式加 `--journals` 时才限定到指定期刊画像。
> 每次运行会在 `09_log.txt` 第一屏和 `10_trace.json` 的 `scope_on` 字段里写明本次是否限刊，便于回查。

## 四、学科域预设

内置四个（`data/presets.json`），用户把自己的 JSON 丢进 `presets/` 目录即可新增或覆盖，**不需要改代码**：

| id | 名称 | 期刊画像 |
|---|---|---|
| `diabetes-metabolism` | 糖尿病 · 肥胖 · 代谢病 | clinical |
| `chronic-care-primary` | 慢病管理 · 基层防治 | public_health |
| `evidence-medicine` | 循证医学 · 卫生技术评估 | clinical |
| `public-health-policy` | 公共卫生 · 卫生政策 | public_health |

预设会把该领域的 **MeSH 扩展词、优先研究设计、可用 Clinical Query 过滤器**注入规划阶段。

## 五、输出在哪

每次运行在 `paths.runs_root` 下新建一个目录（默认 `./runs/`）：

```
20261105-1830_GLP1受体激动剂对青少年肥胖/
  01_question.md        问题、限定、所用预设
  02_plan.json          检索计划（英文核心问题 + PICO + 检索式 + 所选过滤器）
  03_papers.json        全部候选题录（含摘要、MeSH、关键词、PMCID）
  04_evidence.json      精读抽取的结构化证据
  05_evidence_table.md  ★ 证据总表（一眼看完所有文献的设计/样本/结论）
  06_fulltext/          抓到的开放全文（若有）
  07_answer.md          ★ 答案正文（带 PMID）
  08_bibliography.md    参考文献（脚本生成，权威）
  09_log.txt            执行日志
  10_trace.json         模型调用、耗时、错误表、PMID 校验结果
  11_prisma.md          PRISMA 流程计数 + 全部检索式明细
```

**读答案只看 `07_answer.md` 和 `05_evidence_table.md`**，其余是过程留痕。

## 六、拿到结果之后怎么用

1. 读 `07_answer.md`，核对每段是否都有 PMID
2. 读 `05_evidence_table.md`，确认关键数字与文献对得上
3. 读 `11_prisma.md`，PRISMA 计数可直接用于系统评价的方法学部分
4. 按用户关注点重写：该压缩的压缩、该补背景的补背景
5. **区分三件事**：文献说了什么 / 证据有多强（看 GRADE）/ 我的推断是什么
6. 如果某一节文献不足，明确告诉用户"这块证据薄"，不要糊过去

## 七、模型怎么选

`config.json` 的 `models.chain` 按角色配置了路由链，脚本会先探测平台可用模型，按顺序取第一个可用的：

- **planner**（规划检索式 + 第二轮扩写）→ DeepSeek-V3.2
- **reader**（逐篇抽取证据）→ DeepSeek-V3.2（长上下文）
- **critic**（挑方法学缺陷、判冲突、给 GRADE 分级）→ DeepSeek-R1
- **writer**（成文）→ DeepSeek-V3.2
- **rerank**（语义重排，可选）→ Qwen3-Reranker-8B

改模型只改 `config.json`，不用动代码。`--models` 可以看实际命中的是哪几个。

**端点可换**：任何 OpenAI 兼容端点都行（硅基流动 / DeepSeek / Kimi / OpenAI / 本地 Ollama）。
只有硅基流动这类提供 `/rerank` 的服务才能做语义重排；没有就关掉 `provider.rerank.enabled`，流程照常。

## 八、常见问题

**命中太少**：说明检索式过窄。把问题拆成 2–3 个同义词组重跑，或去掉 `--research-only` 看是不是被过滤光了。

**命中太多但都不相关**：加 `--journals clinical`，或加 `--constraints "2020 年后"`，或用 `--cq` 套用更窄的研究设计过滤器。

**抓不到全文**：正常。PubMed 只有开放获取（PMC）才有全文，多数临床刊只有摘要。摘要在多数情况下够用；不够时答案里会注明"仅摘要"。

**回答里出现没见过的文献**：不应该发生。参考文献由脚本按 PMID 回查生成；若正文引用了不在候选池里的 PMID，`10_trace.json` 的 `errors` 与 `pmid_check` 字段会报警。

**报 "Search Backend failed / Search is temporarily unavailable"**：这是 **NCBI 检索后端临时故障**，不是"没有相关文献"。等一会儿重试即可，工具已把这种情况与真正的零命中区分开。

## 九、维护

- 换 API key：改 `.env`（或系统环境变量），**不要写进 config 文件**
- 换模型 / 换端点：改 `config.json` 的 `provider` 与 `models.chain`
- 改输出目录：改 `config.json` 的 `paths.runs_root`
- 加学科域预设：往 `presets/` 目录丢 JSON，或改 `data/presets.json`
- 加期刊画像：改 `data/journal_profiles.json`
- 加 Clinical Query 过滤器：改 `data/clinical_queries.json`
- 改写作风格：`scripts/prompts.py` 的 `WRITER_SYSTEM`
- 自检：`python scripts/selftest.py`（逐环节体检，约 2 分钟；上游故障时会显示"跳过"而不是"失败"）

⚠️ 仓库里**不得出现任何明文密钥**。`.env` / `config.json` / `config.local.json` 都已在 `.gitignore` 中。
