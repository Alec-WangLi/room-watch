"""推送配置引导：一条命令配好，并立刻发一条测试消息验证。

    rw push                           # 交互式
    rw push --channel wxpusher --token AT_xxx --uid UID_xxx
    rw push --channel serverchan --sendkey SCTxxx
    rw push --channel wecom_bot --webhook https://qyapi.weixin.qq.com/...

凭据一律写进 `config.local.yaml`（不入库），`config.yaml` 里的占位串原样不动。
这样既不用重写带满中文注释的入库文件（早期用 yaml.safe_dump 会把注释全抹掉），
也不会让真实 webhook 混进 Git 历史。
"""

from __future__ import annotations

import argparse
import os
import sys

import requests
import yaml

CONFIG = "config.yaml"
LOCAL_CONFIG = "config.local.yaml"

LOCAL_HEADER = """# 本机配置覆盖（不入库）
# 由 `rw push` 生成：只放生效渠道与凭据，其余键仍以 config.yaml 为准。
# 要改采集/录像参数请改 config.yaml，不要在这里重复写，否则会覆盖主配置。
"""

CHANNELS = ("wxpusher", "serverchan", "wecom_bot")

WECOM_SEND_API = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key={key}"


def normalize_wecom_webhook(value: str) -> str:
    """接受完整 webhook 地址或裸 key，统一成标准地址。

    历史坑（2026-10-18 实际踩到）：旧版 write_config 只把输入替换掉
    `key=REPLACE_ME` 里的占位符，用户粘完整地址时就会变成
    `?key=https://qyapi.../send?key=xxx` 这种双嵌套。

    send 接口对这种畸形地址校验宽松——文字照样能发出去，看起来「测试成功」；
    但 upload_media 严格校验 key，直接返回 40058 invalid param 'key'，
    于是视频永远发不出去，而且没有任何明显症状。所以这里先剥出真正的 key。
    """
    v = (value or "").strip().strip('"').strip("'")
    if not v:
        return ""
    if "key=" in v:
        v = v.rsplit("key=", 1)[-1]  # 双嵌套时取最后一截
        v = v.split("&")[0].strip()
    if not v:
        return ""
    return WECOM_SEND_API.format(key=v)


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
    return {"webhook": normalize_wecom_webhook(hook)}


def write_config(channel: str, creds: dict) -> list:
    """把渠道与凭据写进 config.local.yaml，返回变更摘要。

    不碰 config.yaml：那是入库文件，凭据必须保持占位。
    已存在的 local 文件按 key 合并，所以换渠道不会把别的渠道凭据清掉。
    """
    data: dict = {}
    if os.path.exists(LOCAL_CONFIG):
        try:
            with open(LOCAL_CONFIG, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
        except (OSError, yaml.YAMLError):
            data = {}

    changes = [f"写入 {LOCAL_CONFIG}（不入库）"]
    notify = data.setdefault("notify", {})
    notify["channel"] = channel
    changes.append(f"生效渠道设为 {channel}")

    if channel == "wecom_bot":
        notify.setdefault("wecom_bot", {})["webhook"] = normalize_wecom_webhook(
            creds.get("webhook", "")
        )
        changes.append("已写入 webhook")
    elif channel == "wxpusher":
        block = notify.setdefault("wxpusher", {})
        block["app_token"] = creds.get("token", "")
        block["uid"] = creds.get("uid", "")
        changes.append("已写入 app_token / uid")
    else:
        notify.setdefault("serverchan", {})["sendkey"] = creds.get("sendkey", "")
        changes.append("已写入 sendkey")

    with open(LOCAL_CONFIG, "w", encoding="utf-8") as f:
        f.write(LOCAL_HEADER)
        yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)
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
        print(f"\n已写入 {LOCAL_CONFIG}。重启监控后生效。")
        return 0

    print("\n发送测试消息...")
    if send_test(channel, creds):
        print("✓ 成功！去手机上看看有没有收到。")
        print("\n注意：监控进程需要**重启一次**才会用上新配置。")
        if channel == "wecom_bot":
            print("建议再验证一次录像推送：rw push-video <某段 mp4>")
        return 0
    print("✗ 失败。检查上面的返回信息；凭据填错的话重新跑一次即可覆盖。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
