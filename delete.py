import os
import time
import serial
import threading
import re
import cv2
import json
import numpy as np
import base64
import pickle
from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout,
                               QHBoxLayout, QPushButton, QListWidget, QFileDialog,
                               QLabel, QSpinBox, QListWidgetItem, QAbstractItemView,
                               QLineEdit)
from PySide6.QtCore import Qt, Signal, Slot, QObject
from PySide6.QtGui import QColor, QBrush

# --- Configurações de Protocolo ---
START_BYTE_1 = 0xA5
START_BYTE_2 = 0x5A
MATRIX_WIDTH = 32
MATRIX_HEIGHT = 18
CONFIG_FILE = "config.json"

# --- Configurações de Imagem ---
SATURATION_BOOST = 1.6
GAMMA_CORRECTION = 1.2

# ==============================================================================
# AQUII FICAM OS TEUS DADOS EMBUTIDOS
# Podes gerar este conteúdo usando o botão "Gerar Código das Imagens" na UI
# ==============================================================================
EMBEDDED_DATA = {}  # Formato: {"nome_anim": [bytearray_frame1, bytearray_frame2, ...]}


class WorkerSignals(QObject):
    status = Signal(str, int, int, int, int)
    finished = Signal()


class AnimationPlayer(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("LED Matrix Pro - Embedded Version")
        self.resize(850, 700)

        # Estilo visual Dark
        self.setStyleSheet("""
            QMainWindow, QWidget { background-color: #1e1e1e; color: #e0e0e0; }
            QListWidget { background-color: #252526; border: 1px solid #333333; }
            QPushButton { background-color: #0e639c; color: white; border-radius: 4px; padding: 8px; }
            QPushButton#btn_play[playing="true"] { background-color: #a82e2e; }
            #status_bar { background-color: #007acc; color: white; padding: 5px; }
        """)

        self.playing = False
        self.serial_conn = None
        self.animations_data = {}
        self.root_dir = ""

        self.signals = WorkerSignals()
        self.signals.status.connect(self.update_status_ui)
        self.signals.finished.connect(self.clear_status_ui)

        self.init_ui()
        self.load_settings()

    def init_ui(self):
        layout = QVBoxLayout()

        # Porta Serial
        port_layout = QHBoxLayout()
        self.port_input = QLineEdit("COM20")
        port_layout.addWidget(QLabel("Porta Serial:"))
        port_layout.addWidget(self.port_input)
        layout.addLayout(port_layout)

        # Lista e Configs
        content = QHBoxLayout()
        self.list_widget = QListWidget()
        self.list_widget.itemSelectionChanged.connect(self.on_selection_changed)
        content.addWidget(self.list_widget, 2)

        self.config_group = QWidget()
        cfg_vbox = QVBoxLayout(self.config_group)
        self.spin_ms = self.create_setting(cfg_vbox, "Frame (ms):", 1, 10000)
        self.spin_loops = self.create_setting(cfg_vbox, "Loops:", 1, 1000)
        self.spin_hold = self.create_setting(cfg_vbox, "Hold Final (ms):", 0, 10000)
        content.addWidget(self.config_group, 1)
        layout.addLayout(content)

        # Status
        self.status_label = QLabel("Pronto.")
        self.status_label.setObjectName("status_bar")
        self.status_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.status_label)

        # Botões
        self.btn_load = QPushButton("Carregar Pastas Externas")
        self.btn_load.clicked.connect(self.load_folders)

        self.btn_export = QPushButton("Gerar Código para Embutir (Console)")
        self.btn_export.setStyleSheet("background-color: #6a1b9a;")
        self.btn_export.clicked.connect(self.export_to_code)

        self.btn_play = QPushButton("START")
        self.btn_play.setObjectName("btn_play")
        self.btn_play.setFixedHeight(50)
        self.btn_play.clicked.connect(self.toggle_play)

        layout.addWidget(self.btn_load)
        layout.addWidget(self.btn_export)
        layout.addWidget(self.btn_play)

        container = QWidget()
        container.setLayout(layout)
        self.setCentralWidget(container)

    def create_setting(self, layout, label, min_v, max_v):
        row = QHBoxLayout()
        row.addWidget(QLabel(label))
        spin = QSpinBox()
        spin.setRange(min_v, max_v)
        spin.valueChanged.connect(self.update_current_config)
        row.addWidget(spin)
        layout.addLayout(row)
        return spin

    def apply_image_processing(self, frame_rgb):
        hsv = cv2.cvtColor(frame_rgb, cv2.COLOR_RGB2HSV).astype("float32")
        hsv[:, :, 1] *= SATURATION_BOOST
        hsv[:, :, 1] = np.clip(hsv[:, :, 1], 0, 255)
        processed = cv2.cvtColor(hsv.astype("uint8"), cv2.COLOR_HSV2RGB)

        invGamma = 1.0 / GAMMA_CORRECTION
        table = np.array([((i / 255.0) ** invGamma) * 255 for i in np.arange(0, 256)]).astype("uint8")
        return cv2.LUT(processed, table)

    def process_folder_to_buffer(self, folder_path):
        files = [f for f in os.listdir(folder_path) if f.lower().endswith('.bmp')]
        files.sort(key=lambda f: [int(t) if t.isdigit() else t.lower() for t in re.split('([0-9]+)', f)])

        processed_frames = []
        for f in files:
            img = cv2.imread(os.path.join(folder_path, f))
            if img is not None:
                img = cv2.resize(img, (MATRIX_WIDTH, MATRIX_HEIGHT))
                img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
                img = self.apply_image_processing(img)

                pixel_data = []
                for y in range(MATRIX_HEIGHT):
                    row = img[y, :] if y % 2 == 0 else img[y, ::-1]
                    for pixel in row:
                        pixel_data.extend([pixel[0], pixel[1], pixel[2]])
                processed_frames.append(bytearray(pixel_data))
        return processed_frames

    def toggle_play(self):
        if not self.playing:
            try:
                self.serial_conn = serial.Serial(self.port_input.text().strip(), 1000000, timeout=0.1)
                self.playing = True
                self.btn_play.setText("STOP")
                self.btn_play.setProperty("playing", "true")
                self.refresh_style(self.btn_play)
                threading.Thread(target=self.play_loop, daemon=True).start()
            except Exception as e:
                self.status_label.setText(f"Erro: {e}")
        else:
            self.playing = False
            self.btn_play.setText("START")
            self.btn_play.setProperty("playing", "false")
            self.refresh_style(self.btn_play)
            if self.serial_conn: self.serial_conn.close()

    def play_loop(self):
        while self.playing:
            order = [self.list_widget.item(i).text().replace("▶ ", "") for i in range(self.list_widget.count())]
            for anim_name in order:
                if not self.playing: break
                config = self.animations_data.get(anim_name)
                if not config: continue

                frames = config.get('frames', [])
                loops = config.get('loops', 1)
                delay = config.get('ms_per_frame', 100) / 1000.0

                for l in range(loops):
                    if not self.playing: break
                    for i, frame_data in enumerate(frames):
                        if not self.playing: break
                        self.signals.status.emit(anim_name, l + 1, loops, i + 1, len(frames))

                        packet = bytearray([START_BYTE_1, START_BYTE_2]) + frame_data
                        try:
                            self.serial_conn.write(packet)
                        except:
                            self.playing = False;
                            break

                        if i == len(frames) - 1 and l == loops - 1 and config['hold_ms'] > 0:
                            time.sleep(config['hold_ms'] / 1000.0)
                        else:
                            time.sleep(delay)
        self.signals.finished.emit()

    def export_to_code(self):
        """Transforma as imagens carregadas em texto para colares no EMBEDDED_DATA."""
        if not self.animations_data:
            print("Nada para exportar.")
            return

        # Usamos pickle + base64 para comprimir os bytes e tornar o texto "colável"
        data_to_save = {name: d['frames'] for name, d in self.animations_data.items()}
        b64_data = base64.b64encode(pickle.dumps(data_to_save)).decode('utf-8')

        print("\n--- COPIE O CÓDIGO ABAIXO PARA A VARIÁVEL EMBEDDED_DATA ---\n")
        print(f"import base64, pickle")
        print(f"EMBEDDED_DATA = pickle.loads(base64.b64decode('{b64_data}'))")
        print("\n--- FIM DO CÓDIGO ---\n")
        self.status_label.setText("Código gerado no Terminal/Console!")

    def load_folders(self, directory=None):
        path = directory or QFileDialog.getExistingDirectory(self, "Pasta das Animações")
        if not path: return
        self.root_dir = path
        self.list_widget.clear()

        folders = sorted([f.path for f in os.scandir(path) if f.is_dir()])
        for f in folders:
            name = os.path.basename(f)
            frames = self.process_folder_to_buffer(f)
            if frames:
                self.animations_data[name] = {
                    'frames': frames,
                    'ms_per_frame': 100,
                    'loops': 1,
                    'hold_ms': 0
                }
                self.list_widget.addItem(name)

    def load_settings(self):
        # 1. Tentar carregar dados embutidos se existirem
        if EMBEDDED_DATA:
            for name, frames in EMBEDDED_DATA.items():
                self.animations_data[name] = {
                    'frames': frames, 'ms_per_frame': 100, 'loops': 1, 'hold_ms': 0
                }
                self.list_widget.addItem(name)

        # 2. Sobrepor com o JSON (Porta, Tempos, Ordem)
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, "r") as f:
                    cfg = json.load(f)
                self.port_input.setText(cfg.get("port", "COM20"))

                # Se houver uma pasta configurada, tenta carregar do disco (sobrescreve embutido)
                if os.path.exists(cfg.get("root_dir", "")):
                    self.load_folders(cfg["root_dir"])

                # Aplica tempos e ordem do JSON
                for name, settings in cfg.get("animations", {}).items():
                    if name in self.animations_data:
                        self.animations_data[name].update(settings)

                if "order" in cfg:
                    # Reordenar lista
                    saved_order = cfg["order"]
                    items = []
                    for name in saved_order:
                        for i in range(self.list_widget.count()):
                            if self.list_widget.item(i).text() == name:
                                items.append(self.list_widget.takeItem(i))
                                break
                    for item in items: self.list_widget.addItem(item)
            except:
                pass

    def save_settings(self):
        config = {
            "port": self.port_input.text(),
            "root_dir": self.root_dir,
            "animations": {n: {k: v for k, v in d.items() if k != 'frames'} for n, d in self.animations_data.items()},
            "order": [self.list_widget.item(i).text().replace("▶ ", "") for i in range(self.list_widget.count())]
        }
        with open(CONFIG_FILE, "w") as f: json.dump(config, f, indent=4)

    def update_current_config(self):
        sel = self.list_widget.selectedItems()
        if not sel: return
        name = sel[0].text().replace("▶ ", "")
        if name in self.animations_data:
            self.animations_data[name].update({
                'ms_per_frame': self.spin_ms.value(),
                'loops': self.spin_loops.value(),
                'hold_ms': self.spin_hold.value()
            })
            self.save_settings()

    def on_selection_changed(self):
        sel = self.list_widget.selectedItems()
        if not sel: return
        name = sel[0].text().replace("▶ ", "")
        cfg = self.animations_data.get(name)
        if cfg:
            self.spin_ms.blockSignals(True);
            self.spin_ms.setValue(cfg['ms_per_frame']);
            self.spin_ms.blockSignals(False)
            self.spin_loops.blockSignals(True);
            self.spin_loops.setValue(cfg['loops']);
            self.spin_loops.blockSignals(False)
            self.spin_hold.blockSignals(True);
            self.spin_hold.setValue(cfg['hold_ms']);
            self.spin_hold.blockSignals(False)

    @Slot(str, int, int, int, int)
    def update_status_ui(self, name, loop, t_loops, frame, t_frames):
        self.status_label.setText(f"PLAYING: {name} | Loop {loop}/{t_loops} | F: {frame}/{t_frames}")

    @Slot()
    def clear_status_ui(self):
        self.status_label.setText("Parado.")

    def refresh_style(self, widget):
        widget.style().unpolish(widget)
        widget.style().polish(widget)

    def closeEvent(self, event):
        self.save_settings()
        super().closeEvent(event)


if __name__ == "__main__":
    app = QApplication([])
    window = AnimationPlayer()
    window.show()
    app.exec()