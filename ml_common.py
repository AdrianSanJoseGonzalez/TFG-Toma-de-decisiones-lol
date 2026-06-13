

from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.model_selection import train_test_split
from sklearn.preprocessing import LabelEncoder

BASE_DIR     = Path(__file__).resolve().parent
DATASET_PATH = BASE_DIR / "ml_dataset" / "dataset_completo.csv"
MODEL_DIR    = BASE_DIR / "ml_models"

RANDOM_STATE = 42
VAL_SIZE     = 0.15   
TEST_SIZE    = 0.15   

FUSION_MAP = {
    "RECALL_LOW_HP": "RECALL",
    "ROAM":          "MOVE",
    "PICK":          "SKIRMISH",
    "PUSH_TOWER":    "PUSH_STRUCTURE",
    "PUSH_INHIB":    "PUSH_STRUCTURE",
}


def load_clean_dataset(path=DATASET_PATH):
    """Carga el CSV y aplica la limpieza común a todos los modelos."""
    df = pd.read_csv(path)

    df = df[df["win"] == 1].copy()

    df["label"] = df["label"].replace(FUSION_MAP)

    df = df[df["label"] != "DEAD"].copy()

    absurdo = (
        ((df["role"] == "UTILITY") & (df["label"] == "SPLITPUSH")) |
        ((df["role"] != "JUNGLE") & (df["label"] == "GANK"))
    )
    df = df[~absurdo]

    return df


def split_games(df, val_size=VAL_SIZE, test_size=TEST_SIZE,
                random_state=RANDOM_STATE):
    
    all_games = np.sort(df["game_id"].unique())
    train_games, holdout = train_test_split(
        all_games, test_size=val_size + test_size, random_state=random_state
    )
    val_games, test_games = train_test_split(
        holdout, test_size=test_size / (val_size + test_size),
        random_state=random_state
    )
    return train_games, val_games, test_games


def encode_categoricals(dfs, cols):
    df_train = dfs[0]
    encoders = {}
    for col in cols:
        for d in dfs:
            d[col] = d[col].fillna("UNKNOWN").astype(str)

        le = LabelEncoder()
        le.fit(pd.concat([df_train[col], pd.Series(["UNKNOWN"])], ignore_index=True))
        known = set(le.classes_)

        for d in dfs:
            d[col] = le.transform(d[col].where(d[col].isin(known), "UNKNOWN"))
        encoders[col] = le
    return encoders


def apply_encoders(df, encoders):  
    for col, le in encoders.items():
        vals = df[col].fillna("UNKNOWN").astype(str)
        known = set(le.classes_)
        df[col] = le.transform(vals.where(vals.isin(known), "UNKNOWN"))
