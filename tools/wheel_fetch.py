"""离线依赖获取器：用 httpx 解析依赖并下载 wheel，供 pip --no-index 安装。

背景：本机 pip 的网络栈不可用（pypi.org 报 SSLEOFError，各国内镜像挂死到超时），
但 httpx 访问 pypi.org / files.pythonhosted.org 完全正常。
因此本脚本用 httpx 完成「解析 + 下载」，再由 pip 以
`--no-index --find-links wheels` 离线安装。

解析器是**约束感知**的：会读取 requires_dist 中的版本说明符并求交集
（否则会出现 pydantic 2.13.5 要求 pydantic-core==2.46.5 却装了 2.49.0 的冲突）。
同时会逐版本回溯，跳过没有兼容 wheel 的发行版。

需要 packaging（系统 Python 已具备）。建议用系统 Python 运行本脚本。

用法:
    python tools/wheel_fetch.py --out wheels
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import httpx
from packaging.markers import default_environment
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.version import InvalidVersion, Version

PYPI_JSON = "https://pypi.org/pypi/{name}/json"
PYPI_VERSION_JSON = "https://pypi.org/pypi/{name}/{version}/json"

ROOT_PACKAGES: tuple[str, ...] = (
    "httpx",
    "websockets",
    "brotli",
    "click",
    "pydantic",
    "fastapi",
    "uvicorn",
    "qrcode",
    "pytest",
    "pytest-asyncio",
    # 覆盖率（M4）：pytest-cov 会带出 coverage
    "pytest-cov",
    # 构建工具：editable 安装需要 bdist_wheel
    "setuptools",
    "wheel",
)


def _log(message: str) -> None:
    print(message, flush=True)


def _environment(python_version: str) -> dict[str, str]:
    env = dict(default_environment())
    env["python_version"] = python_version
    env["extra"] = ""
    return env


def wheel_score(filename: str) -> int | None:
    """给 wheel 打分；None 表示与 win_amd64 / cp311 不兼容。分数越高越优先。"""
    if not filename.endswith(".whl"):
        return None
    parts = filename[: -len(".whl")].split("-")
    if len(parts) < 5:
        return None
    py_tag, abi_tag, platform_tag = parts[-3], parts[-2], parts[-1]

    platforms = platform_tag.split(".")
    if not any(p in ("any", "win_amd64") for p in platforms):
        return None

    pytags = py_tag.split(".")
    abis = abi_tag.split(".")

    if "cp311" in pytags and "cp311" in abis and "win_amd64" in platforms:
        return 100
    if "cp311" in pytags and any(a.startswith("abi3") for a in abis) and "win_amd64" in platforms:
        return 90
    if "cp311" in pytags and "win_amd64" in platforms:
        return 80
    if "py3" in pytags and "none" in abis and "any" in platforms:
        return 50
    if "py2.py3" in pytags and "none" in abis and "any" in platforms:
        return 45
    return None


class PyPI:
    """带重试与缓存的 PyPI 元数据客户端。"""

    def __init__(self, timeout: float = 45.0) -> None:
        self.client = httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": "bili-danmaku-wheel-fetcher/0.2"},
        )
        self._project: dict[str, dict] = {}
        self._version: dict[tuple[str, str], dict] = {}

    def _get(self, url: str) -> dict | None:
        for attempt in range(3):
            try:
                resp = self.client.get(url)
                if resp.status_code == 404:
                    return None
                resp.raise_for_status()
                return resp.json()
            except Exception as exc:  # noqa: BLE001
                if attempt == 2:
                    _log(f"    ! {url} 获取失败: {type(exc).__name__}: {exc}")
                    return None
                time.sleep(1.0 * (attempt + 1))
        return None

    def project(self, name: str) -> dict | None:
        key = name.lower()
        if key not in self._project:
            payload = self._get(PYPI_JSON.format(name=name))
            if payload is None:
                return None
            self._project[key] = payload
        return self._project[key]

    def release(self, name: str, version: str) -> dict | None:
        key = (name.lower(), version)
        if key not in self._version:
            payload = self._get(PYPI_VERSION_JSON.format(name=name, version=version))
            if payload is None:
                return None
            self._version[key] = payload
        return self._version[key]

    def versions_desc(self, name: str) -> list[str]:
        payload = self.project(name)
        if payload is None:
            return []
        releases = payload.get("releases") or {}
        parsed: list[tuple[Version, str]] = []
        for raw in releases:
            try:
                parsed.append((Version(raw), raw))
            except InvalidVersion:
                continue
        parsed.sort(key=lambda item: item[0], reverse=True)
        return [raw for _version, raw in parsed]


class Resolver:
    """约束感知的依赖解析。"""

    def __init__(self, pypi: PyPI, env: dict[str, str]) -> None:
        self.pypi = pypi
        self.env = env
        self.constraints: dict[str, SpecifierSet] = {}
        self.chosen: dict[str, str] = {}
        self.failures: list[str] = []

    def _deps(self, name: str, version: str) -> list[Requirement]:
        payload = self.pypi.release(name, version)
        if payload is None:
            return []
        out: list[Requirement] = []
        for raw in payload["info"].get("requires_dist") or []:
            try:
                requirement = Requirement(raw)
            except Exception:  # noqa: BLE001
                continue
            if requirement.marker is not None:
                # 不安装 extras：任何与 extra 相关的依赖都跳过
                if "extra" in str(requirement.marker):
                    continue
                try:
                    if not requirement.marker.evaluate(self.env):
                        continue
                except Exception:  # noqa: BLE001
                    continue
            out.append(requirement)
        return out

    def _pick(self, name: str, spec: SpecifierSet) -> tuple[str, dict] | None:
        """从高到低挑第一个「满足约束且存在兼容 wheel」的版本。"""
        for version in self.pypi.versions_desc(name):
            if not spec.contains(version, prereleases=False):
                continue
            payload = self.pypi.release(name, version)
            if payload is None:
                continue
            best = self._best_file(payload)
            if best is not None:
                return version, best
        return None

    @staticmethod
    def _best_file(payload: dict) -> dict | None:
        best: tuple[int, dict] | None = None
        for entry in payload.get("urls") or []:
            if entry.get("yanked"):
                continue
            score = wheel_score(entry.get("filename", ""))
            if score is None or not entry.get("url"):
                continue
            if best is None or score > best[0]:
                best = (score, entry)
        return best[1] if best else None

    def resolve(self, roots: tuple[str, ...]) -> dict[str, dict]:
        """返回 {name: {"version": str, "file": entry}}。"""
        worklist: list[tuple[str, SpecifierSet]] = [(name, SpecifierSet()) for name in roots]
        passes = 0

        while worklist and passes < 2000:
            passes += 1
            name, spec = worklist.pop(0)
            key = name.lower()

            merged = self.constraints.get(key, SpecifierSet()) & spec
            if key in self.chosen and self.constraints.get(key) == merged:
                continue
            self.constraints[key] = merged

            picked = self._pick(name, merged)
            if picked is None:
                message = f"{name}{merged}: 无满足约束且带兼容 wheel 的版本"
                if message not in self.failures:
                    self.failures.append(message)
                continue

            version, entry = picked
            if self.chosen.get(key) == version:
                continue
            self.chosen[key] = version
            _log(f"  解析 {name}=={version}")

            for requirement in self._deps(name, version):
                worklist.append((requirement.name, requirement.specifier))

        result: dict[str, dict] = {}
        for key, version in self.chosen.items():
            payload = self.pypi.release(key, version)
            entry = self._best_file(payload) if payload else None
            if entry is None:
                self.failures.append(f"{key}=={version}: 最终无法定位 wheel")
                continue
            result[key] = {"version": version, "file": entry}
        return result


class Downloader:
    def __init__(self, out_dir: Path, pypi: PyPI) -> None:
        self.out_dir = out_dir
        self.pypi = pypi
        self.downloaded: list[str] = []

    def fetch(self, resolved: dict[str, dict]) -> list[str]:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        for name in sorted(resolved):
            entry = resolved[name]["file"]
            filename = entry["filename"]
            target = self.out_dir / filename
            if target.is_file() and target.stat().st_size > 0:
                _log(f"  复用 {filename}")
                self.downloaded.append(filename)
                continue
            resp = self.pypi.client.get(entry["url"])
            resp.raise_for_status()
            target.write_bytes(resp.content)
            _log(f"  下载 {filename}  ({len(resp.content) // 1024} KB)")
            self.downloaded.append(filename)
        return self.downloaded


def main() -> int:
    parser = argparse.ArgumentParser(description="用 httpx 解析并离线获取 wheel")
    parser.add_argument("--out", default="wheels", help="wheel 输出目录（默认 wheels）")
    parser.add_argument("--python-version", default="3.11", help="目标 Python 版本")
    parser.add_argument(
        "--packages",
        nargs="*",
        default=None,
        help="追加的根包（会与内置清单合并，而不是覆盖它 —— 覆盖会让 lock 文件丢掉项目依赖）",
    )
    args = parser.parse_args()

    out_dir = Path(args.out)
    # 追加而非覆盖：`--packages foo` 只是想多要一个包时，不应把项目依赖从 lock 里挤掉
    roots = ROOT_PACKAGES + tuple(args.packages or ())
    env = _environment(args.python_version)

    _log(f"目标 Python {args.python_version} / win_amd64 → {out_dir}/")
    _log(f"根包: {', '.join(roots)}\n")

    pypi = PyPI()
    resolver = Resolver(pypi, env)
    _log("=== 解析依赖 ===")
    resolved = resolver.resolve(roots)

    _log("\n=== 下载 wheel ===")
    downloader = Downloader(out_dir, pypi)
    downloader.fetch(resolved)

    lock = out_dir / "requirements.lock.txt"
    lock.write_text(
        "\n".join(f"{name}=={resolved[name]['version']}" for name in sorted(resolved)) + "\n",
        encoding="utf-8",
    )

    _log("\n=== 汇总 ===")
    _log(f"解析成功 {len(resolved)} 个包，wheel {len(downloader.downloaded)} 个")
    _log(f"锁定版本: {lock}")

    if resolver.failures:
        _log(f"\n失败 {len(resolver.failures)} 项:")
        for item in resolver.failures:
            _log(f"  - {item}")
        return 1

    packages = " ".join(sorted(resolved))
    _log("\n离线安装命令：")
    _log(
        f"  .venv\\Scripts\\python.exe -m pip install --no-input --no-index "
        f"--find-links {out_dir} {packages}"
    )
    _log("  .venv\\Scripts\\python.exe -m pip install --no-input --no-index --no-deps -e .")
    return 0


if __name__ == "__main__":
    if sys.version_info < (3, 9):  # pragma: no cover
        _log("需要 Python 3.9+")
        raise SystemExit(2)
    raise SystemExit(main())
