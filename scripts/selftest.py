"""端到端自测：对运行中的 web + authoritative + resolver 发起真实 UDP 查询。

用法（容器内或本地等价进程均可）：
    python -m scripts.selftest

覆盖：
1. 冷查询回源 + 缓存命中（回源次数不增）
2. 正 TTL 到期后按权威现状重查（改址生效）
3. 删除记录 -> NXDOMAIN 按 SOA 负缓存期限失效
4. 负缓存期间恢复记录，TTL 内仍 NXDOMAIN；到期后拿到新 A
5. 回源失败（权威静默丢包）：不沿用过期答复、不缓存 NXDOMAIN、回 SERVFAIL
6. 同名并发冷查询只回源一次（singleflight），且各自 DNS ID 正确
7. 单个等待者超时不取消回源、不影响其他等待者
8. 慢域名不阻塞另一域名
9. 退役：冲突拒绝 / 原子回滚 / 缓存拦截 / 保留期写入拦截 / 调低 TTL 不放行 / 到期放行
10. 迟到的权威旧回包不得复活已退役的绑定
11. 重启全部服务后：缓存清空、保留期不提前放行、到期后允许再分配
    （由 scripts/selftest.sh / selftest-local.sh 控制重启并调用 --restart-check）
"""
import json
import os
import random
import socket
import sys
import time
import urllib.error
import urllib.request

WEB = os.environ.get("WEB_BASE", "http://127.0.0.1:8000")
RESOLVER = (os.environ.get("RESOLVER_UDP_HOST", "127.0.0.1"),
            int(os.environ.get("RESOLVER_UDP_PORT", "5354")))

# dnslib 只在被测服务的镜像里是硬依赖；自测脚本本身也用它编解码报文
from dnslib import QTYPE, RCODE, DNSRecord  # noqa: E402

PASS, FAIL = 0, 0
FAILURE_DETAILS = []


def check(label: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  \033[32m[PASS]\033[0m {label}")
    else:
        FAIL += 1
        FAILURE_DETAILS.append(f"{label} :: {detail}")
        print(f"  \033[31m[FAIL]\033[0m {label} {detail}")


def http(method: str, path: str, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        WEB + path, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode())


def http_status(method: str, path: str, body=None):
    """与 http 相同，但不抛异常，返回 (status_code, body)，用于断言 4xx。"""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        WEB + path, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, {"raw": raw}


def stats(name: str) -> dict:
    with urllib.request.urlopen(
        f"http://{os.environ.get('RESOLVER_STATS_HOST', '127.0.0.1')}:"
        f"{os.environ.get('RESOLVER_STATS_PORT', '8081')}/stats?name={name}",
        timeout=5,
    ) as resp:
        return json.loads(resp.read().decode())["names"].get(name, {})


def full_stats() -> dict:
    with urllib.request.urlopen(
        f"http://{os.environ.get('RESOLVER_STATS_HOST', '127.0.0.1')}:"
        f"{os.environ.get('RESOLVER_STATS_PORT', '8081')}/stats",
        timeout=5,
    ) as resp:
        return json.loads(resp.read().decode())


def retired_map() -> dict:
    """当前退役保留表：ip -> 条目。"""
    return {r["ip"]: r for r in http("GET", "/api/state").get("retired", [])}


def domain_record(name: str):
    for d in http("GET", "/api/state")["domains"]:
        if d["name"] == name:
            return d["record"]
    return None


def dns_query(name: str, timeout: float):
    """真实 UDP 查询，返回 (reply_or_None, query_id)；超时返回 (None, id)。"""
    q = DNSRecord.question(name, qtype="A")
    q.header.id = random.randint(1, 65535)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(timeout)
        sock.sendto(q.pack(), RESOLVER)
        try:
            data, _ = sock.recvfrom(65535)
        except socket.timeout:
            return None, int(q.header.id)
    return DNSRecord.parse(data), int(q.header.id)


def wait_until(deadline_s, desc, fn):
    end = time.time() + deadline_s
    last = None
    while time.time() < end:
        last = fn()
        if last:
            return last
        time.sleep(0.1)
    raise AssertionError(f"等待超时：{desc}（最后状态={last}）")


def set_record(name, ip, ttl):
    return http("POST", f"/api/domains/{name}/record", {"ip": ip, "ttl": ttl})


def del_record(name):
    return http("DELETE", f"/api/domains/{name}/record")


def retire_status(name):
    return http_status("POST", f"/api/domains/{name}/retire")


def set_neg_ttl(ttl):
    return http("PUT", "/api/settings/neg-ttl", {"neg_ttl": ttl})


def set_delay(name, ms):
    return http("PUT", f"/api/domains/{name}/delay", {"delay_ms": ms})


def set_drop(name, dropped):
    return http("PUT", f"/api/domains/{name}/drop", {"dropped": dropped})


def answers_of(reply):
    return [str(rr.rdata) for rr in reply.rr if rr.rtype == QTYPE.A]


def restart_check():
    """阶段 11：重启后缓存必须为空；退役保留期不得因重启提前放行或丢失。"""
    print("\n== 11. 重启后：缓存清空 + 保留期不提前放行 + 到期允许再分配 ==")
    full = full_stats()
    check("重启后缓存表为空", all(not v.get("cached") for v in full["names"].values()),
          detail=str(full["names"]))
    check("重启后回源计数归零", all(v.get("upstream_attempts") == 0 for v in full["names"].values()),
          detail=str(full["names"]))

    state = http("GET", "/api/state")
    d1 = state["domains"][0]["name"]
    final_ip = "10.0.0.10"
    retired = {r["ip"]: r for r in state.get("retired", [])}
    info = retired.get(final_ip)
    check("重启后退役保留条目仍在（重启未清空保留期）", info is not None, detail=str(retired))
    rec = [d for d in state["domains"] if d["name"] == d1][0]["record"]
    check("重启后已撤销的记录未复活", rec is None, detail=str(rec))

    if info is not None:
        if info["remaining"] > 0:
            code, body = http_status("POST", f"/api/domains/{d1}/record",
                                     {"ip": final_ip, "ttl": 2})
            check("重启后保留期内写入该地址仍被拒绝(409)", code == 409,
                  detail=f"code={code} body={body}")
            wait_until(info["remaining"] + 5, "保留期到期",
                       lambda: retired_map().get(final_ip, {}).get("hold_until", float("inf")) <= time.time())
        code, body = http_status("POST", f"/api/domains/{d1}/record",
                                 {"ip": final_ip, "ttl": 2})
        check("保留期到期后允许重新分配（重启未把地址卡死）", code == 200,
              detail=f"code={code} body={body}")
        reply, _ = dns_query(d1, 3.0)
        check("重新分配后解析恢复正常",
              reply is not None and reply.header.rcode == RCODE.NOERROR
              and answers_of(reply) == [final_ip],
              detail=str(answers_of(reply) if reply else None))
    summarize()


def summarize():
    print(f"\n结果：{PASS} 通过 / {FAIL} 失败")
    if FAIL:
        print("失败项：")
        for d in FAILURE_DETAILS:
            print(" -", d)
        sys.exit(1)
    sys.exit(0)


def main():
    state = http("GET", "/api/state")
    d1, d2 = state["domains"][0]["name"], state["domains"][1]["name"]
    print(f"被测域名：{d1} / {d2}；解析器：{RESOLVER}；网页：{WEB}")

    # 统一短 TTL，缩短测试时长
    POS_TTL, NEG_TTL = 2, 2
    set_neg_ttl(NEG_TTL)
    set_delay(d1, 0); set_delay(d2, 0)
    set_drop(d1, False); set_drop(d2, False)
    set_record(d1, "10.0.0.10", POS_TTL)
    set_record(d2, "10.0.0.20", POS_TTL)

    print("\n== 1. 冷查询回源 + 缓存命中 ==")
    reply, qid = dns_query(d1, 3.0)
    check("冷查询拿到 NOERROR", reply is not None and reply.header.rcode == RCODE.NOERROR,
          detail=str(reply.header if reply else None))
    check("答复 DNS ID 与查询一致", reply is not None and int(reply.header.id) == qid)
    check("应答内容正确", reply is not None and answers_of(reply) == ["10.0.0.10"])
    s = wait_until(3, "stats 可见", lambda: stats(d1))
    check("回源次数=1", s.get("upstream_attempts") == 1, detail=str(s))
    attempts_after_first = s["upstream_attempts"]
    reply2, qid2 = dns_query(d1, 3.0)
    check("第二次查询命中缓存且 ID 仍正确",
          reply2 is not None and int(reply2.header.id) == qid2 and answers_of(reply2) == ["10.0.0.10"])
    s = stats(d1)
    check("缓存命中不增加回源次数", s.get("upstream_attempts") == attempts_after_first, detail=str(s))
    check("剩余 TTL 递减且不超过原始 TTL",
          s.get("ttl_remaining") is not None and 0 <= s["ttl_remaining"] <= POS_TTL, detail=str(s))

    print("\n== 2. 正 TTL 到期后按权威现状重查（改址） ==")
    set_record(d1, "10.0.0.11", POS_TTL)
    reply3, _ = dns_query(d1, 3.0)
    check("TTL 未到期仍返回旧地址（缓存有效）",
          reply3 is not None and answers_of(reply3) == ["10.0.0.10"], detail=str(answers_of(reply3)))
    wait_until(POS_TTL + 3, "正缓存到期", lambda: not stats(d1).get("cached"))
    reply4, _ = dns_query(d1, 3.0)
    check("到期重查返回新地址", reply4 is not None and answers_of(reply4) == ["10.0.0.11"],
          detail=str(answers_of(reply4)))
    check("到期重查增加了回源次数", stats(d1).get("upstream_attempts") == attempts_after_first + 1,
          detail=str(stats(d1)))

    print("\n== 3. 删除记录：NXDOMAIN 按 SOA 负缓存期限失效 ==")
    del_record(d1)
    wait_until(POS_TTL + 3, "旧正缓存到期", lambda: not stats(d1).get("cached"))
    reply5, _ = dns_query(d1, 3.0)
    check("回源得到 NXDOMAIN", reply5 is not None and reply5.header.rcode == RCODE.NXDOMAIN)
    soa = [rr for rr in reply5.auth if rr.rtype == QTYPE.SOA]
    check("NXDOMAIN 带 SOA", bool(soa))
    check("SOA minimum 等于配置的负 TTL", bool(soa) and int(soa[0].rdata.times[4]) == NEG_TTL,
          detail=str(soa[0].rdata if soa else None))
    s = stats(d1)
    check("解析器记录为负缓存", s.get("cache_kind") == "negative" and s.get("cached") is True, detail=str(s))
    neg_attempts = s["upstream_attempts"]
    reply6, _ = dns_query(d1, 3.0)
    check("负缓存期内重复查询仍是 NXDOMAIN 且不回源",
          reply6 is not None and reply6.header.rcode == RCODE.NXDOMAIN
          and stats(d1).get("upstream_attempts") == neg_attempts)

    print("\n== 4. 负缓存期内恢复记录，到期后才生效 ==")
    set_record(d1, "10.0.0.12", POS_TTL)
    reply7, _ = dns_query(d1, 3.0)
    check("负 TTL 未到期：恢复记录不立即生效",
          reply7 is not None and reply7.header.rcode == RCODE.NXDOMAIN, detail=str(reply7.header))
    wait_until(NEG_TTL + 3, "负缓存到期", lambda: not stats(d1).get("cached"))
    reply8, _ = dns_query(d1, 3.0)
    check("负缓存到期重查得到恢复后的 A 记录",
          reply8 is not None and reply8.header.rcode == RCODE.NOERROR
          and answers_of(reply8) == ["10.0.0.12"], detail=str(answers_of(reply8) if reply8 else None))

    print("\n== 5. 回源失败：不沿用过期答复 / 不缓存 NXDOMAIN / 回 SERVFAIL ==")
    wait_until(POS_TTL + 3, "正缓存再次到期", lambda: not stats(d1).get("cached"))
    set_drop(d1, True)  # 权威对该域名静默丢包
    reply9, _ = dns_query(d1, 6.0)
    check("回源失败回 SERVFAIL", reply9 is not None and reply9.header.rcode == RCODE.SERVFAIL,
          detail=str(reply9.header if reply9 else None))
    check("失败后没有把 NXDOMAIN 缓存起来",
          stats(d1).get("cache_kind") != "negative" and not stats(d1).get("cached"), detail=str(stats(d1)))
    check("失败被计入 upstream_failures", stats(d1).get("last_error"))
    failed_attempts = stats(d1).get("upstream_attempts")
    reply10, _ = dns_query(d1, 6.0)
    check("再次查询仍重新回源并 SERVFAIL（不沿用任何过期答复）",
          reply10 is not None and reply10.header.rcode == RCODE.SERVFAIL
          and stats(d1).get("upstream_attempts") == failed_attempts + 1,
          detail=str(stats(d1)))
    set_drop(d1, False)
    reply11, _ = dns_query(d1, 6.0)
    check("权威恢复后立即重新拿到真实答复",
          reply11 is not None and reply11.header.rcode == RCODE.NOERROR
          and answers_of(reply11) == ["10.0.0.12"], detail=str(answers_of(reply11) if reply11 else None))

    print("\n== 6. 同名并发冷查询只回源一次，各自 DNS ID 正确 ==")
    set_delay(d2, 1500)
    wait_until(POS_TTL + 3, "d2 缓存到期", lambda: not stats(d2).get("cached"))
    base = stats(d2).get("upstream_attempts", 0)
    results = []
    threads = []

    def worker():
        r, i = dns_query(d2, 5.0)
        results.append((r, i))

    import threading
    first = threading.Thread(target=worker)
    first.start()
    # 显式等待 leader 进入“回源中”，避免其余线程因调度抖动错过同一次 flight
    wait_until(3, "leader 进入 inflight", lambda: stats(d2).get("inflight"))
    for _ in range(7):
        t = threading.Thread(target=worker)
        threads.append(t)
        t.start()
    first.join()
    for t in threads:
        t.join()
    check("8 个并发查询全部拿到应答", len(results) == 8, detail=f"got {len(results)}")
    check("全部 NOERROR 且应答各自匹配自己的 DNS ID",
          all(r is not None and r.header.rcode == RCODE.NOERROR and int(r.header.id) == i
              for r, i in results), detail=str([(int(r.header.id) if r else None, i) for r, i in results]))
    check("应答内容一致", all(answers_of(r) == ["10.0.0.20"] for r, _ in results))
    check("同名冷查询只回源一次", stats(d2).get("upstream_attempts") == base + 1,
          detail=str(stats(d2)))

    print("\n== 7. 单个等待者超时不取消回源、不影响其他等待者 ==")
    set_delay(d2, 1200)
    wait_until(POS_TTL + 3, "d2 缓存到期", lambda: not stats(d2).get("cached"))
    base = stats(d2).get("upstream_attempts", 0)
    slow, fast = [], []

    def slow_client():
        r, i = dns_query(d2, 5.0)
        slow.append((r, i))

    def fast_client():
        r, i = dns_query(d2, 0.35)  # 权威延迟 1.2s，此客户端必然先超时
        fast.append((r, i))

    t_slow = threading.Thread(target=slow_client)
    t_fast = threading.Thread(target=fast_client)
    t_slow.start(); time.sleep(0.05); t_fast.start()
    t_fast.join(); t_slow.join()
    check("短超时客户端收到超时（None）", fast and fast[0][0] is None, detail=str(fast))
    check("长超时客户端仍拿到回源应答且 ID 正确",
          slow and slow[0][0] is not None and int(slow[0][0].header.id) == slow[0][1]
          and answers_of(slow[0][0]) == ["10.0.0.20"], detail=str(slow))
    check("回源只发生一次，未被超时者取消", stats(d2).get("upstream_attempts") == base + 1,
          detail=str(stats(d2)))
    set_delay(d2, 0)

    print("\n== 8. 慢域名不阻塞另一域名 ==")
    set_delay(d2, 2500)
    wait_until(POS_TTL + 3, "d2 缓存到期", lambda: not stats(d2).get("cached"))
    t0 = time.time()
    r_d1, _ = dns_query(d1, 3.0)  # d1 无延迟；若被 d2 阻塞会等到 2.5s 以上
    elapsed = time.time() - t0
    check("d1 在 d2 慢回源期间快速得到答复",
          r_d1 is not None and r_d1.header.rcode == RCODE.NOERROR and elapsed < 1.0,
          detail=f"elapsed={elapsed:.2f}s")
    set_delay(d2, 0)

    print("\n== 9. 退役：冲突拒绝 / 原子回滚 / 缓存拦截 / 保留期写入拦截 / 到期放行 ==")
    RET_IP = "10.0.0.10"
    set_record(d1, RET_IP, 4)
    set_record(d2, RET_IP, POS_TTL)  # d2 同时持有该地址
    code, body = retire_status(d1)
    check("另一现行记录仍用该地址时退役返回 409", code == 409, detail=f"code={code} body={body}")
    rec = domain_record(d1)
    check("冲突后 d1 记录未被删除（原子回滚）", rec is not None and rec["ip"] == RET_IP,
          detail=str(rec))
    check("冲突后未写入保留条目（原子回滚）", RET_IP not in retired_map(), detail=str(retired_map()))
    set_record(d2, "10.0.0.20", POS_TTL)  # d2 让出该地址

    # 让解析器真实发出一次 RET_IP 绑定（答复日志是保留期的依据）
    wait_until(POS_TTL + 3, "d1 旧缓存到期", lambda: not stats(d1).get("cached"))
    reply, _ = dns_query(d1, 3.0)
    check("预备查询确实发出了 RET_IP 绑定",
          reply is not None and answers_of(reply) == [RET_IP],
          detail=str(answers_of(reply) if reply else None))

    code, body = retire_status(d1)
    check("退役成功", code == 200 and body.get("ok"), detail=f"code={code} body={body}")
    hold_until = float(body.get("hold_until") or 0)
    check("保留截止时刻在未来（覆盖已发答复的 TTL）", hold_until > time.time(), detail=str(body))
    check("退役后记录已撤销", domain_record(d1) is None)
    rm = retired_map()
    check("保留条目已写入且剩余时间>0", RET_IP in rm and rm[RET_IP]["remaining"] > 0,
          detail=str(rm))

    # 退役成功后，解析器不得再从缓存发出旧绑定；权威可达时应查到撤销后的结果
    reply, _ = dns_query(d1, 3.0)
    check("退役后立即查询得到 NXDOMAIN 而非旧绑定",
          reply is not None and reply.header.rcode == RCODE.NXDOMAIN and RET_IP not in answers_of(reply),
          detail=str(answers_of(reply) if reply else None))
    check("解析器拦截了退役缓存绑定（retired_blocks>=1）",
          full_stats().get("retired_blocks", 0) >= 1, detail=str(full_stats()))

    code, body = http_status("POST", f"/api/domains/{d2}/record", {"ip": RET_IP, "ttl": POS_TTL})
    check("保留期内把该地址写入 d2 被拒绝(409)", code == 409, detail=f"code={code} body={body}")
    code, body = http_status("POST", f"/api/domains/{d1}/record", {"ip": RET_IP, "ttl": POS_TTL})
    check("保留期内写回 d1 同样被拒绝(409)", code == 409, detail=f"code={code} body={body}")
    code, body = http_status("POST", f"/api/domains/{d1}/record", {"ip": "10.0.0.13", "ttl": POS_TTL})
    check("保留期内写入其他地址不受影响", code == 200, detail=f"code={code} body={body}")

    # 调低 TTL 相关配置不得提前放行
    h1 = retired_map()[RET_IP]["hold_until"]
    set_neg_ttl(1)
    set_record(d1, "10.0.0.13", 1)
    h2 = retired_map()[RET_IP]["hold_until"]
    check("调低 TTL 配置后保留截止时刻不变", abs(h1 - h2) < 0.01, detail=f"{h1} vs {h2}")
    set_neg_ttl(NEG_TTL)

    wait_until(max(1.0, hold_until - time.time()) + 5, "保留期到期",
               lambda: retired_map().get(RET_IP, {}).get("hold_until", float("inf")) <= time.time())
    code, body = http_status("POST", f"/api/domains/{d1}/record", {"ip": RET_IP, "ttl": POS_TTL})
    check("保留期到期后允许重新分配", code == 200, detail=f"code={code} body={body}")

    print("\n== 10. 迟到的权威旧回包不得复活已退役的绑定 ==")
    LATE_IP = "10.0.0.20"
    set_record(d2, LATE_IP, POS_TTL)
    set_delay(d2, 0)
    wait_until(POS_TTL + 3, "d2 旧缓存到期", lambda: not stats(d2).get("cached"))
    reply, _ = dns_query(d2, 3.0)
    check("预备查询拿到旧绑定", reply is not None and answers_of(reply) == [LATE_IP],
          detail=str(answers_of(reply) if reply else None))
    wait_until(POS_TTL + 3, "d2 缓存再次到期", lambda: not stats(d2).get("cached"))
    set_delay(d2, 1500)  # 权威 1.5s 后才回包：制造“退役后旧回包才到达”
    late: list = []
    t = threading.Thread(target=lambda: late.append(dns_query(d2, 8.0)))
    t.start()
    wait_until(3, "解析器回源中", lambda: stats(d2).get("inflight"))
    code, body = retire_status(d2)
    check("在途回源期间退役成功", code == 200 and body.get("ok"), detail=f"code={code} body={body}")
    set_delay(d2, 0)  # 让解析器丢弃旧回包后的重试立即拿到权威当前状态
    t.join()
    reply, qid = late[0]
    check("在途查询最终收到 NXDOMAIN 而非迟到的旧绑定",
          reply is not None and reply.header.rcode == RCODE.NXDOMAIN and LATE_IP not in answers_of(reply),
          detail=str(reply.header if reply else None))
    check("答复 DNS ID 仍与查询一致", reply is not None and int(reply.header.id) == qid)
    check("解析器丢弃了迟到的旧回包（late_drops>=1）",
          full_stats().get("late_drops", 0) >= 1, detail=str(full_stats()))
    reply2, _ = dns_query(d2, 3.0)
    check("之后查询仍是 NXDOMAIN（旧绑定未从缓存复活）",
          reply2 is not None and reply2.header.rcode == RCODE.NXDOMAIN and LATE_IP not in answers_of(reply2),
          detail=str(answers_of(reply2) if reply2 else None))

    print("\n== 11 预备：制造退役保留状态与缓存，供重启后验证 ==")
    FINAL_IP = "10.0.0.10"
    set_record(d1, FINAL_IP, 15)  # TTL=15s：保留期足够覆盖服务重启耗时
    wait_until(POS_TTL + 3, "d1 旧缓存到期", lambda: not stats(d1).get("cached"))
    reply, _ = dns_query(d1, 3.0)
    check("重启前查询发出绑定", reply is not None and answers_of(reply) == [FINAL_IP],
          detail=str(answers_of(reply) if reply else None))
    code, body = retire_status(d1)
    check("重启前退役成功", code == 200 and body.get("ok"), detail=f"code={code} body={body}")
    rm = retired_map()
    check("重启前保留期未过", FINAL_IP in rm and rm[FINAL_IP]["remaining"] > 0, detail=str(rm))
    check("重启前解析器留有查询痕迹", bool(stats(d1)), detail=str(stats(d1)))
    print("（请用 scripts/selftest.sh / selftest-local.sh 重启全部服务并执行 --restart-check）")
    summarize()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--restart-check":
        restart_check()
    else:
        main()
