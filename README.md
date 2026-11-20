# scooph-bucket

Scooph 的官方应用清单仓库。一个应用一个 JSON 文件，App 运行时会从这个仓库拉取清单。

- 格式规范见本 README 与 `schema/entry.schema.json`
- 想加应用：在 `apps/` 下新建 `<packageName>.json`，跑 `make validate`，然后开 PR
- CI 会校验格式；不符合规范的 PR 直接红，不需要人工检查格式

## 布局

```
bucket.json
apps/<packageName>.json
schema/entry.schema.json
scripts/validate.py
```

`bucket.json`：

```json
{
  "schema": 1,
  "name": "Scooph 官方 bucket",
  "maintainer": "Scooph",
  "signature": null
}
```

`apps/<packageName>.json`（文件名必须等于 `package`）：

```json
{
  "schema": 1,
  "package": "com.fsck.k9",
  "name": "K-9 Mail",
  "versionCode": 7000,
  "versionName": "7.000",
  "apk": {
    "url": "https://example.org/k9-7000.apk",
    "size": 6512345,
    "sha256": "9f2c…（64 位十六进制）"
  },
  "source": "https://github.com/thundernest/k-9",
  "minSdk": 26,
  "notes": "可选"
}
```

规则：

- `sha256` 必填，64 位十六进制；缺失或格式错误整条丢弃
- 未知字段忽略，方便格式演进
- `versionCode` 是唯一可靠的版本序，`versionName` 只用于显示
- `package` 必须是合法 applicationId，也是与本机应用比对的唯一键
- `apk.url` 必须是 http(s)，且手机能访问

## 本地校验

```bash
make validate        # 只校验格式
make validate-urls   # 额外检查 apk.url 可达（需要网络）
```

只依赖 Python 3，不需要 Go 或 Android 工具链。

## 添加一个应用

1. 找到应用的官方发布页与 APK 下载地址（建议作者自己的发布页或 F-Droid 镜像）
2. 计算 `sha256` 与文件大小
3. 复制 `apps/_template.json` 为 `apps/<packageName>.json` 并填写
4. `make validate` 通过后提交 PR

## License

本仓库采用 GNU General Public License v3.0（见 `LICENSE`）。
条目内容归各自应用作者。
