"""
Auto-calibracion con cuenta atras.
Ejecuta, alt-tab a LoL, espera la captura.
"""
import mss
import numpy as np
from PIL import Image
import cv2
import os
import sys
import time

import pytesseract
pytesseract.pytesseract.tesseract_cmd = r"C:\Users\Adrian\AppData\Local\Programs\Tesseract-OCR\tesseract.exe"

MONITOR_INDEX = 1
UPSCALE = 5
TEMPLATES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "digit_templates")

HUD_REGIONS = {
    "hp":    {"top": 1130, "left": 115, "width": 115, "height": 27},
    "mana":  {"top": 1160, "left": 100, "width": 130, "height": 27},
    "ad":    {"top": 1130, "left": 265, "width":  43, "height": 27},
    "ap":    {"top": 1130, "left": 335, "width":  43, "height": 27},
    "armor": {"top": 1160, "left": 265, "width":  43, "height": 27},
    "mr":    {"top": 1160, "left": 335, "width":  43, "height": 27},
    "as":    {"top": 1185, "left": 265, "width":  43, "height": 27},
    "speed": {"top": 1185, "left": 335, "width":  43, "height": 27},
}


def grab_region(sct, region):
    monitor = sct.monitors[MONITOR_INDEX]
    r = {
        "top":    monitor["top"]  + region["top"],
        "left":   monitor["left"] + region["left"],
        "width":  region["width"],
        "height": region["height"],
        "mon":    MONITOR_INDEX,
    }
    shot = sct.grab(r)
    return Image.frombytes("RGB", shot.size, shot.rgb)


def preprocess_hsv(img, sat_max=70, val_min=170):
    up = img.resize((img.width * UPSCALE, img.height * UPSCALE), Image.LANCZOS)
    arr = np.array(up)
    hsv = cv2.cvtColor(arr, cv2.COLOR_RGB2HSV)
    mask = (hsv[:, :, 1] < sat_max) & (hsv[:, :, 2] > val_min)
    binary = (mask * 255).astype(np.uint8)
    k = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
    binary = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, k)
    return binary


def preprocess_stat(img):
    up = img.resize((img.width * UPSCALE, img.height * UPSCALE), Image.LANCZOS)
    gray = cv2.cvtColor(np.array(up), cv2.COLOR_RGB2GRAY)
    _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    if np.mean(binary) > 128:
        binary = 255 - binary
    ys, xs = np.where(binary > 128)
    if len(xs) > 0:
        m = 6
        binary = binary[max(0,ys.min()-m):min(binary.shape[0],ys.max()+m),
                        max(0,xs.min()-m):min(binary.shape[1],xs.max()+m)]
    return binary


def segment_digits(binary):
    col_sum = binary.sum(axis=0)
    in_char = False
    segments = []
    x_start = 0
    for x in range(len(col_sum)):
        if col_sum[x] > 0 and not in_char:
            x_start = x
            in_char = True
        elif col_sum[x] == 0 and in_char:
            in_char = False
            if x - x_start > 3:
                segments.append((x_start, x))
    if in_char and len(col_sum) - x_start > 3:
        segments.append((x_start, len(col_sum)))
    
    merged = []
    for seg in segments:
        if merged and seg[0] - merged[-1][1] < 4:
            merged[-1] = (merged[-1][0], seg[1])
        else:
            merged.append(seg)
    
    ys = np.where(binary.sum(axis=1) > 0)[0]
    if len(ys) == 0:
        return []
    y0, y1 = ys.min(), ys.max() + 1
    return [binary[y0:y1, x0:x1] for x0, x1 in merged]


def ocr_single_digit(crop):
    pad = 20
    padded = np.zeros((crop.shape[0]+pad*2, crop.shape[1]+pad*2), dtype=np.uint8)
    padded[pad:pad+crop.shape[0], pad:pad+crop.shape[1]] = crop
    pil = Image.fromarray(padded)
    config = "--psm 10 --oem 1 -c tessedit_char_whitelist=0123456789/"
    text = pytesseract.image_to_string(pil, config=config).strip()
    data = pytesseract.image_to_data(pil, config=config, output_type=pytesseract.Output.DICT)
    confs = [int(c) for c in data['conf'] if int(c) > 0]
    avg_conf = sum(confs) / len(confs) if confs else 0
    return text[:1] if text else "", avg_conf


def main():
    os.makedirs(TEMPLATES_DIR, exist_ok=True)
    debug_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "debug_calibrate")
    os.makedirs(debug_dir, exist_ok=True)
    
    print("=" * 50)
    print("  AUTO-CALIBRACION")
    print("=" * 50)
    print()
    print("  HAZ ALT+TAB A LA VENTANA DE LOL AHORA!")
    print("  Asegurate de tener un campeon seleccionado.")
    print()
    
    for i in range(5, 0, -1):
        print(f"  Capturando en {i}...", flush=True)
        time.sleep(1)
    
    print("  >> CAPTURANDO!", flush=True)
    
    with mss.mss() as sct:
        imgs = {k: grab_region(sct, r) for k, r in HUD_REGIONS.items()}
    
    print("  >> Captura completada. Procesando...\n")
    
    all_digits = {}
    
    for name, img in imgs.items():
        # Guardar raw
        raw_up = img.resize((img.width * UPSCALE, img.height * UPSCALE), Image.LANCZOS)
        raw_up.save(os.path.join(debug_dir, f"{name}_raw.png"))
        
        if name in ("hp", "mana"):
            binary = preprocess_hsv(img)
        else:
            binary = preprocess_stat(img)
        
        cv2.imwrite(os.path.join(debug_dir, f"{name}_proc.png"), binary)
        
        # Texto completo para referencia
        config_full = "--psm 7 --oem 1 -c tessedit_char_whitelist=0123456789/"
        full_text = pytesseract.image_to_string(Image.fromarray(binary), config=config_full).strip()
        
        digits = segment_digits(binary)
        print(f"  [{name:8s}] texto='{full_text}', segmentos={len(digits)}")
        
        for i, crop in enumerate(digits):
            char, conf = ocr_single_digit(crop)
            cv2.imwrite(os.path.join(debug_dir, f"{name}_d{i}_{char or 'UNK'}_c{conf:.0f}.png"), crop)
            if char and char in "0123456789/":
                print(f"             -> digito '{char}' conf={conf:.0f}%  ({crop.shape[1]}x{crop.shape[0]})")
                if char not in all_digits:
                    all_digits[char] = []
                all_digits[char].append((crop, conf, name))
    
    # Guardar mejores templates
    print("\n  --- RESULTADOS ---")
    saved = 0
    for char in sorted(all_digits.keys()):
        best = max(all_digits[char], key=lambda x: x[1])
        crop, conf, region = best
        fname = {"/": "slash", ".": "dot"}.get(char, char)
        path = os.path.join(TEMPLATES_DIR, f"{fname}.png")
        cv2.imwrite(path, crop)
        saved += 1
        print(f"  '{char}' guardado ({crop.shape[1]}x{crop.shape[0]}, conf={conf:.0f}%, de={region})")
    
    missing = set("0123456789/") - set(all_digits.keys())
    print(f"\n  Guardados: {saved}/11")
    if missing:
        print(f"  Faltan: {missing}")
    else:
        print(f"  COMPLETO! Ya puedes usar read_hud_stats.py")
    
    print(f"\n  Imagenes debug en: {debug_dir}")
    print(f"  Templates en: {TEMPLATES_DIR}")


if __name__ == "__main__":
    main()
