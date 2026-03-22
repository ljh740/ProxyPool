# plan.md — Sticky Upstream Proxy Router

## Goal
在一台 macOS 服务器上（Docker 环境），搭建一个入口 HTTP 代理，让容器或客户端统一连入口代理出网；入口代理再转发到由 Web Admin 管理的一组外部上游代理。上游代理列表持久化在 SQLite，支持单跳与多跳链路。

当前只保留一种路由策略：

1. **Shared（共享）**：任意 username 都可用；同一个 username 总是落到同一个代理条目。
2. **Random Pool Prefix（随机池前缀）**：命中 `RANDOM_POOL_PREFIX` 的 username 走随机池，不参与 shared 哈希。
3. **Compat Session Alias（兼容会话别名）**：`session_name` 只作为稳定别名参与 shared 路由，不代表唯一 upstream entry key。

---

## High-level Architecture
- 入口代理：Python HTTP proxy（Docker）
- 路由决策：Python `router.py`
- 状态存储：SQLite
- 运行时配置真源：SQLite `config`
- 上游代理列表真源：SQLite `proxy_list`
- 管理入口：Web Admin

---

## Repo Layout
```
ProxyPool/
  docker-compose.yml
  squid/
    Dockerfile
    entrypoint.sh
  helper/
    auth.py
    config_center.py
    persistence.py
    proxy_server.py
    router.py
    upstream_pool.py
    web_admin.py
  scripts/
    benchmark_chain_latency.sh
    generate_upstream_list.py
    verify.sh
    verify_real.sh
  plan.md
```

---

## Bootstrap Environment Variables
仅以下变量在进程启动时直接从环境读取：

``` 
STATE_DB_PATH=/data/proxypool.sqlite3
WEB_PORT=8077
```

其余运行时配置统一保存在 SQLite 状态文件中，由 Web Admin 管理；管理员密码在首启 `/setup` 中创建。

---

## Runtime Configuration
运行时配置示例：

```
SALT=change-me
RANDOM_POOL_PREFIX=rnd_
AUTH_PASSWORD=change-me
PROXY_HOST=0.0.0.0
PROXY_PORT=3128
```

这些值不再依赖 legacy upstream 环境变量，而是以 SQLite `config` 为准。

---

## Routing Algorithms

### Shared (stateless)
```
idx = uint32(sha256(SALT + username)[0..3]) % N
entry = upstream_entries[idx]
```

### Random Pool Prefix
- 若 username 以前缀 `RANDOM_POOL_PREFIX` 开头
- 则只在标记为 `in_random_pool=true` 的 entry 中随机选择
- 若随机池为空，则请求失败

### Exact Entry Access
- 若 username 直接等于某个 `entry_key`
- 则跳过 shared 哈希，直接路由到该 entry

### Compat Session Alias
- `session_name` 兼容端口会把 `target_value` 当作普通 username
- 然后复用 shared 路由逻辑
- 它不是唯一 entry 标识；唯一精确标识仍然是 `entry_key`

---

## Runtime Flow
1. `proxy_server.py` 启动
2. 打开 SQLite 状态文件
3. 读取 `config`
4. 读取 `proxy_list`
5. 构建 `UpstreamPool(source="admin")`
6. 启动代理服务与 Web Admin

运行时不再从 `UPSTREAM_HOST`、`UPSTREAM_LIST`、`UPSTREAM_LIST_FILE` 或端口范围配置构建 upstream。

---

## Proxy Management
- 手动添加/编辑代理
- 批量生成代理
- 支持 prepend hop
- 支持 cycle first hop
- 支持启用/禁用随机池参与
- 所有变更持久化到 SQLite，并通过 reload 生效

---

## Usage

```
HTTP_PROXY=http://userA:x@localhost:3128
HTTPS_PROXY=http://userA:x@localhost:3128
```

---

## Testing
- 同一 username 多次请求 → 同一代理条目
- 不同普通 username 在 shared 分流下分布相对均匀
- `entry_key` 直连始终命中指定代理条目
- `RANDOM_POOL_PREFIX` 命中时只在随机池条目中随机选择
- 启动与 reload 都从 SQLite `proxy_list` 生效

---

## Defaults
- SALT 固定保存
- RANDOM_POOL_PREFIX 默认留空
