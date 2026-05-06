import os
import time
import serial
import serial.tools.list_ports
import re
import cv2
import json
import numpy as np

# --- Configurações de Protocolo ---
START_BYTE_1 = 0xA5
START_BYTE_2 = 0x5A
MATRIX_WIDTH = 32
MATRIX_HEIGHT = 18
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")

# --- Configurações de Imagem ---
SATURATION_BOOST = 1.6
GAMMA_CORRECTION = 1.2


def find_arduino_port():
    """Procura automaticamente pela porta serial do Arduino/Mesa."""
    ports = list(serial.tools.list_ports.comports())
    # IDs comuns: Arduino (2341), CH340 (1A86), CP210x (10C4)
    target_hwids = ["2341", "1A86", "10C4", "0403"]

    for p in ports:
        # Tenta pelo HWID (mais seguro)
        if any(hwid in p.hwid.upper() for hwid in target_hwids):
            return p.device
        # Tenta por palavras-chave comuns
        if any(x in p.description.upper() for x in ["ARDUINO", "USB SERIAL", "CH340"]):
            return p.device

    # Fallback para Linux/Pi (primeiro dispositivo ACM ou USB detectado)
    for p in ports:
        if "ttyACM" in p.device or "ttyUSB" in p.device:
            return p.device
    return None


def get_animations_path():
    """Tenta encontrar a pasta Animations no diretório do script."""
    local_path = os.path.join(BASE_DIR, "Animations")
    if os.path.exists(local_path):
        return local_path
    return None


def apply_image_processing(frame_rgb):
    hsv = cv2.cvtColor(frame_rgb, cv2.COLOR_RGB2HSV).astype("float32")
    hsv[:, :, 1] *= SATURATION_BOOST
    hsv[:, :, 1] = np.clip(hsv[:, :, 1], 0, 255)
    processed = cv2.cvtColor(hsv.astype("uint8"), cv2.COLOR_HSV2RGB)

    invGamma = 1.0 / GAMMA_CORRECTION
    table = np.array([((i / 255.0) ** invGamma) * 255 for i in np.arange(0, 256)]).astype("uint8")
    return cv2.LUT(processed, table)


def load_animation_frames(folder_path):
    if not os.path.exists(folder_path):
        return None
    files = [f for f in os.listdir(folder_path) if f.lower().endswith('.bmp')]
    files.sort(key=lambda f: [int(t) if t.isdigit() else t.lower() for t in re.split('([0-9]+)', f)])

    processed_frames = []
    for f in files:
        img = cv2.imread(os.path.join(folder_path, f))
        if img is not None:
            img = cv2.resize(img, (MATRIX_WIDTH, MATRIX_HEIGHT))
            img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
            img = apply_image_processing(img)
            pixel_data = []
            for y in range(MATRIX_HEIGHT):
                row = img[y, :] if y % 2 == 0 else img[y, ::-1]
                for pixel in row:
                    pixel_data.extend([pixel[0], pixel[1], pixel[2]])
            processed_frames.append(bytearray(pixel_data))
    return processed_frames


def run_player():
    # 1. Carregar Configurações base
    cfg = {}
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r") as f:
            cfg = json.load(f)

    # --- LÓGICA INTELIGENTE DE PORTA ---
    port = find_arduino_port() or cfg.get("port", "/dev/ttyACM0")
    print(f"[INFO] Porta Serial detectada: {port}")

    # --- LÓGICA INTELIGENTE DE PASTA ---
    root_dir = get_animations_path() or cfg.get("root_dir", "")
    print(f"[INFO] Pasta de Animações: {root_dir}")

    anim_configs = cfg.get("animations", {})
    order = cfg.get("order", [])

    if not root_dir or not os.path.exists(root_dir):
        print("Erro: Pasta 'Animations' não encontrada!")
        return

    # 2. Carregar animações
    print(f"--- A carregar animações de: {root_dir} ---")
    animations_library = {}

    # Se a ordem estiver vazia no JSON, carrega todas as subpastas em ordem alfabética
    play_order = order if order else sorted(next(os.walk(root_dir))[1])

    for anim_name in play_order:
        path = os.path.join(root_dir, anim_name)
        frames = load_animation_frames(path)
        if frames:
            animations_library[anim_name] = frames
            print(f"[OK] {anim_name}: {len(frames)} frames.")
        else:
            print(f"[!] Falha ao carregar: {anim_name}")

    if not animations_library:
        print("Erro: Nenhuma animação válida carregada.")
        return

    # 3. Serial
    try:
        ser = serial.Serial(port, 1000000, timeout=0.1)
        time.sleep(2)  # Aguarda boot do Arduino
    except Exception as e:
        print(f"Erro Serial: {e}")
        return

    # 4. Loop
    try:
        while True:
            for anim_name in play_order:
                frames = animations_library.get(anim_name)
                if not frames: continue

                meta = anim_configs.get(anim_name, {})
                fps_delay = meta.get("ms_per_frame", 100) / 1000.0
                loops = meta.get("loops", 1)
                hold_final = meta.get("hold_ms", 0) / 1000.0

                print(f"-> {anim_name} ({loops}x)")

                for l in range(loops):
                    for i, frame_data in enumerate(frames):
                        packet = bytearray([START_BYTE_1, START_BYTE_2]) + frame_data
                        ser.write(packet)

                        if i == len(frames) - 1 and l == loops - 1:
                            if hold_final > 0: time.sleep(hold_final)
                        else:
                            time.sleep(fps_delay)

    except KeyboardInterrupt:
        print("\nDesligado.")
    finally:
        if 'ser' in locals(): ser.close()


if __name__ == "__main__":
    run_player()