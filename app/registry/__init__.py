"""科学结果登记簿：不可变版本、质量标签、人工复核与发布状态。"""

from app.registry.service import ResultRegistryService, ensure_schema
from app.registry.signing import verify_envelope

__all__ = ["ResultRegistryService", "ensure_schema", "verify_envelope"]
