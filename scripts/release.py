"""创建 GitHub Release 并上传安装包与校验文件。

自动更新依赖固定的资产命名：
  room-watch-<版本>-setup.exe
  room-watch-<版本>-setup.exe.sha256
两者都必须作为 release 资产存在，否则 `rw update` 找不到包或拒绝安装。

用法：
  set GH_TOKEN=ghp_xxx          （或 PowerShell: $env:GH_TOKEN="ghp_xxx"）
  python scripts/release.py

token 需要 repo 权限；若要顺带改 .github/workflows/ 下的文件，还要 workflow 权限。
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
REPO = os.environ.get("RW_REPO", "Alec-WangLi/room-watch")
API = "https://api.github.com"


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


def main() -> int:
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    tag = f"v{version}"
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
    sha_file.write_text(f"{digest}  {setup.name}", encoding="ascii")
    print(f"版本 {version}  安装包 {setup.stat().st_size / 1048576:.1f}MB")
    print(f"sha256 {digest}")

    head = requests.get(f"{API}/repos/{REPO}/commits/main",
                        headers=headers(), timeout=30)
    if head.status_code != 200:
        print(f"[失败] 取 main 提交失败 HTTP {head.status_code}: {head.text[:200]}")
        return 1
    commit_sha = head.json()["sha"]

    ref = requests.post(f"{API}/repos/{REPO}/git/refs",
                        headers=headers(), timeout=30,
                        data=json.dumps({"ref": f"refs/tags/{tag}", "sha": commit_sha}))
    if ref.status_code == 201:
        print(f"已创建 tag {tag} -> {commit_sha[:7]}")
    elif "already exists" in ref.text:
        print(f"tag {tag} 已存在，复用")
    else:
        print(f"[失败] 建 tag HTTP {ref.status_code}: {ref.text[:200]}")
        return 1

    notes = (
        f"room-watch {version}（测试版）\n\n"
        "Windows 单文件安装包，双击安装，装完自动弹出配置向导。\n"
        "客户机不需要预装 Python / ffmpeg，安装包内含运行时与模型。\n\n"
        f"sha256: {digest}\n"
    )
    rel = requests.post(f"{API}/repos/{REPO}/releases",
                        headers=headers(), timeout=60,
                        data=json.dumps({
                            "tag_name": tag,
                            "name": f"room-watch {version}",
                            "body": notes,
                            "draft": False,
                            "prerelease": False,
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
