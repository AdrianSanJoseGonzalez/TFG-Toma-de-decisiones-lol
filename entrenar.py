import cv2, numpy as np, os, glob, json, random, time
import requests, urllib3
urllib3.disable_warnings()
import torch, torch.nn as nn
torch.manual_seed(0); np.random.seed(0); random.seed(0)

DATASET = r"C:\Users\Adrian\Downloads\dataset"
LABELED = os.path.join(DATASET, "labeled")
CACHE   = r"C:\Users\Adrian\Downloads\champ_circle"
SIZE    = 24
MIN_REAL_PER_CLASS = 5   

# ── recortes reales etiquetados ─────────────────────────────────────
real = {}   
for dd in sorted(glob.glob(os.path.join(LABELED, "*"))):
    name = os.path.basename(dd)
    imgs = [cv2.resize(cv2.imread(p),(SIZE,SIZE)) for p in glob.glob(os.path.join(dd,"*.png"))]
    imgs = [im for im in imgs if im is not None]
    if len(imgs) >= (1 if name=="_neg" else MIN_REAL_PER_CLASS):
        real[name] = imgs
if not real:
    raise SystemExit("No hay recortes etiquetados. Usa recolectar_datos.py primero.")
classes = sorted(real.keys())
cidx = {c:i for i,c in enumerate(classes)}
print(f"Clases ({len(classes)}): " + ", ".join(f"{c}({len(real[c])})" for c in classes))

# ── iconos reales para sintético ────────────────────────────────────
bgs = [cv2.imread(p) for p in glob.glob(r"C:\Users\Adrian\Downloads\capturas_limpias\limpia_*.png")]
bgs = [b for b in bgs if b is not None]
def rand_bg(dd):
    if bgs and random.random()<0.8:
        b=random.choice(bgs);H,W=b.shape[:2];x,y=random.randint(0,W-dd-1),random.randint(0,H-dd-1)
        return b[y:y+dd,x:x+dd].copy()
    return np.full((dd,dd,3),random.randint(25,70),np.uint8)
def load_icon(name):
    p=os.path.join(CACHE,f"{name}.png"); return cv2.imread(p) if os.path.exists(p) else None

def synth(icon):
    dd=random.randint(17,26);z=random.uniform(1.12,1.5)
    h,w=icon.shape[:2];m=int(min(h,w)/(2*z));cy,cx=h//2,w//2
    face=cv2.resize(icon[cy-m:cy+m,cx-m:cx+m],(dd,dd),interpolation=cv2.INTER_AREA)
    canvas=rand_bg(SIZE+8);cc=(SIZE+8)//2;off=(cc-dd//2,cc-dd//2)
    mask=np.zeros((dd,dd),np.uint8);cv2.circle(mask,(dd//2,dd//2),dd//2-1,255,-1)
    roi=canvas[off[1]:off[1]+dd,off[0]:off[0]+dd];roi[mask>0]=face[mask>0]
    ctr=(off[0]+dd//2,off[1]+dd//2);rc=random.random()
    if rc<0.45:col=(random.randint(180,255),random.randint(120,180),0)
    elif rc<0.9:col=(0,0,random.randint(180,255))
    else:col=None
    if col:cv2.circle(canvas,ctr,dd//2-1,col,random.choice([1,2]))
    s=4;img=canvas[s:s+SIZE,s:s+SIZE]
    a=random.uniform(0.8,1.2);b=random.randint(-20,20)
    img=np.clip(img.astype(np.float32)*a+b,0,255).astype(np.uint8)
    if random.random()<0.4:img=cv2.GaussianBlur(img,(3,3),0.6)
    return img

# ── construir dataset: real (train/val split) + sintético ───────────
Xtr,ytr,Xva,yva=[],[],[],[]
for c in classes:
    ims=real[c][:]; random.shuffle(ims)
    k=max(1,int(len(ims)*0.2))
    for im in ims[k:]: Xtr.append(im);ytr.append(cidx[c])
    for im in ims[:k]: Xva.append(im);yva.append(cidx[c])
    ic=load_icon(c)
    if ic is not None:
        for _ in range(120): Xtr.append(synth(ic));ytr.append(cidx[c])

def to_t(X): return torch.tensor(np.array(X,np.float32).transpose(0,3,1,2)/255.0)
Xtr_t,ytr_t=to_t(Xtr),torch.tensor(ytr)
Xva_t,yva_t=to_t(Xva),torch.tensor(yva)
print(f"train {len(ytr)} (real+sintetico)  val {len(yva)} (solo REAL)")

class Net(nn.Module):
    def __init__(s,n):
        super().__init__()
        s.c=nn.Sequential(nn.Conv2d(3,32,3,1,1),nn.BatchNorm2d(32),nn.ReLU(),nn.MaxPool2d(2),
            nn.Conv2d(32,64,3,1,1),nn.BatchNorm2d(64),nn.ReLU(),nn.MaxPool2d(2),
            nn.Conv2d(64,128,3,1,1),nn.BatchNorm2d(128),nn.ReLU(),nn.MaxPool2d(2))
        s.f=nn.Sequential(nn.Flatten(),nn.Linear(128*3*3,256),nn.ReLU(),nn.Dropout(0.3),nn.Linear(256,n))
    def forward(s,x):return s.f(s.c(x))

net=Net(len(classes));opt=torch.optim.Adam(net.parameters(),1e-3);lf=nn.CrossEntropyLoss()
print("Entrenando...")
for ep in range(20):
    net.train();perm=torch.randperm(len(ytr_t))
    for i in range(0,len(ytr_t),128):
        idx=perm[i:i+128];opt.zero_grad();lf(net(Xtr_t[idx]),ytr_t[idx]).backward();opt.step()
    if (ep+1)%5==0:
        net.eval()
        with torch.no_grad(): acc=(net(Xva_t).argmax(1)==yva_t).float().mean().item()
        print(f"  ep{ep+1}: val_REAL_top1={acc:.3f}")

torch.save(net.state_dict(), os.path.join(DATASET,"modelo.pt"))
json.dump(classes, open(os.path.join(DATASET,"classes.json"),"w"))
print(f"\nModelo guardado en {DATASET}\\modelo.pt  ({len(classes)} clases)")
