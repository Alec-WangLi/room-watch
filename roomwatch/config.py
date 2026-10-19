"""配置加载：主配置 + 本机覆盖。

为什么要一个覆盖文件
--------------------
`config.yaml` 是入库文件，里面的凭据必须是占位串（`.githooks` 提交前会拦）。
真实 webhook / token 只能放本机。常见做法是复制一份完整副本
（`rw start --config config.local.yaml`），但那样两份配置会漂移：
改主配置的录像时长，覆盖副本还是老值，症状是「明明改了没生效」。

所以只放差异：`config.local.yaml` 只需要三五行（渠道 + 凭据），
主配置改了自动跟随，启动方式也不用改。

    config.yaml         入库，占位串
    config.local.yaml   不入库，只写需要覆盖的键（rw push 自动生成）

加载顺序：主配置 → 深合并本机覆盖 → 相对路径归一。
"""

from __future__ import annotations

import os
from typing import Optional

import yaml

from .paths import ffmpeg_exe, resolve

LOCAL_NAME = "config.local.yaml"

# 这些键的值是相对项目根的路径，读进来就要归一成绝对路径
_PATH_KEYS = (
    ("detector", "model"),
    ("detector", "onnx_model"),
    ("face", "model"),
    ("identity", "owner_file"),
    ("snapshot", "dir"),
    ("storage", "database"),
    ("storage", "status"),
    ("voice", "cache_dir"),
    ("guardian", "watcher_log"),
    ("guardian", "state"),
    ("guardian", "lock"),
    ("guardian", "history"),
    ("guardian", "desired_state"),
    ("guardian", "owner_lock"),
    ("guardian", "gallery_pid"),
    ("guardian", "gallery_log"),
    ("health", "path"),
    ("health", "exit_record"),
    ("health", "fault_log"),
)


def local_path_for(path: str) -> Optional[str]:
    """覆盖文件与主配置同目录，存在才算数。"""
    candidate = os.path.join(os.path.dirname(os.path.abspath(path)), LOCAL_NAME)
    return candidate if os.path.exists(candidate) else None


def merge_config(base: dict, override: dict) -> dict:
    """深合并：字典逐键递归，其它类型直接覆盖。纯函数，便于单测。"""
    out = dict(base or {})
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = merge_config(out[key], value)
        else:
            out[key] = value
    return out


def read_yaml(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def normalize_paths(cfg: dict) -> dict:
    """把配置里的相对路径换成绝对路径（就地修改并返回）。"""
    for section, key in _PATH_KEYS:
        block = cfg.get(section)
        if isinstance(block, dict) and block.get(key):
            block[key] = resolve(block[key])
    _prefer_bundled_ffmpeg(cfg)
    return cfg


def _prefer_bundled_ffmpeg(cfg: dict) -> None:
    """分发包里带了 tools/ffmpeg.exe 时，配置写 `ffmpeg` 也能直接用。

    客户机上大概率没有装 ffmpeg，也不会去改 PATH。这里的规则是：
    **只有配置里的值是裸命令名**（不含路径分隔符）时才替换，
    用户显式写了绝对路径就原样尊重 —— 否则「我明明指定了却没用」同样是坑。
    """
    block = cfg.get("record")
    audio = block.get("audio") if isinstance(block, dict) else None
    if not isinstance(audio, dict):
        return
    configured = str(audio.get("ffmpeg") or "ffmpeg")
    if "/" in configured or "\\" in configured:
        return
    bundled = ffmpeg_exe()
    if bundled:
        audio["ffmpeg"] = bundled


def load_config(path: str) -> dict:
    """读主配置，叠加同目录的 config.local.yaml，再归一路径。"""
    cfg = read_yaml(path)
    local = local_path_for(path)
    if local:
        cfg = merge_config(cfg, read_yaml(local))
    return normalize_paths(cfg)
