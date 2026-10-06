#!/bin/sh
# 容器入口：初始化数据库（SEED_DEMO=1 时写入演示数据），随后启动 gunicorn。
set -e

echo ">> 初始化数据库: ${DATABASE_PATH:-/data/archaeo.db}"
flask --app app init-db

echo ">> 启动 Web 服务（0.0.0.0:8000，workers=${GUNICORN_WORKERS:-2}）"
exec gunicorn --bind 0.0.0.0:8000 \
    --workers "${GUNICORN_WORKERS:-2}" \
    --access-logfile - --error-logfile - \
    "app:create_app()"
