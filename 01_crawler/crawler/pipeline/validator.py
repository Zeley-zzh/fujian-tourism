"""字段校验器：把「脏数据」变成「可展示数据」的最后一道关。

校验分三层：
1. 结构校验 —— 值能不能被解析（时间格式、票价是数字、电话是号码）
2. 语义校验 —— 值合不合理（票价 0~2000、开放时间 00:00-24:00、电话 11 位手机或 7-12 位固话）
3. 溯源校验 —— 有没有 quote、有没有 publish_date、是否过期

任一层不过 → status 降为「待核验」+ 写 review_queue。
注意：校验不过不等于丢弃。永不删除证据，只标记，页面照常展示但带状态徽标。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..config import Settings
from ..db import Database
from ..utils import get_logger, in_effective_range, today

PHONE_MOBILE = re.compile(r"^1[3-9]\d{9}$")
PHONE_LAND = re.compile(r"^0\d{2,3}-?\d{7,8}(?:-\d{1,6})?$")
TIME_RANGE = re.compile(r"(\d{1,2})[:：](\d{2})\s*[—\-~至到]\s*(\d{1,2})[:：](\d{2})")
PRICE = re.compile(r"(\d{1,4}(?:\.\d{1,2})?)\s*元")


@dataclass
class Issue:
    prov_id: int
    severity: str
    reason: str
    detail: str


@dataclass
class ValidateReport:
    checked: int = 0
    passed: int = 0
    downgraded: int = 0
    issues: list[Issue] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "checked": self.checked,
            "passed": self.passed,
            "downgraded": self.downgraded,
            "issues": [
                {"prov_id": i.prov_id, "severity": i.severity,
                 "reason": i.reason, "detail": i.detail}
                for i in self.issues
            ],
        }


class FieldValidator:
    def __init__(self, settings: Settings, db: Database):
        self.s = settings
        self.db = db
        self.log = get_logger("validator", settings.p("log_dir"))

    def validate_all(self) -> ValidateReport:
        rep = ValidateReport()
        rows = self.db.conn.execute(
            "SELECT * FROM field_provenance WHERE is_current=1"
        ).fetchall()

        for r in rows:
            rep.checked += 1
            problems = self._check(r)
            if not problems:
                rep.passed += 1
                continue

            rep.downgraded += 1
            worst = max((p[0] for p in problems), key=lambda s: {"low": 0, "medium": 1, "high": 2}[s])
            reasons = "; ".join(f"[{p[0]}] {p[1]}" for p in problems)
            with self.db.tx() as c:
                if r["status"] == "已核验":
                    c.execute("UPDATE field_provenance SET status='待核验' WHERE prov_id=?",
                              (r["prov_id"],))
                c.execute(
                    "INSERT INTO review_queue(prov_id, doc_id, reason, severity, created_at) "
                    "VALUES (?,?,?,?,?)",
                    (r["prov_id"], r["source_doc_id"], "low_confidence", worst, today()),
                )
            rep.issues.append(Issue(r["prov_id"], worst, "low_confidence", reasons))

        self.log.info("字段校验: 检查 %d 条，通过 %d，降级 %d",
                      rep.checked, rep.passed, rep.downgraded)
        return rep

    # ------------------------------------------------------------------ 单条校验
    def _check(self, row) -> list[tuple[str, str]]:
        """返回 [(severity, 原因)]，空列表表示通过。"""
        out: list[tuple[str, str]] = []
        field = row["field_name"]
        value = (row["field_value"] or "").strip()

        # --- 溯源完整性 ---
        if not row["quote"]:
            out.append(("high", "缺少原文摘录 quote"))
        elif len(row["quote"]) > 200:
            out.append(("low", "quote 超过 200 字"))
        if not row["source_url"]:
            out.append(("high", "缺少 source_url"))
        if not row["publish_date"]:
            out.append(("medium", "缺少发布日期，无法判断时效"))
        if not in_effective_range(row["effective_from"], row["effective_to"]):
            out.append(("high", "超出有效期限"))

        # --- 按字段的语义校验 ---
        if field == "opening_hours":
            m = TIME_RANGE.search(value)
            if not m:
                out.append(("medium", f"无法解析为时间区间: {value[:30]}"))
            else:
                h1, m1, h2, m2 = (int(x) for x in m.groups())
                if not (0 <= h1 <= 24 and 0 <= h2 <= 24 and 0 <= m1 < 60 and 0 <= m2 < 60):
                    out.append(("high", "时间数值越界"))
                elif h1 == h2 and m1 == m2:
                    out.append(("high", "开始与结束时间相同"))

        elif field == "ticket_price":
            m = PRICE.search(value)
            if not m:
                out.append(("medium", f"无法解析为价格: {value[:30]}"))
            else:
                p = float(m.group(1))
                if p > 2000:
                    out.append(("high", f"票价 {p} 元超出常识范围（可能抽取到了别的数字）"))
                if p == 0:
                    out.append(("low", "价格为 0，需确认是否免费开放"))

        elif field == "phone":
            digits = re.sub(r"[^\d\-]", "", value)
            if not (PHONE_MOBILE.match(digits.replace("-", "")) or PHONE_LAND.match(digits)):
                out.append(("medium", f"电话格式不合法: {value[:20]}"))

        elif field == "address":
            if len(value) < 6:
                out.append(("medium", "地址过短"))
            if not re.search(r"[省市区县路街道号镇乡村]", value):
                out.append(("medium", "地址缺少行政区划或道路特征"))

        elif field == "booking_method":
            if len(value) < 2:
                out.append(("low", "预约方式过短"))
            if "微信" not in value and "小程" not in value and "网站" not in value \
                    and "电话" not in value and "平台" not in value and "实名" not in value:
                out.append(("low", "未识别出常见预约渠道关键词"))

        elif field == "policy_validity":
            if not re.search(r"\d{4}", value):
                out.append(("medium", "有效期描述中未包含年份"))

        return out
