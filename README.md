# 本地 Web Search

用 Docker Compose 启动 FastAPI、SearXNG 和 Crawl4AI，提供一个可由 AI Agent 调用的搜索接口。第一版不需要大模型；可以以后再接入内网模型。

## 准备

- 已安装并启动 Docker 和 Docker Compose。
- 能够拉取项目所需的镜像，并能访问所使用的搜索引擎和目标网页。
- 启动脚本需要 `openssl`。

## 启动

在项目目录运行：

```bash
./start.sh
```

首次运行会生成 `.crawl4ai.env` 和 `.searxng.env`，并在本地缺少镜像时构建或拉取镜像，然后启动服务。之后启动默认使用已有镜像；如果修改了 API 代码或 Dockerfile，需要执行 `./start.sh --build` 重新构建。已有密钥文件会保留。密钥文件已被 `.gitignore` 和 `.dockerignore` 排除，不要上传或分享其中的内容。API 仅监听本机 `127.0.0.1:8000`；SearXNG 和 Crawl4AI 只在 Compose 网络中供 API 调用。

API 当前没有访问认证，仅适合本机可信使用；不要直接开放到公网。

检查服务：

```bash
docker compose ps
curl http://127.0.0.1:8000/health
```

`/health` 是 API 自身的存活检查；它正常并不代表搜索引擎或网页抓取一定可用。

## 搜索

当前配置启用了在此网络下测试可用的 360 搜索、Yahoo 和 Google CSE；搜索等待的上游时间上限为 8 秒。上游可用性和排序会变化；可按网络环境调整 `searxng/settings.yml` 中的引擎列表和超时，修改后运行 `docker compose restart searxng`。本次多主题评测的范围和结果见 `docs/engine-evaluation-2026-11-02.md`。

```bash
curl -X POST http://127.0.0.1:8000/search \
  -H 'Content-Type: application/json' \
  -d '{"query":"Python FastAPI 教程","max_results":5}'
```

需要网页正文时，将 `include_raw_content` 设为 `true`：

```bash
curl -X POST http://127.0.0.1:8000/search \
  -H 'Content-Type: application/json' \
  -d '{"query":"Python FastAPI 教程","max_results":3,"include_raw_content":true}'
```

支持的参数是 `query`、`max_results`（1–10）和 `include_raw_content`。返回格式参考 Tavily Search 的常用字段，但并不支持 Tavily 所有参数或接口。`content` 是搜索摘要，`raw_content` 是按需抓取的 Markdown；抓取失败时 `raw_content` 为 `null`，并在该条结果中给出 `crawl_error`。SearXNG 的部分上游搜索引擎不可用时，响应可能包含 `warning`。不支持的请求字段会返回 422，避免误以为已生效。

搜索结果会先按 URL 做保守去重，再在相近排名中适度分散来源；这不是模型重排序。需要更深入的语义相关性排序时，可以在后续接入 reranker。

## 管理

```bash
docker compose ps
```

```bash
docker compose logs --tail=100
```

```bash
docker compose down
```

停止后可用 `./start.sh` 再次启动。Compose 只管理本项目的容器，不会自动停止此前单独运行的 SearXNG 或 Crawl4AI 容器。

## 网页抓取与 DNS

Crawl4AI 保持 SSRF 安全防护。若使用 Clash Verge 或 Mihomo 的 Fake-IP 模式，公共域名可能解析为 `198.18.x.x`，被 Crawl4AI 当作非公网地址拒绝。可以检查代理的 DNS 配置，考虑使用 `redir-host` 模式或恰当配置 Fake-IP 过滤，再验证容器内域名是否解析为公网 IP、搜索和抓取是否可用。仅修改 Docker 的 DNS 地址在存在 DNS 劫持时可能无效。不要通过关闭 SSRF 防护来解决此问题。

## 后续

内网大模型尚未接入。后续可让 Agent 先调用此搜索接口，再由模型根据返回的来源和正文生成答案。
