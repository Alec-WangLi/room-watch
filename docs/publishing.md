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

## 0.1 启用 CI（一次性，30 秒）

`.github/workflows/` 下的文件受 GitHub 保护：**OAuth App 的 token 没有 `workflow`
权限时，push 会被整条拒绝**（`refusing to allow an OAuth App to create or update
workflow ... without workflow scope`）。所以 CI 定义暂时放在 `docs/ci-tests.yml`。

启用方式（不需要任何 token 权限，用网页即可）：

1. 打开 <https://github.com/Alec-WangLi/room-watch/contents/.github/workflows>
2. `Add file` → `Create new file`，文件名填 `tests.yml`
3. 把 `docs/ci-tests.yml` 的内容整段粘进去，提交

之后想继续用命令行改 CI，需要一个带 `workflow` scope 的 classic PAT
（Settings → Developer settings → Tokens (classic) → 勾 `repo` + `workflow`）。

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

### 方式 A：一条命令（推荐）

```bash
# token 需要 repo 权限；只发 Release 不需要 workflow 权限
export GH_TOKEN=ghp_xxx          # PowerShell: $env:GH_TOKEN="ghp_xxx"

# 稳定版：取 main 的提交，tag v<版本>，普通 Release
python scripts/release.py --channel stable

# 测试版：取 beta 的提交，tag v<版本>-beta，勾选 prerelease
python scripts/release.py --channel beta
```

脚本会：读 `VERSION` → 算安装包 SHA256 并写 `.sha256` → 在对应分支
（`main` / `beta`）的当前提交上建 tag → 建 Release → 上传两个资产。
**幂等**：重复执行只补缺失的资产；若已有 tag/release 指向别的提交或
prerelease 标志与通道不符，脚本会拒绝复用。可先加 `--dry-run` 预览。

### 方式 B：手动

```bash
git add -A
git commit -m "release: 0.1.4"
git tag v0.1.4          # 需要本人批准才能打 tag
git push origin main --tags
```

在 GitHub 上 **New release**：

| 字段 | 要求 |
|---|---|
| Tag | 稳定版 `v0.1.4`；测试版 `v0.1.4-beta`（自动更新能从 tag 解析版本） |
| 标题 | 稳定版 / 测试版要写清楚，给人看 |
| 说明 | 写变更；可选在末尾写 `sha256: <64 位>` 作为第二重保障 |
| 资产 | `room-watch-0.1.4-setup.exe` + `room-watch-0.1.4-setup.exe.sha256` |
| 预发布 | 测试版**必须勾**；稳定版**绝不勾**。`/releases/latest` 不返回预发布，所以稳定用户不会被推测试版 |

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

4. 下载 → 校验 SHA256 → 停掉正在跑的监控 → **交接安装**（0.1.6 起）→ 按场景拉起界面/监控。

   交接安装：安装包先复制到 `%TEMP%`，由 `updater.handoff_install()` 生成一个纯 ASCII
   的 `cmd` 脚本，等更新器进程退出后再跑安装器。**不能让更新器自己调 `setup.exe`** ——
   它用的是包内 `runtime\python`，Inno 的 RestartManager 认为它占着待替换的 dll、
   又关不掉父进程，静默模式 30 秒后按 Abort 处理（安装器返回 5 并回滚）。

安装器会用同一 AppId 先跑旧版卸载器再装新版；卸载器刻意保留了
`config.local.yaml` 与 `data\`，所以升级不会丢配置和抓拍记录。

> **历史坑（0.1.5 及更早）**：那时是同步调 `setup.exe`，在真实安装版上
> 100% 失败（rc=5）。升到 0.1.6 必须手工装一次，那之后链路才可用。

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
