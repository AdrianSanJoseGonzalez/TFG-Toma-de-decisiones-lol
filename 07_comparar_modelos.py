
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import joblib
import sys

from ml_common import MODEL_DIR

RESULTS_PATH = MODEL_DIR / "comparativa_resultados.joblib"

if not RESULTS_PATH.exists():
    sys.exit("[ERROR] No existe comparativa_resultados.joblib. "
             "Ejecuta antes: python 08_ensemble_advisor.py")

res      = joblib.load(RESULTS_PATH)
globales = res["globales"]
clases   = res["clases"]

print(f"[*] Resultados sobre {res['frames_evaluados']} frames comparables "
      f"({res['partidas_test']} partidas de test)\n")

# 1. TABLA GLOBAL (Markdown + LaTeX para la memoria)
df_glob = pd.DataFrame(globales).T
df_glob.columns = ["Accuracy", "F1 ponderado", "F1 macro"]

print("=" * 64)
print("  TABLA GLOBAL (Markdown)")
print("=" * 64)
print(f"| {'Modelo':<24} | Accuracy | F1 pond. | F1 macro |")
print(f"|{'-'*26}|----------|----------|----------|")
for modelo, fila in df_glob.iterrows():
    print(f"| {modelo:<24} |   {fila['Accuracy']:.3f}  |   {fila['F1 ponderado']:.3f}  |   {fila['F1 macro']:.3f}  |")

print("\n" + "=" * 64)
print("  TABLA GLOBAL (LaTeX, para la memoria)")
print("=" * 64)
print(r"\begin{table}[htbp]")
print(r"  \centering")
print(r"  \caption{Comparativa de modelos sobre el conjunto de test"
      f" ({res['frames_evaluados']} instantes, {res['partidas_test']} partidas)." + "}")
print(r"  \label{tab:comparativa-modelos}")
print(r"  \begin{tabular}{lccc}")
print(r"    \hline")
print(r"    \textbf{Modelo} & \textbf{Accuracy} & \textbf{F1 ponderado} & \textbf{F1 macro} \\")
print(r"    \hline")
for modelo, fila in df_glob.iterrows():
    print(f"    {modelo} & {fila['Accuracy']:.3f} & {fila['F1 ponderado']:.3f} & {fila['F1 macro']:.3f} \\\\")
print(r"    \hline")
print(r"  \end{tabular}")
print(r"\end{table}")

# 2. TABLA F1 POR CLASE (los 3 modelos)
modelos_clase = ["XGBoost", "LSTM", "Ensemble (Híbrido)"]
f1_clase = pd.DataFrame({
    m: {c: res["por_clase"][m][c]["f1-score"] for c in clases}
    for m in modelos_clase
})
soporte = pd.Series({c: res["por_clase"]["XGBoost"][c]["support"] for c in clases})
f1_clase = f1_clase.loc[soporte.sort_values(ascending=False).index]

print("\n" + "=" * 64)
print("  F1 POR CLASE (ordenado por soporte en test)")
print("=" * 64)
print(f"{'Clase':<20} {'n':>6} {'XGBoost':>9} {'LSTM':>9} {'Ensemble':>9}")
for clase, fila in f1_clase.iterrows():
    print(f"{clase:<20} {soporte[clase]:>6.0f} {fila['XGBoost']:>9.3f} "
          f"{fila['LSTM']:>9.3f} {fila['Ensemble (Híbrido)']:>9.3f}")

# 3. GRÁFICO 1: métricas globales por modelo
fig, ax = plt.subplots(figsize=(11, 6))
modelos = list(df_glob.index)
x = np.arange(len(modelos))
width = 0.27
colores = ["#888888", "steelblue", "coral", "mediumseagreen"]

for j, (metrica, etiqueta) in enumerate([("Accuracy", "Accuracy"),
                                          ("F1 ponderado", "F1 ponderado"),
                                          ("F1 macro", "F1 macro")]):
    vals = df_glob[metrica].values
    rects = ax.bar(x + (j - 1) * width, vals, width, label=etiqueta)
    for r in rects:
        ax.annotate(f"{r.get_height():.2f}", xy=(r.get_x() + r.get_width()/2, r.get_height()),
                    xytext=(0, 3), textcoords="offset points", ha="center", fontsize=8)

ax.set_xticks(x)
ax.set_xticklabels(modelos, fontsize=10)
ax.set_ylabel("Puntuación")
ax.set_ylim(0, 1.05)
ax.set_title(f"Comparativa de Modelos — Test de {res['partidas_test']} partidas "
             f"({res['frames_evaluados']} instantes comparables)", fontsize=13)
ax.legend()
ax.grid(axis="y", linestyle="--", alpha=0.6)
plt.tight_layout()
plt.savefig(MODEL_DIR / "tabla_comparativa.png", dpi=150)
plt.close()

# 4. GRÁFICO 2: F1 por clase, XGBoost vs LSTM vs Ensemble
fig, ax = plt.subplots(figsize=(11, 8))
y = np.arange(len(f1_clase))
height = 0.27
colores_modelo = {"XGBoost": "steelblue", "LSTM": "coral", "Ensemble (Híbrido)": "mediumseagreen"}

for j, m in enumerate(modelos_clase):
    ax.barh(y + (j - 1) * height, f1_clase[m].values, height,
            label=m, color=colores_modelo[m])

ax.set_yticks(y)
ax.set_yticklabels(f1_clase.index, fontsize=10)
ax.invert_yaxis()
ax.set_xlabel("F1-Score")
ax.set_xlim(0, 1.0)
ax.set_title("F1 por Acción — XGBoost vs LSTM vs Ensemble", fontsize=13)
ax.legend(loc="lower right")
ax.grid(axis="x", linestyle="--", alpha=0.6)
plt.tight_layout()
plt.savefig(MODEL_DIR / "comparativa_por_clase.png", dpi=150)
plt.close()

print(f"\n[OK] Gráficos guardados:")
print(f"     {MODEL_DIR / 'tabla_comparativa.png'}")
print(f"     {MODEL_DIR / 'comparativa_por_clase.png'}")
