import pandas as pd
import numpy as np
import xgboost as xgb
import torch
import joblib
from sklearn.metrics import classification_report, accuracy_score, f1_score
import warnings
warnings.filterwarnings('ignore')

from ml_common import MODEL_DIR, load_clean_dataset, apply_encoders
from lstm_model import LoLTacticsLSTM

SEQ_LEN     = 5      
HIDDEN_SIZE = 128
NUM_LAYERS  = 2
BATCH_SIZE  = 512
device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

# 1. CARGA DE ARTEFACTOS
print("[*] Cargando artefactos de los modelos...")

xgb_model = xgb.XGBClassifier()
xgb_model.load_model(str(MODEL_DIR / "xgboost_lol_tactics.json"))
xgb_encoders  = joblib.load(MODEL_DIR / "label_encoders.joblib")
xgb_target_le = joblib.load(MODEL_DIR / "target_encoder.joblib")
xgb_features  = joblib.load(MODEL_DIR / "feature_names.joblib")

lstm_encoders  = joblib.load(MODEL_DIR / "lstm_label_encoders.joblib")
lstm_target_le = joblib.load(MODEL_DIR / "lstm_target_encoder.joblib")
lstm_scaler    = joblib.load(MODEL_DIR / "lstm_scaler.joblib")
lstm_features  = joblib.load(MODEL_DIR / "lstm_feature_names.joblib")

assert np.array_equal(xgb_target_le.classes_, lstm_target_le.classes_), \
    "Los target encoders de XGBoost y LSTM no coinciden. Re-entrena 05 y 06."
CLASSES     = xgb_target_le.classes_
NUM_CLASSES = len(CLASSES)

# 2. DATOS
print("[*] Preparando dataset de test (split oficial)...")

splits = joblib.load(MODEL_DIR / "game_splits.joblib")
df = load_clean_dataset()
df_train = df[df["game_id"].isin(splits["train"])]
df_test  = df[df["game_id"].isin(splits["test"])].copy()

y_true_full = xgb_target_le.transform(df_test["label"])
print(f"  > Partidas test: {len(splits['test'])} ({len(df_test)} filas)")

# 3. PREDICCIONES XGBOOST 
print("[*] Generando predicciones XGBoost...")
X_xgb = df_test.copy()
apply_encoders(X_xgb, xgb_encoders)
probs_xgb_full = xgb_model.predict_proba(X_xgb[xgb_features])

# 4. PREDICCIONES LSTM 
print("[*] Generando predicciones LSTM...")
X_lstm = df_test.copy()
apply_encoders(X_lstm, lstm_encoders)
num_cols = list(lstm_scaler.feature_names_in_)
X_lstm[num_cols] = lstm_scaler.transform(X_lstm[num_cols].fillna(0))

lstm_net = LoLTacticsLSTM(len(lstm_features), HIDDEN_SIZE, NUM_LAYERS, NUM_CLASSES).to(device)
lstm_net.load_state_dict(torch.load(MODEL_DIR / "lstm_lol_tactics_best.pth", map_location=device))
lstm_net.eval()

sequences, eval_index = [], []
for (g_id, champ), group in df_test.groupby(["game_id", "champion"]):
    group = group.sort_values("game_time")
    feats = X_lstm.loc[group.index, lstm_features].values.astype(np.float32)
    if len(group) < SEQ_LEN:
        continue
    for i in range(SEQ_LEN - 1, len(group)):
        sequences.append(feats[i - SEQ_LEN + 1 : i + 1])
        eval_index.append(group.index[i])

X_seq = torch.tensor(np.array(sequences, dtype=np.float32))
probs_lstm_chunks = []
with torch.no_grad():
    for k in range(0, len(X_seq), BATCH_SIZE):
        logits = lstm_net(X_seq[k:k + BATCH_SIZE].to(device))
        probs_lstm_chunks.append(torch.softmax(logits, dim=1).cpu().numpy())
probs_lstm = np.vstack(probs_lstm_chunks)

pos_map  = {idx: pos for pos, idx in enumerate(df_test.index)}
eval_pos = np.array([pos_map[i] for i in eval_index])
probs_xgb = probs_xgb_full[eval_pos]
y_eval    = y_true_full[eval_pos]

n_eval = len(eval_pos)
print(f"  > Frames comparables (con historia): {n_eval} de {len(df_test)}")

eval_frames_csv = MODEL_DIR / "lstm_eval_frames.csv"
if eval_frames_csv.exists():
    n_csv = len(pd.read_csv(eval_frames_csv))
    if n_csv != n_eval:
        print(f"  [WARN] lstm_eval_frames.csv tiene {n_csv} frames, aquí hay {n_eval}."
              f" ¿06 y 08 usan el mismo dataset/split?")

# 5. BASELINE TRIVIAL (clase mayoritaria del train)
clase_mayoritaria = df_train["label"].mode()[0]
y_pred_dummy = np.full(n_eval, xgb_target_le.transform([clase_mayoritaria])[0])


# 6. ENSEMBLE "SUPER-HÍBRIDO" (Especialización + Confianza)

print("\n" + "=" * 60)
print("      APLICANDO LÓGICA DE ENSEMBLE (HÍBRIDO)")
print("=" * 60)

LSTM_CONFIDENCE_THRESHOLD = 0.70
LSTM_MICRO_WEIGHT = 0.70   
LSTM_MACRO_WEIGHT = 0.40   
MICRO_LABELS = ["GANK", "SOLO_KILL"]

final_probs = np.zeros_like(probs_xgb)
for i in range(n_eval):
    lstm_max_prob = probs_lstm[i].max()
    lstm_label    = CLASSES[probs_lstm[i].argmax()]

    if lstm_label in MICRO_LABELS:
        w_lstm = LSTM_MICRO_WEIGHT
    elif lstm_max_prob >= LSTM_CONFIDENCE_THRESHOLD:
        w_lstm = LSTM_MACRO_WEIGHT
    else:
        w_lstm = 0.0
    final_probs[i] = (1.0 - w_lstm) * probs_xgb[i] + w_lstm * probs_lstm[i]

y_pred_xgb = probs_xgb.argmax(axis=1)
y_pred_lstm = probs_lstm.argmax(axis=1)
y_pred_ens = final_probs.argmax(axis=1)

# 7. EVALUACIÓN COMPARATIVA 
def metricas(y_pred):
    return {
        "accuracy":    accuracy_score(y_eval, y_pred),
        "f1_weighted": f1_score(y_eval, y_pred, average="weighted", zero_division=0),
        "f1_macro":    f1_score(y_eval, y_pred, average="macro", zero_division=0),
    }

globales = {
    "Baseline (mayoritaria)": metricas(y_pred_dummy),
    "XGBoost":                metricas(y_pred_xgb),
    "LSTM":                   metricas(y_pred_lstm),
    "Ensemble (Híbrido)":     metricas(y_pred_ens),
}

print(f"\n[*] RESULTADOS ({n_eval} frames comparables de test):")
for nombre, m in globales.items():
    print(f"    {nombre:<24} Acc={m['accuracy']:.4f}  F1w={m['f1_weighted']:.4f}  F1m={m['f1_macro']:.4f}")

f1_xgb, f1_ens = globales["XGBoost"]["f1_weighted"], globales["Ensemble (Híbrido)"]["f1_weighted"]
mejora = (f1_ens - f1_xgb) / f1_xgb * 100
print(f"\n[!] ENSEMBLE vs XGBOOST SOLO: {mejora:+.2f}% en F1 weighted")

print("\n[Reporte Detallado Ensemble]")
print(classification_report(y_eval, y_pred_ens, target_names=CLASSES,
                            labels=np.arange(NUM_CLASSES), zero_division=0))

# 8. GUARDAR RESULTADOS 
def reporte_por_clase(y_pred):
    return classification_report(y_eval, y_pred, target_names=CLASSES,
                                 labels=np.arange(NUM_CLASSES),
                                 zero_division=0, output_dict=True)

resultados = {
    "frames_evaluados": int(n_eval),
    "filas_test_total": int(len(df_test)),
    "partidas_test":    int(len(splits["test"])),
    "clases":           list(CLASSES),
    "globales":         globales,
    "por_clase": {
        "XGBoost":            reporte_por_clase(y_pred_xgb),
        "LSTM":               reporte_por_clase(y_pred_lstm),
        "Ensemble (Híbrido)": reporte_por_clase(y_pred_ens),
    },
}
joblib.dump(resultados, MODEL_DIR / "comparativa_resultados.joblib")

ensemble_config = {
    "strategy":     "Super-Hybrid",
    "threshold":    LSTM_CONFIDENCE_THRESHOLD,
    "micro_weight": LSTM_MICRO_WEIGHT,
    "macro_weight": LSTM_MACRO_WEIGHT,
    "micro_labels": MICRO_LABELS,
    "f1_improvement": mejora,
}
joblib.dump(ensemble_config, MODEL_DIR / "ensemble_config.joblib")

print(f"[OK] Resultados guardados en {MODEL_DIR / 'comparativa_resultados.joblib'}")
print(f"[OK] Config del ensemble en  {MODEL_DIR / 'ensemble_config.joblib'}")
print("\nAhora ejecuta 07_comparar_modelos.py para generar la tabla y los gráficos del TFG.")
