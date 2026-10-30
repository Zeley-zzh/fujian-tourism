"""运行时配置：模型凭据、向量后端，存在本地 JSON 文件里，重启不丢。

为什么要这一层
--------------
需求是「后台界面配一个模型 Key，访客界面就能直接问」。所以 Key 不能只在环境
变量里 —— 环境变量要重启进程才生效，而配置页必须当场生效。这里把配置落到
`data/config.json`，UI 保存后由 `LLMClient.reload()` 立刻拿到新值。

安全取舍
--------
Key 落盘是明文 JSON。这是单机内网部署的取舍：不引入 KMS、不做加密，但
① 文件权限收到 600（POSIX）/ 依赖 NTFS ACL（Windows）；
② `public_view()` 只吐掩码，绝不把完整 Key 回传给页面或 API；
③ 文件写在 `data/` 下，`.gitignore` 已排除，不会被提交。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

RAG_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = RAG_ROOT / "data" / "config.json"
CONFIG_ENV = "RAG_CONFIG_PATH"

DEFAULTS: dict[str, Any] = {
    # 模型接入
    "provider": "deepseek",
    "base_url": "https://api.deepseek.com/v1",
    "model": "deepseek-chat",
    "api_key": "",
    "temperature": 0.2,
    "max_tokens": 900,
    # 向量后端：st = 真实语义模型；hash = 内置字符 n-gram（无外网也能跑）
    "embedding_backend": "st",
    "embedding_model": "BAAI/bge-small-zh-v1.5",
}

# 常见服务商的默认地址，配置页用它做下拉预填
PROVIDERS: dict[str, dict[str, str]] = {
    "deepseek": {"label": "DeepSeek", "base_url": "https://api.deepseek.com/v1",
                 "model": "deepseek-chat"},
    "openai": {"label": "OpenAI", "base_url": "https://api.openai.com/v1",
               "model": "gpt-4o-mini"},
    "dashscope": {"label": "阿里云百炼 / 通义",
                  "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                  "model": "qwen-plus"},
    "moonshot": {"label": "月之暗面 Kimi", "base_url": "https://api.moonshot.cn/v1",
                 "model": "moonshot-v1-8k"},
    "zhipu": {"label": "智谱 GLM", "base_url": "https://open.bigmodel.cn/api/paas/v4",
              "model": "glm-4-flash"},
    "custom": {"label": "自定义（OpenAI 兼容）", "base_url": "", "model": ""},
}


def config_path() -> Path:
    return Path(os.environ.get(CONFIG_ENV) or CONFIG_PATH)


def load() -> dict[str, Any]:
    """读配置；文件不存在或损坏都回落到 DEFAULTS，不让配置错误拖垮整个应用。"""
    path = config_path()
    data = dict(DEFAULTS)
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                data.update({k: v for k, v in raw.items() if v is not None})
        except (json.JSONDecodeError, OSError):
            pass
    # 环境变量优先级最高，便于容器化部署时不把 Key 写进文件
    if os.environ.get("LLM_API_KEY"):
        data["api_key"] = os.environ["LLM_API_KEY"]
    if os.environ.get("OPENAI_API_KEY") and not data["api_key"]:
        data["api_key"] = os.environ["OPENAI_API_KEY"]
    if os.environ.get("LLM_BASE_URL"):
        data["base_url"] = os.environ["LLM_BASE_URL"]
    if os.environ.get("LLM_MODEL"):
        data["model"] = os.environ["LLM_MODEL"]
    if os.environ.get("EMBEDDING_BACKEND"):
        data["embedding_backend"] = os.environ["EMBEDDING_BACKEND"]
    if os.environ.get("EMBEDDING_MODEL"):
        data["embedding_model"] = os.environ["EMBEDDING_MODEL"]
    return data


def save(updates: dict[str, Any]) -> dict[str, Any]:
    """合并写入。传空字符串的 api_key 表示「不改」，避免配置页留空就把 Key 清掉。"""
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    current = load()

    for key, value in updates.items():
        if key not in DEFAULTS:
            continue
        if key == "api_key" and (value is None or str(value).strip() == ""):
            continue
        if isinstance(DEFAULTS[key], float):
            try:
                value = float(value)
            except (TypeError, ValueError):
                continue
        elif isinstance(DEFAULTS[key], int):
            try:
                value = int(value)
            except (TypeError, ValueError):
                continue
        else:
            value = str(value).strip()
        current[key] = value

    payload = {k: current[k] for k in DEFAULTS}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    _harden(path)
    return current


def _harden(path: Path) -> None:
    """尽量收紧文件权限。Windows 上 chmod 只影响只读位，真正的边界靠目录 ACL。"""
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def mask_key(key: str | None) -> str:
    """只露头尾，中间打码。空 Key 返回空串，前端据此显示「未配置」。"""
    if not key:
        return ""
    k = key.strip()
    if len(k) <= 8:
        return "•" * len(k)
    return f"{k[:4]}{'•' * 8}{k[-4:]}"


def public_view() -> dict[str, Any]:
    """给页面和 /api/status 用的安全视图：不含完整 Key。"""
    cfg = load()
    return {
        "provider": cfg["provider"],
        "base_url": cfg["base_url"],
        "model": cfg["model"],
        "temperature": cfg["temperature"],
        "max_tokens": cfg["max_tokens"],
        "embedding_backend": cfg["embedding_backend"],
        "embedding_model": cfg["embedding_model"],
        "api_key_set": bool(cfg["api_key"]),
        "api_key_mask": mask_key(cfg["api_key"]),
        "providers": PROVIDERS,
        "config_path": str(config_path()),
    }


def clear_key() -> None:
    path = config_path()
    cfg = load()
    cfg["api_key"] = ""
    if path.exists():
        payload = {k: cfg[k] for k in DEFAULTS}
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        _harden(path)
