"""
live_collector.py — Recolector de datos en tiempo real de LoL
================================================================
Usa la API oficial de Riot (localhost:2999) para capturar el estado
de la partida cada 10 segundos y guardarlo en un JSON idéntico al
formato que ya procesa tu builder (03_prepare_ml_dataset.py).

USO:
    python live_collector.py

REQUISITOS:
    pip install requests

FUNCIONAMIENTO:
    1. Espera a que League of Legends esté en partida
    2. Empieza a capturar snapshots cada 10 segundos
    3. Guarda el JSON al terminar la partida (o al cerrar con Ctrl+C)
    4. El JSON generado es compatible con tu pipeline de ML

NOTAS:
    - La API localhost:2999 está oficialmente aprobada por Riot
    - Compatible con Vanguard anticheat
    - Los enemigos sin visión aparecen con coordenadas inválidas
      (igual que en los replays, tu get_pos() ya lo filtra)
"""

import requests
import json
import time
import os
import sys
from datetime import datetime
from pathlib import Path

# ── Minimap Tracker para coordenadas ─────────────────────────────
MINIMAP_ENABLED = False
try:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from minimap_tracker import (
        capture_minimap, get_all_positions, pixel_to_game,
        MINIMAP_X, MINIMAP_Y
    )
    MINIMAP_ENABLED = True
    print("[OK] minimap_tracker cargado - coordenadas activadas")
except ImportError as e:
    print(f"[!] minimap_tracker no disponible: {e}")
    print("    Las coordenadas seran null. Pon minimap_tracker.py en la misma carpeta.")

# ── Configuración ─────────────────────────────────────────────────
OUTPUT_DIR      = r"F:\replays_data_extracted_live"   # Mismo dir que los replays
POLL_INTERVAL   = 10.0   # Segundos entre snapshots (igual que replays)
RETRY_INTERVAL  = 3.0    # Segundos entre reintentos cuando no hay partida
MAX_RETRIES     = 5      # Reintentos antes de considerar que la partida acabó

BASE_URL        = "https://127.0.0.1:2999/liveclientdata"

# ── Endpoints ────────────────────────────────────────────────────
ENDPOINTS = {
    "allgamedata":  f"{BASE_URL}/allgamedata",
    "playerlist":   f"{BASE_URL}/playerlist",
    "activeplayer": f"{BASE_URL}/activeplayer",
}

# ── Colores para la consola ───────────────────────────────────────
class C:
    GREEN  = "\033[92m"
    YELLOW = "\033[93m"
    RED    = "\033[91m"
    BLUE   = "\033[94m"
    RESET  = "\033[0m"
    BOLD   = "\033[1m"

def log(msg, color=C.RESET):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"{color}[{ts}] {msg}{C.RESET}")

# ── Llamada a la API ─────────────────────────────────────────────
def fetch(endpoint_name, timeout=3):
    """
    Llama a un endpoint de la API local.
    Devuelve el JSON o None si no hay partida activa.
    """
    url = ENDPOINTS[endpoint_name]
    try:
        resp = requests.get(url, timeout=timeout, verify=False)
        if resp.status_code == 200:
            return resp.json()
        return None
    except requests.exceptions.ConnectionError:
        return None
    except requests.exceptions.Timeout:
        return None
    except Exception as e:
        log(f"Error inesperado en {endpoint_name}: {e}", C.RED)
        return None

# ── Esperar a que empiece la partida ────────────────────────────
def wait_for_game():
    """
    Hace polling hasta que detecta una partida activa.
    """
    log("Esperando a que empiece una partida de LoL...", C.YELLOW)
    log("(Abre el cliente y entra en una partida)", C.YELLOW)
    attempt = 0
    while True:
        data = fetch("allgamedata")
        if data and "gameData" in data:
            game_time = data["gameData"].get("gameTime", 0)
            if game_time > 0:
                log(f"¡Partida detectada! Tiempo: {game_time:.1f}s", C.GREEN)
                return data
        attempt += 1
        if attempt % 10 == 0:
            log(f"Aún esperando... ({attempt * RETRY_INTERVAL:.0f}s)", C.YELLOW)
        time.sleep(RETRY_INTERVAL)

# ── Construir snapshot en formato compatible con el builder ──────
def build_snapshot(allgamedata, playerlist, activeplayer, events_acumulados):
    """
    Construye un snapshot en el mismo formato que usan los JSON
    de replays, para que sea compatible con 03_prepare_ml_dataset.py.
    
    Campos mapeados:
    - game_time, game_data → de allgamedata.gameData
    - all_players → de playerlist (con stats vitales)
    - events → de allgamedata.events (acumulados)
    - oro_equipo_azul/rojo → calculado desde playerlist
    """
    if not allgamedata or not playerlist:
        return None

    game_data_raw = allgamedata.get("gameData", {})
    game_time     = game_data_raw.get("gameTime", 0)

    # ── Procesar jugadores ──────────────────────────────────────
    all_players = []
    oro_blue = 0
    oro_red  = 0

    for p in (playerlist or []):
        team = p.get("team", "")

        # Stats vitales desde el endpoint playerlist
        # La API devuelve championStats con todos los stats
        champ_stats = p.get("championStats", {})

        stats_vitales = {
            "currentHealth": champ_stats.get("currentHealth", 0),
            "maxHealth":     champ_stats.get("maxHealth", 0),
            "mana":          champ_stats.get("resourceValue", 0),
            "max_mana":      champ_stats.get("resourceMax", 0),
            "ad":            champ_stats.get("attackDamage", 0),
            "ap":            champ_stats.get("abilityPower", 0),
            "armor":         champ_stats.get("armor", 0),
            "mr":            champ_stats.get("magicResist", 0),
            "attack_speed":  champ_stats.get("attackSpeed", 0),
            "speed":         champ_stats.get("moveSpeed", 0),
        }

        # Posicion — la API NO devuelve coordenadas XY
        # Usamos el minimap_tracker para detectarlas via computer vision
        position_exact = None

        # Scores
        scores_raw = p.get("scores", {})
        scores = {
            "kills":       scores_raw.get("kills", 0),
            "deaths":      scores_raw.get("deaths", 0),
            "assists":     scores_raw.get("assists", 0),
            "creepScore":  scores_raw.get("creepScore", 0),
            "wardScore":   scores_raw.get("wardScore", 0.0),
            "gold":        p.get("currentGold", 0),
        }

        # Acumular oro por equipo
        gold = p.get("currentGold", 0)
        if team == "ORDER":
            oro_blue += gold
        else:
            oro_red += gold

        # Items
        items_raw = p.get("items", [])
        items = []
        for it in items_raw:
            items.append({
                "itemID":      it.get("itemID", 0),
                "displayName": it.get("displayName", ""),
                "price":       it.get("price", 0),
                "slot":        it.get("slot", 0),
                "count":       it.get("count", 1),
                "canUse":      it.get("canUse", False),
                "consumable":  it.get("consumable", False),
            })

        # Runes
        runes_raw = p.get("runes", {})
        runes = {
            "keystone": {
                "displayName": runes_raw.get("keystone", {}).get("displayName", ""),
                "id":          runes_raw.get("keystone", {}).get("id", 0),
            },
            "primaryRuneTree": {
                "displayName": runes_raw.get("primaryRuneTree", {}).get("displayName", ""),
                "id":          runes_raw.get("primaryRuneTree", {}).get("id", 0),
            },
            "secondaryRuneTree": {
                "displayName": runes_raw.get("secondaryRuneTree", {}).get("displayName", ""),
                "id":          runes_raw.get("secondaryRuneTree", {}).get("id", 0),
            },
        }

        # Summoner spells
        ss_raw = p.get("summonerSpells", {})
        summoner_spells = {
            "summonerSpellOne": {
                "displayName": ss_raw.get("summonerSpellOne", {}).get("displayName", ""),
            },
            "summonerSpellTwo": {
                "displayName": ss_raw.get("summonerSpellTwo", {}).get("displayName", ""),
            },
        }

        player_snap = {
            "championName":     p.get("championName", ""),
            "summonerName":     p.get("summonerName", ""),
            "riotId":           p.get("riotId", p.get("summonerName", "")),
            "riotIdGameName":   p.get("riotIdGameName", ""),
            "riotIdTagLine":    p.get("riotIdTagLine", ""),
            "team":             team,
            "position":         p.get("position", ""),
            "level":            p.get("level", 1),
            "isDead":           p.get("isDead", False),
            "respawnTimer":     p.get("respawnTimer", 0.0),
            "isBot":            p.get("isBot", False),
            "skinID":           p.get("skinID", 0),
            "skinName":         p.get("skinName", ""),
            "items":            items,
            "runes":            runes,
            "summonerSpells":   summoner_spells,
            "scores":           scores,
            "stats_vitales":    stats_vitales,
            "position_exact":   position_exact,
        }
        all_players.append(player_snap)

    # ── Diferencia de oro ───────────────────────────────────────
    diferencia_oro = abs(oro_blue - oro_red)
    if oro_blue > oro_red:
        equipo_ventaja = "ORDER (Azul)"
    elif oro_red > oro_blue:
        equipo_ventaja = "CHAOS (Rojo)"
    else:
        equipo_ventaja = "EMPATE"

    # ── Eventos ─────────────────────────────────────────────────
    # La API devuelve TODOS los eventos hasta ahora en cada llamada
    events_raw = allgamedata.get("events", {}).get("Events", [])
    events = []
    for ev in events_raw:
        event_name = ev.get("EventName", "")
        # Mapear al mismo formato que los replays
        event = {
            "EventName":    event_name,
            "EventTime":    ev.get("EventTime", 0),
            "EventID":      ev.get("EventID", 0),
        }
        # Campos específicos por tipo de evento
        if event_name == "ChampionKill":
            event["KillerName"]   = ev.get("KillerName", "")
            event["VictimName"]   = ev.get("VictimName", "")
            event["Assisters"]    = ev.get("Assisters", [])
            event["Bounty"]       = ev.get("Bounty", 0)
            event["ShutdownBounty"] = ev.get("ShutdownBounty", 0)

        elif event_name in ("DragonKill", "BaronKill", "HeraldKill"):
            event["KillerName"]   = ev.get("KillerName", "")
            event["Assisters"]    = ev.get("Assisters", [])
            event["Stolen"]       = ev.get("Stolen", False)
            # Mapear KillerTeamId desde el nombre del killer
            killer = ev.get("KillerName", "")
            killer_team = next(
                (p.get("team") for p in all_players
                 if p.get("riotIdGameName") == killer or p.get("summonerName") == killer),
                None
            )
            event["KillerTeamId"] = 100 if killer_team == "ORDER" else 200

        elif event_name in ("TowerKill", "TurretPlateDestroyed"):
            event["KillerName"]   = ev.get("KillerName", "")
            event["Assisters"]    = ev.get("Assisters", [])
            event["TurretKilled"] = ev.get("TurretKilled", "")

        elif event_name == "InhibitorKill":
            event["KillerName"]   = ev.get("KillerName", "")
            event["Assisters"]    = ev.get("Assisters", [])

        elif event_name == "EpicMonsterKill":
            event["KillerName"]   = ev.get("KillerName", "")
            event["MonsterType"]  = ev.get("MonsterType", "")
            killer = ev.get("KillerName", "")
            killer_team = next(
                (p.get("team") for p in all_players
                 if p.get("riotIdGameName") == killer or p.get("summonerName") == killer),
                None
            )
            event["KillerTeamId"] = 100 if killer_team == "ORDER" else 200

        elif event_name == "GameEnd":
            event["Result"]       = ev.get("Result", "")
            event["KillerTeamId"] = 100 if ev.get("Result") == "Win" else 200

        events.append(event)

    # ── Detectar equipo ganador (solo al final) ─────────────────
    equipo_ganador = None
    game_end_events = [e for e in events if e["EventName"] == "GameEnd"]
    if game_end_events:
        equipo_ganador = game_end_events[-1].get("KillerTeamId")

    # ── Snapshot final ──────────────────────────────────────────
    snapshot = {
        "game_time":       round(game_time, 2),
        "diferencia_oro":  diferencia_oro,
        "equipo_ventaja":  equipo_ventaja,
        "oro_equipo_azul": oro_blue,
        "oro_equipo_rojo": oro_red,
        "all_players":     all_players,
        "events":          events,
        "game_data": {
            "gameMode":    game_data_raw.get("gameMode", "CLASSIC"),
            "gameTime":    game_time,
            "mapName":     game_data_raw.get("mapName", "Map11"),
            "mapNumber":   game_data_raw.get("mapNumber", 11),
            "mapTerrain":  game_data_raw.get("mapTerrain", "Default"),
        },
        "equipo_ganador": equipo_ganador,
    }
    return snapshot

# ── Generar nombre de archivo ────────────────────────────────────
def get_output_path():
    """
    Genera un nombre de archivo con timestamp para el JSON de salida.
    Formato: LIVE_YYYYMMDD_HHMMSS_data.json
    """
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"LIVE_{ts}_data.json"
    return os.path.join(OUTPUT_DIR, filename)

# ── Guardar JSON ──────────────────────────────────────────────────
def save_json(snapshots, output_path, equipo_ganador=None):
    """
    Guarda todos los snapshots en un JSON compatible con el builder.
    Si conocemos el equipo ganador, lo inyectamos en el primer snapshot.
    """
    if equipo_ganador:
        for snap in snapshots:
            snap["equipo_ganador"] = equipo_ganador

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(snapshots, f, ensure_ascii=False, indent=2)
    return output_path

# ── Loop principal ────────────────────────────────────────────────
def main():
    print(f"\n{C.BOLD}{'='*60}{C.RESET}")
    print(f"{C.BOLD}  LoL Live Data Collector — TFG{C.RESET}")
    print(f"{C.BOLD}{'='*60}{C.RESET}")
    print(f"  Output dir: {OUTPUT_DIR}")
    print(f"  Intervalo:  {POLL_INTERVAL}s por snapshot")
    print(f"  API:        localhost:2999 (oficial Riot)")
    print(f"{C.BOLD}{'='*60}{C.RESET}\n")

    # Suprimir warnings de SSL (la API local usa HTTP, no HTTPS)
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    # Esperar a que empiece la partida
    wait_for_game()

    snapshots      = []
    output_path    = get_output_path()
    equipo_ganador = None
    fail_count     = 0
    snapshot_count = 0

    log(f"Capturando datos... (Ctrl+C para parar)", C.GREEN)
    log(f"Guardando en: {output_path}", C.BLUE)

    try:
        while True:
            # Fetch de los tres endpoints en paralelo
            allgamedata  = fetch("allgamedata")
            playerlist   = fetch("playerlist")
            activeplayer = fetch("activeplayer")

            if not allgamedata:
                fail_count += 1
                log(f"Sin respuesta de la API ({fail_count}/{MAX_RETRIES})", C.YELLOW)
                if fail_count >= MAX_RETRIES:
                    log("Partida terminada o cliente cerrado", C.YELLOW)
                    break
                time.sleep(RETRY_INTERVAL)
                continue

            fail_count = 0

            # Construir snapshot
            snap = build_snapshot(allgamedata, playerlist, activeplayer, snapshots)
            if snap:
                # ── Capturar posiciones del minimapa ──────────────
                if MINIMAP_ENABLED:
                    try:
                        # Extraer nombres de campeones para template matching
                        order_names = [p["championName"].replace(" ", "").replace("'", "")
                                       for p in snap["all_players"] if p["team"] == "ORDER"]
                        chaos_names = [p["championName"].replace(" ", "").replace("'", "")
                                       for p in snap["all_players"] if p["team"] == "CHAOS"]
                        player_names = {"ORDER": order_names, "CHAOS": chaos_names}

                        positions = get_all_positions(player_names=player_names)

                        # Si hay matches por template, asignar por nombre exacto
                        matches = positions.get("matches", None)
                        if matches:
                            name_to_pos = {}
                            for (champ_name, cx, cy, score) in matches:
                                from minimap_tracker import MINIMAP_X, MINIMAP_Y, pixel_to_game
                                px = MINIMAP_X + cx
                                py = MINIMAP_Y + cy
                                gx, gz = pixel_to_game(px, py)
                                name_to_pos[champ_name] = {"x": float(gx), "z": float(gz)}

                            for player in snap["all_players"]:
                                clean_name = player["championName"].replace(" ", "").replace("'", "")
                                if clean_name in name_to_pos:
                                    player["position_exact"] = name_to_pos[clean_name]
                        else:
                            # Fallback: asignar por orden de equipo
                            blue_pos = positions.get("ORDER", [])
                            red_pos  = positions.get("CHAOS", [])
                            order_idx, chaos_idx = 0, 0
                            for player in snap["all_players"]:
                                if player["team"] == "ORDER" and order_idx < len(blue_pos):
                                    x, z = blue_pos[order_idx]
                                    player["position_exact"] = {"x": float(x), "z": float(z)}
                                    order_idx += 1
                                elif player["team"] == "CHAOS" and chaos_idx < len(red_pos):
                                    x, z = red_pos[chaos_idx]
                                    player["position_exact"] = {"x": float(x), "z": float(z)}
                                    chaos_idx += 1
                    except Exception as e:
                        log(f"Error capturando minimap: {e}", C.YELLOW)

                snapshots.append(snap)
                snapshot_count += 1
                game_time = snap["game_time"]
                mins = int(game_time // 60)
                secs = int(game_time % 60)
                n_players = len(snap["all_players"])
                n_events  = len(snap["events"])
                # Contar posiciones detectadas
                n_pos = sum(1 for p in snap["all_players"] if p.get("position_exact"))
                log(
                    f"Snap #{snapshot_count:>3} | "
                    f"T={mins:02d}:{secs:02d} | "
                    f"Jugadores={n_players} | "
                    f"Pos={n_pos}/10 | "
                    f"Eventos={n_events} | "
                    f"Oro azul={snap['oro_equipo_azul']:,} rojo={snap['oro_equipo_rojo']:,}",
                    C.GREEN
                )

                # Detectar fin de partida
                game_end = [e for e in snap["events"] if e["EventName"] == "GameEnd"]
                if game_end:
                    equipo_ganador = game_end[-1].get("KillerTeamId")
                    log(f"¡Partida terminada! Ganador: equipo {equipo_ganador}", C.BLUE)
                    # Esperar un poco para capturar el último estado
                    time.sleep(5)
                    final = fetch("allgamedata")
                    if final:
                        final_snap = build_snapshot(final, fetch("playerlist"), None, snapshots)
                        if final_snap:
                            final_snap["equipo_ganador"] = equipo_ganador
                            snapshots.append(final_snap)
                    break

            time.sleep(POLL_INTERVAL)

    except KeyboardInterrupt:
        log("\nDetenido manualmente por el usuario", C.YELLOW)

    # Guardar siempre, aunque sea incompleto
    if snapshots:
        saved = save_json(snapshots, output_path, equipo_ganador)
        print(f"\n{C.BOLD}{'='*60}{C.RESET}")
        log(f"JSON guardado: {saved}", C.GREEN)
        log(f"Snapshots capturados: {snapshot_count}", C.GREEN)
        log(f"Duración registrada: {snapshots[-1]['game_time']:.0f}s "
            f"({snapshots[-1]['game_time']/60:.1f} min)", C.GREEN)
        if equipo_ganador:
            log(f"Equipo ganador: {equipo_ganador} "
                f"({'ORDER/Azul' if equipo_ganador==100 else 'CHAOS/Rojo'})", C.GREEN)
        else:
            log("Equipo ganador: desconocido (partida interrumpida)", C.YELLOW)
            log("El JSON se puede procesar igualmente pero sin label 'win'", C.YELLOW)
        print(f"{C.BOLD}{'='*60}{C.RESET}\n")
        log("Puedes procesar este archivo con 03_prepare_ml_dataset.py", C.BLUE)
    else:
        log("No se capturó ningún snapshot", C.RED)


if __name__ == "__main__":
    main()
