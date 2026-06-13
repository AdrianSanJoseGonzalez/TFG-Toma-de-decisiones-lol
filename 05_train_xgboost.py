import pandas as pd
import numpy as np
import xgboost as xgb
from sklearn.model_selection import StratifiedGroupKFold, cross_val_score
from sklearn.preprocessing import LabelEncoder
from sklearn.dummy import DummyClassifier
from sklearn.metrics import (classification_report, accuracy_score,
                             confusion_matrix, f1_score)
from sklearn.utils.class_weight import compute_sample_weight
import matplotlib
matplotlib.use('Agg')  # Para servidores sin pantalla
import matplotlib.pyplot as plt
import seaborn as sns
import joblib
import warnings
warnings.filterwarnings('ignore')

from ml_common import (DATASET_PATH, MODEL_DIR, RANDOM_STATE,
                       load_clean_dataset, split_games, encode_categoricals)

# CONFIGURACIÓN
MODEL_DIR.mkdir(exist_ok=True)

USE_OPTUNA    = True   
OPTUNA_TRIALS = 30     

# 1. CARGA Y LIMPIEZA DE DATOS (compartida con el LSTM, ml_common)
print("=" * 60)
print("[1/8] CARGANDO DATASET...")
print("=" * 60)

df = load_clean_dataset(DATASET_PATH)
print(f"  > Filas (solo ganadores, tras limpieza): {len(df)}")
print(f"  > Partidas únicas:  {df['game_id'].nunique()}")

print("\n  > Distribución final de labels:")
label_counts = df["label"].value_counts()
for label, count in label_counts.items():
    pct = count / len(df) * 100
    bar = "#" * int(pct / 2)
    print(f"    {label:<25} {count:>5} ({pct:>5.1f}%) {bar}")

df = df.drop(columns=[c for c in ["x", "z", "x_norm", "z_norm"] if c in df.columns])

# 2. SPLIT TRAIN / VAL / TEST POR PARTIDA COMPLETA
print("\n" + "=" * 60)
print("[2/8] SPLIT POR PARTIDA (train/val/test, sin data leakage)...")
print("=" * 60)

train_games, val_games, test_games = split_games(df)

df_train = df[df["game_id"].isin(train_games)].copy()
df_val   = df[df["game_id"].isin(val_games)].copy()
df_test  = df[df["game_id"].isin(test_games)].copy()

print(f"  > Partidas train: {len(train_games)} ({len(df_train)} filas)")
print(f"  > Partidas val:   {len(val_games)} ({len(df_val)} filas)")
print(f"  > Partidas test:  {len(test_games)} ({len(df_test)} filas)")

joblib.dump(
    {"train": train_games, "val": val_games, "test": test_games},
    MODEL_DIR / "game_splits.joblib"
)

groups_train = df_train["game_id"].values

# 3. PREPROCESAMIENTO
print("\n" + "=" * 60)
print("[3/8] PREPROCESANDO FEATURES...")
print("=" * 60)

COLS_IGNORAR = ["game_id", "game_time", "team", "win"]

df_train = df_train.drop(columns=[c for c in COLS_IGNORAR if c in df_train.columns])
df_val   = df_val.drop(columns=[c for c in COLS_IGNORAR if c in df_val.columns])
df_test  = df_test.drop(columns=[c for c in COLS_IGNORAR if c in df_test.columns])

categorical_cols = [c for c in df_train.select_dtypes(include=["object"]).columns
                    if c != "label"]
print(f"  > Columnas categóricas a encodar: {categorical_cols}")

label_encoders = encode_categoricals([df_train, df_val, df_test], categorical_cols)

le_target = LabelEncoder()
le_target.fit(df["label"])

y_train = le_target.transform(df_train["label"])
y_val   = le_target.transform(df_val["label"])
y_test  = le_target.transform(df_test["label"])

X_train = df_train.drop(columns=["label"])
X_val   = df_val.drop(columns=["label"])
X_test  = df_test.drop(columns=["label"])

print(f"  > Features finales: {X_train.shape[1]}")
print(f"  > Clases:           {le_target.classes_}")

joblib.dump(label_encoders, MODEL_DIR / "label_encoders.joblib")
joblib.dump(le_target,      MODEL_DIR / "target_encoder.joblib")
joblib.dump(list(X_train.columns), MODEL_DIR / "feature_names.joblib")
print(f"  > Encoders guardados en {MODEL_DIR}/")

# 4. PESOS DE CLASE (para clases desbalanceadas)
print("\n" + "=" * 60)
print("[4/8] CALCULANDO PESOS DE CLASE...")
print("=" * 60)

sample_weights = compute_sample_weight(class_weight="balanced", y=y_train)
print(f"  > Pesos calculados para {len(np.unique(y_train))} clases")
print(f"  > Rango de pesos: [{sample_weights.min():.3f}, {sample_weights.max():.3f}]")

# 5. ENTRENAMIENTO XGBOOST (hiperparámetros elegidos con VAL)
print("\n" + "=" * 60)
print("[5/8] ENTRENANDO XGBOOST...")
print("=" * 60)

if USE_OPTUNA:
    import optuna
    optuna.logging.set_verbosity(optuna.logging.WARNING)
    print(f"  > Lanzando búsqueda Optuna ({OPTUNA_TRIALS} trials) sobre VALIDACIÓN...")

    def objective(trial):
        params = {
            'n_estimators':     trial.suggest_int('n_estimators', 200, 800),
            'learning_rate':    trial.suggest_float('learning_rate', 0.01, 0.15),
            'max_depth':        trial.suggest_int('max_depth', 4, 12),
            'subsample':        trial.suggest_float('subsample', 0.6, 1.0),
            'colsample_bytree': trial.suggest_float('colsample_bytree', 0.5, 1.0),
            'min_child_weight': trial.suggest_int('min_child_weight', 1, 15),
            'gamma':            trial.suggest_float('gamma', 0.0, 1.0),
            'reg_alpha':        trial.suggest_float('reg_alpha', 0.0, 1.0),
            'reg_lambda':       trial.suggest_float('reg_lambda', 0.5, 3.0),
        }
        m = xgb.XGBClassifier(
            **params,
            tree_method="hist", eval_metric="mlogloss",
            random_state=RANDOM_STATE, n_jobs=-1, verbosity=0,
            early_stopping_rounds=30,
        )
        m.fit(X_train, y_train, sample_weight=sample_weights,
              eval_set=[(X_val, y_val)], verbose=False)
        preds = m.predict(X_val)
        return f1_score(y_val, preds, average="weighted", zero_division=0)

    study = optuna.create_study(direction="maximize")
    study.optimize(objective, n_trials=OPTUNA_TRIALS, show_progress_bar=False)
    best_params = study.best_params
    print(f"  > Mejor F1 (val): {study.best_value:.4f}")
    print(f"  > Mejores parámetros: {best_params}")
else:
    best_params = dict(
        n_estimators     = 500,
        learning_rate    = 0.05,
        max_depth        = 8,
        subsample        = 0.8,
        colsample_bytree = 0.8,
        min_child_weight = 5,
        gamma            = 0.1,
    )

model = xgb.XGBClassifier(
    **best_params,
    tree_method="hist", eval_metric="mlogloss",
    random_state=RANDOM_STATE, n_jobs=-1, verbosity=1,
    early_stopping_rounds=50,
)
model.fit(
    X_train, y_train,
    sample_weight = sample_weights,
    eval_set      = [(X_val, y_val)],   
    verbose       = 50,
)

model.save_model(str(MODEL_DIR / "xgboost_lol_tactics.json"))
print(f"\n  > Modelo guardado en: {MODEL_DIR / 'xgboost_lol_tactics.json'}")

# 6. BASELINE TRIVIAL (contexto para las métricas del TFG)
print("\n" + "=" * 60)
print("[6/8] BASELINE TRIVIAL (DummyClassifier)...")
print("=" * 60)

dummy = DummyClassifier(strategy="most_frequent").fit(X_train, y_train)
y_pred_dummy = dummy.predict(X_test)

dummy_acc = accuracy_score(y_test, y_pred_dummy)
dummy_f1w = f1_score(y_test, y_pred_dummy, average="weighted", zero_division=0)
dummy_f1m = f1_score(y_test, y_pred_dummy, average="macro", zero_division=0)
print(f"  > Dummy ACCURACY:    {dummy_acc:.2%}")
print(f"  > Dummy F1 WEIGHTED: {dummy_f1w:.2%}")
print(f"  > Dummy F1 MACRO:    {dummy_f1m:.2%}")

# 7. EVALUACIÓN FINAL SOBRE TEST (una sola vez)
print("\n" + "=" * 60)
print("[7/8] EVALUANDO EL MODELO SOBRE TEST...")
print("=" * 60)

y_pred = model.predict(X_test)

# ── MÉTRICAS GLOBALES ─────────────────────────────────────────
accuracy    = accuracy_score(y_test, y_pred)
f1_macro    = f1_score(y_test, y_pred, average="macro",    zero_division=0)
f1_weighted = f1_score(y_test, y_pred, average="weighted", zero_division=0)

print(f"\n  [*] ACCURACY:    {accuracy:.2%}   (baseline: {dummy_acc:.2%})")
print(f"  [*] F1 MACRO:    {f1_macro:.2%}   (baseline: {dummy_f1m:.2%})")
print(f"  [*] F1 WEIGHTED: {f1_weighted:.2%}   (baseline: {dummy_f1w:.2%})\n")

# ── REPORTE POR CLASE ─────────────────────────────────────────
print("\n  > Reporte detallado por acción:")
print(classification_report(
    y_test, y_pred,
    target_names = le_target.classes_,
    labels = np.arange(len(le_target.classes_)),
    zero_division = 0
))

# ── REPORTE POR ROL ───────────────────────────────────────────
print("\n  > F1-Score por Rol:")
if "role" in df_test.columns:
    for rol_val in df_test["role"].unique():
        rol_name = label_encoders["role"].inverse_transform([int(rol_val)])[0]
        mask_rol = df_test["role"] == rol_val
        if mask_rol.sum() > 0:
            f1_rol = f1_score(
                y_test[mask_rol.values],
                y_pred[mask_rol.values],
                average="weighted", zero_division=0
            )
            print(f"    {rol_name:<10} F1={f1_rol:.3f} (n={mask_rol.sum()})")

print("\n  > Validación cruzada (5-fold agrupada por partida, en train)...")
cv = StratifiedGroupKFold(n_splits=5, shuffle=True, random_state=RANDOM_STATE)
cv_model = xgb.XGBClassifier(
    **best_params,
    tree_method="hist", eval_metric="mlogloss",
    random_state=RANDOM_STATE, n_jobs=-1, verbosity=0,
)
cv_scores = cross_val_score(
    cv_model, X_train, y_train,
    groups=groups_train, cv=cv, scoring="f1_weighted"
)
print(f"    CV F1-Weighted: {cv_scores.mean():.3f} (+/- {cv_scores.std()*2:.3f})")

# 8. VISUALIZACIONES PARA LA MEMORIA DEL TFG
print("\n" + "=" * 60)
print("[8/8] GENERANDO GRÁFICOS PARA EL TFG...")
print("=" * 60)

# ── GRÁFICO 1: Matriz de Confusión ────────────────────────────
print("  > Generando matriz de confusión...")
cm = confusion_matrix(y_test, y_pred, labels=np.arange(len(le_target.classes_)))
fig, ax = plt.subplots(figsize=(12, 10))
sns.heatmap(
    cm, annot=True, fmt="d", cmap="Blues",
    xticklabels=le_target.classes_,
    yticklabels=le_target.classes_,
    ax=ax
)
ax.set_xlabel("Predicción", fontsize=13)
ax.set_ylabel("Real",       fontsize=13)
ax.set_title("Matriz de Confusión - XGBoost LoL Tactics\n(Partidas Challenger EUW)", fontsize=14)
plt.xticks(rotation=45, ha="right")
plt.yticks(rotation=0)
plt.tight_layout()
plt.savefig(MODEL_DIR / "confusion_matrix.png", dpi=150)
plt.close()

# ── GRÁFICO 2: Feature Importance (Top 25) ────────────────────
print("  > Generando feature importance...")
feat_imp = pd.DataFrame({
    "Feature":    X_train.columns,
    "Importance": model.feature_importances_
}).sort_values("Importance", ascending=False).head(25)

fig, ax = plt.subplots(figsize=(12, 10))
colors = plt.cm.RdYlGn(np.linspace(0.3, 0.9, len(feat_imp)))
bars = ax.barh(feat_imp["Feature"], feat_imp["Importance"], color=colors[::-1])
ax.set_xlabel("Importancia (ganancia)", fontsize=12)
ax.set_title("Top 25 Variables más Importantes\npara Tomar Decisiones en LoL", fontsize=14)
ax.invert_yaxis()
plt.tight_layout()
plt.savefig(MODEL_DIR / "feature_importance.png", dpi=150)
plt.close()

# ── GRÁFICO 3: Distribución de Labels (train/val/test) ────────
print("  > Generando distribución de labels...")
fig, axes = plt.subplots(1, 3, figsize=(18, 6))
splits = [("TRAIN", y_train, "steelblue"),
          ("VAL",   y_val,   "seagreen"),
          ("TEST",  y_test,  "coral")]
for ax, (nombre, y_split, color) in zip(axes, splits):
    counts = pd.Series(y_split).map(dict(enumerate(le_target.classes_))).value_counts()
    counts.plot(kind="bar", ax=ax, color=color, edgecolor="white")
    ax.set_title(f"Distribución Labels - {nombre}", fontsize=13)
    ax.set_xlabel("")
    ax.tick_params(axis="x", rotation=45)

plt.suptitle("Distribución de Acciones en el Dataset", fontsize=15, y=1.02)
plt.tight_layout()
plt.savefig(MODEL_DIR / "label_distribution.png", dpi=150, bbox_inches="tight")
plt.close()

print("\n" + "=" * 60)
print("  PROCESO COMPLETADO")
print(f"  XGBoost F1w={f1_weighted:.2%} vs Baseline F1w={dummy_f1w:.2%}")
print("=" * 60)
