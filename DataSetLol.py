import requests
import pandas as pd
from urllib.parse import quote
import time
import math


# === CONFIGURACIÓN ===
API_KEY = "RGAPI-08879224-db48-4cd3-8017-173c367bb567"
Region = "asia"
game_name = quote("Hide on bush")
tag_line = "KR1"

# Parámetros de teamfight
TEAMFIGHT_DISTANCE = 3000  # Distancia en unidades del juego para considerar "cerca"
TEAMFIGHT_TIME_WINDOW = 10000  # Ventana de tiempo en ms (10 segundos)


# === 1. Obtener PUUID del jugador ===
url = f"https://{Region}.api.riotgames.com/riot/account/v1/accounts/by-riot-id/{game_name}/{tag_line}"
headers = {"X-Riot-Token": API_KEY}


# Cache de datos de ítems
DD_ITEM_URL = "https://ddragon.leagueoflegends.com/cdn/15.21.1/data/en_US/item.json"
try:
    _dd_resp = requests.get(DD_ITEM_URL)
    _dd_resp.raise_for_status()
    item_data = _dd_resp.json()
    item_lookup = item_data.get("data", {})
except Exception:
    item_lookup = {}


response = requests.get(url, headers=headers)
if response.status_code == 200:
    data = response.json()
    df = pd.DataFrame([data])
    puuid = df['puuid'].iloc[0]
    print("\n✅ Puuid guardado")
else:
    print(f"Error: {response.status_code}")
    exit()


# === FUNCIONES AUXILIARES ===

def calculate_distance(pos1, pos2):
    """Calcula la distancia euclidiana entre dos posiciones."""
    x1, y1 = pos1.get('x', 0), pos1.get('y', 0)
    x2, y2 = pos2.get('x', 0), pos2.get('y', 0)
    return math.sqrt((x2 - x1)**2 + (y2 - y1)**2)


def detect_teamfight(frame, recent_deaths, current_timestamp):
    """
    Detecta si hay un teamfight activo.
    Retorna un dict con participant_id: bool indicando si está en teamfight.
    """
    participant_frames = frame.get("participantFrames", {})
    
    # Separar jugadores por equipo
    team_100 = []  # Blue team
    team_200 = []  # Red team
    
    for pid_str, pf in participant_frames.items():
        pid = int(pid_str)
        team_id = 100 if pid <= 5 else 200
        position = pf.get("position", {"x": 0, "y": 0})
        
        if team_id == 100:
            team_100.append({"id": pid - 1, "pos": position})
        else:
            team_200.append({"id": pid - 1, "pos": position})
    
    # Contar cuántos jugadores de cada equipo están cerca entre sí
    def count_nearby_players(team_players):
        """Cuenta grupos de jugadores cercanos en un equipo."""
        if len(team_players) < 4:
            return []
        
        nearby_groups = []
        for i, player1 in enumerate(team_players):
            group = [player1["id"]]
            for j, player2 in enumerate(team_players):
                if i != j:
                    dist = calculate_distance(player1["pos"], player2["pos"])
                    if dist <= TEAMFIGHT_DISTANCE:
                        group.append(player2["id"])
            if len(group) >= 4:
                nearby_groups.append(group)
        return nearby_groups
    
    # Obtener grupos de jugadores cercanos
    blue_groups = count_nearby_players(team_100)
    red_groups = count_nearby_players(team_200)
    
    # Verificar si hay muertes recientes (dentro de la ventana de tiempo)
    recent_deaths_in_window = [
        d for d in recent_deaths 
        if current_timestamp - d["timestamp"] <= TEAMFIGHT_TIME_WINDOW
    ]
    
    # Determinar si hay teamfight
    teamfight_active = False
    participants_in_tf = set()
    
    if blue_groups and red_groups and len(recent_deaths_in_window) > 0:
        teamfight_active = True
        # Añadir todos los participantes de los grupos al teamfight
        for group in blue_groups:
            participants_in_tf.update(group)
        for group in red_groups:
            participants_in_tf.update(group)
    
    # Crear resultado para cada participante
    result = {}
    for i in range(10):
        result[i] = i in participants_in_tf
    
    return result


def is_trinket(item_id: int) -> bool:
    """Devuelve True si el ítem es un trinket (ítem de visión)."""
    return item_id in [3340, 3363, 3364, 3362, 3361]


def calculate_death_timer(level: int) -> float:
    """Calcula el tiempo de muerte en segundos basado en el nivel del campeón."""
    if level <= 6:
        return 4 + (2 * level)
    else:
        return 21 + (2.5 * (level - 6))


def infer_item_evolution(item_id: int, inventory: list, champion_name: str) -> None:
    """Heurística para inferir evolución de Bounty of Worlds (3867)."""
    SUPPORT_EVOLUTIONS = {
        "Lulu": 3877, "Janna": 3877, "Nami": 3877, "Sona": 3877, "Seraphine": 3877, "Milio": 3877,
        "Leona": 3876, "Nautilus": 3876, "Rell": 3876, "Alistar": 3876, "Blitzcrank": 3876, "Braum": 3876,
        "Zyra": 3870, "Brand": 3870, "Velkoz": 3870, "Xerath": 3870, "Karma": 3870,
        "Bard": 3871, "RenataGlasc": 3871, "Thresh": 3871,
        "Senna": 3869, "Soraka": 3869,
    }

    DEFAULT_EVOLUTION = 3869
    evolved_item = SUPPORT_EVOLUTIONS.get(champion_name, DEFAULT_EVOLUTION)

    if item_id in inventory:
        inventory.remove(item_id)
    if evolved_item not in inventory and len(inventory) < 6:
        inventory.append(evolved_item)

    print(f"Evolución automática para {champion_name}: 3867 → {evolved_item}")


# === 2. Obtener lista de partidas ===
MATCH_COUNT = 1
url_matches = f"https://{Region}.api.riotgames.com/lol/match/v5/matches/by-puuid/{puuid}/ids?start=0&count={MATCH_COUNT}"
resp_matches = requests.get(url_matches, headers=headers)
if resp_matches.status_code == 200:
    matches = resp_matches.json()
    print(f"\nIDs de partidas recientes ({len(matches)}): {matches}")
else:
    print(f"Error al obtener IDs de partidas: {resp_matches.status_code}")
    matches = []

STEALTH_WARD_ID = 3340

# === 3. Procesar cada partida ===
for match_index, match_id in enumerate(matches):
    print(f"\n--- Procesando partida {match_index + 1}/{len(matches)}: {match_id} ---")

    participant_kills = {i: 0 for i in range(10)}
    participant_deaths = {i: 0 for i in range(10)}
    participant_assists = {i: 0 for i in range(10)}
    participant_inventories = {i: [] for i in range(10)}
    participant_trinkets = {i: STEALTH_WARD_ID for i in range(10)}
    participant_death_info = {i: {"is_dead": False, "respawn_time": 0} for i in range(10)}
    recent_deaths = []  # Lista de muertes recientes para detectar teamfights

    url_timeline = f"https://{Region}.api.riotgames.com/lol/match/v5/matches/{match_id}/timeline"
    resp_timeline = requests.get(url_timeline, headers=headers)
    if resp_timeline.status_code != 200:
        print(f"No se pudo obtener timeline para {match_id}: {resp_timeline.status_code}")
        time.sleep(1.2)
        continue
    match_datatimeline = resp_timeline.json()

    url_match = f"https://{Region}.api.riotgames.com/lol/match/v5/matches/{match_id}"
    resp_match = requests.get(url_match, headers=headers)
    if resp_match.status_code != 200:
        print(f"No se pudo obtener datos de la partida {match_id}: {resp_match.status_code}")
        time.sleep(1.2)
        continue
    match_data = resp_match.json()

    if "info" in match_datatimeline and "frames" in match_datatimeline["info"]:
        frames = match_datatimeline["info"]["frames"]
        partidafinalSeg = match_data["info"].get("gameDuration", 0)
        partidaDuracionMax = partidafinalSeg // 60
        max_minute_index = min(partidaDuracionMax, len(frames) - 1)

        if max_minute_index >= 0:
            if match_data["info"].get("gameMode") == "CLASSIC":
                puuid_to_name = {}
                for p in match_data["info"]["participants"]:
                    p_puuid = p.get("puuid")
                    if p_puuid:
                        try:
                            r = requests.get(f"https://{Region}.api.riotgames.com/riot/account/v1/accounts/by-puuid/{p_puuid}", headers=headers)
                            if r.status_code == 200:
                                sd = r.json()
                                puuid_to_name[p_puuid] = f"{sd.get('gameName','Desconocido')}#{sd.get('tagLine','')}"
                            else:
                                puuid_to_name[p_puuid] = "Desconocido"
                        except Exception:
                            puuid_to_name[p_puuid] = "Desconocido"

                # === Procesar minuto a minuto ===
                for minute in range(0, max_minute_index + 1):
                    frame = frames[minute]
                    events = frame.get("events", [])
                    current_frame_timestamp = frame.get("timestamp", minute * 60000)
                    
                    for event in events:
                        event_type = event.get("type")
                        event_timestamp = event.get("timestamp", 0)
                        
                        # PROCESAR KILLS, DEATHS Y ASISTENCIAS
                        if event_type == "CHAMPION_KILL":
                            killerId = event.get("killerId")
                            if killerId and 1 <= killerId <= 10:
                                participant_kills[killerId - 1] += 1

                            assisting_ids = event.get("assistingParticipantIds", [])
                            for assist_id in assisting_ids:
                                if assist_id and 1 <= assist_id <= 10:
                                    participant_assists[assist_id - 1] += 1

                            victimId = event.get("victimId")
                            if victimId and 1 <= victimId <= 10:
                                victim_idx = victimId - 1
                                participant_deaths[victim_idx] += 1
                                
                                # Registrar muerte para detección de teamfight
                                recent_deaths.append({
                                    "timestamp": event_timestamp,
                                    "victim_id": victim_idx
                                })
                                
                                victim_frame = frame.get("participantFrames", {}).get(str(victimId), {})
                                victim_level = victim_frame.get("level", 1)
                                death_timer = calculate_death_timer(victim_level)
                                respawn_timestamp = event_timestamp + (death_timer * 1000)
                                
                                participant_death_info[victim_idx] = {
                                    "is_dead": True,
                                    "respawn_time": respawn_timestamp
                                }
                                
                            continue
                        
                        participant_id = event.get("participantId")
                        if not participant_id or participant_id < 1 or participant_id > 10:
                            continue

                        p_idx = participant_id - 1
                        item_id = event.get("itemId")

                        # GESTIÓN DE EVENTOS DE ITEMS
                        if event_type == "ITEM_PURCHASED":
                            if is_trinket(item_id):
                                participant_trinkets[p_idx] = item_id
                            elif item_id not in participant_inventories[p_idx] and len(participant_inventories[p_idx]) < 6:
                                participant_inventories[p_idx].append(item_id)

                        elif event_type == "ITEM_DESTROYED":
                            if is_trinket(item_id):
                                if participant_trinkets[p_idx] == item_id:
                                    participant_trinkets[p_idx] = STEALTH_WARD_ID
                            elif item_id in participant_inventories[p_idx]:
                                participant_inventories[p_idx].remove(item_id)

                            evolutions = {
                                3004: 3042, 3003: 3040, 3865: 3866,
                                3866: 3867, 3010: 3013,
                            }

                            if item_id in evolutions:
                                new_id = evolutions[item_id]
                                if new_id not in participant_inventories[p_idx] and len(participant_inventories[p_idx]) < 6:
                                    participant_inventories[p_idx].append(new_id)
                                    print(f"🔄 Evolución directa: {item_id} → {new_id}")

                            elif item_id == 3867:
                                champ_name = match_data["info"]["participants"][p_idx].get("championName", "?")
                                infer_item_evolution(item_id, participant_inventories[p_idx], champ_name)

                        elif event_type == "ITEM_SOLD":
                            if item_id in participant_inventories[p_idx]:
                                participant_inventories[p_idx].remove(item_id)

                        elif event_type == "ITEM_UNDO":
                            before_id = event.get("beforeId")
                            if before_id:
                                if is_trinket(before_id) and participant_trinkets[p_idx] == before_id:
                                    participant_trinkets[p_idx] = STEALTH_WARD_ID
                                elif before_id in participant_inventories[p_idx]:
                                    participant_inventories[p_idx].remove(before_id)

                    # Actualizar estados de respawn
                    for participant_id in range(10):
                        death_info = participant_death_info[participant_id]
                        if death_info["is_dead"] and current_frame_timestamp >= death_info["respawn_time"]:
                            participant_death_info[participant_id]["is_dead"] = False

                    # DETECTAR TEAMFIGHT
                    teamfight_status = detect_teamfight(frame, recent_deaths, current_frame_timestamp)

                    # Mostrar inventario y oro
                    print(f"\n💰 Oro al minuto {minute} en {match_data['info'].get('gameMode','?')} (match {match_id}):")
                    for participant_id in range(10):
                        pf = frame.get("participantFrames", {}).get(str(participant_id + 1), {})
                        gold = pf.get("totalGold", "N/A")
                        champ = match_data["info"]["participants"][participant_id].get("championName", "?")
                        puuidTot = match_data["info"]["participants"][participant_id].get("puuid")
                        roll = match_data["info"]["participants"][participant_id].get("teamPosition", "?")
                        teamId = match_data["info"]["participants"][participant_id].get("teamId", "?")
                        summoner_name = puuid_to_name.get(puuidTot, "Desconocido")
                        team_color = "Blue" if int(teamId) == 100 else "Red" if int(teamId) == 200 else str(teamId)
                        level = pf.get("level", "N/A")
                        position = pf.get("position", {"x": "N/A", "y": "N/A"})
                        
                        is_dead = participant_death_info[participant_id]["is_dead"]
                        status_emoji = "💀" if is_dead else "✅"
                        status_text = "MUERTO" if is_dead else "VIVO"
                        
                        # ESTADO DE TEAMFIGHT
                        in_teamfight = teamfight_status[participant_id]
                        tf_emoji = "⚔️" if in_teamfight else "🛡️"
                        tf_text = "EN TEAMFIGHT" if in_teamfight else "No teamfight"
                        
                        respawn_info = ""
                        if is_dead:
                            time_to_respawn = (participant_death_info[participant_id]["respawn_time"] - current_frame_timestamp) / 1000
                            respawn_info = f" (respawn en {time_to_respawn:.1f}s)"

                        inv_display = []
                        gold_spent = 0
                        for iid in participant_inventories[participant_id]:
                            info = item_lookup.get(str(iid), {})
                            name = info.get("name", f"Item_{iid}")
                            cost = info.get("gold", {}).get("total", 0)
                            inv_display.append(f"{name} ({cost})")
                            gold_spent += cost

                        trinket_id = participant_trinkets[participant_id]
                        if trinket_id:
                            info = item_lookup.get(str(trinket_id), {})
                            trinket_name = info.get("name", f"Item_{trinket_id}")
                            inv_display.append(f"{trinket_name} (Trinket)")

                        while len(inv_display) < 7:
                            inv_display.append("NA")

                        print(f"[{team_color}] {summoner_name} ({roll}) - {champ}")
                        print(f"  {status_emoji} Estado: {status_text}{respawn_info}")
                        print(f"  {tf_emoji} Teamfight: {tf_text}")
                        print(f"  🏅 Nivel: {level}, K/D/A: {participant_kills[participant_id]}/{participant_deaths[participant_id]}/{participant_assists[participant_id]}, Posición: ({position.get('x')}, {position.get('y')})")
                        print(f"  💰 Total gastado: {gold_spent}g, Total oro: {gold}g")
                        print(f"  📦 Items: {' | '.join(inv_display)}\n")
                
                # ESTADO FINAL DE LA PARTIDA
                print(f"\n{'='*80}")
                print(f"🏁 ESTADO FINAL DE LA PARTIDA (Minuto {max_minute_index + 1}) - {match_id}")
                print(f"{'='*80}\n")
                
                for participant_id in range(10):
                    participant = match_data["info"]["participants"][participant_id]
                    
                    final_kills = participant.get("kills", 0)
                    final_deaths = participant.get("deaths", 0)
                    final_assists = participant.get("assists", 0)
                    final_gold = participant.get("goldEarned", 0)
                    final_level = participant.get("champLevel", "N/A")
                    
                    champ = participant.get("championName", "?")
                    puuidTot = participant.get("puuid")
                    roll = participant.get("teamPosition", "?")
                    teamId = participant.get("teamId", "?")
                    summoner_name = puuid_to_name.get(puuidTot, "Desconocido")
                    team_color = "Blue" if int(teamId) == 100 else "Red" if int(teamId) == 200 else str(teamId)
                    
                    final_items = [
                        participant.get("item0", 0), participant.get("item1", 0),
                        participant.get("item2", 0), participant.get("item3", 0),
                        participant.get("item4", 0), participant.get("item5", 0),
                    ]
                    final_trinket = participant.get("item6", 0)
                    
                    inv_display = []
                    gold_spent = 0
                    for iid in final_items:
                        if iid != 0:
                            info = item_lookup.get(str(iid), {})
                            name = info.get("name", f"Item_{iid}")
                            cost = info.get("gold", {}).get("total", 0)
                            inv_display.append(f"{name} ({cost})")
                            gold_spent += cost
                    
                    if final_trinket != 0:
                        info = item_lookup.get(str(final_trinket), {})
                        trinket_name = info.get("name", f"Item_{final_trinket}")
                        inv_display.append(f"{trinket_name} (Trinket)")
                    
                    while len(inv_display) < 7:
                        inv_display.append("NA")
                    
                    print(f"[{team_color}] {summoner_name} ({roll}) - {champ}")
                    print(f"  🏅 Nivel: {final_level}, K/D/A: {final_kills}/{final_deaths}/{final_assists}")
                    print(f"  💰 Total gastado: {gold_spent}g, Total oro ganado: {final_gold}g")
                    print(f"  📦 Items: {' | '.join(inv_display)}\n")

            else:
                print(f"Modo {match_data['info'].get('gameMode', '?')} no es CLASSIC. Saltando.")

    time.sleep(1.2)