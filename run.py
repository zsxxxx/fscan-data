#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""开发服务器入口：python run.py"""

from __future__ import annotations

import os

import uvicorn

# 代理配置：出口 IP 被平台限流时，设置 SEARCH_PROXY 更换出口。
# 留空则直连。可用环境变量覆盖：set SEARCH_PROXY=http://127.0.0.1:65202
_DEFAULT_PROXY = "http://127.0.0.1:65202"
if not os.getenv("SEARCH_PROXY"):
    os.environ["SEARCH_PROXY"] = _DEFAULT_PROXY

from app.main import app

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8765, log_level="info")
