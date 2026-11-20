# 贡献指南

## 加一个应用

1. Fork 本仓库，新建分支。
2. 把 `templates/app.json` 复制到 `apps/<packageName>.json`。
3. 填写真实数据：
   - `package`：应用的 applicationId（必须与文件名一致）
   - `versionCode` / `versionName`：来自 APK 的 `versionCode` / `versionName`
   - `apk.url`：手机能直接下载的 APK 地址（优先应用官方发布页或 F-Droid 镜像）
   - `apk.size` / `apk.sha256`：按下载到的文件计算：
     ```bash
     sha256sum app.apk
     stat -c%s app.apk
     ```
4. 本地校验：
   ```bash
   make validate
   make validate-urls   # 有网络时建议也跑一次
   ```
5. 提交 PR，CI 会再跑一遍同样的校验。

## 约定

- 一个应用一个文件，文件名 = `package`，不要放多个应用到一个文件里。
- `sha256` 必须是真实 APK 的值，不能填占位符——App 会用它校验下载。
- 不要改别人的条目来「降版本」；发布新版本时，把同一个文件里的 `versionCode`、`versionName`、`apk` 一起更新。
- 同一个应用如果由多个来源维护，合并在一个条目里，`source` 填最上游的仓库。
