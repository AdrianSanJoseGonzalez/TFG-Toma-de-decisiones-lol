
import cv2, numpy as np, os, sys, json, time, glob
import requests, urllib3
urllib3.disable_warnings()
import torch, torch.nn as nn
sys.path.insert(0, r"C:\Users\Adrian\Downloads")
import detector_v2
from minimap_tracker import capture_minimap

DATASET = r"C:\Users\Adrian\Downloads\dataset"
OUT = os.path.join(DATASET, "vistas"); os.makedirs(OUT, exist_ok=True)
API = "https://127.0.0.1:2999/liveclientdata"
SIZE, R, SCALE, CONF_MIN = 24, 16, 2, 0.45   

classes = json.load(open(os.path.join(DATASET, "classes.json")))
class Net(nn.Module):
    def __init__(s, n):
        super().__init__()
        s.c = nn.Sequential(
            nn.Conv2d(3,32,3,1,1), nn.BatchNorm2d(32), nn.ReLU(), nn.MaxPool2d(2),
            nn.Conv2d(32,64,3,1,1), nn.BatchNorm2d(64), nn.ReLU(), nn.MaxPool2d(2),
            nn.Conv2d(64,128,3,1,1), nn.BatchNorm2d(128), nn.ReLU(), nn.MaxPool2d(2))
        s.f = nn.Sequential(nn.Flatten(), nn.Linear(128*3*3,256), nn.ReLU(),
                            nn.Dropout(0.3), nn.Linear(256, n))
    def forward(s, x): return s.f(s.c(x))
net = Net(len(classes)); net.load_state_dict(torch.load(os.path.join(DATASET,"modelo.pt"))); net.eval()
NEG = classes.index("_neg") if "_neg" in classes else None

def get_roster_api():
    try:
        pl = requests.get(f"{API}/playerlist", timeout=2, verify=False).json()
        r = {"ORDER": [], "CHAOS": []}
        for p in pl:
            nm = p.get("championName","").replace(" ","").replace("'","")
            if p.get("team") in r and nm: r[p["team"]].append(nm)
        me = requests.get(f"{API}/activeplayername", timeout=2, verify=False).json()
        myteam = next((p["team"] for p in pl if p.get("summonerName")==me), "ORDER")
        return r, myteam
    except Exception:
        return None, "ORDER"

def idxs(names): return [classes.index(n) for n in names if n in classes]

_TTA = [(0,0), (-2,0), (2,0), (0,-2), (0,2), (-2,-2), (2,2), (-3,0), (3,0)]

def identificar(img, cx, cy, allowed):
    cand = allowed + ([NEG] if NEG is not None else [])
    if not cand: return None, 0.0
    acc = None
    for dx, dy in _TTA:
        x, y = cx + dx, cy + dy
        crop = img[max(0,y-R):y+R, max(0,x-R):x+R]
        if crop.shape[0] < 6 or crop.shape[1] < 6: continue
        c = cv2.resize(crop, (SIZE, SIZE), interpolation=cv2.INTER_AREA)
        t = torch.tensor(c.astype(np.float32).transpose(2,0,1)[None]/255.0)
        with torch.no_grad(): p = net(t).softmax(1)[0]
        acc = p if acc is None else acc + p
    if acc is None: return None, 0.0
    sub = acc[cand]; sub = sub / sub.sum()
    j = int(sub.argmax())
    return classes[cand[j]], float(sub[j])

def procesar(img, ally_i, enemy_i):
    dets = detector_v2.detectar_campeones(img, top_crop=0)
    scored = []
    for d in dets:
        allowed = ally_i if d["team"] == "ALLY" else enemy_i
        name, conf = identificar(img, d["cx"], d["cy"], allowed)
        if name is None: continue
        scored.append((name, conf, d))
    acc = sorted([(c, n, d) for (n, c, d) in scored if n and n != "_neg" and c >= CONF_MIN],
                 key=lambda x: -x[0])
    usados, final = set(), []
    for conf, name, d in acc:
        if name in usados: continue
        usados.add(name); final.append((name, conf, d["cx"], d["cy"], d["team"]))
    return final, scored

def pintar(img, items, scored=None):
    vis = cv2.resize(img, None, fx=SCALE, fy=SCALE, interpolation=cv2.INTER_NEAREST)
    if DEBUG and scored:
        for name, conf, d in scored:
            if name and name != "_neg" and conf >= CONF_MIN: continue
            cx, cy = d["cx"]*SCALE, d["cy"]*SCALE
            cv2.circle(vis, (cx, cy), 13, (160,160,160), 1)
            cv2.putText(vis, f"{name} {conf:.0%}", (cx-24, cy+24),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.35, (170,170,170), 1)
    for name, conf, cx, cy, team, fresh in items:
        X, Y = cx*SCALE, cy*SCALE
        col = (255,160,0) if team == "ALLY" else (0,0,255)
        cv2.circle(vis, (X, Y), 15, col, 2 if fresh else 1)
        cv2.putText(vis, name, (X-22, Y-18), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0,0,0), 3)
        cv2.putText(vis, name, (X-22, Y-18), cv2.FONT_HERSHEY_SIMPLEX, 0.55, col, 1)
    cv2.putText(vis, f"Detectados: {len(items)}", (5,20), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0,255,255), 2)
    return vis

_args = [a for a in sys.argv[1:] if not a.startswith("-")]
GAME = _args[0] if _args else None
INTERVAL = 3.0  
if "--cada" in sys.argv:
    try: INTERVAL = float(sys.argv[sys.argv.index("--cada") + 1])
    except Exception: pass
DEBUG = "--debug" in sys.argv 
if GAME:
    gdir = GAME if os.path.isdir(GAME) else os.path.join(DATASET, GAME)
    roster = None
    rp = os.path.join(gdir,"roster.json")
    if os.path.exists(rp):
        info = json.load(open(rp,encoding="utf-8")); roster = info.get("roster"); myteam = info.get("my_team","ORDER")
    if roster:
        ally = roster["ORDER"] if myteam=="ORDER" else roster["CHAOS"]
        enemy = roster["CHAOS"] if myteam=="ORDER" else roster["ORDER"]
    else:
        ally = enemy = [c for c in classes if c!="_neg"]
    frames = sorted(glob.glob(os.path.join(gdir,"frames","*.png")))
    sample = frames[::max(1,len(frames)//6)][:6]
    for i, fp in enumerate(sample):
        im = cv2.imread(fp)
        final, scored = procesar(im, idxs(ally), idxs(enemy))
        vis = pintar(im, [(n,c,cx,cy,t,True) for (n,c,cx,cy,t) in final], scored)
        out = os.path.join(OUT, f"vista_{i+1}.png"); cv2.imwrite(out, vis)
        print(f"  {out}")
    print(f"\nListo. Mira las imágenes en {OUT}")
else:
    import keyboard
    roster, myteam = get_roster_api()
    if roster:
        ally = roster["ORDER"] if myteam=="ORDER" else roster["CHAOS"]
        enemy = roster["CHAOS"] if myteam=="ORDER" else roster["ORDER"]
        print(f"ALIADOS {ally} | ENEMIGOS {enemy}")
    else:
        ally = enemy = [c for c in classes if c!="_neg"]
    ally_i, enemy_i = idxs(ally), idxs(enemy)
    print(f"\nEN VIVO: ventana flotante + imagen cada {INTERVAL:.0f}s en {OUT}\\ultima.png")
    print("Cierra con 'q' en la ventana o F10.\n")
    cv2.namedWindow("en vivo", cv2.WINDOW_NORMAL)
    try:  
        cv2.setWindowProperty("en vivo", cv2.WND_PROP_TOPMOST, 1)
    except Exception:
        pass
    MEM = 18            
    tracks = {}         
    n = 0
    last_img = 0
    while True:
        img = capture_minimap()
        final, scored = procesar(img, ally_i, enemy_i)
        seen = set()
        for name, conf, cx, cy, team in final:
            tracks[name] = [cx, cy, conf, team, 0]; seen.add(name)
        for name in list(tracks):
            if name not in seen:
                tracks[name][4] += 1
                if tracks[name][4] > MEM: del tracks[name]
        items = [(nm, t[2], t[0], t[1], t[3], t[4] == 0) for nm, t in tracks.items()]
        vis = pintar(img, items, scored)
        cv2.imshow("en vivo", vis)
        txt = "  ".join(f"{nm}{'' if t[4]==0 else '·'}" for nm, t in tracks.items()) or "(nada)"
        print(f"\r[{time.strftime('%H:%M:%S')}] {len(items)} -> {txt}        ", end="", flush=True)
        if time.time() - last_img >= INTERVAL:
            n += 1
            cv2.imwrite(os.path.join(OUT, "ultima.png"), vis)
            cv2.imwrite(os.path.join(OUT, f"vivo_{n:03d}.png"), vis)
            last_img = time.time()
        k = cv2.waitKey(80) & 0xFF
        if k == ord('q') or keyboard.is_pressed("f10"):
            print("\nSaliendo."); break
    cv2.destroyAllWindows()
