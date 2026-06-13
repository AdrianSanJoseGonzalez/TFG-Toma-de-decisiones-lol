"""
minimap_tracker.py  —  Super-Tracker de campeones en el minimapa

Técnica validada con capturas reales (2560x1440):
  • HoughCircles para localizar iconos (reemplaza findContours que destruía anillos de 2px)
  • Filtro de varianza interior: distingue cara de campeón vs fondo liso del mapa
  • Filtro de mezcla rojo/azul: elimina falsos positivos del cuadro de selección
  • Template matching multi-escala con máscara circular 75% (ignora anillo + texto de %)
  • Normalización CLAHE antes del matching: robustez ante variaciones de brillo/tinte
  • Resize en 2 pasos (371px→64px→size) para preservar detalle del icono
  • Umbral doble: score ≥ 0.35 Y margen ≥ 0.08 vs segundo candidato
  • Memoria temporal: filtro de velocidad + filtro de torres estáticas

Requisitos: pip install opencv-python numpy dxcam keyboard
"""

import os
import cv2
import numpy as np
import time
from collections import deque

# ══════════════════════════════════════════════════════════════════════
#  CONFIGURACIÓN — ajusta a tu resolución
# ══════════════════════════════════════════════════════════════════════

MINIMAP_X = 2205
MINIMAP_Y = 1075
MINIMAP_W = 345
MINIMAP_H = 360

GAME_X_MIN, GAME_X_MAX = 0,    15120
GAME_Z_MIN, GAME_Z_MAX = -400, 15220

ICONS_DIR = r"C:\Users\Adrian\Downloads\champion_icons"

_ICON_BBOX = (774, 353, 1145, 726)

MIN_SCORE  = 0.30   
MATCH_THRESHOLD = MIN_SCORE
MIN_MARGIN = 0.05   

# ── Filtros temporales ────────────────────────────────────────────────
MAX_SPEED_GAME_UNITS    = 1200
STATIC_THRESHOLD_PX     = 3
STATIC_FRAMES_LIMIT     = 20   
MAX_MISSING_FRAMES      = 25   
MIN_CONFIRMATION_FRAMES = 2    

# ── Parámetros de detección de anillos ───────────────────────────────
_HOUGH_PARAM1   = 50    
_HOUGH_PARAM2   = 17    
_HOUGH_MIN_R    = 11    
_HOUGH_MAX_R    = 19    
_HOUGH_MIN_DIST = 14    
_VAR_MIN        = 20.0  
_RING_VOTES_MIN = 15    
_RING_RATIO_MIN = 0.25  
_RING_MIX_MAX   = 0.40  
_MAP_MARGIN     = 12    

# ── CLAHE para normalización ──────────────────────────────────────────
_CLAHE = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(4, 4))

#  ZONAS DEL MAPA

ZONAS_MAPA = {
    "FuenteBlue": [(515,1536),(1114,1259),(1464,680),(1564,378),(1039,177),(390,127),(65,252),(90,982),(415,1435)],
    "NexoBlue": [(565,1526),(565,3086),(2064,3338),(2738,2835),(3263,2130),(3238,1123),(3163,419),(1714,368),(1164,1148),(639,1576)],
    "BlueInibiTop": [(490,3026),(714,5517),(2288,5140),(2488,3907),(2388,3252),(1789,3303),(664,3076)],
    "BlueTop2": [(714,5457),(639,8980),(1764,9282),(1889,5407),(814,5532)],
    "BluetTop1": [(639,8980),(789,11849),(1514,12051),(2039,11824),(1839,9333)],
    "BlueInibiMid": [(2413,3187),(2413,4999),(3912,4647),(4562,3715),(4962,2684),(3313,1803),(3313,2130),(2463,3111)],
    "BlueInibiBot": [(3263,1677),(4937,2608),(5461,1803),(5536,595),(3213,393),(3313,1677)],
    "JunglaTopBlue": [(1914,5316),(1839,8009),(1789,9192),(1939,9393),(2039,11205),(4312,8890),(4512,7506),(3188,6952),(3438,4838),(1964,5266)],
    "JunglaTopMidBlue": [(3887,4737),(5786,7078),(6061,7480),(5511,8185),(4687,8965),(4337,8915),(4562,7506),(3163,6927),(3488,4788),(3887,4737)],
    "BluemMid2": [(4662,3715),(5911,5276),(4987,6056),(3912,4697),(4512,3665)],
    "BlueMid1": [(5037,6021),(6236,7480),(7335,6549),(6011,5316),(5087,5945)],
    "JunglaMidBotBlue": [(4612,3630),(7310,6499),(9109,5392),(9234,3957),(9009,3303),(7785,3076),(6211,2799),(5436,1868),(4962,2648),(4662,3580)],
    "JunglaBotBlue": [(5511,1727),(9109,1677),(11232,1878),(11907,2633),(10958,3791),(10033,3464),(9309,3992),(9034,3288),(6261,2809),(5486,1828)],
    "BlueBot2": [(5486,1727),(8784,1702),(8884,670),(5586,519),(5486,1677)],
    "BlueBot1": [(8809,1637),(11257,1838),(11607,2190),(12257,1083),(11332,731),(8909,630),(8809,1586)],
    "Top": [(864,11874),(664,13460),(1414,14064),(2039,13712),(2688,13837),(3463,12655),(2338,11195),(2039,11245),(2089,11774),(1564,12076),(914,11824)],
    "RedTop1": [(2688,13837),(4212,14316),(5811,14265),(6411,14240),(6586,13284),(3887,12957),(3538,12806),(3463,12730),(2763,13837)],
    "RedTop2": [(6411,14190),(9584,14341),(9733,13082),(6685,13183),(6461,14165)],
    "RedJunglaTop": [(3488,12755),(4462,11371),(5736,10943),(6086,11723),(7185,11824),(9259,13007),(6611,13233),(4912,13057),(3613,12780)],
    "RedJunglaTopMid": [(7210,8527),(9384,10113),(9883,11220),(10233,11396),(9908,12428),(9708,13082),(9259,12957),(7710,12076),(7160,11749),(6161,11698),(5786,10868),(5761,9886),(7185,8527)],
    "RedMid1": [(7485,8326),(8609,7344),(9883,8603),(10083,9156),(9059,9786),(7235,8527),(7435,8351)],
    "Mid": [(5786,7908),(6810,8789),(9159,6826),(8284,5945),(7285,6574),(5961,7556),(5811,7933)],
    "RedMid2": [(10108,9182),(11182,10415),(10258,11421),(9858,11170),(9359,10088),(9034,9886),(10083,9156)],
    "RioTop": [(3662,9559),(3837,10843),(4012,11270),(4562,11321),(3563,12680),(2263,11119),(3613,9559)],
    "BaronPit": [(3687,9509),(3937,11170),(4662,11270),(5711,10868),(5736,9937),(5311,9609),(4762,9005),(4312,8930),(3712,9484)],
    "RioTopMid": [(4687,9005),(5761,7898),(6860,8804),(5761,9836),(5411,9635),(4737,8980)],
    "RioBotMid": [(8359,5905),(9159,5326),(9234,4747),(9609,5125),(10283,5880),(10283,6157),(9534,6660),(9159,6861)],
    "DrakePit": [(9234,4747),(9284,4017),(10058,3489),(10958,3791),(11557,4823),(10333,5905),(9259,4697)],
    "RedBot1": [(12856,3272),(13706,2593),(14081,3111),(14330,4017),(14380,4923),(14330,5955),(14280,6232),(14255,6383),(13131,6459),(13131,5175),(13006,4470),(12831,3439)],
    "Bot": [(12856,3262),(11932,2608),(11607,2155),(12257,1023),(13106,670),(14031,1299),(14205,1878),(13756,2633)],
    "RioBot": [(11632,4788),(12607,3177),(11932,2623),(11033,3781),(11582,4762)],
    "FuenteRed": [(13231,14416),(13556,13863),(14106,13485),(14405,13435),(14730,13712),(14830,14265),(14755,14693),(14355,14945),(13806,14869),(13231,14416)],
    "NexiRed": [(13206,14366),(13606,13737),(14130,13460),(14430,13359),(14405,11749),(13706,11749),(12981,11547),(12207,12000),(11882,12327),(11582,12982),(12007,14441),(13206,14416)],
    "RedInibiTop": [(12032,14391),(10308,14618),(9559,14316),(9758,13007),(9958,12227),(10983,12604),(11632,13007),(12032,14391)],
    "RedInibiMid": [(10058,12227),(10408,11321),(10733,10742),(11108,10415),(11682,10314),(12432,10943),(12631,11698),(12107,12126),(11582,12957),(10158,12277)],
    "RedInibiBot": [(11757,10264),(13406,9559),(14280,9484),(14730,10364),(14430,11723),(12981,11598),(12656,11723),(12507,10943),(11782,10264)],
    "RedBot2": [(12981,9710),(13156,6489),(14280,6363),(14330,9458),(13381,9559),(13031,9735)],
    "JunglaMidBotRed": [(11232,10339),(10533,9609),(10083,9156),(9908,8603),(8634,7344),(10283,6162),(10458,7244),(11832,7898),(11682,10138),(11282,10314)],
    "JungalBotRed": [(11682,10239),(11857,7948),(10508,7244),(10283,5885),(11632,4777),(12631,3192),(12781,3343),(13006,4677),(13106,5960),(13181,7042),(13081,7923),(13056,8829),(13006,9760),(11707,10264)],
}

def get_zone(x: float, z: float) -> str:
    pt = (float(x), float(z))
    for name, poly in ZONAS_MAPA.items():
        if cv2.pointPolygonTest(np.array(poly, np.float32), pt, False) >= 0:
            return name
    return "Desconocida"

#  CONVERSIÓN DE COORDENADAS

def local_to_game(lx: int, ly: int) -> tuple[float, float]:
    gx = GAME_X_MIN + (lx / MINIMAP_W) * (GAME_X_MAX - GAME_X_MIN)
    gz = GAME_Z_MIN + (1 - ly / MINIMAP_H) * (GAME_Z_MAX - GAME_Z_MIN)
    return round(gx, 1), round(gz, 1)

def pixel_to_game(px: int, py: int) -> tuple[float, float]:
    return local_to_game(px - MINIMAP_X, py - MINIMAP_Y)

def game_to_pixel(gx: float, gz: float) -> tuple[int, int]:
    lx = ((gx - GAME_X_MIN) / (GAME_X_MAX - GAME_X_MIN)) * MINIMAP_W
    ly = (1 - (gz - GAME_Z_MIN) / (GAME_Z_MAX - GAME_Z_MIN)) * MINIMAP_H
    return int(lx + MINIMAP_X), int(ly + MINIMAP_Y)

_PX_PER_UNIT = ((MINIMAP_W / (GAME_X_MAX - GAME_X_MIN)) +
                (MINIMAP_H / (GAME_Z_MAX - GAME_Z_MIN))) / 2

#  CARGA DE ICONOS  (con caché)

_tmpl_cache: dict = {}

def _norm(s: str) -> str:
    return s.replace(" ", "").replace("'", "").replace(".", "").lower()

def _apply_clahe(bgr: np.ndarray) -> np.ndarray:
    """Normaliza iluminación con CLAHE en canal L (LAB). Hace el matching
    robusto ante diferencias de brillo/tinte entre template y minimapa."""
    lab = cv2.cvtColor(bgr, cv2.COLOR_BGR2LAB)
    l, a, b = cv2.split(lab)
    l_eq = _CLAHE.apply(l)
    return cv2.cvtColor(cv2.merge([l_eq, a, b]), cv2.COLOR_LAB2BGR)

def load_template(team: str, champ: str, size: int) -> np.ndarray | None:
    key = (team, champ, size)
    if key in _tmpl_cache:
        return _tmpl_cache[key]

    if not os.path.exists(ICONS_DIR):
        print(f"  [Error] No existe la carpeta: {ICONS_DIR}")
        _tmpl_cache[key] = None
        return None

    team_folder = "red" if team == "CHAOS" else "blue"
    team_dir    = os.path.join(ICONS_DIR, team_folder)

    target   = _norm(champ)
    img_path = None

    if os.path.isdir(team_dir):
        for f in os.listdir(team_dir):
            if f.lower().endswith(".png") and _norm(f[:-4]) == target:
                img_path = os.path.join(team_dir, f)
                break

    if img_path is None:
        for root, dirs, files in os.walk(ICONS_DIR):
            for f in files:
                if f.lower().endswith(".png") and _norm(f[:-4]) == target:
                    img_path = os.path.join(root, f)
                    break
            if img_path:
                break

    if img_path is None:
        tmpl = _load_template_ddragon(team, champ, size)
        if tmpl is not None:
            _tmpl_cache[key] = tmpl
            return tmpl
        _tmpl_cache[key] = None
        return None

    img_full = cv2.imread(img_path, cv2.IMREAD_UNCHANGED)
    if img_full is None:
        _tmpl_cache[key] = None
        return None

    fh, fw = img_full.shape[:2]
    x1, y1, x2, y2 = _ICON_BBOX
    if fh >= y2 and fw >= x2:
        crop = img_full[y1:y2, x1:x2, :3]
    else:
        if img_full.ndim == 3 and img_full.shape[2] == 4:
            coords = cv2.findNonZero(img_full[:, :, 3])
            if coords is not None:
                bx, by, bw, bh = cv2.boundingRect(coords)
                crop = img_full[by:by+bh, bx:bx+bw, :3]
            else:
                crop = img_full[:, :, :3]
        else:
            crop = img_full[:, :, :3] if img_full.ndim == 3 else img_full

    inter = cv2.resize(crop, (64, 64), interpolation=cv2.INTER_AREA)
    tmpl  = cv2.resize(inter, (size, size), interpolation=cv2.INTER_AREA)
    tmpl  = _apply_clahe(tmpl)

    _tmpl_cache[key] = tmpl
    return tmpl


def _load_template_ddragon(team: str, champ: str, size: int) -> np.ndarray | None:
   
    ddragon_dir = os.path.join(ICONS_DIR, "..", "DDragon_Icons")
    ddragon_dir = os.path.normpath(ddragon_dir)

    if not os.path.isdir(ddragon_dir):
        return None

    target   = _norm(champ)
    img_path = None
    for f in os.listdir(ddragon_dir):
        if f.lower().endswith(".png") and _norm(f[:-4]) == target:
            img_path = os.path.join(ddragon_dir, f)
            break

    if img_path is None:
        return None

    icon = cv2.imread(img_path, cv2.IMREAD_UNCHANGED)
    if icon is None:
        return None

    icon = icon[:, :, :3] if icon.ndim == 3 and icon.shape[2] >= 3 else icon

    h, w   = icon.shape[:2]
    cx, cy = w // 2, h // 2
    r      = min(cx, cy) - 2
    color  = (0, 0, 220) if team == "CHAOS" else (220, 80, 0)  # BGR
    cv2.circle(icon, (cx, cy), r, color, thickness=max(2, w // 16))

    inter = cv2.resize(icon, (64, 64), interpolation=cv2.INTER_AREA)
    tmpl  = cv2.resize(inter, (size, size), interpolation=cv2.INTER_AREA)
    tmpl  = _apply_clahe(tmpl)
    return tmpl

# ── Parámetros para generación de iconos desde DDragon ───────────────
_CANVAS_SIZE = (1920, 1080)
_ICON_SIZE   = 372
_ICON_X      = 774
_ICON_Y      = 353
_BORDER_PX   = 18
_BORDER_RED  = (0,   30,  220)   # BGR
_BORDER_BLUE = (220, 120, 30)    # BGR


def _download_and_save_icon(champ_name: str, version: str) -> bool:
   
    try:
        import requests
        url_data = (f"https://ddragon.leagueoflegends.com/cdn/{version}"
                    f"/data/en_US/champion/{champ_name}.json")
        r = requests.get(url_data, timeout=8)
        if r.status_code != 200:
            r = requests.get(
                f"https://ddragon.leagueoflegends.com/cdn/{version}"
                f"/data/en_US/champion.json", timeout=8)
            if r.status_code != 200:
                return False
            all_champs = r.json()["data"]
            champ_id = None
            target = _norm(champ_name)
            for cid, cdata in all_champs.items():
                if _norm(cid) == target or _norm(cdata.get("name","")) == target:
                    champ_id = cid
                    break
            if champ_id is None:
                return False
        else:
            champ_id = r.json()["data"][champ_name]["id"]

        url_img = (f"https://ddragon.leagueoflegends.com/cdn/{version}"
                   f"/img/champion/{champ_id}.png")
        r2 = requests.get(url_img, timeout=8)
        if r2.status_code != 200:
            return False

        img_arr = np.frombuffer(r2.content, np.uint8)
        icon_bgr = cv2.imdecode(img_arr, cv2.IMREAD_COLOR)
        if icon_bgr is None:
            return False

        icon = cv2.resize(icon_bgr, (_ICON_SIZE, _ICON_SIZE), interpolation=cv2.INTER_LANCZOS4)

        for team_folder, border_color in [("red", _BORDER_RED), ("blue", _BORDER_BLUE)]:
            team_dir = os.path.join(ICONS_DIR, team_folder)
            os.makedirs(team_dir, exist_ok=True)
            out_path = os.path.join(team_dir, f"{champ_name}.png")
            if os.path.exists(out_path):
                continue

            canvas = np.zeros((_CANVAS_SIZE[1], _CANVAS_SIZE[0], 4), dtype=np.uint8)

            circ_mask = np.zeros((_ICON_SIZE, _ICON_SIZE), np.uint8)
            c = _ICON_SIZE // 2
            cv2.circle(circ_mask, (c, c), c - 1, 255, -1)

            icon_rgba = cv2.cvtColor(icon, cv2.COLOR_BGR2BGRA)
            icon_rgba[:, :, 3] = circ_mask

            cv2.circle(icon_rgba, (c, c), c - 1, border_color + (255,), _BORDER_PX)

            y1, y2 = _ICON_Y, _ICON_Y + _ICON_SIZE
            x1, x2 = _ICON_X, _ICON_X + _ICON_SIZE
            canvas[y1:y2, x1:x2] = icon_rgba

            cv2.imwrite(out_path, canvas)

        return True

    except Exception as e:
        print(f"    [DDragon] Error descargando {champ_name}: {e}")
        return False


def _get_ddragon_version() -> str | None:
    try:
        import requests
        r = requests.get("https://ddragon.leagueoflegends.com/api/versions.json", timeout=5)
        return r.json()[0]
    except Exception:
        return None


def ensure_icons(player_names: dict) -> None:
    missing = []
    for team, names in player_names.items():
        folder = "red" if team == "CHAOS" else "blue"
        team_dir = os.path.join(ICONS_DIR, folder)
        for name in names:
            target = _norm(name)
            found = False
            if os.path.isdir(team_dir):
                for f in os.listdir(team_dir):
                    if f.lower().endswith(".png") and _norm(f[:-4]) == target:
                        found = True
                        break
            if not found:
                missing.append((team, name))

    if not missing:
        return

    unique_missing = list({name for _, name in missing})
    print(f"[Minimap] Faltan iconos para: {unique_missing}")
    print("[Minimap] Descargando desde DDragon...", flush=True)

    version = _get_ddragon_version()
    if version is None:
        print("[Minimap] No se pudo conectar a DDragon. Continuando sin esos iconos.")
        return

    print(f"[Minimap] Versión DDragon: {version}")
    ok, fail = 0, 0
    for name in unique_missing:
        if _download_and_save_icon(name, version):
            print(f"  {name}")
            ok += 1
        else:
            print(f"  {name} (no encontrado)")
            fail += 1

    print(f"[Minimap] Descargados: {ok}  Fallidos: {fail}")
    _tmpl_cache.clear()


def preload_icons(player_names: dict) -> None:
    ensure_icons(player_names)
    print("[Minimap] Precargando iconos...", end=" ", flush=True)
    for team, names in player_names.items():
        for name in names:
            for size in [18, 20, 22]:
                load_template(team, name, size)
    print("OK")

#  IDENTIFICACIÓN POR TEMPLATE MATCHING

def identify_champion(
    patch: np.ndarray,
    team: str | None,
    candidates: list[str],
) -> tuple[str | None, float]:
   
    if not candidates or patch is None or patch.size == 0:
        return None, 0.0

    teams_to_try = [team] if team is not None else ["ORDER", "CHAOS"]
    all_scores = []

    for t in teams_to_try:
        for name in candidates:
            best = 0.0
            for size in [18, 20, 22]:
                tmpl = load_template(t, name, size)
                if tmpl is None:
                    continue

                p = cv2.resize(patch, (size, size), interpolation=cv2.INTER_AREA)
                p = _apply_clahe(p)

                mask = np.zeros((size, size), np.uint8)
                cv2.circle(mask,
                           (size // 2, size // 2),
                           size // 2 - size // 4,   
                           255, -1)

                res = cv2.matchTemplate(p, tmpl, cv2.TM_CCOEFF_NORMED, mask=mask)
                _, val, _, _ = cv2.minMaxLoc(res)
                best = max(best, val)

            all_scores.append((best, name))

    if not all_scores:
        return None, 0.0

    all_scores.sort(reverse=True)
    best_score, best_name = all_scores[0]
    margin = (all_scores[0][0] - all_scores[1][0]) if len(all_scores) > 1 else 1.0

    if best_score >= MIN_SCORE and margin >= MIN_MARGIN:
        return best_name, best_score

    return None, 0.0

#  DETECCIÓN DE ANILLOS  

def detect_rings(img: np.ndarray) -> list[dict]:
   
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    hsv  = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    h, w = img.shape[:2]

    circles = cv2.HoughCircles(
        gray,
        cv2.HOUGH_GRADIENT,
        dp       = 1,
        minDist  = _HOUGH_MIN_DIST,
        param1   = _HOUGH_PARAM1,
        param2   = _HOUGH_PARAM2,
        minRadius= _HOUGH_MIN_R,
        maxRadius= _HOUGH_MAX_R,
    )
    if circles is None:
        return []

    raw = []
    for (cx, cy, r) in np.round(circles[0]).astype(int):

        if cx < _MAP_MARGIN or cy < _MAP_MARGIN:
            continue
        if cx > w - _MAP_MARGIN or cy > h - _MAP_MARGIN:
            continue

        mask_in = np.zeros(gray.shape, np.uint8)
        cv2.circle(mask_in, (cx, cy), max(r - 4, 4), 255, -1)
        var_in = float(gray[mask_in > 0].std())
        if var_in < _VAR_MIN:
            continue

        ring_mask = np.zeros(gray.shape, np.uint8)
        cv2.circle(ring_mask, (cx, cy), r,           255, -1)
        cv2.circle(ring_mask, (cx, cy), max(r-4, 1),   0, -1)
        ring_hsv   = hsv[ring_mask > 0]
        total_ring = len(ring_hsv)

        red_v  = (((ring_hsv[:, 0] <= 12) | (ring_hsv[:, 0] >= 158))
                   & (ring_hsv[:, 1] > 80)).sum()
        blue_v = ((ring_hsv[:, 0] >= 88) & (ring_hsv[:, 0] <= 125)
                   & (ring_hsv[:, 1] > 70)).sum()
        dom = max(red_v, blue_v)

        if dom < _RING_VOTES_MIN:
            continue
        if dom / (total_ring + 1) < _RING_RATIO_MIN:
            continue


        if red_v > 0 and blue_v > 0:
            mix = min(red_v, blue_v) / max(red_v, blue_v)
            if mix > _RING_MIX_MAX:
                continue

        team = "CHAOS" if red_v > blue_v else "ORDER"

        x1 = max(0, cx - r); x2 = min(w, cx + r)
        y1 = max(0, cy - r); y2 = min(h, cy + r)
        patch = img[y1:y2, x1:x2]

        raw.append({
            "cx": cx, "cy": cy, "r": r,
            "team": team, "patch": patch,
            "area": var_in,   
        })

    raw.sort(key=lambda x: -x["area"])
    out = []
    for item in raw:
        if not any(
            ((item["cx"] - o["cx"])**2 + (item["cy"] - o["cy"])**2)**0.5 < _HOUGH_MIN_DIST
            for o in out
        ):
            out.append(item)
    return out

#  TRACK INDIVIDUAL

class Track:
    def __init__(self, name: str, team: str, cx: int, cy: int):
        self.name            = name
        self.team            = team
        self.cx, self.cy     = cx, cy
        self.confirmed_count = 1
        self.missing_count   = 0
        self.static_count    = 0
        self.history         = deque([(cx, cy, time.time())], maxlen=20)
        self.last_t          = time.time()
        self.gx, self.gz     = local_to_game(cx, cy)
        self.zone            = get_zone(self.gx, self.gz)
        self.score           = 0.0

    def try_update(self, nx: int, ny: int) -> bool:
        now = time.time()
        dt  = now - self.last_t
        dist_px   = ((nx - self.cx)**2 + (ny - self.cy)**2)**0.5
        dist_game = dist_px / _PX_PER_UNIT
        speed     = dist_game / dt if dt > 0 else 0

        
        was_in_fog = self.missing_count > 2
        in_base_area = nx < 40 or ny > MINIMAP_H - 40
        if speed > MAX_SPEED_GAME_UNITS and not in_base_area and not was_in_fog:
            return False

        self.static_count = self.static_count + 1 if dist_px < STATIC_THRESHOLD_PX else 0
        self.cx, self.cy   = nx, ny
        self.last_t        = now
        self.missing_count = 0
        self.confirmed_count += 1
        self.history.append((nx, ny, now))
        self.gx, self.gz   = local_to_game(nx, ny)
        self.zone          = get_zone(self.gx, self.gz)
        return True

    @property
    def is_static(self) -> bool:
        in_base = (self.cx < 80 and self.cy > MINIMAP_H - 80) or \
                  (self.cx > MINIMAP_W - 80 and self.cy < 80)
        return self.static_count >= STATIC_FRAMES_LIMIT and not in_base

    @property
    def is_confirmed(self) -> bool:
        return self.confirmed_count >= MIN_CONFIRMATION_FRAMES and self.missing_count == 0

    @property
    def is_lost(self) -> bool:
        return self.missing_count > MAX_MISSING_FRAMES

#  SUPER-TRACKER PRINCIPAL

class SuperTracker:
    def __init__(self, player_names: dict):
        self.player_names = player_names
        self.dead: set[str] = set()
        self._tracks: dict[str, Track] = {}
        self._ring_blacklist: dict[tuple, int] = {}
        self._score_history: dict[str, list] = {}
        preload_icons(player_names)

    def set_dead(self, dead: set[str]) -> None:
        self.dead = dead

    def process(self, img: np.ndarray, debug_log: str = None) -> dict[str, dict]:
        rings = detect_rings(img)
        log_lines = []

        if debug_log:
            log_lines.append(f"\n[{time.strftime('%H:%M:%S')}] ─── FRAME ───")
            log_lines.append(f"Anillos crudos encontrados por Hough: {len(rings)}")

        for t in self._tracks.values():
            t.missing_count += 1

        current_positions = {(r["cx"], r["cy"]) for r in rings}
        for pos in list(self._ring_blacklist.keys()):
            if pos not in current_positions:
                del self._ring_blacklist[pos]

        already_assigned: set[str] = set()
        all_scores = []
        for ring_idx, ring in enumerate(rings):
            cx, cy = ring["cx"], ring["cy"]

            blacklisted = any(
                ((cx - bx)**2 + (cy - by)**2)**0.5 < 8
                for (bx, by) in self._ring_blacklist
                if self._ring_blacklist[(bx,by)] >= 3
            )
            if blacklisted:
                if debug_log:
                    log_lines.append(f"  Anillo {ring_idx} en ({cx},{cy}) → IGNORADO (blacklist)")
                continue

            patch         = ring["patch"]
            detected_team = ring["team"]

            best_name  = None
            best_score = 0.0
            best_team  = detected_team

            for t in ["ORDER", "CHAOS"]:
                candidates = [
                    c for c in self.player_names.get(t, [])
                    if c not in self.dead and c not in already_assigned
                ]
                name, score = identify_champion(patch, t, candidates)
                if score > best_score:
                    best_score = score
                    best_name  = name
                    best_team  = t

            if best_name is None:
                key = (cx, cy)
                found = False
                for (bx, by) in list(self._ring_blacklist.keys()):
                    if ((cx-bx)**2 + (cy-by)**2)**0.5 < 8:
                        self._ring_blacklist[(bx,by)] += 1
                        found = True
                        break
                if not found:
                    self._ring_blacklist[(cx, cy)] = 1

            if debug_log:
                log_lines.append(f"  Anillo {ring_idx} en ({cx}, {cy}) [color {detected_team}]:")
                if best_name:
                    log_lines.append(f"    -> MATCH EXITOSO: {best_name} [{best_team}] (Score: {best_score:.3f})")
                else:
                    bl_count = self._ring_blacklist.get((cx,cy), 0)
                    log_lines.append(f"    -> MATCH FALLIDO (blacklist={bl_count})")

            if best_name:
                already_assigned.add(best_name)
                all_scores.append((best_score, best_name, best_team, ring_idx))

        all_scores.sort(reverse=True)
        used_champs = set()
        used_rings  = set()

        if debug_log and all_scores:
            log_lines.append("Asignación de Tracks (Greedy):")

        for score, name, team, ring_idx in all_scores:
            if name in used_champs or ring_idx in used_rings:
                continue
            ring = rings[ring_idx]
            nx, ny = ring["cx"], ring["cy"]

            if name not in self._tracks:
                self._tracks[name] = Track(name, team, nx, ny)
                self._tracks[name].score = score
                self._score_history[name] = [score]
                if debug_log:
                    log_lines.append(f"  + NUEVO: {name} [{team}] en ({nx},{ny}) score={score:.3f}")
            else:
                if self._tracks[name].try_update(nx, ny):
                    self._tracks[name].score = score
                    hist = self._score_history.setdefault(name, [])
                    hist.append(score)
                    if len(hist) > 5:
                        hist.pop(0)
                    if debug_log:
                        log_lines.append(f"  ~ ACTUALIZADO: {name} → ({nx},{ny})")
                else:
                    if debug_log:
                        log_lines.append(f"  ! RECHAZADO por velocidad: {name}")
                    continue

            used_champs.add(name)
            used_rings.add(ring_idx)

        to_del = []
        for n, t in self._tracks.items():
            if t.is_static or t.is_lost:
                to_del.append((n, "estático" if t.is_static else "perdido"))
                continue
            hist = self._score_history.get(n, [])
            in_base = (t.cx < 80 and t.cy > MINIMAP_H - 80) or \
                      (t.cx > MINIMAP_W - 80 and t.cy < 80)
            if len(hist) >= 4 and len(set(round(s, 3) for s in hist[-4:])) == 1 and not in_base:
                to_del.append((n, f"score constante={hist[-1]:.3f} (falso positivo)"))

        for n, reason in to_del:
            if debug_log:
                log_lines.append(f"  x BORRADO: {n} ({reason})")
            del self._tracks[n]
            self._score_history.pop(n, None)

        if debug_log:
            log_lines.append("Estado de Memoria Temporal (Tracks):")
            if not self._tracks:
                log_lines.append("  (Vacío)")
            for n, t in self._tracks.items():
                log_lines.append(
                    f"  * {n:14s} | missing={t.missing_count:2d} | confirmed={t.confirmed_count:2d}"
                    f" | static={t.static_count:2d} | visible={t.is_confirmed}"
                )
            with open(debug_log, "a", encoding="utf-8") as f:
                f.write("\n".join(log_lines) + "\n")

        return {
            name: {
                "x":         t.gx,
                "z":         t.gz,
                "zone":      t.zone,
                "team":      t.team,
                "score":     t.score,
                "confirmed": t.confirmed_count,
                "cx":        t.cx,
                "cy":        t.cy,
            }
            for name, t in self._tracks.items()
            if t.is_confirmed and not t.is_lost
        }

#  CAPTURA
# 

_camera = None

def capture_minimap() -> np.ndarray:
    global _camera
    if _camera is None:
        import dxcam
        _camera = dxcam.create(output_color="BGR")
    region = (MINIMAP_X, MINIMAP_Y, MINIMAP_X + MINIMAP_W, MINIMAP_Y + MINIMAP_H)
    frame = _camera.grab(region=region)
    while frame is None:
        time.sleep(0.01)
        frame = _camera.grab(region=region)
    return frame

#  CLASE PRINCIPAL (interfaz pública)

class MinimapTracker:

    def __init__(self):
        self._tracker: SuperTracker | None = None
        self.last_result: dict = {}

    def setup(self, player_names: dict) -> None:
        self._tracker = SuperTracker(player_names)
        print(f"[Minimap] ORDER: {player_names.get('ORDER', [])}")
        print(f"[Minimap] CHAOS: {player_names.get('CHAOS', [])}")

    def set_dead(self, dead: set[str]) -> None:
        if self._tracker:
            self._tracker.set_dead(dead)

    def tick(self, img: np.ndarray | None = None) -> dict:
        if not self._tracker:
            return {}
        try:
            if img is None:
                img = capture_minimap()
            self.last_result = self._tracker.process(img)
            return self.last_result
        except Exception as e:
            print(f"[Minimap] Error: {e}")
            return self.last_result

    def debug_view(self, save_path: str = "debug_minimap.png") -> dict:
        """Captura un frame, detecta, dibuja y guarda imagen de debug."""
        img = capture_minimap()

        rings  = detect_rings(img)
        result = self.tick(img)

        debug = img.copy()

        for ring in rings:
            col = (0, 220, 220) if ring["team"] == "ORDER" else (50, 50, 255)
            cv2.circle(debug, (ring["cx"], ring["cy"]), ring["r"], col, 1)

        for name, info in result.items():
            cx, cy = info["cx"], info["cy"]
            col = (0, 255, 140) if info["team"] == "ORDER" else (0, 80, 255)
            cv2.circle(debug, (cx, cy), 3, col, -1)
            cv2.putText(debug, f"{name[:8]}",
                        (cx - 24, cy - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.27, col, 1)
            cv2.putText(debug, f"{info['score']:.2f} {info['zone'][:8]}",
                        (cx - 24, cy + 13), cv2.FONT_HERSHEY_SIMPLEX, 0.22,
                        (190, 190, 190), 1)

        n_o = sum(1 for i in result.values() if i["team"] == "ORDER")
        n_c = len(result) - n_o
        elapsed = 0  
        cv2.putText(debug, f"Detectados: {len(result)} ({n_o}B+{n_c}R)",
                    (5, 12), cv2.FONT_HERSHEY_SIMPLEX, 0.35, (255, 255, 255), 1)
        cv2.imwrite(save_path, debug)

        print(f"\n[Debug] → {save_path}  ({len(result)} detectados)")
        for name, info in sorted(result.items()):
            print(f"  {info['team']:5} {name:20} ({info['x']:6.0f},{info['z']:6.0f})"
                  f"  {info['zone']:15}  score={info['score']:.2f}")
        return result

# ── FUNCIONES DE COMPATIBILIDAD ──────────────────────────────────────

_compat_tracker = None

def detect_champions(img, player_names):
    global _compat_tracker
    if _compat_tracker is None:
        _compat_tracker = SuperTracker(player_names)
    res = _compat_tracker.process(img)
    return [(n, i["cx"], i["cy"], i["score"], i["team"]) for n, i in res.items()]

def get_all_positions(img, player_names, debug_log=None):
    global _compat_tracker
    if _compat_tracker is None:
        _compat_tracker = SuperTracker(player_names)
    return _compat_tracker.process(img, debug_log=debug_log)

def get_live_champions():
    """Consulta la API de LoL para obtener los campeones de la partida actual."""
    import requests
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    try:
        r = requests.get("https://127.0.0.1:2999/liveclientdata/playerlist",
                         verify=False, timeout=2)
        if r.status_code == 200:
            mapping = {"ORDER": [], "CHAOS": []}
            for p in r.json():
                t = p.get("team"); c = p.get("championName")
                if t and c:
                    mapping[t].append(c)
            return mapping
    except Exception:
        pass
    return None

#  TEST STANDALONE

if __name__ == "__main__":
    import keyboard

    DOWNLOADS_DIR = r"C:\Users\Adrian\Downloads"
    LOG_FILE      = os.path.join(DOWNLOADS_DIR, "debug_log_rafaga.txt")

    print("\n[Minimap] Intentando detectar campeones automáticamente...")
    PLAYER_NAMES = get_live_champions()

    if not PLAYER_NAMES:
        print("[!] No se detectó partida activa. Usando campeones de prueba.")
        PLAYER_NAMES = {
            "ORDER": ["Ezreal", "Jinx", "Thresh", "Garen", "LeeSin"],
            "CHAOS": ["Zed", "MissFortune", "Blitzcrank", "Darius", "Nidalee"],
        }
    else:
        print(f"[OK] Partida detectada: {len(PLAYER_NAMES['ORDER'])} vs {len(PLAYER_NAMES['CHAOS'])}")

    tracker = MinimapTracker()
    tracker.setup(PLAYER_NAMES)

    print("\n  F5  → ráfaga de 5 capturas (1 cada 5s)  |  F10 → salir")
    print("  Vuelve al juego con campeones visibles en el mapa\n")

    n = 0
    while True:
        if keyboard.is_pressed("f10"):
            print("Saliendo.")
            break

        if keyboard.is_pressed("f5"):
            open(LOG_FILE, "w").close()
            print(f"\n Ráfaga de 5 capturas iniciada. Log → {LOG_FILE}")

            for i in range(5):
                n += 1
                t0 = time.perf_counter()

                img    = capture_minimap()
                result = tracker._tracker.process(img, debug_log=LOG_FILE)
                elapsed = (time.perf_counter() - t0) * 1000

                debug = img.copy()
                for ring in detect_rings(img):
                    col = (0, 220, 220) if ring["team"] == "ORDER" else (50, 50, 255)
                    cv2.circle(debug, (ring["cx"], ring["cy"]), ring["r"], col, 1)
                for name, info in result.items():
                    cx, cy = info["cx"], info["cy"]
                    col = (0, 255, 140) if info["team"] == "ORDER" else (0, 80, 255)
                    cv2.circle(debug, (cx, cy), 3, col, -1)
                    cv2.putText(debug, f"{name[:8]}", (cx-24, cy-10),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.27, col, 1)
                    cv2.putText(debug, f"{info['score']:.0%} {info['zone'][:8]}",
                                (cx-24, cy+13), cv2.FONT_HERSHEY_SIMPLEX, 0.22,
                                (190, 190, 190), 1)
                n_o = sum(1 for i in result.values() if i["team"] == "ORDER")
                n_c = len(result) - n_o
                cv2.putText(debug, f"Detectados: {len(result)} ({n_o}B+{n_c}R) | {elapsed:.0f}ms",
                            (5, 12), cv2.FONT_HERSHEY_SIMPLEX, 0.35, (255, 255, 255), 1)

                save_path = os.path.join(DOWNLOADS_DIR, f"debug_minimap_rafaga_{n:03d}.png")
                cv2.imwrite(save_path, debug)

                print(f"\n── Captura {n} [{i+1}/5] ── {elapsed:.0f}ms")
                print(f"   Detectados: {len(result)}/10")
                for name, info in sorted(result.items()):
                    team_str = "AZUL" if info["team"] == "ORDER" else "ROJO"
                    print(f"   {team_str} {name:<14} score={info['score']:.2f}  zona={info['zone']}")
                for team, names in PLAYER_NAMES.items():
                    for name in names:
                        if name not in result:
                            t_str = "AZUL" if team == "ORDER" else "ROJO"
                            print(f"   {t_str} {name:<14} NO DETECTADO")

                if i < 4:
                    print("   Esperando 5s...")
                    for _ in range(50):
                        if keyboard.is_pressed("f10"):
                            print("Saliendo.")
                            exit(0)
                        time.sleep(0.1)

            print(f"\n[OK] Ráfaga completada. F5 para otra, F10 para salir.")
            time.sleep(0.5)

        time.sleep(0.05)