import cv2, numpy as np, glob, os

BLUE_H = (90, 112);  BLUE_S = 55;   BLUE_V = 90     
RED_H_LO = 7; RED_H_HI = 173;       RED_S = 70; RED_V = 90   

# ── Geometría del icono (px, minimapa ~345x360) ──────────────────────
R_INNER = 5     
R_RING  = 11    
NMS_DIST = 7    

# ── Umbrales de discriminación (calibrables) ─────────────────────────
INNER_VAR_MIN = 8.0     
RING_FRAC_MIN = 0.04    
MIN_BLOB_AREA = 10


def _masks(hsv):
    H, S, V = hsv[:, :, 0].astype(int), hsv[:, :, 1].astype(int), hsv[:, :, 2].astype(int)
    blue = ((H >= BLUE_H[0]) & (H <= BLUE_H[1]) & (S > BLUE_S) & (V > BLUE_V))
    red  = (((H <= RED_H_LO) | (H >= RED_H_HI)) & (S > RED_S) & (V > RED_V))
    return blue.astype(np.uint8) * 255, red.astype(np.uint8) * 255


def _disk(shape, cx, cy, r):
    m = np.zeros(shape, np.uint8)
    cv2.circle(m, (cx, cy), r, 255, -1)
    return m


def _candidates_from_mask(mask, gray, color_mask, team, top_crop):
    H, W = gray.shape
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    grouped = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, k, iterations=2)
    grouped = cv2.dilate(grouped, k, iterations=1)
    n, lbl, stats, cent = cv2.connectedComponentsWithStats(grouped, connectivity=8)

    out = []
    for i in range(1, n):
        x, y, w, h, area = stats[i]
        if area < MIN_BLOB_AREA:
            continue
        if not (8 <= w <= 34 and 8 <= h <= 34):
            continue
        cx, cy = int(round(cent[i][0])), int(round(cent[i][1]))
        if cy < top_crop + R_RING or cy > H - 3 or cx < 3 or cx > W - 3:
            continue

        inner_m = _disk(gray.shape, cx, cy, R_INNER)
        ring_m  = cv2.subtract(_disk(gray.shape, cx, cy, R_RING + 1),
                               _disk(gray.shape, cx, cy, R_INNER + 1))

        inner_var = float(gray[inner_m > 0].std()) if inner_m.any() else 0.0
        ring_px   = int((ring_m > 0).sum())
        ring_col  = int((color_mask[ring_m > 0] > 0).sum())
        ring_frac = ring_col / (ring_px + 1)

        if inner_var >= INNER_VAR_MIN and ring_frac >= RING_FRAC_MIN:
            out.append(dict(cx=cx, cy=cy, r=R_RING, team=team,
                            var=round(inner_var, 1), ring=round(ring_frac, 2),
                            ringpx=ring_col))
    return out


# ── Quita-estructuras: las torres/wards son sprites FIJOS (escudo con nº) ──
import os as _os, json
_BASE   = _os.path.dirname(_os.path.abspath(__file__))
_ASSETS = _os.path.join(_BASE, "_assets")
_SHIELD_PATH = _os.path.join(_ASSETS, "shield_tmpl.png")
_SHIELD = cv2.imread(_SHIELD_PATH, cv2.IMREAD_GRAYSCALE) if _os.path.exists(_SHIELD_PATH) else None
SHIELD_THRESH = 0.28   

def _shield_score(gray, cx, cy):
    if _SHIELD is None:
        return 0.0
    R = 11
    c = gray[max(0,cy-R):cy+R, max(0,cx-R):cx+R]
    if c.shape[0] < 6 or c.shape[1] < 6:
        return 0.0
    c = cv2.equalizeHist(cv2.resize(c, (22,22), interpolation=cv2.INTER_AREA))
    return float(cv2.matchTemplate(c, _SHIELD, cv2.TM_CCOEFF_NORMED).max())


# ── Mapa de POSICIONES de estructuras (torres/base) — robusto entre partidas ──
_ESTRUCT_PATH = _os.path.join(_ASSETS, "estructuras.json")
_ESTRUCTURAS = json.load(open(_ESTRUCT_PATH)) if _os.path.exists(_ESTRUCT_PATH) else []
ESTRUCT_DIST = 9   
def _es_estructura(cx, cy):
    return any((cx-ex)**2 + (cy-ey)**2 < ESTRUCT_DIST**2 for ex, ey in _ESTRUCTURAS)


HUEDIV_MIN = 6

def _hue_div(img, cx, cy, r=11):
    c = img[max(0,cy-r):cy+r, max(0,cx-r):cx+r]
    if c.shape[0] < 6 or c.shape[1] < 6:
        return 0
    hsv = cv2.cvtColor(cv2.resize(c, (24,24)), cv2.COLOR_BGR2HSV)
    H, S, V = hsv[:,:,0].astype(int), hsv[:,:,1], hsv[:,:,2]
    sat = (S > 60) & (V > 70)
    hh = H[sat]
    return len(np.unique(hh // 12)) if len(hh) > 5 else 0


def detectar_campeones(img, top_crop=0, debug=False, quitar_estructuras=False):
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    hsv  = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    blue, red = _masks(hsv)

    cands  = _candidates_from_mask(blue, gray, blue, "ALLY",  top_crop)
    cands += _candidates_from_mask(red,  gray, red,  "ENEMY", top_crop)

    if quitar_estructuras:
        cands = [c for c in cands if _hue_div(img, c["cx"], c["cy"]) >= HUEDIV_MIN]
        if _ESTRUCTURAS:
            cands = [c for c in cands if not _es_estructura(c["cx"], c["cy"])]

    cands.sort(key=lambda d: -d["ringpx"])
    keep = []
    for it in cands:
        if not any((it["cx"]-o["cx"])**2 + (it["cy"]-o["cy"])**2 < NMS_DIST**2 for o in keep):
            keep.append(it)

    return keep


# ════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    clean = sorted(glob.glob(_os.path.join(_BASE, "capturas_limpias", "limpia_*.png")))
    if clean:
        imgs, top = clean, 0
        print(f"Usando {len(clean)} capturas LIMPIAS\n")
    else:
        imgs = sorted(glob.glob(_os.path.join(_BASE, "debug_minimap_rafaga_*.png")))
        top = 16
        print(f"Sin capturas limpias. Usando {len(imgs)} de debug (top_crop=16)\n")

    OUT = _os.path.join(_BASE, "_diag_out")
    os.makedirs(OUT, exist_ok=True)

    for path in imgs:
        img = cv2.imread(path)
        keep = detectar_campeones(img, top_crop=top, debug=True)
        ally = sum(1 for d in keep if d["team"] == "ALLY")
        ene  = sum(1 for d in keep if d["team"] == "ENEMY")
        print(f"{os.path.basename(path)}: {len(keep)} campeones  ALI:{ally}  ENE:{ene}")

        vis = img.copy()
        for d in keep:
            col = (0, 0, 255) if d["team"] == "ENEMY" else (255, 160, 0)
            cv2.circle(vis, (d["cx"], d["cy"]), d["r"], col, 2)
            cv2.putText(vis, f"v{d['var']:.0f}", (d["cx"]-10, d["cy"]-13),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.28, col, 1)
        cv2.putText(vis, f"ALI:{ally} ENE:{ene}", (5, 12),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.34, (0,255,255), 1)
        cv2.imwrite(os.path.join(OUT, "v2_" + os.path.basename(path)), vis)
