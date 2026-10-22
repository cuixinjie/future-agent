# 本地 SearXNG

这是 M2 的可选搜索服务，使用官方 GHCR 镜像，固定到实际验收的镜像摘要。
只发布 `127.0.0.1:8888`，启用 HTML 和 JSON，不需要模型 API 密钥。
本机私有实例关闭 limiter，不需要额外部署 Valkey。
参考：[官方容器部署](https://docs.searxng.org/admin/installation-docker.html)。

先启动 Docker Desktop 的 Linux 引擎，在仓库根目录运行：

```powershell
./deploy/searxng/start.ps1
```

脚本只在配置不存在时生成随机密钥并写入已忽略的
`data/searxng/config/settings.yml`，保留已有配置。缓存使用 Docker 命名卷。
在项目根目录 `.env` 中设置：

```dotenv
SEARCH_BASE_URL=http://127.0.0.1:8888
```

网页：[本地搜索](http://127.0.0.1:8888/)。JSON 接口验证：

```powershell
Invoke-RestMethod 'http://127.0.0.1:8888/search?q=Python&format=json'
docker compose -f deploy/searxng/compose.yaml ps
docker compose -f deploy/searxng/compose.yaml logs --tail 50
docker compose -f deploy/searxng/compose.yaml stop
docker compose -f deploy/searxng/compose.yaml up -d
```

`restart: unless-stopped` 在 Docker 引擎重新启动后恢复未手动停止的服务。
关闭 Docker Desktop 时搜索不可用。`down` 保留命名卷；不要使用 `down -v`
删除缓存，除非确实需要清除数据。

2026-10-21 实测：JSON 请求 HTTP 200，`Python stable release official`
返回 33 条真实结果，`google cse` 超时仍在 `unresponsive_engines` 保留。
随后通过项目 `SearXNGProvider` 的实际兼容结果见本次验收产物。
搜索成功不代表网页抓取成功；宿主 DNS 返回非公网地址时，正文抓取仍会被安全规则拒绝。
