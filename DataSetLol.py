import requests
import pandas as pd
from urllib.parse import quote
import time




# === CONFIGURACIÓN ===
API_KEY = "RGAPI-60232fe0-9d77-4d6c-abc6-d633a2b9a562"  # pon tu API key actual
Region = "asia"
game_name = quote("Hide on bush")
tag_line = "KR1"


# === 1. Obtener PUUID del jugador ===
url = f"https://{Region}.api.riotgames.com/riot/account/v1/accounts/by-riot-id/{game_name}/{tag_line}"
headers = {"X-Riot-Token": API_KEY}


# Cache de datos de ítems (para mostrar nombres y costes de items)
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


def is_trinket(item_id: int) -> bool:
    """Devuelve True si el ítem es un trinket (ítem de visión)."""
    return item_id in [3340, 3363, 3364, 3362, 3361]




def infer_item_evolution(item_id: int, inventory: list, champion_name: str) -> None:
    """
    Heurística para inferir en qué ítem evolucionó Bounty of Worlds (3867),
    cuando el evento de compra no aparece en el timeline.
    """
    SUPPORT_EVOLUTIONS = {
        # Enchanters
        "Lulu": 3877, "Janna": 3877, "Nami": 3877, "Sona": 3877, "Seraphine": 3877, "Milio": 3877,
        # Engage/Tanks
        "Leona": 3876, "Nautilus": 3876, "Rell": 3876, "Alistar": 3876, "Blitzcrank": 3876, "Braum": 3876,
        # Mages poke
        "Zyra": 3870, "Brand": 3870, "Velkoz": 3870, "Xerath": 3870, "Karma": 3870,
        # Utility
        "Bard": 3871, "RenataGlasc": 3871, "Thresh": 3871,
        # Defensivos o visión
        "Senna": 3869, "Soraka": 3869,
    }


    DEFAULT_EVOLUTION = 3869
    evolved_item = SUPPORT_EVOLUTIONS.get(champion_name, DEFAULT_EVOLUTION)


    # Sustituir el ítem en inventario
    if item_id in inventory:
        inventory.remove(item_id)
    if evolved_item not in inventory and len(inventory) < 6:  # 6 slots normales
        inventory.append(evolved_item)


    print(f"🧭 Inferida evolución automática para {champion_name}: 3867 → {evolved_item}")




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
    participant_inventories = {i: [] for i in range(10)}
    participant_trinkets = {i: STEALTH_WARD_ID for i in range(10)}

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
                    for event in events:
                        event_type = event.get("type")
                        
                        # ⬅️ PROCESAR KILLS PRIMERO (no tienen itemId ni participantId como killer)
                        # Las kils estan mal pues hay campeones que tienen mas kills que las que se les asignan

                        if event_type == "CHAMPION_KILL":
                            killerId = event.get("killerId")
                            if killerId and 1 <= killerId <= 10:
                                participant_kills[killerId - 1] += 1
                            continue  # Siguiente evento
                        
                        # Para el resto de eventos, verificar participantId
                        participant_id = event.get("participantId")
                        if not participant_id or participant_id < 1 or participant_id > 10:
                            continue

                        p_idx = participant_id - 1
                        item_id = event.get("itemId")

                        # === GESTIÓN DE EVENTOS ===
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

                            # === Evoluciones conocidas ===
                            evolutions = {
                                3004: 3042,  # Manamune → Muramana
                                3003: 3040,  # Archangel's Staff → Seraph's Embrace
                                3865: 3866,  # World Atlas → Runic Compass
                                3866: 3867,  # Runic Compass → Bounty of Worlds
                                3010: 3013,  # symbiotic 
                            }

                            if item_id in evolutions:
                                new_id = evolutions[item_id]
                                if new_id not in participant_inventories[p_idx] and len(participant_inventories[p_idx]) < 6:
                                    participant_inventories[p_idx].append(new_id)
                                    print(f"🔄 Evolución directa: {item_id} → {new_id}")

                            elif item_id == 3867:  # Bounty of Worlds
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

                    # === Mostrar inventario y oro ===
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
                        deaths = pf.get("deaths", "N/A")
                        assists = pf.get("assists", "N/A")

                        inv_display = []
                        gold_spent = 0
                        for iid in participant_inventories[participant_id]:
                            info = item_lookup.get(str(iid), {})
                            name = info.get("name", f"Item_{iid}")
                            cost = info.get("gold", {}).get("total", 0)
                            inv_display.append(f"{name} ({cost})")
                            gold_spent += cost

                        # Añadir trinket (si existe)
                        trinket_id = participant_trinkets[participant_id]
                        if trinket_id:
                            info = item_lookup.get(str(trinket_id), {})
                            trinket_name = info.get("name", f"Item_{trinket_id}")
                            inv_display.append(f"{trinket_name} (Trinket)")

                        while len(inv_display) < 7:
                            inv_display.append("NA")

                        print(f"[{team_color}] {summoner_name} ({roll}) - {champ}")
                        print(f"  🏅 Nivel: {level}, K/D/A: {participant_kills[participant_id]}/{deaths}/{assists}, Posición: ({position.get('x')}, {position.get('y')})")
                        print(f"  💰 Total gastado: {gold_spent}g, Total oro: {gold}g")
                        print(f"  📦 Items: {' | '.join(inv_display)}\n")

            else:
                print(f"Modo {match_data['info'].get('gameMode', '?')} no es CLASSIC. Saltando.")

    time.sleep(1.2)