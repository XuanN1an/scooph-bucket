#!/usr/bin/env python3
"""校验 scooph-bucket 的清单格式。

与 App 里的 Go 读入器保持同一套硬性规则：
- bucket.json 存在且 schema=1
- apps/*.json 文件名必须等于 package
- package 是合法 applicationId
- sha256 为 64 位十六进制
- versionCode >= 1
- apk.url 为 http(s)

`--check-urls` 额外做一次 HEAD 探测；2xx/3xx/403 视为可达（部分 CDN 拒绝 HEAD）。
"""

from __future__ import annotations

import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z][A-Za-z0-9_]*)+$")
SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")


def fail(errors: list[str], path: Path, message: str) -> None:
    errors.append(f"{path.relative_to(ROOT)}: {message}")


def validate_bucket_json(errors: list[str]) -> None:
    path = ROOT / "bucket.json"
    if not path.is_file():
        fail(errors, path, "缺少 bucket.json")
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        fail(errors, path, f"JSON 解析失败：{exc}")
        return
    if data.get("schema") != 1:
        fail(errors, path, "schema 必须是 1")
    if not isinstance(data.get("name"), str) or not data["name"].strip():
        fail(errors, path, "name 必须是非空字符串")
    signature = data.get("signature", None)
    if signature is not None and not isinstance(signature, str):
        fail(errors, path, "signature 必须是字符串或 null")


def validate_entry(errors: list[str], path: Path, check_urls: bool) -> None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        fail(errors, path, f"JSON 解析失败：{exc}")
        return

    expected_package = path.stem
    package = data.get("package")
    if not isinstance(package, str) or not package:
        fail(errors, path, "package 必填")
    else:
        if not PACKAGE_RE.match(package):
            fail(errors, path, f"package 不是合法 applicationId：{package!r}")
        if package != expected_package:
            fail(errors, path, f"package ({package}) 必须等于文件名 ({expected_package})")

    if data.get("schema") != 1:
        fail(errors, path, "schema 必须是 1")
    if not isinstance(data.get("name"), str) or not data["name"].strip():
        fail(errors, path, "name 必须是非空字符串")

    version_code = data.get("versionCode")
    if not isinstance(version_code, int) or isinstance(version_code, bool) or version_code < 1:
        fail(errors, path, "versionCode 必须是 >= 1 的整数")
    if not isinstance(data.get("versionName"), str):
        fail(errors, path, "versionName 必须是字符串")

    apk = data.get("apk")
    if not isinstance(apk, dict):
        fail(errors, path, "apk 必须是对象")
        return
    sha = apk.get("sha256")
    if not isinstance(sha, str) or not SHA256_RE.match(sha):
        fail(errors, path, "apk.sha256 必须是 64 位十六进制")
    size = apk.get("size")
    if not isinstance(size, int) or isinstance(size, bool) or size < 0:
        fail(errors, path, "apk.size 必须是非负整数")
    url = apk.get("url")
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        fail(errors, path, "apk.url 必须是 http(s) URL")
    elif check_urls:
        check_url(errors, path, url)

    source = data.get("source")
    if source is not None and not isinstance(source, str):
        fail(errors, path, "source 必须是字符串或省略")
    min_sdk = data.get("minSdk")
    if min_sdk is not None and (not isinstance(min_sdk, int) or isinstance(min_sdk, bool) or min_sdk < 1):
        fail(errors, path, "minSdk 必须是正整数或省略")
    notes = data.get("notes")
    if notes is not None and not isinstance(notes, str):
        fail(errors, path, "notes 必须是字符串或省略")


def check_url(errors: list[str], path: Path, url: str) -> None:
    request = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "scooph-bucket-validator"})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            code = response.status
    except urllib.error.HTTPError as exc:
        code = exc.code
    except Exception as exc:  # noqa: BLE001 - 网络原因都按不可达处理
        fail(errors, path, f"apk.url 不可达：{exc}")
        return
    if not (200 <= code < 400 or code == 403):
        fail(errors, path, f"apk.url 返回 HTTP {code}")


def main() -> int:
    check_urls = "--check-urls" in sys.argv[1:]
    errors: list[str] = []
    validate_bucket_json(errors)

    apps = ROOT / "apps"
    if not apps.is_dir():
        fail(errors, apps, "缺少 apps/ 目录")
    else:
        entries = sorted(p for p in apps.glob("*.json") if p.is_file())
        for path in entries:
            validate_entry(errors, path, check_urls)
        print(f"检查 {len(entries)} 个条目")

    if errors:
        print("\n校验失败：", file=sys.stderr)
        for line in errors:
            print(f"  - {line}", file=sys.stderr)
        return 1
    print("校验通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
