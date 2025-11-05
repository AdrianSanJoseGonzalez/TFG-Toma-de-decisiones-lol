import requests
import os
import time

# === CONFIGURACIÓN ===
API_KEY = "RGAPI-c239fa40-be3e-4891-b9ee-ea35f0b5b8a9"  # tu API key
REGION_ROUTING = "asia"  # KR usa el cluster ASIA
REGION_PLATFORM = "KR"   # región del servidor
SAVE_PATH = "Replays"    # carpeta donde guardar los .rofl
NUM_MATCHES = 5          # nº de partidas por jugador

# === 1. Obtener lista de jugadores Challenger KR ===
print("Obteniendo lista de jugadores Challenger KR...")
url = f"https://{REGION_PLATFORM}.api.riotgames.com/lol/league/v4/challengerleagues/by-queue/RANKED_SOLO_5x5"
r = requests.get(url, headers={"X-Riot-Token": API_KEY})
r.raise_for_status()
players = r.json()["entries"]

# Extraer directamente los PUUIDs
puuids = [p["puuid"] for p in players[:10] if "puuid" in p]
print(f"✅ PUUIDs obtenidos: {len(puuids)}")

# === 2. Obtener partidas de cada PUUID ===
match_ids = set()
for puuid in puuids:
    url = f"https://{REGION_ROUTING}.api.riotgames.com/lol/match/v5/matches/by-puuid/{puuid}/ids?count={NUM_MATCHES}"
    r = requests.get(url, headers={"X-Riot-Token": API_KEY})
    r.raise_for_status()
    match_ids.update(r.json())
    time.sleep(1.2)  # evitar rate limit

print(f"Total de partidas obtenidas: {len(match_ids)}")

# === 3. Descargar los archivos .rofl ===
os.makedirs(SAVE_PATH, exist_ok=True)

for match_id in match_ids:
    print(f"Descargando replay: {match_id}")
    replay_url = f"https://{REGION_PLATFORM}.api.riotgames.com/lol/replay/v1/replays/{match_id}/download"
    response = requests.get(replay_url, headers={"X-Riot-Token": API_KEY}, stream=True)

    if response.status_code == 200:
        out_path = os.path.join(SAVE_PATH, f"{match_id}.rofl")
        with open(out_path, "wb") as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)
        print(f"✅ Guardado: {out_path}")
    else:
        print(f"❌ Error al descargar {match_id}: {response.status_code}")

    time.sleep(1.2)
