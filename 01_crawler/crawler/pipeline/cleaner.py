"""数据清洗与调优：正文去噪、去重、实体抽取、字段溯源生成、切片。

这是「清洗调优」的主体，四件事：
1. 正文规范化（去页眉页脚、去重复导航、合并断行）
2. 近重复文档检测（转载稿常一字不差重复发布）
3. 易变字段抽取 → field_provenance（带 quote 原文摘录与三态判定）
4. 中文切片 → chunks（与 RAG 侧同参数，避免两端切法不一致）
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field

from ..config import Settings
from ..db import Database
from ..utils import (clean_text, collapse_for_hash, cjk_ratio, content_hash,
                     get_logger, in_effective_range, today, truncate)

# ---------------- 去噪规则 ----------------
NAV_PATTERNS = [
    r"^(首页|网站首页|返回|打印|关闭|分享到|上一篇|下一篇|当前位置|您现在的位置)\s*[:：>》]?.*$",
    r"^(主办单位|承办单位|技术支持|网站标识码|ICP备|公安机关备案|政府网站).*$",
    r"^\s*\d+\s*/\s*\d+\s*$",                 # 页码 1/3
    r"^(字体|字号)\s*[:：].*$",
    r"^(扫一扫|二维码|微信扫).*$",
]
NAV_RE = [re.compile(p) for p in NAV_PATTERNS]

FOOTER_KEYS = ("主办单位", "承办单位", "技术支持", "网站标识码", "ICP备", "版权所有", "公安机关备案")


@dataclass
class CleanStats:
    scanned: int = 0
    cleaned: int = 0
    duplicates: int = 0
    fields_extracted: int = 0
    fields_verified: int = 0
    fields_pending: int = 0
    fields_conflict: int = 0
    chunks: int = 0
    spots: int = 0


class Cleaner:
    def __init__(self, settings: Settings, db: Database):
        self.s = settings
        self.db = db
        self.log = get_logger("cleaner", settings.p("log_dir"))
        self.stats = CleanStats()

    # ================================================================== 主流程
    def run(self) -> CleanStats:
        rows = self.db.documents_for_export()
        self.stats.scanned = len(rows)
        self.log.info("开始清洗 %d 篇文档", len(rows))

        # 1) 正文清洗 + 近重复标记
        cleaned_texts: dict[str, str] = {}
        hash_buckets: dict[str, list[str]] = defaultdict(list)
        for row in rows:
            text = self.normalize_body(row["content_text"] or "", row["title"] or "")
            cleaned_texts[row["doc_id"]] = text
            # 近重复用「去空白后的 500 字前缀」做粗指纹，转载稿常整段照搬
            fp = content_hash(text[:500])
            hash_buckets[fp].append(row["doc_id"])
            self.stats.cleaned += 1

        dup_groups = {k: v for k, v in hash_buckets.items() if len(v) > 1}
        self.stats.duplicates = sum(len(v) - 1 for v in dup_groups.values())
        primary = {}   # 被合并的 doc_id → 主文档 doc_id
        for ids in dup_groups.values():
            ids_sorted = sorted(ids)
            for dup in ids_sorted[1:]:
                primary[dup] = ids_sorted[0]
        if dup_groups:
            self.log.info("检出 %d 组近重复文档，共 %d 篇被判定为转载",
                          len(dup_groups), self.stats.duplicates)

        # 2) 景区档案 + 字段溯源（只从非重复文档抽，避免同一句话抽两遍）
        for row in rows:
            doc_id = row["doc_id"]
            if doc_id in primary:
                continue
            text = cleaned_texts[doc_id]
            if not text:
                continue
            self._extract_spots_and_fields(row, text)

        # 3) 景区-政策关联
        self._link_spots_to_docs()

        # 4) 切片
        for row in rows:
            doc_id = row["doc_id"]
            text = cleaned_texts[doc_id]
            if not text.strip():
                continue
            chunks = self.chunk_text(text, doc_id=doc_id,
                                     doc_title=row["title"] or "",
                                     publish_date=row["publish_date"])
            self.db.replace_chunks(doc_id, chunks)
            self.stats.chunks += len(chunks)

        # 5) 到期自动降级
        self._downgrade_expired()

        self.log.info("清洗完成: 文档=%d 切片=%d 字段=%d (已核验 %d / 待核验 %d / 冲突 %d)",
                      self.stats.cleaned, self.stats.chunks, self.stats.fields_extracted,
                      self.stats.fields_verified, self.stats.fields_pending,
                      self.stats.fields_conflict)
        return self.stats

    # ================================================================== 正文清洗
    def normalize_body(self, text: str, title: str) -> str:
        """去导航/页脚/重复行，合并被 HTML 硬断开的句子。"""
        text = clean_text(text)
        if not text:
            return ""

        lines = text.split("\n")
        # 去掉标题重复行（正文第一行常是标题）
        if lines and title and collapse_for_hash(lines[0]) == collapse_for_hash(title):
            lines = lines[1:]

        kept: list[str] = []
        seen_lines: set[str] = set()
        for line in lines:
            s = line.strip()
            if not s:
                continue
            if any(rx.match(s) for rx in NAV_RE):
                continue
            if any(k in s for k in FOOTER_KEYS):
                continue
            # 连续重复行（分页导致的重复段落）
            key = collapse_for_hash(s)
            if key in seen_lines and len(s) < 80:
                continue
            seen_lines.add(key)
            kept.append(s)

        body = "\n".join(kept)

        # 合并被硬断行的句子：中文行尾不是句末标点且下一行也不像新段落时接上
        merged: list[str] = []
        endings = "。！？；：”』】)"
        for line in body.split("\n"):
            if (merged and len(merged[-1]) > 20
                    and merged[-1][-1] not in endings
                    and not re.match(r"^[一二三四五六七八九十\d]+[、.．)）]", line)
                    and len(line) > 10):
                merged[-1] = merged[-1] + line
            else:
                merged.append(line)

        body = "\n".join(merged)
        body = re.sub(r"\n{3,}", "\n\n", body)
        return body.strip()

    # ================================================================== 字段抽取
    def _extract_spots_and_fields(self, row, text: str) -> None:
        cfg = self.s.scenic_fields or {}
        title = row["title"] or ""

        # 名录类公告与单景区公告是两种文档，必须分开处理。
        # 名录（「1.漳州高峰谷景区」这种编号行）里每个名字只出现一次，
        # 全文共用一句「确定以下 7 家旅游景区为国家 4A 级」。
        # 若把单景区那套字段抽取套到名录的每个条目上，几十个景区会共享同一句
        # 「门票 65 元」—— 那是凭空编造的关联关系。所以名录只记
        # 「存在 + 等级 + 城市」这三件原文真的说了的事。
        roster = self._detect_roster(text, title)
        if roster:
            for item in roster:
                spot_id = self.db.upsert_spot(
                    name=item["name"],
                    city=item["city"],
                    level=item["level"],
                    category=item["category"],
                    source_doc_id=row["doc_id"],
                )
                self.stats.spots += 1
                if item["level"]:
                    self._add_field(row, spot_id, "level", item["level"], item["quote"])
                # 类别（A级旅游景区 / 省级旅游度假区）原文确实写了，独立记一条证据：
                # 只有等级列的话，度假区会因为「没有几 A」而看起来什么都没说。
                self._add_field(row, spot_id, "category", item["category"], item["quote"])
            return

        # 单篇公告：只有标题点名的景区才算「这篇文档讲的就是它」。
        # 正文顺带提到的景区一律不抽字段——实测一篇《福建省邮轮旅游产业发展
        # 规划》正文里提到了崇武古城、南靖土楼、永定土楼，若按正文命中就抽，
        # 三个景区会共享同一句「位于海上丝绸之路核心区，共建 21 世纪海上丝绸
        # 之路……」，这是凭空造出来的关联。同理《新增 4 家 4A 级景区》那篇是
        # 名录体，正文提到的名字不该各自带上同一段介绍。
        spot_names = self._detect_spot_names(text, title, title_only=True)
        if not spot_names:
            return

        for spot_name in spot_names:
            fields: dict[str, tuple[str, str | None]] = {}
            for field_name, spec in cfg.items():
                value, quote = self._extract_field(text, spec)
                if value:
                    fields[field_name] = (value, quote)
            spot_id = self.db.upsert_spot(
                name=spot_name,
                # 名称里自带的地名最准；名称里没有才退回整篇文档的城市。
                # 反例：一篇讲全省的公告里提到「武夷山」，整篇的城市标记是福州，
                # 直接套用会把武夷山标成福州。
                city=self._city_from_name(spot_name) or self._detect_city(text),
                level=self._detect_level(text),
                # 冗余列只做列表页展示，值必须与溯源记录一致，不许另起一套口径
                address=(fields.get("address") or (None, None))[0],
                source_doc_id=row["doc_id"],
            )
            self.stats.spots += 1

            for field_name, (value, quote) in fields.items():
                self._add_field(row, spot_id, field_name, value, quote)

    def _add_field(self, row, spot_id: str, field_name: str, value: str,
                   quote: str | None) -> None:
        status = self._judge_status(
            quote=quote,
            source_url=row["url"],
            publish_date=row["publish_date"],
            eff_from=row["effective_from"],
            eff_to=row["effective_to"],
        )
        self.db.add_provenance(
            spot_id=spot_id,
            field_name=field_name,
            field_value=value,
            source_doc_id=row["doc_id"],
            source_url=row["url"],
            doc_title=row["title"] or "",
            issuer=row["issuer"],
            publish_date=row["publish_date"],
            effective_from=row["effective_from"],
            effective_to=row["effective_to"],
            quote=quote,
            status=status,
        )
        self.stats.fields_extracted += 1
        if status == "已核验":
            self.stats.fields_verified += 1
        elif status == "冲突":
            self.stats.fields_conflict += 1
        else:
            self.stats.fields_pending += 1

    # -------------------------------------------------------------- 名录识别
    # 「1.漳州高峰谷景区」「（2）泉州八仙过海旅游度假区」这类编号行
    ROSTER_NUM_RE = re.compile(
        r"^\s*(?:[(（]?\d{1,2}(?:[)）.、．]|\s*[)）]))\s*(?P<name>[^\s].{1,38})$")
    # 「一、4A级旅游景区」这类分级小标题
    SECTION_RE = re.compile(r"^[一二三四五六七八九十]{1,3}\s*[、.．]\s*(?P<head>\S.{0,38})$")
    LEVEL_IN_HEAD_RE = re.compile(r"([1-5])\s*A\s*级")
    # 行内等级：「…景区(3A)」（红色旅游名单每行自带）
    LEVEL_IN_LINE_RE = re.compile(r"[（(]\s*([1-5])\s*A\s*[)）]")
    # 小标题里去掉这些后如果什么都不剩，说明整行只是分类名，不是景区名
    SECTION_FILLER_RE = re.compile(
        r"[1-5]\s*A\s*级|国家级|省级|市级|县级|福建省|福建|全省|全市|全国|旅游度假区|"
        r"旅游景区|红色旅游|智慧景区|景区|度假区|博物馆|纪念馆|的")

    def _detect_roster(self, text: str, title: str) -> list[dict]:
        """识别名单式文档，返回 [{name, level, city, quote}]。

        两种形态都见过：
        - 编号行：`1.漳州高峰谷景区`（2022 年 4A 级名单）
        - 纯名字行：`鼓岭旅游度假区`（度假区名单，靠上下「国家级/省级」分段）
        判据是「至少有 3 个以景区后缀结尾的独立行」，否则当单景区文档。
        """
        items: list[dict] = []
        seen: set[str] = set()
        # 标题本身就声明了等级时，整篇名单都按这个等级算 ——
        # 「关于确定 4 家旅游景区为国家 4A 级旅游景区的公告」正文里没有
        # 「一、4A 级旅游景区」这种小标题，等级只写在标题和首句里。
        # 不认标题会把这些公告整批判成 None，等于白丢等级。
        tm = self.LEVEL_IN_HEAD_RE.search(title or "")
        section_level: str | None = f"{tm.group(1)}A" if tm else None
        tier: str | None = None
        if "度假区" in (title or ""):
            tier = "国家级" if "国家级" in (title or "") else "省级"
        for raw in (text or "").splitlines():
            line = raw.strip()
            if not line:
                continue
            if len(line) <= 12 and line in ("国家级", "省级", "市级", "国家级旅游度假区",
                                            "省级旅游度假区"):
                tier = line.replace("旅游度假区", "")
                continue

            m = self.SECTION_RE.match(line)
            if m and len(line) <= 40:
                head = m.group("head")
                # 判小标题看「去掉通用词后还剩什么」，不能看后缀——
                # 「二、省级旅游度假区」也以「度假区」结尾，用后缀判会把它当景区名。
                if len(self.SECTION_FILLER_RE.sub("", head).strip(" ·、（）()")) <= 2:
                    lm = self.LEVEL_IN_HEAD_RE.search(head)
                    section_level = f"{lm.group(1)}A" if lm else None
                    if "度假区" in head:
                        tier = "省级" if "省级" in head else (
                            "国家级" if "国家级" in head else tier)
                    continue

            name = None
            line_level: str | None = None
            nm = self.ROSTER_NUM_RE.match(line)
            if nm:
                name = nm.group("name").strip().rstrip("。；;，,")
                lm2 = self.LEVEL_IN_LINE_RE.search(name)
                if lm2:
                    line_level = f"{lm2.group(1)}A"
            elif len(line) <= 24 and not re.search(r"[，。；：]", line) \
                    and not line.startswith(("国家级", "省级", "市级", "其中", "上述")):
                name = line
            if not name:
                continue

            # 先剥「(3A)」「等 7 家」这类尾巴，再判是否以景区后缀结尾——
            # 反过来会把「…景区(3A)」整行丢掉（红色旅游名单就带这个尾巴）。
            name = self._trim_spot_name(name)
            suffixes = self._allowed_suffixes(title)
            if not name or name in seen or not self._looks_like_spot_name(name, suffixes):
                continue
            seen.add(name)
            items.append({
                "name": name,
                "level": line_level or section_level,
                # 度假区名单只分国家级/省级，不写几 A；等级列留空，
                # 但类别要记下来，否则「省级度假区」和「4A 景区」混成一堆。
                "category": self._roster_category(title, name, tier),
                "city": self._city_from_name(name),
                "quote": truncate(line, 200),
            })
        return items if len(items) >= 3 else []

    @classmethod
    def _ends_with_suffix(cls, s: str) -> bool:
        return any(s.endswith(x) for x in cls.SPOT_SUFFIX)

    @staticmethod
    def _roster_category(title: str, name: str, tier: str | None) -> str:
        """名录条目的类别。原文只说了「景区」还是「度假区」，就只记这两个。

        「红色旅游」是名单主题不是类别：同一家景区既可能进红色旅游名单，
        又可能进 4A 级名单，把它当类别会让同一个景区被归成两类。
        """
        if "度假区" in name or "度假区" in (title or ""):
            return f"{tier}旅游度假区" if tier else "旅游度假区"
        return "A级旅游景区"

    # 这些词出现在「名字」里，说明抓到的是句子成分而不是地名。
    # 实测反例：推动景区、利用公共资源建设的景区、游客对旅游景区、中的景区。
    NAME_VERB_WORDS = ("推动", "利用", "建设", "建立", "成立", "落实", "管理", "促进",
                       "加强", "提升", "发展", "评定", "符合", "达到", "实行", "应当",
                       "可以", "属于", "纳入", "作为", "成为", "包括", "涉及", "确定",
                       "公布", "组织", "开展", "服务", "保护", "规划", "开发", "经营",
                       "收费", "开放", "部门", "有关", "相关", "各级", "各地", "工作")
    # 以这些片段开头说明原文行被从中间截断了（「漳州市…」断成「州市…」）
    NAME_FRAGMENT_START = ("州市", "省市", "区市", "家", "个", "条", "项", "级", "等")

    @classmethod
    def _looks_like_spot_name(cls, name: str, suffixes=None) -> bool:
        """判断一个候选串是否像景区专名（而不是句子碎片）。"""
        if not name or len(name) < 3 or len(name) > 18:
            return False
        if not any(name.endswith(x) for x in (suffixes or cls.SPOT_SUFFIX)):
            return False
        if re.search(r"[1-5]\s*A\s*级|[A-Za-z]|\d|\s", name):
            return False
        if name.startswith(cls.NAME_FRAGMENT_START):
            return False
        # 「九、莆田南日岛…」这类带序号残留
        if re.match(r"^[一二三四五六七八九十]{1,3}\s*[、.．]", name):
            return False
        # 句子连接词出现在名字中间，说明整串是从句子里截出来的
        # （实测反例：游客对旅游景区、物质文化遗产有机融入景区、打造邮轮旅游岛）
        if re.search(r"[对把被将向从于的与和及以考按由使令让,，。；：、]", name):
            return False
        if any(w in name for w in cls.NAME_VERB_WORDS):
            return False
        # 整串都是分类名（「省级旅游度假区」「福建省旅游景区」）时不算专名
        if len(cls.SECTION_FILLER_RE.sub("", name).strip(" ·、（）()")) <= 2:
            return False
        return True

    @classmethod
    def _allowed_suffixes(cls, title: str) -> tuple[str, ...]:
        """名录里允许的后缀。

        强后缀（景区/度假区/博物馆…）默认放行。裸「山」「岛」这类只在
        标题点名了同类对象时才放行 —— 「海岛名录」里的「大坠岛」是真地方，
        而一篇讲山岳管理的文件里，「刘大山」「谢秀山」是段落里的碎词。
        """
        extra = tuple(s for s in ("山", "岛", "湖", "塔", "桥", "寺", "庙", "园")
                      if s in (title or ""))
        return cls.STRONG_SUFFIX + extra

    @staticmethod
    def _trim_spot_name(name: str) -> str:
        """去掉「等7家」「（含XX）」「(3A)」这类尾巴，以及开头的序号残留。"""
        n = re.sub(r"^(?:\d{1,2}[.、．)）]|[(（]\d{1,2}[)）])\s*", "", name)
        n = re.sub(r"[（(][^）)]{0,20}[）)]\s*$", "", n).strip()
        n = re.sub(r"等\s*\d+\s*家.*$", "", n).strip()
        return n

    # 县市 → 地级市。名录里常写县名（宁化、松溪、柘荣），页面上标「市」更有用。
    COUNTY_CITY = {
        "宁化": "三明", "沙县": "三明", "泰宁": "三明", "建宁": "三明", "尤溪": "三明",
        "松溪": "南平", "武夷山": "南平", "建瓯": "南平", "邵武": "南平", "政和": "南平",
        "柘荣": "宁德", "福鼎": "宁德", "霞浦": "宁德", "屏南": "宁德", "古田": "宁德",
        "连江": "福州", "永泰": "福州", "长乐": "福州", "闽侯": "福州", "罗源": "福州",
        "连城": "龙岩", "上杭": "龙岩", "永定": "龙岩", "长汀": "龙岩", "武平": "龙岩",
        "惠安": "泉州", "晋江": "泉州", "石狮": "泉州", "德化": "泉州", "安溪": "泉州",
        "南靖": "漳州", "东山": "漳州", "平和": "漳州", "华安": "漳州", "漳浦": "漳州",
    }
    CITY_LIST = ("福州", "厦门", "漳州", "泉州", "三明", "莆田", "南平", "龙岩", "宁德", "平潭")

    def _city_from_name(self, name: str) -> str | None:
        """从名称里判断所属地级市。

        地级市名直接命中优先；否则找县名 —— 必须取**最靠左**的那个，
        不能按字典顺序。反例：「上杭古田旅游度假区」里的「上杭」属于龙岩，
        「古田」是宁德的地名，但这里指的是上杭县古田镇，按字典顺序会判成宁德。
        """
        for c in self.CITY_LIST:
            if c in name:
                return c
        best: tuple[int, str] | None = None
        for county, city in self.COUNTY_CITY.items():
            pos = name.find(county)
            if pos < 0:
                continue
            if best is None or pos < best[0]:
                best = (pos, city)
        return best[1] if best else None

    SPOT_SUFFIX = ("风景区", "风景名胜区", "景区", "旅游区", "古镇", "古城", "公园", "山", "岛",
                   "寺", "庙", "土楼", "石窟", "博物馆", "纪念馆", "度假区", "保护区", "温泉",
                   "峡谷")
    # 单景区抽取只认「强后缀」。裸「山」「岛」「寺」这些太泛：正文里
    # 「青山」「小山」「金山」都会被匹配成景区名，实测抽出的 254 条里
    # 大半是这种噪声（《青山、林小山、号大院印江山…）。
    STRONG_SUFFIX = ("风景区", "风景名胜区", "景区", "旅游区", "度假区", "古镇", "古城",
                     "博物馆", "纪念馆", "土楼", "石窟")
    SPOT_RE = re.compile(r"([\u4e00-\u9fa5]{2,10}?(?:" + "|".join(STRONG_SUFFIX) + "))")
    # 以这些字开头的匹配几乎都是句子成分而非景区名（「级旅游景区」「的景区」）
    NAME_BAD_PREFIX = ("级", "的", "和", "与", "及", "等", "各", "该", "上述", "相关",
                       "全市", "全省", "全国", "本", "此", "对", "在", "为", "是")
    NAME_BAD_WORDS = {"旅游景区", "级旅游景区", "A级旅游景区", "旅游度假区", "省级旅游度假区",
                      "国家级旅游度假区", "全域旅游", "红色旅游", "智慧景区", "景区"}

    def _detect_spot_names(self, text: str, title: str,
                           title_only: bool = False) -> list[str]:
        """抽景区名。title_only=True 时只认标题点名的景区。

        单篇公告走 title_only：一篇讲某景区的公告，标题里一定有它。
        正文反复出现的名字不采信——实测《邮轮旅游产业发展规划》正文提到
        崇武古城、南靖土楼、永定土楼，若采信，三个景区会共享同一段
        「位于海上丝绸之路核心区」的介绍，那是编造出来的关联。

        正文出现次数门槛是 2：只在正文出现一次的十有八九是引用别处的例子。
        宁少不滥——抽错的景区名会连带抽错的字段，比少抽一条更糟。
        """
        names: list[str] = []
        for m in self.SPOT_RE.finditer(title):
            names.append(m.group(1))
        if not title_only:
            counts: dict[str, int] = defaultdict(int)
            for m in self.SPOT_RE.finditer(text):
                counts[m.group(1)] += 1
            for name, c in counts.items():
                if c >= 2 and name not in names:
                    names.append(name)
        out: list[str] = []
        for n in dict.fromkeys(names):
            n = self._trim_spot_name(n)
            # 统一用 _looks_like_spot_name 守门：既挡通用词，也挡句子碎片
            # （实测反例：推动景区、游客对旅游景区、中的景区、利用公共资源建设的景区）。
            if n in self.NAME_BAD_WORDS or n.startswith(self.NAME_BAD_PREFIX):
                continue
            if not self._looks_like_spot_name(n):
                continue
            out.append(n)
        return out[:3]

    def _detect_city(self, text: str) -> str | None:
        for c in ["福州", "厦门", "漳州", "泉州", "三明", "莆田", "南平", "龙岩", "宁德", "平潭"]:
            if c in text:
                return c
        return None

    def _detect_level(self, text: str) -> str | None:
        m = re.search(r"([1-5])\s*A\s*级", text)
        if m:
            return f"{m.group(1)}A"
        # 兼容 AAAAA / AAAA 连写形态（部分景区名录直接写字母）
        for letters, level in (("AAAAA", "5A"), ("AAAA", "4A"),
                              ("AAA", "3A"), ("AA", "2A")):
            if letters in text:
                return level
        return None

    def _extract_field(self, text: str, spec: dict,
                       spot_name: str | None = None) -> tuple[str | None, str | None]:
        """按配置的关键词定位句子，再用正则抽值；返回 (值, 原文摘录)。

        `spot_name` 是硬约束：命中的那一句里必须真的出现这个景区名。
        没有这一层，政策法规类公文抽出来的会是别人的地址——实测省文旅厅
        的采购公告里「地址:福州市鼓楼区东水路76号」是代理机构的地址，
        不是任何一个景区的地址；把它挂到景区名下就是编造。
        宁可不抽，也不制造一条查不到出处的字段。
        """
        keywords = spec.get("keywords") or []
        regex = spec.get("regex")

        for kw in keywords:
            for m in re.finditer(re.escape(kw), text):
                start = max(0, m.start() - 40)
                end = min(len(text), m.end() + 160)
                window = text[start:end].replace("\n", " ")
                sentence = self._sentence_around(window, kw)
                if spot_name and spot_name not in sentence:
                    continue
                if regex:
                    vm = re.search(regex, window)
                    if vm:
                        val = vm.group(0).strip()
                        return val, truncate(sentence, 200)
                else:
                    # 无正则时取「关键词后到本句末」的一段作为值（如预约方式、地址），
                    # 必须按句界定界，否则会把后续段落一起吞进字段值。
                    after = window[m.end() - start:]
                    # 关键词本身可能是更长标签的前半截（如「预约」命中「预约方式：」），
                    # 要把残留的标签尾巴和冒号一并剥掉，否则值会带上「方式:」前缀。
                    after = re.sub(r"^\s*(?:方式|时间|电话|热线|标准|信息|说明|要求)?\s*[：:]\s*", "", after)
                    val = truncate(self._cut_sentence(after), 80)
                    if val:
                        return val, truncate(sentence, 200)
        return None, None

    @staticmethod
    def _cut_sentence(text: str) -> str:
        """截到最近的句末标点（不含标点），找不到就返回原文。"""
        for i, ch in enumerate(text):
            if ch in "。；！？\n":
                return text[:i].strip()
        return text.strip()

    @staticmethod
    def _sentence_around(window: str, kw: str) -> str:
        idx = window.find(kw)
        if idx < 0:
            return window[:200]
        left = max(window.rfind("。", 0, idx), window.rfind("；", 0, idx)) + 1
        right_candidates = [p for p in (window.find("。", idx), window.find("；", idx)) if p > 0]
        right = min(right_candidates) + 1 if right_candidates else len(window)
        return window[left:right].strip()

    @staticmethod
    def _judge_status(*, quote: str | None, source_url: str, publish_date: str | None,
                      eff_from: str | None, eff_to: str | None) -> str:
        """三态判定：缺任何溯源要素 → 待核验；超出有效期 → 待核验；否则已核验。"""
        if not quote or not source_url or not publish_date:
            return "待核验"
        if not in_effective_range(eff_from, eff_to):
            return "待核验"
        return "已核验"

    # ================================================================== 关联
    def _link_spots_to_docs(self) -> None:
        """景区名出现在政策正文里 → 建关联；标题命中权重更高。"""
        spots = self.db.conn.execute("SELECT spot_id, name, alias FROM scenic_spots").fetchall()
        docs = self.db.documents_for_export()
        for spot in spots:
            names = [spot["name"]] + [a for a in (spot["alias"] or "").split(",") if a]
            for row in docs:
                text = row["content_text"] or ""
                title = row["title"] or ""
                hit_title = any(n and n in title for n in names)
                hit_body = any(n and n in text for n in names)
                if not (hit_title or hit_body):
                    continue
                relation = "applies_to" if hit_title else "mentions"
                conf = 0.9 if hit_title else 0.6
                ev = ""
                for n in names:
                    if n and n in text:
                        i = text.find(n)
                        ev = text[max(0, i - 30): i + 90].replace("\n", " ")
                        break
                self.db.link_spot_policy(spot["spot_id"], row["doc_id"], relation, conf, ev)

    # ================================================================== 切片
    def chunk_text(self, text: str, *, doc_id: str, doc_title: str,
                   publish_date: str | None) -> list[dict]:
        """中文长文切片：先按标题/段落切语义块，再按字符上限细切。

        参数选择的理由（chunk=480 / overlap=80）：
        - 中文一个 token ≈ 1.3-1.6 字；480 字 ≈ 300-380 token，
          对 bge-small-zh（512 token 上限）留足余量，避免截断丢信息。
        - overlap 80 字 ≈ 上一块末尾两句话，保证跨块指代不断裂
          （政策文常见「上述景区」这类跨段指代）。
        - 更小的块（如 256）会让「票价+有效期+例外情形」被切散，反而降低召回。
        """
        size = self.s.chunking.chunk_size
        overlap = self.s.chunking.overlap
        min_chars = self.s.chunking.min_chunk_chars

        # 1) 先切语义块：按空行 + 小标题
        paras = [p.strip() for p in re.split(r"\n{2,}", text) if p.strip()]
        blocks: list[tuple[str, str]] = []   # (heading_path, 文本)
        current_heading = ""
        buf = ""
        for p in paras:
            if re.match(r"^([一二三四五六七八九十]+[、.]|\d+[、.]|第[一二三四五六七八九十]+[章节])", p) and len(p) < 40:
                if buf:
                    blocks.append((current_heading, buf.strip()))
                current_heading = p if not current_heading else f"{current_heading} > {p}"
                buf = p + "\n"
                continue
            buf += p + "\n"
            if len(buf) >= size:
                blocks.append((current_heading, buf.strip()))
                buf = ""
        if buf.strip():
            blocks.append((current_heading, buf.strip()))

        # 2) 超长块再按句切
        chunks: list[dict] = []
        idx = 0
        for heading, block in blocks:
            for piece in self._split_long(block, size, overlap):
                if len(piece) < min_chars and idx > 0:
                    # 太短就并到上一块，避免出现「一句话一块」的碎片
                    chunks[-1]["text"] += "\n" + piece
                    chunks[-1]["char_len"] = len(chunks[-1]["text"])
                    continue
                chunks.append({
                    "chunk_id": f"chunk_{doc_id}_{idx:04d}",
                    "chunk_index": idx,
                    "text": piece,
                    "heading_path": heading or None,
                    "token_estimate": int(len(piece) / 1.4),
                })
                idx += 1
        return chunks

    @staticmethod
    def _split_long(block: str, size: int, overlap: int) -> list[str]:
        if len(block) <= size:
            return [block]
        sentences = re.split(r"(?<=[。！？；\n])", block)
        out: list[str] = []
        cur = ""
        for s in sentences:
            if not s:
                continue
            if len(cur) + len(s) <= size:
                cur += s
                continue
            if cur:
                out.append(cur.strip())
                tail = cur[-overlap:] if overlap < len(cur) else cur
                cur = tail + s
            else:
                # 单句超长（表格/长列表），硬切
                for i in range(0, len(s), size - overlap):
                    out.append(s[i: i + size].strip())
                cur = ""
        if cur.strip():
            out.append(cur.strip())
        return [o for o in out if o]

    # ================================================================== 到期降级
    def _downgrade_expired(self) -> int:
        """effective_to 已过 → 待核验。这是「数据会自己变旧」的兜底。"""
        rows = self.db.conn.execute(
            "SELECT prov_id, effective_to FROM field_provenance WHERE status='已核验'"
        ).fetchall()
        n = 0
        t = today()
        with self.db.tx() as c:
            for r in rows:
                if r["effective_to"] and r["effective_to"] < t:
                    c.execute("UPDATE field_provenance SET status='待核验' WHERE prov_id=?",
                              (r["prov_id"],))
                    c.execute(
                        "INSERT INTO review_queue(prov_id, reason, severity, created_at) "
                        "VALUES (?,?,?,?)",
                        (r["prov_id"], "expired", "medium", today()),
                    )
                    n += 1
        if n:
            self.log.info("%d 条字段因超出有效期降级为「待核验」", n)
        return n
