# 福建文旅政策与景区知识库

两个独立可运行的项目，按交付要求分文件夹：

| 文件夹 | 内容 | 验收状态 |
| --- | --- | --- |
| [`01_crawler/`](01_crawler/) | 绕过反爬的爬取项目 + 数据清洗调优 + 字段溯源库 | 52 项测试通过，真实站点采集跑通 |
| [`02_rag/`](02_rag/) | LangChain + FAISS 向量库的本地化知识库问答 | 59 项测试通过，全部页面实跑 200 |

数据源是**福建省文化和旅游厅**（`https://wlt.fujian.gov.cn`）与文化和旅游部的
公开公文，列表走站内检索接口，正文取自详情页，附件（PDF/DOC/DOCX/WPS/OFD/XLSX）
下载后解析并入正文。

两项目通过 `tourism.db` 交接，契约见 [`CONTRACT.md`](CONTRACT.md)。
`02_rag` 不复制语料，只读 01 的库，向量索引落自己的 `data/faiss/`。

---

## 30 秒跑起来

```powershell
$py = "D:\app\python-envs\fujian-tourism\Scripts\python.exe"
$env:PYTHONIOENCODING = 'utf-8'

# 1) 采集 + 清洗 + 校验 + 导出
cd D:\cxdownload\python\fujian-tourism\01_crawler
& $py cli.py pipeline --all

# 2) 建向量索引
cd ..\02_rag
& $py -m scripts.init_data --rebuild

# 3) 起服务 → http://127.0.0.1:8848
& $py -m uvicorn app.main:app --host 127.0.0.1 --port 8848
```

打开 `/admin` 填一次模型 API Key，访客页 `/ask` 就开始用模型作答。
不填也能用：检索、字段、溯源全部正常，回答改成从原文摘句拼装，
页面标「**未接入模型·本地摘录**」——不假装调用过模型。

---

## 交付清单对齐（原提示词 10 节）

| # | 交付项 | 落在哪 |
| --- | --- | --- |
| 1 | 需求拆解 | [`01_crawler/README.md`](01_crawler/README.md) 第一节（四层：采集/结构化/事实/治理，含验收口径） |
| 2 | 功能模块表 | [`01_crawler/README.md`](01_crawler/README.md) 第二节（15 文件 → 职责 → 关键类）；02 侧见 [`02_rag/README.md`](02_rag/README.md) 第五节 |
| 3 | DDL | [`01_crawler/crawler/db/schema.sql`](01_crawler/crawler/db/schema.sql) + README 第三节（10 表 1 视图 + 设计理由） |
| 4 | 数据更新机制 | [`01_crawler/README.md`](01_crawler/README.md) 第四节（full/incremental/单篇重采、冲突不覆盖、过期不删除、幂等） |
| 5 | 爬虫实现（含绕过反爬） | [`01_crawler/crawler/anticrawl/`](01_crawler/crawler/anticrawl/) 六个模块 + README 第五节 |
| 6 | 字段校验与呈现 | [`01_crawler/crawler/pipeline/validator.py`](01_crawler/crawler/pipeline/validator.py) + [`02_rag/app/templates/spot.html`](02_rag/app/templates/spot.html) + 两份 README |
| 7 | 知识库问答全流程 | [`02_rag/README.md`](02_rag/README.md) 第一节（含检索→prompt→引用归一化全链路图与设计取舍） |
| 8 | 技术栈与运行方式 | [`02_rag/README.md`](02_rag/README.md) 第二节（含环境变量表与路由表） |
| 9 | 5 分钟演示脚本 | [`02_rag/README.md`](02_rag/README.md) 第三节（带时间轴，逐屏可点） |
| 10 | 风险清单 | [`02_rag/README.md`](02_rag/README.md) 第四节（11 条，含缓解手段与上生产的硬条件） |

---

## 硬约束怎么落地的

| 约束 | 实现位置 | 判据 |
| --- | --- | --- |
| 易变字段必须挂 8 项溯源元数据 | `field_provenance` 表 | `PRAGMA table_info` 含 `source_doc_id/source_url/doc_title/issuer/publish_date/effective_from/effective_to/quote/fetched_at`，测试断言 10 列 |
| `quote` ≤ 200 字 | `utils.truncate(text, 200)` 在标点处切 | `test_citation_quote_truncated_to_200` |
| 三态标记 | `cleaner._judge_status` + `validator` 两层校验 | 每页字段都带 `已核验` / `待核验` / `冲突` 之一 |
| 冲突不静默丢旧值 | `Database.add_provenance` 写 `conflict_group` 并把双方置 `冲突` | `test_add_provenance_conflict_keeps_both` + 页面 `/spot/{id}` 并排 |
| 页面提供变更对比 | `doc_versions.diff_summary`（句子差集） | `/spot/{id}` 的「冲突与变更对比」区 + `/doc/{id}` 的版本历史 |
| LLM 调用收口 | [`02_rag/app/llm.py`](02_rag/app/llm.py) 的 `LLMClient.complete()` | 全项目仅此一处出网（`grep -r "chat/completions"` 只命中它） |
| 未配置模型时 UI 标注 | `LLMResponse.badge` → `base.html` 状态条 | 每个页面 HTML 都含「未接入模型·本地摘录」 |
| 页面真实数据、无空路由 | 所有模板只吃 SQLite 查询结果 | `test_no_empty_route_all_pages_have_body` 断言每页 > 1500 字符 |
| 后台配 Key 即刻生效 | `/admin` → `settings_store.save()` → `get_llm(refresh=True)` | 保存后 `/ask` 状态条从「本地摘录」变「模型作答 · {model}」 |
| 不编造景区名/票价/文号 | 全部字段值来自抓取的原文摘录 | 每条 `field_provenance.quote` 都能在对应文档正文里找到 |

---

## 一键验收

```powershell
$py = "D:\app\python-envs\fujian-tourism\Scripts\python.exe"
$env:PYTHONIOENCODING = 'utf-8'

cd D:\cxdownload\python\fujian-tourism\01_crawler
& $py -m pytest -q          # 52 passed

cd ..\02_rag
& $py -m pytest -q          # 59 passed
& $py -m scripts.cli status # 库/索引/模型三态
```

---

## 环境

* Python 3.12.13（`D:\app\python-envs\fujian-tourism`，uv 管理）
* 关键依赖：httpx、curl_cffi、beautifulsoup4、lxml、pypdf、python-docx、openpyxl、
  olefile、PyYAML、fastapi、uvicorn、jinja2、**langchain / langchain-community /
  langchain-openai / langchain-text-splitters**、**faiss-cpu**、
  **sentence-transformers**（BAAI/bge-small-zh-v1.5）
* 可选：ddddocr（验证码）、playwright（浏览器兜底）
* 依赖装法遵循镜像优先：`$env:UV_DEFAULT_INDEX='https://mirrors.aliyun.com/pypi/simple'`
* 拉 HuggingFace 权重建议设 `$env:HF_ENDPOINT='https://hf-mirror.com'`
