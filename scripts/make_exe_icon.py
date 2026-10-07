"""Exporta o mesmo ícone usado pelo app para o executável Windows."""
import os
from pathlib import Path
import sys
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
from PySide6.QtWidgets import QApplication
from app.ui.icons import make_icon
app = QApplication([])
destination = root / 'assets/apolo.ico'
destination.parent.mkdir(parents=True, exist_ok=True)
assert make_icon().pixmap(64, 64).save(str(destination), 'ICO')
