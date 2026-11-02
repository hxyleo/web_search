# 本地 Web Search

用 Docker Compose 启动 FastAPI、SearXNG、Crawl4AI 和抓取专用 DNS 转发服务，提供一个可由 AI Agent 调用的搜索接口。当前不需要大模型；可以以后再接入内网模型。

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

支持 `query`、`max_results`（1–10）、`include_raw_content`（布尔值或 `"markdown"`）、`language`、`time_range`（`day`、`month`、`year`）以及 `include_domains`、`exclude_domains`。`search_depth` 仅支持 `basic`，`topic` 仅支持 `general`。语言和时间筛选的效果取决于上游引擎；域名筛选在已返回的候选结果中进行，因此可能少于 `max_results`，并不保证搜索到目标域名的全部网页。

返回格式参考 Tavily Search 的常用字段，但并不支持 Tavily 所有参数或接口，也不会生成答案或模型相关性分数。未支持的参数或功能会返回 422。`content` 是搜索摘要，`raw_content` 是按需抓取的 Markdown；抓取失败时 `raw_content` 为 `null`，并在该条结果中给出 `crawl_error`。正文超过 200,000 字符时会截断并标注 `raw_content_truncated: true`。部分上游搜索引擎不可用时，响应可能包含 `warning`。

搜索候选与成功提取的正文分别在进程内缓存 10 分钟，重启 API 后清空；上游搜索有错误或网页抓取失败时，对应失败结果不会被缓存。相同请求会合并，搜索和抓取分别有并发、队列和超时限制。同一主机的顶层网页抓取启动间隔至少 0.5 秒；此限制不覆盖网页内的子资源或重定向。一个搜索请求的正文提取总等待时间上限为 25 秒，超时时会保留已完成的结果和其他网页的摘要。

搜索结果会先按 URL 做保守去重，再在相近排名中适度分散来源；这不是模型重排序。需要更深入的语义相关性排序时，可以在后续接入 reranker。

## 使用 Tavily Python SDK

已用 `tavily-python==0.8.5` 测试 Search 接口的常用参数；其他 SDK 版本和 Tavily 的其他接口尚未验证。安装 SDK 后可以这样调用：

```python
from tavily import TavilyClient

client = TavilyClient(api_key="local-placeholder", api_base_url="http://127.0.0.1:8000")
result = client.search(
    "FastAPI dependency injection documentation",
    search_depth="basic",
    max_results=2,
    include_domains=["fastapi.tiangolo.com"],
    include_raw_content="markdown",
)
print(result)
```

这里的 `api_key` 只是传给 SDK 的占位值，本地服务不会验证它，不是安全认证；不要将服务直接开放到公网。

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

Clash Verge 或 Mihomo 的 Fake-IP 模式可能将公共域名解析为 `198.18.x.x`，被 Crawl4AI 的 SSRF 防护拦截。项目为 Crawl4AI 配置了独立的 DNS-over-HTTPS 转发服务；它使用 TLS 验证访问 Cloudflare（`1.1.1.1`）和 Google（`8.8.8.8`）的 DNS 服务，抓取网址的域名会发送给其中一个服务，故障回退时可能发送给两者。它不修改宿主机的代理设置，也不关闭 Crawl4AI 的地址校验和连接时 IP 绑定。转发服务不会把域名写入自身日志。公网网页的抓取和本机、Docker 私网及云元数据地址的直接拦截已在当前环境验证。

DNS 服务默认使用 Docker 子网 `172.30.253.0/24`、地址 `172.30.253.53`。如果和已有网络冲突，可在项目目录的 `.env` 文件中同时设置 `CRAWLER_DNS_SUBNET` 与 `CRAWLER_DNS_IP`，确保后者属于前者，然后重启服务。该文件已被 Git 忽略。DNS 服务不向宿主机映射端口；其容器健康检查只验证本地 DNS 端口正在监听，不检查公网 DNS 是否可用。如果无法连接上述公共 DNS 服务，网页抓取可能失败。

## 后续

内网大模型尚未接入。后续可让 Agent 先调用此搜索接口，再由模型根据返回的来源和正文生成答案。
