# 福建文旅政策与景区知识库

一套完整可运行的本地知识库系统：**反爬爬虫 → 数据清洗与字段溯源 → LangChain + FAISS 向量问答**。
数据源是福建省文化和旅游厅（`wlt.fujian.gov.cn`）与文化和旅游部的公开公文，全部字段值可逐条溯源到原文摘录。

| 文件夹 | 内容 | 状态 |
| --- | --- | --- |
| [`01_crawler/`](01_crawler/) | 绕过反爬的爬取项目 + 数据清洗调优 + 字段溯源库 | 52 项测试通过，真实站点采集 300 篇跑通 |
| [`02_rag/`](02_rag/) | LangChain + FAISS 向量库的本地化知识库问答（FastAPI） | 59 项测试通过，全部页面实跑 200 |

两项目通过 `tourism.db`（SQLite，10 表 1 视图）交接，契约见 [`CONTRACT.md`](CONTRACT.md)。
`02_rag` 不复制语料，只读 01 的库，向量索引落在自己的 `data/faiss/`。

**当前库内数据**：300 篇公文 / 6325 个切片 / 107 个景区 / 144 条字段证据，向量索引 6325 条（`BAAI/bge-small-zh-v1.5`，512 维）。

---

## 30 秒跑起来

```powershell
$py = "D:\app\python-envs\fujian-tourism\Scripts\python.exe"   # Python 3.12 venv
$env:PYTHONIOENCODING = 'utf-8'
$env:HF_ENDPOINT = 'https://hf-mirror.com'                     # HuggingFace 镜像（首次下模型）

# 1) 采集 + 清洗 + 校验 + 导出（约 50 分钟，可用 --max-requests/--detail-limit 调小）
cd 01_crawler
& $py cli.py pipeline --all

# 2) 建向量索引（语义模型首次加载约 60 秒）
cd ..\02_rag
& $py -m scripts.init_data --rebuild

# 3) 起服务 → http://127.0.0.1:8848
& $py -m uvicorn app.main:app --host 127.0.0.1 --port 8848
```

打开 `/admin` 填一次模型 API Key（DeepSeek/OpenAI/通义/月之暗面/智谱 任一），访客页 `/ask` 即刻用模型作答。
不填也能用：检索、字段、溯源全部正常，回答改为从原文摘句拼装，页面标注「**未接入模型·本地摘录**」——不假装调用过模型。

---

## 抗反爬体系（01_crawler）

核心思想是**把请求变得像人**，六个模块各管一层，全部可配置、可单独关闭（`config/settings.yaml`）：

```
crawler/anticrawl/
├── client.py        请求编排 + 拦截判定 + 重试 + 浏览器兜底
├── fingerprint.py   TLS/JA3 + HTTP/2 帧 + 头部顺序 三层指纹伪装
├── throttle.py      拟人节奏 + 令牌桶 + 指数退避
├── proxy_pool.py    代理池（隔离 / 复活 / 粘性会话 / 密码脱敏）
├── captcha.py       四类验证码（图片 OCR / 算术 / 滑块 / 点选）
└── robots.py        robots.txt + sitemap 抓取边界
```

### 指纹伪装（fingerprint.py）

- **TLS 层**：`curl_cffi` 以 `impersonate=chrome124` 复刻 Chrome 的 JA3/JA4 指纹——Python httpx 的 TLS ClientHello 与 Chrome 差异显著，是 WAF（安全狗、雷池等）最常用的第一道判据。
- **HTTP/2 层**：SETTINGS / WINDOW_UPDATE / PRIORITY 帧序列跟随浏览器 profile。
- **头部层**：头部出现顺序本身就是指纹，用 `OrderedDict` 固定 Chrome 的真实顺序，并补齐 `sec-ch-ua` 全家族。

### 拟人节奏（throttle.py）

- 每 host 独立计时，`min_interval 2.0s ~ max_interval 4.5s` 均匀随机——**固定间隔本身就是机器人特征**。
- 连续 3 次请求后强睡 `burst_sleep 15s`，另有 12% 概率追加 1.5~3 倍「走神」长睡。
- 失败退避 `min(1.8^attempt × U(0.7,1.3), 300s)`，429 触发 host 级冷却 60s。

### 代理池（proxy_pool.py）

- 三种轮换策略：`per_request` / `per_session` / `sticky_5min`（同一 host 5 分钟内粘住同一出口，避免会话 Cookie 频繁换 IP 触发风控）。
- 失败计数 + 隔离区（`cooldown × min(fail_count, 5)`），`report_fail(hard=True)` 立即隔离；长期无成功的节点延迟评分劣化、自动排后。
- 全程无代理也能跑（本次真实采集就是 0 代理直连完成的）。

### 验证码（captcha.py）

| 类型 | 打法 |
| --- | --- |
| 图片文字码 | `ddddocr` 本地 OCR（离线，不依赖外部 API） |
| 算术码 | OCR 结果 + `ast` 白名单求值（只允许 `+-*/`） |
| 滑块 | 手写 Sobel-x 边缘检测 + 列投影 `argmax` 求缺口 x 偏移——不依赖 OpenCV，numpy + PIL 即可 |
| 点选 | ddddocr `det` 目标检测返回 `"x,y;x,y"` 序列 |
| 兜底 | 上述全失败 → 落盘 `data/manual_captcha/<hash>.png` 等人工回填 `<同名>.txt` |

### 拦截判定与降级（client.py）

- 双路判定：HTTP 状态码（403/401→block，429→rate_limit）+ **正文指纹**（200 但内容是拦截页——只对 `len(content) < 4096` 的短页查标记，避免「正文提到『安全验证』四个字」被误杀）。
- 命中验证码 → 自动找 `img[src*=captcha]` → OCR → 回填表单 → POST 重放，全流程无需人工。
- 连续 403 且代理池耗尽 → host 熔断 600s，不硬撞。
- 最终兜底 `browser_fallback()`：Playwright headless + `add_init_script` 抹掉 `navigator.webdriver`、补齐 `window.chrome`/languages/plugins。

### 真实站点适配（parsers/wlt_gov.py，本次实战踩出来的）

- 列表页由 avalon.js 动态渲染，HTML 里没有数据 → 直接打站内 WAS5 检索 JSON 接口（需 `X-Requested-With: XMLHttpRequest` 头 + `json.loads(strict=False)`，正文含裸控制字符）。
- 详情页两套模板、正文容器六路回退；旧模板 `div.detp` 内嵌 `<html><body>` 会让 BeautifulSoup 重排结构导致 `get_text()` 返回空串，需先正则剥离。
- 附件链接不在 `<a>` 里（被模板注释掉了），藏在 `var apps=[...]` JS 数组里，用正则提取。
- 正文为空分两种处置：图片版（`image_only`，入库不下结论）与附件承载（下载 PDF/DOC 并入正文）。
- **附件解析**支持 PDF / DOCX / **DOC（Word 97 二进制，走 OLE2 piece table）** / WPS / **OFD（国产版式，zip+XML 取 TextCode）** / XLSX / CSV。
- 拒绝把跨站链接（文旅部、税务总局等）纳入采集，相对 URL 自动补全，标题剥 HTML 标签与站名后缀。

---

## 数据管道（清洗 → 溯源 → 校验 → 导出）

```
pipeline/
├── crawler.py    采集编排：栏目 API 分页 + seed_urls + 增量过滤 + 请求预算（max_requests_per_run / detail_limit）
├── cleaner.py    正文清洗（去导航/页脚/硬断行合并）+ 近重复检测 + 景区/字段抽取 + 切片 + 过期降级
├── validator.py  两层校验：溯源完整性（五问）+ 字段语义（时间/价格/电话/地址格式）
└── exporter.py   documents.jsonl / spots.jsonl / quality_report.json
```

### 字段抽取的正确性纪律

- **名录路径**与**单景区路径**分离：名录公告（「1.漳州高峰谷景区」编号行）只记原文真的说了的三件事——存在、质量等级、城市；硬套 6 字段抽取会让几十个景区共享同一段「门票 65 元」，那是编造的关联。
- 单篇路径的 `address` 等字段加**同句必须出现景区名**的硬约束——政策公文里的「地址」十有八九是采购代理机构的。
- 景区名守门 `_looks_like_spot_name`：长度 / 后缀 / 连接词 / 44 个动词词表（推动/建设/落实…）/ 裸后缀（山/岛/湖）只在标题点题时放行——「海岛名录里的大坠岛」是真地方，「山岳文件里的刘大山」是碎词。
- **宁可不抽，不制造一条查不到出处的字段**。

### 三态与溯源

每条字段证据（`field_provenance`）挂 8 项溯源元数据（来源文档、URL、发文机关、发布日期、生效起止、≤200 字原文摘录、抓取时间），按「五问」口径判三态：

- **已核验**：摘录、URL、发布日期、有效期齐备且在有效期内
- **待核验**：缺任一项或已过有效期
- **冲突**：同一景区同一字段出现不同值——**不静默丢旧值**，双方都标冲突、共享 `conflict_group`、进复核队列，页面上并排展示 + `doc_versions.diff_summary` 给变更对比

`quote` 截断永远落在标点处；校验只降级不删证据。

---

## 问答系统（02_rag）

```
app/
├── main.py            FastAPI + Jinja2 服务端渲染（10 条路由，无前端框架）
├── store.py           TourismDB 数据访问 + LangChain FAISS VectorStore + 混合检索
├── embeddings.py      bge-small-zh-v1.5 语义向量（失败自动降级哈希向量，不宕机）
├── ingest.py          索引构建（语料指纹 corpus_hash + 模型名三项全等才复用，否则重建）
├── llm.py             LLMClient —— 全项目唯一模型出口（LangChain ChatOpenAI）
├── qa.py              检索 → prompt 组装 → 引用编号清洗（[9] 越界整段删，不替模型编来源）
└── settings_store.py  /admin 配置落盘（data/config.json，chmod 0600，页面只回显掩码）
```

- **混合检索**：FAISS 语义召回 + 关键词加权（`final = vec + 0.25*kw`）——纯向量在专有名词上会漏，问「甲峰景区门票多少」时真正写了景区名的那篇可能排第七。
- FAISS 走内积（向量已归一化，内积即余弦，分数可解释）；LangChain 层返回 L2 距离，`vec = 1/(1+distance)` 转相关度。
- Prompt 头部带元数据（`[1] 《doc_title》｜发文机关｜文号｜发布日期｜有效期至`），模型能写出「根据 2024 年 8 月 8 日《…》的规定」。
- 未接模型时给抽取式回答：不生成新句子，按命中度取原文前 5 句，每句标 `（来源 [n]）`，结尾强制三句免责。
- `/admin` 保存 Key 即刻生效（`get_llm(refresh=True)`），换向量模型自动触发索引重建。

页面全部服务端渲染、真实数据读取、无空路由（测试断言每页 > 1500 字符）。

---

## 一键验收

```powershell
cd 01_crawler; & $py -m pytest -q        # 52 passed
cd ..\02_rag;  & $py -m pytest -q        # 59 passed
& $py -m scripts.cli status              # 库 / 索引 / 模型 三态
```

## 环境与依赖

* Python 3.12（建议 uv 管理 venv；镜像优先：`$env:UV_DEFAULT_INDEX='https://mirrors.aliyun.com/pypi/simple'`）
* 爬虫：httpx、curl_cffi、beautifulsoup4、lxml、pypdf、python-docx、openpyxl、olefile、PyYAML
* 问答：fastapi、uvicorn、jinja2、**langchain 全家桶**、**faiss-cpu**、**sentence-transformers**（BAAI/bge-small-zh-v1.5）
* 可选：ddddocr（验证码）、playwright（浏览器兜底）
* HuggingFace 镜像：`$env:HF_ENDPOINT='https://hf-mirror.com'`

## 合规边界

只抓公开可访问的政务公开页面，遵守 robots.txt 与站点限速（本次全量采集 438 次请求、节奏参数见 `settings.yaml`，未触发任何封禁）；系统定位是内部知识库，字段展示永远带「以官方最新公告为准」。请将采集范围控制在自己有权访问的公开数据内。
