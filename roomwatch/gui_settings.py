"""设置窗口：摄像头 / 推送 / 相册 / 身份 / 录像 / 心跳 / 更新 / 自启 / 声音 九个页签。

设计约束（都很实在）
--------------------
1. **本模块不 import tkinter，也不 import yaml。**
   tkinter 由 `RoomWatchApp` 传进来（`app.tk` / `app.ttk`），
   这样 `tests/test_settings.py` 能在没有图形环境的解释器里校验页签与 SPEC 一致；
   yaml 更不能有——打包后的 exe 里只有 tkinter + Pillow，读写配置一律走
   `rw config` 子进程（`app._run_cli`）。
2. **密钥只显示掩码。** 输入框是空的，占位提示写「已设置（…5179）」；
   留空保存＝不改。想换就把整条新值粘进去。
3. **保存 = 一次 `rw config --set`。** 后端先全量校验再落盘、落盘后复读，
   界面只负责把结果念给用户听。
4. 改动在**下次启动监控**时生效（watcher 不热加载配置），所以保存后明确提示。
"""

from __future__ import annotations

from typing import Optional


# 页签顺序。必须与 settings.sections() 一致（tests/test_settings.py 钉住）。
SECTIONS = ("摄像头", "推送", "相册", "身份", "录像", "心跳", "更新", "自启", "声音")

# 每项设置生效方式的提示。none 由保存方写 reload 标记，进程内直接应用；
# watcher 是默认值，不在每行重复，保存后会统一提示需要重启哪个进程。
RESTART_TEXT = {
    "none": "保存后立即生效",
    "guardian": "需重启守护进程",
    "gallery": "需重启相册",
}


class SettingsDialog:
    """一个 Toplevel。`autoload=False` 时只建骨架，供 --selftest 使用。"""

    def __init__(self, app, autoload: bool = True) -> None:
        self.app = app
        self.tk = app.tk
        self.ttk = app.ttk
        self.labels: dict = {}
        self.vars: dict = {}
        self.multi_vars: dict = {}
        self.secret_labels: dict = {}
        self._built = False       # 控件是否已按快照建出来（只建一次）
        self._populated = False   # 是否至少成功读过一次配置
        self._saving = False

        self.win = self.tk.Toplevel(app.win)
        self.win.title("设置 · 房间监控")
        self.win.geometry("800x700")
        self.win.minsize(720, 620)
        self.win.transient(app.win)

        self.notebook = self.ttk.Notebook(self.win)
        self.notebook.pack(fill="both", expand=True, padx=12, pady=(12, 6))

        self.tab_frames: dict = {}
        self.rows_holder: dict = {}
        self.actions_holder: dict = {}
        for name in SECTIONS:
            frame = self.ttk.Frame(self.notebook, padding=10)
            self.notebook.add(frame, text=name)
            self.tab_frames[name] = frame
            # 先把底部按钮占好位，再让内容区吃掉剩余空间；顺序反了按钮会被挤到看不见
            actions = self.ttk.Frame(frame)
            actions.pack(side="bottom", fill="x", pady=(10, 0))
            self.actions_holder[name] = actions
            if name == "推送":
                # 推送页字段多，内容区可滚动，避免把底部按钮挤出窗口。
                canvas = self.tk.Canvas(frame, highlightthickness=0, borderwidth=0,
                                        width=720, height=380)
                scroll = self.ttk.Scrollbar(frame, orient="vertical", command=canvas.yview)
                holder = self.ttk.Frame(canvas)
                window_id = canvas.create_window((0, 0), window=holder, anchor="nw",
                                                 width=700)
                holder.bind(
                    "<Configure>",
                    lambda _e, c=canvas: c.configure(scrollregion=c.bbox("all")),
                )
                canvas.bind(
                    "<Configure>",
                    lambda e, c=canvas, w=window_id: c.itemconfigure(w, width=e.width),
                )
                canvas.configure(yscrollcommand=scroll.set)
                canvas.pack(side="left", fill="both", expand=True)
                scroll.pack(side="right", fill="y")
                self._push_canvas = canvas
                canvas.bind_all("<MouseWheel>", self._on_push_wheel)
            else:
                holder = self.ttk.Frame(frame)
                holder.pack(fill="both", expand=True)
            self.rows_holder[name] = holder

        self._build_camera_block(self.rows_holder["摄像头"])
        self._build_gallery_block(self.rows_holder["相册"])
        self._build_storage_block(self.rows_holder["录像"])
        self._build_heartbeat_block(self.rows_holder["心跳"])
        self._build_identity_block(self.rows_holder["身份"])
        self._build_autostart_block(self.rows_holder["自启"])
        self._build_push_actions(self.actions_holder["推送"], self.rows_holder["推送"])

        footer = self.ttk.Frame(self.win)
        footer.pack(fill="x", padx=12, pady=(0, 12))
        self.status_label = self.tk.Label(footer, text="读取当前配置…", anchor="w",
                                          font=app.font_small, fg="#666")
        self.status_label.pack(side="left", fill="x", expand=True)
        self.btn_close = self.ttk.Button(footer, text="关闭", command=self.close)
        self.btn_close.pack(side="right", padx=(6, 0))
        self.btn_save = self.ttk.Button(footer, text="保存", command=self.on_save)
        self.btn_save.pack(side="right")

        if autoload:
            self.reload()
            self.refresh_autostart()
            self.refresh_identity()
        else:
            self.status_label.configure(text="（自检模式：未读取配置）")

    # ---------------- 特殊区块 ----------------

    def _on_push_wheel(self, event) -> None:
        try:
            if self.notebook.index("current") == SECTIONS.index("推送"):
                self._push_canvas.yview_scroll(int(-event.delta / 120), "units")
        except Exception:  # noqa: BLE001 - 窗口已关闭/事件来自别的控件
            pass

    def _build_identity_block(self, parent) -> None:
        box = self.ttk.LabelFrame(parent, text="本人身份", padding=8)
        box.pack(fill="x", pady=(0, 8))
        self._owner_photo = None
        self.identity_photo = self.tk.Label(box, text="", anchor="w")
        self.identity_photo.pack(fill="x", pady=(0, 4))
        self.identity_label = self.tk.Label(box, text="读取中…", anchor="w",
                                            justify="left", wraplength=700,
                                            font=self.app.font_small, fg="#666")
        self.identity_label.pack(fill="x")
        bar = self.ttk.Frame(box)
        bar.pack(fill="x", pady=(6, 0))
        self.ttk.Button(bar, text="重新读取", command=self.refresh_identity).pack(side="left")
        self.ttk.Button(bar, text="注册本人（现场）",
                        command=self._enroll_live).pack(side="left", padx=6)
        self.ttk.Button(bar, text="用最新抓拍",
                        command=self._enroll_latest).pack(side="left")
        self.ttk.Button(bar, text="清除模板", command=self.on_clear_identity).pack(side="left", padx=6)
        self.ttk.Label(box, text="注册需要摄像头，且监控必须是停止状态（设备独占）。",
                       font=self.app.font_small, foreground="#888").pack(fill="x", pady=(6, 0))

    def _build_autostart_block(self, parent) -> None:
        box = self.ttk.LabelFrame(parent, text="开机自启", padding=8)
        box.pack(fill="x", pady=(0, 8))
        self.autostart_label = self.tk.Label(box, text="读取中…", anchor="w",
                                             justify="left", wraplength=700,
                                             font=self.app.font_small, fg="#666")
        self.autostart_label.pack(fill="x")
        bar = self.ttk.Frame(box)
        bar.pack(fill="x", pady=(6, 0))
        self.ttk.Button(bar, text="开启自启",
                        command=lambda: self.on_autostart(True)).pack(side="left")
        self.ttk.Button(bar, text="关闭自启",
                        command=lambda: self.on_autostart(False)).pack(side="left", padx=6)
        self.ttk.Button(bar, text="刷新状态", command=self.refresh_autostart).pack(side="left")
        self.ttk.Label(box, text="开机自启拉起的是守护进程：监控意外退出时它会重新拉起。",
                       font=self.app.font_small, foreground="#888").pack(fill="x", pady=(6, 0))

    def _build_push_actions(self, parent, holder) -> None:
        bar = self.ttk.Frame(parent)
        bar.pack(side="bottom", fill="x", pady=(6, 0))
        self.ttk.Button(bar, text="发送测试消息", command=self.on_test_push).pack(side="left")
        self.ttk.Label(bar, text="会先保存当前页面，再发一条测试消息",
                       font=self.app.font_small, foreground="#888").pack(side="left", padx=8)
        # 渠道状态放在内容顶部：页面字段多，放底部会被挤到看不见。
        self.channel_warning = self.tk.Label(
            holder, text="", anchor="w", justify="left", wraplength=700,
            font=self.app.font_small, fg="#993C1D",
        )
        self.channel_warning.pack(side="top", fill="x", pady=(0, 6))

    def _build_heartbeat_block(self, parent) -> None:
        box = self.ttk.LabelFrame(parent, text="死手开关状态", padding=8)
        box.pack(fill="x", pady=(0, 8))
        self.heartbeat_label = self.tk.Label(
            box, text="读取中…", anchor="w", justify="left", wraplength=700,
            font=self.app.font_small, fg="#666",
        )
        self.heartbeat_label.pack(fill="x")
        bar = self.ttk.Frame(box)
        bar.pack(fill="x", pady=(6, 0))
        self.ttk.Button(bar, text="测试心跳", command=self.on_test_heartbeat).pack(side="left")
        self.ttk.Label(box, text="心跳只发送存活信息，不包含画面、人脸或配置；测试使用已保存的 URL。",
                       font=self.app.font_small, foreground="#888").pack(fill="x", pady=(6, 0))

    def _show_heartbeat(self, info: dict) -> None:
        if not info.get("configured"):
            self.heartbeat_label.configure(
                text="未配置外部心跳：整机断电/断网时无法通知。\n"
                     "填好「心跳成功 URL」并保存后，外部服务发现心跳消失会提醒你。",
                fg="#993C1D")
            return
        if info.get("last_at"):
            ok = bool(info.get("last_ok"))
            text = (f"已配置 · 最近一次 {info.get('last_at')} · "
                    + ("成功" if ok else "失败"))
            if info.get("last_error"):
                text += "\n" + str(info["last_error"])
            self.heartbeat_label.configure(text=text, fg="#0F6E56" if ok else "#993C1D")
            return
        self.heartbeat_label.configure(
            text="已配置，尚未发送过心跳（守护进程启动后会按间隔发送）。",
            fg="#8A5A00")

    def _show_channel_warning(self, channels: list, outbox: dict,
                              issues: Optional[dict] = None,
                              events_deliverable: bool = True) -> None:
        issues = issues or {}
        if not channels:
            text = "告警不会送达：没有启用任何通知渠道。"
            color = "#993C1D"
        elif not events_deliverable:
            text = ("人员事件不会送达：所有启用渠道都关闭了「人员事件」；"
                    "崩溃/恢复等 critical 告警仍会发。")
            color = "#993C1D"
        elif issues:
            text = ("有渠道未配置完整，告警可能发不出去："
                    + "；".join(f"{name}（{msg}）" for name, msg in issues.items()))
            color = "#993C1D"
        elif len(channels) == 1:
            text = (f"当前只启用一个渠道（{channels[0]}）；建议至少两个，"
                    "渠道失效时才不会安静地失联。")
            color = "#8A5A00"
        else:
            text = f"已启用 {len(channels)} 个渠道：" + "、".join(channels)
            color = "#0F6E56"

        pending = int(outbox.get("pending") or 0)
        if pending:
            text += f" · 待发送 {pending} 条"
        health = outbox.get("channels") or {}
        details = []
        for name in channels:
            info = health.get(name) or {}
            if info.get("last_ok_at"):
                details.append(f"{name} 最近成功 {info['last_ok_at']}")
            elif info.get("last_error"):
                details.append(f"{name} 最近失败 {info.get('last_error_at') or ''}")
        if details:
            text += "\n" + "；".join(details[:4])
        self.channel_warning.configure(text=text, fg=color)

    def on_test_heartbeat(self) -> None:
        if self._saving:
            return
        self.status_label.configure(text="测试心跳…", fg="#666")

        def work() -> dict:
            return self.app._run_cli(["heartbeat", "--test", "--json"], timeout=30)

        self.app._run_task(work, self._on_test_heartbeat)

    def _on_test_heartbeat(self, result: dict) -> None:
        if result.get("ok"):
            self.status_label.configure(text="心跳成功", fg="#0F6E56")
            self.app.messagebox.showinfo("心跳成功", "外部心跳 URL 已返回 2xx。")
        else:
            message = str(result.get("message") or result.get("error") or "未知原因")
            self.status_label.configure(text="心跳失败", fg="#993C1D")
            self.app.messagebox.showerror("心跳失败", message)
        self.reload()

    def _build_camera_block(self, parent) -> None:
        box = self.ttk.LabelFrame(parent, text="验证", padding=8)
        box.pack(fill="x", pady=(0, 8))
        self.camera_label = self.tk.Label(box, text="改完先保存，再点「测试摄像头」。",
                                          anchor="w", justify="left", wraplength=700,
                                          font=self.app.font_small, fg="#666")
        self.camera_label.pack(fill="x")
        bar = self.ttk.Frame(box)
        bar.pack(fill="x", pady=(6, 0))
        self.ttk.Button(bar, text="测试摄像头",
                        command=self.on_test_camera).pack(side="left")
        self.ttk.Button(bar, text="改回本机摄像头",
                        command=self.on_clear_camera_source).pack(side="left", padx=6)
        self.ttk.Label(box, text="测试需要摄像头空闲；监控运行中请先停止监控。",
                       font=self.app.font_small, foreground="#888").pack(fill="x", pady=(6, 0))

    def _build_gallery_block(self, parent) -> None:
        box = self.ttk.LabelFrame(parent, text="如何用手机访问", padding=8)
        box.pack(fill="x", pady=(0, 8))
        self.gallery_label = self.tk.Label(box, text="读取中…", anchor="w", justify="left",
                                           wraplength=700,
                                           font=self.app.font_small, fg="#666")
        self.gallery_label.pack(fill="x")
        self.ttk.Label(box, text="手机与电脑要连同一个 Wi-Fi；首次访问可能需要在 Windows 防火墙里放行。",
                       wraplength=700, justify="left",
                       font=self.app.font_small, foreground="#888").pack(fill="x", pady=(6, 0))

    def _show_gallery_info(self, info: dict) -> None:
        if not info:
            self.gallery_label.configure(text="相册地址读取失败。", fg="#993C1D")
            return
        local = str(info.get("local_url") or "")
        if not info.get("lan_enabled"):
            self.gallery_label.configure(
                text=f"当前仅本机可访问：{local}\n"
                     "要手机查看：把「允许局域网访问」改成 0.0.0.0，"
                     "并设置至少 12 位密码，保存后重启监控。",
                fg="#993C1D")
            return
        if not info.get("token_set"):
            self.gallery_label.configure(
                text="已允许局域网访问，但没有设置相册密码；相册会拒绝启动。\n"
                     "请在上面设置至少 12 位密码。",
                fg="#993C1D")
            return
        lines = [f"{item.get('interface')}：{item.get('url')}"
                 for item in (info.get("lan_urls") or [])[:4]]
        if not lines:
            self.gallery_label.configure(
                text="已允许局域网访问，但没有找到可用的局域网地址（检查 Wi-Fi/网线）。",
                fg="#993C1D")
            return
        self.gallery_label.configure(
            text="手机访问（同一 Wi-Fi，用户名随意，密码就是上面设置的相册密码）：\n"
                 + "\n".join(lines),
            fg="#0F6E56")

    # ---------------- 存储 ----------------

    def _build_storage_block(self, parent) -> None:
        box = self.ttk.LabelFrame(parent, text="存储与容量预测", padding=8)
        box.pack(fill="x", pady=(0, 8))
        self.storage_label = self.tk.Label(
            box, text="读取中…", anchor="w", justify="left", wraplength=700,
            font=self.app.font_small, fg="#666",
        )
        self.storage_label.pack(fill="x")

    def _show_storage_info(self, info: dict) -> None:
        """容量摘要。数字口径与 `rw storage` 完全一致（同一个 forecast）。"""
        if not info:
            self.storage_label.configure(text="存储统计将在监控运行后出现。", fg="#666")
            return

        gb = 1024 ** 3
        used = float(info.get("used_bytes") or 0) / gb
        max_bytes = float(info.get("max_bytes") or 0)
        free = info.get("free_bytes")
        forecast = info.get("forecast") or {}
        status = str(forecast.get("status") or "")
        lines = []
        if max_bytes > 0:
            lines.append(f"已用 {used:.1f} GB / 上限 {max_bytes / gb:.0f} GB")
        else:
            lines.append(f"已用 {used:.1f} GB（未设容量上限）")
        if free is not None:
            lines[-1] += f" · 磁盘剩余 {float(free) / gb:.1f} GB"

        daily = float(forecast.get("daily_bytes") or 0) / (1024 ** 2)
        if daily > 0:
            lines.append(f"最近增速约 {daily:.0f} MB/天")

        if status == "over":
            lines.append("⚠ 已超过容量上限，正在按最旧优先清理；"
                         "可调小保留天数或调大容量上限。")
            color = "#993C1D"
        elif status == "warning":
            days = forecast.get("days_to_cap")
            if days is not None:
                lines.append(f"⚠ 按当前增速，约 {days:.1f} 天后达到容量上限；"
                             "可调小保留天数或调大容量上限。")
            color = "#993C1D"
        elif status == "unlimited":
            lines.append("未设置容量上限与保留天数：磁盘由你自行管理。")
            color = "#8A5A00"
        else:
            projected = forecast.get("projected_days")
            keep = info.get("keep_days")
            if projected is not None:
                lines.append(f"按当前设置，文件预计保留约 {projected:.0f} 天。")
            elif keep:
                lines.append(f"文件按天保留 {keep} 天。")
            color = "#0F6E56"

        self.storage_label.configure(text="\n".join(lines), fg=color)

    # ---------------- 读取 ----------------

    def reload(self) -> None:
        self.status_label.configure(text="读取当前配置…", fg="#666")

        def work() -> dict:
            return self.app._run_cli(["config", "--json"], timeout=60)

        self.app._run_task(work, self._on_snapshot)

    def _on_snapshot(self, data: dict) -> None:
        if not data.get("ok") or not data.get("labels"):
            self.status_label.configure(
                text="读取配置失败：" + str(data.get("error") or "未知原因"), fg="#993C1D")
            return
        self.labels = data["labels"]
        self._fill_rows(data.get("values") or {}, data.get("secrets") or {})
        self._show_gallery_info(data.get("gallery") or {})
        self._show_heartbeat(data.get("heartbeat") or {})
        self._show_storage_info(data.get("storage") or {})
        self._show_channel_warning(
            data.get("channels") or [], data.get("outbox") or {},
            data.get("channel_issues") or {},
            events_deliverable=bool(data.get("events_deliverable", True)))
        self.status_label.configure(text="已读取。保存后会提示哪些设置需要重启。", fg="#666")

    def _fill_rows(self, values: dict, secrets: dict) -> None:
        """第一次建控件；之后只刷新值。

        重复建控件会让同一项出现两排，而且用户正打字的那一格会被替换掉，
        所以「建」和「填」必须分开。
        """
        if not self._built:
            for key, meta in self.labels.items():
                holder = self.rows_holder.get(meta.get("section") or "")
                if holder is None:
                    continue
                self._make_row(holder, key, meta, values.get(key), secrets.get(key) or {})
            self._built = True
        else:
            for key, var in self.vars.items():
                meta = self.labels.get(key) or {}
                if meta.get("secret") or key not in values:
                    continue
                raw = values[key]
                if meta.get("kind") == "bool":
                    var.set(bool(raw))
                else:
                    var.set("" if raw is None else str(raw))

        for key, label in self.secret_labels.items():
            label.configure(text=self._secret_text(secrets.get(key) or {}))
        for key, boxes in self.multi_vars.items():
            raw = values.get(key)
            if isinstance(raw, (list, tuple)):
                selected = {str(item).strip().lower() for item in raw}
            else:
                selected = {
                    item.strip().lower()
                    for item in str(raw or "").replace(";", ",").split(",")
                    if item.strip()
                }
            for name, var in boxes.items():
                var.set(name in selected)
        self._populated = True

    def _secret_text(self, info: dict) -> str:
        if info.get("set"):
            return f"已设置 {info.get('tail') or ''}（留空＝不改）"
        return "未设置"

    def _make_row(self, parent, key: str, meta: dict, value, secret_info: dict) -> None:
        tk, ttk = self.tk, self.ttk
        row = ttk.Frame(parent)
        row.pack(fill="x", pady=3)
        ttk.Label(row, text=meta.get("label") or key, width=24, anchor="w").pack(side="left")

        kind = meta.get("kind") or "text"
        if kind == "bool":
            var = tk.BooleanVar(value=bool(value))
            ttk.Checkbutton(row, variable=var).pack(side="left")
            self.vars[key] = var
        elif kind == "multi":
            frame = ttk.Frame(row)
            frame.pack(side="left", fill="x", expand=True)
            boxes = {}
            for choice in meta.get("choices") or ():
                var = tk.BooleanVar(value=False)
                ttk.Checkbutton(frame, text=str(choice), variable=var).pack(
                    side="left", padx=(0, 10))
                boxes[str(choice)] = var
            self.multi_vars[key] = boxes
        elif kind == "choice":
            var = tk.StringVar(value=str(value or ""))
            ttk.Combobox(row, textvariable=var, values=list(meta.get("choices") or ()),
                         state="readonly", width=18).pack(side="left")
            self.vars[key] = var
        else:
            var = tk.StringVar(value="" if value is None else str(value))
            ttk.Entry(row, textvariable=var, width=40).pack(side="left", fill="x", expand=True)
            self.vars[key] = var

        # 密钥状态放在输入框下一行：放右边会被窗口边缘裁掉，
        # 用户就会以为「明明配过却什么都没有」。
        if meta.get("secret"):
            info = ttk.Label(parent, text=self._secret_text(secret_info),
                             foreground="#888", justify="left")
            info.pack(fill="x", padx=(200, 0), pady=(0, 2))
            self.secret_labels[key] = info

        hint = str(meta.get("hint") or "")
        extra = RESTART_TEXT.get(str(meta.get("restart") or "watcher"), "")
        if extra and hint:
            text = f"{hint} · {extra}"
        elif extra:
            text = extra
        else:
            text = hint
        if text:
            ttk.Label(parent, text=text, foreground="#888",
                      wraplength=560, justify="left",
                      font=self.app.font_small).pack(fill="x", padx=(200, 0))

    # ---------------- 保存 ----------------

    def _collect(self) -> dict:
        changes: dict = {}
        for key, var in self.vars.items():
            meta = self.labels.get(key) or {}
            if meta.get("secret"):
                text = str(var.get() or "").strip()
                if text:
                    changes[key] = text
                continue
            if meta.get("kind") == "bool":
                changes[key] = "true" if bool(var.get()) else "false"
                continue
            text = str(var.get() or "").strip()
            if not text and meta.get("kind") in ("int", "float", "choice"):
                continue      # 空值不提交：老配置缺这个键时不该因此存不进去
            changes[key] = text
        for key, boxes in self.multi_vars.items():
            chosen = [name for name, var in boxes.items() if bool(var.get())]
            changes[key] = ",".join(chosen)
        return changes

    def _save_args(self) -> list[str]:
        pairs = [f"{key}={value}" for key, value in self._collect().items()]
        return ["config", "--set", *pairs, "--json"]

    def on_save(self) -> None:
        if self._saving or not self._populated:
            return
        self._saving = True
        self.btn_save.configure(state="disabled")
        self.status_label.configure(text="保存中…", fg="#666")
        args = self._save_args()

        def work() -> dict:
            return self.app._run_cli(args, timeout=60)

        self.app._run_task(work, self._on_saved)

    def _on_saved(self, result: dict) -> None:
        self._saving = False
        self.btn_save.configure(state="normal")
        if not result.get("ok"):
            errors = result.get("errors") or {}
            detail = "\n".join(f"· {msg}" for msg in errors.values()) or str(
                result.get("error") or "未知原因")
            self.status_label.configure(text="没有保存：有设置项不合法", fg="#993C1D")
            self.app.messagebox.showerror("保存失败", detail)
            return
        scopes = result.get("requires_restart") or []
        if scopes:
            names = {"watcher": "监控", "guardian": "守护进程", "gallery": "相册"}
            text = "已保存；需要重启：" + "、".join(names.get(s, s) for s in scopes)
        else:
            text = "已保存并立即生效。"
        self.status_label.configure(text=text, fg="#0F6E56")
        for key, var in self.vars.items():
            if (self.labels.get(key) or {}).get("secret"):
                var.set("")
        self.reload()
        self.app.refresh_identity()

    def on_test_push(self) -> None:
        if self._saving or not self._populated:
            return
        self._saving = True
        self.btn_save.configure(state="disabled")
        self.status_label.configure(text="保存并发送测试消息…", fg="#666")
        args = self._save_args()

        def work() -> dict:
            saved = self.app._run_cli(args, timeout=60)
            if not saved.get("ok"):
                return {"ok": False, "stage": "save", "detail": saved}
            tested = self.app._run_cli(["config", "--test", "--json"], timeout=60)
            return {"ok": bool(tested.get("ok")), "stage": "test", "detail": tested}

        self.app._run_task(work, self._on_test_push)

    def _on_test_push(self, result: dict) -> None:
        self._saving = False
        self.btn_save.configure(state="normal")
        detail = result.get("detail") or {}
        if result.get("ok"):
            self.status_label.configure(text="测试消息已发出，去手机看看。", fg="#0F6E56")
            self.app.messagebox.showinfo("测试消息", "发送成功。手机上应该收到一条「推送测试」。")
            for key, var in self.vars.items():
                if (self.labels.get(key) or {}).get("secret"):
                    var.set("")
            self.reload()
            return
        if result.get("stage") == "save":
            errors = detail.get("errors") or {}
            message = "\n".join(errors.values()) or str(detail.get("error") or "保存失败")
            self.app.messagebox.showerror("保存失败，未发送测试", message)
        else:
            message = str(detail.get("message") or detail.get("error") or detail.get("out") or "")
            self.app.messagebox.showerror(
                "测试消息没发出去",
                (message[-600:] or "没有返回原因") +
                "\n\n常见原因：webhook 粘错（应形如 .../webhook/send?key=xxx）、"
                "机器人已被移出群、当前渠道没有填凭据。")
        self.status_label.configure(text="测试未通过，配置已保存", fg="#993C1D")

    # ---------------- 摄像头 ----------------

    def on_test_camera(self) -> None:
        if self._saving or not self._populated:
            return
        # 用实时状态，别用主窗口每 2 秒刷一次的缓存：用户刚点完停止就进来测试，
        # 缓存里可能还留着已经死掉的 PID。
        from . import gui as gui_module

        state = gui_module.read_state(self.app.project)
        if state.pid:
            self.app.messagebox.showwarning(
                "请先停止监控", "摄像头同一时间只能被一个进程占用，先停止监控再测试。")
            return
        self._saving = True
        self.btn_save.configure(state="disabled")
        self.camera_label.configure(text="正在保存并测试摄像头…", fg="#666")
        args = self._save_args()

        def work() -> dict:
            saved = self.app._run_cli(args, timeout=60)
            if not saved.get("ok"):
                return {"ok": False, "stage": "save", "detail": saved}
            tested = self.app._run_cli(
                ["probe", "--config", str(self.app.config), "--json"],
                timeout=90, need_cv2=True,
            )
            return {"ok": bool(tested.get("ok")), "stage": "test", "detail": tested}

        self.app._run_task(work, self._on_test_camera)

    def _on_test_camera(self, result: dict) -> None:
        self._saving = False
        self.btn_save.configure(state="normal")
        detail = result.get("detail") or {}
        if result.get("ok"):
            size = ""
            if detail.get("width") and detail.get("height"):
                size = f"（{detail['width']}×{detail['height']}）"
            self.camera_label.configure(text=f"摄像头可用{size}，能读到画面。", fg="#0F6E56")
            self.app.messagebox.showinfo("摄像头可用", f"已经拍到画面{size}。")
            for key, var in self.vars.items():
                if (self.labels.get(key) or {}).get("secret"):
                    var.set("")
            self.reload()
            return
        if result.get("stage") == "save":
            errors = detail.get("errors") or {}
            message = "\n".join(errors.values()) or str(detail.get("error") or "保存失败")
            self.camera_label.configure(text="设置没有保存。", fg="#993C1D")
            self.app.messagebox.showerror("保存失败，未测试摄像头", message)
            return
        message = str(detail.get("error") or detail.get("out") or detail.get("err") or "")
        self.camera_label.configure(text="摄像头测试失败。", fg="#993C1D")
        self.app.messagebox.showerror(
            "摄像头不可用",
            (message[-600:] or "没有拿到原因") +
            "\n\n常见原因：编号不对、摄像头被别的程序占用、RTSP 地址或密码错误。")

    def on_clear_camera_source(self) -> None:
        if not self.app.messagebox.askyesno(
                "改回本机摄像头",
                "会删除本机配置里的网络摄像头地址，改用「本机摄像头编号」。\n\n继续吗？"):
            return

        def work() -> dict:
            # backend 可能是当初配 RTSP 时写下的 ffmpeg；不清掉会让本机摄像头
            # 继续走 FFmpeg 打开方式。source 和 backend 一起回落到默认值。
            return self.app._run_cli(
                ["config", "--unset", "camera.source", "camera.backend", "--json"],
                timeout=30)

        self.app._run_task(work, self._on_camera_source_cleared)

    def _on_camera_source_cleared(self, result: dict) -> None:
        if result.get("ok"):
            self.camera_label.configure(text="已改回本机摄像头编号。", fg="#0F6E56")
            self.reload()
        else:
            errors = result.get("errors") or {}
            self.app.messagebox.showerror(
                "操作失败", "\n".join(errors.values()) or str(result.get("error") or ""))

    # ---------------- 身份 ----------------

    def refresh_identity(self) -> None:
        def work() -> dict:
            return self.app._run_cli(["identity", "--json"], timeout=40)

        self.app._run_task(work, self._on_identity)

    def _on_identity(self, result: dict) -> None:
        info = result.get("identity") or {}
        if not info:
            self._show_owner_photo("")
            self.identity_label.configure(text="读取失败", fg="#993C1D")
            return
        if not info.get("registered"):
            self._show_owner_photo("")
            self.identity_label.configure(
                text="未注册。现在所有脸都按「访客」处理：会抓拍、会播报、会推送。", fg="#993C1D")
            return
        self._show_owner_photo(str(info.get("photo") or ""))
        created = str(info.get("created_at") or info.get("mtime") or "").replace("T", " ")
        self.identity_label.configure(
            text=f"已注册 · {info.get('samples') or 0} 个样本 · {info.get('dim') or 0} 维特征"
                 + (f" · {created}" if created else "")
                 + f"\n文件：{info.get('path') or ''}",
            fg="#0F6E56")

    def _show_owner_photo(self, path: str) -> None:
        """显示注册时留下的现场照片；没有就说明原因，不占位。"""
        self._owner_photo = None
        if not path:
            self.identity_photo.configure(
                image="", text="还没有本人照片；重新注册一次后这里会显示。")
            return
        try:
            img = self.tk.PhotoImage(file=path)
            factor = max(1, img.width() // 180)
            self._owner_photo = img.subsample(factor, factor)
            self.identity_photo.configure(image=self._owner_photo, text="")
        except Exception:  # noqa: BLE001 - 照片坏了不影响注册信息
            self.identity_photo.configure(
                image="", text="本人照片读取失败；重新注册可修复。")

    def _enroll_live(self) -> None:
        # 交给主窗口：注册要抢摄像头，还得保证监控是停的，逻辑不能在这里再写一遍
        self.win.lower()
        self.app.on_enroll()

    def _enroll_latest(self) -> None:
        self.win.lower()
        self.app.on_enroll_from_latest()

    def on_clear_identity(self) -> None:
        if not self.app.messagebox.askyesno(
                "清除本人模板",
                "删除后所有脸都会按访客处理（你自己进门也会被抓拍、推送）。\n\n确定清除吗？"):
            return

        def work() -> dict:
            return self.app._run_cli(["identity", "--clear", "--json"], timeout=30)

        self.app._run_task(work, self._on_cleared)

    def _on_cleared(self, result: dict) -> None:
        if result.get("ok"):
            self.app.messagebox.showinfo("已清除", "本人模板已删除。需要时重新注册即可。")
        else:
            self.app.messagebox.showerror("清除失败",
                                          str(result.get("error") or result.get("out") or ""))
        self.refresh_identity()
        self.app.refresh_identity()

    # ---------------- 自启 ----------------

    def refresh_autostart(self) -> None:
        def work() -> dict:
            return self.app._run_cli(["autostart", "--json"], timeout=30)

        self.app._run_task(work, self._on_autostart)

    def _on_autostart(self, result: dict) -> None:
        info = result.get("autostart") or {}
        if not info:
            self.autostart_label.configure(text="读取失败", fg="#993C1D")
            return
        if not info.get("supported"):
            self.autostart_label.configure(text="当前系统不支持（仅 Windows）", fg="#993C1D")
            return
        if not info.get("enabled"):
            text = "未开启。开机后需要自己点「启动监控」。"
            color = "#993C1D"
        elif info.get("points_to_this_install"):
            text = "已开启（指向当前安装目录）。"
            color = "#0F6E56"
        else:
            text = ("已开启，但指向别处：" + str(info.get("command") or "")
                    + "\n可能是旧安装目录留下的，建议关掉再重新开启。")
            color = "#993C1D"
        self.autostart_label.configure(text=text, fg=color)

    def on_autostart(self, enable: bool) -> None:
        action = "--enable" if enable else "--disable"

        def work() -> dict:
            return self.app._run_cli(["autostart", action, "--json"], timeout=30)

        self.status_label.configure(text="处理中…", fg="#666")
        self.app._run_task(work, self._on_autostart_changed)

    def _on_autostart_changed(self, result: dict) -> None:
        ok = bool(result.get("ok"))
        message = str(result.get("message") or result.get("error") or "")
        self.status_label.configure(text=message or ("完成" if ok else "失败"),
                                    fg="#0F6E56" if ok else "#993C1D")
        if not ok and message:
            self.app.messagebox.showwarning("自启设置未生效", message)
        self.refresh_autostart()

    # ---------------- 关闭 ----------------

    def close(self) -> None:
        try:
            self.win.unbind_all("<MouseWheel>")
        except Exception:  # noqa: BLE001 - 已销毁
            pass
        try:
            self.win.destroy()
        except Exception:  # noqa: BLE001 - 已销毁
            pass


def open_settings(app) -> Optional[SettingsDialog]:
    """打开（或抬到前面）设置窗口。同一时间只留一个。"""
    existing = getattr(app, "_settings_dialog", None)
    if existing is not None:
        try:
            if existing.win.winfo_exists():
                existing.win.lift()
                existing.win.focus_force()
                return existing
        except Exception:  # noqa: BLE001
            pass
    dialog = SettingsDialog(app)
    app._settings_dialog = dialog
    return dialog
