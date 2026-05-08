import pandas as pd
import numpy as np
import xgboost as xgb
from sklearn.model_selection import train_test_split, StratifiedKFold, cross_val_score
from sklearn.preprocessing import LabelEncoder
from sklearn.metrics import (classification_report, accuracy_score,
                             confusion_matrix, ConfusionMatrixDisplay, f1_score)
from sklearn.utils.class_weight import compute_sample_weight
import matplotlib.pyplot as plt
import matplotlib
matplotlib.use('Agg')  # Para servidores sin pantalla
import seaborn as sns
import joblib
import os
import warnings
warnings.filterwarnings('ignore')

# ══════════════════════════════════════════════════════════════
# CONFIGURACIÓN
# ══════════════════════════════════════════════════════════════
DATASET_PATH = r"C:\Users\Adrian\.gemini\antigravity\scratch\lol_replay_downloader\ml_dataset\dataset_completo.csv"
MODEL_DIR    = "ml_models"
RANDOM_STATE = 42
TEST_SIZE    = 0.2   # 20% de partidas para test
MIN_SAMPLES_POR_CLASE = 10  # Clases con menos ejemplos se fusionan

os.makedirs(MODEL_DIR, exist_ok=True)

# ══════════════════════════════════════════════════════════════
# 1. CARGA Y LIMPIEZA DE DATOS
# ══════════════════════════════════════════════════════════════
print("=" * 60)
print("[1/7] CARGANDO DATASET...")
print("=" * 60)

df = pd.read_csv(DATASET_PATH)
print(f"  > Filas totales:    {len(df)}")
print(f"  > Partidas únicas:  {df['game_id'].nunique()}")
print(f"  > Columnas:         {len(df.columns)}")

# ── IMITATION LEARNING: Solo aprender de jugadores ganadores ──
df = df[df["win"] == 1].copy()
print(f"  > Filas ganadores (Imitation Learning): {len(df)}")

# ── FUSIÓN DE CLASES MINORITARIAS ─────────────────────────────
# Las clases con muy pocos ejemplos se fusionan con la más similar
# para evitar que XGBoost las ignore completamente
fusion_map = {
    "RECALL_LOW_HP": "RECALL",       # Recall urgente → Recall
    "ROAM":          "MOVE",         # Roaming → Movimiento
    "PICK":          "SKIRMISH",     # Pick → Escaramuza
    "PUSH_TOWER":    "PUSH_STRUCTURE", # Empujar torre → Empujar estructura
    "PUSH_INHIB":    "PUSH_STRUCTURE", # Empujar inhib → Empujar estructura
}
df["label"] = df["label"].replace(fusion_map)

# ── ELIMINAR ETIQUETAS RUIDOSAS ───────────
# DEAD no es una decisión táctica, es un estado inevitable.
df = df[df["label"] != "DEAD"].copy()

# ── LIMPIEZA DE ABSURDOS POR ROL ───────────
# Eliminar combinaciones rol+label que ensucian el dataset
condiciones_absurdas = [
    (df["role"] == "UTILITY") & (df["label"] == "SPLITPUSH"),
    (df["role"] != "JUNGLE") & (df["label"] == "GANK"),  # Solo el Jungla puede hacer Gank
]
for condicion in condiciones_absurdas:
    df = df[~condicion]

print("\n  > Distribución final de labels:")
label_counts = df["label"].value_counts()
for label, count in label_counts.items():
    pct = count / len(df) * 100
    bar = "#" * int(pct / 2)
    print(f"    {label:<25} {count:>5} ({pct:>5.1f}%) {bar}")

# ── FEATURE ENGINEERING SEMÁNTICO (TFG) ──────────────────────
MAP_CENTER_X = 7500
MAP_CENTER_Z = 7500

df['dist_al_centro'] = np.sqrt(
    (df['x'] - MAP_CENTER_X)**2 + 
    (df['z'] - MAP_CENTER_Z)**2
)

df['en_lado_aliado'] = (
    ((df['team'] == 'ORDER') & ((df['x'] + df['z']) < 15000)) |
    ((df['team'] == 'CHAOS') & ((df['x'] + df['z']) > 15000))
).astype(int)

df['dist_fuente_aliada'] = np.where(
    df['team'] == 'ORDER',
    np.sqrt((df['x'] - 560)**2  + (df['z'] - 560)**2),
    np.sqrt((df['x'] - 14340)**2 + (df['z'] - 14390)**2)
)

# Eliminar x y z por separado (petición de Claude)
df = df.drop(columns=['x', 'z', 'x_norm', 'z_norm'])

# ══════════════════════════════════════════════════════════════
# 2. SPLIT POR PARTIDA COMPLETA (evita data leakage)
# ══════════════════════════════════════════════════════════════
print("\n" + "=" * 60)
print("[2/7] SPLIT POR PARTIDA (evitando data leakage)...")
print("=" * 60)

# CRÍTICO: Split de partidas enteras, NO de filas individuales
# Si una partida queda mitad en train y mitad en test,
# el modelo "hace trampa" al haber visto el contexto de esa partida
all_games  = df["game_id"].unique()
train_games, test_games = train_test_split(
    all_games, test_size=TEST_SIZE, random_state=RANDOM_STATE
)

train_mask = df["game_id"].isin(train_games)
df_train   = df[train_mask].copy()
df_test    = df[~train_mask].copy()

print(f"  > Partidas train: {len(train_games)} ({len(df_train)} filas)")
print(f"  > Partidas test:  {len(test_games)}  ({len(df_test)} filas)")

# ══════════════════════════════════════════════════════════════
# 3. PREPROCESAMIENTO
# ══════════════════════════════════════════════════════════════
print("\n" + "=" * 60)
print("[3/7] PREPROCESANDO FEATURES...")
print("=" * 60)

# Columnas a eliminar (identificadores, no features)
COLS_IGNORAR = ["game_id", "game_time", "team", "win"]

df_train = df_train.drop(columns=[c for c in COLS_IGNORAR if c in df_train.columns])
df_test  = df_test.drop(columns=[c for c in COLS_IGNORAR if c in df_test.columns])

# ── ENCODING DE VARIABLES CATEGÓRICAS ────────────────────────
# NOTA: champion SÍ se incluye como feature (información valiosa)
# Un Malzahar mid farmea diferente a un Rammus jungle
label_encoders = {}
categorical_cols = df_train.select_dtypes(include=["object"]).columns.tolist()
categorical_cols = [c for c in categorical_cols if c != "label"]

print(f"  > Columnas categóricas a encodar: {categorical_cols}")

for col in categorical_cols:
    le = LabelEncoder()
    df_train[col] = df_train[col].fillna("UNKNOWN").astype(str)
    df_test[col]  = df_test[col].fillna("UNKNOWN").astype(str)

    le.fit(pd.concat([df_train[col], df_test[col]]).unique())
    df_train[col] = le.transform(df_train[col])
    df_test[col]  = le.transform(df_test[col])
    label_encoders[col] = le

# ── ENCODING DEL TARGET ───────────────────────────────────────
le_target = LabelEncoder()
le_target.fit(pd.concat([df_train["label"], df_test["label"]]))

y_train = le_target.transform(df_train["label"])
y_test  = le_target.transform(df_test["label"])

X_train = df_train.drop(columns=["label"])
X_test  = df_test.drop(columns=["label"])

print(f"  > Features finales: {X_train.shape[1]}")
print(f"  > Clases:           {le_target.classes_}")

# Guardar encoders para la demo en vivo
joblib.dump(label_encoders, f"{MODEL_DIR}/label_encoders.joblib")
joblib.dump(le_target,      f"{MODEL_DIR}/target_encoder.joblib")
joblib.dump(list(X_train.columns), f"{MODEL_DIR}/feature_names.joblib")
print(f"  > Encoders guardados en {MODEL_DIR}/")

# ══════════════════════════════════════════════════════════════
# 4. PESOS DE CLASE (para clases desbalanceadas)
# ══════════════════════════════════════════════════════════════
print("\n" + "=" * 60)
print("[4/7] CALCULANDO PESOS DE CLASE...")
print("=" * 60)

# Con FARM al 45% y CONTEST_OBJECTIVE al 0.7%,
# sin pesos el modelo ignorará las clases minoritarias
sample_weights = compute_sample_weight(class_weight="balanced", y=y_train)
print(f"  > Pesos calculados para {len(np.unique(y_train))} clases")
print(f"  > Rango de pesos: [{sample_weights.min():.3f}, {sample_weights.max():.3f}]")

# ══════════════════════════════════════════════════════════════
# 5. ENTRENAMIENTO XGBOOST
# ══════════════════════════════════════════════════════════════
print("\n" + "=" * 60)
print("[5/7] ENTRENANDO XGBOOST...")
print("=" * 60)

# ── Búsqueda de Hiperparámetros con Optuna ────────────────────
import optuna
optuna.logging.set_verbosity(optuna.logging.WARNING)

USE_OPTUNA = True    # Cambiar a False para usar los parámetros manuales
OPTUNA_TRIALS = 30   # Número de combinaciones a probar

if USE_OPTUNA:
    print("  > Lanzando búsqueda Optuna ({} trials)...".format(OPTUNA_TRIALS))

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
              eval_set=[(X_test, y_test)], verbose=False)
        preds = m.predict(X_test)
        return f1_score(y_test, preds, average="weighted", zero_division=0)

    study = optuna.create_study(direction="maximize")
    study.optimize(objective, n_trials=OPTUNA_TRIALS, show_progress_bar=False)
    best = study.best_params
    print(f"  > Mejor F1 encontrado: {study.best_value:.4f}")
    print(f"  > Mejores parámetros: {best}")

    model = xgb.XGBClassifier(
        **best,
        tree_method="hist", eval_metric="mlogloss",
        random_state=RANDOM_STATE, n_jobs=-1, verbosity=1,
        early_stopping_rounds=50,
    )
else:
    # Parámetros manuales (fallback)
    model = xgb.XGBClassifier(
        n_estimators     = 500,
        learning_rate    = 0.05,
        max_depth        = 8,
        subsample        = 0.8,
        colsample_bytree = 0.8,
        min_child_weight = 5,
        gamma            = 0.1,
        early_stopping_rounds = 50,
        tree_method      = "hist",
        eval_metric      = "mlogloss",
        random_state     = RANDOM_STATE,
        n_jobs           = -1,
        verbosity        = 1,
    )

model.fit(
    X_train, y_train,
    sample_weight        = sample_weights,
    eval_set             = [(X_test, y_test)],
    verbose              = 50,
)

# Guardar modelo
model.save_model(f"{MODEL_DIR}/xgboost_lol_tactics.json")
print(f"\n  > Modelo guardado en: {MODEL_DIR}/xgboost_lol_tactics.json")

# ══════════════════════════════════════════════════════════════
# 6. EVALUACIÓN COMPLETA
# ══════════════════════════════════════════════════════════════
print("\n" + "=" * 60)
print("[6/7] EVALUANDO EL MODELO...")
print("=" * 60)

y_pred = model.predict(X_test)

# ── MÉTRICAS GLOBALES ─────────────────────────────────────────
accuracy   = accuracy_score(y_test, y_pred)
f1_macro   = f1_score(y_test, y_pred, average="macro",    zero_division=0)
f1_weighted = f1_score(y_test, y_pred, average="weighted", zero_division=0)

print(f"\n  [*] ACCURACY:    {accuracy:.2%}")
print(f"  [*] F1 MACRO:    {f1_macro:.2%}  (sin sesgo)")
print(f"  [*] F1 WEIGHTED: {f1_weighted:.2%}  (con sesgo)\n")

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
# Identificamos el nombre real de la columna "role" encodada.
roles_col = "role" if "role" in df_test.columns else None
if roles_col:
    for rol_val in df_test[roles_col].unique():
        # Desencodificamos para imprimir bonito si tenemos el encoder
        rol_name = label_encoders["role"].inverse_transform([int(rol_val)])[0] if "role" in label_encoders else str(rol_val)
        mask_rol = df_test[roles_col] == rol_val
        if mask_rol.sum() > 0:
            f1_rol = f1_score(
                y_test[mask_rol.values],
                y_pred[mask_rol.values],
                average="weighted", zero_division=0
            )
            print(f"    {rol_name:<10} F1={f1_rol:.3f} (n={mask_rol.sum()})")

# ── CROSS VALIDATION ──────────────────────────────────────────
print("\n  > Validación cruzada (5-fold) en datos de entrenamiento...")
cv = StratifiedKFold(n_splits=5, shuffle=True, random_state=RANDOM_STATE)
cv_scores = cross_val_score(
    xgb.XGBClassifier(
        n_estimators=100, learning_rate=0.05, max_depth=8,  # Reducido n_estimators para el CV para ir rápido
        tree_method="hist", n_jobs=-1, verbosity=0,
        random_state=RANDOM_STATE
    ),
    X_train, y_train,
    cv=cv, scoring="f1_weighted"
)
print(f"    CV F1-Weighted: {cv_scores.mean():.3f} (+/- {cv_scores.std()*2:.3f})")

# ══════════════════════════════════════════════════════════════
# 7. VISUALIZACIONES PARA LA MEMORIA DEL TFG
# ══════════════════════════════════════════════════════════════
print("\n" + "=" * 60)
print("[7/7] GENERANDO GRÁFICOS PARA EL TFG...")
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
plt.savefig(f"{MODEL_DIR}/confusion_matrix.png", dpi=150)
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
plt.savefig(f"{MODEL_DIR}/feature_importance.png", dpi=150)
plt.close()

# ── GRÁFICO 3: Distribución de Labels (train vs test) ─────────
print("  > Generando distribución de labels...")
fig, axes = plt.subplots(1, 2, figsize=(14, 6))
train_counts = pd.Series(y_train).map(dict(enumerate(le_target.classes_))).value_counts()
test_counts  = pd.Series(y_test).map(dict(enumerate(le_target.classes_))).value_counts()

train_counts.plot(kind="bar", ax=axes[0], color="steelblue", edgecolor="white")
axes[0].set_title("Distribución Labels - TRAIN", fontsize=13)
axes[0].set_xlabel("")
axes[0].tick_params(axis="x", rotation=45)

test_counts.plot(kind="bar", ax=axes[1], color="coral", edgecolor="white")
axes[1].set_title("Distribución Labels - TEST", fontsize=13)
axes[1].set_xlabel("")
axes[1].tick_params(axis="x", rotation=45)

plt.suptitle("Distribución de Acciones en el Dataset", fontsize=15, y=1.02)
plt.tight_layout()
plt.savefig(f"{MODEL_DIR}/label_distribution.png", dpi=150, bbox_inches="tight")
plt.close()

print("\n" + "=" * 60)
print("  PROCESO COMPLETADO")
print("=" * 60)