"""创建 GitHub Release 并上传安装包与校验文件。

两个发布通道，不要发错：

  稳定版  --channel stable  → 取 main 的提交，tag v<版本>，普通 Release。
          内置自动更新（/releases/latest）能看到它。
  测试版  --channel beta    → 取 beta 的提交，tag v<版本>-beta，勾选 prerelease。
          GitHub 的 /releases/latest 不返回预发布，所以稳定用户不会被推测试版；
          测试者手动下载安装。

自动更新依赖固定的资产命名（两个通道一致）：
  room-watch-<版本>-setup.exe
  room-watch-<版本>-setup.exe.sha256
两者都必须作为 release 资产存在，否则 `rw update` 找不到包或拒绝安装。

用法：
  set GH_TOKEN=ghp_xxx          （或 PowerShell: $env:GH_TOKEN="ghp_xxx"）
  python scripts/release.py --channel stable
  python scripts/release.py --channel beta
  python scripts/release.py --channel beta --dry-run

token 需要 repo 权限；若要顺带改 .github/workflows/ 下的文件，还要 workflow 权限。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
REPO = os.environ.get("RW_REPO", "Alec-WangLi/room-watch")
API = "https://api.github.com"

CHANNELS = {
    "stable": {"branch": "main", "prerelease": False, "label": "稳定版（main 分支）"},
    "beta": {"branch": "beta", "prerelease": True, "label": "测试版（beta 分支）"},
}


def token() -> str:
    """优先读环境变量；退回到 build/gh-token.tmp（本地设备流授权产物，不入库）。"""
    env = (os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or "").strip()
    if env:
        return env
    fallback = ROOT / "build" / "gh-token.tmp"
    if fallback.exists():
        return fallback.read_text(encoding="utf-8").strip()
    raise SystemExit("缺少 GH_TOKEN：请设置环境变量，或先完成设备授权生成 build/gh-token.tmp")


def headers() -> dict:
    return {
        "Authorization": f"token {token()}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "room-watch-release",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="发布 room-watch 的 GitHub Release")
    parser.add_argument("--channel", choices=tuple(CHANNELS), default="stable",
                        help="stable=main 普通 Release；beta=beta 预发布")
    parser.add_argument("--tag", default=None,
                        help="覆盖 tag（默认 stable=v<版本>，beta=v<版本>-beta）")
    parser.add_argument("--version", default=None, help="覆盖 VERSION 文件里的版本号")
    parser.add_argument("--dry-run", action="store_true", help="只打印将要做的事，不写 GitHub")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    channel = CHANNELS[args.channel]
    version = args.version or (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    tag = args.tag or (f"v{version}" if args.channel == "stable"
                       else f"v{version}-beta")
    setup = ROOT / "dist" / f"room-watch-{version}-setup.exe"
    sha_file = setup.with_suffix(setup.suffix + ".sha256")
    if not setup.exists():
        print(f"[失败] 找不到安装包：{setup}")
        return 1

    sha = hashlib.sha256()
    with open(setup, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            sha.update(chunk)
    digest = sha.hexdigest()
    print(f"通道 {args.channel}（{channel['label']}）")
    print(f"版本 {version}  安装包 {setup.stat().st_size / 1048576:.1f}MB")
    print(f"tag  {tag}  prerelease={channel['prerelease']}")
    print(f"sha256 {digest}")

    head = requests.get(f"{API}/repos/{REPO}/commits/{channel['branch']}",
                        headers=headers(), timeout=30)
    if head.status_code != 200:
        print(f"[失败] 取 {channel['branch']} 提交失败 HTTP {head.status_code}: {head.text[:200]}")
        return 1
    commit_sha = head.json()["sha"]
    if args.dry_run:
        print(f"[dry-run] 将在 {channel['branch']} 的 {commit_sha[:7]} 上创建 {tag}")
        print("[dry-run] 不上传、不写 GitHub")
        return 0

    sha_file.write_text(f"{digest}  {setup.name}", encoding="ascii")

    ref = requests.post(f"{API}/repos/{REPO}/git/refs",
                        headers=headers(), timeout=30,
                        data=json.dumps({"ref": f"refs/tags/{tag}", "sha": commit_sha}))
    if ref.status_code == 201:
        print(f"已创建 tag {tag} -> {commit_sha[:7]}")
    elif "already exists" in ref.text:
        got = requests.get(f"{API}/repos/{REPO}/git/ref/tags/{tag}",
                           headers=headers(), timeout=30)
        existing_sha = ((got.json().get("object") or {}).get("sha")
                        if got.status_code == 200 else None)
        if existing_sha and existing_sha != commit_sha:
            print(f"[失败] tag {tag} 已存在但指向 {existing_sha[:7]}，"
                  f"不是 {channel['branch']} 的 {commit_sha[:7]}；拒绝覆盖")
            return 1
        print(f"tag {tag} 已存在，复用")
    else:
        print(f"[失败] 建 tag HTTP {ref.status_code}: {ref.text[:200]}")
        return 1

    notes = (
        f"room-watch {version} {channel['label']}\n\n"
        "Windows 单文件安装包，双击安装，装完自动弹出配置向导。\n"
        "客户机不需要预装 Python / ffmpeg，安装包内含运行时与模型。\n\n"
        f"sha256: {digest}\n"
    )
    if channel["prerelease"]:
        notes += ("\n这是预发布版本：内置自动更新默认不收取预发布，"
                  "请手动下载安装；稳定用户不会被推到这个版本。\n")

    rel = requests.post(f"{API}/repos/{REPO}/releases",
                        headers=headers(), timeout=60,
                        data=json.dumps({
                            "tag_name": tag,
                            "name": f"room-watch {version} "
                                    + ("测试版" if channel["prerelease"] else "稳定版"),
                            "body": notes,
                            "draft": False,
                            "prerelease": channel["prerelease"],
                        }))
    if rel.status_code == 201:
        info = rel.json()
    elif rel.status_code == 422 and "already_exists" in rel.text:
        # 重跑幂等：release 已存在就复用，只补缺失的资产
        got = requests.get(f"{API}/repos/{REPO}/releases/tags/{tag}",
                           headers=headers(), timeout=30)
        if got.status_code != 200:
            print(f"[失败] release 已存在但取不回来 HTTP {got.status_code}")
            return 1
        info = got.json()
        if bool(info.get("prerelease")) != channel["prerelease"]:
            print(f"[失败] release {tag} 已存在但 prerelease="
                  f"{info.get('prerelease')}，与通道 {args.channel} 不符；拒绝复用")
            return 1
        print(f"release {tag} 已存在，复用")
    else:
        print(f"[失败] 建 release HTTP {rel.status_code}: {rel.text[:300]}")
        return 1

    upload_url = info["upload_url"].split("{")[0]
    existing = {a["name"] for a in info.get("assets", [])}
    print(f"release: {info['html_url']}")

    for path in (setup, sha_file):
        if path.name in existing:
            print(f"跳过（已存在）{path.name}")
            continue
        size = path.stat().st_size
        print(f"上传 {path.name} ({size / 1048576:.1f}MB) …", flush=True)
        with open(path, "rb") as fh:
            resp = requests.post(
                upload_url,
                params={"name": path.name},
                headers={**headers(), "Content-Type": "application/octet-stream"},
                data=fh,
                timeout=1800,
            )
        if resp.status_code in (200, 201):
            print(f"  ok  {resp.json().get('browser_download_url')}")
        else:
            print(f"  失败 HTTP {resp.status_code}: {resp.text[:200]}")
            return 1

    print("完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
