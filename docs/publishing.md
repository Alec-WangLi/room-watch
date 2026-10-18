# 发版与发布规范

自动更新（`rw update`）不是「随便传个 exe」就能工作的：它按固定规则找 release、
按固定名字找安装包、按固定格式校验摘要。这份文档就是那些规则。

## 0. 首次推送到 GitHub

仓库已在本地初始化，主分支为 `main`，remote 指向
`https://github.com/Alec-WangLi/room-watch.git`。

在网页上新建仓库时：

- 名字 `room-watch`，**不要**勾「Add a README / .gitignore / license」——
  本地已经有内容，初始化了反而要处理冲突；
- 可见性按需要（公开即开源）。

然后三选一推送：

```bash
# 方式 A：PAT（把 <TOKEN> 换掉；建议用完就撤销该 token）
git push https://<TOKEN>@github.com/Alec-WangLi/room-watch.git main

# 方式 B：SSH（提前把公钥加到 GitHub）
git remote set-url origin git@github.com:Alec-WangLi/room-watch.git
git push -u origin main

# 方式 C：让 Git 凭据管理器弹窗
git push -u origin main     # 首次会弹出登录窗口
```

首次推送约 **52MB**（主要是 `models/` 的三个权重），慢一点正常。

推送前确认：

```bash
git log --oneline | wc -l          # 19 个提交
git status --short                  # 应该为空
git remote -v
```

> 历史在开源前用 `git-filter-repo` 重写过（清除真实 webhook key 与本机用户名路径），
> 因此所有 commit hash 与重写前不同。如果之前推送过，需要 `git push --force`，
> 且要意识到旧对象在 GitHub 上仍可能通过 API 访问到 —— 这种情况下正确答案是
> 删库重建，而不是强推。

## 1. 发版前检查

```bash
# 版本纪律：没有本人同意，只动最后一位
cat VERSION                 # 例如 0.1.3
cat VERSION-APPROVED        # 已批准的基线，例如 0.1
python -m roomwatch test --fast
```

`tests/test_paths.py::VersionTest` 会在前两位越过基线时判红，不要绕过它。

## 2. 出包

```powershell
# 免安装目录（同时产出 zip）
powershell -ExecutionPolicy Bypass -File scripts\build-portable.ps1

# 单文件安装包（自动更新用的就是它）
powershell -ExecutionPolicy Bypass -File scripts\build-installer.ps1
```

产物：

```
dist\room-watch-<版本>-setup.exe      ← 自动更新唯一认的资产
dist\room-watch-<版本>.zip            ← 给不想装的人
```

## 3. 生成校验文件（必须）

```powershell
$v = (Get-Content VERSION -Raw).Trim()
$exe = "dist\room-watch-$v-setup.exe"
$hash = (Get-FileHash $exe -Algorithm SHA256).Hash.ToLower()
"$hash  room-watch-$v-setup.exe" | Out-File -Encoding ascii -NoNewline "$exe.sha256"
```

`rw update` 默认 `require_checksum: true`：**没有 `.sha256` 资产就拒绝安装**。
这不是洁癖 —— 更新链路会下载并运行一个 exe，缺了摘要等于把「下载到什么就装什么」的口子打开。

## 4. 打 tag 并发布

```bash
git add -A
git commit -m "release: 0.1.4"
git tag v0.1.4          # 需要本人批准才能打 tag
git push origin main --tags
```

在 GitHub 上 **New release**：

| 字段 | 要求 |
|---|---|
| Tag | `v0.1.4`（必须以 `v` 开头，自动更新能从 tag 解析版本） |
| 标题 | 随便写，给人看 |
| 说明 | 写变更；可选在末尾写 `sha256: <64 位>` 作为第二重保障 |
| 资产 | `room-watch-0.1.4-setup.exe` + `room-watch-0.1.4-setup.exe.sha256` |
| 预发布 | 勾了 prerelease 就别指望稳定用户会自动升上来（当前策略不区分，谨慎勾） |

资产名必须严格是 `room-watch-<版本>-setup.exe`。自动更新用正则
`room-watch-(\d+\.\d+\.\d+)-setup\.exe$` 匹配，名字不对就找不到包。

## 5. 客户端会怎么走

`rw update`（或 `rw update --check`）：

1. 先请求 `api.github.com/repos/<repo>/releases/latest`；
2. 遇到 403/429（匿名 API 60 次/小时被打满）**自动退回**网页重定向
   `github.com/<repo>/releases/latest`，从 302 的 `Location` 里取 tag，不耗配额；
3. 版本比对后按纪律处置：

| 情况 | 行为 |
|---|---|
| 同一 `主.次`，修订号前进（0.1.3 → 0.1.4） | 自动下载并安装 |
| 跨次版本（0.1.4 → 0.2.0） | 打印说明，要求 `rw update --yes` |
| 跨主版本 | 同上 |
| 远端更旧 | 默认拒绝，要回滚得 `--force` |

4. 下载 → 校验 SHA256 → 停掉正在跑的监控 → 静默安装 → 重新拉起监控。

安装器会用同一 AppId 先跑旧版卸载器再装新版；卸载器刻意保留了
`config.local.yaml` 与 `data\`，所以升级不会丢配置和抓拍记录。

## 6. 哪些东西不要上传

仓库里已经用 `.gitignore` 挡住，这里再列一遍，避免有人 `git add -f`：

| 路径 | 原因 |
|---|---|
| `config.local.yaml` | 真实 webhook / 相册密码 |
| `data/` | 抓拍、录像、人脸特征（隐私 + 生物特征） |
| `var/` | 运行时状态、日志、数据库 |
| `dist/`、`build/` | 构建产物，几十上百 MB |
| `identity/`、`*.npz` | 人脸模板 |
| `.workbuddy/` | AI 工作区记忆 |
| `start-monitor.vbs` | 含本机绝对路径 |

发布安装包走 **Release 资产**，不要提交进仓库。
