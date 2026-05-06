import os
import time
import serial
import re
import cv2
import json
import numpy as np

# --- Configurações de Protocolo ---
START_BYTE_1 = 0xA5
START_BYTE_2 = 0x5A
MATRIX_WIDTH = 32
MATRIX_HEIGHT = 18
CONFIG_FILE = "config.json"

# --- Configurações de Imagem (Otimização para LEDs) ---
SATURATION_BOOST = 1.6
GAMMA_CORRECTION = 1.2


def apply_image_processing(frame_rgb):
    """Aplica saturação e correção gamma para compensar a cor dos LEDs."""
    hsv = cv2.cvtColor(frame_rgb, cv2.COLOR_RGB2HSV).astype("float32")
    hsv[:, :, 1] *= SATURATION_BOOST
    hsv[:, :, 1] = np.clip(hsv[:, :, 1], 0, 255)
    processed = cv2.cvtColor(hsv.astype("uint8"), cv2.COLOR_HSV2RGB)

    invGamma = 1.0 / GAMMA_CORRECTION
    table = np.array([((i / 255.0) ** invGamma) * 255 for i in np.arange(0, 256)]).astype("uint8")
    return cv2.LUT(processed, table)


def load_animation_frames(folder_path):
    """Lê frames BMP e converte para o formato zig-zag da matriz."""
    if not os.path.exists(folder_path):
        return None

    files = [f for f in os.listdir(folder_path) if f.lower().endswith('.bmp')]
    # Ordenação natural (1, 2, 10 em vez de 1, 10, 2)
    files.sort(key=lambda f: [int(t) if t.isdigit() else t.lower() for t in re.split('([0-9]+)', f)])

    processed_frames = []
    for f in files:
        img = cv2.imread(os.path.join(folder_path, f))
        if img is not None:
            img = cv2.resize(img, (MATRIX_WIDTH, MATRIX_HEIGHT))
            img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
            img = apply_image_processing(img)

            # Conversão para Zig-Zag
            pixel_data = []
            for y in range(MATRIX_HEIGHT):
                # Inverte a direção das linhas ímpares para o wiring em S
                row = img[y, :] if y % 2 == 0 else img[y, ::-1]
                for pixel in row:
                    pixel_data.extend([pixel[0], pixel[1], pixel[2]])
            processed_frames.append(bytearray(pixel_data))
    return processed_frames


def run_player():
    # 1. Carregar Configurações do JSON
    if not os.path.exists(CONFIG_FILE):
        print(f"Erro: {CONFIG_FILE} não encontrado!")
        return

    with open(CONFIG_FILE, "r") as f:
        cfg = json.load(f)

    port = cfg.get("port", "COM20")
    root_dir = cfg.get("root_dir", "")
    anim_configs = cfg.get("animations", {})
    order = cfg.get("order", [])

    # 2. Carregar todas as animações da pasta para a memória
    print(f"--- A carregar animações de: {root_dir} ---")
    animations_library = {}
    for anim_name in order:
        path = os.path.join(root_dir, anim_name)
        frames = load_animation_frames(path)
        if frames:
            animations_library[anim_name] = frames
            print(f"[OK] {anim_name}: {len(frames)} frames carregados.")
        else:
            print(f"[ERRO] Não foi possível carregar: {anim_name}")

    if not animations_library:
        print("Erro: Nenhuma animação carregada. Verifica o root_dir no config.json.")
        return

    # 3. Abrir Porta Serial
    try:
        ser = serial.Serial(port, 1000000, timeout=0.1)
        print(f"\nConectado em {port}. A reproduzir...\n")
    except Exception as e:
        print(f"Erro Serial: {e}")
        return

    # 4. Loop de Reprodução Infinito
    try:
        while True:
            for anim_name in order:
                frames = animations_library.get(anim_name)
                if not frames: continue

                # Puxa settings do JSON ou usa valores padrão
                meta = anim_configs.get(anim_name, {})
                fps_delay = meta.get("ms_per_frame", 100) / 1000.0
                loops = meta.get("loops", 1)
                hold_final = meta.get("hold_ms", 0) / 1000.0

                print(f"-> {anim_name} | Loops: {loops} | Speed: {meta.get('ms_per_frame')}ms")

                for l in range(loops):
                    for i, frame_data in enumerate(frames):
                        # Envia cabeçalho + payload
                        packet = bytearray([START_BYTE_1, START_BYTE_2]) + frame_data
                        ser.write(packet)

                        # Timing: Se for o último frame do último loop, aplica o hold
                        if i == len(frames) - 1 and l == loops - 1:
                            if hold_final > 0:
                                time.sleep(hold_final)
                        else:
                            time.sleep(fps_delay)

    except KeyboardInterrupt:
        print("\nInterrompido pelo utilizador.")
    finally:
        ser.close()
        print("Porta serial fechada.")


if __name__ == "__main__":
    run_player()