#!/usr/bin/env python3
"""按上游 GitHub Release 自动更新 apps/<package>.json。

设计目标：
- 只在「上游确实发了新版本」时改文件；版本比较用 APK 自己的 versionCode；
- 下载后重新计算 size/sha256，不信任 Release 元数据；
- 校验下载到的 APK 包名与条目一致，避免上游改名却写错文件；
- 单个上游失败不影响其它上游，但最终退出码非 0，让 CI 变红可见。

用法：
    python scripts/update_upstreams.py            # 更新所有条目
    python scripts/update_upstreams.py --dry-run  # 只看会发生什么
    python scripts/update_upstreams.py --only com.predidit.kazumi

CI 里推荐 `pip install androguard`，脚本没有 aapt/aapt2 时用它读 APK 元数据。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APPS = ROOT / "apps"
API = "https://api.github.com"
UA = "scooph-bucket-updater"

# 要跟踪的上游。新增应用时在这里加一项即可。
UPSTREAMS: list[dict] = [
    {
        "package": "com.predidit.kazumi",
        "repo": "Predidit/Kazumi",
        "asset_re": r"^Kazumi_android_.*\.apk$",
        "name": "Kazumi",
        "source": "https://github.com/Predidit/Kazumi",
        "min_sdk": 24,
        "notes": "Kazumi 动漫客户端",
    },
    {
        "package": "com.a7m3p9xv.t6qk2z8.app",
        "repo": "hect0x7/JMComic-APK",
        "asset_re": r"^\d+(?:\.\d+)+\.apk$",
        "name": "JMComic",
        "source": "https://github.com/hect0x7/JMComic-APK",
        "min_sdk": 23,
        "notes": "JMComic（第三方 APK 仓库）",
    },
    {
        # 这个仓库的 APK 资产名在 EhViewer-<ver>.apk 和 <ver>.apk 之间变过，
        # 且发布页只有单个安装包，所以直接匹配任意 .apk，由包名检查兜底。
        "repo": "xiaojieonly/Ehviewer_CN_SXJ",
        "asset_re": r"(?i)\.apk$",
        "name": "EhViewer",
        "source": "https://github.com/xiaojieonly/Ehviewer_CN_SXJ",
        "notes": "EhViewer（Ehviewer_CN_SXJ）",
    },
]


def api_get(url: str) -> object:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": UA,
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def pick_asset(repo: str, asset_re: str) -> tuple[dict, dict] | tuple[None, None]:
    """返回 (release, asset)：最近的非 draft/prerelease 版本里第一个匹配的 APK。"""
    releases = api_get(f"{API}/repos/{repo}/releases?per_page=20")
    if not isinstance(releases, list):
        return None, None
    pattern = re.compile(asset_re)
    for release in releases:
        if release.get("draft") or release.get("prerelease"):
            continue
        for asset in release.get("assets", []):
            if pattern.search(asset.get("name", "")):
                return release, asset
    return None, None


def download(url: str, dest: Path) -> int:
    request = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/octet-stream"})
    written = 0
    with urllib.request.urlopen(request, timeout=180) as response, dest.open("wb") as handle:
        while chunk := response.read(1 << 20):
            handle.write(chunk)
            written += len(chunk)
    return written


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def apk_meta(path: Path) -> tuple[str, int, str]:
    """读 package / versionCode / versionName。优先 aapt2/aapt，其次 androguard。"""
    aapt = os.environ.get("AAPT") or shutil.which("aapt") or shutil.which("aapt2")
    if aapt:
        result = subprocess.run([aapt, "dump", "badging", str(path)], capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            raise RuntimeError(f"aapt 读取失败：{result.stderr.strip()[:200]}")
        match = re.search(
            r"package: name='([^']+)' versionCode='([^']+)' versionName='([^']*)'",
            result.stdout,
        )
        if not match:
            raise RuntimeError("aapt 输出里没有 package 行")
        return match.group(1), int(match.group(2)), match.group(3)

    try:
        from androguard.core.apk import APK as AndroAPK
    except ImportError:
        try:
            from androguard.core.bytecodes.apk import APK as AndroAPK
        except ImportError as exc:  # pragma: no cover - 依赖缺失
            raise RuntimeError("需要 aapt/aapt2 或 androguard 才能读取 APK 元数据") from exc
    parsed = AndroAPK(str(path))
    return parsed.get_package(), int(parsed.get_androidversion_code()), parsed.get_androidversion_name()


def existing_version(path: Path) -> tuple[dict | None, int]:
    if not path.exists():
        return None, -1
    data = json.loads(path.read_text(encoding="utf-8"))
    return data, int(data.get("versionCode", -1))


def build_entry(
    config: dict,
    package: str,
    version_code: int,
    version_name: str,
    asset: dict,
    size: int,
    sha: str,
) -> dict:
    entry = {
        "schema": 1,
        "package": package,
        "name": config["name"],
        "versionCode": version_code,
        "versionName": version_name,
        "apk": {"url": asset["browser_download_url"], "size": size, "sha256": sha},
        "source": config["source"],
        "notes": config["notes"],
    }
    if config.get("min_sdk") is not None:
        entry["minSdk"] = config["min_sdk"]
    return entry


def update_one(config: dict, dry_run: bool) -> None:
    release, asset = pick_asset(config["repo"], config["asset_re"])
    if not release or not asset:
        raise RuntimeError(f"{config['repo']} 没有匹配 {config['asset_re']} 的非预发布资产")

    with tempfile.TemporaryDirectory(prefix="scooph-upstream-") as tmp:
        apk = Path(tmp) / "app.apk"
        size = download(asset["browser_download_url"], apk)
        actual_package, version_code, version_name = apk_meta(apk)

        expected = config.get("package")
        if expected and actual_package != expected:
            raise RuntimeError(f"包名不一致：期望 {expected}，APK 实际是 {actual_package}")

        # 没写死包名的条目（例如上游换过 fork）以 APK 里的包名为准；
        # 文件名必须等于 package，这是 bucket 的硬规则。
        entry_path = APPS / f"{actual_package}.json"
        _, old_code = existing_version(entry_path)
        if old_code >= version_code:
            print(f"[{actual_package}] 已是最新：{version_name} ({version_code})")
            return

        sha = sha256_of(apk)
        entry = build_entry(config, actual_package, version_code, version_name, asset, size, sha)
        print(
            f"[{actual_package}] {old_code} -> {version_code} ({version_name}) "
            f"{size} bytes {sha[:12]}… release={release.get('tag_name')}"
        )
        if dry_run:
            print(f"[{actual_package}] dry-run，不写文件")
            return
        APPS.mkdir(parents=True, exist_ok=True)
        entry_path.write_text(json.dumps(entry, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="只打印，不写文件")
    parser.add_argument("--only", help="只处理指定 package")
    args = parser.parse_args()

    failures = 0
    for config in UPSTREAMS:
        if args.only and config["package"] != args.only:
            continue
        try:
            update_one(config, args.dry_run)
        except Exception as exc:  # noqa: BLE001 - 单个上游失败继续处理其它上游
            failures += 1
            print(f"[{config['package']}] 失败：{exc}", file=sys.stderr)

    if failures:
        print(f"完成，{failures} 个上游失败", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
