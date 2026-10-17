"""推送配置引导：一条命令配好，并立刻发一条测试消息验证。

    python setup_push.py              # 交互式
    python setup_push.py --channel wxpusher --token AT_xxx --uid UID_xxx
    python setup_push.py --channel serverchan --sendkey SCTxxx
    python setup_push.py --channel wecom_bot --webhook https://qyapi.weixin.qq.com/...

config.yaml 里全是中文注释，直接用 yaml 库重写会把注释全部抹掉，
所以这里做的是**精准文本替换**：只替换占位符和 channel 那一行。
"""

from __future__ import annotations

import argparse
import re
import sys

import requests

CONFIG = "config.yaml"

PLACEHOLDERS = {
    "wxpusher": [("AT_xxx_replace_me", "app_token"), ("UID_xxx_replace_me", "uid")],
    "serverchan": [("SCTxxx_replace_me", "sendkey")],
    "wecom_bot": [("REPLACE_ME", "webhook")],
}

CHANNELS = ("wxpusher", "serverchan", "wecom_bot")


def ask(prompt: str, default: str = "") -> str:
    try:
        v = input(prompt).strip()
    except EOFError:
        return default
    return v or default


def pick_channel() -> str:
    print("选择推送渠道：")
    print("  1. WxPusher（微信，只能发文字）")
    print("  2. Server酱（微信服务号，只能发文字）")
    print("  3. 企业微信群机器人（能把照片直接发到微信，推荐）")
    while True:
        c = ask("输入 1/2/3 [3]: ", "3")
        if c in ("1", "wxpusher"):
            return "wxpusher"
        if c in ("2", "serverchan"):
            return "serverchan"
        if c in ("3", "wecom_bot"):
            return "wecom_bot"
        print("没看懂，再输一次。")


def collect_creds(channel: str, args) -> dict:
    if channel == "wxpusher":
        token = args.token or ask("粘贴 WxPusher appToken（AT_ 开头）: ")
        uid = args.uid or ask("粘贴你的 UID（UID_ 开头）: ")
        return {"token": token, "uid": uid}
    if channel == "serverchan":
        key = args.sendkey or ask("粘贴 Server酱 SendKey（SCT 开头）: ")
        return {"sendkey": key}
    hook = args.webhook or ask("粘贴企业微信机器人 Webhook 地址: ")
    return {"webhook": hook}


def write_config(channel: str, creds: dict) -> list:
    """精准替换占位符和 channel 行，保住所有注释。返回替换摘要。"""
    with open(CONFIG, "r", encoding="utf-8") as f:
        text = f.read()

    changes = []
    for old, name in PLACEHOLDERS[channel]:
        if old not in text:
            changes.append(f"跳过 {name}：配置里找不到占位符（可能已填过）")
            continue
        value = creds.get(
            "token" if name == "app_token" else name, ""
        )
        text = text.replace(old, value)
        changes.append(f"已写入 {name}")

    # 把生效渠道改成所选的
    text, n = re.subn(r"^(\s*channel:\s*)\S+", rf"\g<1>{channel}", text, count=1, flags=re.M)
    if n:
        changes.append(f"生效渠道设为 {channel}")
    else:
        changes.append("警告：没找到 channel 行，请手动检查")

    with open(CONFIG, "w", encoding="utf-8") as f:
        f.write(text)
    return changes


def send_test(channel: str, creds: dict) -> bool:
    title = "room-watch 推送测试"
    body = "收到这条消息，说明推送配置成功。有人进入房间时你会收到类似提醒。"
    try:
        if channel == "wxpusher":
            r = requests.post(
                "https://wxpusher.zjiecode.com/api/send/message",
                json={
                    "appToken": creds["token"],
                    "content": body,
                    "summary": title,
                    "contentType": 1,
                    "uids": [creds["uid"]],
                },
                timeout=10,
            )
            ok = r.status_code == 200 and r.json().get("code") == 1000
            if not ok:
                print("WxPusher 返回:", r.text[:300])
            return ok
        if channel == "serverchan":
            r = requests.post(
                f"https://sctapi.ftqq.com/{creds['sendkey']}.send",
                data={"title": title, "desp": body},
                timeout=10,
            )
            ok = r.status_code == 200 and r.json().get("code") == 0
            if not ok:
                print("Server酱 返回:", r.text[:300])
            return ok
        if channel == "wecom_bot":
            r = requests.post(
                creds["webhook"],
                json={"msgtype": "text", "text": {"content": f"{title}\n{body}"}},
                timeout=10,
            )
            ok = r.status_code == 200 and r.json().get("errcode") == 0
            if not ok:
                print("企业微信 返回:", r.text[:300])
            return ok
    except Exception as exc:
        print("发送异常:", exc)
    return False


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="推送配置引导")
    ap.add_argument("--channel", choices=CHANNELS)
    ap.add_argument("--token")
    ap.add_argument("--uid")
    ap.add_argument("--sendkey")
    ap.add_argument("--webhook")
    ap.add_argument("--no-test", action="store_true", help="只写配置，不发测试消息")
    args = ap.parse_args(argv)

    channel = args.channel or pick_channel()
    creds = collect_creds(channel, args)

    missing = [k for k, v in creds.items() if not v]
    if missing:
        print(f"缺少 {missing}，什么都没改。")
        return 2

    print()
    for line in write_config(channel, creds):
        print(" ", line)

    if args.no_test:
        print("\n已写入配置。重启 watcher.py 后生效。")
        return 0

    print("\n发送测试消息...")
    if send_test(channel, creds):
        print("✓ 成功！去手机上看看有没有收到。")
        print("\n注意：watcher.py 需要**重启一次**才会用上新配置。")
        return 0
    print("✗ 失败。检查上面的返回信息；凭据填错的话重新跑一次即可覆盖。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
