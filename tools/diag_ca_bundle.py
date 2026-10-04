"""诊断 6：决定性测试 —— 用 **pip 22.3.1 自己那份 2022 年的 certifi CA 包**
去校验 pypi.org 的当前证书链。

已知变量对照（都是实测）：
  * venv pip 22.3.1 : certifi **2022.09.24**
  * 全局 pip 26.0.1 : certifi **2026.01.04**
  * 系统代理 127.0.0.1:7890 在监听，但走它要 9.4s（直连 0.7s）

若"用 2022 的 CA 包校验 pypi.org 失败"，则根因就是 **pip 自带的 CA 包过期**，
修复方式：升级 venv 的 pip（顺带更新 certifi），或给它指定新 CA。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VENV_PY = os.path.join(ROOT, ".venv", "Scripts", "python.exe")
BUILD = os.path.join(ROOT, "build")
os.makedirs(BUILD, exist_ok=True)

CHILD = r"""
import json, os, socket, ssl, sys, time

HOST = "pypi.org"
out = {"openssl": ssl.OPENSSL_VERSION}

bundles = {
    "venv_pip_certifi_2022": r"F:\CodingBox\DeepseekHarness\LiveDanmakuExtractor\.venv\Lib\site-packages\pip\_vendor\certifi\cacert.pem",
    "global_pip_certifi_2026": r"E:\Python\Lib\site-packages\pip\_vendor\certifi\cacert.pem",
}

def try_connect(host, port, cafile, server_hostname, label):
    ctx = ssl.create_default_context(cafile=cafile) if cafile else ssl.create_default_context()
    ctx.check_hostname = True
    ctx.verify_mode = ssl.CERT_REQUIRED
    try:
        with socket.create_connection((host, port), timeout=12) as raw:
            with ctx.wrap_socket(raw, server_hostname=server_hostname) as tls:
                cert = tls.getpeercert()
                issuer = dict(x[0] for x in cert.get("issuer", []))
                subject = dict(x[0] for x in cert.get("subject", []))
                return {
                    "status": "OK",
                    "tls": tls.version(),
                    "subject_cn": subject.get("commonName"),
                    "issuer_cn": issuer.get("commonName"),
                    "issuer_org": issuer.get("organizationName"),
                }
    except Exception as exc:
        return {"status": "FAIL", "error": f"{type(exc).__name__}: {str(exc)[:160]}"}

for label, cafile in bundles.items():
    out[label] = {"cafile_exists": os.path.isfile(cafile)}
    if out[label]["cafile_exists"]:
        out[label]["direct"] = try_connect(HOST, 443, cafile, HOST, label)

# 再用系统默认（无 cafile）做对照
out["system_default_direct"] = try_connect(HOST, 443, None, HOST, "default")

print("R " + json.dumps(out, ensure_ascii=False, default=str))
"""

path = os.path.join(BUILD, "diag6_child.py")
with open(path, "w", encoding="utf-8") as handle:
    handle.write(CHILD)

print("=" * 88)
print("用不同 CA 包直连 pypi.org:443 校验证书链", flush=True)
print("=" * 88)
result = subprocess.run(
    [VENV_PY, path], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120
)
payload = None
for line in (result.stdout or "").splitlines():
    if line.startswith("R "):
        payload = json.loads(line[2:])
if payload is None:
    print("❌ 无输出")
    print((result.stdout or "")[-800:])
    print((result.stderr or "")[-1200:])
    raise SystemExit(1)

print(f"  OpenSSL = {payload['openssl']}")
print()
for key in ("venv_pip_certifi_2022", "global_pip_certifi_2026", "system_default_direct"):
    value = payload.get(key)
    print(f"  【{key}】")
    if isinstance(value, dict):
        for sub, item in value.items():
            if isinstance(item, dict):
                print(f"      {sub}: {item.get('status')} {item.get('tls','')} "
                      f"issuer={item.get('issuer_cn')} ({item.get('issuer_org')}) {item.get('error','')}")
            else:
                print(f"      {sub}: {item}")
    print()

print("=" * 88)
print("判据")
print("=" * 88)
old = payload.get("venv_pip_certifi_2022", {}).get("direct", {})
new = payload.get("global_pip_certifi_2026", {}).get("direct", {})
sysdef = payload.get("system_default_direct", {})
print(f"  2022 CA 包直连      : {old.get('status')} {old.get('error','')}")
print(f"  2026 CA 包直连      : {new.get('status')} {new.get('error','')}")
print(f"  系统默认 CA 直连    : {sysdef.get('status')} {sysdef.get('error','')}")
if old.get("status") == "FAIL" and new.get("status") == "OK":
    print("  ⇒ ✅ 根因 = venv 内 pip 22.3.1 自带的 certifi CA 包过旧")
else:
    print("  ⇒ ⚠️ 旧 CA 包不是根因，需要继续排查（可能仍是代理路径问题）")
