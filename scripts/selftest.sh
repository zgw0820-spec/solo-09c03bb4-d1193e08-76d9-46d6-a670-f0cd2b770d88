#!/usr/bin/env bash
# 在 docker compose 环境中运行同一套端到端自测，并验证：
# 解析器重启后缓存清空；退役保留期（SQLite 卷）在全部服务重启后不提前放行。
set -euo pipefail

cd "$(dirname "$0")/.."

docker compose up -d --build

# 等网页就绪
for i in $(seq 1 60); do
  if docker compose exec -T web python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/api/state',timeout=2)" 2>/dev/null; then
    break
  fi
  sleep 1
done

docker compose exec -T \
  -e WEB_BASE=http://127.0.0.1:8000 \
  -e RESOLVER_UDP_HOST=resolver -e RESOLVER_UDP_PORT=53 \
  -e RESOLVER_STATS_HOST=resolver -e RESOLVER_STATS_PORT=8081 \
  web python -m scripts.selftest

echo ">> 重启全部容器（缓存应清空；保留期在共享卷上，不得提前放行）"
docker compose restart authoritative resolver web

# 再次等网页就绪
for i in $(seq 1 60); do
  if docker compose exec -T web python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/api/state',timeout=2)" 2>/dev/null; then
    break
  fi
  sleep 1
done

docker compose exec -T \
  -e WEB_BASE=http://127.0.0.1:8000 \
  -e RESOLVER_UDP_HOST=resolver -e RESOLVER_UDP_PORT=53 \
  -e RESOLVER_STATS_HOST=resolver -e RESOLVER_STATS_PORT=8081 \
  web python -m scripts.selftest --restart-check

echo ">> 全部自测通过"
