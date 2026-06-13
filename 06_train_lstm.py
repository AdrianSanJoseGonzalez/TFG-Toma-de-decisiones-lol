

import random
import pandas as pd
import numpy as np
import torch
import torch.nn as nn
from torch.utils.data import Dataset, DataLoader
from sklearn.preprocessing import LabelEncoder, StandardScaler
from sklearn.metrics import classification_report, confusion_matrix, f1_score, accuracy_score
from sklearn.utils.class_weight import compute_class_weight
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import seaborn as sns
import joblib
import warnings
warnings.filterwarnings('ignore')

from ml_common import (DATASET_PATH, MODEL_DIR, RANDOM_STATE,
                       load_clean_dataset, split_games, encode_categoricals)
from lstm_model import LoLTacticsLSTM


random.seed(RANDOM_STATE)
np.random.seed(RANDOM_STATE)
torch.manual_seed(RANDOM_STATE)
torch.cuda.manual_seed_all(RANDOM_STATE)

SEQ_LEN       = 5    
BATCH_SIZE    = 64
EPOCHS        = 40
LEARNING_RATE = 0.001
HIDDEN_SIZE   = 128
NUM_LAYERS    = 2

MODEL_DIR.mkdir(exist_ok=True)
device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
print(f"[*] Dispositivo de cálculo: {device.type.upper()}")

# 1. CARGA Y LIMPIEZA 
print("\n" + "="*60)
print("[1/6] CARGANDO Y LIMPIANDO DATOS...")
print("="*60)

df = load_clean_dataset(DATASET_PATH)

print(f"  > Filas tras limpieza: {len(df)}")
print(f"  > Distribución de labels:")
for label, count in df["label"].value_counts().items():
    print(f"    {label:<25} {count:>5}")

# 2. SPLIT TRAIN / VAL / TEST POR PARTIDA
print("\n" + "="*60)
print("[2/6] SPLIT POR PARTIDA (train/val/test)...")
print("="*60)

train_games, val_games, test_games = split_games(df)
df_train = df[df["game_id"].isin(train_games)].copy()
df_val   = df[df["game_id"].isin(val_games)].copy()
df_test  = df[df["game_id"].isin(test_games)].copy()

print(f"  > Train: {len(train_games)} partidas ({len(df_train)} filas)")
print(f"  > Val:   {len(val_games)} partidas ({len(df_val)} filas)")
print(f"  > Test:  {len(test_games)} partidas ({len(df_test)} filas)")

for d in (df_train, df_val, df_test):
    d["_champ_key"] = d["champion"]

# 3. PREPROCESAMIENTO
print("\n" + "="*60)
print("[3/6] PREPROCESANDO FEATURES...")
print("="*60)

COLS_IGNORAR = ["game_id", "game_time", "team", "win", "label", "label_idx", "_champ_key"]

COLS_REDUNDANTES = [
    "x", "z",            
    "x_norm", "z_norm",
    "level_norm",        
    "time_norm",        
    "current_hp",       
    "current_resource",  
    "max_resource",      
    "max_hp",
]

ITEM_COLS = ["item_0", "item_1", "item_2", "item_3", "item_4", "item_5",
             "trinket", "item_7", "item_quest"]

COLS_BINARIAS = [
    "is_dead", "alma_equipo", "proximo_es_alma",
    "dragon_disponible", "baron_buff_activo", "baron_disponible",
    "en_lado_aliado", "has_boots",
    "grubs_disponibles", "herald_disponible", "herald_tomado", "is_moving",
]
COLS_BINARIAS = [c for c in COLS_BINARIAS if c in df_train.columns]

CATEGORICAL = [
    c for c in df_train.select_dtypes(include=["object"]).columns
    if c not in COLS_IGNORAR
]

NUMERICAL_SCALE = [
    c for c in df_train.columns
    if c not in CATEGORICAL + COLS_IGNORAR + COLS_REDUNDANTES
                + ITEM_COLS + COLS_BINARIAS
]

print(f"  > Categóricas a encodar ({len(CATEGORICAL)}): {CATEGORICAL}")
print(f"  > Numéricas a escalar ({len(NUMERICAL_SCALE)}): primeras 10: {NUMERICAL_SCALE[:10]}")
print(f"  > Binarias sin escalar ({len(COLS_BINARIAS)}): {COLS_BINARIAS}")

label_encoders = encode_categoricals([df_train, df_val, df_test], CATEGORICAL)

le_target = LabelEncoder()
le_target.fit(df["label"])
df_train["label_idx"] = le_target.transform(df_train["label"])
df_val["label_idx"]   = le_target.transform(df_val["label"])
df_test["label_idx"]  = le_target.transform(df_test["label"])

scaler = StandardScaler()
df_train[NUMERICAL_SCALE] = scaler.fit_transform(df_train[NUMERICAL_SCALE].fillna(0))
df_val[NUMERICAL_SCALE]   = scaler.transform(df_val[NUMERICAL_SCALE].fillna(0))
df_test[NUMERICAL_SCALE]  = scaler.transform(df_test[NUMERICAL_SCALE].fillna(0))

FEATURES = CATEGORICAL + NUMERICAL_SCALE + COLS_BINARIAS
print(f"\n  > Features totales para el LSTM: {len(FEATURES)}")

joblib.dump(label_encoders, MODEL_DIR / "lstm_label_encoders.joblib")
joblib.dump(le_target,      MODEL_DIR / "lstm_target_encoder.joblib")
joblib.dump(scaler,         MODEL_DIR / "lstm_scaler.joblib")
joblib.dump(FEATURES,       MODEL_DIR / "lstm_feature_names.joblib")

# 4. CREACIÓN DE SECUENCIAS
print("\n" + "="*60)
print("[4/6] CREANDO SECUENCIAS TEMPORALES...")
print("="*60)

def create_sequences(df_part, seq_len, stride=1):
    """Ventanas deslizantes por (partida, campeón).

    Devuelve también los metadatos del frame final de cada ventana
    (game_id, champion, game_time, role) para poder comparar con XGBoost
    sobre exactamente los mismos frames.
    """
    X, y, meta = [], [], []

    for (g_id, champ), group in df_part.groupby(["game_id", "_champ_key"]):
        group  = group.sort_values("game_time")
        vals   = group[FEATURES].values.astype(np.float32)
        labels = group["label_idx"].values.astype(np.int64)

        if len(group) < seq_len:
            continue

        for i in range(0, len(group) - seq_len + 1, stride):
            X.append(vals[i : i + seq_len])
            y.append(labels[i + seq_len - 1])
            last = group.iloc[i + seq_len - 1]
            meta.append((g_id, champ, last["game_time"], last["role"]))

    meta_df = pd.DataFrame(meta, columns=["game_id", "champion", "game_time", "role"])
    return np.array(X, dtype=np.float32), np.array(y, dtype=np.int64), meta_df

print(f"  > Creando secuencias con SEQ_LEN={SEQ_LEN}, STRIDE={STRIDE}...")
X_train, y_train, meta_train = create_sequences(df_train, SEQ_LEN, STRIDE)
X_val,   y_val,   meta_val   = create_sequences(df_val,   SEQ_LEN, stride=1)
X_test,  y_test,  meta_test  = create_sequences(df_test,  SEQ_LEN, stride=1)

print(f"  > Train: {X_train.shape}  ({X_train.shape[0]} secuencias)")
print(f"  > Val:   {X_val.shape}  ({X_val.shape[0]} secuencias)")
print(f"  > Test:  {X_test.shape}  ({X_test.shape[0]} secuencias)")

meta_out = meta_test.copy()
meta_out["role"] = label_encoders["role"].inverse_transform(meta_out["role"].astype(int))
meta_out.to_csv(MODEL_DIR / "lstm_eval_frames.csv", index=False)
print(f"  > Frames evaluables guardados en {MODEL_DIR / 'lstm_eval_frames.csv'}")


# 5. DATASETS Y MODELO
class LoLDataset(Dataset):
    def __init__(self, X, y):
        self.X = torch.tensor(X)
        self.y = torch.tensor(y)
    def __len__(self): return len(self.X)
    def __getitem__(self, idx): return self.X[idx], self.y[idx]

train_loader = DataLoader(LoLDataset(X_train, y_train), batch_size=BATCH_SIZE, shuffle=True)
val_loader   = DataLoader(LoLDataset(X_val,   y_val),   batch_size=BATCH_SIZE, shuffle=False)
test_loader  = DataLoader(LoLDataset(X_test,  y_test),  batch_size=BATCH_SIZE, shuffle=False)

NUM_CLASSES = len(le_target.classes_)

present = np.unique(y_train)
cw = compute_class_weight('balanced', classes=present, y=y_train)
class_weights = np.ones(NUM_CLASSES, dtype=np.float32)
class_weights[present] = np.power(cw, 0.5).astype(np.float32)
class_weights_tensor = torch.tensor(class_weights, dtype=torch.float32).to(device)

INPUT_SIZE = X_train.shape[2]
model     = LoLTacticsLSTM(INPUT_SIZE, HIDDEN_SIZE, NUM_LAYERS, NUM_CLASSES).to(device)
criterion = nn.CrossEntropyLoss(weight=class_weights_tensor)
optimizer = torch.optim.AdamW(model.parameters(), lr=LEARNING_RATE, weight_decay=1e-4)
scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(optimizer, patience=3, factor=0.5)

# 6. ENTRENAMIENTO (early stopping con VAL, nunca con test)
print("\n" + "="*60)
print("[5/6] ENTRENANDO LSTM...")
print("="*60)

best_loss = float('inf')
patience_counter = 0
EARLY_STOP_PATIENCE = 8

for epoch in range(EPOCHS):
    model.train()
    train_loss = 0
    for batch_X, batch_y in train_loader:
        batch_X, batch_y = batch_X.to(device), batch_y.to(device)
        optimizer.zero_grad()
        loss = criterion(model(batch_X), batch_y)
        loss.backward()
        torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
        optimizer.step()
        train_loss += loss.item()
    train_loss /= len(train_loader)

    model.eval()
    val_loss = 0
    with torch.no_grad():
        for batch_X, batch_y in val_loader:
            batch_X, batch_y = batch_X.to(device), batch_y.to(device)
            val_loss += criterion(model(batch_X), batch_y).item()
    val_loss /= len(val_loader)
    scheduler.step(val_loss)

    print(f"  Epoch [{epoch+1:>2}/{EPOCHS}]  train_loss={train_loss:.4f}  val_loss={val_loss:.4f}")

    if val_loss < best_loss - 0.001:
        best_loss = val_loss
        patience_counter = 0
        torch.save(model.state_dict(), MODEL_DIR / "lstm_lol_tactics_best.pth")
    else:
        patience_counter += 1
        if patience_counter >= EARLY_STOP_PATIENCE:
            print(f"\n  [!] Early stopping en epoch {epoch+1}")
            break

model.load_state_dict(torch.load(MODEL_DIR / "lstm_lol_tactics_best.pth", map_location=device))

# 7. EVALUACIÓN FINAL SOBRE TEST (una sola vez)
print("\n" + "="*60)
print("[6/6] EVALUANDO LSTM SOBRE TEST...")
print("="*60)

model.eval()
all_preds, all_labels = [], []
with torch.no_grad():
    for batch_X, batch_y in test_loader:
        outputs = model(batch_X.to(device))
        _, predicted = torch.max(outputs, 1)
        all_preds.extend(predicted.cpu().numpy())
        all_labels.extend(batch_y.numpy())

all_preds, all_labels = np.array(all_preds), np.array(all_labels)
print(f"\n  [*] ACCURACY: {accuracy_score(all_labels, all_preds):.2%}")
print(f"  [*] F1 MACRO:    {f1_score(all_labels, all_preds, average='macro', zero_division=0):.2%}")
print(f"  [*] F1 WEIGHTED: {f1_score(all_labels, all_preds, average='weighted', zero_division=0):.2%}\n")
print(classification_report(
    all_labels, all_preds,
    target_names=le_target.classes_,
    labels=np.arange(NUM_CLASSES),
    zero_division=0
))

print("\n  > F1-Score por Rol:")
roles_test = meta_test["role"].values
for rol_enc in np.unique(roles_test):
    rol_name = label_encoders["role"].inverse_transform([int(rol_enc)])[0]
    mask = roles_test == rol_enc
    if mask.sum() > 10:
        f1_rol = f1_score(all_labels[mask], all_preds[mask], average="weighted", zero_division=0)
        print(f"    {rol_name:<10} F1={f1_rol:.3f} (n={mask.sum()})")

cm = confusion_matrix(all_labels, all_preds, labels=np.arange(NUM_CLASSES))
fig, ax = plt.subplots(figsize=(12, 10))
sns.heatmap(cm, annot=True, fmt="d", cmap="Purples",
            xticklabels=le_target.classes_, yticklabels=le_target.classes_, ax=ax)
ax.set_xlabel("Predicción", fontsize=13)
ax.set_ylabel("Real",       fontsize=13)
ax.set_title("Matriz de Confusión - LSTM LoL Tactics\n(Partidas Challenger EUW)", fontsize=14)
plt.xticks(rotation=45, ha="right")
plt.yticks(rotation=0)
plt.tight_layout()
plt.savefig(MODEL_DIR / "lstm_confusion_matrix.png", dpi=150)
plt.close()

print("\n" + "="*60)
print("  PROCESO COMPLETADO")
print("="*60)
