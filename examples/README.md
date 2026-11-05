# 示例输出

这个目录放**脱敏后**的真实运行示例，让别人不跑一遍也能看懂产物长什么样。

## 计划放什么

```
example-<主题>/
  05_evidence_table.md   # 证据总表（脱敏后）
  07_answer.md           # 答案正文
  11_prisma.md           # PRISMA 流程计数
  10_trace.json          # trace（去掉本机路径与耗时）
```

## 脱敏要求

放之前必须处理：

- 删除 `06_fulltext/`（PMC 开放全文版权归出版商，**不得入库**）
- `10_trace.json` 里去掉 `runs_root` 等本机绝对路径
- `03_papers.json` 原文不放（PubMed 题录含摘要，属 PubMed 内容），
  如需展示结构，只保留字段名和一两篇的 PMID
- 确认没有任何 API key、邮箱、账号信息
- 若问题涉及未发表研究或你自己的课题，换成公开的示例问题

## 生成示例的推荐做法

用公开的、有代表性的问题跑一遍，例如：

```bash
python scripts/ask.py "GLP-1 受体激动剂对青少年肥胖的效果如何？" \
  --preset diabetes-metabolism --journals clinical
```

然后把 `05_evidence_table.md`、`07_answer.md`、`11_prisma.md` 复制进来，按上面的要求脱敏。

（这个目录现在是空的，等第一次成功跑通后再填。）
