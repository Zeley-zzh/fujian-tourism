# 01_crawler — 福建文旅数据采集与清洗调优

> 本目录是「数据层」：把公开的文旅政策/景区公告采下来，清洗成结构化事实，
> 给每个易变字段挂上可追溯的证据链，最后导出一份**质量报告**和一份**语料**，
> 交给 `02_rag` 做向量库问答。
>
> 数据源是**福建省文化和旅游厅**（`https://wlt.fujian.gov.cn`），配置在
> `config/settings.yaml` 的 `sites` 段。列表来自站内 `was5/web/search` 检索接口，
> 正文来自详情页，附件（PDF/DOC/DOCX/WPS/OFD/XLSX）下载后解析并入正文。

---

## 一、需求拆解

把「建一个可核验的文旅知识库」拆成可验收的四层：

| 层 | 要解决的问题 | 本目录的交付物 | 验收口径 |
| --- | --- | --- | --- |
| 采集层 | 目标站点有 UA 检测、频率限制、验证码、JS 渲染 | `crawler/anticrawl/` | 被 403/429 时能自动降速、换代理、过码、兜底渲染，且不静默失败 |
| 结构化层 | HTML 千站千面，正文/文号/发布日期藏在各处 | `crawler/parsers/` | 新站只需写 YAML selector，不改代码 |
| 事实层 | 票价、开放时间这类字段一年改好几次，改错了要背锅 | `crawler/db/` + `pipeline/cleaner.py` | 每个字段值都能回答：出自哪篇、哪句原文、谁发的、何时生效、现在还灵不灵 |
| 治理层 | 冲突、过期、抓漏了没人知道 | `pipeline/validator.py` + `pipeline/exporter.py` | 出一份机器可读的质量报告，冲突/过期进人工复核队列 |

**明确不做**（避免过度设计）：不做分布式爬虫、不接消息队列、不做实时增量推送。
单机、串行、可重跑，够一个团队日常更新也够小规模部署。

---

## 二、功能模块表

| 模块 | 文件 | 职责 | 关键类/函数 |
| --- | --- | --- | --- |
| 配置 | `crawler/config.py` | 读 YAML → dataclass；支持环境变量覆盖 | `load_settings` / `Settings.p()` |
| 公共工具 | `crawler/utils.py` | URL 归一化、正文清洗、哈希、日期区间、截断 | `normalize_url` `content_hash` `in_effective_range` `truncate` |
| 指纹伪装 | `anticrawl/fingerprint.py` | TLS(JA3) + HTTP/2 + 头部顺序三层一致 | `FingerprintSpoofer` `browser_headers` |
| 代理池 | `anticrawl/proxy_pool.py` | 轮换/黏性绑定、失败隔离、软复活 | `ProxyPool.acquire/report_ok/report_fail` |
| 限速 | `anticrawl/throttle.py` | 按 host 独立计时、令牌桶、指数退避、熔断冷却 | `HostThrottle.acquire/backoff` |
| 验证码 | `anticrawl/captcha.py` | OCR / 算术 / 滑块缺口定位 / 点选 / 人工兜底 | `CaptchaSolver.solve_*` |
| robots | `anticrawl/robots.py` | 解析 robots.txt 与 sitemap（含 sitemapindex 递归） | `RobotsGate` |
| 请求客户端 | `anticrawl/client.py` | 统一 fetch：伪装、重试、拦截判定、验证码、渲染兜底 | `AntiCrawlClient.fetch` |
| 解析器 | `parsers/*.py` | 列表页/详情页/附件 三类抽取 | `GovNoticeParser` `ScenicListParser` `parse_bytes` |
| 数据库 | `db/schema.sql` + `db/__init__.py` | 建表、版本演进、证据写入、冲突标记 | `Database.upsert_document/add_provenance` |
| 采集流程 | `pipeline/crawler.py` | 列表翻页 → 去重 → 增量 → 逐详情 → 落原文 | `Crawler.crawl_site` |
| 清洗调优 | `pipeline/cleaner.py` | 正文清洗、近重复、字段抽取、关联、切片 | `Cleaner.run` |
| 校验 | `pipeline/validator.py` | 溯源完整性 + 字段语义规则，只降级不删证据 | `FieldValidator.validate_all` |
| 导出 | `pipeline/exporter.py` | 语料 jsonl + 景区 jsonl + 质量报告 json | `Exporter.run` |
| 命令行 | `cli.py` | init/crawl/clean/validate/export/pipeline/stats/report | `main()` |

---

## 三、数据库设计（DDL 摘要）

完整 DDL 在 `crawler/db/schema.sql`。核心思想：**文档与事实分离，事实必带证据**。

### 3.1 文档与版本

```sql
CREATE TABLE documents (
  doc_id          TEXT PRIMARY KEY,           -- d0001 递增
  url             TEXT NOT NULL,
  url_hash        TEXT NOT NULL UNIQUE,       -- 归一化 URL 的 sha1，去重用
  doc_type        TEXT NOT NULL,
  title           TEXT NOT NULL,
  issuer          TEXT, doc_no TEXT,          -- 发文机关 / 文号
  publish_date    TEXT, effective_from TEXT, effective_to TEXT,
  content_html    TEXT, content_text TEXT,
  content_hash    TEXT NOT NULL,              -- 去空白正文的 sha256
  raw_path        TEXT,                       -- data/raw/... 原文快照
  fetch_status    TEXT NOT NULL,              -- ok / failed
  first_seen_at   TEXT NOT NULL, last_fetched_at TEXT NOT NULL,
  current_version INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE doc_versions (
  doc_id TEXT NOT NULL, version_no INTEGER NOT NULL,
  content_text TEXT NOT NULL, content_hash TEXT NOT NULL,
  diff_summary TEXT,                          -- 「新增: … / 删除: …」
  fetched_at TEXT NOT NULL,
  UNIQUE(doc_id, version_no)
);
```

**为什么单独存版本表**：政策改一次票价，旧值就是法律意义上的旧版本证据。
`documents` 只留当前版，历史在 `doc_versions`，`upsert_document` 发现 `content_hash` 变了才升版本，
并算出句级差集写进 `diff_summary`——页面上「变更对比」直接读这一列。

### 3.2 景点与字段证据

```sql
CREATE TABLE scenic_spots (
  spot_id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE,
  alias TEXT, level TEXT, city TEXT, county TEXT,
  address TEXT, lng REAL, lat REAL, intro TEXT, source_doc_id TEXT
);

CREATE TABLE field_provenance (
  prov_id        INTEGER PRIMARY KEY AUTOINCREMENT,
  spot_id        TEXT NOT NULL,
  field_name     TEXT NOT NULL,      -- 6 个易变字段之一
  field_value    TEXT NOT NULL,
  -- ↓ 10 个溯源元数据，缺一个就降级为「待核验」
  source_doc_id  TEXT NOT NULL, source_url TEXT NOT NULL,
  doc_title      TEXT NOT NULL, issuer TEXT,
  publish_date   TEXT, effective_from TEXT, effective_to TEXT,
  quote          TEXT,               -- 原文摘录，≤200 字
  fetched_at     TEXT NOT NULL,
  -- ↓ 治理列
  status         TEXT NOT NULL,      -- 已核验 / 待核验 / 冲突
  is_current     INTEGER NOT NULL DEFAULT 1,
  conflict_group TEXT,               -- 同一字段的不同值共享一个组号
  reviewed_by TEXT, reviewed_at TEXT, review_note TEXT
);
```

`spot_policy_link` 记录景区与文档的关系（`applies_to` / `mentions` / `supersedes` / `references`）
与置信度，避免「一篇提到 20 个景区」的文档被当成 20 条强证据。

### 3.3 向量库与运行记录

```sql
CREATE TABLE chunks (
  chunk_id TEXT PRIMARY KEY,        -- chunk_{doc_id}_{index:04d}
  doc_id TEXT NOT NULL, chunk_index INTEGER NOT NULL,
  text TEXT NOT NULL, char_len INTEGER NOT NULL,
  heading_path TEXT, page_no INTEGER, faiss_row INTEGER, token_estimate REAL,
  UNIQUE(doc_id, chunk_index)
);

CREATE TABLE faiss_index_meta (      -- 02_rag 建完索引回写这里
  index_name TEXT UNIQUE, embedding_model TEXT, dim INTEGER, metric TEXT,
  total_vectors INTEGER, nlist INTEGER, index_path TEXT,
  corpus_hash TEXT, built_at TEXT
);

CREATE TABLE crawl_jobs (...)        -- 每次任务的计数与状态
CREATE TABLE crawl_logs (...)        -- 逐 URL 事件（list_ok / detail_ok / blocked / captcha …）
CREATE TABLE review_queue (...)      -- 冲突 / 过期 / 低置信 / 缺摘录，等人工
CREATE VIEW  v_spot_current_fields AS ...  -- 按景区汇总当前字段，页面直接查
```

注意 `chunks.faiss_row` 是有意为之：FAISS 的向量序号与数据库行必须能互相映射，
否则删一条语料就得重建全库。`02_rag` 建索引时把 `faiss_row` 回填。

---

## 四、数据更新机制

### 4.1 三种更新粒度

| 粒度 | 触发 | 行为 |
| --- | --- | --- |
| 全量 `full` | 首次建库、结构大改 | 遍历所有列表页 + sitemap，逐篇比对 |
| 增量 `incremental` | 日常定时（建议每日） | 只处理「发布日期 > 上次成功任务日期」的条目；无日期的条目跳过（宁可漏，不可重复） |
| 单篇重采 | 人工点击「立即核验」 | 按 doc_id 取 URL 重抓，走同一 `upsert_document` 路径 |

### 4.2 更新时发生什么

```
fetch detail ─→ parse ─→ content_hash 与库内比对
                            ├─ 相同 → 'unchanged'，只更新 last_fetched_at
                            └─ 不同 → 'updated'：旧正文快照进 doc_versions、
                                        current_version+1、生成 diff_summary
                                      └─→ 字段重新抽取
                                            ├─ 值相同 → 仅刷新 fetched_at
                                            └─ 值不同 → 旧值**不删**，
                                                 双方 status='冲突'，共享 conflict_group，
                                                 进 review_queue(severity=high)
```

**为什么冲突不覆盖**：一条公告写 65 元、另一条写 80 元，谁对取决于生效日期与适用范围。
系统不替人做判断，只把两个值、两段原文、两个链接摆出来，让人在 30 秒内点「采纳 A / 采纳 B」。

### 4.3 过期不是删除

`effective_to < 今天` 的字段，`Cleaner._downgrade_expired` 把 `status` 改成 `待核验` 并入队
`review_queue(reason='expired')`。数据留着，只是页面会显示黄色警示，问答会带免责说明。

### 4.4 幂等与可重跑

- 所有写入按 `url_hash` / `(doc_id, version_no)` / `(doc_id, chunk_index)` / `(spot, field, value, doc)` 去重。
- 同一份数据跑 10 次，库里的行数不变；只有真实变化才产生新行。
- 测试里 `test_replace_chunks_is_idempotent`、`test_upsert_document_new_then_unchanged` 守着这条。

---

## 五、爬虫实现与反爬处理

目标站点通常有四类限制，`anticrawl/` 逐个对付：

### 5.1 指纹一致性（`fingerprint.py`）

三层必须"同一个人"：

| 层 | 手段 | 实现 |
| --- | --- | --- |
| TLS 指纹（JA3/JA4） | `curl_cffi` 的 `impersonate="chrome124"` | 直接复用真实 Chrome 的 ClientHello 顺序与扩展 |
| HTTP/2 帧指纹 | 交给 curl_cffi 的 h2 实现 | 不手写 h2，避免窗口大小/优先级特征暴露 |
| 头部顺序与 sec-ch-ua | `OrderedDict` 固定顺序 + 与 UA 匹配的 `sec-ch-ua` | `browser_headers()` 按 Chrome 实际顺序拼 |

只改 UA 是最容易被识破的做法：UA 说 Chrome124、TLS 却是 Python requests，
一个 JA3 哈希就穿帮。所以三者必须同源。

### 5.2 频率与节奏（`throttle.py`）

```python
HostThrottle(min_interval=2.0, max_interval=4.5, burst_limit=3, burst_sleep=15)
```

- 每个 host 独立计时，互不牵连；
- 间隔在 `[2.0, 4.5]` 秒随机，不是固定 2 秒——固定间隔本身就是机器人特征；
- 12% 概率追加一次 `[4.5, 13.5]` 秒的长停顿，模拟人走开；
- 每连续 3 次请求后强制睡 15 秒，让单分钟的请求数不出现在直方图的长尾；
- 触发 429 立即 `cool_down_for_host(host, 60)`；
- 重试退避 `min(1.8**attempt × U(0.7,1.3), 300)`，带抖动避免多线程同步重试。

### 5.3 代理池（`proxy_pool.py`）

```
rotate: per_request | per_session | sticky_5min
```

- `sticky_5min` 是默认倾向：同一会话在 5 分钟内用同一出口 IP，因为"同一访客翻页翻到一半换 IP"更可疑；
- 失败计数达 `max_fail_count`（默认 3）才隔离，隔离时长 `cooldown × min(fail_count, 5)`；
- `hard=True`（连接直接失败）立即隔离，不浪费一次重试；
- 日志与 `__str__` 一律脱敏密码（`http://user:***@host`），避免凭证进日志。

代理列表从 `config/settings.yaml` 或环境变量 `CRAWL_PROXY` 读，支持 `http/socks5`。

### 5.4 验证码（`captcha.py`）

四种形态，四种打法：

| 类型 | 方法 | 说明 |
| --- | --- | --- |
| 图片字符码 | `solve_image` → ddddocr | 轻量 CNN，离线可跑，不依赖 GPU |
| 算术码 | `solve_arithmetic` | OCR 出 `3+5=?` 后 `ast` 安全求值，只允许 `+-*/` |
| 滑块 | `solve_slider` | **不依赖 OpenCV**：Sobel-x 求边缘 → 列投影 → 缺口位置 argmax；搜索区间限定 `[0.05W, W-piece-4]` 排除边界噪声 |
| 点选 | `solve_click` | ddddocr 的 det 模型返回坐标，按序点击 |

识别不了就 `_fallback` 把图片落盘到 `data/manual_captcha/<hash>.png`，
在旁边等一份同名 `.txt`——人工填进去，流程自己继续。**不静默跳过**。

### 5.5 拦截判定（`client._classify`）

最坑的不是 403，而是**200 但返回的是拦截页**。判定双路：

```python
if status in (403, 401): return True, "block"
if status == 429:        return True, "rate_limit"
if status == 503:        # 看正文是限流话术还是硬拦
if len(content) < 4096:  # 只有短正文才查标记，避免长正文误杀
    命中 CAPTCHA_MARKERS → "captcha"
    命中 BLOCK_MARKERS   → "block"
```

`len(content) < 4096` 这个门槛来自实测：正常政策正文动辄几十 KB，
而被拦时返回的是几百字节的提示页。少了这个判断，「本景区实行安全验证制度」这种
正常句子会被误判成拦截，导致整站抓不到。

### 5.6 渲染兜底（`browser_fallback`）

详情页失败且错误含 403/blocked 时，用 Playwright 真渲染一次：

```js
Object.defineProperty(navigator, 'webdriver', { get: () => undefined })
window.chrome = { runtime: {} }
```

只抹两个最常被检的痕迹，不做大规模注入——注入越多，反而越容易被行为分析识别。
渲染是最后手段，比纯 HTTP 慢 20 倍，所以只在失败后触发。

### 5.7 真实站点适配（`parsers/wlt_gov.py`）

福建省文旅厅站点的三个反常识之处，写解析器时踩了个遍：

**列表不在 HTML 里。** 栏目页由前端脚本渲染，静态 HTML 只有骨架。
真实数据走站内检索接口 `GET /was5/web/search`，参数 `channelid` + `classsql` + `page`。
两个细节：响应头是 `text/html` 但正文是 JSON；JSON 里带裸控制字符，
必须 `json.loads(text, strict=False)`，用严格模式直接抛
`Invalid control character at: line 19 column 16`。

**详情页有两套模板。** 旧模板正文在 `div.detp`，新模板在
`div.article_content` / `div.TRS_Editor`。旧模板里还嵌了一整层
`<html><head></head><body>`——BeautifulSoup 遇到嵌套 body 会重排结构，
`get_text()` 直接返回空串。所以解析前先剥掉嵌套的 html/head/body 标签。

**附件有时在 JS 数组里。** 新模板的 `<a href="...docx">` 被 `<!-- -->` 注释掉了
（在条件渲染的下载条里），真身是 `var apps = [{name, url}, ...]`。
只用 CSS 选择器一条都拿不到，得正则抓数组。

正文为空的页面分两种，处理方式不同：

| 情况 | 判定 | 处理 |
| --- | --- | --- |
| 正文是图片 | 容器非空但文本为 0、含 `<img>` | 入库并标 `image_only`，不做 OCR 猜测 |
| 正文在附件里 | 正文短、附件列表非空 | 下载附件解析并入正文 |

**绝不回退到「抓整页 body」**——整页九成是导航页脚，混进语料会毁掉检索。
为此有 `BOILERPLATE_MARKS` 样板词表，命中两条以上就丢弃该容器。

### 5.8 附件格式

按 magic 判型优先于扩展名，因为政务站的扩展名经常与真实字节不符。

| 格式 | 解析方式 |
| --- | --- |
| PDF | `pypdf`，保留每页文本 |
| DOCX | `python-docx`，保留标题层级与表格 |
| DOC / WPS | OLE2 + piece table，见下 |
| OFD | zip + XML，取 `TextCode` 节点 |
| XLSX / CSV | `openpyxl` / `csv`，编码回退 `utf-8-sig → gb18030` |

`.doc` 走 piece table 而不是「扫可打印字符」：后者会把页眉、样式名、修订记录
一起捞进来，段落顺序也是乱的。piece table 是 Word 自己记录「哪个区间对应
正文第几段」的索引，按它解出来的就是阅读顺序。关键路径是
`WordDocument` 流的 FIB → `fWhichTblStm` 决定用 `0Table` 还是 `1Table`
→ 该流的 Clx → Pcdt → PlcPcd → 每段 PCD 的 `fc` 高位 `0x40000000`
标记单字节 CP1252，否则 UTF-16LE。

实测两篇真实公文解出 351 字与 2661 字，文号、落款、印发日期都在，顺序正确。

### 5.9 新站接入成本

```yaml
sites:
  - key: fj_wlt
    base_url: "https://wlt.fujian.gov.cn"
    parser: wlt_gov
    api: {url: ".../was5/web/search", channelid: "256730", prepage: 50}
    channels:
      - {key: zcfg, classsql: "parentid,chnlid+=55443,55444,55445,37577", max_pages: 8}
      - {key: tzgg, classsql: "parentid,chnlid+=37547,37549,37564,37546", max_pages: 20}
    detail_selector:
      title: ["meta[name=ArticleTitle]", "h1"]
      body:  ["div.article_content", "div.TRS_Editor", "div.detp"]
```

selector 是**列表**，按顺序试第一个非空的——两套模板共用一份配置。
结构差异大到选择器表达不了才需要写新 parser（继承 `BaseParser`，
实现 `parse_list` / `parse_detail`，再 `register("name", cls)`）。

---

## 六、字段校验与呈现

### 6.1 什么算「可核验」

六个易变字段——开放时间、票价、政策有效期、地址、预约方式、电话——每个值必须能回答五问：

1. 出自哪篇文档？（`source_doc_id` + `source_url` + `doc_title`）
2. 谁发的？（`issuer`）
3. 什么时候发的？（`publish_date`）
4. 什么时候生效、到什么时候？（`effective_from` / `effective_to`）
5. 原文怎么说的？（`quote`，≤200 字）

五问齐 → `已核验`；缺任何一项 → `待核验`；同字段多值 → `冲突`。

### 6.2 两层校验（`validator.py`）

**第一层：溯源完整性**

| 检查 | 级别 |
| --- | --- |
| `quote` 为空 | high |
| `quote` > 200 字 | low |
| `source_url` 为空 | high |
| `publish_date` 为空 | medium |
| 已超 `effective_to` | high |

**第二层：字段语义**

| 字段 | 规则 | 判错案例 |
| --- | --- | --- |
| 开放时间 | 小时 0-24、分钟 0-60、起止不能相同 | `25:00—26:00`、`09:00—09:00` |
| 票价 | 数字且 ≤ 2000 元；=0 视为可疑 | `9999 元` |
| 电话 | 手机 `^1[3-9]\d{9}$` 或座机 `^0\d{2,3}-?\d{7,8}(-\d+)?$` | `12345` |
| 地址 | 长度 ≥6 且含行政区划词 | `湖东路` |
| 预约方式 | 含 微信/小程/网站/电话/平台/实名 之一 | 空泛描述 |
| 政策有效期 | 含四位年份 | `长期有效`（→ 待核验，非判错） |

**只降级不删证据**：校验失败把 `status` 改成 `待核验` 并进 `review_queue`，
原文和摘录一字不动。证据一旦被程序删掉，人工复核就没得看了。

### 6.3 页面呈现（由 02_rag 渲染）

- **三态用颜色和图标区分**，不只靠文字：绿=已核验、黄=待核验、红=冲突；
- 每个字段值后面跟一个「来源」按钮，点开显示：文档标题、发文机关、发布日期、
  原文摘录、原链接、抓取时间；
- **冲突字段并排显示两个值**，各自带来源与生效日期，顶部一句「系统未自动合并，
  请人工确认」，配两篇文档的 `diff_summary`（哪句新增、哪句删除）；
- 过期的字段给一行黄色说明：「该政策已于 YYYY-MM-DD 失效，以下内容仅供参考」；
- 全站页脚常驻数据口径：`数据抓取于 YYYY-MM-DD HH:MM`。

---

## 七、怎么跑

```bash
# 0) 建虚拟环境（本机用 uv，镜像走阿里云）
uv venv D:\app\python-envs\fujian-tourism --python 3.12
$env:UV_DEFAULT_INDEX = 'https://mirrors.aliyun.com/pypi/simple'
uv pip install --python D:\app\python-envs\fujian-tourism\Scripts\python.exe -r requirements.txt

# 1) 建库
python cli.py init

# 2) 采集（真实站点，走 was5 检索接口 + 详情页 + 附件）
python cli.py crawl --all                    # 全量：三个栏目都抓
python cli.py crawl --site fj_wlt --incremental   # 增量：只看上次之后的新增
python cli.py pipeline --all                 # 一步跑完 采集→清洗→校验→导出

# 预算控制（默认取 settings.yaml 的 max_requests_per_run / detail_limit）
python cli.py --max-requests 600 --detail-limit 260 crawl --all

# 3) 分步跑
python cli.py clean                          # 清洗 + 字段抽取 + 切片
python cli.py validate                       # 校验并降级
python cli.py export                         # 导出语料与质量报告

# 4) 看结果
python cli.py stats                          # 库内计数
python cli.py report                         # 质量报告摘要
```

产物位置：

```
data/tourism.db                     # SQLite 主库
data/raw/<site>/<ts>_<name>.html    # 原文快照（可回溯）
data/corpus/documents.jsonl         # 每行：文档元数据 + chunks
data/corpus/spots.jsonl             # 每行：景区 + 全部字段证据
data/reports/quality_report.json    # 质量报告（机器可读）
logs/crawl.log                      # 逐 URL 事件
```

`data/manual_captcha/` 会出现在验证码识别失败时——里面是待人工填写的图片，
离开这个目录就说明那次跑没有需要人工的地方。

---

## 八、测试

```bash
python -m pytest tests -q
```

52 项，全部离线：

| 文件 | 覆盖 |
| --- | --- |
| `test_utils.py` | URL 归一化去跟踪参数、内容哈希抗空白、日期区间、截断落标点 |
| `test_parsers.py` | 列表解析、相对链接补全、详情抽文号/日期/有效期、去 script、无效页判定、名录表两形态、注册表回退 |
| `test_db_provenance.py` | 版本演进、变更差集、证据去重、**冲突双值保留**、10 个溯源列存在、切片幂等、chunk_id 格式 |
| `test_cleaner_chunk.py` | 去导航/页脚/页码、字段抽取按句界定界、剥标签前缀、三态判定、切片 id 与长度上限、按小标题切 |
| `test_client_classify.py` | 403/401/429/503 判定、200 短拦截页识别、长正文不误杀、验证码标记 |
| `test_attachments.py` | magic 判型优先于扩展名、OLE2 残缺文件不静默吞、`.wps` 路由到 Word 解析器、OFD 文本抽取、zip 家族内部细分 |

---

## 九、已知限制

1. **站点结构会变**。目标站栏目页由前端脚本渲染，列表只能走它的检索接口；
   接口参数（`channelid`、`classsql`）写在 `settings.yaml` 里，改版时改配置不改代码。
2. **验证码只覆盖无状态图片码**。需要 JS 生成 token、行为轨迹、或第三方打码平台的场景，
   走 `browser_fallback` 或 `captcha.engine=remote_api`。
3. **`.xls` 老格式不解析**。`.doc`/`.wps`/`.ofd`/`.pdf`/`.docx`/`.xlsx`/`.csv` 都走真实解析；
   老式 `.xls`（BIFF）仍返回明确错误而不是静默丢弃。
4. **近重复判定用前 500 字指纹**。对「同一篇公告被转载但标题不同」有效，
   对「正文前半相同、后半不同」的改写稿会漏判，属于精度换成本的取舍。
5. **正文是图片的公告只入库不下结论**。这类页标 `image_only`，正文为空，
   字段抽取自然拿不到值——不做 OCR 猜测，避免把图里的错字当成原文。
6. **不做法律合规判断**。抓取范围、频率、robots 遵从度由使用方按目标站点规则设定；
   本工具的 `--bypass` 开关会关闭 robots 检查，默认保持开启。
