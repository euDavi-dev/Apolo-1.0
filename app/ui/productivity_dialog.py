from __future__ import annotations

import math
import time
from datetime import datetime

from PySide6.QtCore import QDateTime, Qt, QTimer
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDateTimeEdit,
                               QDialog, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
                               QMessageBox, QPushButton, QSpinBox, QStackedWidget,
                               QTabWidget, QTreeWidget, QTreeWidgetItem, QVBoxLayout,
                               QWidget)


_STATES = {"active": "Pendente", "done": "Concluída", "fired": "Aviso entregue",
           "cancelled": "Cancelado"}


def _label(text: str, object_name: str = "muted") -> QLabel:
    label = QLabel(text)
    label.setObjectName(object_name)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    return label


class ProductivityDialog(QDialog):
    """Agenda local, com as mesmas tarefas disponíveis na conversa e por voz."""

    def __init__(self, assistant, parent=None):
        super().__init__(parent)
        self.a = assistant
        self._items: list[dict] = []
        self._trees: dict[str, QTreeWidget] = {}
        self._stacks: dict[str, QStackedWidget] = {}
        self._cancel_buttons: dict[str, QPushButton] = {}
        self.setWindowTitle("Agenda do APOLO")
        self.resize(760, 590)
        self.setMinimumSize(580, 510)
        self.setStyleSheet("""
            QTreeWidget { background:#151214; border:1px solid #40302b;
                          border-radius:5px; outline:none; }
            QTreeWidget::item { padding:9px 5px; }
            QTreeWidget::item:selected { background:#583023; color:#fff4e9; }
            QHeaderView::section { background:#241a18; color:#cfa89a;
                                   border:none; padding:8px 5px; }
            QDateTimeEdit { background:#151214; border:1px solid #624235;
                            border-radius:5px; padding:9px 11px; }
            QDateTimeEdit:focus { border-color:#ff5531; }
            QCalendarWidget QWidget { background:#201918; }
            QCalendarWidget QAbstractItemView { background:#201918;
                                               selection-background-color:#623021; }
        """)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(22, 20, 22, 18)
        outer.setSpacing(12)
        outer.addWidget(_label("Sua agenda", "title"))
        outer.addWidget(_label("Organize o dia e deixe o Apolo lembrar você."))

        self.tabs = QTabWidget()
        self.tabs.addTab(self._task_tab(), "Tarefas")
        self.tabs.addTab(self._timer_tab(), "Temporizadores")
        self.tabs.addTab(self._reminder_tab(), "Lembretes")
        outer.addWidget(self.tabs, 1)
        self.include_completed = QCheckBox("Mostrar concluídos e avisos entregues")
        self.include_completed.toggled.connect(self._render)
        outer.addWidget(self.include_completed)
        outer.addWidget(_label("Os avisos funcionam com o Apolo aberto, inclusive na bandeja. "
                               "Sua agenda fica salva nesta conta, neste computador."))
        footer = QHBoxLayout()
        self.feedback = _label("")
        footer.addWidget(self.feedback, 1)
        close = QPushButton("Fechar")
        close.clicked.connect(self.close)
        footer.addWidget(close)
        outer.addLayout(footer)

        self._countdown = QTimer(self)
        self._countdown.setInterval(1000)
        self._countdown.timeout.connect(self._update_countdowns)
        signal = getattr(self.a, "productivity_changed", None)
        if signal is not None:
            signal.connect(self.set_items)
        self.refresh()

    def _page(self) -> tuple[QWidget, QVBoxLayout]:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(16, 16, 16, 14)
        layout.setSpacing(12)
        return page, layout

    def _button(self, text: str, callback, primary=False) -> QPushButton:
        button = QPushButton(text)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        if primary:
            button.setObjectName("primary")
        button.clicked.connect(callback)
        return button

    def _task_tab(self) -> QWidget:
        page, layout = self._page()
        self.task_title = QLineEdit()
        self.task_title.setPlaceholderText("O que você precisa fazer?")
        self.task_title.setMaxLength(200)
        self.task_title.returnPressed.connect(self._add_task)
        row = QHBoxLayout()
        row.addWidget(self.task_title, 1)
        row.addWidget(self._button("Adicionar", self._add_task, True))
        layout.addLayout(row)
        layout.addWidget(_label('Por voz: “adicionar tarefa comprar café” ou “minhas tarefas”.'))
        self._list(layout, "task", ["Nº", "Tarefa", "Estado"],
                   "Sua lista está livre. Adicione uma tarefa para começar.")
        actions = QHBoxLayout()
        self.complete_button = self._button("Concluir tarefa", self._complete_task)
        self.complete_button.setEnabled(False)
        actions.addWidget(self.complete_button)
        actions.addStretch()
        actions.addWidget(self._cancel_button("task"))
        layout.addLayout(actions)
        return page

    def _timer_tab(self) -> QWidget:
        page, layout = self._page()
        self.timer_title = QLineEdit()
        self.timer_title.setPlaceholderText("Nome do temporizador (opcional)")
        self.timer_title.setMaxLength(200)
        layout.addWidget(self.timer_title)
        self.timer_duration = QSpinBox()
        self.timer_duration.setRange(1, 43200)
        self.timer_duration.setValue(5)
        self.timer_unit = QComboBox()
        for text, seconds in (("minutos", 60), ("segundos", 1), ("horas", 3600)):
            self.timer_unit.addItem(text, seconds)
        self.timer_unit.currentIndexChanged.connect(self._timer_unit_changed)
        row = QHBoxLayout()
        row.addWidget(_label("Duração"))
        row.addWidget(self.timer_duration, 1)
        row.addWidget(self.timer_unit)
        row.addWidget(self._button("Iniciar", self._add_timer, True))
        layout.addLayout(row)
        layout.addWidget(_label('Por voz: “temporizador de 5 minutos”.'))
        self._list(layout, "timer", ["Nº", "Temporizador", "Restante", "Estado"],
                   "Nenhum temporizador em andamento. Reserve um tempo para se concentrar.")
        row = QHBoxLayout()
        row.addStretch()
        row.addWidget(self._cancel_button("timer"))
        layout.addLayout(row)
        return page

    def _reminder_tab(self) -> QWidget:
        page, layout = self._page()
        self.reminder_title = QLineEdit()
        self.reminder_title.setPlaceholderText("Do que você quer se lembrar?")
        self.reminder_title.setMaxLength(200)
        layout.addWidget(self.reminder_title)
        self.reminder_due = QDateTimeEdit(QDateTime.currentDateTime().addSecs(3600))
        self.reminder_due.setCalendarPopup(True)
        self.reminder_due.setDisplayFormat("dd/MM/yyyy 'às' HH:mm")
        self.reminder_due.setMinimumDateTime(QDateTime.currentDateTime())
        row = QHBoxLayout()
        row.addWidget(_label("Quando"))
        row.addWidget(self.reminder_due, 1)
        row.addWidget(self._button("Agendar", self._add_reminder, True))
        layout.addLayout(row)
        layout.addWidget(_label('Por voz: “me lembre de beber água em 20 minutos”.'))
        self._list(layout, "reminder", ["Nº", "Lembrete", "Quando", "Estado"],
                   "Nenhum lembrete agendado. Escolha uma data e um horário acima.")
        row = QHBoxLayout()
        row.addStretch()
        row.addWidget(self._cancel_button("reminder"))
        layout.addLayout(row)
        return page

    def _list(self, layout, kind: str, columns: list[str], empty_text: str) -> None:
        tree = QTreeWidget()
        tree.setHeaderLabels(columns)
        tree.setRootIsDecorated(False)
        tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        tree.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        tree.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        tree.setAlternatingRowColors(False)
        tree.header().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        tree.itemSelectionChanged.connect(self._selection_changed)
        stack = QStackedWidget()
        stack.addWidget(tree)
        empty = _label(empty_text)
        empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty.setContentsMargins(24, 20, 24, 20)
        stack.addWidget(empty)
        layout.addWidget(stack, 1)
        self._trees[kind] = tree
        self._stacks[kind] = stack

    def _cancel_button(self, kind: str) -> QPushButton:
        button = self._button("Cancelar selecionado", lambda: self._cancel(kind))
        button.setEnabled(False)
        self._cancel_buttons[kind] = button
        return button

    def _selected(self, kind: str) -> dict | None:
        rows = self._trees[kind].selectedItems()
        return rows[0].data(0, Qt.ItemDataRole.UserRole) if rows else None

    def _selection_changed(self) -> None:
        for kind, button in self._cancel_buttons.items():
            row = self._selected(kind)
            button.setEnabled(bool(row and row["status"] == "active"))
        row = self._selected("task")
        self.complete_button.setEnabled(bool(row and row["status"] == "active"))

    def refresh(self) -> None:
        try:
            self.set_items(self.a.productivity.list_items(include_completed=True))
        except (ValueError, OSError) as exc:
            self.feedback.setText(f"Não foi possível carregar a agenda: {exc}")

    def set_items(self, items) -> None:
        self._items = list(items)
        self._render()

    def _render(self, *_args) -> None:
        include_completed = self.include_completed.isChecked()
        for kind, tree in self._trees.items():
            selected = self._selected(kind)
            selected_id = selected["id"] if selected else None
            tree.blockSignals(True)
            tree.clear()
            rows = [item for item in self._items if item["kind"] == kind
                    and (include_completed or item["status"] == "active")]
            for item in rows:
                state = _STATES.get(item["status"], item["status"])
                if item["status"] == "active" and kind != "task":
                    state = "Ativo" if kind == "timer" else "Agendado"
                texts = [str(item["id"]), item["title"]]
                if kind == "timer":
                    texts.append(self._remaining(item))
                elif kind == "reminder":
                    texts.append(datetime.fromtimestamp(item["due_at"]).strftime("%d/%m/%Y %H:%M"))
                texts.append(state)
                entry = QTreeWidgetItem(texts)
                entry.setData(0, Qt.ItemDataRole.UserRole, item)
                entry.setToolTip(0, f"Use o número {item['id']} para concluir ou cancelar pela conversa.")
                tree.addTopLevelItem(entry)
                if item["id"] == selected_id:
                    entry.setSelected(True)
            tree.blockSignals(False)
            self._stacks[kind].setCurrentIndex(0 if rows else 1)
        self._selection_changed()

    @staticmethod
    def _remaining(item: dict) -> str:
        if item["status"] != "active":
            return "—"
        seconds = max(0, math.ceil(item["due_at"] - time.time()))
        if not seconds:
            return "Aguardando aviso"
        hours, seconds = divmod(seconds, 3600)
        minutes, seconds = divmod(seconds, 60)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"

    def _update_countdowns(self) -> None:
        tree = self._trees["timer"]
        for index in range(tree.topLevelItemCount()):
            entry = tree.topLevelItem(index)
            entry.setText(2, self._remaining(entry.data(0, Qt.ItemDataRole.UserRole)))

    def _timer_unit_changed(self) -> None:
        # Até trinta dias; limites iguais em todas as unidades.
        unit = self.timer_unit.currentData()
        self.timer_duration.setMaximum(2592000 // unit)

    def _mutate(self, callback, message: str) -> bool:
        try:
            callback()
        except (ValueError, OSError) as exc:
            QMessageBox.warning(self, "Confira os dados", str(exc))
            return False
        self.feedback.setText(message)
        refresh = getattr(self.a, "refresh_productivity", None)
        if refresh is not None and getattr(self.a, "productivity_changed", None) is not None:
            refresh()
        else:
            self.refresh()
        return True

    def _add_task(self) -> None:
        if self._mutate(lambda: self.a.productivity.add_task(self.task_title.text()), "Tarefa adicionada."):
            self.task_title.clear()
            self.task_title.setFocus()

    def _add_timer(self) -> None:
        seconds = self.timer_duration.value() * self.timer_unit.currentData()
        title = self.timer_title.text().strip() or "Temporizador"
        if self._mutate(lambda: self.a.productivity.add_timer(seconds, title), "Temporizador iniciado."):
            self.timer_title.clear()

    def _add_reminder(self) -> None:
        due_at = self.reminder_due.dateTime().toMSecsSinceEpoch() / 1000
        if due_at <= time.time():
            QMessageBox.warning(self, "Confira o horário", "Escolha uma data e um horário no futuro.")
            return
        if self._mutate(lambda: self.a.productivity.add_reminder(self.reminder_title.text(), due_at),
                        "Lembrete agendado."):
            self.reminder_title.clear()

    def _complete_task(self) -> None:
        row = self._selected("task")
        if row and row["status"] == "active":
            self._mutate(lambda: self.a.productivity.complete_task(row["id"]), "Tarefa concluída.")

    def _cancel(self, kind: str) -> None:
        row = self._selected(kind)
        if row and row["status"] == "active":
            self._mutate(lambda: self.a.productivity.cancel(row["id"]), "Item cancelado.")

    def showEvent(self, event) -> None:
        self.reminder_due.setMinimumDateTime(QDateTime.currentDateTime())
        self.refresh()
        self._countdown.start()
        super().showEvent(event)

    def hideEvent(self, event) -> None:
        self._countdown.stop()
        super().hideEvent(event)
