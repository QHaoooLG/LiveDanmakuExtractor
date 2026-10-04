"""诊断 7：复刻 pip 的真实请求路径 —— **CONNECT 隧道 + TLS**。

诊断 5 已实测：
  * 直连 pypi.org:443              -> 0.7s OK
  * stdlib urllib **走代理**        -> 9.4s OK（很慢，但能通）
  * urllib3 走代理 GET /simple/     -> 200 OK，但响应体 46 MB，读不完

pip 真正做的是：向代理发 `CONNECT pypi.org:443`，拿到 200 后
**在该隧道上做 TLS 握手**。本脚本把这条路径单独复刻出来，
分别用 venv 的 pip 22.3.1 与全局 pip 26.0.1 的 urllib3 走一遍。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VENV_PY = os.path.join(ROOT, ".venv", "Scripts", "python.exe")
BUILD = os.path.join(ROOT, "build")
os.makedirs(os.path.dirname(BUILD), exist_ok=True)

CHILD = r"""
import json, socket, ssl, sys, time

PROXY = ("127.0.0.1", 7890)
HOST, PORT = "pypi.org", 443
out = {"openssl": ssl.OPENSSL_VERSION}

# --- 1) 手工 CONNECT 隧道，再用 ssl 包一层（完全复刻 pip 的路径）---
def via_proxy_manual(use_cafile=None):
    res = {}
    try:
        t0 = time.time()
        raw = socket.create_connection(PROXY, timeout=15)
        res["proxy_connect_s"] = round(time.time() - t0, 2)
        req = f"CONNECT {HOST}:{PORT} HTTP/1.1\r\nHost: {HOST}:{PORT}\r\n\r\n"
        raw.sendall(req.encode())
        raw.settimeout(15)
        t0 = time.time()
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = raw.recv(4096)
            if not chunk:
                res["connect_reply"] = "连接被对端关闭"
                return res
            buf += chunk
        res["connect_reply"] = buf.split(b"\r\n")[0].decode("latin-1")
        res["connect_s"] = round(time.time() - t0, 2)
        if b" 200 " not in buf.split(b"\r\n")[0]:
            raw.close()
            return res
        ctx = ssl.create_default_context(cafile=use_cafile) if use_cafile else ssl.create_default_context()
        t0 = time.time()
        tls = ctx.wrap_socket(raw, server_hostname=HOST)
        res["tls"] = tls.version()
        res["tls_s"] = round(time.time() - t0, 2)
        tls.sendall(f"HEAD /simple/ HTTP/1.1\r\nHost: {HOST}\r\nConnection: close\r\n\r\n".encode())
        tls.settimeout(20)
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = tls.recv(4096)
            if not chunk:
                break
            data += chunk
        res["http_reply"] = data.split(b"\r\n")[0].decode("latin-1") if data else "（无响应）"
        tls.close()
        res["status"] = "OK"
    except Exception as exc:
        res["status"] = "FAIL"
        res["error"] = f"{type(exc).__name__}: {str(exc)[:160]}"
    return res

out["manual_proxy"] = via_proxy_manual()

# --- 2) urllib3 走代理，但只读很少字节（避免 46MB 响应体）---
try:
    import pip._vendor.urllib3 as u
    out["urllib3"] = u.__version__
    for label, kwargs in (("proxy_auto", {}), ("proxy_explicit", {"proxy": "http://127.0.0.1:7890"})):
        try:
            t0 = time.time()
            pool = u.ProxyManager("http://127.0.0.1:7890", retries=False, timeout=15.0) \
                if label == "proxy_auto" else u.ProxyManager("http://127.0.0.1:7890", retries=False, timeout=15.0)
            resp = pool.request("HEAD", f"https://{HOST}/simple/")
            out[label] = f"OK status={resp.status} {time.time()-t0:.1f}s"
        except Exception as exc:
            out[label] = f"FAIL {type(exc).__name__}: {str(exc)[:140]}"
except Exception as exc:
    out["urllib3"] = f"ERR {exc}"

print("R " + json.dumps(out, ensure_ascii=False, default=str))
"""

path = os.path.join(BUILD, "diag7_child.py")
with open(path, "w", encoding="utf-8") as handle:
    handle.write(CHILD)

print("=" * 88)
print("复刻 pip 的代理路径：CONNECT 隧道 + TLS + HEAD", flush=True)
print("=" * 88)

for label, exe in (("venv 解释器 (pip 22.3.1 的 urllib3)", VENV_PY),
                   ("全局解释器 (pip 26.0.1 的 urllib3)", r"E:\Python\python.exe")):
    print(f"\n【{label}】")
    try:
        result = subprocess.run(
            [exe, path], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90
        )
        payload = None
        for line in (result.stdout or "").splitlines():
            if line.startswith("R "):
                payload = json.loads(line[2:])
        if payload is None:
            print(f"    ❌ 无输出 stdout={(result.stdout or '')[:300]!r} stderr={(result.stderr or '')[:300]!r}")
            continue
        manual = payload.get("manual_proxy", {})
        print(f"    openssl            = {payload.get('openssl')}")
        print(f"    urllib3            = {payload.get('urllib3')}")
        print(f"    --- 手工 CONNECT 隧道 ---")
        for key in ("status", "connect_reply", "proxy_connect_s", "connect_s", "tls", "tls_s", "http_reply", "error"):
            if key in manual:
                print(f"      {key:<18} {manual[key]}")
        for key in ("proxy_auto", "proxy_explicit"):
            if key in payload:
                print(f"    {key:<18} {payload[key]}")
    except subprocess.TimeoutExpired:
        print("    ⏱ 90 秒硬超时")
