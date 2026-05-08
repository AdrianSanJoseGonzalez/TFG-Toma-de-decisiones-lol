"""
minimap_tracker.py — Detector de coordenadas via minimapa
===========================================================
Lee el minimapa de pantalla con OpenCV para extraer las coordenadas
de todos los campeones visibles en tiempo real.

Compatible con Vanguard: solo lee píxeles de pantalla, NO memoria del juego.

CALIBRACIÓN para 2560x1440 con minimapa por defecto:
  Minimapa en pantalla: (1985, 1072) → (2540, 1422)
  Tamaño: 555 x 350 pixels

USO:
    pip install opencv-python numpy pyautogui pillow mss keyboard
    python minimap_tracker.py

INTEGRACIÓN con live_collector.py:
    from minimap_tracker import MinimapTracker
    tracker = MinimapTracker()
    positions = tracker.get_positions()
    # positions = {'Ahri': (x, z), 'Jinx': (x, z), ...}
"""

import cv2
import numpy as np
import time
import os
import sys

try:
    import keyboard
    KEYBOARD_AVAILABLE = True
except ImportError:
    KEYBOARD_AVAILABLE = False
    print("[!] 'keyboard' no instalado. pip install keyboard")
    print("    Sin ella, el modo debug robará el foco del juego.")

# Añadir el directorio donde está zonas_mapa.py al path de Python
sys.path.append(r"C:\Users\Adrian\.gemini\antigravity\scratch\lol_replay_downloader")

# Importar las zonas para dibujar la calibración
try:
    from zonas_mapa import ZONAS_MAPA
except ImportError as e:
    print(f"Error importando ZONAS_MAPA: {e}")
    ZONAS_MAPA = {}

# ── Métodos de captura (de mejor a peor para juegos fullscreen) ──
# 1. dxcam  → DXGI Desktop Duplication, captura DirectX fullscreen
# 2. mss    → rápido pero solo funciona en ventana/borderless
# 3. pyautogui → fallback lento

DXCAM_AVAILABLE = False
MSS_AVAILABLE   = False
_dxcam_camera   = None  # instancia persistente de dxcam

try:
    import dxcam
    DXCAM_AVAILABLE = True
    print("[OK] dxcam disponible - captura DirectX fullscreen")
except ImportError:
    pass

if not DXCAM_AVAILABLE:
    try:
        import mss
        import mss.tools
        MSS_AVAILABLE = True
        print("[i] Usando mss - requiere ventana sin bordes")
    except ImportError:
        import pyautogui
        print("[i] Usando pyautogui - fallback lento")

# ══════════════════════════════════════════════════════════════
# CALIBRACIÓN DEL MINIMAPA
# Ajustada para 2560x1440 con minimapa por defecto
# Si usas otro tamaño de minimapa en opciones, ajusta estos valores
# ══════════════════════════════════════════════════════════════

# Coordenadas del minimapa en píxeles de pantalla
# ¡OJO! El minimapa del LoL es un CUADRADO perfecto (ratio 1:1).
# Viendo tu imagen, el +Centro está desalineado del río.
# Para alinear el mapa con las cruces:
# - Aumentar MINIMAP_X mueve el mapa hacia la IZQUIERDA en la ventana.
# - Disminuir MINIMAP_Y mueve el mapa hacia ABAJO en la ventana.
MINIMAP_X      = 2205   # 10px mas a la izquierda
MINIMAP_Y      = 1075   # Bajado 15px para incluir fuente azul
MINIMAP_W      = 345
MINIMAP_H      = 360    # Aumentado 15px para cubrir hasta abajo

# Coordenadas del mundo LoL (World Coordinates que representan los bordes del minimapa)
# El minimapa visual tiene "padding" (bordes negros), por lo que sus esquinas
# no son exactamente 0 y 14820. Vamos a calibrar esto.
global GAME_X_MIN, GAME_X_MAX, GAME_Z_MIN, GAME_Z_MAX
GAME_X_MIN = 600
GAME_X_MAX = 15320  
GAME_Z_MIN = 800
GAME_Z_MAX = 15920  

# ── Colores de los iconos en el minimapa ──────────────────────
# Equipo azul (ORDER) → círculos azules/cyan
# Equipo rojo (CHAOS) → círculos rojos
# Tú mismo → tiene un borde blanco adicional

# Rangos HSV para detección de colores
# HSV: Hue (0-179), Saturation (0-255), Value (0-255)

# Azul/Cyan (aliados ORDER)
BLUE_HSV_LOW  = np.array([85,  80, 100])
BLUE_HSV_HIGH = np.array([130, 255, 255])

# Rojo (enemigos CHAOS) — el rojo en HSV está en dos rangos
RED_HSV_LOW1  = np.array([0,   100, 100])
RED_HSV_HIGH1 = np.array([10,  255, 255])
RED_HSV_LOW2  = np.array([165, 100, 100])
RED_HSV_HIGH2 = np.array([179, 255, 255])

# Blanco (borde de tu propio campeón)
WHITE_HSV_LOW  = np.array([0,  0,  200])
WHITE_HSV_HIGH = np.array([179, 30, 255])


def pixel_to_game(px, py):
    """
    Convierte coordenadas de píxel del minimapa a coordenadas del juego.
    
    El minimapa tiene el origen (0,0) del juego en la esquina
    inferior izquierda, y el eje Z crece hacia arriba.
    """
    x_norm = (px - MINIMAP_X) / MINIMAP_W
    y_norm = (py - MINIMAP_Y) / MINIMAP_H

    # Mapear [0,1] -> [GAME_X_MIN, GAME_X_MAX]
    x_game = GAME_X_MIN + x_norm * (GAME_X_MAX - GAME_X_MIN)

    # Z crece de abajo a arriba (invertir Y)
    z_game = GAME_Z_MIN + (1.0 - y_norm) * (GAME_Z_MAX - GAME_Z_MIN)

    # Clamp a los límites del mapa
    x_game = max(GAME_X_MIN, min(GAME_X_MAX, x_game))
    z_game = max(GAME_Z_MIN, min(GAME_Z_MAX, z_game))

    return round(x_game, 0), round(z_game, 0)


def game_to_pixel(x_game, z_game, local_coords=False):
    """Inverso: coordenadas del juego → píxel en pantalla (o local al minimapa)."""
    # Normalizar al rango [0, 1] usando MIN y MAX
    game_range_x = GAME_X_MAX - GAME_X_MIN
    game_range_z = GAME_Z_MAX - GAME_Z_MIN
    
    # Evitar division por cero
    if game_range_x == 0: game_range_x = 1
    if game_range_z == 0: game_range_z = 1
    
    x_norm = (x_game - GAME_X_MIN) / game_range_x
    z_norm = (z_game - GAME_Z_MIN) / game_range_z
    
    px = x_norm * MINIMAP_W
    py = (1.0 - z_norm) * MINIMAP_H
    
    if not local_coords:
        px += MINIMAP_X
        py += MINIMAP_Y
        
    return int(px), int(py)


def capture_minimap():
    """
    Captura la región del minimapa.
    
    Orden de prioridad:
      1. dxcam  — captura DirectX fullscreen (DXGI Desktop Duplication)
      2. mss    — rápido, pero solo ventana/borderless
      3. pyautogui — fallback universal lento
    
    Devuelve imagen BGR de OpenCV.
    """
    global _dxcam_camera

    if DXCAM_AVAILABLE:
        # dxcam captura la pantalla completa y luego recortamos
        if _dxcam_camera is None:
            _dxcam_camera = dxcam.create(output_color="BGR")
        
        # Definir región como (left, top, right, bottom)
        region = (
            MINIMAP_X,
            MINIMAP_Y,
            MINIMAP_X + MINIMAP_W,
            MINIMAP_Y + MINIMAP_H,
        )
        frame = _dxcam_camera.grab(region=region)
        
        if frame is not None:
            return frame
        else:
            # Si falla (p.ej. la primera vez), capturar pantalla completa y recortar
            full = _dxcam_camera.grab()
            if full is not None:
                return full[
                    MINIMAP_Y : MINIMAP_Y + MINIMAP_H,
                    MINIMAP_X : MINIMAP_X + MINIMAP_W,
                ]
            # Si sigue fallando, intentar recrear la cámara
            _dxcam_camera = dxcam.create(output_color="BGR")
            frame = _dxcam_camera.grab(region=region)
            if frame is not None:
                return frame

    if MSS_AVAILABLE:
        region = {
            "left":   MINIMAP_X,
            "top":    MINIMAP_Y,
            "width":  MINIMAP_W,
            "height": MINIMAP_H,
        }
        with mss.mss() as sct:
            screenshot = sct.grab(region)
            img = np.array(screenshot)
            return cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)

    # Fallback: pyautogui
    import pyautogui
    screenshot = pyautogui.screenshot(region=(
        MINIMAP_X, MINIMAP_Y, MINIMAP_W, MINIMAP_H
    ))
    return cv2.cvtColor(np.array(screenshot), cv2.COLOR_RGB2BGR)


def find_champion_circles(img, color="blue"):
    """
    Detecta círculos de campeones en el minimapa por color.
    
    Los iconos de campeones en el minimapa son círculos con borde
    de color (azul para aliados, rojo para enemigos).
    
    Devuelve lista de (pixel_x, pixel_y, radio) relativos al minimapa.
    """
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)

    if color == "blue":
        mask = cv2.inRange(hsv, BLUE_HSV_LOW, BLUE_HSV_HIGH)
    elif color == "red":
        mask1 = cv2.inRange(hsv, RED_HSV_LOW1, RED_HSV_HIGH1)
        mask2 = cv2.inRange(hsv, RED_HSV_LOW2, RED_HSV_HIGH2)
        mask  = cv2.bitwise_or(mask1, mask2)
    elif color == "white":
        mask = cv2.inRange(hsv, WHITE_HSV_LOW, WHITE_HSV_HIGH)
    else:
        return []

    # Suavizar para reducir ruido
    mask = cv2.GaussianBlur(mask, (5, 5), 0)
    mask = cv2.dilate(mask, np.ones((3, 3), np.uint8), iterations=1)

    # Detectar círculos con Hough
    circles = cv2.HoughCircles(
        mask,
        cv2.HOUGH_GRADIENT,
        dp=1,
        minDist=15,         # Mínima distancia entre centros
        param1=50,
        param2=12,          # Umbral de acumulador (más bajo = más detecciones)
        minRadius=6,        # Radio mínimo del icono
        maxRadius=20,       # Radio máximo del icono
    )

    if circles is None:
        return []

    result = []
    circles = np.round(circles[0, :]).astype(int)
    for x, y, r in circles:
        # Convertir de coordenadas relativas al minimapa a absolutas
        px = MINIMAP_X + x
        py = MINIMAP_Y + y
        result.append((px, py, r))

    return result


def get_all_positions(img=None, player_names=None):
    """
    Detecta todas las posiciones de campeones visibles en el minimapa.
    
    Args:
        img: imagen del minimapa (si None, captura automáticamente)
        player_names: dict {'ORDER': [...], 'CHAOS': [...]} para nombrar
    
    Returns:
        dict {
            'ORDER': [(x_game, z_game), ...],  # aliados azules
            'CHAOS': [(x_game, z_game), ...],  # enemigos rojos
            'raw_pixels': {'blue': [...], 'red': [...]}
        }
    """
    if img is None:
        img = capture_minimap()
 
    blue_circles = find_champion_circles(img, "blue")
    red_circles  = find_champion_circles(img, "red")

    blue_positions = [pixel_to_game(px, py) for px, py, r in blue_circles]
    red_positions  = [pixel_to_game(px, py) for px, py, r in red_circles]

    return {
        "ORDER": blue_positions,
        "CHAOS": red_positions,
        "raw_pixels": {
            "blue": blue_circles,
            "red":  red_circles,
        }
    }


class MinimapTracker:
    """
    Clase principal para integrar con live_collector.py.
    
    Uso:
        tracker = MinimapTracker()
        positions = tracker.get_positions(player_list)
    """

    def __init__(self):
        self.last_positions = {}
        self.last_capture   = None

    def get_positions(self, player_list=None):
        """
        Captura el minimapa y devuelve las posiciones de todos
        los campeones visibles.
        
        Args:
            player_list: lista de jugadores de la API (para nombrarlos)
        
        Returns:
            dict {summoner_name: {'x': float, 'z': float}}
        """
        try:
            img       = capture_minimap()
            positions = get_all_positions(img, player_list)
            self.last_capture = img

            result = {}

            if player_list:
                # Intentar asignar posiciones a jugadores por equipo
                order_players = [p for p in player_list if p.get("team") == "ORDER"]
                chaos_players = [p for p in player_list if p.get("team") == "CHAOS"]

                # Asignación simple: el círculo más cercano a la última
                # posición conocida del jugador
                for i, pos in enumerate(positions["ORDER"]):
                    if i < len(order_players):
                        name = order_players[i].get("summonerName", f"ORDER_{i}")
                        result[name] = {"x": pos[0], "z": pos[1]}

                for i, pos in enumerate(positions["CHAOS"]):
                    if i < len(chaos_players):
                        name = chaos_players[i].get("summonerName", f"CHAOS_{i}")
                        result[name] = {"x": pos[0], "z": pos[1]}
            else:
                # Sin nombres, devolver por índice
                for i, pos in enumerate(positions["ORDER"]):
                    result[f"ORDER_{i}"] = {"x": pos[0], "z": pos[1]}
                for i, pos in enumerate(positions["CHAOS"]):
                    result[f"CHAOS_{i}"] = {"x": pos[0], "z": pos[1]}

            self.last_positions = result
            return result

        except Exception as e:
            print(f"[MinimapTracker] Error: {e}")
            return self.last_positions  # Devolver última posición conocida

    def debug_view(self, save_path="debug_minimap.png"):
        """
        Modo calibracion para UNA SOLA PANTALLA.
        
        Dos modos:
          MODO JUEGO:  Estas en el LoL, pulsas F5 para capturar.
          MODO AJUSTE: Estas en el terminal, escribes letras para mover zonas.
        """
        global GAME_X_MIN, GAME_X_MAX, GAME_Z_MIN, GAME_Z_MAX

        if not KEYBOARD_AVAILABLE:
            print("[ERROR] Necesitas la libreria 'keyboard': pip install keyboard")
            return {}

        save_path = os.path.abspath(save_path)

        def _draw_overlay(img):
            """Dibuja zonas y referencia sobre la imagen, guarda a disco."""
            debug = img.copy()
            for nombre_zona, poligono in ZONAS_MAPA.items():
                pts = []
                for (gx, gy) in poligono:
                    px, py = game_to_pixel(gx, gy, local_coords=True)
                    pts.append([px, py])
                if pts:
                    pts_arr = np.array(pts, np.int32).reshape((-1, 1, 2))
                    cv2.polylines(debug, [pts_arr], isClosed=True, color=(0, 255, 0), thickness=1)
            ref_points = [
                (560,  560,  "Fuente_Azul"),
                (7410, 7410, "Centro"),
                (14340, 14390, "Fuente_Roja"),
            ]
            for x_g, z_g, label in ref_points:
                cx, cy = game_to_pixel(x_g, z_g, local_coords=True)
                if 0 <= cx < MINIMAP_W and 0 <= cy < MINIMAP_H:
                    cv2.drawMarker(debug, (cx, cy), (0, 255, 255), cv2.MARKER_CROSS, 10, 1)
                    cv2.putText(debug, label, (cx+5, cy), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (0, 255, 255), 1)
            info = f"X:[{GAME_X_MIN},{GAME_X_MAX}] Z:[{GAME_Z_MIN},{GAME_Z_MAX}]"
            cv2.putText(debug, info, (10, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.35, (255, 255, 255), 1)
            cv2.imwrite(save_path, debug)

        def _print_menu():
            print()
            print("-"*45)
            print("  AJUSTE DE CALIBRACION")
            print("-"*45)
            print(f"  Valores actuales:")
            print(f"    X_MIN={GAME_X_MIN}  X_MAX={GAME_X_MAX}")
            print(f"    Z_MIN={GAME_Z_MIN}  Z_MAX={GAME_Z_MAX}")
            print()
            print("  Escribe una letra + Enter:")
            print("    a/d = X_MIN -/+100")
            print("    j/l = X_MAX -/+100")
            print("    w/s = Z_MAX +/-100")
            print("    i/k = Z_MIN +/-100")
            print()
            print("    f = volver al juego (capturar con F5)")
            print("    q = salir y guardar")
            print("-"*45)

        # ── PASO 1: Primera captura ──
        print("\n" + "="*50)
        print("  CALIBRACION DEL MINIMAPA")
        print("="*50)
        print(f"  Imagen: {save_path}")
        print()
        print("  1. Vuelve al juego (alt-tab)")
        print("  2. Pulsa F5 para capturar el minimapa")
        print("  3. Vuelve aqui (alt-tab) para ajustar")
        print("="*50)

        # Abrir visor HTML que se auto-refresca
        viewer_path = os.path.join(os.path.dirname(save_path), "minimap_viewer.html")
        if os.path.exists(viewer_path):
            print(f"\n  Abriendo visor en el navegador...")
            import webbrowser
            webbrowser.open(f"file:///{viewer_path}")
            print(f"  -> El navegador muestra la imagen y se refresca solo!")
        else:
            print(f"\n  [!] No se encontro {viewer_path}")
            print(f"      Abre debug_minimap.png manualmente")

        last_img = None
        frame_count = 0
        positions = {}

        while True:
            # ── MODO JUEGO: esperar F5 ──
            print("\n>> Esperando F5 desde el juego...")
            print("   (pulsa Ctrl+C aqui para salir)\n")

            try:
                while True:
                    if keyboard.is_pressed('f5'):
                        frame_count += 1
                        last_img = capture_minimap()
                        positions = get_all_positions(last_img)
                        _draw_overlay(last_img)
                        print(f"  [OK] Captura #{frame_count} guardada!")
                        print(f"  -> {save_path}")
                        print(f"  -> Alt-tab aqui para ajustar las zonas")
                        time.sleep(0.5)  # evitar doble captura
                        break
                    if keyboard.is_pressed('f10'):
                        print("\n  Saliendo...")
                        self._print_final(GAME_X_MIN, GAME_X_MAX, GAME_Z_MIN, GAME_Z_MAX, save_path, frame_count)
                        return positions
                    time.sleep(0.05)
            except KeyboardInterrupt:
                self._print_final(GAME_X_MIN, GAME_X_MAX, GAME_Z_MIN, GAME_Z_MAX, save_path, frame_count)
                return positions

            # ── MODO AJUSTE: leer del terminal ──
            _print_menu()

            while True:
                try:
                    cmd = input("  > ").strip().lower()
                except (KeyboardInterrupt, EOFError):
                    self._print_final(GAME_X_MIN, GAME_X_MAX, GAME_Z_MIN, GAME_Z_MAX, save_path, frame_count)
                    return positions

                if cmd == 'q':
                    self._print_final(GAME_X_MIN, GAME_X_MAX, GAME_Z_MIN, GAME_Z_MAX, save_path, frame_count)
                    return positions
                elif cmd == 'f':
                    break  # volver a modo juego
                elif cmd == 'a':
                    GAME_X_MIN -= 100
                    print(f"    X_MIN = {GAME_X_MIN}")
                elif cmd == 'd':
                    GAME_X_MIN += 100
                    print(f"    X_MIN = {GAME_X_MIN}")
                elif cmd == 'j':
                    GAME_X_MAX -= 100
                    print(f"    X_MAX = {GAME_X_MAX}")
                elif cmd == 'l':
                    GAME_X_MAX += 100
                    print(f"    X_MAX = {GAME_X_MAX}")
                elif cmd == 'w':
                    GAME_Z_MAX += 100
                    print(f"    Z_MAX = {GAME_Z_MAX}")
                elif cmd == 's':
                    GAME_Z_MAX -= 100
                    print(f"    Z_MAX = {GAME_Z_MAX}")
                elif cmd == 'i':
                    GAME_Z_MIN += 100
                    print(f"    Z_MIN = {GAME_Z_MIN}")
                elif cmd == 'k':
                    GAME_Z_MIN -= 100
                    print(f"    Z_MIN = {GAME_Z_MIN}")
                elif cmd == '':
                    continue
                else:
                    print("    ? Comando no reconocido. Usa a/d/j/l/w/s/i/k/f/q")
                    continue

                # Re-dibujar sobre la misma captura
                if last_img is not None:
                    _draw_overlay(last_img)
                    print("    -> Imagen actualizada! Refresca el visor.")

    @staticmethod
    def _print_final(x_min, x_max, z_min, z_max, save_path, count):
        print(f"\n{'='*50}")
        print(f"  CALIBRACION FINALIZADA ({count} capturas)")
        print(f"{'='*50}")
        print(f"  Copia estos valores a tu codigo:\n")
        print(f"  GAME_X_MIN = {x_min}")
        print(f"  GAME_X_MAX = {x_max}")
        print(f"  GAME_Z_MIN = {z_min}")
        print(f"  GAME_Z_MAX = {z_max}")
        print(f"\n  Ultima imagen: {save_path}")
        print(f"{'='*50}")


# ── Test standalone ───────────────────────────────────────────────
if __name__ == "__main__":
    print("="*60)
    print("  MinimapTracker — Test de calibración")
    print("="*60)
    print(f"  Minimapa en: ({MINIMAP_X},{MINIMAP_Y}) tamaño {MINIMAP_W}x{MINIMAP_H}")
    print()

    if not MSS_AVAILABLE:
        print("[!] mss no instalado, usando pyautogui (más lento)")
        print("    pip install mss  para mejor rendimiento")
    
    print("Iniciando en 5 segundos... pon el juego en pantalla")
    for i in range(5, 0, -1):
        print(f"  {i}...", end="\r")
        time.sleep(1)

    print("\nCapturando minimapa...")
    tracker = MinimapTracker()
    positions = tracker.debug_view("debug_minimap.png")

    print()
    print("POSICIONES DETECTADAS:")
    print(f"  Equipo azul (ORDER): {positions['ORDER']}")
    print(f"  Equipo rojo (CHAOS): {positions['CHAOS']}")
    print()
    print("Súbeme 'debug_minimap.png' para ver si la detección es correcta.")
    print()

    # Test de conversión de coordenadas
    print("TEST DE CONVERSIÓN:")
    test_cases = [
        (MINIMAP_X,            MINIMAP_Y + MINIMAP_H, "Esquina inf-izq (fuente azul)"),
        (MINIMAP_X + MINIMAP_W, MINIMAP_Y,            "Esquina sup-der (fuente roja)"),
        (MINIMAP_X + MINIMAP_W//2, MINIMAP_Y + MINIMAP_H//2, "Centro"),
    ]
    for px, py, label in test_cases:
        x_g, z_g = pixel_to_game(px, py)
        print(f"  {label}: pixel({px},{py}) → juego({x_g:.0f},{z_g:.0f})")
