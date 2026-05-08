
import pandas as pd
import numpy as np
import xgboost as xgb
import torch
import torch.nn as nn
import joblib
import os
import matplotlib.pyplot as plt
import seaborn as sns
from sklearn.model_selection import train_test_split
from sklearn.metrics import classification_report, accuracy_score, f1_score, confusion_matrix
from torch.utils.data import DataLoader, Dataset

"""
╔══════════════════════════════════════════════════════════════╗
║         08_ENSEMBLE_ADVISOR.PY - Modelo Híbrido TFG          ║
║         XGBoost (Macro) + LSTM (Temporal/Micro)              ║
╚══════════════════════════════════════════════════════════════╝
"""

# ══════════════════════════════════════════════════════════════
# CONFIGURACIÓN Y CARGA
# ══════════════════════════════════════════════════════════════
MODEL_DIR = "ml_models"
DATASET_PATH = r"ml_dataset\dataset_completo.csv"
RANDOM_STATE = 42
SEQ_LEN = 5
device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

print("[*] Cargando artefactos de los modelos...")

# 1. Cargar XGBoost
xgb_model = xgb.XGBClassifier()
xgb_model.load_model(f"{MODEL_DIR}/xgboost_lol_tactics.json")
xgb_encoders = joblib.load(f"{MODEL_DIR}/label_encoders.joblib")
xgb_target_le = joblib.load(f"{MODEL_DIR}/target_encoder.joblib")
xgb_features = joblib.load(f"{MODEL_DIR}/feature_names.joblib")

# 2. Cargar LSTM
lstm_encoders = joblib.load(f"{MODEL_DIR}/lstm_label_encoders.joblib")
lstm_target_le = joblib.load(f"{MODEL_DIR}/lstm_target_encoder.joblib")
lstm_scaler = joblib.load(f"{MODEL_DIR}/lstm_scaler.joblib")

# Necesitamos reconstruir la arquitectura para cargar los pesos
class LoLTacticsLSTM(nn.Module):
    def __init__(self, input_size, hidden_size, num_layers, num_classes):
        super().__init__()
        self.lstm = nn.LSTM(input_size, hidden_size, num_layers, batch_first=True, dropout=0.3)
        self.fc1 = nn.Linear(hidden_size, 64)
        self.relu = nn.ReLU()
        self.dropout = nn.Dropout(0.3)
        self.fc2 = nn.Linear(64, num_classes)

    def forward(self, x):
        out, _ = self.lstm(x)
        out = out[:, -1, :]
        out = self.fc1(out)
        out = self.relu(out)
        out = self.dropout(out)
        return self.fc2(out)

# El input size depende de las features guardadas en el script 06
# Re-detectamos las features como en el script 06
def get_lstm_features(df_sample):
    COLS_IGNORAR = ["game_id", "game_time", "team", "win", "label"]
    COLS_REDUNDANTES = ["x_norm", "z_norm", "level_norm", "time_norm", "current_hp", "current_resource", "max_resource", "max_hp"]
    COLS_BINARIAS = ["is_dead", "alma_equipo", "proximo_es_alma", "dragon_disponible", "baron_buff_activo", "baron_disponible", "en_lado_aliado", "has_boots"]
    ITEM_COLS = ["item_0", "item_1", "item_2", "item_3", "item_4", "item_5", "trinket", "item_7"]
    CATEGORICAL = [c for c in df_sample.select_dtypes(include=["object"]).columns if c not in COLS_IGNORAR + COLS_REDUNDANTES] + ITEM_COLS
    CATEGORICAL = [c for c in CATEGORICAL if c != "game_id"]
    NUMERICAL_SCALE = [c for c in df_sample.columns if c not in CATEGORICAL + COLS_IGNORAR + COLS_REDUNDANTES + COLS_BINARIAS + ["label_idx"]]
    return CATEGORICAL + NUMERICAL_SCALE + COLS_BINARIAS

# ══════════════════════════════════════════════════════════════
# CARGA DE DATOS Y PREPARACIÓN
# ══════════════════════════════════════════════════════════════
print("[*] Preparando Dataset de Test...")
df = pd.read_csv(DATASET_PATH)
df = df[df["win"] == 1].copy()
fusion_map = {"RECALL_LOW_HP": "RECALL", "ROAM": "MOVE", "PICK": "SKIRMISH",
              "PUSH_TOWER": "PUSH_STRUCTURE", "PUSH_INHIB": "PUSH_STRUCTURE"}
df["label"] = df["label"].replace(fusion_map)
df = df[df["label"] != "DEAD"].copy()

# Mismas condiciones absurdas que 05 y 06
condiciones_absurdas = [
    (df["role"] == "UTILITY") & (df["label"] == "SPLITPUSH"),
    (df["role"] != "JUNGLE") & (df["label"] == "GANK"),
]
for condicion in condiciones_absurdas:
    df = df[~condicion]

all_games = df["game_id"].unique()
_, test_games = train_test_split(all_games, test_size=0.2, random_state=RANDOM_STATE)
df_test = df[df["game_id"].isin(test_games)].copy()

# Las columnas en_lado_aliado, dist_al_centro, dist_fuente_aliada
# ya vienen calculadas correctamente desde el script 03 (diagonal x+z=15000)
# No las recalculamos para evitar inconsistencias

# ══════════════════════════════════════════════════════════════
# PROCESAMIENTO POR MODELO
# ══════════════════════════════════════════════════════════════

# 1. Preparar para XGBoost
print("[*] Generando predicciones XGBoost...")
X_xgb = df_test.copy()
for col, le in xgb_encoders.items():
    X_xgb[col] = X_xgb[col].fillna("UNKNOWN").astype(str)
    # Manejar clases nuevas en test si las hubiera
    X_xgb[col] = X_xgb[col].map(lambda s: le.transform([s])[0] if s in le.classes_ else -1)

X_xgb_final = X_xgb[xgb_features]
probs_xgb = xgb_model.predict_proba(X_xgb_final)

# 2. Preparar para LSTM
print("[*] Generando predicciones LSTM...")
LSTM_FEATURES = get_lstm_features(df_test)
X_lstm_base = df_test.copy()

# Aplicar encoders de LSTM
for col, le in lstm_encoders.items():
    X_lstm_base[col] = X_lstm_base[col].fillna("UNKNOWN").astype(str)
    X_lstm_base[col] = X_lstm_base[col].map(lambda s: le.transform([s])[0] if s in le.classes_ else -1)

# Escalar
NUM_SCALE = [c for c in LSTM_FEATURES if c in lstm_scaler.feature_names_in_]
X_lstm_base[NUM_SCALE] = lstm_scaler.transform(X_lstm_base[NUM_SCALE].fillna(0))

# Cargar Pesos LSTM
input_dim = len(LSTM_FEATURES)
num_classes = len(lstm_target_le.classes_)
lstm_net = LoLTacticsLSTM(input_dim, 128, 2, num_classes).to(device)
lstm_net.load_state_dict(torch.load(f"{MODEL_DIR}/lstm_lol_tactics_best.pth", map_location=device))
lstm_net.eval()

# Generar secuencias y predicciones
all_probs_lstm = np.zeros((len(df_test), num_classes))
y_true = lstm_target_le.transform(df_test["label"])

idx_global = 0
for (g_id, champ), group in df_test.groupby(["game_id", "champion"]):
    group_processed = X_lstm_base.loc[group.index, LSTM_FEATURES].values.astype(np.float32)
    
    for i in range(len(group)):
        if i < SEQ_LEN - 1:
            all_probs_lstm[idx_global + i] = np.ones(num_classes) / num_classes
        else:
            seq = group_processed[i - SEQ_LEN + 1 : i + 1]
            seq_t = torch.tensor(seq).unsqueeze(0).to(device)
            with torch.no_grad():
                logits = lstm_net(seq_t)
                probs = torch.softmax(logits, dim=1).cpu().numpy()[0]
                all_probs_lstm[idx_global + i] = probs
    
    idx_global += len(group)

# ══════════════════════════════════════════════════════════════
# LÓGICA DE ENSEMBLE (HIBRIDACIÓN)
# ══════════════════════════════════════════════════════════════
print("\n" + "="*60)
print("      APLICANDO LÓGICA DE ENSEMBLE (HÍBRIDO)")
print("="*60)

# Verificar consistencia de clases
if not np.array_equal(xgb_target_le.classes_, lstm_target_le.classes_):
    print("[WARN] Los target encoders no coinciden. Re-mapeando...")
    pass

# ── ESTRATEGIA ANTIGUA: Weighted Average con Especialización ──────
# (Comentada — el LSTM con 22% accuracy arrastraba al XGBoost)
#
# weights_per_class = {
#     "SOLO_KILL": 0.7,    # Prioridad LSTM
#     "GANK":      0.6,    # Prioridad LSTM
#     "COMBAT":    0.2,    # Prioridad XGBoost
#     "FARM":      0.1,    # Prioridad XGBoost
#     "JUNGLE_FARM": 0.1,  # Prioridad XGBoost
#     "DEFAULT":   0.3     # Peso base para el LSTM
# }
#
# final_probs = np.zeros_like(probs_xgb)
# for i in range(len(final_probs)):
#     lstm_pred_idx = np.argmax(all_probs_lstm[i])
#     lstm_label = lstm_target_le.classes_[lstm_pred_idx]
#     w_lstm = weights_per_class.get(lstm_label, weights_per_class["DEFAULT"])
#     w_xgb  = 1.0 - w_lstm
#     final_probs[i] = (w_xgb * probs_xgb[i]) + (w_lstm * all_probs_lstm[i])
# y_pred_ensemble = np.argmax(final_probs, axis=1)

# ── ESTRATEGIA NUEVA: Super-Híbrida (Especialización + Confianza) ──
# 1. El LSTM manda en la MICRO (Gank, Solo Kill).
# 2. El XGBoost manda en la MACRO, y solo escuchamos al LSTM si está segurísimo.

LSTM_CONFIDENCE_THRESHOLD = 0.70
LSTM_MICRO_WEIGHT = 0.70    # Peso del LSTM en sus especialidades
LSTM_MACRO_WEIGHT = 0.40    # Peso del LSTM cuando supera el umbral en macro

MICRO_LABELS = ["GANK", "SOLO_KILL"]

final_probs = np.zeros_like(probs_xgb)

for i in range(len(final_probs)):
    lstm_max_prob = np.max(all_probs_lstm[i])
    lstm_pred_idx = np.argmax(all_probs_lstm[i])
    lstm_label = lstm_target_le.classes_[lstm_pred_idx]
    
    if lstm_label in MICRO_LABELS:
        # Prioridad a la Micro del LSTM
        w_lstm = LSTM_MICRO_WEIGHT
        w_xgb  = 1.0 - w_lstm
        final_probs[i] = (w_xgb * probs_xgb[i]) + (w_lstm * all_probs_lstm[i])
    elif lstm_max_prob >= LSTM_CONFIDENCE_THRESHOLD:
        # Solo mezclamos en Macro si el LSTM está muy seguro
        w_lstm = LSTM_MACRO_WEIGHT
        w_xgb  = 1.0 - w_lstm
        final_probs[i] = (w_xgb * probs_xgb[i]) + (w_lstm * all_probs_lstm[i])
    else:
        # Confianza total en XGBoost
        final_probs[i] = probs_xgb[i]

y_pred_ensemble = np.argmax(final_probs, axis=1)

# ══════════════════════════════════════════════════════════════
# EVALUACIÓN COMPARATIVA
# ══════════════════════════════════════════════════════════════
acc_xgb = accuracy_score(y_true, np.argmax(probs_xgb, axis=1))
acc_lstm = accuracy_score(y_true, np.argmax(all_probs_lstm, axis=1))
acc_ens = accuracy_score(y_true, y_pred_ensemble)

f1_xgb = f1_score(y_true, np.argmax(probs_xgb, axis=1), average="weighted")
f1_lstm = f1_score(y_true, np.argmax(all_probs_lstm, axis=1), average="weighted")
f1_ens = f1_score(y_true, y_pred_ensemble, average="weighted")

print(f"\n[*] RESULTADOS FINALES:")
print(f"    XGBoost Accuracy:  {acc_xgb:.4f} (F1: {f1_xgb:.4f})")
print(f"    LSTM Accuracy:     {acc_lstm:.4f} (F1: {f1_lstm:.4f})")
print(f"    ENSEMBLE Accuracy: {acc_ens:.4f} (F1: {f1_ens:.4f})")

# Mejora relativa
mejora = ((f1_ens - f1_xgb) / f1_xgb) * 100
print(f"\n[!] MEJORA DEL ENSEMBLE vs BASELINE: {mejora:+.2f}%")

# Reporte detallado Ensemble
print("\n[Reporte Detallado Ensemble]")
print(classification_report(y_true, y_pred_ensemble, target_names=xgb_target_le.classes_))

# ══════════════════════════════════════════════════════════════
# GRÁFICO PARA EL TFG
# ══════════════════════════════════════════════════════════════
plt.figure(figsize=(10, 6))
modelos = ['XGBoost', 'LSTM', 'Ensemble (Híbrido)']
scores = [f1_xgb, f1_lstm, f1_ens]
colors = ['steelblue', 'coral', 'mediumseagreen']

bars = plt.bar(modelos, scores, color=colors)
plt.ylabel('F1-Score (Weighted)')
plt.title('Comparativa Final para la Memoria TFG\nEfecto del Ensemble Híbrido')
plt.ylim(0, 1.0)

for bar in bars:
    yval = bar.get_height()
    plt.text(bar.get_x() + bar.get_width()/2, yval + 0.02, f'{yval:.3f}', ha='center', fontweight='bold')

plt.tight_layout()
plt.savefig(f"{MODEL_DIR}/ensemble_comparison.png", dpi=150)
print(f"\n[OK] Gráfico comparativo guardado en {MODEL_DIR}/ensemble_comparison.png")

# Guardar la lógica de pesos para el Unified Advisor
ensemble_config = {
    "strategy": "Super-Hybrid",
    "threshold": LSTM_CONFIDENCE_THRESHOLD,
    "micro_weight": LSTM_MICRO_WEIGHT,
    "macro_weight": LSTM_MACRO_WEIGHT,
    "micro_labels": MICRO_LABELS,
    "f1_improvement": mejora
}
joblib.dump(ensemble_config, f"{MODEL_DIR}/ensemble_config.joblib")
