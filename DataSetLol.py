import requests
import pandas as pd
from urllib.parse import quote
import time

# === CONFIGURACIÓN ===
API_KEY = "RGAPI-b83f6027-ea87-4976-a0a7-328a4a593945"  # pon tu API key actual
Region = "asia"  # cambia según la región de routing: americas / asia / europe / sea
game_name = quote("Hide on bush")
tag_line = "KR1"  # el tag de riot id, si usas endpoint riot id

# === 1. Obtener PUUID del jugador ===
url = f"https://{Region}.api.riotgames.com/riot/account/v1/accounts/by-riot-id/{game_name}/{tag_line}"
headers = {"X-Riot-Token": API_KEY}

response = requests.get(url, headers=headers)
if response.status_code == 200:
    data = response.json()
    # Convertir la respuesta a un DataFrame de pandas
    df = pd.DataFrame([data])
    # Extraer el puuid como variable
    puuid = df['puuid'].iloc[0]  # Obtiene el primer (y único) valor de la columna puuid
    print("\nPuuid guardado")
else:
    print(f"Error: {response.status_code}")

# === 2. Obtener lista de partidas ===
# Número de partidas a solicitar (ajústalo si quieres más/menos)
MATCH_COUNT = 1
url_matches = f"https://{Region}.api.riotgames.com/lol/match/v5/matches/by-puuid/{puuid}/ids?start=0&count={MATCH_COUNT}&api_key={API_KEY}"
resp_matches = requests.get(url_matches)
if resp_matches.status_code == 200:
    matches = resp_matches.json()
    print(f"\nIDs de partidas recientes ({len(matches)}): {matches}")
else:
    print(f"Error al obtener IDs de partidas: {resp_matches.status_code}")
    matches = []

# === 3. Procesar cada partida obtenida ===
for match_index, match_id in enumerate(matches):
    print(f"\n--- Procesando partida {match_index + 1}/{len(matches)}: {match_id} ---")

    # Timeline
    url_timeline = f"https://{Region}.api.riotgames.com/lol/match/v5/matches/{match_id}/timeline?api_key={API_KEY}"
    resp_timeline = requests.get(url_timeline)
    if resp_timeline.status_code != 200:
        print(f"No se pudo obtener timeline para {match_id}: {resp_timeline.status_code}")
        # Respeta rate limit mínimo antes de continuar con la siguiente
        time.sleep(1.2)
        continue
    match_datatimeline = resp_timeline.json()

    # Match data
    url_match = f"https://{Region}.api.riotgames.com/lol/match/v5/matches/{match_id}?api_key={API_KEY}"
    resp_match = requests.get(url_match)
    if resp_match.status_code != 200:
        print(f"No se pudo obtener datos de la partida {match_id}: {resp_match.status_code}")
        time.sleep(1.2)
        continue
    match_data = resp_match.json()

    # 4️⃣ Obtener oro por minuto hasta la duración de la partida
    if "info" in match_datatimeline and "frames" in match_datatimeline["info"]:
        frames = match_datatimeline["info"]["frames"]
        partidafinalSeg = match_data["info"].get("gameDuration", 0)
        partidaDuracionMax = partidafinalSeg // 60
        # limitar al número de frames disponibles
        max_minute_index = min(partidaDuracionMax, len(frames) - 1)

        if max_minute_index >= 0:
            if match_data["info"].get("gameMode") == "CLASSIC":
                # cachear nombres por puuid para no pedirlos en cada minuto
                puuid_to_name = {}
                for p in match_data["info"]["participants"]:
                    p_puuid = p.get("puuid")
                    if p_puuid:
                        try:
                            r = requests.get(f"https://{Region}.api.riotgames.com/riot/account/v1/accounts/by-puuid/{p_puuid}?api_key={API_KEY}")
                            if r.status_code == 200:
                                sd = r.json()
                                puuid_to_name[p_puuid] = f"{sd.get('gameName','Desconocido')}#{sd.get('tagLine','')}"
                            else:
                                puuid_to_name[p_puuid] = "Desconocido"
                        except Exception:
                            puuid_to_name[p_puuid] = "Desconocido"

                # iterar por cada minuto desde 0 hasta max_minute_index
                for minute in range(0, max_minute_index + 1):
                    frame = frames[minute]
                    print(f"\nOro de cada jugador al minuto {minute} en {match_data['info'].get('gameMode','Desconocido')} (match {match_id}):")
                    for participant_id in range(10):
                        participantFrames = frame.get("participantFrames", {})
                        pf = participantFrames.get(str(participant_id + 1), {})
                        gold = pf.get("totalGold", "N/A")
                        champ = match_data["info"]["participants"][participant_id].get("championName", "?")
                        puuidTot = match_data["info"]["participants"][participant_id].get("puuid")
                        roll = match_data["info"]["participants"][participant_id].get("teamPosition", "?")
                        teamId = match_data["info"]["participants"][participant_id].get("teamId", "?")

                        summoner_name = puuid_to_name.get(puuidTot, "Desconocido")

                        # Mapear teamId a color
                        team_color = "Desconocido"
                        try:
                            if int(teamId) == 100:
                                team_color = "blue"
                            elif int(teamId) == 200:
                                team_color = "red"
                        except Exception:
                            team_color = str(teamId)

                        print(f" {summoner_name}/{roll} [{team_color}] ({champ}): {gold} de oro")
            else:
                print(f"\nEl modo de juego {match_data['info'].get('gameMode', 'Desconocido')} no es CLASSIC. Saltando extracción de oro por minuto.")
        else:
            print(f"No hay suficientes frames en timeline para extraer datos por minuto (frames={len(frames)})")
    else:
        print("Timeline no contiene 'info' o 'frames'. No se puede extraer datos por minuto.")

    # Pausa entre partidas para evitar rate limits
    time.sleep(1.2)