

import pandas as pd
import numpy as np
import torch
import torch.nn as nn
from torch.utils.data import Dataset, DataLoader
from sklearn.model_selection import train_test_split
from sklearn.preprocessing import LabelEncoder, StandardScaler
from sklearn.metrics import classification_report, confusion_matrix, f1_score, accuracy_score
from sklearn.utils.class_weight import compute_class_weight
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import seaborn as sns
import os
import joblib
import warnings
warnings.filterwarnings('ignore')

# ══════════════════════════════════════════════════════════════
# CONFIGURACIÓN
# ══════════════════════════════════════════════════════════════
DATASET_PATH = r"ml_dataset\dataset_completo.csv"
MODEL_DIR    = "ml_models"
RANDOM_STATE = 42

# Hiperparámetros LSTM
SEQ_LEN       = 5      # Frames históricos que ve el LSTM
STRIDE        = 3      # Paso entre ventanas (reduce correlación)
BATCH_SIZE    = 64
EPOCHS        = 40
LEARNING_RATE = 0.001
HIDDEN_SIZE   = 128
NUM_LAYERS    = 2

os.makedirs(MODEL_DIR, exist_ok=True)
device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
print(f"[*] Dispositivo de cálculo: {device.type.upper()}")

# ══════════════════════════════════════════════════════════════
# 1. CARGA Y LIMPIEZA (idéntica a XGBoost para comparación justa)
# ══════════════════════════════════════════════════════════════
print("\n" + "="*60)
print("[1/6] CARGANDO Y LIMPIANDO DATOS...")
print("="*60)

df = pd.read_csv(DATASET_PATH)
df = df[df["win"] == 1].copy()

# Mismas fusiones que XGBoost
fusion_map = {
    "RECALL_LOW_HP": "RECALL",
    "ROAM":          "MOVE",
    "PICK":          "SKIRMISH",
    "PUSH_TOWER":    "PUSH_STRUCTURE",
    "PUSH_INHIB":    "PUSH_STRUCTURE",
}
df["label"] = df["label"].replace(fusion_map)

# Solo eliminar DEAD (igual que XGBoost — GANK se mantiene)
df = df[df["label"] != "DEAD"].copy()

# Mismas condiciones absurdas que XGBoost
condiciones_absurdas = [
    (df["role"] == "UTILITY") & (df["label"] == "SPLITPUSH"),
    (df["role"] != "JUNGLE") & (df["label"] == "GANK"),
]
for condicion in condiciones_absurdas:
    df = df[~condicion]

print(f"  > Filas tras limpieza: {len(df)}")
print(f"  > Distribución de labels:")
for label, count in df["label"].value_counts().items():
    print(f"    {label:<25} {count:>5}")

# ══════════════════════════════════════════════════════════════
# 2. SPLIT POR PARTIDA (mismo random_state que XGBoost)
# ══════════════════════════════════════════════════════════════
print("\n" + "="*60)
print("[2/6] SPLIT POR PARTIDA...")
print("="*60)

all_games = df["game_id"].unique()
train_games, test_games = train_test_split(
    all_games, test_size=0.2, random_state=RANDOM_STATE
)
df_train = df[df["game_id"].isin(train_games)].copy()
df_test  = df[df["game_id"].isin(test_games)].copy()

print(f"  > Train: {len(train_games)} partidas ({len(df_train)} filas)")
print(f"  > Test:  {len(test_games)} partidas ({len(df_test)} filas)")

# ══════════════════════════════════════════════════════════════
# 3. PREPROCESAMIENTO
# ══════════════════════════════════════════════════════════════
print("\n" + "="*60)
print("[3/6] PREPROCESANDO FEATURES...")
print("="*60)

# Columnas a ignorar completamente (no son features)
COLS_IGNORAR = ["game_id", "game_time", "team", "win", "label"]

# Columnas redundantes
COLS_REDUNDANTES = [
    "x_norm", "z_norm",  # versión normalizada ya está obsoleta
    "level_norm",        # level ya está, level_norm es redundante
    "time_norm",         # game_min ya está, time_norm es redundante
    "current_hp",        # tenemos hp_pct, max_hp, que ya capturan esta info
    "current_resource",  # ídem con resource_pct
    "max_resource",      # raramente útil dado resource_pct
    "max_hp",
]

# Columnas binarias: NO escalar, pero sí incluir como features
COLS_BINARIAS = [
    "is_dead", "alma_equipo", "proximo_es_alma",
    "dragon_disponible", "baron_buff_activo", "baron_disponible",
    "en_lado_aliado", "has_boots",
]

# Detectar categóricas (forzamos los items ya que son IDs numéricos pero actúan como categorías)
ITEM_COLS = ["item_0", "item_1", "item_2", "item_3", "item_4", "item_5", "trinket", "item_7"]
CATEGORICAL = [
    c for c in df_train.select_dtypes(include=["object"]).columns
    if c not in COLS_IGNORAR + COLS_REDUNDANTES
] + ITEM_COLS
# Asegurarnos de excluir game_id si se coló
CATEGORICAL = [c for c in CATEGORICAL if c != "game_id"]

# Numéricas escalables: todo lo que no sea categórico, ignorado, redundante o binario
NUMERICAL_SCALE = [
    c for c in df_train.columns
    if c not in CATEGORICAL + COLS_IGNORAR + COLS_REDUNDANTES + COLS_BINARIAS + ["label_idx"]
]

print(f"  > Categóricas a encodar ({len(CATEGORICAL)}): {CATEGORICAL}")
print(f"  > Numéricas a escalar ({len(NUMERICAL_SCALE)}): primeras 10: {NUMERICAL_SCALE[:10]}")
print(f"  > Binarias sin escalar ({len(COLS_BINARIAS)}): {COLS_BINARIAS}")

# Encoding de categóricas
label_encoders = {}
for col in CATEGORICAL:
    le = LabelEncoder()
    df_train[col] = df_train[col].fillna("UNKNOWN").astype(str)
    df_test[col]  = df_test[col].fillna("UNKNOWN").astype(str)
    le.fit(pd.concat([df_train[col], df_test[col]]))
    df_train[col] = le.transform(df_train[col])
    df_test[col]  = le.transform(df_test[col])
    label_encoders[col] = le

# Encoding del target
le_target = LabelEncoder()
le_target.fit(pd.concat([df_train["label"], df_test["label"]]))
df_train["label_idx"] = le_target.transform(df_train["label"])
df_test["label_idx"]  = le_target.transform(df_test["label"])

# Scaler SOLO en numéricas (no binarias)
scaler = StandardScaler()
df_train[NUMERICAL_SCALE] = scaler.fit_transform(df_train[NUMERICAL_SCALE].fillna(0))
df_test[NUMERICAL_SCALE]  = scaler.transform(df_test[NUMERICAL_SCALE].fillna(0))

# Guardar artefactos
joblib.dump(label_encoders, f"{MODEL_DIR}/lstm_label_encoders.joblib")
joblib.dump(le_target,      f"{MODEL_DIR}/lstm_target_encoder.joblib")
joblib.dump(scaler,         f"{MODEL_DIR}/lstm_scaler.joblib")

# Features finales que verá el LSTM
FEATURES = CATEGORICAL + NUMERICAL_SCALE + COLS_BINARIAS
print(f"\n  > Features totales para el LSTM: {len(FEATURES)}")

# ══════════════════════════════════════════════════════════════
# 4. CREACIÓN DE SECUENCIAS
# ══════════════════════════════════════════════════════════════
print("\n" + "="*60)
print("[4/6] CREANDO SECUENCIAS TEMPORALES...")
print("="*60)

def create_sequences(df_part, seq_len, stride=1):
    X, y = [], []

    for (g_id, champ), group in df_part.groupby(["game_id", "champion"]):
        group = group.sort_values("game_time")
        vals   = group[FEATURES].values.astype(np.float32)
        labels = group["label_idx"].values.astype(np.int64)

        if len(group) < seq_len:
            continue

        for i in range(0, len(group) - seq_len + 1, stride):
            X.append(vals[i : i + seq_len])
            y.append(labels[i + seq_len - 1])

    return np.array(X, dtype=np.float32), np.array(y, dtype=np.int64)

print(f"  > Creando secuencias con SEQ_LEN={SEQ_LEN}, STRIDE={STRIDE}...")
X_train, y_train = create_sequences(df_train, SEQ_LEN, STRIDE)
X_test,  y_test  = create_sequences(df_test,  SEQ_LEN, stride=1)

print(f"  > Train: {X_train.shape}  ({X_train.shape[0]} secuencias)")
print(f"  > Test:  {X_test.shape}  ({X_test.shape[0]} secuencias)")

# ══════════════════════════════════════════════════════════════
# 5. DATASETS Y MODELO
# ══════════════════════════════════════════════════════════════
class LoLDataset(Dataset):
    def __init__(self, X, y):
        self.X = torch.tensor(X)
        self.y = torch.tensor(y)
    def __len__(self): return len(self.X)
    def __getitem__(self, idx): return self.X[idx], self.y[idx]

train_loader = DataLoader(LoLDataset(X_train, y_train), batch_size=BATCH_SIZE, shuffle=True)
test_loader  = DataLoader(LoLDataset(X_test,  y_test),  batch_size=BATCH_SIZE, shuffle=False)

classes      = np.unique(y_train)
class_weights = compute_class_weight('balanced', classes=classes, y=y_train)
# NUEVO: Suavizar pesos para redes neuronales (evita gradientes extremos en clases raras)
class_weights = np.power(class_weights, 0.5) 
class_weights_tensor = torch.tensor(class_weights, dtype=torch.float32).to(device)

class LoLTacticsLSTM(nn.Module):
    def __init__(self, input_size, hidden_size, num_layers, num_classes):
        super().__init__()
        self.lstm = nn.LSTM(input_size, hidden_size, num_layers, batch_first=True, dropout=0.3)
        self.fc1     = nn.Linear(hidden_size, 64)
        self.relu    = nn.ReLU()
        self.dropout = nn.Dropout(0.3)
        self.fc2     = nn.Linear(64, num_classes)

    def forward(self, x):
        out, _ = self.lstm(x)
        out = out[:, -1, :]
        out = self.fc1(out)
        out = self.relu(out)
        out = self.dropout(out)
        return self.fc2(out)

INPUT_SIZE  = X_train.shape[2]
NUM_CLASSES = len(le_target.classes_)
model     = LoLTacticsLSTM(INPUT_SIZE, HIDDEN_SIZE, NUM_LAYERS, NUM_CLASSES).to(device)
criterion = nn.CrossEntropyLoss(weight=class_weights_tensor)
# NUEVO: Optimizador AdamW para mejor regularización en secuencias
optimizer = torch.optim.AdamW(model.parameters(), lr=LEARNING_RATE, weight_decay=1e-4)
scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(optimizer, patience=3, factor=0.5)

# ══════════════════════════════════════════════════════════════
# 6. ENTRENAMIENTO
# ══════════════════════════════════════════════════════════════
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
        for batch_X, batch_y in test_loader:
            batch_X, batch_y = batch_X.to(device), batch_y.to(device)
            val_loss += criterion(model(batch_X), batch_y).item()
    val_loss /= len(test_loader)
    scheduler.step(val_loss)

    print(f"  Epoch [{epoch+1:>2}/{EPOCHS}]  train_loss={train_loss:.4f}  val_loss={val_loss:.4f}")

    if val_loss < best_loss - 0.001:
        best_loss = val_loss
        patience_counter = 0
        torch.save(model.state_dict(), f"{MODEL_DIR}/lstm_lol_tactics_best.pth")
    else:
        patience_counter += 1
        if patience_counter >= EARLY_STOP_PATIENCE:
            print(f"\n  [!] Early stopping en epoch {epoch+1}")
            break

model.load_state_dict(torch.load(f"{MODEL_DIR}/lstm_lol_tactics_best.pth", map_location=device))

# ══════════════════════════════════════════════════════════════
# 7. EVALUACIÓN
# ══════════════════════════════════════════════════════════════
print("\n" + "="*60)
print("[6/6] EVALUANDO LSTM...")
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
print(classification_report(all_labels, all_preds, target_names=le_target.classes_, zero_division=0))

# F1 por rol
test_rows = []
for (g_id, champ), group in df_test.groupby(["game_id", "champion"]):
    group = group.sort_values("game_time")
    if len(group) < SEQ_LEN: continue
    for i in range(len(group) - SEQ_LEN + 1):
        test_rows.append(group.iloc[i + SEQ_LEN - 1]["role"])

roles_test = np.array(test_rows)
for rol_enc in np.unique(df_test["role"]):
    rol_name = label_encoders["role"].inverse_transform([int(rol_enc)])[0]
    mask = roles_test == rol_enc
    if mask.sum() > 10:
        f1_rol = f1_score(all_labels[mask], all_preds[mask], average="weighted", zero_division=0)
        print(f"    {rol_name:<10} F1={f1_rol:.3f}")

cm = confusion_matrix(all_labels, all_preds, labels=np.arange(NUM_CLASSES))
plt.figure(figsize=(12, 10))
sns.heatmap(cm, annot=True, fmt="d", cmap="Purples", xticklabels=le_target.classes_, yticklabels=le_target.classes_)
plt.xticks(rotation=45, ha="right"); plt.tight_layout()
plt.savefig(f"{MODEL_DIR}/lstm_confusion_matrix.png", dpi=150); plt.close()
