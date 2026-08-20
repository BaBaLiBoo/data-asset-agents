"""Uvicorn 入口：uvicorn supervisor_main:app --reload。"""

from asset_supervisor.api import create_app

# 启动文件只负责创建应用，具体依赖装配由 asset_supervisor 包完成。
app = create_app()
