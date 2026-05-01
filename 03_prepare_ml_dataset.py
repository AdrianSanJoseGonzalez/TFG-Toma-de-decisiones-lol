"""
03_prepare_ml_dataset.py — Generador de dataset ML para IA de LoL
Extrae TODA la información disponible de los JSON de replays.
Genera: ml_dataset/dataset_completo.csv
"""
import json, math, glob, os
import pandas as pd
import numpy as np
from pathlib import Path
from zonas_mapa import (
    MAP_X_MIN, MAP_X_MAX, MAP_Z_MIN, MAP_Z_MAX,
    DRAGON_PIT, BARON_PIT, ZONAS_MAPA, ZONA_CATEGORIA,
    dist_2d, norm, obtener_zona,
)

INPUT_DIR = r"F:\replays_data_extracted"
OUTPUT_FILE = r"C:\Users\Adrian\.gemini\antigravity\scratch\lol_replay_downloader\ml_dataset\dataset_completo.csv"

# ── helpers ──────────────────────────────────────────────────────────
def get_pos(player):
    p = player.get("position_exact")
    if p and "x" in p and "z" in p and not isinstance(p.get("x"), str):
        return p["x"], p["z"]
    return None

def count_nearby(player, all_players, same_team, radius=2500):
    pos = get_pos(player)
    if not pos: return 0
    px, pz = pos
    count = 0
    for p in all_players:
        if p.get("riotId") == player.get("riotId"): continue
        if (p["team"] == player["team"]) != same_team: continue
        ep = get_pos(p)
        if ep and dist_2d(px, pz, ep[0], ep[1]) < radius:
            count += 1
    return count

def nearest_dist(player, all_players, same_team):
    pos = get_pos(player)
    if not pos: return 99999
    px, pz = pos
    best = 99999
    for p in all_players:
        if p.get("riotId") == player.get("riotId"): continue
        if (p["team"] == player["team"]) != same_team: continue
        ep = get_pos(p)
        if ep:
            d = dist_2d(px, pz, ep[0], ep[1])
            if d < best: best = d
    return best

def items_gold(player):
    return sum(item.get("price", 0) for item in player.get("items", []))

def has_item_type(player, item_ids):
    return int(any(i.get("itemID") in item_ids for i in player.get("items", [])))

# IDs de items clave
BOOTS_IDS = {1001, 3006, 3047, 3111, 3158, 3020, 3009, 3117, 3115}
MYTHIC_IDS = {6655,6656,6657,6662,6671,6672,6673,6691,6692,6693,3001,3068,3078,3084,3190,4005,6620,6621,6630,6631,6632}

# ── Etiquetado de acciones ────────────────────────────────────────
OBJECTIVE_RADIUS = 2500
TEAMFIGHT_RADIUS = 2500

def get_action_label(player, all_players, all_events, t_now, t_next, kda_deltas):
    """
    Etiqueta la acción del jugador usando:
    - kda_deltas: dict {player_name: (dk, dd, da)} cambios de KDA vs snapshot anterior
    - all_events: eventos de la línea temporal (DragonKill, BaronKill, ChampionKill)
    - Posiciones relativas de todos los jugadores
    """
    pos = get_pos(player)
    is_dead = player.get("isDead", False) or player.get("respawnTimer", 0) > 0

    obj_win = [e for e in all_events if e["EventName"] in ("DragonKill", "BaronKill")
               and t_now < e.get("EventTime", 0) <= t_next]
               
    kills_win = [e for e in all_events if e["EventName"] == "ChampionKill"
                 and t_now < e.get("EventTime", 0) <= t_next]
                 
    tower_win = [e for e in all_events if e["EventName"] in ("TowerKill", "TurretPlateDestroyed")
                 and t_now < e.get("EventTime", 0) <= t_next]

    if is_dead or pos is None:
        return "DEAD"

    px, pz = pos
    p_name = player.get("riotIdGameName") or player.get("summonerName", "")
    role = player.get("position", "").upper()
    zona = obtener_zona(px, pz)
    cat = ZONA_CATEGORIA.get(zona, "OTHER")

    # Proximidad
    allies = count_nearby(player, all_players, True, TEAMFIGHT_RADIUS)
    enemies = count_nearby(player, all_players, False, TEAMFIGHT_RADIUS)

    # ¿Hubo acción violenta en este snapshot? (alguien cercano mató o murió)
    my_delta = kda_deltas.get(p_name, (0, 0, 0))
    my_involved_delta = (my_delta[0] > 0 or my_delta[1] > 0 or my_delta[2] > 0)
    
    my_involved_event = any(
        e.get("KillerName") == p_name or p_name in e.get("Assisters", []) or e.get("VictimName") == p_name
        for e in kills_win
    )
    my_involved = my_involved_delta or my_involved_event

    # Contar cuántos jugadores cercanos tuvieron cambios de KDA
    fighters_nearby = 0
    for p in all_players:
        ep = get_pos(p)
        if not ep: continue
        if dist_2d(px, pz, ep[0], ep[1]) < TEAMFIGHT_RADIUS:
            pn = p.get("riotIdGameName") or p.get("summonerName", "")
            d = kda_deltas.get(pn, (0, 0, 0))
            if d[0] > 0 or d[1] > 0 or d[2] > 0:
                fighters_nearby += 1

    fight_nearby = (fighters_nearby >= 2) or (len(kills_win) >= 1)

    # 1. CONTEST_OBJECTIVE — cerca de Drake/Baron cuando se mata uno
    if obj_win:
        if dist_2d(px, pz, *DRAGON_PIT) < OBJECTIVE_RADIUS or \
           dist_2d(px, pz, *BARON_PIT) < OBJECTIVE_RADIUS:
            return "CONTEST_OBJECTIVE"

    # 1.5 PUSH_TOWER — cerca de una torre cuando se destruye
    if tower_win:
        my_involved_tower = any(
            e.get("KillerName") == p_name or p_name in e.get("Assisters", [])
            for e in tower_win
        )
        if my_involved_tower:
            return "PUSH_TOWER"

    # 2. TEAMFIGHT — ≥2 aliados + ≥2 enemigos agrupados con acción de KDA
    if allies >= 2 and enemies >= 2 and fight_nearby:
        return "TEAMFIGHT"

    # 3. GANK — jungla en zona de lane con enemigo cerca y acción, min <= 20
    game_min = t_now / 60.0
    if role == "JUNGLE" and cat == "LANE" and enemies >= 1 and my_involved and game_min <= 20:
        return "GANK"

    # 4. ROAM — no-jungla fuera de su lane con acción, min <= 20
    if role != "JUNGLE" and cat != "LANE" and my_involved and enemies >= 1 and game_min <= 20:
        return "ROAM"

    # 5. PICK / CAZADA — (Mid/Late game) 1 enemigo solo contra varios, o viceversa
    if game_min >= 15 and fight_nearby:
        if (allies >= 1 and enemies == 1) or (allies == 0 and enemies >= 2):
            return "PICK"

    # 6. SOLO_KILL — 1v1 sin nadie más cerca
    if fight_nearby and allies == 0 and enemies == 1:
        return "SOLO_KILL"

    # 7. SKIRMISH (pelea pequeña) — Cualquier otra pelea que no sea Teamfight, Pick o Solo Kill
    if allies >= 1 and enemies >= 1 and fight_nearby:
        return "SKIRMISH"

    # 8. RECALL / RECALL_LOW_HP — en base
    if (px < 1500 and pz < 1500) or (px > 13500 and pz > 13500):
        sv = player.get("stats_vitales", {})
        cur_hp = sv.get("currentHealth") or 0
        max_hp = sv.get("maxHealth") or 1
        hp_pct = cur_hp / max_hp
        if hp_pct < 0.30:
            return "RECALL_LOW_HP"
        return "RECALL"

    # 9. SPLITPUSH — solo en lane enemiga sin aliados (Mid/Late game, min >= 15)
    if cat == "LANE" and allies == 0 and enemies == 0 and game_min >= 15:
        # Verificar si está en lado enemigo del mapa
        team = player.get("team", "")
        in_enemy_side = (team == "ORDER" and (px + pz) > 15000) or \
                        (team == "CHAOS" and (px + pz) < 15000)
        if in_enemy_side and player.get("level", 1) >= 6:
            return "SPLITPUSH"

    # 10. FARM — en lane
    if cat == "LANE":
        return "FARM"

    # 11. MOVE — cualquier otra cosa (rotando, en jungla, etc.)
    return "MOVE"

# ── Procesamiento principal ──────────────────────────────────────
def process_game(json_path):
    with open(json_path, 'r', encoding='utf-8') as f:
        data = json.load(f)

    if not data: return []

    game_id = Path(json_path).stem
    all_events = [ev for snap in data for ev in snap.get("events", [])]
    winner_team_id = data[0].get("equipo_ganador")

    rows = []
    prev_snap_players = {}  # Para calcular deltas

    for i, snap in enumerate(data):
        t_now = snap["game_time"]
        t_next = data[i+1]["game_time"] if i+1 < len(data) else t_now + 10
        all_players = snap.get("all_players", [])
        game_min = t_now / 60.0

        # Calcular KDA deltas (cambios desde el último snapshot)
        kda_deltas = {}
        for p in all_players:
            p_name = p.get("riotIdGameName") or p.get("summonerName", "")
            sc = p.get("scores", {})
            k = sc.get("kills", 0)
            d = sc.get("deaths", 0)
            a = sc.get("assists", 0)
            
            if p_name in prev_snap_players:
                pk, pd2, pa = prev_snap_players[p_name]
                kda_deltas[p_name] = (max(0, k - pk), max(0, d - pd2), max(0, a - pa))
            else:
                kda_deltas[p_name] = (0, 0, 0)
                
            prev_snap_players[p_name] = (k, d, a)

        # Datos de equipo
        oro_blue = snap.get("oro_equipo_azul", 0)
        oro_red = snap.get("oro_equipo_rojo", 0)
        diff_oro = oro_blue - oro_red  # Positivo = ventaja blue

        # Stats de equipo
        team_stats = {"ORDER": {"gold": 0, "kills": 0, "deaths": 0, "cs": 0, "levels": [], "count": 0},
                      "CHAOS": {"gold": 0, "kills": 0, "deaths": 0, "cs": 0, "levels": [], "count": 0}}
        for p in all_players:
            t = p.get("team", "")
            if t in team_stats:
                sc = p.get("scores", {})
                team_stats[t]["gold"] += sc.get("gold", 0)
                team_stats[t]["kills"] += sc.get("kills", 0)
                team_stats[t]["deaths"] += sc.get("deaths", 0)
                team_stats[t]["cs"] += sc.get("creepScore", 0)
                team_stats[t]["levels"].append(p.get("level", 1))
                team_stats[t]["count"] += 1

        for p in all_players:
            pos = get_pos(p)
            scores = p.get("scores", {})
            sv = p.get("stats_vitales", {})
            team = p.get("team", "")
            p_team_id = 100 if team == "ORDER" else 200
            role = p.get("position", "")
            champ = p.get("championName", "")
            p_name = p.get("riotIdGameName") or p.get("summonerName", "")

            # Posición
            px = pos[0] if pos else 0
            pz = pos[1] if pos else 0
            zona = obtener_zona(px, pz) if pos else "Muerto"
            zona_cat = ZONA_CATEGORIA.get(zona, "OTHER")

            # Stats vitales
            cur_hp = sv.get("currentHealth") or 0
            max_hp = sv.get("maxHealth") or 0
            hp_pct = cur_hp / max_hp if max_hp > 0 else 0

            # Recurso (mana/energia/furia) — buscar la key que exista
            res_cur = sv.get("mana") or sv.get("energia") or sv.get("furia") or 0
            res_max = sv.get("max_mana") or sv.get("max_energia") or sv.get("max_furia") or 0
            res_pct = res_cur / res_max if res_max and res_max > 0 else 0

            # Stats de combate
            ad = sv.get("ad") or 0
            ap = sv.get("ap") or 0
            armor = sv.get("armor") or 0
            mr = sv.get("mr") or 0
            atk_speed = sv.get("attack_speed") or 0
            move_speed = sv.get("speed") or 0

            # KDA
            kills = scores.get("kills", 0)
            deaths = scores.get("deaths", 0)
            assists = scores.get("assists", 0)
            cs = scores.get("creepScore", 0)
            gold = scores.get("gold", 0)
            ward_score = scores.get("wardScore", 0)

            # Eficiencia temporal
            cs_per_min = cs / game_min if game_min > 0.5 else 0
            gold_per_min = gold / game_min if game_min > 0.5 else 0

            # Conteos de cercanía
            allies_near = count_nearby(p, all_players, True)
            enemies_near = count_nearby(p, all_players, False)
            dist_nearest_ally = nearest_dist(p, all_players, True)
            dist_nearest_enemy = nearest_dist(p, all_players, False)

            # Distancias a objetivos
            dist_drake = dist_2d(px, pz, *DRAGON_PIT) if pos else 99999
            dist_baron = dist_2d(px, pz, *BARON_PIT) if pos else 99999

            # Items — individuales por slot + resumen
            items_list = p.get("items", [])
            item_by_slot = {}
            for it in items_list:
                item_by_slot[it.get("slot", -1)] = it.get("itemID", 0)
            item_0 = item_by_slot.get(0, 0)
            item_1 = item_by_slot.get(1, 0)
            item_2 = item_by_slot.get(2, 0)
            item_3 = item_by_slot.get(3, 0)
            item_4 = item_by_slot.get(4, 0)
            item_5 = item_by_slot.get(5, 0)
            trinket = item_by_slot.get(6, 0)
            item_quest = item_by_slot.get(7, 0)  # Slot 7: botas quest ADC (S15)
            n_items = len([it for it in items_list if it.get("slot", 7) < 6])
            if item_quest: n_items += 1  # Contar bota quest como item real
            total_item_gold = items_gold(p)
            has_boots = has_item_type(p, BOOTS_IDS)

            # Estado muerto (fuente de verdad: API)
            is_dead = int(p.get("isDead", False) or p.get("respawnTimer", 0) > 0)
            respawn_timer = p.get("respawnTimer", 0)

            # Runes y Summoner Spells
            runes = p.get("runes", {})
            keystone = runes.get("keystone", {}).get("displayName", "")
            primary_tree = runes.get("primaryRuneTree", {}).get("displayName", "")
            secondary_tree = runes.get("secondaryRuneTree", {}).get("displayName", "")

            ss = p.get("summonerSpells", {})
            spell1 = ss.get("summonerSpellOne", {}).get("displayName", "")
            spell2 = ss.get("summonerSpellTwo", {}).get("displayName", "")

            # Contexto de equipo
            my_team = team_stats.get(team, {})
            enemy_team_key = "CHAOS" if team == "ORDER" else "ORDER"
            enemy_team = team_stats.get(enemy_team_key, {})

            team_total_gold = my_team.get("gold", 0)
            team_total_kills = my_team.get("kills", 0)
            team_avg_level = np.mean(my_team.get("levels", [1])) if my_team.get("levels") else 1
            enemy_total_gold = enemy_team.get("gold", 0)
            enemy_total_kills = enemy_team.get("kills", 0)
            enemy_avg_level = np.mean(enemy_team.get("levels", [1])) if enemy_team.get("levels") else 1

            # Gold share (% de oro del equipo que tiene este jugador)
            gold_share = gold / team_total_gold if team_total_gold > 0 else 0.2

            # Kill participation
            kp = (kills + assists) / team_total_kills if team_total_kills > 0 else 0

            # Gold diff individual (vs avg del equipo enemigo)
            enemy_avg_gold = enemy_total_gold / max(enemy_team.get("count", 1), 1)
            my_gold_diff = gold - enemy_avg_gold

            # Label y win
            win = 1 if winner_team_id == p_team_id else 0
            label = get_action_label(p, all_players, all_events, t_now, t_next, kda_deltas)

            row = {
                # Identificadores
                "game_id": game_id,
                "game_time": round(t_now, 1),
                "champion": champ,
                "role": role,
                "team": team,
                "win": win,

                # Temporal
                "game_min": round(game_min, 2),
                "time_norm": round(norm(t_now, 0, 2400), 4),

                # Posición
                "x": round(px, 1),
                "z": round(pz, 1),
                "x_norm": round(norm(px, MAP_X_MIN, MAP_X_MAX), 4),
                "z_norm": round(norm(pz, MAP_Z_MIN, MAP_Z_MAX), 4),
                "zona": zona,
                "zona_categoria": zona_cat,

                # Nivel
                "level": p.get("level", 1),
                "level_norm": round(norm(p.get("level", 1), 1, 18), 4),

                # Stats vitales
                "hp_pct": round(hp_pct, 3),
                "resource_pct": round(res_pct, 3),
                "current_hp": cur_hp,
                "max_hp": max_hp,
                "current_resource": res_cur if isinstance(res_cur, (int, float)) else 0,
                "max_resource": res_max if isinstance(res_max, (int, float)) else 0,

                # Stats combate
                "ad": ad if isinstance(ad, (int, float)) else 0,
                "ap": ap if isinstance(ap, (int, float)) else 0,
                "armor": armor if isinstance(armor, (int, float)) else 0,
                "mr": mr if isinstance(mr, (int, float)) else 0,
                "attack_speed": atk_speed if isinstance(atk_speed, (int, float)) else 0,
                "move_speed": move_speed if isinstance(move_speed, (int, float)) else 0,

                # KDA y scoring
                "kills": kills,
                "deaths": deaths,
                "assists": assists,
                "cs": cs,
                "gold": gold,
                "ward_score": round(ward_score, 1),
                "cs_per_min": round(cs_per_min, 2),
                "gold_per_min": round(gold_per_min, 1),
                "kill_participation": round(kp, 3),

                # Estado
                "is_dead": is_dead,
                "respawn_timer": round(respawn_timer, 1),

                # Cercanía
                "allies_nearby": allies_near,
                "enemies_nearby": enemies_near,
                "dist_nearest_ally": round(min(dist_nearest_ally, 15000), 0),
                "dist_nearest_enemy": round(min(dist_nearest_enemy, 15000), 0),

                # Distancias objetivos
                "dist_drake": round(min(dist_drake, 20000), 0),
                "dist_baron": round(min(dist_baron, 20000), 0),

                # Items
                "item_0": item_0,
                "item_1": item_1,
                "item_2": item_2,
                "item_3": item_3,
                "item_4": item_4,
                "item_5": item_5,
                "trinket": trinket,
                "item_quest": item_quest,
                "n_items": n_items,
                "item_gold_total": total_item_gold,
                "has_boots": has_boots,

                # Economía equipo
                "team_gold": team_total_gold,
                "enemy_gold": enemy_total_gold,
                "gold_diff_team": team_total_gold - enemy_total_gold,
                "gold_diff_individual": round(my_gold_diff, 0),
                "gold_share": round(gold_share, 3),

                # Nivel equipo
                "team_avg_level": round(team_avg_level, 2),
                "enemy_avg_level": round(enemy_avg_level, 2),
                "team_kills": team_total_kills,
                "enemy_kills": enemy_total_kills,

                # Runes & Spells
                "keystone": keystone,
                "primary_tree": primary_tree,
                "secondary_tree": secondary_tree,
                "spell1": spell1,
                "spell2": spell2,

                # Map info
                "map_terrain": snap.get("game_data", {}).get("mapTerrain", "Default"),

                # Label
                "label": label,
            }
            rows.append(row)

    return rows

# ── Main ────────────────────────────────────────────────────────────
def main():
    os.makedirs(os.path.dirname(OUTPUT_FILE), exist_ok=True)
    files = sorted(glob.glob(os.path.join(INPUT_DIR, "*.json")))
    print(f"[INFO] Encontrados {len(files)} archivos JSON en {INPUT_DIR}")

    if not files:
        print("[ERROR] No se encontraron archivos JSON.")
        return

    all_data = []
    for idx, f in enumerate(files):
        name = os.path.basename(f)
        print(f"  [{idx+1}/{len(files)}] Procesando {name}...", end=" ")
        try:
            rows = process_game(f)
            rows = [r for r in rows if r["label"] != "END_GAME"]
            all_data.extend(rows)
            print(f"{len(rows)} filas")
        except Exception as e:
            print(f"ERROR: {e}")

    df = pd.DataFrame(all_data)
    df.to_csv(OUTPUT_FILE, index=False)

    print(f"\n{'='*60}")
    print(f"[OK] Dataset generado: {OUTPUT_FILE}")
    print(f"     Filas totales: {len(df):,}")
    print(f"     Partidas:      {df['game_id'].nunique()}")
    print(f"     Columnas:      {len(df.columns)}")
    print(f"     Campeones:     {df['champion'].nunique()}")
    print(f"\n--- Distribución de Labels ---")
    print(df["label"].value_counts().to_string())
    print(f"\n--- Distribución de Zonas (top 10) ---")
    print(df["zona"].value_counts().head(10).to_string())
    print(f"\n--- HP pct stats (solo vivos) ---")
    alive = df[df["is_dead"] == 0]
    print(f"     Media:  {alive['hp_pct'].mean():.3f}")
    print(f"     > 0:    {(alive['hp_pct'] > 0).sum()} / {len(alive)}")
    print(f"\n--- Columnas generadas ---")
    for c in df.columns:
        print(f"     {c}")

if __name__ == "__main__":
    main()
