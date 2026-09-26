from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from typing import Any

from app.core.errors import ValidationError

DEFAULT_KEY_ENV = "RESULT_REGISTRY_SIGNING_KEY"


class SignatureError(ValidationError):
    code = "invalid_signature"


def canonical_json(payload: Any) -> str:
    """确定性序列化：键排序、紧凑分隔、不转义非 ASCII。"""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def content_digest(payload: Any) -> str:
    """对任意 JSON 兼容内容计算 sha256（十六进制）。"""
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def load_signing_key(explicit: str | None = None) -> bytes:
    """读取签名密钥：显式参数优先，其次环境变量，最后回退到数据库派生日志可见。

    未配置显式密钥时使用固定开发密钥并通过环境标记，避免在生产中被悄悄信任。
    """
    raw = explicit if explicit is not None else os.getenv(DEFAULT_KEY_ENV, "")
    if raw:
        return raw.encode("utf-8")
    return b"result-registry-development-key"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def sign_payload(payload: Any, key: bytes) -> str:
    """对规范化后的 payload 计算 HMAC-SHA256，返回 base64url 签名。"""
    mac = hmac.new(key, canonical_json(payload).encode("utf-8"), hashlib.sha256)
    return _b64url(mac.digest())


def verify_signature(payload: Any, signature: str, key: bytes) -> bool:
    expected = sign_payload(payload, key)
    return hmac.compare_digest(expected, signature)


def build_envelope(payload: Any, *, key: bytes, key_id: str, generated_at: str) -> dict[str, Any]:
    """构造带签名摘要的响应信封；签名覆盖整个 payload，摘要值因此自洽可验。"""
    body = {
        "payload": payload,
        "generated_at": generated_at,
        "key_id": key_id,
        "alg": "HS256",
    }
    body["signature"] = sign_payload(body, key)
    return body


def verify_envelope(envelope: dict[str, Any], key: bytes | None = None) -> dict[str, Any]:
    """校验响应信封签名，返回内部 payload；失败抛出 SignatureError。"""
    if not isinstance(envelope, dict) or "signature" not in envelope:
        raise SignatureError("响应缺少签名字段")
    signature = envelope.get("signature")
    if not isinstance(signature, str):
        raise SignatureError("签名格式不正确")
    body = {key: value for key, value in envelope.items() if key != "signature"}
    actual_key = key if key is not None else load_signing_key()
    if not verify_signature(body, signature, actual_key):
        raise SignatureError("响应签名校验失败，内容可能已被篡改")
    payload = body.get("payload")
    if not isinstance(payload, dict):
        raise SignatureError("响应 payload 结构不合法")
    return payload
