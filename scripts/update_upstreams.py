#!/usr/bin/env python3
"""自动跟踪 apps/ 里所有条目的上游更新（Scoop bucket 的 checkver/autoupdate 思路）。

规则：
1. 遍历 apps/*.json，从 `apk.url` 或 `source` 推断 GitHub 仓库；
2. 不依赖任何中央配置——只要条目在 apps/ 里就会被尝试；
3. 取最近的、非 draft / 非 prerelease 且带 APK 资产的 Release；
4. 资产名匹配：`upstream.asset_re` > 当前资产名归一化后相等 > Release 里只有一个 APK；
5. 下载后重新算 size/sha256，用 aapt2/androguard 读 versionCode；
6. 只有 versionCode 变大才写回；包名对不上直接跳过，不写错误条目；
7. 非 GitHub 上游（例如官网直链）打印「跳过」并继续，不算失败。

`upstream` 是可选覆盖字段（文件格式允许未知字段）：

    "upstream": {"repo": "owner/repo", "asset_re": "Kazumi_android_.*\\.apk$"}

`SEEDS` 只用于「条目还不存在」的新应用引导：种子里不写包名，脚本从 APK 里读，
写进 apps/<package>.json；之后每次都由上面的通用流程接管。

用法：
    python scripts/update_upstreams.py            # 更新所有
    python scripts/update_upstreams.py --dry-run
    python scripts/update_upstreams.py --only com.predidit.kazumi
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

# 只用于「apps/ 里还没有这个应用」时的首次创建。已有条目一律走通用推断。
SEEDS: list[dict] = [
    {
        "repo": "xiaojieonly/Ehviewer_CN_SXJ",
        "asset_re": r"(?i)\.apk$",
        "name": "EhViewer",
        "source": "https://github.com/xiaojieonly/Ehviewer_CN_SXJ",
        "notes": "EhViewer（Ehviewer_CN_SXJ）",
    },
]

RELEASE_URL_RE = re.compile(r"https?://github\.com/([^/]+)/([^/]+)/releases/download/", re.I)
REPO_URL_RE = re.compile(r"https?://github\.com/([^/]+)/([^/]+?)(?:\.git)?(?:/.*)?$", re.I)
APK_NAME_RE = re.compile(r"\.apk$", re.I)


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


def download(url: str, dest: Path) -> int:
    request = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/octet-stream"})
    written = 0
    with urllib.request.urlopen(request, timeout=300) as response, dest.open("wb") as handle:
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
        result = subprocess.run([aapt, "dump", "badging", str(path)], capture_output=True, text=True, timeout=180)
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


# ---------------------------------------------------------------- 上游推断


def infer_repo(*urls: str) -> str | None:
    """从 Release 资产地址或 source 页面推断 owner/repo。"""
    for url in urls:
        if not url:
            continue
        match = RELEASE_URL_RE.search(url) or REPO_URL_RE.search(url)
        if match:
            return f"{match.group(1)}/{match.group(2)}"
    return None


def normalize_asset(name: str) -> str:
    """去掉版本号与标点，得到跨版本稳定的资产名指纹。"""
    without_version = re.sub(r"\d+(?:\.\d+)*", "", name.lower())
    return re.sub(r"[^a-z]+", "", without_version)


def pick_asset(
    releases: list[dict],
    current_name: str,
    asset_re: str | None,
) -> tuple[dict, dict] | tuple[None, None]:
    pattern = re.compile(asset_re) if asset_re else None
    wanted = normalize_asset(current_name)
    for release in releases:
        if release.get("draft") or release.get("prerelease"):
            continue
        apks = [a for a in release.get("assets", []) if APK_NAME_RE.search(a.get("name", ""))]
        if not apks:
            continue
        if pattern:
            matches = [a for a in apks if pattern.search(a["name"])]
        else:
            matches = [a for a in apks if normalize_asset(a["name"]) == wanted]
        # 上游改过命名（例如 EhViewer-2.0.1.5.apk -> 2.0.2.3.apk）时，
        # 只要这个 Release 只有一个 APK，就认为它就是要找的包。
        if not matches and len(apks) == 1:
            matches = apks
        if matches:
            return release, matches[0]
    return None, None


_RELEASE_CACHE: dict[str, list[dict]] = {}


def list_releases(repo: str) -> list[dict]:
    if repo in _RELEASE_CACHE:
        return _RELEASE_CACHE[repo]
    releases = api_get(f"{API}/repos/{repo}/releases?per_page=20")
    result = releases if isinstance(releases, list) else []
    _RELEASE_CACHE[repo] = result
    return result


# ---------------------------------------------------------------- 条目处理


def load_entry(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - 坏文件不应拖垮整轮
        print(f"[{path.name}] 读取失败：{exc}", file=sys.stderr)
        return None


def build_entry(
    data: dict,
    version_code: int,
    version_name: str,
    asset: dict,
    size: int,
    sha: str,
) -> dict:
    entry = dict(data)
    entry["versionCode"] = version_code
    entry["versionName"] = version_name
    entry["apk"] = {"url": asset["browser_download_url"], "size": size, "sha256": sha}
    entry.setdefault("schema", 1)
    entry.setdefault("package", data["package"])
    return entry


def update_entry(path: Path, data: dict, dry_run: bool) -> None:
    package = data["package"]
    apk = data.get("apk") or {}
    repo = (data.get("upstream") or {}).get("repo") or infer_repo(apk.get("url", ""), data.get("source", ""))
    if not repo:
        print(f"[{package}] 跳过：不是 GitHub 上游（{data.get('source') or apk.get('url')}）")
        return
    asset_re = (data.get("upstream") or {}).get("asset_re")

    releases = list_releases(repo)
    release, asset = pick_asset(releases, Path(apk.get("url", "")).name, asset_re)
    if not release or not asset:
        print(f"[{package}] 跳过：{repo} 最近版本里没有匹配的 APK 资产")
        return
    # 资产地址没变说明上游还没发新包，直接跳过，省掉一次完整下载。
    if asset["browser_download_url"] == apk.get("url"):
        print(f"[{package}] 已是最新：资产地址未变（{asset['name']}）")
        return

    with tempfile.TemporaryDirectory(prefix="scooph-upstream-") as tmp:
        file = Path(tmp) / "app.apk"
        size = download(asset["browser_download_url"], file)
        actual_package, version_code, version_name = apk_meta(file)
        if actual_package != package:
            print(f"[{package}] 跳过：下载到的 APK 实际包名是 {actual_package}")
            return
        if version_code <= int(data.get("versionCode", -1)):
            print(f"[{package}] 已是最新：{version_name} ({version_code})")
            return

        sha = sha256_of(file)
        updated = build_entry(data, version_code, version_name, asset, size, sha)
        print(
            f"[{package}] {data.get('versionCode')} -> {version_code} ({version_name}) "
            f"{size} bytes {sha[:12]}… release={release.get('tag_name')} repo={repo}"
        )
        if dry_run:
            print(f"[{package}] dry-run，不写文件")
            return
        path.write_text(json.dumps(updated, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def seed_entry(seed: dict, dry_run: bool) -> None:
    """给 apps/ 里还没有的应用创建第一条条目。"""
    existing = [
        json.loads(p.read_text(encoding="utf-8"))
        for p in APPS.glob("*.json")
        if not p.name.startswith("_")
    ]
    if any(entry.get("source") == seed["source"] for entry in existing):
        return  # 已经引导过了

    releases = list_releases(seed["repo"])
    release, asset = pick_asset(releases, "app.apk", seed.get("asset_re"))
    if not release or not asset:
        print(f"[seed] {seed['repo']} 没有可用的 APK 资产")
        return

    with tempfile.TemporaryDirectory(prefix="scooph-seed-") as tmp:
        file = Path(tmp) / "app.apk"
        size = download(asset["browser_download_url"], file)
        package, version_code, version_name = apk_meta(file)
        target = APPS / f"{package}.json"
        if target.exists():
            return
        sha = sha256_of(file)
        data = {
            "schema": 1,
            "package": package,
            "name": seed["name"],
            "versionCode": version_code,
            "versionName": version_name,
            "apk": {"url": asset["browser_download_url"], "size": size, "sha256": sha},
            "source": seed["source"],
            "notes": seed["notes"],
        }
        if seed.get("min_sdk") is not None:
            data["minSdk"] = seed["min_sdk"]
        print(
            f"[seed] {package} {version_code} ({version_name}) {size} bytes "
            f"release={release.get('tag_name')} -> {target.name}"
        )
        if dry_run:
            print(f"[seed] dry-run，不写文件")
            return
        APPS.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="只打印，不写文件")
    parser.add_argument("--only", help="只处理指定 package")
    parser.add_argument("--no-seeds", action="store_true", help="不处理引导用的新应用种子")
    args = parser.parse_args()

    failures = 0
    if not args.no_seeds:
        for seed in SEEDS:
            try:
                seed_entry(seed, args.dry_run)
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"[seed] {seed['repo']} 失败：{exc}", file=sys.stderr)

    for path in sorted(APPS.glob("*.json")):
        if path.name.startswith("_"):
            continue
        data = load_entry(path)
        if not data:
            failures += 1
            continue
        if args.only and data.get("package") != args.only:
            continue
        try:
            update_entry(path, data, args.dry_run)
        except Exception as exc:  # noqa: BLE001 - 单个条目失败继续处理其它条目
            failures += 1
            print(f"[{data.get('package', path.name)}] 失败：{exc}", file=sys.stderr)

    if failures:
        print(f"完成，{failures} 个条目失败", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
