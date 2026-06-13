# TFG — Asesor de macro-acciones para League of Legends

Sistema de inteligencia artificial que recomienda la **macro-acción** más adecuada en
una partida de *League of Legends* (farmear, disputar un objetivo, rotar, combatir,
regresar a la base…) a partir del estado de la partida y de su evolución, **imitando las
decisiones de jugadores de élite**.

Proyecto Fin de Grado del Grado en Ingeniería Informática (Universidad de Deusto). Área:
Inteligencia Artificial.

---

## ¿Qué hace?

El sistema se compone de dos fases:

- **Fase *offline* (entrenamiento).** Descarga repeticiones de partidas de jugadores de
  élite, extrae de ellas una línea temporal detallada del estado de juego (combinando la
  API del cliente, la API oficial y visión por computador), etiqueta automáticamente
  cada instante con una macro-acción y, sobre el conjunto de datos resultante, entrena y
  compara tres modelos: **XGBoost**, una red **LSTM** y un **ensemble**. El modelo final
  seleccionado es **XGBoost**.
- **Fase *online* (prueba de concepto).** Un módulo de visión por computador detecta e
  identifica a los campeones en el minimapa en tiempo real mediante una **CNN**, como
  base para aplicar el asesor durante una partida en vivo.

---

## Requisitos

- **Python 3.10+**
- **Tesseract OCR** (motor externo, usado por `read_hud_stats.py`):
  [instalador](https://github.com/UB-Mannheim/tesseract)
- **Cliente de League of Legends** instalado (necesario para la extracción de
  repeticiones y para la detección en vivo).
- **Clave de la API de Riot Games** (gratuita en
  [developer.riotgames.com](https://developer.riotgames.com)) para los pasos de
  descarga y eventos.
- GPU opcional: el entrenamiento de la LSTM/CNN funciona también en CPU.

## Instalación

```bash
git clone https://github.com/<usuario>/TFG-Toma-de-decisiones-lol.git
cd TFG-Toma-de-decisiones-lol

python -m venv venv
venv\Scripts\activate        # Windows
# source venv/bin/activate   # Linux/Mac

pip install -r requirements.txt
```

Después, instala Tesseract OCR aparte (enlace arriba) y, si es necesario, ajusta la
clave de la API de Riot en los scripts que la usan (`01`, `02`).

---

## Estructura del repositorio

### Fase *offline* — construcción del dataset y modelado

| Archivo | Función |
|---|---|
| `01_dowload_replays.py` | Descarga repeticiones (`.rofl`) de jugadores de élite vía la API de Riot y el cliente |
| `02_extract_replay_data.py` | Reproduce cada repetición y extrae el estado de juego (cámara rotativa + OCR del HUD + eventos) |
| `03_prepare_ml_dataset.py` | Construye el dataset tabular y aplica el etiquetado automático de macro-acciones |
| `05_train_xgboost.py` | Entrena el modelo XGBoost (ajuste de hiperparámetros con Optuna) |
| `06_train_lstm.py` | Entrena la red LSTM |
| `08_ensemble_advisor.py` | Construye y evalúa el *ensemble* XGBoost + LSTM |
| `07_comparar_modelos.py` | Genera la tabla y los gráficos comparativos de resultados |
| `ml_common.py` | Limpieza, división por partidas y codificación comunes a todos los modelos |
| `lstm_model.py` | Arquitectura de la red LSTM |
| `zonas_mapa.py` | Polígonos del mapa y utilidades geométricas (test punto-en-polígono) |
| `read_hud_stats.py` | OCR del HUD para leer las estadísticas vitales (requiere `digit_templates/`) |
| `creador_zonas.html` | Herramienta para definir y calibrar las zonas del mapa |

### Fase *online* — detección en el minimapa

| Archivo | Función |
|---|---|
| `recolectar_datos.py` | Captura recortes del minimapa (modo `--auto`: autoetiqueta un campeón) |
| `entrenar.py` | Entrena la CNN de identificación de campeones |
| `detector_v2.py` | Detector de candidatos en el minimapa por color de aro (requiere `_assets/`) |
| `minimap_tracker.py` | Captura del minimapa de la pantalla |
| `ver_deteccion.py` | Detección e identificación en vivo (visualización) |
| `live_collector.py` | Recolector del estado de la partida en tiempo real |

### Datos

| Carpeta | Contenido |
|---|---|
| `ml_dataset/` | `dataset_completo.csv` — conjunto de datos final (entrada del modelado) |
| `dataset/` | CNN entrenada (`modelo.pt`, `classes.json`) y recortes etiquetados (`labeled/`) |
| `digit_templates/` | Plantillas de dígitos para el OCR del HUD |
| `_assets/` | Recursos del detector del minimapa (`estructuras.json`, `shield_tmpl.png`) |

---

## Uso

### Reproducir el entrenamiento (a partir del dataset incluido)

El conjunto de datos final ya está incluido en `ml_dataset/dataset_completo.csv`, de modo
que se puede reproducir el modelado directamente, sin pasar por la descarga y extracción
(que requieren repeticiones vigentes y el cliente del juego):

```bash
python 05_train_xgboost.py     # entrena XGBoost
python 06_train_lstm.py        # entrena la LSTM
python 08_ensemble_advisor.py  # evalúa el ensemble y guarda los resultados
python 07_comparar_modelos.py  # genera la tabla y los gráficos comparativos
```

### Detección en el minimapa en vivo

Con el juego abierto y una partida en curso:

```bash
python ver_deteccion.py
```

---

## Notas

- **Reproducibilidad de la fase de datos:** las repeticiones de Riot caducan a las pocas
  semanas y solo existen para el parche actual, por lo que los pasos `01` y `02` no
  pueden re-ejecutarse sobre partidas antiguas. El dataset ya procesado se incluye para
  permitir reproducir el resto del *pipeline*.
- **Rutas:** algunos scripts contienen rutas configurables al inicio del archivo; ajusta
  las que correspondan a tu instalación.
- Este proyecto se desarrolló con fines académicos. El uso de los datos y de los activos
  del juego se acoge a las API y catálogos públicos que Riot Games pone a disposición de
  los desarrolladores.

## Autor

Adrián San José González — Proyecto Fin de Grado, Universidad de Deusto, 2026.
