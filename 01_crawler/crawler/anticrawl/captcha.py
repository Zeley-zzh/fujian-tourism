"""验证码识别：图形码 / 算术码 / 滑块 / 点选，四个入口。

依赖 ddddocr（离线，CPU 可跑，模型自带）。它不在时自动降级：
- manual 模式：把图片落盘，等人工回填 data/manual_captcha/<id>.txt
- remote_api 模式：POST 到打码平台（地址从配置读，不硬编码任何 key）

滑块缺口定位用纯 numpy 的 Sobel 边缘 + 列投影，不依赖 opencv，
因为演示机不一定装得上 opencv（wheel 大、需要编译环境）。
"""
from __future__ import annotations

import base64
import hashlib
import time
from dataclasses import dataclass
from pathlib import Path

try:
    import numpy as np
except ImportError:  # pragma: no cover
    np = None


@dataclass
class CaptchaResult:
    ok: bool
    text: str = ""
    offset_x: int = 0
    confidence: float = 0.0
    engine: str = "none"
    elapsed_ms: int = 0
    error: str = ""


class CaptchaSolver:
    def __init__(self, *, engine: str = "ddddocr", manual_dir: Path | None = None,
                 remote_api: str = "", poll_interval: float = 1.0, poll_timeout: float = 120.0):
        self.engine = engine
        self.manual_dir = manual_dir or Path("data/manual_captcha")
        self.remote_api = remote_api
        self.poll_interval = poll_interval
        self.poll_timeout = poll_timeout
        self._ocr = None
        self._ocr_det = None
        self.stats = {"attempts": 0, "success": 0, "fail": 0, "engine": engine}

    # ---------- 引擎懒加载 ----------
    def _load_ddddocr(self):
        if self._ocr is not None:
            return self._ocr
        try:
            import ddddocr  # type: ignore
        except ImportError:
            return None
        # show_ad=False 关掉启动广告，避免污染日志
        try:
            self._ocr = ddddocr.DdddOcr(show_ad=False)
        except TypeError:
            self._ocr = ddddocr.DdddOcr()
        return self._ocr

    def _load_det(self):
        if self._ocr_det is not None:
            return self._ocr_det
        try:
            import ddddocr  # type: ignore
        except ImportError:
            return None
        try:
            self._ocr_det = ddddocr.DdddOcr(det=True, show_ad=False)
        except TypeError:
            self._ocr_det = ddddocr.DdddOcr(det=True)
        return self._ocr_det

    # ---------- 1. 普通图形码 ----------
    def solve_image(self, img_bytes: bytes) -> CaptchaResult:
        t0 = time.time()
        self.stats["attempts"] += 1

        if self.engine == "ddddocr":
            ocr = self._load_ddddocr()
            if ocr is None:
                return self._fallback(img_bytes, "ddddocr 未安装", t0)
            try:
                text = ocr.classification(img_bytes)
                ok = bool(text and text.strip())
                self.stats["success" if ok else "fail"] += 1
                return CaptchaResult(ok=ok, text=text.strip(), engine="ddddocr",
                                     confidence=0.8 if ok else 0.0,
                                     elapsed_ms=int((time.time() - t0) * 1000))
            except Exception as e:  # noqa: BLE001
                return self._fallback(img_bytes, f"ddddocr 异常: {e}", t0)

        if self.engine == "remote_api":
            return self._solve_remote(img_bytes, t0)

        return self._fallback(img_bytes, f"engine={self.engine} 需人工", t0)

    # ---------- 2. 算术验证码 ----------
    def solve_arithmetic(self, img_bytes: bytes) -> CaptchaResult:
        """「3+5=?」这类：OCR 出算式，本地求值。"""
        r = self.solve_image(img_bytes)
        if not r.ok:
            return r
        expr = r.text.replace("×", "*").replace("÷", "/").replace("x", "*")
        expr = "".join(ch for ch in expr if ch in "0123456789+-*/=? ")
        expr = expr.split("=")[0].strip()
        try:
            import ast
            import operator as op
            ops = {ast.Add: op.add, ast.Sub: op.sub, ast.Mult: op.mul, ast.Div: op.truediv}
            node = ast.parse(expr, mode="eval").body

            def ev(n):
                if isinstance(n, ast.BinOp):
                    return ops[type(n.op)](ev(n.left), ev(n.right))
                if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)):
                    return n.value
                raise ValueError("unsupported")

            r.text = str(int(ev(node)))
            return r
        except Exception:  # noqa: BLE001
            r.ok = False
            r.error = "算式求值失败"
            return r

    # ---------- 3. 滑块缺口 ----------
    def solve_slider(self, bg_bytes: bytes, piece_bytes: bytes | None = None,
                     piece_width: int = 60) -> CaptchaResult:
        """返回缺口左边界 x 坐标（相对背景图）。

        做法：灰度 → Sobel 水平梯度 → 列累积 → 在 [0, W - piece_w] 内取峰值。
        比模板匹配稳，因为很多站会加噪点/干扰线，模板匹配直接崩。
        """
        t0 = time.time()
        if np is None:
            return CaptchaResult(ok=False, engine="slider", error="缺少 numpy",
                                 elapsed_ms=int((time.time() - t0) * 1000))
        try:
            arr = self._decode_gray(bg_bytes)
            if arr is None:
                raise ValueError("背景图解码失败")
            h, w = arr.shape
            gx = np.zeros((h, w), dtype=np.float32)
            # 手写 Sobel-x（避免 cv2 依赖）
            gx[:, 1:-1] = (arr[:, 2:] - arr[:, :-2]).astype(np.float32)
            col = np.abs(gx).sum(axis=0)
            # 平滑，抑制单列噪声
            if w > 9:
                kernel = np.ones(9, dtype=np.float32) / 9
                col = np.convolve(col, kernel, mode="same")
            lo = max(0, int(w * 0.05))
            hi = max(lo + 1, w - piece_width - 4)
            seg = col[lo:hi]
            if seg.size == 0:
                raise ValueError("有效搜索区间为空")
            x = int(lo + int(np.argmax(seg)))
            conf = float(seg.max() / (seg.mean() + 1e-6)) / 10.0
            return CaptchaResult(ok=True, offset_x=x, engine="slider-sobel",
                                 confidence=min(conf, 1.0),
                                 elapsed_ms=int((time.time() - t0) * 1000))
        except Exception as e:  # noqa: BLE001
            return CaptchaResult(ok=False, engine="slider", error=str(e),
                                 elapsed_ms=int((time.time() - t0) * 1000))

    # ---------- 4. 点选验证码 ----------
    def solve_click(self, img_bytes: bytes, expect_count: int = 3) -> CaptchaResult:
        """返回各目标框中心点，格式 "x1,y1;x2,y2;..."。"""
        t0 = time.time()
        det = self._load_det()
        if det is None:
            return CaptchaResult(ok=False, engine="click", error="ddddocr det 不可用",
                                 elapsed_ms=int((time.time() - t0) * 1000))
        try:
            boxes = det.detection(img_bytes)
        except Exception as e:  # noqa: BLE001
            return CaptchaResult(ok=False, engine="click", error=str(e),
                                 elapsed_ms=int((time.time() - t0) * 1000))
        pts = []
        for b in boxes[:expect_count]:
            x1, y1, x2, y2 = b[:4]
            pts.append(f"{int((x1 + x2) / 2)},{int((y1 + y2) / 2)}")
        return CaptchaResult(ok=len(pts) >= expect_count, text=";".join(pts),
                             engine="click-det", confidence=1.0 if len(pts) >= expect_count else 0.4,
                             elapsed_ms=int((time.time() - t0) * 1000))

    # ---------- 内部 ----------
    def _decode_gray(self, img_bytes: bytes):
        """纯 Python 解码 PNG/JPEG → 灰度 ndarray。用 Pillow（比 numpy 自带解码通用）。"""
        try:
            from io import BytesIO

            from PIL import Image
        except ImportError:
            return None
        img = Image.open(BytesIO(img_bytes)).convert("L")
        return np.array(img, dtype=np.uint8)

    def _fallback(self, img_bytes: bytes, reason: str, t0: float) -> CaptchaResult:
        """落盘等人工；返回 ok=False，由上层决定是否阻塞等待。"""
        self.manual_dir.mkdir(parents=True, exist_ok=True)
        h = hashlib.sha1(img_bytes).hexdigest()[:12]
        img_path = self.manual_dir / f"{h}.png"
        ans_path = self.manual_dir / f"{h}.txt"
        if not img_path.exists():
            img_path.write_bytes(img_bytes)
        if self.engine == "manual":
            waited = 0.0
            while waited < self.poll_timeout:
                if ans_path.exists():
                    text = ans_path.read_text(encoding="utf-8").strip()
                    self.stats["success"] += 1
                    return CaptchaResult(ok=True, text=text, engine="manual",
                                         confidence=1.0,
                                         elapsed_ms=int((time.time() - t0) * 1000))
                time.sleep(self.poll_interval)
                waited += self.poll_interval
            self.stats["fail"] += 1
            return CaptchaResult(ok=False, engine="manual", error="人工识别超时",
                                 elapsed_ms=int((time.time() - t0) * 1000))
        self.stats["fail"] += 1
        return CaptchaResult(ok=False, engine=self.engine,
                             error=f"{reason}；图片已存 {img_path}",
                             elapsed_ms=int((time.time() - t0) * 1000))

    def _solve_remote(self, img_bytes: bytes, t0: float) -> CaptchaResult:
        if not self.remote_api:
            return self._fallback(img_bytes, "未配置打码平台地址", t0)
        try:
            import httpx
            payload = {"image": base64.b64encode(img_bytes).decode()}
            r = httpx.post(self.remote_api, json=payload, timeout=30)
            data = r.json()
            text = str(data.get("text") or data.get("result") or "").strip()
            ok = bool(text)
            self.stats["success" if ok else "fail"] += 1
            return CaptchaResult(ok=ok, text=text, engine="remote_api",
                                 confidence=0.9 if ok else 0.0,
                                 elapsed_ms=int((time.time() - t0) * 1000))
        except Exception as e:  # noqa: BLE001
            return self._fallback(img_bytes, f"打码平台异常: {e}", t0)
