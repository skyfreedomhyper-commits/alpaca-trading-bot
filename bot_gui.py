"""
Alpaca Trading Bot V6 — GUI Dashboard (tabbed)
三功能整合：試跑驗證 / Paper 交易 / 陰陽燭回測
"""

import json
import os
import sys
import webbrowser
from datetime import datetime, timezone

import pandas as pd
from dotenv import load_dotenv
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
from matplotlib.figure import Figure
from matplotlib.patches import Rectangle

from PyQt6.QtCore import Qt, QTimer, QThread, pyqtSignal, QProcess
from PyQt6.QtGui import QFont, QColor, QTextCursor
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QSplitter,
    QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QGroupBox, QTableWidget, QTableWidgetItem,
    QTextEdit, QStatusBar, QHeaderView, QSizePolicy,
    QLineEdit, QMessageBox, QTabWidget, QScrollArea, QFrame,
)

# ── paths ────────────────────────────────────────────────────────
_DIR           = os.path.dirname(os.path.abspath(__file__))
WATCHLIST_FILE = os.path.join(_DIR, "watchlist.json")
load_dotenv(os.path.join(_DIR, ".env"))
_API_KEY      = os.getenv("ALPACA_API_KEY", "")
_API_SECRET   = os.getenv("ALPACA_API_SECRET", "")
PAPER_TRADING = True  # 預設為 Paper Trade

# ── tab indices ──────────────────────────────────────────────────
TAB_OVERVIEW  = 0
TAB_ACCOUNT   = 1
TAB_WATCHLIST = 2
TAB_POSITIONS = 3
TAB_ORDERS    = 4
TAB_BACKTEST  = 5

# ── colours (Catppuccin Mocha) ───────────────────────────────────
C_BG      = "#1e1e2e"
C_CARD    = "#2a2a3e"
C_BORDER  = "#45475a"
C_TEXT    = "#cdd6f4"
C_SUBTEXT = "#6c7086"
C_GREEN   = "#a6e3a1"
C_RED     = "#f38ba8"
C_YELLOW  = "#f9e2af"
C_BLUE    = "#89b4fa"
C_PURPLE  = "#cba6f7"
C_LOG_BG  = "#0d0d1a"

_DETAIL_BTN = (
    f"QPushButton{{border:none;color:{C_SUBTEXT};font-size:11px;"
    f"background:transparent;padding:2px 4px;}}"
    f"QPushButton:hover{{color:{C_BLUE};}}"
)
_BASE_STYLE = f"""
    QMainWindow,QWidget{{background:{C_BG};color:{C_TEXT};}}
    QGroupBox{{background:{C_CARD};border:1px solid {C_BORDER};border-radius:6px;
               margin-top:10px;padding:8px;font-weight:bold;color:{C_BLUE};}}
    QGroupBox::title{{subcontrol-origin:margin;left:10px;padding:0 4px;}}
    QTableWidget{{background:{C_CARD};color:{C_TEXT};border:none;gridline-color:{C_BORDER};}}
    QTableWidget::item{{padding:3px 6px;}}
    QHeaderView::section{{background:{C_BG};color:{C_SUBTEXT};border:none;
                          padding:4px 6px;font-weight:bold;}}
    QScrollArea{{border:none;background:{C_BG};}}
    QScrollBar:vertical{{background:{C_BG};width:8px;border-radius:4px;}}
    QScrollBar::handle:vertical{{background:{C_BORDER};border-radius:4px;}}
    QPushButton{{background:{C_CARD};color:{C_TEXT};border:1px solid {C_BORDER};
                 border-radius:5px;padding:6px 18px;font-size:13px;}}
    QPushButton:hover{{background:{C_BORDER};}}
    QPushButton:disabled{{color:{C_SUBTEXT};border-color:{C_BORDER};}}
    QLineEdit{{background:{C_BG};color:{C_TEXT};border:1px solid {C_BORDER};
               border-radius:4px;padding:5px 8px;font-size:13px;}}
    QLineEdit:focus{{border-color:{C_BLUE};}}
    QTabWidget::pane{{border:none;background:{C_BG};}}
    QTabBar::tab{{background:{C_CARD};color:{C_SUBTEXT};padding:9px 14px;
                  border:none;border-bottom:2px solid transparent;
                  min-width:60px;font-size:12px;}}
    QTabBar::tab:selected{{color:{C_BLUE};border-bottom:2px solid {C_BLUE};
                           background:{C_BG};font-weight:bold;}}
    QTabBar::tab:hover:!selected{{color:{C_TEXT};background:{C_BG};}}
"""


# ── Watchlist persistence ────────────────────────────────────────
def load_watchlist() -> list[str]:
    if os.path.exists(WATCHLIST_FILE):
        try:
            with open(WATCHLIST_FILE, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                return [s.upper().strip() for s in data if s.strip()]
        except Exception:
            pass
    try:
        import alpaca_trading_bot as _b
        return list(_b.WATCHLIST)
    except Exception:
        return ["NFLX", "TSLA"]


def save_watchlist(symbols: list[str]):
    with open(WATCHLIST_FILE, "w", encoding="utf-8") as f:
        json.dump(symbols, f, indent=2)


# ── Background worker ────────────────────────────────────────────
class RefreshWorker(QThread):
    done = pyqtSignal(dict)

    def __init__(self, watchlist: list[str]):
        super().__init__()
        self._watchlist = list(watchlist)

    def run(self):
        res: dict = {
            "account":   None,
            "clock":     None,
            "positions": [],
            "bars":      {},   # sym → {close,high,low,volume,vwap}
            "prices":    {},   # sym → float  (convenience)
            "orders":    [],
            "error":     None,
        }
        try:
            from alpaca.trading.client import TradingClient
            from alpaca.trading.requests import GetOrdersRequest
            from alpaca.data.historical import StockHistoricalDataClient
            from alpaca.data.requests import StockLatestBarRequest
            from alpaca.data.enums import DataFeed

            tc = TradingClient(_API_KEY, _API_SECRET, paper=PAPER_TRADING)
            dc = StockHistoricalDataClient(_API_KEY, _API_SECRET)

            res["account"]   = tc.get_account()
            res["clock"]     = tc.get_clock()
            res["positions"] = tc.get_all_positions()

            try:
                res["orders"] = tc.get_orders(
                    filter=GetOrdersRequest(limit=20, status="all")
                )
            except Exception:
                res["orders"] = []

            if self._watchlist:
                latest = dc.get_stock_latest_bar(
                    StockLatestBarRequest(
                        symbol_or_symbols=self._watchlist,
                        feed=DataFeed.IEX,
                    )
                )
                for s, b in latest.items():
                    res["bars"][s] = {
                        "close":  float(b.close),
                        "high":   float(b.high),
                        "low":    float(b.low),
                        "volume": int(b.volume),
                        "vwap":   float(b.vwap) if b.vwap else 0.0,
                    }
                res["prices"] = {s: d["close"] for s, d in res["bars"].items()}

        except Exception as exc:
            res["error"] = str(exc)

        self.done.emit(res)


# ── Backtest worker ──────────────────────────────────────────────
class BacktestWorker(QThread):
    done     = pyqtSignal(dict)   # full results dict
    progress = pyqtSignal(str)    # one log line per symbol

    def __init__(self, api_key: str, api_secret: str,
                 watchlist: list, crypto_watchlist: list,
                 lookback_days: int = 365):
        super().__init__()
        self._api_key          = api_key
        self._api_secret       = api_secret
        self._watchlist        = list(watchlist)
        self._crypto_watchlist = list(crypto_watchlist)
        self._lookback_days    = lookback_days

    def run(self):
        try:
            import alpaca_trading_bot as _b
            results = _b.backtest_for_gui(
                api_key          = self._api_key,
                api_secret       = self._api_secret,
                watchlist        = self._watchlist,
                crypto_watchlist = self._crypto_watchlist,
                lookback_days    = self._lookback_days,
                progress_callback= lambda msg: self.progress.emit(str(msg)),
            )
        except Exception as exc:
            results = {
                "stocks": [], "crypto": [],
                "errors": [{"symbol": "SYSTEM", "error": str(exc)}],
            }
        self.done.emit(results)


# ── Widget helpers ───────────────────────────────────────────────
def _lbl(text="", bold=False, color=C_TEXT, size=13) -> QLabel:
    w = QLabel(text)
    f = QFont(); f.setPointSize(size); f.setBold(bold)
    w.setFont(f)
    w.setStyleSheet(f"color:{color};background:transparent;")
    return w


def _dot(color: str) -> QLabel:
    w = QLabel("●")
    w.setStyleSheet(f"color:{color};font-size:16px;background:transparent;")
    return w


def _sep() -> QFrame:
    line = QFrame()
    line.setFrameShape(QFrame.Shape.HLine)
    line.setStyleSheet(f"color:{C_BORDER};max-height:1px;background:{C_BORDER};")
    return line


def _cell(text="", color=C_TEXT,
          align=Qt.AlignmentFlag.AlignLeft) -> QTableWidgetItem:
    item = QTableWidgetItem(text)
    item.setForeground(QColor(color))
    item.setTextAlignment(int(align | Qt.AlignmentFlag.AlignVCenter))
    return item


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _tbl(headers: list[str],
         fixed_last: int = 0,
         max_h: int = 0) -> QTableWidget:
    t = QTableWidget(0, len(headers))
    t.setHorizontalHeaderLabels(headers)
    hdr = t.horizontalHeader()
    for i in range(len(headers)):
        if fixed_last and i == len(headers) - 1:
            hdr.setSectionResizeMode(i, QHeaderView.ResizeMode.Fixed)
            t.setColumnWidth(i, fixed_last)
        else:
            hdr.setSectionResizeMode(i, QHeaderView.ResizeMode.Stretch)
    t.verticalHeader().setVisible(False)
    t.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
    t.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
    t.setAlternatingRowColors(False)
    if max_h:
        t.setMaximumHeight(max_h)
    return t


# ── Main Window ──────────────────────────────────────────────────
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Alpaca Trading Bot V6 — Dashboard")
        self.setMinimumSize(1300, 740)
        self.setStyleSheet(_BASE_STYLE)

        self._process: QProcess | None = None
        self._worker:  RefreshWorker | None = None
        self._prev_prices: dict[str, float] = {}
        self._gui_watchlist: list[str] = load_watchlist()
        self._backtest_results: dict | None = None
        self._bt_worker: BacktestWorker | None = None
        self._current_bt_symbol: str = ""
        self._bt_all_rows: list[dict] = []

        self._build_ui()
        self._refresh_dashboard()

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh_dashboard)
        self._timer.start(30_000)

    # ── close ────────────────────────────────────────────────────
    def closeEvent(self, event):
        if self._process and self._process.state() != QProcess.ProcessState.NotRunning:
            self._process.terminate()
            self._process.waitForFinished(3000)
        if self._bt_worker and self._bt_worker.isRunning():
            self._bt_worker.quit()
            self._bt_worker.wait(3000)
        event.accept()

    # ── shell ────────────────────────────────────────────────────
    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._build_toolbar())

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setHandleWidth(2)
        splitter.setStyleSheet(f"QSplitter::handle{{background:{C_BORDER};}}")
        splitter.addWidget(self._build_tabs())
        splitter.addWidget(self._build_log_panel())
        splitter.setSizes([500, 800])
        root.addWidget(splitter, 1)

        sb = QStatusBar()
        sb.setStyleSheet(
            f"background:{C_CARD};color:{C_SUBTEXT};border-top:1px solid {C_BORDER};")
        self.setStatusBar(sb)
        self._lbl_refresh = QLabel("上次刷新：--")
        self._lbl_botstatus = QLabel("Bot 狀態：已停止")
        sb.addWidget(self._lbl_botstatus)
        sb.addPermanentWidget(self._lbl_refresh)

    # ── toolbar ──────────────────────────────────────────────────
    def _build_toolbar(self) -> QWidget:
        bar = QWidget()
        bar.setFixedHeight(52)
        bar.setStyleSheet(
            f"background:{C_CARD};border-bottom:1px solid {C_BORDER};")
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(16, 0, 16, 0)
        lay.addWidget(_lbl("🤖  Alpaca Trading Bot V6 — Dashboard",
                           bold=True, size=14, color=C_BLUE))
        lay.addStretch()

        self._btn_trial = QPushButton("🔬  試跑")
        self._btn_trial.setToolTip("試跑驗證（毋須 API Key）")
        self._btn_trial.setStyleSheet(
            f"QPushButton{{background:{C_YELLOW};color:#1e1e2e;border:none;"
            f"border-radius:5px;padding:6px 18px;font-weight:bold;}}"
            f"QPushButton:hover{{background:#ffe0a0;}}"
            f"QPushButton:disabled{{background:{C_BORDER};color:{C_SUBTEXT};}}")
        self._btn_trial.clicked.connect(self._run_trial)

        self._btn_start = QPushButton("▶  啟動 Bot")
        self._btn_start.setToolTip("Paper 交易（需 API Key）")
        self._btn_start.setStyleSheet(
            f"QPushButton{{background:{C_GREEN};color:#1e1e2e;border:none;"
            f"border-radius:5px;padding:6px 18px;font-weight:bold;}}"
            f"QPushButton:hover{{background:#b9f0b4;}}"
            f"QPushButton:disabled{{background:{C_BORDER};color:{C_SUBTEXT};}}")
        self._btn_start.clicked.connect(self._start_bot)

        self._btn_stop = QPushButton("⏹  停止")
        self._btn_stop.setEnabled(False)
        self._btn_stop.setStyleSheet(
            f"QPushButton{{background:{C_RED};color:#1e1e2e;border:none;"
            f"border-radius:5px;padding:6px 18px;font-weight:bold;}}"
            f"QPushButton:hover{{background:#f5a3b5;}}"
            f"QPushButton:disabled{{background:{C_BORDER};color:{C_SUBTEXT};}}")
        self._btn_stop.clicked.connect(self._stop_bot)

        self._btn_backtest = QPushButton("📊  回測")
        self._btn_backtest.setToolTip("陰陽燭圖勝率回測（需 API Key）")
        self._btn_backtest.setStyleSheet(
            f"QPushButton{{background:{C_PURPLE};color:#1e1e2e;border:none;"
            f"border-radius:5px;padding:6px 18px;font-weight:bold;}}"
            f"QPushButton:hover{{background:#d9b8ff;}}"
            f"QPushButton:disabled{{background:{C_BORDER};color:{C_SUBTEXT};}}")
        self._btn_backtest.clicked.connect(self._run_backtest)

        # ── Mode Toggle Button ──
        self._btn_mode = QPushButton("模式：PAPER")
        self._btn_mode.setToolTip("點擊切換 Paper Trade / 實盤 Trade")
        self._btn_mode.setFixedWidth(130)
        self._btn_mode.clicked.connect(self._toggle_mode)
        self._update_mode_button()

        lay.addWidget(self._btn_trial)
        lay.addSpacing(8)
        lay.addWidget(self._btn_start)
        lay.addSpacing(8)
        lay.addWidget(self._btn_stop)
        lay.addSpacing(8)
        lay.addWidget(self._btn_backtest)
        lay.addSpacing(16)
        lay.addWidget(self._btn_mode)
        return bar

    # ── tab container ────────────────────────────────────────────
    def _build_tabs(self) -> QTabWidget:
        self._tabs = QTabWidget()
        self._tabs.addTab(self._build_tab_overview(),  "  總覽  ")
        self._tabs.addTab(self._build_tab_account(),   "  帳戶  ")
        self._tabs.addTab(self._build_tab_watchlist(), " 觀察清單 ")
        self._tabs.addTab(self._build_tab_positions(), "  持倉  ")
        self._tabs.addTab(self._build_tab_orders(),    " 交易記錄 ")
        self._tabs.addTab(self._build_tab_backtest(),  " 📊 回測圖表 ")
        return self._tabs

    # ── helpers ──────────────────────────────────────────────────
    def _detail_btn(self, tab_idx: int) -> QPushButton:
        btn = QPushButton("詳情 ›")
        btn.setStyleSheet(_DETAIL_BTN)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.clicked.connect(lambda _, t=tab_idx: self._tabs.setCurrentIndex(t))
        return btn

    def _card(self, title: str, detail_tab: int = -1) -> tuple[QGroupBox, QVBoxLayout]:
        box = QGroupBox()
        lay = QVBoxLayout(box)
        lay.setSpacing(6)
        hdr = QHBoxLayout()
        hdr.addWidget(_lbl(title, bold=True, color=C_BLUE, size=12))
        hdr.addStretch()
        if detail_tab >= 0:
            hdr.addWidget(self._detail_btn(detail_tab))
        lay.addLayout(hdr)
        lay.addWidget(_sep())
        return box, lay

    def _scrolled(self, inner: QWidget) -> QScrollArea:
        sa = QScrollArea()
        sa.setWidgetResizable(True)
        sa.setWidget(inner)
        return sa

    # ═══════════════════════════════════════════════════════════════
    # TAB 0 — Overview
    # ═══════════════════════════════════════════════════════════════
    def _build_tab_overview(self) -> QWidget:
        body = QWidget()
        lay  = QVBoxLayout(body)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(12)

        # ── Account summary ──
        box, bl = self._card("📊  帳戶資訊", TAB_ACCOUNT)
        self._ov_acct: dict[str, QLabel] = {}
        for k, label in [("equity", "淨值"), ("buying_power", "可用資金"),
                         ("mode", "交易模式"), ("status", "帳戶狀態")]:
            row = QHBoxLayout()
            row.addWidget(_lbl(label + "：", color=C_SUBTEXT))
            v = _lbl("--", bold=True)
            row.addWidget(v); row.addStretch()
            self._ov_acct[k] = v
            bl.addLayout(row)
        lay.addWidget(box)

        # ── Market status ──
        box2, bl2 = self._card("🕐  市場狀態")
        r1 = QHBoxLayout()
        self._ov_dot  = _dot(C_RED)
        self._ov_mkt  = _lbl("收市中", bold=True)
        r1.addWidget(self._ov_dot); r1.addWidget(self._ov_mkt); r1.addStretch()
        bl2.addLayout(r1)
        for attr, label in [("_ov_session", "交易時段："),
                             ("_ov_next",    "下次開市：")]:
            row = QHBoxLayout()
            row.addWidget(_lbl(label, color=C_SUBTEXT))
            v = _lbl("--"); setattr(self, attr, v)
            row.addWidget(v); row.addStretch()
            bl2.addLayout(row)
        self._ov_session.setText("09:30 – 16:00 ET")
        lay.addWidget(box2)

        # ── Watchlist (compact) ──
        box3, bl3 = self._card("👁  觀察清單", TAB_WATCHLIST)
        self._tbl_wl_ov = _tbl(["代碼", "現價 (USD)", "漲跌幅"], max_h=180)
        bl3.addWidget(self._tbl_wl_ov)
        lay.addWidget(box3)

        # ── Positions (compact) ──
        box4, bl4 = self._card("💼  持倉明細", TAB_POSITIONS)
        self._tbl_pos_ov = _tbl(["代碼", "股數", "未實現損益"], max_h=160)
        bl4.addWidget(self._tbl_pos_ov)
        lay.addWidget(box4)

        lay.addStretch()
        return self._scrolled(body)

    # ═══════════════════════════════════════════════════════════════
    # TAB 1 — Account detail
    # ═══════════════════════════════════════════════════════════════
    def _build_tab_account(self) -> QWidget:
        body = QWidget()
        lay  = QVBoxLayout(body)
        lay.setContentsMargins(16, 16, 16, 16)
        lay.setSpacing(14)

        lay.addWidget(_lbl("帳戶詳情", bold=True, size=15, color=C_BLUE))
        lay.addWidget(_sep())

        self._acct_vals: dict[str, QLabel] = {}
        sections = [
            ("基本資訊", [
                ("account_number", "帳號 ID"),
                ("account_type",   "帳戶類型"),
                ("status",         "帳戶狀態"),
                ("mode",           "交易模式"),
            ]),
            ("資金", [
                ("equity",          "淨值 (Equity)"),
                ("last_equity",     "上次淨值"),
                ("cash",            "現金"),
                ("buying_power",    "可用資金"),
                ("portfolio_value", "投資組合市值"),
            ]),
            ("限制 / 風控", [
                ("daytrade_count",       "當日交易次數"),
                ("pattern_day_trader",   "模式日交易者 (PDT)"),
                ("trading_blocked",      "交易限制"),
                ("transfers_blocked",    "轉帳限制"),
                ("account_blocked",      "帳戶鎖定"),
            ]),
        ]
        for sec_title, fields in sections:
            grp = QGroupBox(sec_title)
            gl  = QVBoxLayout(grp)
            gl.setSpacing(6)
            for k, label in fields:
                row = QHBoxLayout()
                row.addWidget(_lbl(label + "：", color=C_SUBTEXT, size=12))
                row.addSpacing(8)
                v = _lbl("--", bold=True, size=12)
                row.addWidget(v)
                row.addStretch()
                self._acct_vals[k] = v
                gl.addLayout(row)
            lay.addWidget(grp)

        lay.addStretch()
        return self._scrolled(body)

    # ═══════════════════════════════════════════════════════════════
    # TAB 2 — Watchlist detail
    # ═══════════════════════════════════════════════════════════════
    def _build_tab_watchlist(self) -> QWidget:
        body = QWidget()
        lay  = QVBoxLayout(body)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(10)

        lay.addWidget(_lbl("觀察清單管理", bold=True, size=15, color=C_BLUE))
        lay.addWidget(_sep())

        # input row
        inp_row = QHBoxLayout()
        self._inp_sym = QLineEdit()
        self._inp_sym.setPlaceholderText("輸入美股代碼（如 AAPL）後按 Enter 或點擊加入")
        self._inp_sym.setFixedHeight(34)
        self._inp_sym.returnPressed.connect(self._add_symbol)
        btn_add = QPushButton("＋ 加入")
        btn_add.setFixedSize(90, 34)
        btn_add.setStyleSheet(
            f"QPushButton{{background:{C_BLUE};color:#1e1e2e;border:none;"
            f"border-radius:4px;font-weight:bold;}}"
            f"QPushButton:hover{{background:#a8c8ff;}}")
        btn_add.clicked.connect(self._add_symbol)
        inp_row.addWidget(self._inp_sym, 1)
        inp_row.addSpacing(8)
        inp_row.addWidget(btn_add)
        lay.addLayout(inp_row)

        # table: symbol / price / high / low / volume / vwap / remove
        self._tbl_wl_det = _tbl(
            ["代碼", "現價", "日高", "日低", "成交量", "VWAP", ""],
            fixed_last=46,
        )
        lay.addWidget(self._tbl_wl_det, 1)

        self._lbl_wl_count = _lbl("", color=C_SUBTEXT, size=11)
        lay.addWidget(self._lbl_wl_count)
        return body

    # ═══════════════════════════════════════════════════════════════
    # TAB 3 — Positions detail
    # ═══════════════════════════════════════════════════════════════
    def _build_tab_positions(self) -> QWidget:
        body = QWidget()
        lay  = QVBoxLayout(body)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(10)

        lay.addWidget(_lbl("持倉明細", bold=True, size=15, color=C_BLUE))
        lay.addWidget(_sep())

        # summary row
        sumrow = QHBoxLayout()
        self._pos_summary: dict[str, QLabel] = {}
        for k, label in [("count", "持倉數量"), ("total_val", "總市值"),
                         ("total_pl", "總未實現損益")]:
            sumrow.addWidget(_lbl(label + "：", color=C_SUBTEXT))
            v = _lbl("--", bold=True)
            self._pos_summary[k] = v
            sumrow.addWidget(v)
            sumrow.addSpacing(24)
        sumrow.addStretch()
        lay.addLayout(sumrow)
        lay.addWidget(_sep())

        # full table
        self._tbl_pos_det = _tbl(
            ["代碼", "方向", "股數", "均入價", "現價", "市值", "未實現損益", "損益%"]
        )
        lay.addWidget(self._tbl_pos_det, 1)
        return body

    # ═══════════════════════════════════════════════════════════════
    # TAB 4 — Orders
    # ═══════════════════════════════════════════════════════════════
    def _build_tab_orders(self) -> QWidget:
        body = QWidget()
        lay  = QVBoxLayout(body)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(10)

        hdr_row = QHBoxLayout()
        hdr_row.addWidget(_lbl("近期交易記錄（最近 20 筆）",
                               bold=True, size=15, color=C_BLUE))
        hdr_row.addStretch()
        btn_ref = QPushButton("↻ 刷新")
        btn_ref.setFixedWidth(80)
        btn_ref.clicked.connect(self._refresh_dashboard)
        hdr_row.addWidget(btn_ref)
        lay.addLayout(hdr_row)
        lay.addWidget(_sep())

        self._tbl_orders = _tbl(
            ["提交時間", "代碼", "方向", "數量", "均成交價", "狀態"]
        )
        lay.addWidget(self._tbl_orders, 1)
        return body

    # ═══════════════════════════════════════════════════════════════
    # TAB 5 — Backtest Chart
    # ═══════════════════════════════════════════════════════════════
    def _build_tab_backtest(self) -> QWidget:
        body = QWidget()
        outer = QHBoxLayout(body)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # ── Left pane (300 px fixed) ─────────────────────────────
        left = QWidget()
        left.setFixedWidth(300)
        left.setStyleSheet(
            f"background:{C_CARD};border-right:1px solid {C_BORDER};")
        ll = QVBoxLayout(left)
        ll.setContentsMargins(10, 12, 10, 12)
        ll.setSpacing(10)

        ll.addWidget(_lbl("回測標的", bold=True, color=C_BLUE))
        ll.addWidget(_sep())

        # symbol chips
        self._bt_chip_area = QWidget()
        self._bt_chip_layout = QVBoxLayout(self._bt_chip_area)
        self._bt_chip_layout.setSpacing(4)
        self._bt_chip_layout.setContentsMargins(0, 0, 0, 0)
        chip_scroll = self._scrolled(self._bt_chip_area)
        chip_scroll.setMaximumHeight(200)
        ll.addWidget(chip_scroll)

        ll.addWidget(_sep())
        ll.addWidget(_lbl("統計對比（選中標的）", bold=True, color=C_BLUE))

        self._bt_stats_tbl = _tbl(["指標", "純MA", "MA+燭形"], max_h=130)
        ll.addWidget(self._bt_stats_tbl)

        ll.addWidget(_sep())

        self._btn_tv = QPushButton("在 TradingView 開啟 ↗")
        self._btn_tv.setToolTip("在系統瀏覽器開啟 TradingView 圖表")
        self._btn_tv.setStyleSheet(
            f"QPushButton{{background:{C_BLUE};color:#1e1e2e;border:none;"
            f"border-radius:5px;padding:6px 10px;font-size:12px;font-weight:bold;}}"
            f"QPushButton:hover{{background:#a8c8ff;}}"
            f"QPushButton:disabled{{background:{C_BORDER};color:{C_SUBTEXT};}}")
        self._btn_tv.setEnabled(False)
        self._btn_tv.clicked.connect(self._open_tradingview)
        ll.addWidget(self._btn_tv)

        ll.addWidget(_lbl("完成回測後選擇標的以顯示 K 線圖",
                          color=C_SUBTEXT, size=10))
        ll.addStretch()

        # ── Right pane (chart) ───────────────────────────────────
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(6, 6, 6, 6)
        rl.setSpacing(0)

        self._bt_fig = Figure(figsize=(9, 5), dpi=100,
                              facecolor=C_BG, tight_layout=False)
        self._bt_canvas = FigureCanvasQTAgg(self._bt_fig)
        self._bt_canvas.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._bt_fig.text(
            0.5, 0.5, "請先執行回測（📊 回測）",
            ha="center", va="center", color=C_SUBTEXT, fontsize=14)
        self._bt_canvas.draw()
        rl.addWidget(self._bt_canvas, 1)

        outer.addWidget(left)
        outer.addWidget(right, 1)
        return body

    # ═══════════════════════════════════════════════════════════════
    # Log panel (right side, always visible)
    # ═══════════════════════════════════════════════════════════════
    def _build_log_panel(self) -> QWidget:
        w   = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        hdr = QWidget()
        hdr.setFixedHeight(40)
        hdr.setStyleSheet(
            f"background:{C_CARD};border-bottom:1px solid {C_BORDER};")
        hl = QHBoxLayout(hdr)
        hl.setContentsMargins(12, 0, 12, 0)
        hl.addWidget(_lbl("📋  Live Log", bold=True, color=C_BLUE))
        hl.addStretch()
        btn_clr = QPushButton("🗑  清除")
        btn_clr.setFixedWidth(80)
        btn_clr.clicked.connect(lambda: self._log.clear())
        hl.addWidget(btn_clr)
        lay.addWidget(hdr)

        self._log = QTextEdit()
        self._log.setReadOnly(True)
        self._log.setFont(QFont("Consolas", 10))
        self._log.setStyleSheet(
            f"background:{C_LOG_BG};color:{C_TEXT};border:none;padding:8px;")
        lay.addWidget(self._log, 1)
        return w

    # ═══════════════════════════════════════════════════════════════
    # Watchlist management
    # ═══════════════════════════════════════════════════════════════
    def _add_symbol(self):
        raw = self._inp_sym.text().strip().upper()
        if not raw:
            return
        if not raw.isalpha() or len(raw) > 5:
            QMessageBox.warning(self, "無效代碼",
                                f"「{raw}」不是有效的美股代碼（1–5 個英文字母）。")
            return
        if raw in self._gui_watchlist:
            QMessageBox.information(self, "已存在",
                                    f"「{raw}」已在觀察清單中。")
            self._inp_sym.clear()
            return
        self._gui_watchlist.append(raw)
        save_watchlist(self._gui_watchlist)
        self._inp_sym.clear()
        self._log_append(f"[觀察清單] 加入 {raw}", C_BLUE)
        self._refresh_dashboard()

    def _remove_symbol(self, symbol: str):
        if symbol in self._gui_watchlist:
            self._gui_watchlist.remove(symbol)
            save_watchlist(self._gui_watchlist)
            self._log_append(f"[觀察清單] 移除 {symbol}", C_YELLOW)
            self._repaint_wl_tables({})

    # ═══════════════════════════════════════════════════════════════
    # Bot process
    # ═══════════════════════════════════════════════════════════════
    def _get_python(self) -> str:
        python = os.path.join(os.path.dirname(_DIR), ".venv", "Scripts", "python.exe")
        return python if os.path.exists(python) else sys.executable

    def _busy(self) -> bool:
        proc_busy = bool(self._process and
                         self._process.state() != QProcess.ProcessState.NotRunning)
        bt_busy   = bool(self._bt_worker and self._bt_worker.isRunning())
        return proc_busy or bt_busy

    def _set_buttons(self, *, running: bool):
        self._btn_trial.setEnabled(not running)
        self._btn_start.setEnabled(not running)
        self._btn_stop.setEnabled(running)
        self._btn_backtest.setEnabled(not running)

    def _run_trial(self):
        if self._busy():
            QMessageBox.warning(self, "執行中", "請先停止當前任務。")
            return
        self._process = QProcess(self)
        self._process.setProgram(self._get_python())
        self._process.setArguments(["-X", "utf8", os.path.join(_DIR, "alpaca_trading_bot.py")])
        self._process.setWorkingDirectory(_DIR)
        self._process.readyReadStandardOutput.connect(self._on_stdout)
        self._process.readyReadStandardError.connect(self._on_stderr)
        self._process.finished.connect(self._on_trial_done)
        self._process.start()
        self._set_buttons(running=True)
        self._lbl_botstatus.setText(
            f"Bot 狀態：<span style='color:{C_YELLOW}'>試跑中</span>")
        self._log_append("[系統] 試跑驗證已啟動（毋須 API Key）…", C_YELLOW)

    def _on_trial_done(self):
        self._set_buttons(running=False)
        self._btn_mode.setEnabled(True)
        self._lbl_botstatus.setText("Bot 狀態：已停止")
        self._log_append("[系統] 試跑驗證完成 ✓", C_YELLOW)

    def _toggle_mode(self):
        global PAPER_TRADING
        if self._busy():
            return
        
        PAPER_TRADING = not PAPER_TRADING
        self._update_mode_button()
        
        mode_str = "模擬 (Paper)" if PAPER_TRADING else "實盤 (Live)"
        color = C_BLUE if PAPER_TRADING else C_RED
        self._log_append(f"[系統] 交易模式已切換至：{mode_str}", color)
        
        # 立即刷新數據以更新帳戶面板
        self._refresh_dashboard()

    def _update_mode_button(self):
        if PAPER_TRADING:
            text = "模式：PAPER"
            bg = C_BLUE
            hover = "#a8c8ff"
        else:
            text = "模式：LIVE ⚠️"
            bg = C_RED
            hover = "#f5a3b5"
            
        self._btn_mode.setText(text)
        self._btn_mode.setStyleSheet(
            f"QPushButton{{background:{bg};color:#1e1e2e;border:none;"
            f"border-radius:5px;padding:6px 10px;font-weight:bold;}}"
            f"QPushButton:hover{{background:{hover};}}"
            f"QPushButton:disabled{{background:{C_BORDER};color:{C_SUBTEXT};}}")

    def _start_bot(self):
        if self._busy():
            return
        
        args = ["-X", "utf8", os.path.join(_DIR, "alpaca_trading_bot.py"), "--live"]
        mode_label = "Paper Trading"
        mode_color = C_GREEN
        
        if not PAPER_TRADING:
            args.append("--real")
            mode_label = "實盤交易 (LIVE)"
            mode_color = C_RED
            
            reply = QMessageBox.warning(
                self, "⚠️ 實盤交易確認",
                "您即將開啟【實盤交易】模式！這將動用您的真實資金。\n\n確定要繼續嗎？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No
            )
            if reply == QMessageBox.StandardButton.No:
                return

        self._process = QProcess(self)
        self._process.setProgram(self._get_python())
        self._process.setArguments(args)
        self._process.setWorkingDirectory(_DIR)
        self._process.readyReadStandardOutput.connect(self._on_stdout)
        self._process.readyReadStandardError.connect(self._on_stderr)
        self._process.finished.connect(self._on_bot_done)
        self._process.start()
        
        self._set_buttons(running=True)
        self._btn_mode.setEnabled(False) # 運行中禁止切換模式
        
        self._lbl_botstatus.setText(
            f"Bot 狀態：<span style='color:{mode_color}'>執行中 ({mode_label})</span>")
        self._log_append(f"[系統] {mode_label} Bot 已啟動", mode_color)

    def _stop_bot(self):
        if self._process:
            self._process.terminate()
            if not self._process.waitForFinished(2000):
                self._process.kill()

    def _on_bot_done(self):
        self._set_buttons(running=False)
        self._btn_mode.setEnabled(True)
        self._lbl_botstatus.setText("Bot 狀態：已停止")
        self._log_append("[系統] Bot 已停止", C_YELLOW)

    def _run_backtest(self):
        if self._busy():
            QMessageBox.warning(self, "執行中", "請先停止當前任務再執行回測。")
            return

        if not _API_KEY or not _API_SECRET:
            # No API key — text-only fallback via QProcess
            self._process = QProcess(self)
            self._process.setProgram(self._get_python())
            self._process.setArguments(
                ["-X", "utf8",
                 os.path.join(_DIR, "alpaca_trading_bot.py"), "--backtest"])
            self._process.setWorkingDirectory(_DIR)
            self._process.readyReadStandardOutput.connect(self._on_stdout)
            self._process.readyReadStandardError.connect(self._on_stderr)
            self._process.finished.connect(self._on_backtest_done_text)
            self._process.start()
            self._set_buttons(running=True)
            self._lbl_botstatus.setText(
                f"Bot 狀態：<span style='color:{C_PURPLE}'>回測中（文字模式）</span>")
            self._log_append("[系統] 未設定 API Key，以文字模式執行回測…", C_PURPLE)
            return

        # API keys available — use BacktestWorker for chart rendering
        try:
            import alpaca_trading_bot as _b
            cwl = list(_b.CRYPTO_WATCHLIST)
        except Exception:
            cwl = []

        self._bt_worker = BacktestWorker(
            _API_KEY, _API_SECRET,
            self._gui_watchlist, cwl, lookback_days=365,
        )
        self._bt_worker.progress.connect(
            lambda msg: self._log_append(f"[回測] {msg}", C_PURPLE))
        self._bt_worker.done.connect(self._on_backtest_results)
        self._bt_worker.start()
        self._set_buttons(running=True)
        self._lbl_botstatus.setText(
            f"Bot 狀態：<span style='color:{C_PURPLE}'>回測中…</span>")
        self._log_append(
            f"[系統] 陰陽燭回測已啟動（{len(self._gui_watchlist)} 隻股票）…", C_PURPLE)

    def _on_backtest_done_text(self):
        self._set_buttons(running=False)
        self._lbl_botstatus.setText("Bot 狀態：已停止")
        self._log_append("[系統] 回測完成 ✓（文字模式）", C_PURPLE)

    def _on_stdout(self):
        raw = bytes(self._process.readAllStandardOutput()).decode("utf-8", errors="replace")
        for line in raw.splitlines():
            if not line.strip():
                continue
            c = C_TEXT
            if "[WARNING]" in line or "[WARN]" in line:
                c = C_YELLOW
            elif "[ERROR]" in line or "[CRITICAL]" in line:
                c = C_RED
            self._log_append(line, c)

    def _on_stderr(self):
        raw = bytes(self._process.readAllStandardError()).decode("utf-8", errors="replace")
        for line in raw.splitlines():
            if line.strip():
                self._log_append(line, C_RED)

    def _log_append(self, text: str, color: str = C_TEXT):
        if " [" in text:
            ts, rest = text.split(" [", 1)
            html = (f"<span style='color:{C_SUBTEXT}'>{_esc(ts)}</span>"
                    f"<span style='color:{color}'> [{_esc(rest)}</span>")
        else:
            html = f"<span style='color:{color}'>{_esc(text)}</span>"
        self._log.append(html)
        self._log.moveCursor(QTextCursor.MoveOperation.End)

    # ═══════════════════════════════════════════════════════════════
    # Backtest chart helpers
    # ═══════════════════════════════════════════════════════════════
    def _draw_candle_chart(self, symbol: str, row: dict):
        """Render OHLCV candlestick chart with MA + trade markers into self._bt_fig."""
        self._bt_fig.clear()

        bars_df = row.get("bars_df")
        candle  = row.get("candle", {})

        if bars_df is None or bars_df.empty:
            self._bt_fig.text(0.5, 0.5, f"{symbol}\n數據不足，無法顯示圖表",
                              ha="center", va="center",
                              color=C_SUBTEXT, fontsize=13)
            self._bt_canvas.draw()
            return

        n       = len(bars_df)
        opens   = bars_df["open"].values
        highs   = bars_df["high"].values
        lows    = bars_df["low"].values
        closes  = bars_df["close"].values
        volumes = bars_df["volume"].values
        dates   = list(bars_df.index)

        # date string → bar integer index
        date_to_idx: dict[str, int] = {str(d)[:10]: i for i, d in enumerate(dates)}

        # ── Subplots (70% price / 30% volume) ─────────────────
        gs  = self._bt_fig.add_gridspec(
            2, 1, height_ratios=[0.70, 0.30], hspace=0.04)
        ax  = self._bt_fig.add_subplot(gs[0])
        axv = self._bt_fig.add_subplot(gs[1], sharex=ax)

        for a in (ax, axv):
            a.set_facecolor(C_BG)
            a.tick_params(colors=C_SUBTEXT, labelsize=8)
            for spine in a.spines.values():
                spine.set_edgecolor(C_BORDER)
            a.grid(color=C_BORDER, alpha=0.25, linewidth=0.5)

        # ── Candles ────────────────────────────────────────────
        bw = 0.6
        for i in range(n):
            o, h, l, c = opens[i], highs[i], lows[i], closes[i]
            bull   = c >= o
            body_c = C_GREEN if bull else C_RED
            ax.plot([i, i], [l, h], color=body_c, linewidth=0.8, zorder=2)
            rect = Rectangle(
                (i - bw / 2, min(o, c)), bw, max(abs(c - o), 1e-6),
                facecolor=body_c, edgecolor=body_c,
                linewidth=0.4, zorder=3)
            ax.add_patch(rect)
            axv.bar(i, volumes[i], width=bw, color=body_c, alpha=0.65, zorder=2)

        # ── MA lines ───────────────────────────────────────────
        s = pd.Series(closes)
        ax.plot(range(n), s.rolling(5).mean(),
                color=C_BLUE,   linewidth=1.0, label="MA5",  zorder=4)
        ax.plot(range(n), s.rolling(20).mean(),
                color=C_YELLOW, linewidth=1.0, label="MA20", zorder=4)

        # ── Trade markers (candle strategy) ────────────────────
        for trade in candle.get("trades", []):
            ei = date_to_idx.get(str(trade.get("entry_date", ""))[:10])
            xi = date_to_idx.get(str(trade.get("exit_date",  ""))[:10])
            win = trade.get("pnl_pct", 0) > 0

            if ei is not None:
                ax.plot(ei, closes[ei] * 0.993,
                        marker="^", color=C_GREEN,
                        markersize=8, zorder=6, linestyle="None")
            if xi is not None:
                ax.plot(xi, closes[xi] * 1.007,
                        marker="v",
                        color=C_GREEN if win else C_RED,
                        markersize=8, zorder=6, linestyle="None")
            if ei is not None and xi is not None and xi > ei:
                ax.axvspan(ei, xi, alpha=0.07,
                           color=C_GREEN if win else C_RED, zorder=1)

        # ── X-axis labels ──────────────────────────────────────
        step   = max(1, n // 10)
        ticks  = list(range(0, n, step))
        labels = [str(dates[i])[:10] for i in ticks]
        ax.set_xticks(ticks)
        ax.set_xticklabels([])
        axv.set_xticks(ticks)
        axv.set_xticklabels(labels, rotation=28, ha="right", fontsize=7,
                            color=C_SUBTEXT)

        # ── Axis ranges / labels ───────────────────────────────
        ax.set_xlim(-1, n)
        pad = (highs.max() - lows.min()) * 0.05 or highs.max() * 0.01
        ax.set_ylim(lows.min() - pad, highs.max() + pad)
        tv_rating = row.get("tv", {}).get("tv_rating", "N/A")
        ax.set_title(
            f"{symbol}  ·  {n} 個交易日  ·  TV: {tv_rating}",
            color=C_TEXT, fontsize=11, pad=5)
        ax.set_ylabel("價格 (USD)", color=C_SUBTEXT, fontsize=8)
        axv.set_ylabel("成交量", color=C_SUBTEXT, fontsize=8)
        ax.tick_params(axis="y", colors=C_SUBTEXT)
        axv.tick_params(axis="y", colors=C_SUBTEXT)

        ax.legend(fontsize=8, facecolor=C_CARD,
                  edgecolor=C_BORDER, labelcolor=C_TEXT, loc="upper left")

        self._bt_fig.subplots_adjust(
            left=0.07, right=0.97, top=0.93, bottom=0.12)
        self._bt_canvas.draw()

    def _populate_bt_chips(self, results: dict):
        """Rebuild symbol chip buttons after backtest completes."""
        while self._bt_chip_layout.count():
            item = self._bt_chip_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        all_rows = results.get("stocks", []) + results.get("crypto", [])
        self._bt_all_rows = all_rows

        if not all_rows:
            self._bt_chip_layout.addWidget(_lbl("無回測結果", color=C_SUBTEXT))
            return

        for row in all_rows:
            sym = row["symbol"]
            btn = QPushButton(sym)
            btn.setCheckable(True)
            btn.setStyleSheet(
                f"QPushButton{{background:{C_CARD};color:{C_TEXT};"
                f"border:1px solid {C_BORDER};border-radius:4px;"
                f"padding:5px 8px;font-size:12px;text-align:left;}}"
                f"QPushButton:checked{{background:{C_PURPLE};color:#1e1e2e;"
                f"border-color:{C_PURPLE};}}"
                f"QPushButton:hover:!checked{{background:{C_BORDER};}}")
            btn.clicked.connect(lambda _, r=row: self._select_bt_symbol(r))
            self._bt_chip_layout.addWidget(btn)

        self._select_bt_symbol(all_rows[0])
        first = self._bt_chip_layout.itemAt(0).widget()
        if first:
            first.setChecked(True)

    def _select_bt_symbol(self, row: dict):
        """Load stats + chart for the selected symbol row."""
        symbol = row["symbol"]
        self._current_bt_symbol = symbol

        # update chip checked state
        for i in range(self._bt_chip_layout.count()):
            btn = self._bt_chip_layout.itemAt(i).widget()
            if isinstance(btn, QPushButton):
                btn.setChecked(btn.text() == symbol)

        # stats comparison table (3 rows)
        ma     = row.get("ma",     {})
        candle = row.get("candle", {})
        ma_wr  = ma.get("win_rate", 0)
        ca_wr  = candle.get("win_rate", 0)
        ma_pnl = ma.get("avg_pnl", 0)
        ca_pnl = candle.get("avg_pnl", 0)

        stats = [
            ("交易筆數", str(ma.get("total", 0)), str(candle.get("total", 0)),
             C_TEXT),
            ("勝率",
             f"{ma_wr*100:.1f}%",
             f"{ca_wr*100:.1f}%",
             C_GREEN if ca_wr >= ma_wr else C_RED),
            ("平均損益",
             f"{ma_pnl:+.2f}%",
             f"{ca_pnl:+.2f}%",
             C_GREEN if ca_pnl >= ma_pnl else C_RED),
        ]
        self._bt_stats_tbl.setRowCount(len(stats))
        for r, (label, ma_v, ca_v, ca_col) in enumerate(stats):
            self._bt_stats_tbl.setItem(r, 0, _cell(label, C_SUBTEXT))
            self._bt_stats_tbl.setItem(r, 1, _cell(ma_v))
            self._bt_stats_tbl.setItem(r, 2, _cell(ca_v, ca_col))
        self._bt_stats_tbl.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch)

        self._btn_tv.setEnabled(True)
        self._draw_candle_chart(symbol, row)

    def _open_tradingview(self):
        sym = self._current_bt_symbol.replace("/", "")
        webbrowser.open(f"https://www.tradingview.com/chart/?symbol={sym}")

    def _on_backtest_results(self, results: dict):
        self._backtest_results = results
        self._set_buttons(running=False)
        self._lbl_botstatus.setText("Bot 狀態：已停止")

        total  = len(results.get("stocks", [])) + len(results.get("crypto", []))
        errors = results.get("errors", [])
        self._log_append(
            f"[系統] 回測完成 ✓  成功 {total} 個標的，錯誤 {len(errors)} 個",
            C_PURPLE)
        for e in errors:
            self._log_append(f"[回測錯誤] {e['symbol']}: {e['error']}", C_RED)

        if total > 0:
            self._populate_bt_chips(results)
            self._tabs.setCurrentIndex(TAB_BACKTEST)

    # ═══════════════════════════════════════════════════════════════
    # Refresh & update
    # ═══════════════════════════════════════════════════════════════
    def _refresh_dashboard(self):
        if self._worker and self._worker.isRunning():
            return
        self._worker = RefreshWorker(self._gui_watchlist)
        self._worker.done.connect(self._on_data)
        self._worker.start()

    def _on_data(self, data: dict):
        if data["error"]:
            self._log_append(f"[刷新錯誤] {data['error']}", C_YELLOW)

        self._update_overview(data)
        self._update_account_tab(data)
        self._repaint_wl_tables(data.get("bars", {}))
        self._update_positions(data)
        self._update_orders(data)

        self._lbl_refresh.setText(
            f"上次刷新：{datetime.now().strftime('%H:%M:%S')}")

    # ── overview ─────────────────────────────────────────────────
    def _update_overview(self, data: dict):
        acc = data["account"]
        if acc:
            self._ov_acct["equity"].setText(f"${float(acc.equity):,.2f}")
            self._ov_acct["buying_power"].setText(f"${float(acc.buying_power):,.2f}")
            self._ov_acct["mode"].setText("Paper" if PAPER_TRADING else "實盤")
            ok = str(acc.status).upper() == "ACTIVE"
            self._ov_acct["status"].setStyleSheet(
                f"color:{'C_GREEN' if ok else C_YELLOW};background:transparent;font-weight:bold;")
            self._ov_acct["status"].setText(str(acc.status).upper())

        clk = data["clock"]
        if clk:
            if clk.is_open:
                self._ov_dot.setStyleSheet(
                    f"color:{C_GREEN};font-size:16px;background:transparent;")
                self._ov_mkt.setText("開市中")
            else:
                self._ov_dot.setStyleSheet(
                    f"color:{C_RED};font-size:16px;background:transparent;")
                self._ov_mkt.setText("收市中")
            self._ov_next.setText(
                clk.next_open.astimezone(timezone.utc).strftime("%m/%d %H:%M UTC"))

        # overview positions (compact)
        positions = data["positions"]
        self._tbl_pos_ov.setRowCount(len(positions))
        for r, pos in enumerate(positions):
            pl  = float(pos.unrealized_pl)
            clr = C_GREEN if pl >= 0 else C_RED
            self._tbl_pos_ov.setItem(r, 0, _cell(pos.symbol))
            self._tbl_pos_ov.setItem(
                r, 1, _cell(str(pos.qty), align=Qt.AlignmentFlag.AlignRight))
            self._tbl_pos_ov.setItem(
                r, 2, _cell(f"${pl:+,.2f}", clr, Qt.AlignmentFlag.AlignRight))

    # ── account tab ──────────────────────────────────────────────
    def _update_account_tab(self, data: dict):
        acc = data["account"]
        if not acc:
            return
        def s(k, v):
            if k in self._acct_vals:
                self._acct_vals[k].setText(str(v))

        s("account_number", getattr(acc, "account_number", "--"))
        s("account_type",   getattr(acc, "account_type",   "--"))
        s("status",         str(acc.status).upper())
        s("mode",           "Paper Trading" if PAPER_TRADING else "實盤交易")
        s("equity",         f"${float(acc.equity):,.2f}")
        s("last_equity",    f"${float(acc.last_equity):,.2f}"
                            if getattr(acc, "last_equity", None) else "--")
        s("cash",           f"${float(acc.cash):,.2f}"
                            if getattr(acc, "cash", None) else "--")
        s("buying_power",   f"${float(acc.buying_power):,.2f}")
        s("portfolio_value",f"${float(acc.portfolio_value):,.2f}"
                            if getattr(acc, "portfolio_value", None) else "--")
        s("daytrade_count", getattr(acc, "daytrade_count", "--"))
        s("pattern_day_trader",
          "是 ⚠️" if getattr(acc, "pattern_day_trader", False) else "否")
        s("trading_blocked",
          "是" if getattr(acc, "trading_blocked", False) else "否")
        s("transfers_blocked",
          "是" if getattr(acc, "transfers_blocked", False) else "否")
        s("account_blocked",
          "是" if getattr(acc, "account_blocked", False) else "否")

    # ── watchlist tables (overview + detail) ──────────────────────
    def _repaint_wl_tables(self, bars: dict):
        wl = self._gui_watchlist

        # ── overview compact ──
        self._tbl_wl_ov.setRowCount(len(wl))
        for r, sym in enumerate(wl):
            bar   = bars.get(sym, {})
            price = bar.get("close")
            prev  = self._prev_prices.get(sym)
            if price and prev and prev > 0:
                pct = (price - prev) / prev * 100
                chg_str = f"{'▲' if pct >= 0 else '▼'} {pct:+.2f}%"
                chg_col = C_GREEN if pct >= 0 else C_RED
            else:
                chg_str, chg_col = "─", C_SUBTEXT
            self._tbl_wl_ov.setItem(r, 0, _cell(sym))
            self._tbl_wl_ov.setItem(
                r, 1, _cell(f"${price:.2f}" if price else "--",
                            align=Qt.AlignmentFlag.AlignRight))
            self._tbl_wl_ov.setItem(
                r, 2, _cell(chg_str, chg_col, Qt.AlignmentFlag.AlignRight))

        # ── detail table ──
        self._tbl_wl_det.setRowCount(0)
        self._tbl_wl_det.setRowCount(len(wl))
        for r, sym in enumerate(wl):
            bar = bars.get(sym, {})
            cl  = bar.get("close");  hi = bar.get("high")
            lo  = bar.get("low");    vo = bar.get("volume")
            vw  = bar.get("vwap")

            prev  = self._prev_prices.get(sym)
            if cl and prev and prev > 0:
                pct     = (cl - prev) / prev * 100
                sym_col = C_GREEN if pct >= 0 else C_RED
            else:
                sym_col = C_TEXT

            self._tbl_wl_det.setItem(r, 0, _cell(sym, sym_col))
            self._tbl_wl_det.setItem(
                r, 1, _cell(f"${cl:.2f}" if cl else "--",
                            C_TEXT, Qt.AlignmentFlag.AlignRight))
            self._tbl_wl_det.setItem(
                r, 2, _cell(f"${hi:.2f}" if hi else "--",
                            C_GREEN, Qt.AlignmentFlag.AlignRight))
            self._tbl_wl_det.setItem(
                r, 3, _cell(f"${lo:.2f}" if lo else "--",
                            C_RED, Qt.AlignmentFlag.AlignRight))
            self._tbl_wl_det.setItem(
                r, 4, _cell(f"{vo:,}" if vo else "--",
                            C_TEXT, Qt.AlignmentFlag.AlignRight))
            self._tbl_wl_det.setItem(
                r, 5, _cell(f"${vw:.2f}" if vw else "--",
                            C_SUBTEXT, Qt.AlignmentFlag.AlignRight))

            btn_del = QPushButton("✕")
            btn_del.setFixedSize(30, 24)
            btn_del.setStyleSheet(
                f"QPushButton{{background:transparent;color:{C_RED};"
                f"border:none;font-size:14px;font-weight:bold;}}"
                f"QPushButton:hover{{color:#ff8fa8;}}")
            btn_del.clicked.connect(lambda _, s=sym: self._remove_symbol(s))
            self._tbl_wl_det.setCellWidget(r, 6, btn_del)
            self._tbl_wl_det.setRowHeight(r, 30)

        self._lbl_wl_count.setText(f"共 {len(wl)} 隻股票")

        if bars:
            self._prev_prices = {s: d["close"] for s, d in bars.items()}

    # ── positions detail ─────────────────────────────────────────
    def _update_positions(self, data: dict):
        positions = data["positions"]
        total_mv = sum(float(p.market_value) for p in positions
                       if getattr(p, "market_value", None))
        total_pl = sum(float(p.unrealized_pl) for p in positions)

        self._pos_summary["count"].setText(str(len(positions)))
        self._pos_summary["total_val"].setText(f"${total_mv:,.2f}")
        pl_col = C_GREEN if total_pl >= 0 else C_RED
        self._pos_summary["total_pl"].setStyleSheet(
            f"color:{pl_col};background:transparent;font-weight:bold;")
        self._pos_summary["total_pl"].setText(f"${total_pl:+,.2f}")

        self._tbl_pos_det.setRowCount(len(positions))
        for r, pos in enumerate(positions):
            pl    = float(pos.unrealized_pl)
            plpct = float(getattr(pos, "unrealized_plpc", 0) or 0) * 100
            mv    = float(getattr(pos, "market_value", 0) or 0)
            side  = str(getattr(pos, "side", "long")).upper()
            pl_c  = C_GREEN if pl >= 0 else C_RED
            s_c   = C_GREEN if side == "LONG" else C_RED

            self._tbl_pos_det.setItem(r, 0, _cell(pos.symbol))
            self._tbl_pos_det.setItem(r, 1, _cell(side, s_c))
            self._tbl_pos_det.setItem(
                r, 2, _cell(str(pos.qty), align=Qt.AlignmentFlag.AlignRight))
            self._tbl_pos_det.setItem(
                r, 3, _cell(f"${float(pos.avg_entry_price):.2f}",
                            align=Qt.AlignmentFlag.AlignRight))
            self._tbl_pos_det.setItem(
                r, 4, _cell(f"${float(pos.current_price):.2f}",
                            align=Qt.AlignmentFlag.AlignRight))
            self._tbl_pos_det.setItem(
                r, 5, _cell(f"${mv:,.2f}", align=Qt.AlignmentFlag.AlignRight))
            self._tbl_pos_det.setItem(
                r, 6, _cell(f"${pl:+,.2f}", pl_c, Qt.AlignmentFlag.AlignRight))
            self._tbl_pos_det.setItem(
                r, 7, _cell(f"{plpct:+.2f}%", pl_c, Qt.AlignmentFlag.AlignRight))

    # ── orders ───────────────────────────────────────────────────
    def _update_orders(self, data: dict):
        orders = data.get("orders", [])
        self._tbl_orders.setRowCount(len(orders))
        for r, o in enumerate(orders):
            side  = str(getattr(o, "side", "")).upper()
            s_col = C_GREEN if "BUY" in side else C_RED if "SELL" in side else C_TEXT
            stat  = str(getattr(o, "status", "--")).upper()
            stat_col = (C_GREEN  if stat == "FILLED"
                        else C_RED    if stat in ("CANCELED", "REJECTED", "EXPIRED")
                        else C_YELLOW if stat == "PARTIALLY_FILLED"
                        else C_SUBTEXT)

            submitted = getattr(o, "submitted_at", None)
            time_str  = (submitted.astimezone(timezone.utc).strftime("%m/%d %H:%M")
                         if submitted else "--")
            fp = getattr(o, "filled_avg_price", None)
            fp_str = f"${float(fp):.2f}" if fp else "--"
            qty = getattr(o, "filled_qty", None) or getattr(o, "qty", "--")

            self._tbl_orders.setItem(r, 0, _cell(time_str, C_SUBTEXT))
            self._tbl_orders.setItem(r, 1, _cell(str(o.symbol)))
            self._tbl_orders.setItem(r, 2, _cell(side, s_col))
            self._tbl_orders.setItem(
                r, 3, _cell(str(qty), align=Qt.AlignmentFlag.AlignRight))
            self._tbl_orders.setItem(
                r, 4, _cell(fp_str, align=Qt.AlignmentFlag.AlignRight))
            self._tbl_orders.setItem(r, 5, _cell(stat, stat_col))


# ── entry ────────────────────────────────────────────────────────
if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    win = MainWindow()
    win.show()
    sys.exit(app.exec())
