"""服务配置：通过环境变量覆盖，均有合理默认值。"""
from __future__ import annotations

import os

#: 原文最大字符数，超出返回 422
MAX_TEXT_LENGTH: int = int(os.environ.get("MAX_TEXT_LENGTH", "1000000"))
#: 单次请求最大规则数，超出返回 422
MAX_RULES: int = int(os.environ.get("MAX_RULES", "500"))
#: 容器内监听端口（compose 默认映射到宿主机 8000）
PORT: int = int(os.environ.get("PORT", "8000"))
