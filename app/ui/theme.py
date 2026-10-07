from PySide6.QtGui import QFont

BG = "#0b0b0d"
ACCENT = "#ff3529"
TEXT = "#f8eff0"
MUTED = "#aa9694"
OK = "#f1b6bd"
WARN = "#ffba83"
ERR = "#ff526d"
LEVEL_COLORS = {"ok": OK, "warn": WARN, "err": ERR, "off": "#65515a"}

# Paleta de apresentação: não altera os estados do assistente.
ORB_COLORS = {"IDLE": "#ff3121", "ACTIVATED": "#ffe4dc",
              "LISTENING": "#ff4c2e", "PROCESSING": "#e92b16",
              "SPEAKING": "#ff6c35", "ERROR": "#ff1641"}


def display_font(size: float, weight=QFont.Weight.Light, spacing: float = 0.0) -> QFont:
    f = QFont()
    f.setFamilies(["Bahnschrift", "Segoe UI", "Arial"])
    f.setPointSizeF(size)
    f.setWeight(weight)
    if spacing:
        f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, spacing)
    return f


def mono_font(size: float = 9.5) -> QFont:
    f = QFont()
    f.setFamilies(["Cascadia Mono", "Consolas", "Courier New"])
    f.setPointSizeF(size)
    return f


STYLE = f"""
QWidget {{ color:{TEXT}; font-family:"Segoe UI"; font-size:14px; }}
QMainWindow, QDialog, QWizard {{ background:{BG}; }}
QLabel {{ background:transparent; }}
QWidget#navigationRail {{ background:#111113; border-right:1px solid #262326; }}
QWidget#systemStrip {{ background:transparent; border-top:1px solid #322527; }}
QLabel#muted {{ color:{MUTED}; }}
QLabel#eyebrow {{ color:#df887a; font-size:10px; font-weight:600; }}
QLabel#title {{ color:#fff5ed; font-size:22px; font-weight:600; }}
QLabel#heroTitle {{ color:#fff3e8; font-size:27px; font-weight:500; }}
QLabel#clock {{ color:#fff5ed; font-size:42px; font-weight:300; }}
QLabel#temperature {{ color:#f5e6de; font-size:32px; font-weight:300; }}
QLabel#weatherDetail {{ color:#7f7272; font-size:10px; }}
QFrame#conversation {{ background:transparent; border:none; border-left:1px solid #3d2c2c; border-radius:0; }}
QFrame#composer {{ background:transparent; border:none; border-top:1px solid #644139; }}
QFrame#card, QFrame#clockCard {{ background:#161416; border:1px solid #3d3030; border-radius:8px; }}
QPushButton {{ background:#1a1718; border:1px solid #443330; border-radius:5px; padding:9px 14px; color:#e7d8cf; }}
QPushButton:hover {{ background:#352019; border-color:#ff5131; color:#fff4e9; }}
QPushButton:pressed {{ background:#5a251a; }}
QPushButton:focus {{ border-color:#ff7050; }}
QPushButton:disabled {{ color:#78635f; border-color:#322725; }}
QPushButton#railAction {{ background:transparent; border:1px solid transparent; font-size:11px; color:#a99a95; padding:5px 0; }}
QPushButton#railAction:hover {{ background:#241914; border-color:#6b2c1d; color:#ff6d44; }}
QPushButton#primary {{ background:#ff3924; color:#150c09; border:1px solid #ff5137; font-weight:700; border-radius:4px; }}
QPushButton#primary:hover {{ background:#ff6943; }}
QPushButton#tb {{ background:transparent; border:none; color:#9d8580; padding:5px 0; font-size:11px; }}
QPushButton#tb:hover {{ color:#ff613b; }}
QPushButton#voiceAction {{ background:transparent; border:1px solid #76503f; border-radius:21px; color:#e4ad96; padding:8px 16px; }}
QPushButton#voiceAction:hover {{ border-color:#ff683e; background:#362019; }}
QPushButton#quickAction {{ background:transparent; border:none; border-bottom:1px solid #725044; border-radius:0; color:#d0aa98; padding:9px 0; margin-right:16px; font-size:12px; }}
QPushButton#quickAction:hover {{ color:#ff7544; border-bottom-color:#ff4f2a; }}
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {{ background:#151214; border:1px solid #624235; border-radius:5px; padding:9px 11px; selection-background-color:#b4311d; }}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus {{ border-color:#ff5531; }}
QLineEdit#messageInput {{ background:transparent; border:none; border-radius:0; padding:9px 0; font-size:16px; }}
QComboBox QAbstractItemView {{ background:#201918; color:{TEXT}; selection-background-color:#623021; border:1px solid #86503a; padding:5px; }}
QSplitter::handle {{ background:transparent; }}
QSplitter::handle:hover {{ background:#493025; }}
QScrollArea {{ border:none; background:transparent; }}
QScrollArea > QWidget > QWidget {{ background:transparent; }}
QScrollBar:vertical {{ width:4px; background:transparent; margin:0; }}
QScrollBar::handle:vertical {{ background:#764b39; border-radius:2px; min-height:30px; }}
QScrollBar::handle:vertical:hover {{ background:#ff5931; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height:0; }}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background:transparent; }}
QTabWidget::pane {{ background:#141113; border:1px solid #40302b; border-radius:5px; top:-1px; }}
QTabBar::tab {{ padding:12px 16px; background:transparent; color:{MUTED}; border-bottom:2px solid transparent; }}
QTabBar::tab:selected {{ color:#ff9c6a; background:#2b1b16; border-bottom:2px solid #ff4c27; }}
QCheckBox {{ spacing:10px; }}
QCheckBox::indicator {{ width:17px; height:17px; border-radius:3px; border:1px solid #88503c; background:#17100f; }}
QCheckBox::indicator:checked {{ background:#ff4228; border:3px solid #ffb17a; }}
QProgressBar {{ background:#261813; border:1px solid #5e3e2d; border-radius:4px; height:12px; color:transparent; }}
QProgressBar::chunk {{ background:#ff4924; border-radius:3px; }}
QMenu {{ background:#201713; border:1px solid #67402c; padding:7px; }}
QMenu::item {{ padding:9px 22px; border-radius:3px; }}
QMenu::item:selected {{ background:#5c2b17; }}
QToolTip {{ background:#352018; color:#ffead8; border:1px solid #925033; padding:7px; }}
"""
