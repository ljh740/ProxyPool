

# plan.md — Sticky Upstream Proxy Router (shared/exclusive switchable)

## Goal
在一台 macOS 服务器上（Docker 环境），搭建一个“入口代理”（HTTP proxy），让 Docker 内的服务统一连入口代理出网；入口代理再把请求转发到一组外部上游 HTTP 代理（同一 host，不同端口 10001-10100，账号密码一致）。

需要支持两种可切换策略：

1. **Shared（共享）**：任意 username 都可用；同一个 username 总是落到同一个上游端口（粘性）。允许不同 username 共享同一条上游；目标是分布尽量均匀，避免极端偏斜。
2. **Exclusive（独享）**：任意 username 都可用；每个 username 独占一个上游端口（100 个用户最多 100 个端口），不允许多个用户共享同一端口。若端口耗尽：可配置拒绝或降级到 shared。

可选扩展：
- **SharedCapped（共享限载）**：共享但限制每个端口最多绑定 X 个用户。

---

## High-level Architecture
- 入口代理：Squid（Docker）
- 路由决策：Squid external ACL → Python helper
- 状态存储（exclusive/shared_capped）：Redis
- 上游代理：UPSTREAM_HOST + 端口 10001-10100 + 统一账号密码

---

## Repo Layout
```
ProxyPool/
  docker-compose.yml
  squid/
    Dockerfile
    entrypoint.sh
    squid.conf.in
  helper/
    router.py
    requirements.txt
  scripts/
    gen_squid_conf.py
  plan.md
```

---

## Environment Variables

```
UPSTREAM_HOST=proxy.example.com
UP_USER=foo
UP_PASS=bar

PORT_FIRST=10001
PORT_LAST=10100

MODE=shared            # shared | exclusive | shared_capped
SALT=change-me

EXCLUSIVE_FALLBACK=deny   # deny | shared
TTL_SECONDS=0             # 0 = 永久绑定

CAP_PER_PORT=2            # shared_capped 使用

REDIS_HOST=redis
REDIS_PORT=6379
```

---

## Routing Algorithms

### Shared (stateless)
```
idx = uint32(sha256(SALT + username)[0..3]) % N
port = PORT_FIRST + idx
```

### Exclusive (stateful)
- 如果存在 bind:user → 返回
- 否则扫描端口池，SETNX port:<port> user
- 成功后写 bind:user port
- 若耗尽 → fallback 或 ERR

### SharedCapped
- 若 user 已绑定 → 返回
- 否则选择负载 < CAP_PER_PORT 的端口
- 绑定并增加计数

---

## Squid Integration

- 使用 basic_fake_auth 只要求携带用户名
- external_acl_type 调用 router.py
- router.py 返回：OK message=<port> 或 ERR
- 每个端口对应一个 cache_peer

---

## Implementation Steps

### 1. Dockerfile (squid)

- FROM ubuntu/squid
- 安装 python3, pip
- pip install redis
- 拷贝 helper 和 entrypoint

### 2. router.py

- 从 env 读取 MODE
- 根据 MODE 调用对应策略
- 连接 Redis（如需要）
- stdin → stdout 循环

### 3. gen_squid_conf.py

- 读取 env
- 生成：
  - cache_peer 列表
  - acl is_<port>
  - cache_peer_access

### 4. entrypoint.sh

- 调用 gen_squid_conf.py > /etc/squid/squid.conf
- squid -k parse
- squid -N -f /etc/squid/squid.conf

### 5. docker-compose.yml

- squid + redis
- 暴露 3128

---

## Usage

```
HTTP_PROXY=http://userA:x@squid:3128
HTTPS_PROXY=http://userA:x@squid:3128
```

---

## Testing

- 同一 username 多次请求 → 同一出口
- shared：不同 username 大致均匀
- exclusive：user1..user100 端口唯一
- user101 → 拒绝或降级

---

## Defaults

- MODE=shared
- SALT 固定保存
- EXCLUSIVE_FALLBACK=deny
- TTL_SECONDS=0