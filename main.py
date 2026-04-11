import asyncio
import io
import logging
import sys
import time
from typing import Any

import aiohttp
from textual import on
from textual.app import App, ComposeResult
from textual.containers import Grid, Horizontal, Vertical
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    Label,
    RichLog,
    Static,
    TabbedContent,
    TabPane,
)

import actions
import log_config
from checker import (
    check_services,
    check_single_service,
    get_langfuse_metrics,
    get_n8n_executions,
    get_qdrant_context_size,
)
from config_manager import ConfigManager
from screens import ConfirmScreen, DockerLogsScreen, HelpScreen, LogFileScreen, ServiceDetailScreen
from storage import Storage

logger = logging.getLogger(__name__)


def _fmt_response_time(res: dict[str, Any]) -> str:
    if res.get("status") in ("DOWN", "UNREACHABLE", "CIRCUIT_OPEN") or res.get("response_time_ms") is None:
        return "[dim]—[/dim]"
    try:
        ms = float(res["response_time_ms"])
    except (TypeError, ValueError):
        return "[dim]—[/dim]"
    if ms < 500:
        return f"[green]{ms:.0f}ms[/green]"
    if ms <= 2000:
        return f"[yellow]{ms:.0f}ms[/yellow]"
    return f"[red]{ms:.0f}ms[/red]"


def _fmt_uptime(pct: float | None) -> str:
    if pct is None:
        return "[dim]—[/dim]"
    if pct > 99.0:
        return f"[green]{pct:.1f}%[/green]"
    if pct >= 95.0:
        return f"[yellow]{pct:.1f}%[/yellow]"
    return f"[red]{pct:.1f}%[/red]"


def _fmt_uptime_cell(pct: float | None) -> str:
    if pct is None:
        return "[dim]—[/dim]"
    if pct > 99.0:
        return f"[green]{pct:.1f}%[/green]"
    if pct >= 95.0:
        return f"[yellow]{pct:.1f}%[/yellow]"
    return f"[red]{pct:.1f}%[/red]"


def _cost_sparkline_markup(storage: Storage, hours: int = 24) -> str:
    hist = storage.get_cost_history(hours)
    if not hist:
        return "[dim]Cost 24h: (no data)[/dim]"
    costs = [float(h["cost"]) for h in hist]
    lo, hi = min(costs), max(costs)
    chars = "▁▂▃▄▅▆▇█"
    if hi - lo < 1e-12:
        line = chars[4] * min(len(costs), 40)
    else:
        line = "".join(chars[min(7, int((c - lo) / (hi - lo) * 7))] for c in costs[-40:])
    last = costs[-1]
    return f"[bold #fdf500]Cost 24h:[/] {line}  [bold #00f0ff]${last:.2f}[/]"


def _fmt_event_row(ev: dict[str, Any]) -> tuple[str, str, str, str]:
    et = str(ev.get("event_type", ""))
    col = "#00f0ff"
    if et in ("heal", "restart"):
        col = "#fdf500"
    elif et in ("error", "budget_exceeded"):
        col = "#ff003c"
    elif et in ("sleep", "wake"):
        col = "#00f0ff"
    elif et in ("heal_success", "heal_failed"):
        col = "#00ff66" if et == "heal_success" else "#ff003c"
    elif et == "profile_switch":
        col = "#bd00ff"
    t = str(ev.get("created_at", ""))[:19]
    svc = ev.get("service_name") or "—"
    det = (ev.get("details") or "—")[:80]
    return (
        t,
        f"[{col}]{et}[/{col}]",
        str(svc),
        det,
    )


def _fmt_docker_status(status: str) -> str:
    s = status.lower()
    if "running" in s or " up " in s or s.startswith("up"):
        return f"[green]{status}[/green]"
    if "paused" in s:
        return f"[yellow]{status}[/yellow]"
    if "exited" in s or "dead" in s:
        return f"[red]{status}[/red]"
    return f"[yellow]{status}[/yellow]"


class VibeOpsCenter(App):
    """Textual dashboard for local AI stack health, budgets, Docker, and auto-heal."""

    TITLE = "Vibe Ops Center"
    SUB_TITLE = "v1.0.0"

    CSS = """
    Screen {
        background: #0a0a0f;
        color: #00f0ff;
    }
    Header {
        background: #ff003c;
        color: #fdf500;
        text-style: bold;
        height: 1;
    }
    #profile-strip {
        dock: top;
        height: 1;
        background: #12121c;
        color: #00f0ff;
        text-align: center;
        text-style: bold;
        border-bottom: solid #00f0ff;
    }
    TabbedContent {
        height: 1fr;
    }
    TabbedContent ContentSwitcher {
        border: none;
    }
    TabbedContent Tab {
        background: #12121c;
        color: #555555;
        border-bottom: solid #555555;
    }
    TabbedContent Tab:hover {
        color: #fdf500;
    }
    TabbedContent Tab.-active {
        background: #0a0a0f;
        color: #00f0ff;
        border-bottom: solid #00f0ff;
        text-style: bold;
    }
    #dash-root {
        height: 1fr;
    }
    #main-grid {
        grid-size: 1 3;
        grid-rows: 4 1fr 12;
        padding: 0 1;
        height: 1fr;
    }
    #top-metrics {
        border: heavy #00f0ff;
        border-title-align: left;
        background: #12121c;
        align: center middle;
        height: 100%;
    }
    #lf-metrics, #qdrant-metrics {
        text-style: bold;
        width: 1fr;
        content-align: center middle;
    }
    #lf-metrics { color: #fdf500; }
    #qdrant-metrics { color: #bd00ff; }

    DataTable {
        width: 100%;
        height: 1fr;
        background: #12121c;
        border: heavy #ff003c;
        border-title-align: left;
        color: #00f0ff;
    }
    DataTable > .datatable--header {
        background: #1e1e2e;
        color: #fdf500;
        text-style: bold;
    }
    DataTable > .datatable--cursor {
        background: #fdf500;
        color: #0a0a0f;
    }
    RichLog {
        height: 100%;
        background: #12121c;
        border: heavy #fdf500;
        border-title-align: left;
        color: #00f0ff;
    }
    #buttons-panel {
        height: 4;
        dock: bottom;
        padding: 0 2;
        align: center middle;
        background: #0a0a0f;
        border-top: heavy #00f0ff;
    }
    #heal-status {
        color: #555555;
        margin-left: 2;
        text-style: bold;
    }
    Button {
        margin: 0 1;
        min-width: 18;
        border: solid #00f0ff;
        background: #0a0a0f;
        color: #00f0ff;
        transition: background 200ms, color 200ms;
    }
    Button:hover {
        background: #00f0ff;
        color: #0a0a0f;
        text-style: bold;
    }
    #btn-sleep { border: solid #ff003c; color: #ff003c; }
    #btn-sleep:hover { background: #ff003c; color: #0a0a0f; }
    #btn-wake { border: solid #00ff66; color: #00ff66; }
    #btn-wake:hover { background: #00ff66; color: #0a0a0f; }
    #analytics-pane {
        height: 1fr;
        padding: 0 1;
    }
    #analytics-top {
        height: 12;
        margin-bottom: 1;
    }
    #analytics-cost {
        width: 1fr;
        min-width: 20;
        border: solid #00f0ff;
        background: #12121c;
        color: #00f0ff;
        padding: 0 1;
        content-align: left middle;
    }
    #uptime-matrix {
        width: 1fr;
        min-height: 8;
        border: solid #bd00ff;
    }
    #analytics-mid {
        height: 3;
        margin-bottom: 1;
        align: left middle;
    }
    #heal-stats {
        width: 1fr;
        border: solid #00ff66;
        background: #12121c;
        padding: 0 1;
        color: #00f0ff;
    }
    #btn-view-log {
        min-width: 18;
    }
    #events-table {
        height: 1fr;
        min-height: 8;
        border: solid #fdf500;
    }
    #docker-pane {
        height: 1fr;
        padding: 0 1;
    }
    #docker-table {
        height: 1fr;
        border: solid #00f0ff;
        margin-bottom: 1;
    }
    #docker-actions {
        height: 3;
        align: center middle;
    }
    """

    BINDINGS = [
        ("r", "restart_selected", "Restart Selected"),
        ("a", "toggle_autoheal", "Auto-Heal"),
        ("t", "fire_webhook", "Test Webhook"),
        ("d", "fix_docker", "Fix Docker"),
        ("i", "inspect", "Inspect"),
        ("c", "clear_logs", "Clear Logs"),
        ("1", "tab_dashboard", "Tab 1"),
        ("2", "tab_analytics", "Tab 2"),
        ("3", "tab_docker", "Tab 3"),
        ("question_mark", "help", "Help"),
        ("f1", "help", "Help"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.services: list[dict[str, Any]] = []
        self.auto_heal_enabled = False
        self.downtime_counter: dict[str, int] = {}
        self.is_sleeping = False
        self._n8n_update_counter = 0
        self.storage = Storage()
        self._last_cost: tuple[float, int] | None = None
        self.current_profile = "Unknown"
        self._docker_rows: list[dict[str, Any]] = []
        self._prev_status: dict[str, str] = {}
        self._notification_cooldowns: dict[tuple[str, str], float] = {}
        self._last_heal_time: float | None = None
        self._heal_fail_streak: dict[str, int] = {}
        self._unreachable: set[str] = set()

        config = ConfigManager.get()
        lf = config.get("langfuse_api", {})
        try:
            self.daily_budget_limit = float(lf.get("daily_budget_limit", 1.0)) if isinstance(lf, dict) else 1.0
        except (TypeError, ValueError):
            self.daily_budget_limit = 1.0

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static("⚡ Profile: [bold]Unknown[/]", id="profile-strip")
        with TabbedContent(initial="tab-dashboard"):
            with TabPane("⚡ Dashboard", id="tab-dashboard"):
                with Vertical(id="dash-root"):
                    with Grid(id="main-grid"):
                        with Horizontal(id="top-metrics"):
                            yield Label("📊 Langfuse: Loading...", id="lf-metrics")
                            yield Label("🧠 Qdrant: Loading...", id="qdrant-metrics")
                        yield DataTable(id="dash-services-table", zebra_stripes=True)
                        yield RichLog(id="n8n-log", markup=True)
                    with Horizontal(id="buttons-panel"):
                        yield Button("💤 Sleep", id="btn-sleep", variant="error")
                        yield Button("⚡ Wake Up", id="btn-wake", variant="success")
                        yield Button("☁ CLOUD", id="btn-profile-cloud", variant="primary")
                        yield Button("💻 LOCAL", id="btn-profile-local", variant="warning")
                        yield Label("🛡️ AutoHeal: OFF", id="heal-status")
            with TabPane("📈 Analytics", id="tab-analytics"):
                with Vertical(id="analytics-pane"):
                    with Horizontal(id="analytics-top"):
                        yield Static("[dim]Cost loading…[/]", id="analytics-cost")
                        yield DataTable(id="uptime-matrix", show_header=True, zebra_stripes=True)
                    with Horizontal(id="analytics-mid"):
                        yield Static("[dim]Auto-Heal Stats: loading…[/]", id="heal-stats")
                        yield Button("📄 View Log File", id="btn-view-log", variant="primary")
                    yield DataTable(id="events-table", zebra_stripes=True)
            with TabPane("🐳 Docker", id="tab-docker"):
                with Vertical(id="docker-pane"):
                    yield DataTable(id="docker-table", cursor_type="row", zebra_stripes=True)
                    with Horizontal(id="docker-actions"):
                        yield Button("🔄 Refresh", id="btn-dock-refresh")
                        yield Button("📋 Logs", id="btn-dock-logs")
                        yield Button("🔧 Fix Docker", id="btn-dock-fix")
        yield Footer()

    def _set_tab(self, tab_id: str) -> None:
        try:
            self.query_one(TabbedContent).active = tab_id
        except Exception:
            pass

    def action_tab_dashboard(self) -> None:
        self._set_tab("tab-dashboard")

    def action_tab_analytics(self) -> None:
        self._set_tab("tab-analytics")

    def action_tab_docker(self) -> None:
        self._set_tab("tab-docker")

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

    @on(TabbedContent.TabActivated)
    async def on_tabbed_content_tab_activated(self, event: TabbedContent.TabActivated) -> None:
        pane = getattr(event, "pane", None) or getattr(event, "tab", None)
        pid = str(getattr(pane, "id", "") or "")
        if pid == "tab-analytics":
            await self._refresh_events_table_only()
            await self._refresh_heal_stats()

    @on(Button.Pressed, "#btn-view-log")
    def open_log_file_screen(self) -> None:
        self.push_screen(LogFileScreen())

    async def on_unmount(self) -> None:
        logger.info("Application shutdown")
        try:
            self.storage.close()
        except Exception:
            logger.exception("storage.close failed")

    def _refresh_profile_strip(self) -> None:
        strip = self.query_one("#profile-strip", Static)
        if self.current_profile.upper() == "CLOUD":
            strip.update("☁ Profile: [bold magenta]CLOUD[/]")
        elif self.current_profile.upper() == "LOCAL":
            strip.update("⚡ Profile: [bold cyan]LOCAL[/]")
        else:
            strip.update("⚡ Profile: [dim]Unknown[/]")

    def _sound_enabled(self) -> bool:
        raw = ConfigManager.get().get("notifications", {})
        if not isinstance(raw, dict):
            return True
        return bool(raw.get("sound_enabled", True))

    def _bell(self, count: int = 1) -> None:
        if not self._sound_enabled():
            return
        try:
            sys.stdout.write("\a" * count)
            sys.stdout.flush()
        except OSError:
            pass

    def _notify_throttled(self, key: tuple[str, str], message: str, *, severity: str = "information") -> None:
        now = time.monotonic()
        last = self._notification_cooldowns.get(key, 0.0)
        if now - last < 60.0:
            return
        self._notification_cooldowns[key] = now
        self.notify(message, severity=severity)

    def _service_dict_by_name(self, name: str) -> dict[str, Any] | None:
        for s in ConfigManager.get().get("services", []):
            if isinstance(s, dict) and str(s.get("name")) == name:
                return s
        return None

    async def on_mount(self) -> None:
        logger.info("app mount")
        self.query_one("#top-metrics").border_title = "SYSTEM METRICS"

        table = self.query_one("#dash-services-table", DataTable)
        table.border_title = "SERVICE STATUS MONITOR"
        table.cursor_type = "row"
        table.can_focus = True
        table.add_columns("Service Name", "Status", "Response Time", "Uptime 24h")

        log = self.query_one("#n8n-log", RichLog)
        log.border_title = "SYSTEM LOGS & ACTION OUTPUT"

        um = self.query_one("#uptime-matrix", DataTable)
        um.border_title = "SERVICE UPTIME MATRIX"

        et = self.query_one("#events-table", DataTable)
        et.border_title = "RECENT EVENTS"

        hs = self.query_one("#heal-stats", Static)
        hs.border_title = "AUTO-HEAL STATS (today)"

        dt = self.query_one("#docker-table", DataTable)
        dt.border_title = "CONTAINER STATUS"
        dt.add_columns("Name", "Status", "CPU / RAM", "Ports", "Image")

        await asyncio.to_thread(self.storage.cleanup_old_data, 30)
        self._refresh_profile_strip()

        await self.update_status()
        await self._refresh_analytics_widgets()
        await self._refresh_docker_table()

        try:
            interval = float(ConfigManager.get().get("app", {}).get("polling_interval", 5.0))
        except (TypeError, ValueError):
            interval = 5.0
        self.set_interval(interval, self.update_status)
        self.set_interval(60.0, self._refresh_analytics_widgets)
        self.set_interval(30.0, self._refresh_events_table_only)
        self.set_interval(15.0, self._refresh_docker_table)

    async def _refresh_analytics_widgets(self) -> None:
        try:
            cost_txt = await asyncio.to_thread(_cost_sparkline_markup, self.storage, 24)
            self.query_one("#analytics-cost", Static).update(cost_txt)
        except Exception:
            pass
        try:
            await self._fill_uptime_matrix()
        except Exception:
            pass
        await self._refresh_heal_stats()

    async def _refresh_heal_stats(self) -> None:
        try:
            stats = await asyncio.to_thread(self.storage.get_heal_stats_today)
            total = int(stats.get("total_heals", 0))
            ok = int(stats.get("successful", 0))
            fail = int(stats.get("failed", 0))
            denom = ok + fail
            ok_pct = round(100.0 * ok / denom, 1) if denom else 0.0
            fail_pct = round(100.0 * fail / denom, 1) if denom else 0.0
            self.query_one("#heal-stats", Static).update(
                f"[bold #fdf500]Total heals:[/] {total}  "
                f"[green]OK:[/] {ok} ([green]{ok_pct}%[/])  "
                f"[red]Failed:[/] {fail} ([red]{fail_pct}%[/])"
            )
        except Exception:
            pass

    async def _refresh_events_table_only(self) -> None:
        try:
            events = await asyncio.to_thread(self.storage.get_recent_events, 30)
            et = self.query_one("#events-table", DataTable)
            et.clear()
            et.add_columns("Time", "Type", "Service", "Details")
            for ev in events:
                et.add_row(*_fmt_event_row(ev))
        except Exception:
            pass

    async def _fill_uptime_matrix(self) -> None:
        cfg = ConfigManager.get()
        services = [s for s in cfg.get("services", []) if isinstance(s, dict) and s.get("name")]
        um = self.query_one("#uptime-matrix", DataTable)
        um.clear()
        um.add_columns("Service", "1h", "6h", "12h", "24h")
        for s in services:
            name = str(s["name"])
            p1 = await asyncio.to_thread(self.storage.get_uptime_percent, name, 1)
            p6 = await asyncio.to_thread(self.storage.get_uptime_percent, name, 6)
            p12 = await asyncio.to_thread(self.storage.get_uptime_percent, name, 12)
            p24 = await asyncio.to_thread(self.storage.get_uptime_percent, name, 24)
            um.add_row(
                f"[cyan]{name}[/cyan]",
                _fmt_uptime_cell(p1),
                _fmt_uptime_cell(p6),
                _fmt_uptime_cell(p12),
                _fmt_uptime_cell(p24),
            )

    async def _refresh_docker_table(self) -> None:
        try:
            rows = await asyncio.to_thread(actions.inspect_containers)
            names = [str(r.get("name", "")).strip() for r in rows]
            stats_list = await asyncio.gather(
                *[asyncio.to_thread(actions.get_container_stats, nm) for nm in names],
                return_exceptions=True,
            )
            enriched: list[dict[str, Any]] = []
            for r, nm, stats in zip(rows, names, stats_list):
                if isinstance(stats, Exception):
                    stats = {}
                cpu = str(stats.get("cpu_percent", "—"))
                mem = str(stats.get("memory_usage", "—"))
                lim = str(stats.get("memory_limit", ""))
                ram_cell = f"{cpu} | {mem}" + (f" / {lim}" if lim else "")
                row = dict(r)
                row["_cpu_ram"] = ram_cell
                enriched.append(row)
            self._docker_rows = enriched
            dt = self.query_one("#docker-table", DataTable)
            cur = dt.cursor_row
            dt.clear()
            dt.add_columns("Name", "Status", "CPU / RAM", "Ports", "Image")
            for r in enriched:
                dt.add_row(
                    r.get("name", ""),
                    _fmt_docker_status(str(r.get("status", ""))),
                    str(r.get("_cpu_ram", "—")),
                    str(r.get("ports", "")),
                    str(r.get("image", "")),
                )
            if cur is not None and cur < len(enriched):
                dt.move_cursor(row=cur, animate=False)
        except Exception:
            self._docker_rows = []

    async def on_key(self, event: Any) -> None:
        if event.key == "enter":
            foc = self.focused
            if foc is not None and getattr(foc, "id", None) == "dash-services-table":
                tbl = self.query_one("#dash-services-table", DataTable)
                if tbl.cursor_row is not None:
                    await self.open_service_detail(int(tbl.cursor_row))

    async def open_service_detail(self, row_index: int) -> None:
        if not self.services or row_index < 0 or row_index >= len(self.services):
            return
        row = self.services[row_index]
        name = str(row["name"])
        status = str(row["status"])

        async def do_restart() -> None:
            await self.restart_service_interactive(name, status)

        await self.push_screen_wait(ServiceDetailScreen(self.storage, row, on_restart=do_restart))

    async def restart_service_interactive(self, name: str, status: str) -> None:
        if status == "UP":
            ok = await self.push_screen_wait(ConfirmScreen("Service is UP. Restart anyway?", "Yes", "No"))
            if not ok:
                return
        if status not in ("UP", "DEGRADED", "DOWN", "UNREACHABLE", "CIRCUIT_OPEN"):
            return
        self.notify(f"Restarting {name}...")
        try:
            res = await asyncio.to_thread(actions.restart_service, name)
            await asyncio.to_thread(self.storage.record_event, "restart", name, res)
            self.query_one("#n8n-log", RichLog).write(f"[yellow]Restart {name}: {res}[/yellow]")
            if "Error" not in res and "Exception" not in res:
                self._unreachable.discard(name)
                self._heal_fail_streak.pop(name, None)
                logger.info("manual restart cleared UNREACHABLE for %s", name)
            if "Error" in res or "Exception" in res:
                await asyncio.to_thread(self.storage.record_event, "error", name, res)
            await self.update_status()
        except Exception as e:
            await asyncio.to_thread(self.storage.record_event, "error", name, str(e))
            self.query_one("#n8n-log", RichLog).write(f"[red]Restart error: {e}[/red]")

    async def update_status(self) -> None:
        try:
            autoheal_threshold = int(ConfigManager.get().get("app", {}).get("autoheal_threshold", 3))
        except (TypeError, ValueError):
            autoheal_threshold = 3

        self.services = await check_services()

        table = self.query_one("#dash-services-table", DataTable)
        cursor_row = table.cursor_row
        table.clear()
        table.add_columns("Service Name", "Status", "Response Time", "Uptime 24h")

        for res in self.services:
            name = str(res["name"])
            raw_status = str(res["status"])

            if name in self._unreachable:
                display_status = "UNREACHABLE"
            else:
                display_status = raw_status

            prev = self._prev_status.get(name)
            if prev == "UP" and raw_status == "DOWN" and name not in self._unreachable and raw_status != "CIRCUIT_OPEN":
                self._bell(1)
                self._notify_throttled((name, "down"), f"{name} is DOWN", severity="error")

            if raw_status == "CIRCUIT_OPEN":
                pass
            elif raw_status == "DOWN" and name not in self._unreachable:
                self.downtime_counter[name] = self.downtime_counter.get(name, 0) + 1
                if self.auto_heal_enabled and self.downtime_counter[name] >= autoheal_threshold:
                    can_heal = self._last_heal_time is None or (time.monotonic() - self._last_heal_time) >= 15.0
                    if can_heal:
                        self._notify_throttled((name, "autoheal"), f"Auto-healing {name}...", severity="error")
                        self._last_heal_time = time.monotonic()
                        self.downtime_counter[name] = 0

                        async def auto_heal(n: str, st: Storage, app: "VibeOpsCenter") -> None:
                            logger.info("auto-heal start service=%s", n)
                            healer_result = await asyncio.to_thread(actions.restart_service, n)
                            app.query_one("#n8n-log", RichLog).write(
                                f"[yellow]Heal Output ({n}): {healer_result}[/yellow]"
                            )
                            await asyncio.to_thread(st.record_event, "heal", n, healer_result)
                            if "Error" in healer_result or "Exception" in healer_result:
                                await asyncio.to_thread(st.record_event, "error", n, healer_result)
                                logger.warning("Heal restart error for %s: %s", n, healer_result)
                                return
                            await asyncio.sleep(10)
                            svc = app._service_dict_by_name(n)
                            if not svc:
                                return
                            async with aiohttp.ClientSession() as session:
                                verified = await check_single_service(session, svc)
                            vstatus = str(verified.get("status", "DOWN"))
                            if vstatus in ("UP", "DEGRADED"):
                                await asyncio.to_thread(st.record_event, "heal_success", n, vstatus)
                                app._heal_fail_streak.pop(n, None)
                                logger.info("Heal verified OK for %s (%s)", n, vstatus)
                            else:
                                logger.warning("Heal failed for %s (still %s)", n, vstatus)
                                await asyncio.to_thread(st.record_event, "heal_failed", n, vstatus)
                                streak = app._heal_fail_streak.get(n, 0) + 1
                                app._heal_fail_streak[n] = streak
                                if streak >= 3:
                                    app._unreachable.add(n)
                                    logger.warning("Service %s marked UNREACHABLE after %s failed heals", n, streak)
                                    app.notify(
                                        f"{n} is UNREACHABLE — auto-heal disabled. Use manual restart.",
                                        severity="error",
                                        timeout=60,
                                    )

                        asyncio.create_task(auto_heal(name, self.storage, self))
                    else:
                        self.downtime_counter[name] = autoheal_threshold
            elif raw_status in ("UP", "DEGRADED"):
                self.downtime_counter[name] = 0

            status_map = {
                "UP": "[bold green]● UP[/bold green]",
                "DEGRADED": "[bold yellow]◑ DEGRADED[/bold yellow]",
                "DOWN": "[bold red]○ DOWN[/bold red]",
                "CIRCUIT_OPEN": "[bold red]○ CIRCUIT OPEN[/bold red]",
                "UNREACHABLE": "[bold #bd00ff]◆ UNREACHABLE[/bold #bd00ff]",
            }
            rt_val: float | None = None if res.get("response_time_ms") is None else float(res["response_time_ms"])
            if display_status == "UNREACHABLE":
                rt_val = None
            await asyncio.to_thread(self.storage.record_check, name, display_status, rt_val)
            uptime_pct = await asyncio.to_thread(self.storage.get_uptime_percent, name, 24)
            table.add_row(
                f"[cyan]{name}[/cyan]",
                status_map.get(display_status, display_status),
                _fmt_response_time({**res, "status": display_status}),
                _fmt_uptime(uptime_pct),
            )
            res["status"] = display_status
            self._prev_status[name] = raw_status

        if cursor_row is not None and cursor_row < len(self.services):
            table.move_cursor(row=cursor_row, animate=False)

        lf = await get_langfuse_metrics()
        cost = float(lf["cost"])
        traces = int(lf["traces"])

        if self._last_cost is None or self._last_cost != (cost, traces):
            await asyncio.to_thread(self.storage.record_cost, cost, traces)
            self._last_cost = (cost, traces)

        hist_1h = await asyncio.to_thread(self.storage.get_cost_history, 1)
        cost_baseline: float | None = None
        if hist_1h:
            cost_baseline = float(hist_1h[0]["cost"])
        if cost_baseline is None or abs(cost - cost_baseline) < 1e-12:
            trend = "[green]→[/green]"
        elif cost > cost_baseline:
            trend = "[red]↑[/red]"
        else:
            trend = "[green]→[/green]"

        self.query_one("#lf-metrics", Label).update(
            f"📊 Langfuse — Traces: [bold]{traces}[/] | Cost: [bold]${cost:.4f}[/] {trend}"
        )

        qdrant = await get_qdrant_context_size()
        self.query_one("#qdrant-metrics", Label).update(f"🧠 {qdrant}")

        if cost >= self.daily_budget_limit and not self.is_sleeping:
            self._bell(2)
            logger.warning("budget exceeded cost=%s limit=%s", cost, self.daily_budget_limit)
            self._notify_throttled(
                ("*", "budget_exceeded"),
                "BUDGET LIMIT EXCEEDED!",
                severity="error",
            )
            await asyncio.to_thread(self.storage.record_event, "budget_exceeded", None, f"{cost:.6f}")
            self.is_sleeping = True
            try:
                await asyncio.to_thread(actions.toggle_sleep_mode, True)
            except Exception as e:
                await asyncio.to_thread(self.storage.record_event, "error", None, str(e))

        self._n8n_update_counter += 1
        if self._n8n_update_counter % 6 == 0:
            n8n_lines = await get_n8n_executions()
            log = self.query_one("#n8n-log", RichLog)
            log.write("[bold cyan]┌─ n8n Recent ──────────────┐[/bold cyan]")
            for line in n8n_lines:
                log.write(f"[cyan]│[/cyan] {line}")
            log.write("[bold cyan]└───────────────────────────┘[/bold cyan]")

    @on(Button.Pressed, "#btn-sleep")
    async def sleep_mode(self) -> None:
        ok = await self.push_screen_wait(ConfirmScreen("Stop ALL services?", "Confirm", "Cancel"))
        if not ok:
            return
        self.notify("Engaging Sleep Mode...", title="System")
        self.is_sleeping = True
        try:
            result = await asyncio.to_thread(actions.toggle_sleep_mode, True)
            await asyncio.to_thread(self.storage.record_event, "sleep", None, result)
            self.query_one("#n8n-log", RichLog).write(f"[bold red]Sleep Mode: {result}[/bold red]")
        except Exception as e:
            await asyncio.to_thread(self.storage.record_event, "error", None, str(e))
            self.query_one("#n8n-log", RichLog).write(f"[bold red]Sleep error: {e}[/bold red]")

    @on(Button.Pressed, "#btn-wake")
    async def wake_mode(self) -> None:
        self.notify("Waking up systems...", title="System")
        self.is_sleeping = False
        try:
            result = await asyncio.to_thread(actions.toggle_sleep_mode, False)
            await asyncio.to_thread(self.storage.record_event, "wake", None, result)
            self.query_one("#n8n-log", RichLog).write(f"[bold green]Wake Up: {result}[/bold green]")
        except Exception as e:
            await asyncio.to_thread(self.storage.record_event, "error", None, str(e))
            self.query_one("#n8n-log", RichLog).write(f"[bold red]Wake error: {e}[/bold red]")

    @on(Button.Pressed, "Button#btn-profile-cloud, Button#btn-profile-local")
    async def handle_profiles(self, event: Button.Pressed) -> None:
        profile = "cloud" if "cloud" in str(event.button.id) else "local"
        self.notify(f"Switching to {profile.upper()}...", severity="information")
        try:
            result = await asyncio.to_thread(actions.switch_profile, profile)
            await asyncio.to_thread(self.storage.record_event, "profile_switch", None, profile)
            if "Error" not in result and "Exception" not in result:
                self.current_profile = "CLOUD" if profile == "cloud" else "LOCAL"
                self._refresh_profile_strip()
            self.query_one("#n8n-log", RichLog).write(f"[yellow]Profile Action: {result}[/yellow]")
            if "Error" in result or "Exception" in result:
                await asyncio.to_thread(self.storage.record_event, "error", None, result)
        except Exception as e:
            await asyncio.to_thread(self.storage.record_event, "error", None, str(e))
            self.query_one("#n8n-log", RichLog).write(f"[red]Profile error: {e}[/red]")

    async def action_toggle_autoheal(self) -> None:
        self.auto_heal_enabled = not self.auto_heal_enabled
        status_label = self.query_one("#heal-status", Label)
        if self.auto_heal_enabled:
            status_label.update("🛡️ AutoHeal: [bold green]ON[/]")
            status_label.styles.color = "#00ff66"
        else:
            status_label.update("🛡️ AutoHeal: OFF")
            status_label.styles.color = "#555555"

    async def action_clear_logs(self) -> None:
        self.query_one("#n8n-log", RichLog).clear()

    async def action_fix_docker(self) -> None:
        self.notify("Docker Maintenance...", severity="information")
        try:
            result = await asyncio.to_thread(actions.ensure_docker_running)
            self.query_one("#n8n-log", RichLog).write(f"[yellow]Docker: {result}[/yellow]")
            if "Error" in result:
                await asyncio.to_thread(self.storage.record_event, "error", None, result)
        except Exception as e:
            await asyncio.to_thread(self.storage.record_event, "error", None, str(e))

    async def action_inspect(self) -> None:
        self.notify("Inspecting Docker stacks...", severity="information")
        try:
            output = await asyncio.to_thread(actions.inspect_containers_text)
            log = self.query_one("#n8n-log", RichLog)
            log.write(f"\n[bold magenta]┌{'─' * 40}┐[/bold magenta]")
            log.write("[bold magenta]│ DOCKER INSPECTION REPORT               │[/bold magenta]")
            log.write(f"[bold magenta]└{'─' * 40}┘[/bold magenta]")
            log.write(output)
            if output.startswith("(") or "Error" in output:
                await asyncio.to_thread(self.storage.record_event, "error", None, output)
        except Exception as e:
            await asyncio.to_thread(self.storage.record_event, "error", None, str(e))

    @on(DataTable.RowSelected, "#dash-services-table")
    async def on_dash_row_selected(self, event: DataTable.RowSelected) -> None:
        await self.open_service_detail(int(event.cursor_row))

    async def action_restart_selected(self) -> None:
        table = self.query_one("#dash-services-table", DataTable)
        if table.cursor_row is not None:
            row = self.services[int(table.cursor_row)]
            await self.restart_service_interactive(str(row["name"]), str(row["status"]))

    async def action_fire_webhook(self) -> None:
        self.notify("Firing E2E Webhook...")
        try:
            result = await asyncio.to_thread(actions.fire_test_webhook)
            if "Error" in result or "Exception" in result:
                await asyncio.to_thread(self.storage.record_event, "error", None, result)
        except Exception as e:
            await asyncio.to_thread(self.storage.record_event, "error", None, str(e))

    @on(Button.Pressed, "#btn-dock-refresh")
    async def docker_refresh_btn(self) -> None:
        await self._refresh_docker_table()

    @on(Button.Pressed, "#btn-dock-logs")
    async def docker_logs_btn(self) -> None:
        dt = self.query_one("#docker-table", DataTable)
        r = dt.cursor_row
        if r is None or r < 0 or r >= len(self._docker_rows):
            self.notify("Select a container row first", severity="warning")
            return
        name = str(self._docker_rows[r].get("name", "")).strip()
        if not name:
            self.notify("Could not read container name", severity="error")
            return
        await self.push_screen_wait(DockerLogsScreen(name))

    @on(Button.Pressed, "#btn-dock-fix")
    async def docker_fix_btn(self) -> None:
        res = await asyncio.to_thread(actions.ensure_docker_running)
        self.notify(res, severity="information")
        if "Error" in res:
            await asyncio.to_thread(self.storage.record_event, "error", None, res)
        await self._refresh_docker_table()


if sys.stdout.encoding != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

if __name__ == "__main__":
    log_config.setup_logging()
    VibeOpsCenter().run()
