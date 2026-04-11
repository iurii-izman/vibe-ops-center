"""Modal Textual screens: service details, confirmations, help, Docker and file logs."""

from __future__ import annotations

import asyncio
from typing import Any, Callable, Coroutine

from textual import on
from textual.containers import Container, Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Label, RichLog, Static

import actions
from config_manager import ConfigManager
from log_config import LOG_PATH
from storage import Storage


def _service_config_map() -> dict[str, dict[str, Any]]:
    cfg = ConfigManager.get()
    out: dict[str, dict[str, Any]] = {}
    for s in cfg.get("services", []):
        if isinstance(s, dict) and s.get("name"):
            out[str(s["name"])] = s
    return out


class HelpScreen(ModalScreen[None]):
    """Справка по горячим клавишам и вкладкам."""

    BINDINGS = [("escape", "close", "Close"), ("f1", "close", "Close")]

    CSS = """
    HelpScreen {
        align: center middle;
    }
    #help-box {
        width: 70;
        height: auto;
        max-height: 90%;
        border: heavy #00f0ff;
        background: #0a0a0f;
        padding: 1 2;
    }
    #help-title {
        text-style: bold;
        color: #fdf500;
        margin-bottom: 1;
    }
    #help-body {
        color: #00f0ff;
        margin-bottom: 1;
    }
    #help-close {
        border: solid #00f0ff;
        background: #12121c;
        color: #00f0ff;
        min-width: 16;
    }
    """

    def compose(self) -> Any:
        with Container(id="help-box"):
            yield Static("VIBE OPS CENTER — HELP", id="help-title")
            yield Static(
                "[bold]Версия:[/bold] v1.0.0\n\n"
                "[bold #fdf500]Вкладки[/]\n"
                "  [cyan]1[/] — Dashboard  [cyan]2[/] — Analytics  [cyan]3[/] — Docker\n\n"
                "[bold #fdf500]Горячие клавиши[/]\n"
                "  [cyan]r[/] — Restart выбранного сервиса\n"
                "  [cyan]a[/] — Auto-Heal вкл/выкл\n"
                "  [cyan]t[/] — Test webhook\n"
                "  [cyan]d[/] — Fix Docker (daemon)\n"
                "  [cyan]i[/] — Inspect (дамп в лог Dashboard)\n"
                "  [cyan]c[/] — Clear logs\n"
                "  [cyan]Enter[/] или двойной клик по строке — детали сервиса\n"
                "  [cyan]?[/] / [cyan]F1[/] — Эта справка\n"
                "  Analytics: кнопка [cyan]View Log File[/] — хвост vibe_ops.log\n\n"
                "[dim]Cyberpunk UI: #0a0a0f / #00f0ff / #ff003c / #fdf500 / #bd00ff / #00ff66[/]",
                id="help-body",
            )
            yield Button("Close", id="help-close", variant="primary")

    def action_close(self) -> None:
        self.dismiss()

    @on(Button.Pressed, "#help-close")
    def close_btn(self) -> None:
        self.dismiss()


class ConfirmScreen(ModalScreen[bool]):
    """Да / Нет."""

    CSS = """
    ConfirmScreen {
        align: center middle;
    }
    #confirm-box {
        width: 56;
        border: heavy #fdf500;
        background: #0a0a0f;
        padding: 1 2;
    }
    #confirm-msg {
        color: #00f0ff;
        margin-bottom: 1;
    }
    Button {
        margin: 0 1;
        min-width: 14;
        border: solid #00f0ff;
        background: #12121c;
        color: #00f0ff;
    }
    #btn-yes { border: solid #00ff66; color: #00ff66; }
    #btn-no { border: solid #ff003c; color: #ff003c; }
    """

    def __init__(self, message: str, yes_label: str = "Yes", no_label: str = "No") -> None:
        super().__init__()
        self._message = message
        self._yes = yes_label
        self._no = no_label

    def compose(self) -> Any:
        with Container(id="confirm-box"):
            yield Static(self._message, id="confirm-msg")
            with Horizontal():
                yield Button(self._yes, id="btn-yes", variant="success")
                yield Button(self._no, id="btn-no", variant="error")

    @on(Button.Pressed, "#btn-yes")
    def yes(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#btn-no")
    def no(self) -> None:
        self.dismiss(False)


class DockerLogsScreen(ModalScreen[None]):
    BINDINGS = [("escape", "close", "Close")]

    CSS = """
    DockerLogsScreen {
        align: center middle;
    }
    #log-box {
        width: 90;
        height: 28;
        border: heavy #bd00ff;
        background: #0a0a0f;
        padding: 0 1;
    }
    DockerLogsScreen RichLog {
        height: 1fr;
        border: solid #00f0ff;
        background: #12121c;
        color: #00f0ff;
    }
    """

    def __init__(self, container_name: str) -> None:
        super().__init__()
        self._name = container_name

    def compose(self) -> Any:
        with Vertical(id="log-box"):
            yield Label(f"[bold #fdf500]docker logs[/] [cyan]{self._name}[/]", id="log-title")
            yield RichLog(id="docker-log-body", markup=True, wrap=True)
            yield Button("Close", id="docker-log-close")

    async def on_mount(self) -> None:
        body = self.query_one("#docker-log-body", RichLog)
        out = await asyncio.to_thread(actions.get_container_logs, self._name, 50)
        body.write(out if out else "[dim](empty)[/dim]")

    def action_close(self) -> None:
        self.dismiss()

    @on(Button.Pressed, "#docker-log-close")
    def close_b(self) -> None:
        self.dismiss()


class ServiceDetailScreen(ModalScreen[None]):
    """Детали сервиса + Restart / Close."""

    BINDINGS = [("escape", "close", "Close")]

    def __init__(
        self,
        storage: Storage,
        service_row: dict[str, Any],
        on_restart: Callable[[], Coroutine[Any, Any, None]],
    ) -> None:
        super().__init__()
        self._storage = storage
        self._row = service_row
        self._on_restart = on_restart
        self._status = str(service_row.get("status", "DOWN"))

    CSS = """
    ServiceDetailScreen {
        align: center middle;
    }
    #svc-box {
        width: 76;
        height: auto;
        max-height: 92%;
        background: #12121c;
        padding: 0 1 1 1;
    }
    #svc-head {
        text-style: bold;
        color: #fdf500;
        margin-bottom: 1;
    }
    #svc-meta {
        color: #00f0ff;
        margin-bottom: 1;
    }
    ServiceDetailScreen DataTable {
        height: 8;
        border: solid #555555;
        background: #0a0a0f;
    }
    ServiceDetailScreen Button {
        margin-top: 1;
        min-width: 14;
        border: solid #00f0ff;
        background: #0a0a0f;
        color: #00f0ff;
    }
    #svc-restart { border: solid #fdf500; color: #fdf500; }
    """

    def compose(self) -> Any:
        with Vertical(id="svc-box"):
            yield Static("", id="svc-head")
            yield Static("", id="svc-meta")
            yield DataTable(id="svc-history")
            with Horizontal():
                yield Button("Restart", id="svc-restart", variant="warning")
                yield Button("Close", id="svc-close")

    async def on_mount(self) -> None:
        name = str(self._row["name"])
        cfg_map = _service_config_map()
        meta = cfg_map.get(name, {})
        url = str(meta.get("url", self._row.get("url", "")))
        stype = str(meta.get("type", "?"))

        border_colors = {
            "UP": "#00ff66",
            "DEGRADED": "#fdf500",
            "DOWN": "#ff003c",
            "UNREACHABLE": "#bd00ff",
            "CIRCUIT_OPEN": "#ff003c",
        }
        bc = border_colors.get(self._status, "#00f0ff")
        box = self.query_one("#svc-box", Vertical)
        box.styles.border = ("heavy", bc)

        self.query_one("#svc-head", Static).update(f"[bold]{name}[/]  [{self._status}]")

        up24 = await asyncio.to_thread(self._storage.get_uptime_percent, name, 24)
        avg1 = await asyncio.to_thread(self._storage.get_avg_response_time, name, 1)
        up_s = f"{up24:.1f}%" if up24 is not None else "—"
        avg_s = f"{avg1:.1f} ms" if avg1 is not None else "—"
        self.query_one("#svc-meta", Static).update(
            f"URL: [cyan]{url}[/]\nType: [bold]{stype}[/]\nUptime 24h: [bold]{up_s}[/]  |  Avg RT 1h: [bold]{avg_s}[/]"
        )

        hist = await asyncio.to_thread(self._storage.get_status_history, name, 10)
        dt = self.query_one("#svc-history", DataTable)
        dt.add_columns("Time", "Status", "RT ms")
        for h in reversed(hist):
            ts = str(h.get("checked_at", ""))[:19]
            st = str(h.get("status", ""))
            rt = h.get("response_time_ms")
            rt_s = "—" if rt is None else f"{float(rt):.1f}"
            dt.add_row(ts, st, rt_s)

    def action_close(self) -> None:
        self.dismiss()

    @on(Button.Pressed, "#svc-close")
    def close_btn(self) -> None:
        self.dismiss()

    @on(Button.Pressed, "#svc-restart")
    async def do_restart(self) -> None:
        await self._on_restart()
        self.dismiss()


class LogFileScreen(ModalScreen[None]):
    """Последние строки vibe_ops.log."""

    BINDINGS = [("escape", "close", "Close")]

    CSS = """
    LogFileScreen {
        align: center middle;
    }
    #logfile-box {
        width: 90;
        height: 28;
        border: heavy #00f0ff;
        background: #0a0a0f;
        padding: 0 1;
    }
    LogFileScreen RichLog {
        height: 1fr;
        border: solid #fdf500;
        background: #12121c;
        color: #00f0ff;
    }
    """

    def compose(self) -> Any:
        with Vertical(id="logfile-box"):
            yield Label("[bold #fdf500]vibe_ops.log[/] (last 50 lines)", id="logfile-title")
            yield RichLog(id="logfile-body", markup=False, wrap=True)
            yield Button("Close", id="logfile-close")

    async def on_mount(self) -> None:
        body = self.query_one("#logfile-body", RichLog)

        def _read_tail() -> str:
            if not LOG_PATH.is_file():
                return "(log file not found)"
            try:
                text = LOG_PATH.read_text(encoding="utf-8", errors="replace")
            except OSError as e:
                return f"(read error: {e})"
            lines = text.splitlines()
            return "\n".join(lines[-50:]) if lines else "(empty)"

        out = await asyncio.to_thread(_read_tail)
        body.write(out)

    def action_close(self) -> None:
        self.dismiss()

    @on(Button.Pressed, "#logfile-close")
    def close_logfile(self) -> None:
        self.dismiss()
