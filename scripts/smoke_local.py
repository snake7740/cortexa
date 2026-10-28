"""部署自测：对任意 Cortexa 端点跑一遍官方 Smoke 同款契约（Health → Add → Search）。

用途：公网部署完成后、在官网点"申请评测"之前先自己跑一次，避免消耗官方 smoke 配额
（每小时 1 次、每赛道 ≤30 次）去调试低级错误。只依赖标准库，服务器/本机/CI 均可直接跑。

用法：
    python scripts/smoke_local.py http://127.0.0.1:8000
    python scripts/smoke_local.py https://memory.example.com --key <Memory System Key>
    python scripts/smoke_local.py https://memory.example.com --key <k> --scheme token|bearer|x-api-key
    python scripts/smoke_local.py ... --keep      # 不清理测试 user_id（默认跑完 DELETE /scope/<uid>）

校验项（2026-10 现行 api-guide）：
    H1 GET /health 无鉴权、任意 2xx
    A1 POST /add 返回 200 且 success=true
    A2 request_id / user_id / session_id 三 ID 逐字节回显
    A3 同 request_id 重放仍成功（官方重试沿用同 request_id，409 不算成功）
    S1 POST /search 返回 200 且 data 为数组
    S2 len(data) ≤ top_k；每条 id/content 非空字符串
    S3 可选字段 score 为数字、created_at 为字符串（存在时）
    S4 Add 后立即 Search 能命中刚写入内容（同步写入语义）
    X1 带 key 时：错 key 返回 401（无 key 部署跳过）
返回码：0=全部通过；1=任一失败（打印失败项便于定位）。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
import uuid

TIMEOUT = 60


def _req(method: str, url: str, body: dict | None, headers: dict) -> tuple[int, dict | list | None, str]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    hdrs = {"Content-Type": "application/json", **headers}
    req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", "replace")
            try:
                return resp.status, json.loads(raw), raw
            except json.JSONDecodeError:
                return resp.status, None, raw
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw), raw
        except json.JSONDecodeError:
            return exc.code, None, raw


def _auth_headers(key: str | None, scheme: str) -> dict:
    if not key:
        return {}
    if scheme == "x-api-key":
        return {"X-Api-Key": key}
    if scheme == "bearer":
        return {"Authorization": f"Bearer {key}"}
    return {"Authorization": f"Token {key}"}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("base_url", help="如 http://127.0.0.1:8000 或 https://memory.example.com")
    ap.add_argument("--key", default=None, help="Memory System Key（部署未设鉴权则不传）")
    ap.add_argument("--scheme", default="token", choices=["token", "bearer", "x-api-key"])
    ap.add_argument("--top-k", type=int, default=100, help="正式评测固定 100")
    ap.add_argument("--keep", action="store_true", help="跑完不删测试 user_id")
    args = ap.parse_args()

    base = args.base_url.rstrip("/")
    auth = _auth_headers(args.key, args.scheme)
    uid = f"smoke-{uuid.uuid4().hex[:8]}"
    sid = f"{uid}-s1"
    rid = f"{uid}-r1"
    marker = f"Zephyrine-{uuid.uuid4().hex[:6]}"
    failures: list[str] = []

    def check(tag: str, ok: bool, detail: str = "") -> None:
        print(f"  [{'OK ' if ok else 'FAIL'}] {tag}" + (f"  -- {detail}" if detail and not ok else ""))
        if not ok:
            failures.append(tag)

    # ---- H1 ----
    print(f"== Health {base}/health")
    t0 = time.perf_counter()
    st, js, raw = _req("GET", f"{base}/health", None, {})
    check("H1 health 2xx 无鉴权", 200 <= st < 300, f"status={st} body={raw[:200]}")
    if isinstance(js, dict):
        print(f"       version={js.get('version')} scene={js.get('scene')} journal={js.get('journal_mode')} "
              f"embed={js.get('embedding_provider')} key_required={js.get('api_key_required')} "
              f"({(time.perf_counter()-t0)*1000:.0f}ms)")
        if js.get("scene") not in (None, "aml"):
            print("       !! 提醒：参赛部署 CORTEXA_SCENE 应为 aml")
        if js.get("journal_mode") not in (None, "eval", "off"):
            print("       !! 提醒：参赛部署 CORTEXA_JOURNAL_MODE 应为 eval（评测数据不留日志）")
        if args.key and js.get("api_key_required") is False:
            print("       !! 提醒：传了 --key 但服务端未开鉴权（CORTEXA_API_KEY 为空）")

    # ---- A1 A2 ----
    print("== Add")
    now_ms = int(time.time() * 1000)
    add_body = {
        "request_id": rid,
        "user_id": uid,
        "session_id": sid,
        "messages": [
            {"role": "user", "timestamp": now_ms - 60_000,
             "content": f"My cat is named {marker} and she loves tuna."},
            {"role": "assistant", "timestamp": now_ms - 59_000,
             "content": f"Got it, {marker} the tuna-loving cat."},
        ],
    }
    t0 = time.perf_counter()
    st, js, raw = _req("POST", f"{base}/add", add_body, auth)
    add_ms = (time.perf_counter() - t0) * 1000
    check("A1 add 200 + success=true", st == 200 and isinstance(js, dict) and js.get("success") is True,
          f"status={st} body={raw[:200]}")
    if isinstance(js, dict):
        echoed = (js.get("request_id"), js.get("user_id"), js.get("session_id"))
        check("A2 三 ID 逐字节回显", echoed == (rid, uid, sid), f"got={echoed}")
    print(f"       add latency {add_ms:.0f}ms")

    # ---- A3 ----
    st, js, raw = _req("POST", f"{base}/add", add_body, auth)
    check("A3 同 request_id 重放仍 200/success", st == 200 and isinstance(js, dict) and js.get("success") is True,
          f"status={st} body={raw[:200]}")

    # ---- S1 S2 S3 S4 ----
    print("== Search")
    t0 = time.perf_counter()
    st, js, raw = _req("POST", f"{base}/search",
                       {"query": "What is the name of my cat?", "user_id": uid, "top_k": args.top_k}, auth)
    search_ms = (time.perf_counter() - t0) * 1000
    data = js.get("data") if isinstance(js, dict) else None
    check("S1 search 200 + data 数组", st == 200 and isinstance(data, list), f"status={st} body={raw[:200]}")
    if isinstance(data, list):
        check("S2 len(data) ≤ top_k", len(data) <= args.top_k, f"len={len(data)} top_k={args.top_k}")
        bad = [i for i, d in enumerate(data)
               if not (isinstance(d, dict) and isinstance(d.get("id"), str) and d["id"]
                       and isinstance(d.get("content"), str) and d["content"])]
        check("S2 每条 id/content 非空字符串", not bad, f"bad_index={bad[:5]}")
        bad_opt = [i for i, d in enumerate(data) if isinstance(d, dict) and (
            ("score" in d and d["score"] is not None and not isinstance(d["score"], (int, float)))
            or ("created_at" in d and d["created_at"] is not None and not isinstance(d["created_at"], str)))]
        check("S3 score 数字 / created_at 字符串", not bad_opt, f"bad_index={bad_opt[:5]}")
        hit = any(isinstance(d, dict) and marker in str(d.get("content", "")) for d in data)
        check("S4 Add 后立即 Search 命中刚写内容", hit, f"top={[str(d.get('content',''))[:40] for d in data[:3]]}")
        extra = [k for k in (js or {}) if k != "data"]
        if extra:
            print(f"       (响应除 data 外还有字段 {extra}，官方忽略未知字段，仅提示)")
    print(f"       search latency {search_ms:.0f}ms, returned {len(data) if isinstance(data, list) else '-'}")

    # ---- X1 ----
    if args.key:
        print("== Auth")
        st, _, _ = _req("POST", f"{base}/search", {"query": "x", "user_id": uid, "top_k": 1},
                        _auth_headers(args.key + "-wrong", args.scheme))
        check("X1 错 key 返回 401", st == 401, f"status={st}")
        st, _, _ = _req("POST", f"{base}/search", {"query": "x", "user_id": uid, "top_k": 1}, {})
        check("X1 无 key 返回 401", st == 401, f"status={st}")

    # ---- cleanup ----
    if not args.keep:
        st, _, _ = _req("DELETE", f"{base}/scope/{uid}", None, auth)
        print(f"== Cleanup DELETE /scope/{uid} -> {st}")

    print()
    if failures:
        print(f"SMOKE FAIL ({len(failures)}): " + ", ".join(failures))
        return 1
    print("SMOKE OK — 契约面全部通过，可以去官网申请 Smoke 评测。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
