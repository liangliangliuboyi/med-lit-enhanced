# 贡献指南

感谢你愿意参与。这个项目很小，贡献门槛也低。

## 环境

- Python **3.9+**，**不需要 pip install 任何东西**（本项目零第三方依赖）
- 一个 OpenAI 兼容端点的 API key（默认硅基流动）
- （可选）一个 NCBI API key

```bash
git clone https://github.com/<your-id>/medlit-agent.git
cd medlit-agent
cp .env.example .env      # 填入 SILICONFLOW_API_KEY
python scripts/selftest.py
```

`selftest.py` 会逐环节体检（配置、预设、期刊画像、模型、PubMed 检索、题录解析、
PMC 全文、研究论文判定、PMID 越界校验、端到端迷你跑）。
**遇到 NCBI 上游故障时，相关项会显示 `SKIP` 而不是 `FAIL`** —— 那不是你的环境问题。

## 提交前必做

```bash
# 1. 确认没有密钥进库
git status --porcelain
git diff --cached | grep -iE "sk-[a-zA-Z0-9]{20,}|api_key\"\s*:\s*\"[a-f0-9]{20,}"

# 2. 跑自检
python scripts/selftest.py
```

`.env` / `config.json` / `config.local.json` 已在 `.gitignore` 中，但请**每次提交前人工确认一遍**。
一旦密钥进了 git 历史，即使删除文件也还在历史里——必须立刻在服务商后台吊销该 key。

## 怎么加东西

| 想加 | 改哪里 | 要改代码吗 |
|---|---|---|
| 学科域预设 | `presets/<你的id>.json` 或 `data/presets.json` | **不用** |
| 期刊画像 | `data/journal_profiles.json` | **不用** |
| Clinical Query 过滤器 | `data/clinical_queries.json` | **不用** |
| 换模型 / 换端点 | `config.json` 的 `provider` 与 `models.chain` | **不用** |
| 改写作风格 | `scripts/prompts.py` 的 `WRITER_SYSTEM` | 不用 |
| 改抽取字段 | `scripts/prompts.py` 的 `READER_SYSTEM` + `scripts/pipeline.py` 的字段透传 | 要 |
| 加一个流水线环节 | `scripts/pipeline.py` 的 `Run.execute()` | 要 |

**核心原则请别破坏**：模型只做需要判断力的事（怎么问、怎么抽、怎么挑矛盾、怎么写）；
计数、过滤、排序、拼参考文献、校验 PMID 全部留在程序侧。

## 提 issue

请附上：
1. 完整命令行
2. `runs/<你的目录>/09_log.txt`
3. `runs/<你的目录>/10_trace.json`
4. Python 版本与操作系统

如果和检索结果有关，附上 `02_plan.json` 与 `11_prisma.md`——这两份能省掉大量来回排查。

## 提交信息

用中文或英文都可以，格式参考 Conventional Commits：

```
feat(presets): 新增肿瘤营养学学科域预设
fix(pubmed): 识别 NCBI 返回 200 + ERROR 字段的情况，避免把上游故障误报成零命中
docs(readme): 补充本地 Ollama 配置示例
```

## 边界

- 不要提交任何密钥、任何 `.env`、任何 PubMed 抓取的全文
- 不要把 `runs/` 目录提交进来（`.gitignore` 已排除）
- 提问里**不要**包含具体患者信息

## 维护者不承诺响应时效

这是个个人维护的开源项目。欢迎提 PR、提 issue、提想法，但请理解维护者可能几周才看一次。
