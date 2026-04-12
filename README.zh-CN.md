# ProxyPool

[中文说明](README.zh-CN.md) | [English](README.md)

一个自托管的代理管理与路由服务，用一个稳定的本地入口统一接入和调度多条上游代理。

## 这个项目做什么
- 通过 Web Admin 管理上游代理，而不是手工维护运行时文件
- 对外提供一个主本地代理入口 `3128`，供浏览器、脚本和服务统一接入
- 基于用户名维持稳定会话，同时保留精确绑定单条代理和随机池路由能力
- 支持 `http`、`socks5`、`socks5h` 上游，以及多跳代理链
- 为不能发送代理认证信息的客户端提供固定兼容端口

## 适用场景
- 需要稳定出口身份的浏览器自动化任务
- 希望所有本地工具和服务都只连接一个代理地址的场景
- 希望通过 Web 界面统一管理代理池、粘性会话和兼容接入方式的团队

## 核心能力
- 基于用户名的粘性路由：同一个用户名会稳定落到同一条代理记录
- 主认证监听端口 `3128` 同时支持入站 `http`、`socks5` 和 `socks5h`
- 通过 `RANDOM_POOL_PREFIX` 支持随机池前缀覆盖
- 支持导入代理列表，导入结果按手动代理保存
- 导入前可选实时测活，并在确认后只保存有效条目
- 手动新增/编辑代理时支持 prepend-hop 链
- 批量生成同时支持直连 hop 和链式 hop
- 兼容端口可以绑定到精确 `entry_key`，也可以绑定稳定 `session_name`

## 快速开始
1. 复制环境变量模板并按需修改：
   - `cp .env.example .env`
2. 构建并启动：
   - `docker compose up --build`
3. 打开 Web Admin：
   - `http://localhost:8077`
4. 完成首次启动初始化：
   - 在 `/setup` 页面设置管理员密码
   - `AUTH_PASSWORD` 和 `SALT` 会在首次启动时自动生成
5. 在 Web Admin 中添加代理：
   - `Proxies` → `Import List`
   - 或 `Proxies` → `Add Proxy`
   - 或 `Proxies` → `Batch Generate`
6. 按需配置兼容端口：
   - `Compat Ports` → 将 `33100-33999` 中的某个端口映射到 `entry_key` 或 `session_name`

## 启动时配置
容器启动时只直接读取以下环境变量：
- `STATE_DB_PATH`
- `WEB_PORT`

其余运行时代理配置、管理员密码以及代理列表，都会持久化到 SQLite 状态文件中。

如果旧的 `.env` 或 `docker compose` 覆盖配置中仍然保留 `ADMIN_PASSWORD`，当前启动流程会忽略它。

## 运行时配置
以下配置项应在 Web Admin 的 `Config Center` 中修改：
- `PROXY_HOST`、`PROXY_PORT`
- `AUTH_PASSWORD`、`AUTH_REALM`
- `UPSTREAM_CONNECT_TIMEOUT`、`UPSTREAM_CONNECT_RETRIES`
- `RELAY_TIMEOUT`
- `REWRITE_LOOPBACK_TO_HOST` = auto | always | off
- `HOST_LOOPBACK_ADDRESS`
- `LOG_LEVEL`
- `SALT`
- `RANDOM_POOL_PREFIX`
- `ROUTER_DEBUG_LOG`

说明：
- `AUTH_PASSWORD` 是主认证代理监听端口使用的共享密码。
- `AUTH_REALM` 是客户端收到 `407 Proxy Authentication Required` 时看到的 HTTP Basic 认证域。它只影响客户端认证提示和凭据缓存范围，不影响路由，也不参与密码校验。

## 管理员初始化与重置
- 即使管理员密码尚未配置，Web Admin 也会启动。
- 首次启动时，`/setup` 是唯一允许的管理入口；完成后会创建管理员密码。
- 管理员密码保存在 SQLite 状态中，不再通过 `ADMIN_PASSWORD` 环境变量注入。
- 如果忘记管理员密码，可以清空后重新进入初始化模式：
  - 本地运行：`python3 scripts/reset_admin_password.py`
  - 指定数据库路径：`python3 scripts/reset_admin_password.py --state-db-path /path/to/proxypool.sqlite3`
  - Docker 示例：`docker compose exec squid python3 /opt/scripts/reset_admin_password.py`

## 使用方式
将客户端代理指向本地主入口 `3128`。这个入口需要认证密码：
- `HTTP_PROXY=http://userA:YOUR_PASSWORD@localhost:3128`
- `HTTPS_PROXY=http://userA:YOUR_PASSWORD@localhost:3128`
- `ALL_PROXY=socks5://userA:YOUR_PASSWORD@localhost:3128`
- `ALL_PROXY=socks5h://userA:YOUR_PASSWORD@localhost:3128`

在主认证端口上：
- 密码必须匹配当前 `AUTH_PASSWORD`
- 用户名就是路由键
- 如果用户名刚好等于某个 `entry_key`，请求会固定走该上游记录
- 如果用户名以前缀 `RANDOM_POOL_PREFIX` 开头，请求会进入随机池
- 否则用户名会通过 shared 路由哈希到固定上游，因此同一个用户名会稳定落到同一条记录
- `socks5h` 与 `socks5` 共用同一个监听器；只要客户端发送的是域名目标，目标域名解析就会保留在代理端完成

对于无法发送代理认证信息的客户端，可以改用兼容端口：
- 精确绑定示例：`http://127.0.0.1:33100`
- 稳定会话别名示例：将 `33101` 绑定到 `chrome-profile-a`，然后客户端使用 `http://127.0.0.1:33101`

兼容端口是由 Web Admin 管理的免认证 HTTP 监听器。

之所以使用固定 Docker 暴露端口范围，是因为容器启动后 Docker 无法再动态追加新的宿主机端口映射。

## 代理管理
运行时代理管理统一通过 Web Admin 的 `Proxies` 页面完成。
当前 Web Admin 支持：
- `Import List`：粘贴行式代理定义，可选先实时测活，再确认保存有效条目为手动代理
- `Add Proxy`：新增或编辑单条手动代理
- `Batch Generate`：基于同一主机和端口范围生成自动代理

## 兼容端口
通过 Web Admin 的 `Compat Ports` 页面管理预先暴露的 `127.0.0.1:33100-33999` 端口范围。

每条映射支持两种目标：
- `entry_key`：这个本地端口始终绑定到某一条精确上游记录
- `session_name`：把该值当作稳定会话别名，再通过 shared 路由哈希到某条上游记录

说明：
- 主认证代理 `3128` 不受兼容端口配置影响。
- 兼容监听器是为 `undetected-chromedriver` 这类工具准备的独立免认证端口。
- 兼容监听器仍然只支持 HTTP 入站；入站 SOCKS5 只在主认证监听器上启用。
- 如果某个 `entry_key` 映射对应的上游记录后来被删除，那么该兼容端口上的请求会失败，直到你更新映射。
- `session_name` 不是每条代理独占的唯一访问 key；真正的唯一直接访问标识是 `entry_key`。

### 公共 API 站点
现在提供一个公开的 `/api` 文档站，面向人工阅读和 AI agent 读文档后生成脚本：

- `GET /api`
  - HTML 文档页，适合人工浏览
- `GET /api.txt`
  - 纯文本版，适合 LLM / agent 抓取
- `GET /api.json`
  - 紧凑机器可读描述
- `GET /api/openapi.json`
  - OpenAPI 文档

真正的调用入口在 `/api/v1`：

- `GET /api/v1/health`
- `GET /api/v1/resolve?username=<name>`
- `GET /api/v1/resolve?entry_key=<key>`
- `GET /api/v1/resolve?listen_port=<port>`
- `GET /api/v1/entries`
  - 支持按标签过滤，例如 `tag_country=US`
- `GET /api/v1/compat/mappings`
- `POST /api/v1/compat/bind`
- `POST /api/v1/compat/allocate`
- `POST /api/v1/compat/unbind`

这组 API 默认不做鉴权，适合本地或内网自动化调用；同时，代理条目相关返回现在可能包含真实的上游用户名、密码以及完整代理 URI，所以 `/api` 只应暴露在本机或可信内网。

示例：
- `curl 'http://127.0.0.1:8077/api'`
- `curl 'http://127.0.0.1:8077/api/v1/resolve?username=browser-a'`
- `curl 'http://127.0.0.1:8077/api/v1/entries?tag_country=US'`
- `curl -H 'Content-Type: application/json' -d '{"username":"browser-a"}' http://127.0.0.1:8077/api/v1/compat/bind`
- `curl -X POST http://127.0.0.1:8077/api/v1/compat/allocate`

### 手动新增/编辑
单条代理记录支持以下字段：
- scheme
- host
- port
- username / password
- 可选 prepend-hop 代理链
- 是否加入随机池

### 批量生成
批量生成用于从同一主机和端口范围快速创建大量代理记录。

同时支持：
- 为每一条生成记录追加统一 prepend hop
- 在链式中继场景下轮换第一个 hop

### 支持的行格式
在导入代理列表或在批量生成中配置链式 hop 时，支持以下格式：
- `socks5://user:pass@host:port`
- `http://user:pass@host:port`
- `host:port`
- `host:port:user:pass`
- `scheme,host,port,user,pass`

如果是一条多跳链，请使用 ` | ` 连接各 hop，例如：
- `http://127.0.0.1:30001 | socks5://user:pass@dc.decodo.com:10001`

请保留 `|` 两侧的空格，这样密码中包含 `|` 时才不会被误切分。

## 验证
- 逻辑验证：`./scripts/verify.sh`
- 真实请求验证：`./scripts/verify_real.sh`
  - 使用 `https://ipinfo.io/json` 检查粘性行为和 IP 分布

## 工具脚本
- 生成大规模行式代理列表文件：
  - `python3 scripts/generate_upstream_list.py --host proxy.example.com --port-first 10001 --port-last 10100 --output config/upstreams.txt`
- 生成链式输出：
  - `python3 scripts/generate_upstream_list.py --host proxy.example.com --port-first 10001 --port-last 10100 --cycle-first-hop http://127.0.0.1:30001 --cycle-first-hop http://127.0.0.1:30002 --output config/upstreams.txt`
- 链路延迟基准：`./scripts/benchmark_chain_latency.sh`
  - 使用项目状态中的显式代理配置，对比直连配置与链式配置的延迟差异

## 说明
- `.env` 含有敏感凭据，应仅保留在本地。
- 在 `.env` 中请直接写入真实密码字符。如果你在 shell 命令里写过 `\\~`，通常说明真实密码字符其实是 `~`。
- 对于暂时性的上游连接或握手失败，代理会先重试数次，再返回 `502`。
- 在 Docker 中，默认只有代理链的第一跳会把 `127.0.0.1`、`localhost`、`::1` 重写为 `host.docker.internal`。
