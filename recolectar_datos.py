import time, os, json, sys
import cv2, keyboard, requests, urllib3
urllib3.disable_warnings()

sys.path.insert(0, r"C:\Users\Adrian\Downloads")
from minimap_tracker import capture_minimap, MINIMAP_W, MINIMAP_H
import detector_v2

DATASET = r"C:\Users\Adrian\Downloads\dataset"
LABELED = os.path.join(DATASET, "labeled")
INTERVALO = 1.0   
CROP_R, SAVE = 14, 32
os.makedirs(DATASET, exist_ok=True)

AUTO = None
AUTO_TEAM = "ENEMY" if "--enemy" in sys.argv else "ALLY"   
if "--auto" in sys.argv:
    i = sys.argv.index("--auto")
    if i+1 < len(sys.argv):
        AUTO = sys.argv[i+1]
        os.makedirs(os.path.join(LABELED, AUTO), exist_ok=True)

API = "https://127.0.0.1:2999/liveclientdata"

def get_roster():
    try:
        pl = requests.get(f"{API}/playerlist", timeout=2, verify=False).json()
        roster = {"ORDER": [], "CHAOS": []}
        for p in pl:
            name = p.get("championName", "").replace(" ", "").replace("'", "")
            team = p.get("team")
            if team in roster and name:
                roster[team].append(name)
        try:
            me = requests.get(f"{API}/activeplayername", timeout=2, verify=False).json()
            myteam = None
            for p in pl:
                if p.get("summonerName") == me or p.get("riotId","").startswith(str(me)):
                    myteam = p.get("team"); break
        except Exception:
            myteam = None
        return {"roster": roster, "my_team": myteam}
    except Exception as e:
        print(f"  [!] No se pudo leer la API: {e}")
        return None

print("="*55)
print("  RECOLECTOR DE DATOS")
if AUTO:
    print(f"  MODO AUTO -> etiqueta todo como: {AUTO}")
    print(f"  (asegúrate de que SOLO {AUTO} se ve en el minimapa)")
    print(f"  Recortes -> {LABELED}\\{AUTO}")
else:
    print(f"  Frames cada {INTERVALO}s -> {DATASET}\\game_<fecha>")
print("  F8 = grabar | F9 = parar | F10 = salir")
print("="*55)

grabando = False
sess_dir = None
n = 0          
ncrops = 0    
last = 0

while True:
    if keyboard.is_pressed("f10"):
        print("\nSaliendo.")
        break

    if keyboard.is_pressed("f8") and not grabando:
        info = get_roster()
        if info is None or not info["roster"]["ORDER"]:
            if not AUTO:   
                print("  [!] No hay partida activa o API no disponible. Reintenta en partida.")
                time.sleep(1)
                continue
        stamp = time.strftime("%Y%m%d_%H%M%S")
        sess_dir = os.path.join(DATASET, f"game_{stamp}")
        os.makedirs(os.path.join(sess_dir, "frames"), exist_ok=True)
        if info:
            with open(os.path.join(sess_dir, "roster.json"), "w", encoding="utf-8") as f:
                json.dump(info, f, indent=2, ensure_ascii=False)
        grabando = True; n = 0; ncrops = 0
        print(f"\n[REC] {sess_dir}")
        if AUTO:
            print(f"  Auto-etiquetando como: {AUTO}  (MUEVE al campeón sin parar)")
        elif info:
            print(f"  ORDER: {info['roster']['ORDER']}")
            print(f"  CHAOS: {info['roster']['CHAOS']}")
        time.sleep(0.5)

    if keyboard.is_pressed("f9") and grabando:
        grabando = False
        if AUTO:
            print(f"[STOP] {ncrops} recortes de {AUTO} guardados en {LABELED}\\{AUTO}")
        else:
            print(f"[STOP] {n} frames guardados en {sess_dir}")
        time.sleep(0.5)

    if grabando and time.time() - last >= INTERVALO:
        try:
            img = capture_minimap()
            n += 1
            cv2.imwrite(os.path.join(sess_dir, "frames", f"f_{n:05d}.png"), img)
            if AUTO:
                for det in detector_v2.detectar_campeones(img, top_crop=0):
                    if det["team"] != AUTO_TEAM:
                        continue
                    cx, cy = det["cx"], det["cy"]
                    crop = img[max(0,cy-CROP_R):cy+CROP_R, max(0,cx-CROP_R):cx+CROP_R]
                    if crop.shape[0] < 6 or crop.shape[1] < 6:
                        continue
                    crop = cv2.resize(crop, (SAVE, SAVE), interpolation=cv2.INTER_AREA)
                    ncrops += 1
                    cv2.imwrite(os.path.join(LABELED, AUTO, f"{AUTO}_{stamp}_{n:05d}_{cx}_{cy}.png"), crop)
                if n % 5 == 0:
                    print(f"  ...{n} frames, {ncrops} recortes de {AUTO} guardados")
            elif n % 10 == 0:
                print(f"  ...{n} frames")
        except Exception as e:
            print(f"  [!] Error capturando: {e}")
        last = time.time()

    time.sleep(0.03)
