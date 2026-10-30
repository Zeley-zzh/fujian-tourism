# 数据交接契约（01_crawler ⇄ 02_rag）

本文件是两个子项目之间唯一的耦合面。任何一方改动这里的结构，都要同步改另一方。

## 1. 交接产物

`01_crawler` 跑完后在 `01_crawler/data/` 下产出：

| 产物 | 路径 | 说明 |
|---|---|---|
| SQLite 主库 | `data/tourism.db` | 结构化数据 + 溯源元数据 + 文档正文 + 切片 |
| 语料快照 | `data/corpus/documents.jsonl` | 每行一条文档（含正文全文 + 切片数组），供 RAG 侧重建索引 |
| 景区快照 | `data/corpus/spots.jsonl` | 每行一个景区 + 全部字段证据 |
| 清洗报告 | `data/reports/quality_report.json` | 数据质量看板喂给前端用 |
| 原始快照 | `data/raw/<site_key>/<ts>_<name>.<ext>` | 原始 HTML/PDF/DOC 字节，供复现与审计 |

`02_rag` 默认从 `../01_crawler/data/tourism.db` 读；也可用环境变量 `TOURISM_DB_PATH` 覆盖。
库不存在时 `02_rag` 直接报错并提示先跑采集——**不自造数据**：
问答系统的每一句都必须能回到真实公文，掺入虚构样本会毁掉这个属性。

## 2. 必须存在的表（列名冻结）

见 `01_crawler/crawler/db/schema.sql`。RAG 侧只读这几张表：

- `documents` — 文档主体
- `doc_versions` — 版本历史
- `scenic_spots` — 景区档案
- `field_provenance` — 易变字段溯源
- `spot_policy_link` — 景区⇄政策关联
- `chunks` — 中文切片
- `faiss_index_meta` — 向量索引元数据
- `review_queue` — 人工复核队列

RAG 侧**只写两处**：`chunks.faiss_row`（回填向量行号）与 `faiss_index_meta`。

## 3. 状态枚举（两端必须一致）

易变字段三态：`已核验` / `待核验` / `冲突`

判定规则：
- `已核验`：有 `quote` 原文摘录 + `source_url` + `publish_date`，且当前日期落在 `effective_from`–`effective_to` 内。
- `待核验`：缺任一溯源元数据，或已过 `effective_to`，或仅二手来源。
- `冲突`：同一 `(spot_id, field_name)` 存在多份官方文件给出不一致值 → 全部并列保留，不自动择一。

## 4. chunk_id 约定

格式：`chunk_{doc_id}_{index:04d}`，例如 `chunk_d0007_0042`。
问答模块强制引用该 id，前端据此跳回原文锚点 `documents.url#chunk_{...}`。

## 5. 向量索引元数据

`faiss_index_meta` 每次建索引写一行（`index_name` 唯一，默认 `fujian_tourism`）：

| 字段 | 含义 |
|---|---|
| `embedding_model` | 建索引时用的嵌入模型名，**换模型必须重建** |
| `dim` | 向量维度 |
| `metric` | `ip`（内积，向量已归一化） |
| `total_vectors` | 向量数，应与 `chunks` 行数一致 |
| `corpus_hash` | 切片集合的指纹，语料没变可跳过重建 |
| `built_at` | 建立时间 |

三项（`embedding_model` + `corpus_hash` + `total_vectors`）全等时复用索引；
`embedding_model` 与当前后端不一致时**拒绝加载并报错**——不同模型的向量不在同一空间，
混用等于往索引里投毒。

## 6. 运行顺序

```
01_crawler  →  python cli.py init              # 建库
            →  python cli.py crawl --all       # 采集（真实站点）
            →  python cli.py clean             # 清洗 + 字段抽取 + 切片
            →  python cli.py validate          # 校验 + 三态降级
            →  python cli.py export            # 导出 jsonl 与报告
            （或一步：python cli.py pipeline --all）

02_rag      →  python -m scripts.init_data --rebuild   # 建 FAISS 索引
            →  python -m uvicorn app.main:app --host 127.0.0.1 --port 8848
```
