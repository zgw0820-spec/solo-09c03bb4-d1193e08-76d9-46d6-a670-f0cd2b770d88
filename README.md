# 机房 DNS 改址 / 撤销 / 退役演示台

一个面向机房值班场景的最小 DNS 实验平台：在网页上给两个内网域名设置 / 删除 / **退役**
A 记录、调整 TTL 与权威回包延迟，然后发起**真实 UDP DNS 查询**，观察客户端何时拿到新答复、
剩余 TTL 如何递减、解析器向权威回源了几次。

**退役场景**：内网 IPv4 地址要分给另一台机器前，值班员在网页上“退役”原 A 记录 ——
平台撤销记录并把该地址保留到“客户端手里的旧答复全部到期”为止；保留期内该地址
禁止写入任何 A 记录，解析器也不会再从缓存或迟到的权威回包发出旧绑定。

## 架构

```
浏览器 ──HTTP──> web (FastAPI, 8000)
                  │  写记录/退役/配置             读记录/配置（每查询短连接）
                  ▼                            ▲
               SQLite (共享卷 dns-data) ────────┼────────────┐
                  ▲  records/settings          │            │ retired_addresses
                  │  retired_addresses         │            │ answer_log
客户端/网页后端 ──UDP 53──> resolver (缓存解析器, dnslib)
                               │  缓存未命中/过期才回源（UDP 53）
                               │  每发一个 A 答复写 answer_log（发包时刻+报文TTL）
                               │  缓存命中前 / 回源写缓存前检查退役名册
                               ▼
                          authoritative (权威 DNS, dnslib, AA)
                               │
                               └─ 每域名可配回包延迟 / 静默丢包（模拟故障）
```

- **authoritative**：线程化 UDP 权威服务。A 记录存在则回 AA 应答（TTL 取记录值）；
  不存在则回 `NXDOMAIN` 并在 authority 段带 SOA，SOA 的 `minimum` 即负缓存期限。
- **resolver**：线程化 UDP 缓存解析器，另带一个 HTTP 状态口（8081）暴露
  每域名的回源次数、缓存种类、剩余 TTL、单飞状态、退役拦截/迟到回包丢弃计数、最近错误。
  **缓存只在内存**，重启即空；答复日志与退役名册读写共享卷上的 SQLite，重启不丢。
- **web**：FastAPI + 单文件原生 JS 页面，SQLite 存记录、退役保留表与设置，启动时自动建表播种。

## 启动（Docker Compose）

```bash
docker compose up -d --build
# 网页：     http://localhost:8000
# 解析器：   127.0.0.1:5354/udp（dig @127.0.0.1 -p 5354 app1.internal）
# 状态接口： http://localhost:8081/stats
```

无 Docker 的环境可用本地等价进程（需要一个含 fastapi/uvicorn/dnslib 的 Python）：

```bash
scripts/selftest-local.sh        # 自动起三进程、跑自测、重启验证、清理
```

## 网页功能

1. 两个固定域名（默认 `app1.internal`、`app2.internal`，可用环境变量 `DOMAINS` 改）
   的 A 记录**设置 / 改址 / 仅删除 / 退役**，记录 TTL 可配。
2. **退役（撤销+保留地址）**：一个按钮完成“撤销记录 + 保存地址保留期限”
   （同一 SQLite 事务，失败一并回滚）。若另一条现行 A 记录仍用该地址，返回 409 拒绝。
3. **退役地址保留期卡片**：每个被保留地址的退役时刻、**可重新分配时刻**与倒计时；
   保留期内把该地址写入任何 A 记录都会被 409 拒绝，到期后自动放行。
4. 全局**负缓存 TTL**（写入权威 SOA minimum）。
5. 每域名**权威回包延迟（毫秒）**与**静默丢包开关**（模拟权威不可达）。
6. 输入**查询等待上限（毫秒）**后发起真实 UDP 查询，展示结果码、应答、
   答复 DNS ID 是否与查询一致、剩余 TTL、缓存种类、该域名累计回源次数。
7. 解析器实时状态表（含退役缓存拦截、迟到旧回包丢弃计数），可 2 秒自动刷新。

## 退役语义（与需求逐条对应）

| 需求 | 实现位置与行为 |
| --- | --- |
| 网页为 A 记录增加退役操作 | `POST /api/domains/{name}/retire` + 域名卡片“退役”按钮 |
| 另一现行 A 记录仍用该地址则拒绝 | 退役事务内 `SELECT ... WHERE ip=? AND name!=?`，命中即 `RetireConflict` → 409 |
| 撤销记录与保存保留期限一起提交、失败一并回滚 | `db.retire_record()`：`BEGIN IMMEDIATE` 内删记录 + 写 `retired_addresses`，异常 `rollback()` |
| 退役后解析器不能从缓存再发旧绑定 | 缓存命中前检查退役名册 `(name, ip)`：命中即视为失效并重新回源（`retired_blocks` 计数） |
| 退役后不能从迟到的权威回包再发旧绑定 | leader 回源后写缓存前检查：回包含已退役绑定则丢弃并立即重新回源一次；仍是则不缓存、回 SERVFAIL（`late_drops` 计数） |
| 权威可达时应能查到撤销后的结果 | 退役即删记录，权威实时读 SQLite 回 NXDOMAIN；解析器回源后按 SOA minimum 负缓存 |
| 保留到已发旧答复全部到期（发包时间+报文 TTL） | 解析器每发一个 A 答复写 `answer_log(name, ip, sent_at, ttl)`；`hold_until = max(sent_at+ttl)`，并取当前缓存剩余 TTL 兜底，提交后再收紧一次竞态窗口 |
| 调低 TTL 不提前放行 | `hold_until` 只按已发出报文的 TTL 固化在 SQLite，之后改任何 TTL 配置都不影响 |
| 重启服务不提前放行 | `retired_addresses` / `answer_log` 在共享卷 SQLite；web 重启播种跳过已有退役条目的库；解析器缓存虽清空但名册仍在 |
| 网页显示何时可重新分配 | 保留期卡片：可再分配时刻 + 剩余秒数倒计时 |
| 保留期内拒绝写入任何 A 记录 | `POST /api/domains/{name}/record` 先查 `retired_addresses`，未到期 → 409 |
| 到期后允许写入 | 409 判断按 `hold_until > now`；到期条目不再拦截写入 |

> 注意区分两条规则：**地址再分配**看 `hold_until`（到期即放行写入）；
> **旧绑定拦截**与保留期无关——只要 `(name, ip)` 被退役且当前没有同名同 IP 的
> 现行记录，解析器就不得再发出它（防止迟到的旧回包复活已撤销绑定）。
> 若管理员事后把同一绑定重新写回 `records`，名册自动解除拦截。

## 原有缓存语义（保持不变）

| 需求 | 实现位置与行为 |
| --- | --- |
| A 按记录 TTL 失效 | `resolver.CacheEntry.expiry = now + 应答最小 TTL`；命中时按剩余秒数重写 RR TTL |
| NXDOMAIN 按 SOA 负缓存期限失效 | 权威 NXDOMAIN 带 SOA；解析器取 `SOA.minimum` 作为负缓存 TTL |
| 到期后按权威现况重查 | 缓存仅在 `fresh()` 时命中；过期即重新回源，不预先刷新 |
| 回源失败不沿用过期答复 | 失败路径不写/不读缓存，直接 `SERVFAIL`；过期条目不会被返回 |
| 回源失败不缓存为 NXDOMAIN | 仅缓存真实 `NOERROR`/`NXDOMAIN`/`NODATA`；超时/异常/SERVFAIL 一律不缓存 |
| 同名并发冷查询只回源一次 | `inflight` 字典 + `threading.Event` 单飞；后来者在同一 Flight 上等待 |
| 单个请求超时不取消其他等待者 | 等待者超时只让自己回 SERVFAIL；leader 的回源 socket 与 Event 不受影响 |
| 答复对应各自 DNS ID | 缓存模板 `id=0`，发出前用每个客户端自己的查询 ID 重写（自测逐包校验） |
| 慢域名不阻塞另一域名 | 权威与解析器均为线程化 UDP，单飞按域名分键 |
| 重启后不沿用旧缓存 | 缓存纯内存；`--restart-check` 自测验证计数与缓存归零 |

## 自测

```bash
scripts/selftest.sh           # Docker Compose 环境：端到端断言 + 全服务重启验证
scripts/selftest-local.sh     # 本地三进程等价跑法（本仓库即以此验证）
```

覆盖 11 组场景：冷查询/缓存命中、正 TTL 到期改址、NXDOMAIN 负缓存、
负缓存期内恢复记录、回源失败（SERVFAIL、不缓存、不沿用）、8 路同名单飞、
等待者超时隔离、慢域名互不阻塞、**退役全流程**（冲突 409 与原子回滚、缓存拦截、
保留期写入 409、调低 TTL 不放行、到期放行）、**迟到旧回包丢弃**、
**全服务重启后**（缓存清空、保留期不提前放行、到期允许再分配）。

> compose 卷会保留退役/日志数据；重跑自测前建议 `docker compose down -v` 清卷。

## 目录

```
app/
  config.py          环境变量集中配置
  db.py              SQLite 建表 / 记录 / 退役保留 / 答复日志 / 设置（WAL）
  authoritative.py   权威 DNS（dnslib，线程化 UDP）
  resolver.py        缓存解析器 + 单飞 + 退役拦截 + 答复日志 + HTTP 状态口
  web.py             FastAPI 接口（含退役端点与真实 UDP 查询代理）
  static/index.html  操作网页
scripts/
  selftest.py        端到端断言（真实 UDP）
  selftest.sh        compose 跑法
  selftest-local.sh  本地进程跑法
docker-compose.yml   authoritative / resolver / web 三服务
Dockerfile
```

## 说明与边界

- 实验聚焦 A 记录：解析器对非 A 查询直接转发、不缓存。
- 解析器只服务本实验域名的观察需求，没有做递归/转发链路、DNSSEC、TCP/53。
- “静默丢包”是测试夹具，用于确定性复现权威不可达；生产环境请勿保留。
- 解析器读退役名册 / 写答复日志失败时放行并计数（`answer_log_failures`）：
  SQLite 与权威同卷，正常不会失败；失败时回源同样不可用，故障路径仍回 SERVFAIL。
  答复日志写失败会让保留期计算相应偏短（退化），生产环境应改为强依赖。
- `answer_log` 只在退役事务里清理已到期行；长期运行的生产系统应加定期清理。
- 保留期按“解析器发出的答复”计算；若客户端绕过本解析器直连权威，其持有的
  旧答复不在统计内（本实验台客户端只能走解析器）。
