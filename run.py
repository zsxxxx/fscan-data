#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""开发服务器入口：python run.py"""

from __future__ import annotations

import uvicorn

# 代理配置：出口 IP 被平台限流时，在启动命令前设置 SEARCH_PROXY 更换出口。
# 默认直连，不再硬编码固定代理端口（代理软件退出后端口失效会导致所有请求失败）。
# 例：SEARCH_PROXY=http://127.0.0.1:7890 python run.py

from app.main import app

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8765, log_level="info")
