#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""room-watch 提交前检查：拦住推送凭据与隐私文件进入版本库。

为什么需要它
------------
config.yaml 必须入库（它的调参历史是本项目最有价值的版本记录），
但里面四个推送凭据将来要填真值。忽略文件救不了这个矛盾，只能靠提交前拦截。

设计要点
--------
1. 确定性：只读文本 + 正则 + 路径通配，不做任何「看起来像密钥」的启发式推断。
2. 纯函数：scan_config_text() 与 scan_paths() 都不碰磁盘、不联网，便于自测。
3. 可自测：``python .githooks/check_secrets.py --self-test``，
   用临时目录下的构造样本跑正例 + 负例，并打印每条用例的期望 / 实际 / 是否通过。
4. 薄壳：.githooks/pre-commit 只负责挑一个能用的 python 再调用本文件。

退出码
------
0 = 检查通过，允许提交
1 = 发现问题，阻断提交
2 = 脚本自身执行出错
"""

from __future__ import annotations

import argparse
import fnmatch
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence, Set, Tuple

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #

#: 占位串。config.yaml 里当前这四个值都是占位串，因此正常提交应当放行。
PLACEHOLDER_APP_TOKEN = "AT_xxx_replace_me"
PLACEHOLDER_UID = "UID_xxx_replace_me"
PLACEHOLDER_SENDKEY = "SCTxxx_replace_me"
PLACEHOLDER_WEBHOOK_KEY = "REPLACE_ME"

#: 通用扫描中「命中但属于占位串」的豁免集合，防止假阳性。
PLACEHOLDER_VALUES: frozenset = frozenset(
    {PLACEHOLDER_APP_TOKEN, PLACEHOLDER_UID, PLACEHOLDER_SENDKEY, PLACEHOLDER_WEBHOOK_KEY}
)

#: 凭据修复建议，四种凭据共用。
CREDENTIAL_FIX = (
    "改回占位串再提交（推荐，也是钩子放行的唯一方式）；\n"
    "        或者复制一份 config.local.yaml 填真值（已被 .gitignore 忽略），\n"
    "        启动时用 --config config.local.yaml 指定（watcher.py:940 / guardian.py:438 支持）；\n"
    "        或者 git update-index --skip-worktree config.yaml —— 但这只对你这台机器生效，\n"
    "        执行后你对该文件的修改 git 完全看不见（git status 不显示、也提交不了），\n"
    "        将来要恢复须执行 git update-index --no-skip-worktree config.yaml。"
)


@dataclass(frozen=True)
class SecretRule:
    """一条凭据规则。

    Attributes:
        rule_id: 规则标识，同时等于 config.yaml 中的点分路径。
        placeholder: 该字段的占位串，等于它就算安全。
        matcher: ``"prefix"`` 按前缀判定；``"query_key"`` 按 URL 查询参数判定。
        token: prefix 模式下是要匹配的前缀；query_key 模式下是查询参数名。
        why: 命中时打印给用户看的危害说明。
    """

    rule_id: str
    placeholder: str
    matcher: str
    token: str
    why: str


SECRET_RULES: Tuple[SecretRule, ...] = (
    SecretRule(
        rule_id="notify.wxpusher.app_token",
        placeholder=PLACEHOLDER_APP_TOKEN,
        matcher="prefix",
        token="AT_",
        why="WxPusher 应用令牌，拿到它的人可以向你的微信发消息",
    ),
    SecretRule(
        rule_id="notify.wxpusher.uid",
        placeholder=PLACEHOLDER_UID,
        matcher="prefix",
        token="UID_",
        why="WxPusher 用户 UID，决定消息发到谁的微信",
    ),
    SecretRule(
        rule_id="notify.serverchan.sendkey",
        placeholder=PLACEHOLDER_SENDKEY,
        matcher="prefix",
        token="SCT",
        why="Server酱 SendKey，拿到它的人可以向你的微信服务号发消息",
    ),
    SecretRule(
        rule_id="notify.wecom_bot.webhook",
        placeholder=PLACEHOLDER_WEBHOOK_KEY,
        matcher="query_key",
        token="key",
        why="企业微信群机器人 Webhook，拿到它的人可以往你的群里发消息",
    ),
)

RULES_BY_PATH = {rule.rule_id: rule for rule in SECRET_RULES}

#: 安全网：即使缩进被改乱、点分路径解析不出来，也仍能按「值长什么样」抓到真凭据。
GENERIC_TOKEN_PATTERNS: Tuple[Tuple[str, "re.Pattern[str]"], ...] = (
    ("generic.app_token", re.compile(r"AT_[A-Za-z0-9_-]{8,}")),
    ("generic.uid", re.compile(r"UID_[A-Za-z0-9_-]{5,}")),
    ("generic.sendkey", re.compile(r"SCT[A-Za-z0-9_-]{10,}")),
)

GENERIC_WEBHOOK_RE: "re.Pattern[str]" = re.compile(r"[?&]key=([^\s&\"']+)")


@dataclass(frozen=True)
class PathRule:
    """一条暂存路径规则。

    Attributes:
        rule_id: 规则标识。
        scope: ``"full"`` 用通配匹配完整路径；``"name"`` 只匹配文件名。
        pattern: fnmatch 通配符。
        why: 命中时打印给用户看的说明。
        fix: 修复建议。
    """

    rule_id: str
    scope: str
    pattern: str
    why: str
    fix: str


PRIVACY_PATH_RULES: Tuple[PathRule, ...] = (
    PathRule(
        rule_id="path.captures",
        scope="full",
        pattern="captures/**",
        why="抓拍 jpg 与录像 mp4，含房间内人脸与行为记录（当前约 1.9GB）",
        fix="git rm --cached <路径>（文件不会从磁盘删除），并确认 .gitignore 的 captures/ 规则仍在。",
    ),
    PathRule(
        rule_id="path.identity",
        scope="full",
        pattern="identity/**",
        why="本人人脸特征模板所在目录，属生物特征数据",
        fix="git rm --cached <路径>，并确认 .gitignore 的 identity/ 规则仍在。",
    ),
    PathRule(
        rule_id="path.voice_cache",
        scope="full",
        pattern="voice_cache/**",
        why="语音缓存与增益报告，删掉后首次播报会自动重建，无入库价值",
        fix="git rm --cached <路径>，并确认 .gitignore 的 voice_cache/ 规则仍在。",
    ),
    PathRule(
        rule_id="path.workbuddy",
        scope="full",
        pattern=".workbuddy/**",
        why="WorkBuddy 工作区，含 AI 记忆日志（逐小时活动流水、进程 PID、抓拍内容判断）",
        fix="git rm --cached <路径>，并确认 .gitignore 的 .workbuddy/ 规则仍在。",
    ),
    PathRule(
        rule_id="path.db",
        scope="name",
        pattern="*.db",
        why="SQLite 运行时数据库（events.db），是数据不是代码",
        fix="git rm --cached <路径>，并确认 .gitignore 的 *.db 规则仍在。",
    ),
    PathRule(
        rule_id="path.log",
        scope="name",
        pattern="*.log",
        why="运行日志，持续增长且无版本价值",
        fix="git rm --cached <路径>，并确认 .gitignore 的 *.log 规则仍在。",
    ),
    PathRule(
        rule_id="path.npz",
        scope="name",
        pattern="*.npz",
        why="人脸特征 / 嵌入向量文件（与 identity/owner.npz 同类，属生物特征数据）",
        fix=(
            "git rm --cached <路径>。若这是模型权重，先确认它不是人脸模板，"
            "再把它加进 .gitignore 的白名单（在 *.npz 之后写 !<相对路径>）。"
        ),
    ),
)


@dataclass(frozen=True)
class Finding:
    """一条命中记录。"""

    rule_id: str
    where: str
    detail: str
    why: str
    fix: str


# --------------------------------------------------------------------------- #
# 文本解析（纯函数）
# --------------------------------------------------------------------------- #


def strip_comment(line: str) -> str:
    """去掉 YAML 行尾注释，且不会误伤引号内的 ``#``。

    Args:
        line: 原始行（不含换行符）。

    Returns:
        去掉注释后的内容，保留原有缩进。
    """
    quote: Optional[str] = None
    for index, char in enumerate(line):
        if quote is not None:
            if char == quote:
                quote = None
            continue
        if char in ("'", '"'):
            quote = char
            continue
        if char == "#" and (index == 0 or line[index - 1] in " \t"):
            return line[:index]
    return line


def unquote(value: str) -> str:
    """去掉值两侧成对出现的引号。"""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


@dataclass(frozen=True)
class MappingLine:
    """一个解析出来的 ``键: 值`` 映射行。"""

    line_no: int
    dotted_path: str
    value: str


def parse_mapping_lines(text: str) -> List[MappingLine]:
    """用缩进栈解析出所有 ``键: 值`` 行及其点分路径。

    不引入 YAML 依赖：本项目只用了两层缩进的固定结构，缩进栈足够且完全确定。
    空行、纯注释行、列表项（``- `` 开头）一律跳过。

    Args:
        text: 配置文件全文。

    Returns:
        按行号升序的映射行列表。
    """
    stack: List[Tuple[int, str]] = []
    result: List[MappingLine] = []

    for line_no, raw_line in enumerate(text.splitlines(), start=1):
        content = strip_comment(raw_line)
        if not content.strip():
            continue
        stripped = content.strip()
        if stripped.startswith("- "):
            continue
        separator = stripped.find(":")
        if separator <= 0:
            continue

        indent = len(content) - len(content.lstrip(" "))
        key = stripped[:separator].strip().strip("\"'")
        rest = stripped[separator + 1 :].strip()

        while stack and stack[-1][0] >= indent:
            stack.pop()

        if rest == "":
            stack.append((indent, key))
            continue

        dotted = ".".join([item[1] for item in stack] + [key])
        result.append(MappingLine(line_no=line_no, dotted_path=dotted, value=unquote(rest)))

    return result


# --------------------------------------------------------------------------- #
# 规则匹配（纯函数）
# --------------------------------------------------------------------------- #


def rule_hit(rule: SecretRule, value: str) -> Optional[str]:
    """判断某个值是否命中该凭据规则。

    Args:
        rule: 待匹配的规则。
        value: 去掉引号后的配置值。

    Returns:
        命中的具体凭据串；未命中或值为占位串时返回 None。
    """
    if rule.matcher == "prefix":
        if not value.startswith(rule.token):
            return None
        if value == rule.placeholder:
            return None
        return value

    if rule.matcher == "query_key":
        pattern = re.compile(r"[?&]" + re.escape(rule.token) + r"=([^\s&\"']+)")
        match = pattern.search(value)
        if match is None:
            return None
        found = match.group(1)
        if found == rule.placeholder:
            return None
        return found

    return None


def scan_config_text(text: str, source: str = "config.yaml") -> List[Finding]:
    """扫描配置文本，返回所有凭据命中。

    两道检查：
    1. 按点分路径精确匹配（能给出准确的键名与行号）。
    2. 安全网：对每一行（含注释行）按「值的形状」再扫一遍，
       防止缩进被改乱导致路径解析失效而漏判。

    Args:
        text: 配置文件全文。
        source: 报告中显示的文件名。

    Returns:
        命中列表，按行号升序。
    """
    findings: List[Finding] = []
    flagged_lines: Set[int] = set()

    for item in parse_mapping_lines(text):
        rule = RULES_BY_PATH.get(item.dotted_path)
        if rule is None:
            continue
        hit = rule_hit(rule, item.value)
        if hit is None:
            continue
        flagged_lines.add(item.line_no)
        findings.append(
            Finding(
                rule_id=rule.rule_id,
                where=f"{source}:{item.line_no}",
                detail=f"{item.dotted_path} = {hit}",
                why=rule.why,
                fix=CREDENTIAL_FIX,
            )
        )

    # 安全网：即便上面一道没抓到，形状像真凭据的值也要拦下。
    for line_no, raw_line in enumerate(text.splitlines(), start=1):
        if line_no in flagged_lines:
            continue
        for generic_id, pattern in GENERIC_TOKEN_PATTERNS:
            match = pattern.search(raw_line)
            if match is None:
                continue
            token = match.group(0)
            if token in PLACEHOLDER_VALUES:
                continue
            flagged_lines.add(line_no)
            findings.append(
                Finding(
                    rule_id=generic_id,
                    where=f"{source}:{line_no}",
                    detail=f"疑似真凭据：{token}",
                    why="形状与真实推送凭据一致，且不是占位串",
                    fix=CREDENTIAL_FIX,
                )
            )
            break
        else:
            webhook_match = GENERIC_WEBHOOK_RE.search(raw_line)
            if webhook_match is not None:
                key_value = webhook_match.group(1)
                if key_value not in PLACEHOLDER_VALUES and len(key_value) >= 4:
                    flagged_lines.add(line_no)
                    findings.append(
                        Finding(
                            rule_id="generic.webhook_key",
                            where=f"{source}:{line_no}",
                            detail=f"疑似真 Webhook key：{key_value}",
                            why="URL 里的 key 参数不是占位串，可能是可用的群机器人地址",
                            fix=CREDENTIAL_FIX,
                        )
                    )

    findings.sort(key=lambda item: item.where)
    return findings


def scan_paths(paths: Iterable[str]) -> List[Finding]:
    """扫描待提交的文件路径，返回隐私文件命中。

    Args:
        paths: 路径列表，git 输出的 ``/`` 分隔符风格。

    Returns:
        命中列表，按输入顺序。
    """
    findings: List[Finding] = []
    for raw_path in paths:
        normalized = raw_path.replace("\\", "/")
        while normalized.startswith("./"):
            normalized = normalized[2:]
        basename = normalized.rsplit("/", 1)[-1]
        for rule in PRIVACY_PATH_RULES:
            target = normalized if rule.scope == "full" else basename
            if not fnmatch.fnmatch(target, rule.pattern):
                continue
            findings.append(
                Finding(
                    rule_id=rule.rule_id,
                    where=normalized,
                    detail=f"命中忽略规则 {rule.pattern}",
                    why=rule.why,
                    fix=rule.fix,
                )
            )
            break
    return findings


# --------------------------------------------------------------------------- #
# 磁盘 / git 交互
# --------------------------------------------------------------------------- #


def repo_root() -> str:
    """返回仓库根目录（.githooks 的上一级）。"""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read_text(path: str) -> Optional[str]:
    """读取文本文件，读不到返回 None。"""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError:
        return None


def git_staged_paths() -> List[str]:
    """取本次已暂存（新增/修改/重命名/类型变更）的文件路径。

    Returns:
        路径列表；不在 git 仓库中或命令失败时返回空列表。
    """
    command = ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMRT"]
    try:
        completed = subprocess.run(
            command,
            cwd=repo_root(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
    except OSError:
        return []
    if completed.returncode != 0:
        # 还没有任何提交时，退化为列出索引里的全部文件。
        fallback = subprocess.run(
            ["git", "ls-files", "--cached"],
            cwd=repo_root(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if fallback.returncode != 0:
            return []
        completed = fallback
    return [line.strip() for line in completed.stdout.splitlines() if line.strip()]


# --------------------------------------------------------------------------- #
# 报告
# --------------------------------------------------------------------------- #


def render_findings(findings: Sequence[Finding]) -> str:
    """把命中列表渲染成人可直接照做的中文报告。"""
    lines: List[str] = []
    for index, finding in enumerate(findings, start=1):
        lines.append(f"[{index}] 规则 {finding.rule_id}")
        lines.append(f"    位置  : {finding.where}")
        lines.append(f"    命中  : {finding.detail}")
        lines.append(f"    风险  : {finding.why}")
        lines.append("    怎么修:")
        for fix_line in finding.fix.split("\n"):
            lines.append(f"        {fix_line.strip()}")
        lines.append("")
    return "\n".join(lines)


def check(config_path: str, use_git: bool = True, paths: Optional[Sequence[str]] = None) -> List[Finding]:
    """执行一次完整检查（配置 + 暂存路径）。

    Args:
        config_path: 配置文件路径。
        use_git: 是否检查 git 暂存区。
        paths: 直接给定待检路径，优先级高于 use_git。

    Returns:
        命中列表，空列表表示通过。
    """
    findings: List[Finding] = []

    config_text = read_text(config_path)
    if config_text is None:
        print(f"[警告] 读不到配置文件，已跳过凭据检查：{config_path}")
    else:
        findings.extend(scan_config_text(config_text, source=os.path.basename(config_path)))

    if paths is None and use_git:
        paths = git_staged_paths()
    if paths:
        findings.extend(scan_paths(paths))

    return findings


# --------------------------------------------------------------------------- #
# 自测
# --------------------------------------------------------------------------- #

CLEAN_CONFIG = """\
# 房间进入检测 —— 配置（全部占位串，应当放行）
camera:
  index: 0
  backend: dshow

notify:
  channel: wxpusher
  title: 房间有人进入
  wxpusher:
    app_token: "AT_xxx_replace_me"
    uid: "UID_xxx_replace_me"
  serverchan:
    sendkey: "SCTxxx_replace_me"
  wecom_bot:
    webhook: "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=REPLACE_ME"
"""

DIRTY_CONFIG = """\
notify:
  channel: wxpusher
  wxpusher:
    app_token: "AT_xxx_replace_me"
    uid: "UID_xxx_replace_me"
  serverchan:
    sendkey: "SCTxxx_replace_me"
  wecom_bot:
    webhook: "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=REPLACE_ME"
"""


def _config_with(**overrides: str) -> str:
    """基于干净模板生成配置文本，只替换给定的键。"""
    text = CLEAN_CONFIG
    for dotted, value in overrides.items():
        key = dotted.rsplit(".", 1)[-1]
        target = f'{key}: "AT_xxx_replace_me"'
        if key == "uid":
            target = f'{key}: "UID_xxx_replace_me"'
        elif key == "sendkey":
            target = f'{key}: "SCTxxx_replace_me"'
        elif key == "webhook":
            target = f'{key}: "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=REPLACE_ME"'
        replacement = f'{key}: "{value}"'
        if target not in text:
            raise AssertionError(f"模板里找不到待替换的行：{target}")
        text = text.replace(target, replacement)
    return text


@dataclass(frozen=True)
class Case:
    """一条自测用例。"""

    name: str
    kind: str  # "config" 或 "paths"
    payload: str
    expected: Tuple[str, ...]


def build_cases() -> List[Case]:
    """构造全部自测用例：每条规则至少一个正例、一个负例。"""
    return [
        Case(
            name="负例：四项全为占位串（当前 config.yaml 的真实状态）",
            kind="config",
            payload=CLEAN_CONFIG,
            expected=(),
        ),
        Case(
            name="正例：notify.wxpusher.app_token 填入真实值",
            kind="config",
            payload=_config_with(**{"notify.wxpusher.app_token": "AT_Qu8Zk1pLmN3xR7vT2wY"}),
            expected=("notify.wxpusher.app_token",),
        ),
        Case(
            name="负例：notify.wxpusher.app_token 保持占位串",
            kind="config",
            payload=_config_with(),
            expected=(),
        ),
        Case(
            name="正例：notify.wxpusher.uid 填入真实值",
            kind="config",
            payload=_config_with(**{"notify.wxpusher.uid": "UID_9fK2mQ7xLp"}),
            expected=("notify.wxpusher.uid",),
        ),
        Case(
            name="负例：notify.wxpusher.uid 保持占位串",
            kind="config",
            payload=_config_with(),
            expected=(),
        ),
        Case(
            name="正例：notify.serverchan.sendkey 填入真实值",
            kind="config",
            payload=_config_with(**{"notify.serverchan.sendkey": "SCT22847TbQm1xKp9vLz3nR"}),
            expected=("notify.serverchan.sendkey",),
        ),
        Case(
            name="负例：notify.serverchan.sendkey 保持占位串",
            kind="config",
            payload=_config_with(),
            expected=(),
        ),
        Case(
            name="正例：notify.wecom_bot.webhook 填入真实 key",
            kind="config",
            payload=_config_with(
                **{"notify.wecom_bot.webhook": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=693a91f6-7a3c-4bc4-97a0-0ec2sifa5aaa"}
            ),
            expected=("notify.wecom_bot.webhook",),
        ),
        Case(
            name="负例：notify.wecom_bot.webhook 的 key 仍是 REPLACE_ME",
            kind="config",
            payload=_config_with(),
            expected=(),
        ),
        Case(
            name="正例：四项全部填真值，应一次命中四条",
            kind="config",
            payload=_config_with(
                **{
                    "notify.wxpusher.app_token": "AT_Qu8Zk1pLmN3xR7vT2wY",
                    "notify.wxpusher.uid": "UID_9fK2mQ7xLp",
                    "notify.serverchan.sendkey": "SCT22847TbQm1xKp9vLz3nR",
                    "notify.wecom_bot.webhook": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=693a91f6-7a3c-4bc4-97a0-0ec2sifa5aaa",
                }
            ),
            expected=(
                "notify.wxpusher.app_token",
                "notify.wxpusher.uid",
                "notify.serverchan.sendkey",
                "notify.wecom_bot.webhook",
            ),
        ),
        Case(
            name="正例：缩进写乱导致点分路径解析失效时，安全网仍要抓到",
            kind="config",
            payload='app_token: "AT_Qu8Zk1pLmN3xR7vT2wY"',
            expected=("generic.app_token",),
        ),
        Case(
            name="正例：真凭据被写进注释行，也应当拦下",
            kind="config",
            payload="# 记得把 app_token 换成 AT_Qu8Zk1pLmN3xR7vT2wY\n",
            expected=("generic.app_token",),
        ),
        Case(
            name="负例：注释里只是描述格式（AT_ 后面跟空格），不得误报",
            kind="config",
            payload="# 创建一个应用，拿到 appToken（AT_ 开头）\n",
            expected=(),
        ),
        Case(
            name="正例：路径 captures/** 命中",
            kind="paths",
            payload="captures/2026-10-17/090310.jpg",
            expected=("path.captures",),
        ),
        Case(
            name="正例：路径 identity/owner.npz 命中",
            kind="paths",
            payload="identity/owner.npz",
            expected=("path.identity",),
        ),
        Case(
            name="正例：路径 voice_cache/** 命中",
            kind="paths",
            payload="voice_cache/voice_processed.wav",
            expected=("path.voice_cache",),
        ),
        Case(
            name="正例：路径 .workbuddy/** 命中",
            kind="paths",
            payload=".workbuddy/memory/2026-10-17.md",
            expected=("path.workbuddy",),
        ),
        Case(
            name="正例：*.db 命中",
            kind="paths",
            payload="events.db",
            expected=("path.db",),
        ),
        Case(
            name="正例：*.log 命中",
            kind="paths",
            payload="watcher.log",
            expected=("path.log",),
        ),
        Case(
            name="正例：任何 *.npz 命中（与 identity/owner.npz 同类）",
            kind="paths",
            payload="models/somebody.npz",
            expected=("path.npz",),
        ),
        Case(
            name="负例：应当入库的文件全部放行",
            kind="paths",
            payload="\n".join(
                [
                    "watcher.py",
                    "config.yaml",
                    "AGENTS.md",
                    "README.md",
                    "models/face_detection_yunet_2023mar.onnx",
                    "models/face_recognition_sface_2021dec.onnx",
                    "yolov8n.pt",
                    ".githooks/check_secrets.py",
                    ".gitattributes",
                ]
            ),
            expected=(),
        ),
    ]


def _run_case(case: Case) -> Tuple[bool, Tuple[str, ...]]:
    """执行单条用例，返回（是否通过，实际命中的规则集合）。"""
    if case.kind == "config":
        findings = scan_config_text(case.payload, source="sample.yaml")
    else:
        findings = scan_paths([line for line in case.payload.splitlines() if line])
    actual: Tuple[str, ...] = tuple(sorted({item.rule_id for item in findings}))
    expected: Tuple[str, ...] = tuple(sorted(set(case.expected)))
    return actual == expected, actual


def run_self_test() -> int:
    """跑全部自测用例，并对脚本自身做一次端到端笺证。

    Returns:
        0 表示全部通过；1 表示有用例未通过。
    """
    print("=== check_secrets.py 自测 ===")
    print(f"解释器: {sys.executable}")
    print("样本目录: 临时目录（用完即删，不写入项目、不联网、不起进程之外的副作用）")
    print()

    cases = build_cases()
    failures = 0
    for index, case in enumerate(cases, start=1):
        passed, actual = _run_case(case)
        expected_display = "、".join(sorted(set(case.expected))) if case.expected else "无命中"
        actual_display = "、".join(actual) if actual else "无命中"
        print(f"[{index:>2}/{len(cases)}] {case.name}")
        print(f"      期望: {expected_display}")
        print(f"      实际: {actual_display}")
        print(f"      结果: {'PASS' if passed else 'FAIL'}")
        if not passed:
            failures += 1
        print()

    # 端到端笺证：真的起一个子进程跑 CLI，验证退出码（0 放行 / 1 阻断）。
    print("---- 端到端笺证（真实子进程调用本脚本的命令行入口）----")
    with tempfile.TemporaryDirectory(prefix="roomwatch-selftest-") as tmp_dir:
        clean_file = os.path.join(tmp_dir, "clean.yaml")
        dirty_file = os.path.join(tmp_dir, "dirty.yaml")
        with open(clean_file, "w", encoding="utf-8") as handle:
            handle.write(CLEAN_CONFIG)
        with open(dirty_file, "w", encoding="utf-8") as handle:
            handle.write(DIRTY_CONFIG.replace('app_token: "AT_xxx_replace_me"', 'app_token: "AT_Qu8Zk1pLmN3xR7vT2wY"'))

        for label, target, expected_code in (
            ("占位串配置 -> 应当放行（退出码 0）", clean_file, 0),
            ("真实凭据配置 -> 应当阻断（退出码 1）", dirty_file, 1),
        ):
            completed = subprocess.run(
                [sys.executable, os.path.abspath(__file__), "--config", target, "--no-git"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                check=False,
            )
            passed = completed.returncode == expected_code
            if not passed:
                failures += 1
            print(f"      {label}")
            print(f"      期望退出码: {expected_code}")
            print(f"      实际退出码: {completed.returncode}")
            print(f"      结果: {'PASS' if passed else 'FAIL'}")
            print()

    total = len(cases) + 2
    print("---- 汇总 ----")
    print(f"用例总数: {total}   通过: {total - failures}   未通过: {failures}")
    if failures:
        print("结论: 自测未通过，脚本不可用，请修复后再提交。")
        return 1
    print("结论: 自测全部通过。占位串不会误报（无假阳性），真实凭据能抓到（无假阴性）。")
    return 0


# --------------------------------------------------------------------------- #
# 命令行入口
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    """构造命令行参数解析器。"""
    parser = argparse.ArgumentParser(
        description="room-watch 提交前检查：拦住推送凭据与隐私文件进入版本库",
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="跑内置自测（正例 + 负例 + 端到端笺证），不读取项目配置",
    )
    parser.add_argument(
        "--config",
        default=None,
        help="待检查的配置文件，默认 <仓库根>/config.yaml",
    )
    parser.add_argument(
        "--no-git",
        action="store_true",
        help="跳过 git 暂存区路径检查（可在非 git 环境下单独运行）",
    )
    parser.add_argument(
        "--paths-file",
        default=None,
        help="从文件读取待检查路径（每行一个），优先级高于 git 暂存区",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    """命令行入口。

    Args:
        argv: 参数列表，None 表示取 sys.argv[1:]。

    Returns:
        0 通过 / 1 阻断 / 2 脚本出错。
    """
    args = build_parser().parse_args(argv)

    if args.self_test:
        return run_self_test()

    config_path = args.config or os.path.join(repo_root(), "config.yaml")
    paths: Optional[List[str]] = None
    if args.paths_file:
        text = read_text(args.paths_file)
        if text is None:
            print(f"[错误] 读不到路径清单文件：{args.paths_file}", file=sys.stderr)
            return 2
        paths = [line.strip() for line in text.splitlines() if line.strip()]

    staged: List[str] = []
    if paths is None and not args.no_git:
        staged = git_staged_paths()

    print("=== room-watch 提交前检查（.githooks/check_secrets.py）===")
    print(f"配置文件  : {config_path}")
    if paths is not None:
        print(f"待检路径  : {len(paths)} 个（来自 {args.paths_file}）")
    elif args.no_git:
        print("待检路径  : 已跳过（--no-git）")
    else:
        print(f"暂存文件  : {len(staged)} 个")
    print()

    findings = check(config_path, use_git=False, paths=paths if paths is not None else staged)

    if not findings:
        print("检查通过：未发现真实凭据，暂存区也没有隐私文件。")
        return 0

    print(f"[阻断] 发现 {len(findings)} 处问题，本次提交已被阻止。")
    print()
    print(render_findings(findings))
    print("确认无误后重新 git commit 即可。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
