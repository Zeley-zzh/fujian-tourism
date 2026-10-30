# 02_rag — 本地知识库问答

> 读 `01_crawler` 采下来的政策和公告，切成片、算成向量、放进 FAISS，
> 访客问一句话，检索出原文片段交给模型作答，每句带编号回到具体文档。
>
> 模型 Key 在 `/admin` 页面填一次，访客页立刻能用。
> 没填 Key 也能用——检索、字段、溯源全都正常，回答改成从原文摘句拼装，
> 页面上明确标「未接入模型·本地摘录」，不假装调用过模型。

---

## 一、问答全流程

```
访客提问
   │
   ├─ 检索层  Retriever.search()
   │     FAISS 向量召回 max(top_k*4, 12) 个候选
   │     + 字面命中率加权  final = vec + 0.25 * kw
   │     ↓
   ├─ 组织层  build_prompt()
   │     每个片段带头部元数据：[n] 《标题》｜机关｜文号｜发布日期｜有效期至
   │     正文截 700 字，尾部要求「逐句标注引用编号」
   │     ↓
   ├─ 生成层  LLMClient.complete()
   │     LangChain ChatOpenAI → 任意 OpenAI 兼容端点
   │     未配 Key / 调用失败 → local_answer() 摘句拼装
   │     ↓
   └─ 校验层  _sanitize_refs() + _build_citations()
         模型写出 [9] 但只有 5 个片段 → 整段删掉
         引用回到 chunk_id / doc_id / 原文摘录（≤200 字）
```

### 1.1 为什么向量之外还要字面加权

纯向量检索在专有名词上会漏。问「甲峰景区门票多少」，语义上「门票价格」的块
都能排到前面，但真正写了「甲峰景区」的那篇可能排在第七。景区的名字是专有名词，
字面命中比语义相似更能定准，所以把命中查询实词的比例作为加分项，
权重 0.25 是「能翻转明显该排前面的、但不会让字面盖过语义」的量级。

`vec` 由 FAISS 返回的 L2 距离换算：`vec = 1 / (1 + distance)`。
FAISS 默认给距离（越小越近），直接展示成「相关度 0.83」会让人以为越大越远，
换成这个单调递减的映射，分数就变成「越接近 1 越相关」，页面上不必再加解释。

### 1.2 上下文怎么交给模型

片段头部带四类元数据，不是为了好看——模型要能用它们写「根据 2024 年 8 月 8 日
《…》的规定」，而不是含糊说「资料显示」。有效期也放进去，
因为政策类问题的答案常常取决于「这条还灵不灵」。

正文截 700 字，不是全部：一个片段平均 480 字左右，截 700 是给长表格页留余量，
同时保证 5 个片段加起来不超常见模型的上下文舒适区。

### 1.3 引用怎么回到证据

模型有编号幻觉——只给了 5 个片段，它也可能写出 `[8]`。处理方式是**整段删掉**
这个编号，而不是改成 `[1]`：改编号等于替模型编一个来源，比少一句更糟。

`Citation` 里带 `chunk_id` 与 `quote`（原文摘录，截 200 字），
页面上点编号能跳到 `/doc/{doc_id}` 看整篇原文与版本历史。

### 1.4 未接模型时给什么

`local_answer()` 不生成新句子，只从检索结果里挑：
把候选片段按「命中问题关键词数 + 含数字加分」排序，取前 5 句，每句尾部标
`（来源 [n]）`，最后强制三句：

```
以上内容摘自知识库原文，票价、开放时间与政策有效期以官方最新公告为准。
（资料发布日期最晚为 2026-07-28）
（本条回答由本地检索结果直接摘录拼装，未经模型改写。）
```

宁可回答笨但可核对，也不编。这个模式同时是模型的对照组：
接上模型后可以并排看，判断模型到底加了什么价值。

---

## 二、技术栈与运行

| 组件 | 选型 | 说明 |
| --- | --- | --- |
| Web | FastAPI + Jinja2 服务端渲染 | 不引前端框架，页面就是 HTML |
| 向量库 | **LangChain `FAISS`** (`langchain-community`) | `FAISS.from_documents` / `similarity_search_with_score` / `save_local` |
| 嵌入 | `sentence-transformers` + `BAAI/bge-small-zh-v1.5` | 512 维中文小模型，CPU 可跑；装不上自动降级 |
| 模型 | **LangChain `ChatOpenAI`** | 任意 OpenAI 兼容端点：DeepSeek / OpenAI / 通义 / 月之暗面 / 智谱 |
| 存储 | SQLite（复用 `01_crawler/data/tourism.db`） | 文档、切片、字段证据都在里面 |
| 配置 | `data/config.json`（权限 0600） | 页面只回显掩码，接口不返回完整 Key |

### 2.1 三步跑起来

```powershell
$py = 'D:\app\python-envs\fujian-tourism\Scripts\python.exe'
cd D:\cxdownload\python\fujian-tourism\02_rag

# 1) 备数据 + 建索引（库不存在会调 01_crawler 真实采集）
& $py -m scripts.init_data --rebuild

# 2) 起服务
& $py -m uvicorn app.main:app --host 127.0.0.1 --port 8848

# 3) 浏览器打开 http://127.0.0.1:8848
#    先到 /admin 填 API Key，再回 /ask 提问
```

### 2.2 命令行

```powershell
& $py -m scripts.cli build              # 建或复用索引
& $py -m scripts.cli build --force      # 强制重建
& $py -m scripts.cli search "门票多少钱"  # 只检索，不调模型
& $py -m scripts.cli ask "4A 级旅游景区的评定标准是什么"
& $py -m scripts.cli status             # 库与索引状态
```

### 2.3 后台配 Key

打开 `/admin`：

1. 选服务商（默认 DeepSeek），接口地址与模型名自动填好，可改；
2. 填 API Key，保存；
3. 点「测试连接」——会真实调用一次模型，失败返回具体错误，不会假装成功；
4. 回 `/ask`，状态条从「未接入模型·本地摘录」变成「模型作答 · deepseek-chat」。

Key 存在 `02_rag/data/config.json`，文件权限 0600。
页面与 `/api/status` 都只返回掩码（前 4 位 + `••••••••` + 后 4 位）。
要清掉就点页面底部「清除 API Key」。

换向量后端（`st` ↔ `hash`）会让旧索引失效，保存时自动检测并重建。
不同模型的向量不在同一个空间，混用等于往索引里投毒，所以宁可重建。

### 2.4 环境变量

| 变量 | 默认 | 作用 |
| --- | --- | --- |
| `TOURISM_DB_PATH` | `../01_crawler/data/tourism.db` | 换数据库位置 |
| `RAG_INDEX_DIR` | `data/faiss` | 换索引目录 |
| `RAG_CONFIG_PATH` | `data/config.json` | 换配置文件位置 |
| `LLM_API_KEY` | 空 | 优先级高于配置文件 |
| `LLM_BASE_URL` / `LLM_MODEL` | DeepSeek | 同上 |
| `EMBEDDING_BACKEND` | 配置文件里的值 | `st` / `hash` |
| `EMBEDDING_MODEL` | `BAAI/bge-small-zh-v1.5` | 语义模型名 |
| `HF_ENDPOINT` | 官方 | 国内建议 `https://hf-mirror.com` |

### 2.5 路由

| 路由 | 说明 |
| --- | --- |
| `/` | 库内概况、状态条、景区表、最近入库 |
| `/spots` | 景区名录，可按城市 / 三态筛选 |
| `/spot/{id}` | 字段三态 + 冲突并排 + 逐字段溯源 + 变更对比 |
| `/doc/{id}` | 原文、版本历史、该文档提供的字段证据 |
| `/ask` | 问答（表单） |
| `/review` | 复核队列：冲突、过期、低置信 |
| `/admin` | 模型与向量后端配置 |
| `/api/ask?q=` | JSON 问答 |
| `/api/status` | 库、索引、模型状态 |
| `/healthz` | 健康检查 |

### 2.6 测试

```powershell
& $py -m pytest -q
```

59 项，全部离线，不打真实模型（`conftest.py` 有 autouse 夹具把配置指向临时文件
并清掉所有 `LLM_*` 环境变量）。

| 文件 | 覆盖 |
| --- | --- |
| `test_embeddings.py` | 向量维度与归一化、同文本确定性、语义区分度、空文本零向量 |
| `test_ingest.py` | 建索引与元数据、`faiss_row` 回填、指纹未变时复用、换模型拒绝加载 |
| `test_llm.py` | 未配 Key 必为 fallback 且带标签、摘录带引用编号、配置后无标签、Key 掩码、保存后 reload 生效 |
| `test_qa.py` | 检索元数据完整、分数单调、字面命中优先、top_k 生效、越界引用被删、三态字段聚合 |
| `test_web.py` | 全部页面、`/admin` 保存与测试、`/api/ask` JSON、404、**每页正文长度下限防空壳** |

---

## 三、5 分钟演示脚本

| 时间 | 动作 | 说什么 |
| --- | --- | --- |
| 0:00 | 打开 `/` | 「库里有 N 篇省文旅厅和文旅部的公开文件，M 个切片，抽出了 K 条字段证据。」 |
| 0:30 | 看状态条与 KPI | 「已核验 / 待核验 / 冲突三态分别多少条，冲突和过期进复核队列。」 |
| 1:00 | 点 `/spots`，按「冲突」筛 | 「筛出来的这几个景区，某个字段存在两种说法。」 |
| 1:20 | 进 `/spot/{id}` | 「票价有两个值，各自挂着来源、文号、发布日期、原文摘录。系统不替你裁决谁对——一条公告写 65 元、后来一条调价通知写 80 元，谁生效取决于生效日期。」 |
| 2:00 | 展开某字段的「来源」 | 「点开能看到原文那句话，截断到 200 字以内，就是抽取依据。」 |
| 2:30 | 点 `/doc/{id}` | 「整篇原文、版本历史、这篇文档提供了哪些字段证据。」 |
| 3:00 | 去 `/admin` 填 Key，测试连接 | 「Key 只落本机 0600 文件，页面只回显掩码。保存即生效，不重启。」 |
| 3:30 | 回 `/ask` 提问 | 「模型作答，状态条显示用的哪个模型。每句后面的编号指回具体文档。」 |
| 4:00 | 追问一个库里没有的问题 | 「答『资料中未收录』，不编。」 |
| 4:30 | 清掉 Key，再问一次 | 「退回本地摘录：句子还是原文的，只是不再改写。功能不塌。」 |
| 4:50 | 打开 `/review` | 「复核队列按严重度排，人工处理完可以标已解决。」 |

---

## 四、风险清单

| # | 风险 | 影响 | 当前处置 |
| --- | --- | --- | --- |
| 1 | 站点改版导致采集断流 | 库里不再增长 | 解析参数在 YAML 里，改配置不改代码；`crawl_logs` 里 `list_empty` 会立刻暴露 |
| 2 | 规则抽取会漏字段 | 某些景区字段缺失 | 缺失在 `/spots` 上可见，不当成「没有此项」；复核队列兜底 |
| 3 | 冲突只标记不裁决 | 页面出现两个票价 | 这是刻意的：裁决需要业务判断，系统给足证据让人判断 |
| 4 | 语义模型首次加载要下载权重 | 内网环境拉不动 | 设 `HF_ENDPOINT` 走镜像；拉不到自动降级 `hash` 后端，站点照常起 |
| 5 | 换向量模型忘记重建 | 检索全乱 | `index_meta.embedding_model` 与当前后端不一致时**拒绝加载并报错**，保存配置时自动重建 |
| 6 | 模型编造引用编号 | 用户点到不存在的来源 | `_sanitize_refs` 越界即删，宁可少一句 |
| 7 | 模型给出与原文不符的结论 | 误导访客 | system prompt 限定「只根据片段作答」；每条引用可点回原文核对 |
| 8 | `/api/ask` 没有限流 | 被刷会烧模型额度 | 内网部署；对外时必须在网关或本层加限流 |
| 9 | SQLite 单机 | 并发写会锁 | 采集与问答不在同一进程写；问答只读 |
| 10 | Key 泄漏 | 额度被盗 | 文件 0600、页面只回显掩码、接口不返回完整 Key |
| 11 | 数据合规 | 抓取范围与频率越界 | 遵从 robots、限速、只取公开公文；不复用非公开数据 |

---

## 五、目录

```
02_rag/
├── app/
│   ├── main.py              FastAPI 路由（访客 6 页 + 后台 3 个 + 3 个接口）
│   ├── settings_store.py    配置读写、Key 掩码、服务商预填表
│   ├── embeddings.py        Embedder（st / hash）+ LangChain Embeddings 桥
│   ├── store.py             TourismDB（只读查询）+ VectorStore（LangChain FAISS）+ Retriever
│   ├── llm.py               LLMClient.complete() 唯一模型出口 + local_answer 摘录兜底
│   ├── qa.py                QAService 编排 + 引用构建 + 引用越界清理 + 字段三态聚合
│   ├── ingest.py            建索引入口（委托 VectorStore.build）+ 索引状态判定
│   ├── templates/           base / index / spots / spot / doc / ask / review / admin / 404
│   └── static/style.css     纸感排版，浮层胶囊导航，三态配色
├── scripts/
│   ├── init_data.py         备数据（调 01_crawler）+ 建索引
│   └── cli.py               build / search / ask / status
├── tests/                   59 项
├── data/
│   ├── config.json          模型配置（0600，不进版本库）
│   └── faiss/               fujian_tourism.faiss + .pkl
└── README.md
```
