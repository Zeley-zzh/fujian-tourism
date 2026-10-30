-- ============================================================
-- 福建文旅政策与景区知识库 · 结构化数据库
-- 设计取舍说明见 README.md「为什么这么拆」
-- ============================================================
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- ---------- 1. 文档主表（政策、公告、资讯稿、公开附件） ----------
CREATE TABLE IF NOT EXISTS documents (
    doc_id            TEXT PRIMARY KEY,              -- d0001
    url               TEXT NOT NULL,
    url_hash          TEXT NOT NULL UNIQUE,          -- sha1(normalized_url)，去重用
    doc_type          TEXT NOT NULL,                 -- policy | notice | news | attachment
    title             TEXT NOT NULL,
    issuer            TEXT,                          -- 发文单位
    doc_no            TEXT,                          -- 文号（无则 NULL，绝不编造）
    publish_date      TEXT,                          -- YYYY-MM-DD
    effective_from    TEXT,
    effective_to      TEXT,                          -- NULL = 另行公告
    content_html      TEXT,
    content_text      TEXT,                          -- 清洗后纯文本，RAG 用
    content_hash      TEXT NOT NULL,                 -- 正文 sha256，变更检测
    raw_path          TEXT,                          -- data/raw/<doc_id>/xxx
    fetch_status      TEXT NOT NULL DEFAULT 'ok',    -- ok | partial | failed
    first_seen_at     TEXT NOT NULL,
    last_fetched_at   TEXT NOT NULL,
    current_version   INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_documents_publish ON documents(publish_date DESC);
CREATE INDEX IF NOT EXISTS idx_documents_type    ON documents(doc_type, publish_date DESC);
CREATE INDEX IF NOT EXISTS idx_documents_hash    ON documents(content_hash);

-- ---------- 2. 文档版本（新增版本行，不覆盖） ----------
CREATE TABLE IF NOT EXISTS doc_versions (
    version_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id         TEXT NOT NULL REFERENCES documents(doc_id) ON DELETE CASCADE,
    version_no     INTEGER NOT NULL,
    content_hash   TEXT NOT NULL,
    content_text   TEXT NOT NULL,
    content_html   TEXT,
    title          TEXT NOT NULL,
    publish_date   TEXT,
    fetched_at     TEXT NOT NULL,
    diff_summary   TEXT,                -- 与上一版的差异摘要（人可读）
    UNIQUE(doc_id, version_no)
);
CREATE INDEX IF NOT EXISTS idx_docver_doc ON doc_versions(doc_id, version_no DESC);

-- ---------- 3. 景区档案 ----------
CREATE TABLE IF NOT EXISTS scenic_spots (
    spot_id        TEXT PRIMARY KEY,               -- s0001
    name           TEXT NOT NULL,
    alias          TEXT,                           -- 别名/曾用名，逗号分隔
    level          TEXT,                           -- 5A | 4A | 3A | 其他
    category       TEXT,                           -- A级旅游景区 | 省级旅游度假区 | 国家级旅游度假区
    city           TEXT,                           -- 设区市
    county         TEXT,
    address        TEXT,
    lng REAL, lat REAL,
    intro          TEXT,
    source_doc_id  TEXT REFERENCES documents(doc_id),
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_spots_city  ON scenic_spots(city, level);
CREATE INDEX IF NOT EXISTS idx_spots_level ON scenic_spots(level);

-- ---------- 4. 易变字段溯源（核心：一条字段值一条证据） ----------
CREATE TABLE IF NOT EXISTS field_provenance (
    prov_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    spot_id        TEXT NOT NULL REFERENCES scenic_spots(spot_id) ON DELETE CASCADE,
    field_name     TEXT NOT NULL,      -- opening_hours | ticket_price | policy_validity
                                       -- address | booking_method | phone
    field_value    TEXT NOT NULL,
    source_doc_id  TEXT NOT NULL REFERENCES documents(doc_id),
    source_url     TEXT NOT NULL,
    doc_title      TEXT NOT NULL,
    issuer         TEXT,
    publish_date   TEXT,
    effective_from TEXT,
    effective_to   TEXT,
    quote          TEXT,               -- 官方原文摘录，≤200 字
    fetched_at     TEXT NOT NULL,
    status         TEXT NOT NULL,      -- 已核验 | 待核验 | 冲突
    is_current     INTEGER NOT NULL DEFAULT 1,  -- 1=当前生效展示值
    conflict_group TEXT,               -- 同组冲突时相同值，便于并列展示
    reviewed_by    TEXT,               -- 人工复核人
    reviewed_at    TEXT,
    review_note    TEXT
);
CREATE INDEX IF NOT EXISTS idx_prov_spot_field ON field_provenance(spot_id, field_name, is_current);
CREATE INDEX IF NOT EXISTS idx_prov_status     ON field_provenance(status);
CREATE INDEX IF NOT EXISTS idx_prov_conflict   ON field_provenance(conflict_group);

-- ---------- 5. 景区 ⇄ 政策 关联 ----------
CREATE TABLE IF NOT EXISTS spot_policy_link (
    link_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    spot_id      TEXT NOT NULL REFERENCES scenic_spots(spot_id) ON DELETE CASCADE,
    doc_id       TEXT NOT NULL REFERENCES documents(doc_id) ON DELETE CASCADE,
    relation     TEXT NOT NULL,        -- applies_to | mentions | supersedes | references
    confidence   REAL NOT NULL DEFAULT 1.0,
    evidence     TEXT,                 -- 命中的原句，说明为什么认为有关联
    created_at   TEXT NOT NULL,
    UNIQUE(spot_id, doc_id, relation)
);
CREATE INDEX IF NOT EXISTS idx_link_spot ON spot_policy_link(spot_id);
CREATE INDEX IF NOT EXISTS idx_link_doc  ON spot_policy_link(doc_id);

-- ---------- 6. 文档切片（RAG 语料） ----------
CREATE TABLE IF NOT EXISTS chunks (
    chunk_id       TEXT PRIMARY KEY,   -- chunk_d0007_0042
    doc_id         TEXT NOT NULL REFERENCES documents(doc_id) ON DELETE CASCADE,
    chunk_index    INTEGER NOT NULL,
    text           TEXT NOT NULL,
    char_len       INTEGER NOT NULL,
    heading_path   TEXT,               -- 「第三章 > 门票价格」层级路径
    page_no        INTEGER,            -- PDF 页码
    faiss_row      INTEGER,            -- 在 .faiss 文件中的行号
    token_estimate INTEGER,
    created_at     TEXT NOT NULL,
    UNIQUE(doc_id, chunk_index)
);
CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(doc_id, chunk_index);

-- ---------- 7. 向量索引元数据 ----------
CREATE TABLE IF NOT EXISTS faiss_index_meta (
    index_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    index_name     TEXT NOT NULL UNIQUE,   -- fujian_tourism_v1
    embedding_model TEXT NOT NULL,          -- BAAI/bge-small-zh-v1.5
    dim            INTEGER NOT NULL,
    metric         TEXT NOT NULL,           -- IP | L2
    total_vectors  INTEGER NOT NULL,
    nlist          INTEGER,                 -- IVF 聚类数（Flat 时为 NULL）
    index_path     TEXT NOT NULL,
    corpus_hash    TEXT NOT NULL,           -- 语料 sha256，判断是否需重建
    built_at       TEXT NOT NULL
);

-- ---------- 8. 抓取任务与日志 ----------
CREATE TABLE IF NOT EXISTS crawl_jobs (
    job_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    job_name     TEXT NOT NULL,
    mode         TEXT NOT NULL,          -- full | incremental
    site_key     TEXT NOT NULL,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    status       TEXT NOT NULL DEFAULT 'running',  -- running | done | failed
    pages_fetched INTEGER DEFAULT 0,
    docs_new      INTEGER DEFAULT 0,
    docs_changed  INTEGER DEFAULT 0,
    docs_failed   INTEGER DEFAULT 0,
    note         TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_started ON crawl_jobs(started_at DESC);

CREATE TABLE IF NOT EXISTS crawl_logs (
    log_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id     INTEGER REFERENCES crawl_jobs(job_id) ON DELETE CASCADE,
    ts         TEXT NOT NULL,
    level      TEXT NOT NULL,            -- INFO | WARN | ERROR
    url        TEXT,
    event      TEXT NOT NULL,            -- fetch_ok | http_403 | captcha | retry | parse_fail ...
    detail     TEXT,
    elapsed_ms INTEGER
);
CREATE INDEX IF NOT EXISTS idx_logs_job ON crawl_logs(job_id, ts);

-- ---------- 9. 人工复核队列 ----------
CREATE TABLE IF NOT EXISTS review_queue (
    item_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    prov_id     INTEGER REFERENCES field_provenance(prov_id) ON DELETE CASCADE,
    doc_id      TEXT REFERENCES documents(doc_id) ON DELETE CASCADE,
    reason      TEXT NOT NULL,           -- low_confidence | conflict | expired | missing_quote
    severity    TEXT NOT NULL DEFAULT 'medium',
    created_at  TEXT NOT NULL,
    resolved_at TEXT,
    resolver    TEXT,
    resolution  TEXT
);
CREATE INDEX IF NOT EXISTS idx_review_open ON review_queue(resolved_at, severity);

-- ---------- 10. 便捷视图：每个景区当前字段 ----------
CREATE VIEW IF NOT EXISTS v_spot_current_fields AS
SELECT s.spot_id, s.name, p.field_name, p.field_value, p.status,
       p.source_doc_id, p.source_url, p.doc_title, p.issuer,
       p.publish_date, p.effective_from, p.effective_to, p.quote, p.fetched_at
FROM scenic_spots s
JOIN field_provenance p ON p.spot_id = s.spot_id AND p.is_current = 1;
