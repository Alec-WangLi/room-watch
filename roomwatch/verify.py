"""「真的在工作」验证：把首次配置后最该确认的几件事一次跑完。

检查项：
- 启动自检（模型、目录、磁盘、推送凭据）
- 摄像头能否真的出画面（监控运行中时跳过，设备被占用）
- 每个启用渠道能否真的收到一条消息
- 配了外部心跳时，URL 是否可达（可选，失败不阻塞）

判定规则：**必检项**（摄像头、推送、自检）全部通过才算 ok；
外部心跳是可选保障，失败只报告不否决。所有外部动作都可注入，
测试不需要真实摄像头、网络或模型。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Callable, Optional, Sequence

__all__ = ["run_checks", "main"]


def _selfcheck_items(cfg: dict, root: Path) -> list[dict]:
    from . import selfcheck

    try:
        results = selfcheck.run(cfg, root)
    except Exception as exc:  # noqa: BLE001 - 自检失败本身也要变成一条结果
        return [{"name": "自检", "ok": False, "detail": f"自检异常：{exc}",
                 "required": True}]
    return [
        {"name": f"自检 · {item.name}", "ok": bool(item.ok),
         "detail": item.detail, "required": True}
        for item in results
    ]


def _camera_item(config_path: Path, probe: Optional[Callable]) -> dict:
    if probe is None:
        from . import wizard

        probe = lambda path: wizard.probe_camera(Path(path))  # noqa: E731
    try:
        ok, detail = probe(config_path)
    except Exception as exc:  # noqa: BLE001
        ok, detail = False, f"探测异常：{exc}"
    return {
        "name": "摄像头",
        "ok": bool(ok),
        "detail": detail or ("已拍到画面" if ok else "打不开采集源"),
        "required": True,
    }


def _push_items(cfg: dict, notifier=None) -> list[dict]:
    from .delivery import enabled_channels

    notify = cfg.get("notify") or {}
    channels = enabled_channels(notify)
    if not channels:
        return [{
            "name": "推送",
            "ok": False,
            "detail": "没有启用任何推送渠道（告警不会送达）",
            "required": True,
        }]

    if notifier is None:
        from .notifier import Notifier

        notifier = Notifier(notify)
    body = ("room-watch 验证消息\n"
            "收到这条消息，说明推送渠道在当前网络下可用。")
    items = []
    for channel in channels:
        try:
            result = notifier.deliver(channel, "text", {"text": body})
            ok, detail = bool(result.ok), (result.error or "测试消息已送达")
        except Exception as exc:  # noqa: BLE001 - 单个渠道异常不影响其他渠道
            ok, detail = False, f"发送异常：{exc}"
        items.append({
            "name": f"推送 · {channel}",
            "ok": ok,
            "detail": detail,
            "required": True,
        })
    return items


def _heartbeat_item(cfg: dict, ping: Optional[Callable]) -> Optional[dict]:
    from .heartbeat import load_heartbeat, ping as default_ping

    hb = load_heartbeat(cfg)
    if not hb.configured:
        return None
    ping = ping or default_ping
    try:
        ok, message = ping(hb.url, timeout=hb.timeout_seconds)
    except Exception as exc:  # noqa: BLE001
        ok, message = False, f"心跳异常：{exc}"
    return {
        "name": "外部心跳",
        "ok": bool(ok),
        "detail": message or ("心跳成功" if ok else "心跳失败"),
        "required": False,
    }


def run_checks(
    root: str | Path,
    config_path: Optional[str | Path] = None,
    *,
    include_camera: bool = True,
    include_push: bool = True,
    include_heartbeat: bool = True,
    selfcheck_items: Optional[Sequence[dict] | Callable[[], Sequence[dict]]] = None,
    probe: Optional[Callable] = None,
    notifier=None,
    ping: Optional[Callable] = None,
) -> dict:
    """跑一遍验证，返回 {ok, items, checked_at, version}。"""
    from . import __version__
    from .config import load_config

    root = Path(root)
    config_path = Path(config_path) if config_path else root / "config.yaml"
    cfg = load_config(str(config_path))

    items: list[dict] = []
    if selfcheck_items is None:
        items.extend(_selfcheck_items(cfg, root))
    elif callable(selfcheck_items):
        items.extend(selfcheck_items())
    else:
        items.extend(selfcheck_items)

    if include_camera:
        items.append(_camera_item(config_path, probe))
    else:
        items.append({"name": "摄像头", "ok": True, "skipped": True,
                      "detail": "已跳过（监控运行中，设备被占用）",
                      "required": False})

    if include_push:
        items.extend(_push_items(cfg, notifier=notifier))
    else:
        items.append({"name": "推送", "ok": True, "skipped": True,
                      "detail": "已跳过", "required": False})

    if include_heartbeat:
        item = _heartbeat_item(cfg, ping)
        if item is not None:
            items.append(item)

    ok = all(item["ok"] for item in items
             if item.get("required", False) and not item.get("skipped"))
    return {
        "ok": bool(ok),
        "items": items,
        "checked_at": datetime.now().isoformat(timespec="seconds"),
        "version": str(__version__),
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    import argparse
    import json

    from .paths import PROJECT_ROOT

    parser = argparse.ArgumentParser(
        prog="rw verify",
        description="验证摄像头、推送与自检是否真的在工作",
    )
    parser.add_argument("--json", action="store_true", help="机器可读输出")
    parser.add_argument("--no-camera", action="store_true",
                        help="跳过摄像头（监控运行中设备被占用）")
    parser.add_argument("--no-push", action="store_true", help="跳过推送测试")
    parser.add_argument("--no-heartbeat", action="store_true", help="跳过外部心跳")
    parser.add_argument("--config", default=str(PROJECT_ROOT / "config.yaml"))
    args = parser.parse_args(list(argv or []))

    result = run_checks(
        PROJECT_ROOT,
        args.config,
        include_camera=not args.no_camera,
        include_push=not args.no_push,
        include_heartbeat=not args.no_heartbeat,
    )

    if args.json:
        print(json.dumps(result, ensure_ascii=True))
        return 0 if result["ok"] else 1

    print(f"room-watch {result['version']} 验证 @ {result['checked_at']}")
    for item in result["items"]:
        if item.get("skipped"):
            mark = "–"
        else:
            mark = "✓" if item["ok"] else "✗"
        print(f"  {mark} {item['name']}：{item['detail']}")
    print("结论：" + ("全部通过" if result["ok"] else "有必检项未通过"))
    return 0 if result["ok"] else 1
