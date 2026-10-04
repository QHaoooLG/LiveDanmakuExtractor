"""诊断：为什么**同一个** Python 的 TLS 在全局能通、在 venv 里却报 SSLEOFError？

关键疑点（先验，待验证）：
  `where.exe python` 显示全局解释器是 `E:\Python\python.exe`，
  而本项目的 `.venv` 正是由它创建的（`pyvenv.cfg` 里 `home = E:\Python`）。
  那么 `.venv\Scripts\python.exe` 只是**转发器 stub**，
  真正的解释器、OpenSSL、`_ssl` 模块**都是同一份**。

  ⇒ 如果解释器与 OpenSSL 完全相同，那差异只可能在：
     ① **pip 版本**（venv 的 pip 是 ensurepip 自带的老版本，全局是 26.0.1）
     ② 某个**配置/环境变量**在读 venv 时才生效
     ③ venv 里多出来的 `sitecustomize.py` / `.pth` 之类

本脚本把这三个疑点逐条测掉，不做猜测。
"""

from __future__ import annotations

import json
import os
import socket
import ssl
import subprocess
import sys
import sysconfig

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VENV_PY = os.path.join(ROOT, ".venv", "Scripts", "python.exe")
BASE_PY = r"E:\Python\python.exe"

CHILD = r"""
import json, os, ssl, sys, sysconfig
info = {
    "executable": sys.executable,
    "base_executable": getattr(sys, "_base_executable", None),
    "prefix": sys.prefix,
    "base_prefix": sys.base_prefix,
    "ssl_version": ssl.OPENSSL_VERSION,
    "ssl_module_file": getattr(ssl, "__file__", None),
    "openssl_cafile": ssl.get_default_verify_paths().cafile,
    "openssl_capath": ssl.get_default_verify_paths().capath,
    "openssl_certs_file": ssl.get_default_verify_paths().openssl_certs_file,
    "default_cert_file_env": os.environ.get("SSL_CERT_FILE"),
    "default_cert_dir_env": os.environ.get("SSL_CERT_DIR"),
    "purelib": sysconfig.get_paths().get("purelib"),
    "stdlib": sysconfig.get_paths().get("stdlib"),
    "sys_path_head": sys.path[:5],
    "pip_config_env": {k: v for k, v in os.environ.items() if k.startswith("PIP_")},
    "proxy_env": {k: v for k, v in os.environ.items()
                  if k.lower() in ("http_proxy", "https_proxy", "all_proxy", "no_proxy")},
}
# venv 里有没有 sitecustomize / usercustomize 之类会改环境的钩子
for name in ("sitecustomize", "usercustomize"):
    try:
        mod = __import__(name)
        info[f"{name}_file"] = getattr(mod, "__file__", "<builtin>")
    except ImportError:
        info[f"{name}_file"] = None
# venv 的 site-packages 里有没有 *.pth 影响
try:
    import site
    info["site_packages"] = site.getsitepackages()
    info["pth_files"] = sorted(
        p for d in site.getsitepackages() if os.path.isdir(d)
        for p in os.listdir(d) if p.endswith(".pth")
    )
except Exception as exc:
    info["site_packages"] = f"ERR {exc}"
print("PROBE " + json.dumps(info, ensure_ascii=False, default=str))
"""


def run(exe: str) -> dict:
    script = os.path.join(ROOT, "build", "diag_child.py")
    os.makedirs(os.path.dirname(script), exist_ok=True)
    with open(script, "w", encoding="utf-8") as handle:
        handle.write(CHILD)
    result = subprocess.run(
        [exe, script], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60
    )
    for line in (result.stdout or "").splitlines():
        if line.startswith("PROBE "):
            return json.loads(line[len("PROBE ") :])
    return {"_error": (result.stderr or result.stdout or "").strip()[:400]}


def pip_version(exe: str) -> str:
    result = subprocess.run(
        [exe, "-m", "pip", "--version"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90
    )
    return ((result.stdout or "") + (result.stderr or "")).strip()[:160]


def tls_probe(exe: str, label: str) -> str:
    """用该解释器自己的 ssl 模块真连一次 pypi.org，并打出手工握手细节。"""
    code = r"""
import socket, ssl, sys
ctx = ssl.create_default_context()
try:
    with socket.create_connection(("pypi.org", 443), timeout=15) as raw:
        with ctx.wrap_socket(raw, server_hostname="pypi.org") as tls:
            info = {
                "status": "OK",
                "tls_version": tls.version(),
                "cipher": tls.cipher()[0],
                "peer_subject": dict(x[0] for x in tls.getpeercert().get("subject", [])),
            }
except Exception as exc:
    info = {"status": "FAIL", "error": f"{type(exc).__name__}: {exc}"}
print("TLS " + __import__("json").dumps(info, ensure_ascii=False))
"""
    script = os.path.join(ROOT, "build", "diag_tls.py")
    with open(script, "w", encoding="utf-8") as handle:
        handle.write(code)
    result = subprocess.run(
        [exe, script], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90
    )
    for line in (result.stdout or "").splitlines():
        if line.startswith("TLS "):
            return json.loads(line[len("TLS ") :])
    return {"status": "FAIL", "error": (result.stderr or "").strip()[:300]}


def main() -> int:
    print("=" * 84)
    print("1. 解释器身份：venv 的 python.exe 是不是转发器 stub？")
    print("=" * 84)
    for label, exe in (("全局", BASE_PY), ("venv", VENV_PY)):
        info = run(exe)
        if "_error" in info:
            print(f"  {label:<6} ❌ {info['_error']}")
            continue
        print(f"\n  【{label}】{exe}")
        for key in (
            "executable",
            "base_executable",
            "prefix",
            "base_prefix",
            "ssl_version",
            "ssl_module_file",
            "purelib",
            "stdlib",
            "default_cert_file_env",
            "sitecustomize_file",
            "usercustomize_file",
            "pth_files",
            "proxy_env",
            "pip_config_env",
        ):
            print(f"      {key:<24} {info.get(key)}")

    print()
    print("=" * 84)
    print("2. 两个解释器各自直连 pypi.org:443（绕开 pip）")
    print("=" * 84)
    for label, exe in (("全局", BASE_PY), ("venv", VENV_PY)):
        print(f"  {label:<6} {tls_probe(exe, label)}")

    print()
    print("=" * 84)
    print("3. pip 版本")
    print("=" * 84)
    for label, exe in (("全局", BASE_PY), ("venv", VENV_PY)):
        print(f"  {label:<6} {pip_version(exe)}")

    print()
    print("=" * 84)
    print("4. 版本差异是唯一剩余解释：两个 pip 的 vendored urllib3/certifi 不同")
    print("=" * 84)
    code = r"""
import json
out = {}
for name in ("pip", "pip._vendor.urllib3", "pip._vendor.certifi"):
    try:
        mod = __import__(name, fromlist=["__version__"])
        out[name] = getattr(mod, "__version__", "?")
    except Exception as exc:
        out[name] = f"ERR {type(exc).__name__}"
try:
    import pip._vendor.certifi as c
    out["certifi_cacert"] = c.where()
    import os
    out["certifi_cacert_exists"] = os.path.isfile(c.where())
except Exception as exc:
    out["certifi_cacert"] = f"ERR {exc}"
print("V " + json.dumps(out, ensure_ascii=False))
"""
    script = os.path.join(ROOT, "build", "diag_pipver.py")
    with open(script, "w", encoding="utf-8") as handle:
        handle.write(code)
    for label, exe in (("全局", BASE_PY), ("venv", VENV_PY)):
        result = subprocess.run(
            [exe, script], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90
        )
        payload = None
        for line in (result.stdout or "").splitlines():
            if line.startswith("V "):
                payload = json.loads(line[2:])
        print(f"  【{label}】{payload}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
