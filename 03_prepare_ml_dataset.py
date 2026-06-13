

import json, math, glob, os
import pandas as pd
import numpy as np
from pathlib import Path
from zonas_mapa import (
    MAP_X_MIN, MAP_X_MAX, MAP_Z_MIN, MAP_Z_MAX,
    DRAGON_PIT, BARON_PIT, TOWER_POSITIONS, ZONAS_MAPA, ZONA_CATEGORIA,
    dist_2d, norm, obtener_zona,
)

BASE_DIR    = Path(__file__).resolve().parent
INPUT_DIR   = str(BASE_DIR / "replays_data_extracted")
OUTPUT_FILE = str(BASE_DIR / "ml_dataset" / "dataset_completo.csv")

GRUBS_SPAWN     = 300    
GRUBS_DESPAWN   = 840    
HERALD_SPAWN    = 840    
HERALD_DESPAWN  = 1185   
BARON_SPAWN     = 1200   
DRAGON_RESPAWN  = 300    
BARON_RESPAWN   = 360   

# ── IDs de items clave ────────────────────────────────────────────
BOOTS_IDS  = {1001, 3006, 3047, 3111, 3158, 3020, 3009, 3117, 3115, 3008}

# ── helpers ──────────────────────────────────────────────────────
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
        if p.get("isDead", False): continue # No contamos a los muertos
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
        if p.get("isDead", False): continue # Mismo criterio que count_nearby
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

def nearest_tower_dist(px, pz, towers_team):
    """Distancia a la torre VIVA más cercana de un equipo (dict lane->posiciones)."""
    best = 99999
    for tower_list in towers_team.values():
        for (tx, tz) in tower_list:
            d = dist_2d(px, pz, tx, tz)
            if d < best: best = d
    return best

# ── Etiquetado de acciones ────────────────────────────────────────
OBJECTIVE_RADIUS  = 800   
TEAMFIGHT_RADIUS  = 2500

PUSH_BACKFILL_WINDOW = 45.0          
PUSH_RELABEL_SAFE    = {"FARM", "MOVE"}  

def get_action_label(player, all_players, all_events, t_now, t_next,
                     kda_deltas, hp_deltas):
    pos     = get_pos(player)
    is_dead = player.get("isDead", False) or player.get("respawnTimer", 0) > 0

    if is_dead or pos is None:
        return "DEAD"

    px, pz   = pos
    champ_name = player.get("championName", "")
    role     = player.get("position", "").upper()
    team     = player.get("team", "")
    zona     = obtener_zona(px, pz)
    cat      = ZONA_CATEGORIA.get(zona, "OTHER")
    game_min = t_now / 60.0

    # ── Ventana de eventos futuros ──────────────────────────────
    kills_win  = [e for e in all_events if e["EventName"] == "ChampionKill"
                  and t_now < e.get("EventTime", 0) <= t_next]
    tower_win  = [e for e in all_events if e["EventName"] in
                  ("TowerKill", "TurretPlateDestroyed")
                  and t_now < e.get("EventTime", 0) <= t_next]
    inhib_win  = [e for e in all_events if e["EventName"] == "InhibitorKill"
                  and t_now < e.get("EventTime", 0) <= t_next]
    dragon_win = [e for e in all_events if e["EventName"] == "DragonKill"
                  and t_now < e.get("EventTime", 0) <= t_next]
    baron_win  = [e for e in all_events if e["EventName"] == "BaronKill"
                  and t_now < e.get("EventTime", 0) <= t_next]
    herald_win = [e for e in all_events if e["EventName"] == "HeraldKill"
                  and t_now < e.get("EventTime", 0) <= t_next]
    grubs_win  = [e for e in all_events if e["EventName"] == "EpicMonsterKill"
                  and t_now < e.get("EventTime", 0) <= t_next
                  and e.get("EventTime", 0) < GRUBS_DESPAWN]

    # ── Involucración del jugador ───────────────────────────────
    p_name           = player.get("riotIdGameName") or player.get("summonerName", "")
    my_delta         = kda_deltas.get(p_name, (0, 0, 0))
    my_involved_kda  = any(d > 0 for d in my_delta)
    my_took_damage   = hp_deltas.get(p_name, 0) < -0.10
    
    my_in_kill       = any(
        e.get("KillerName") == champ_name or
        champ_name in e.get("Assisters", []) or
        e.get("VictimName") == champ_name
        for e in kills_win
    )
    my_involved = my_involved_kda or my_in_kill or my_took_damage

    # ── Contexto de pelea ───────────────────────────────────────
    allies  = count_nearby(player, all_players, True,  TEAMFIGHT_RADIUS)
    enemies = count_nearby(player, all_players, False, TEAMFIGHT_RADIUS)

    fighters_nearby = 0
    for p in all_players:
        ep = get_pos(p)
        if not ep: continue
        if dist_2d(px, pz, ep[0], ep[1]) < TEAMFIGHT_RADIUS:
            pn = p.get("riotIdGameName") or p.get("summonerName", "")
            d  = kda_deltas.get(pn, (0, 0, 0))
            h  = hp_deltas.get(pn, 0)
            if any(x > 0 for x in d) or h < -0.10:
                fighters_nearby += 1

    fight_nearby = (fighters_nearby >= 2) or (len(kills_win) >= 1)

    # ── Distancias a objetivos ──────────────────────────────────
    dist_drake = dist_2d(px, pz, *DRAGON_PIT)
    dist_baron = dist_2d(px, pz, *BARON_PIT)

    # ÁRBOL DE DECISIÓN DE LABELS

    # 1. PUSH_INHIB — jugador involucrado en destruir inhibidor
    my_in_inhib = any(
        e.get("KillerName") == champ_name or champ_name in e.get("Assisters", [])
        for e in inhib_win
    )
    if my_in_inhib:
        return "PUSH_INHIB"

    # 2. CONTEST_GRUBS — cerca del Barón Pit antes del min 14
    if dist_baron < OBJECTIVE_RADIUS and t_now < GRUBS_DESPAWN:
        return "CONTEST_GRUBS"

    # 3. CONTEST_HERALD — cerca del Barón Pit entre min 14 y 20
    if dist_baron < OBJECTIVE_RADIUS and HERALD_SPAWN <= t_now < BARON_SPAWN:
        return "CONTEST_HERALD"

    # 4. CONTEST_OBJECTIVE — cerca de Dragón o Barón (min 20+)
    if dist_drake < OBJECTIVE_RADIUS:
        return "CONTEST_OBJECTIVE"
    if dist_baron < OBJECTIVE_RADIUS and t_now >= BARON_SPAWN:
        return "CONTEST_OBJECTIVE"

    # 5. PUSH_TOWER — involucrado en destruir torre sin pelea activa
    my_in_tower = any(
        e.get("KillerName") == champ_name or champ_name in e.get("Assisters", [])
        for e in tower_win
    )
    if my_in_tower and cat == "LANE":
        return "PUSH_TOWER"

    # 6. TEAMFIGHT — grupo grande con pelea (mínimo 3v3)
    if allies >= 3 and enemies >= 3 and fight_nearby:
        return "TEAMFIGHT"

    # 7. GANK — jungler en lane con enemigos y acción
    if role == "JUNGLE" and cat == "LANE" and enemies >= 1 and my_involved:
        return "GANK"

    # 8. ROAM — no-jungler fuera de lane con acción, early game
    if role != "JUNGLE" and cat != "LANE" and my_involved and enemies >= 1 and game_min <= 20:
        return "ROAM"

    # 9. PICK — cazada (mid/late game): varios vs 1 o 1 vs varios
    if game_min >= 15 and fight_nearby:
        if (allies >= 1 and enemies == 1) or (allies == 0 and enemies >= 2):
            return "PICK"

    # 10. SOLO_KILL — 1v1 limpio
    if fight_nearby and allies == 0 and enemies == 1:
        return "SOLO_KILL"

    # 11. SKIRMISH — pelea pequeña no clasificada arriba
    if allies >= 1 and enemies >= 1 and fight_nearby:
        return "SKIRMISH"

    # 12. RECALL / RECALL_LOW_HP — en base
    in_base = (px < 1500 and pz < 1500) or (px > 13500 and pz > 13500)
    if in_base:
        sv     = player.get("stats_vitales", {})
        cur_hp = sv.get("currentHealth") or 0
        max_hp = sv.get("maxHealth") or 1
        hp_pct = cur_hp / max_hp
        return "RECALL_LOW_HP" if hp_pct < 0.30 else "RECALL"

    # 13. SPLITPUSH — solo en lane enemiga, min 15+, sin jungla
    if (cat == "LANE" and allies == 0 and enemies == 0
            and game_min >= 15 and role != "JUNGLE"):
        in_enemy = (team == "ORDER" and (px + pz) > 15000) or \
                   (team == "CHAOS" and (px + pz) < 15000)
        if in_enemy and player.get("level", 1) >= 6:
            return "SPLITPUSH"

    # 14. FARM — en lane sin nada especial
    if cat == "LANE":
        return "FARM"

    # 15. MOVE — cualquier otra cosa
    return "MOVE"


# ── Procesamiento de una partida ─────────────────────────────────
def process_game(json_path):
    with open(json_path, 'r', encoding='utf-8') as f:
        data = json.load(f)

    if not data: return []

    game_id   = Path(json_path).stem
    last_time = data[-1].get("game_time", 0)

    if last_time < 900:
        print(f" [Omitida: {last_time/60:.1f} min]", end="")
        return []

    all_events     = [ev for snap in data for ev in snap.get("events", [])]
    winner_team_id = data[0].get("equipo_ganador")

    # ── Estado acumulativo de la partida ────────────────────────
    dragon_order = dragon_chaos = 0
    elder_order  = elder_chaos  = 0
    soul_order   = soul_chaos   = False
    grubs_order  = grubs_chaos  = 0
    last_dragon_time = last_baron_time = last_grubs_time = last_herald_time = 0
    baron_order = baron_chaos = False
    herald_order = herald_chaos = False
    towers_order = towers_chaos = 0      # Torres destruidas POR cada equipo (total)
    inhibs_order = inhibs_chaos = 0      # Inhibidores destruidos POR cada equipo
    # Torres por lane: cuántas torres ha destruido cada equipo en cada carril
    towers_order_lane = {"TOP_LANE": 0, "MID_LANE": 0, "BOT_LANE": 0}
    towers_chaos_lane = {"TOP_LANE": 0, "MID_LANE": 0, "BOT_LANE": 0}
    # Torres vivas por equipo (copia mutable de las posiciones fijas)
    towers_alive = {
        team_id: {lane: list(positions) for lane, positions in lanes.items()}
        for team_id, lanes in TOWER_POSITIONS.items()
    }
    seen_events = set()

    prev_players = {}  
    prev_hp      = {} 

    # ── 1. Determinar el final de la partida para limpiar ruido ──
    max_time = 0
    for snap in data:
        t = snap.get("game_time")
        if isinstance(t, (int, float)) and t > max_time:
            max_time = t
    
    end_threshold = max_time - 10.0 

    rows = []

    for i, snap in enumerate(data):
        t_now      = snap["game_time"]
        if t_now == 0 or t_now > end_threshold: continue 
        
        t_next     = data[i+1]["game_time"] if i+1 < len(data) else t_now + 10
        all_players = snap.get("all_players", [])
        game_min   = t_now / 60.0

        # ── Procesar eventos del frame ──────────────────────────
        for ev in snap.get("events", []):
            key = (ev.get("EventName"), round(ev.get("EventTime", 0), 2))
            if key in seen_events: continue
            seen_events.add(key)

            ename = ev.get("EventName")
            etime = ev.get("EventTime", t_now)
            tid   = ev.get("KillerTeamId", 0)

            if ename == "DragonKill":
                # Lógica: Si alguien ya tiene el alma, el siguiente es Elder
                if soul_order or soul_chaos:
                    if tid == 100: elder_order += 1
                    else:          elder_chaos += 1
                else:
                    # Dragón normal
                    if tid == 100:
                        dragon_order += 1
                        if dragon_order >= 4: soul_order = True
                    else:
                        dragon_chaos += 1
                        if dragon_chaos >= 4: soul_chaos = True
                last_dragon_time = etime

            elif ename == "EpicMonsterKill" and etime < GRUBS_DESPAWN:
                if tid == 100: grubs_order += 1
                else:          grubs_chaos += 1
                last_grubs_time = etime

            elif ename == "HeraldKill":
                last_herald_time = etime
                if tid == 100: herald_order, herald_chaos = True, False
                else:          herald_order, herald_chaos = False, True

            elif ename == "BaronKill":
                last_baron_time = etime
                if tid == 100: baron_order, baron_chaos = True, False
                else:          baron_order, baron_chaos = False, True

            elif ename == "TowerKill":
                # TeamId = equipo de la torre destruida (no el que la destruyó)
                tower_team = ev.get("TeamId", 0)
                lane = ev.get("LaneType", "")
                if tower_team == 100:   # CHAOS destruyó torre de ORDER
                    towers_chaos += 1
                    if lane in towers_chaos_lane: towers_chaos_lane[lane] += 1
                elif tower_team == 200: # ORDER destruyó torre de CHAOS
                    towers_order += 1
                    if lane in towers_order_lane: towers_order_lane[lane] += 1

                if tower_team in towers_alive:
                    lane_towers = towers_alive[tower_team].get(lane)
                    if lane_towers:
                        lane_towers.pop(0)
                    elif towers_alive[tower_team]["NEXUS"]:
                        towers_alive[tower_team]["NEXUS"].pop(0)

            elif ename == "InhibitorKill":
                inhib_team = ev.get("TeamId", 0)
                if inhib_team == 100:   inhibs_chaos += 1
                elif inhib_team == 200: inhibs_order += 1

        # Expirar buffs
        if t_now - last_baron_time > 180:
            baron_order = baron_chaos = False

        # ── Timing de objetivos ─────────────────────────────────
        t_dragon = t_now - last_dragon_time
        dragon_disponible   = 1 if t_dragon >= DRAGON_RESPAWN else 0
        tiempo_hasta_dragon = max(0, DRAGON_RESPAWN - t_dragon)

        t_baron = t_now - last_baron_time
        baron_disponible    = 1 if (t_now >= BARON_SPAWN and t_baron >= BARON_RESPAWN) else 0
        tiempo_hasta_baron  = max(0, BARON_RESPAWN - t_baron)

        herald_disponible   = 1 if HERALD_SPAWN <= t_now < HERALD_DESPAWN else 0
        grubs_disponibles   = 1 if GRUBS_SPAWN  <= t_now < GRUBS_DESPAWN  else 0

        # ── KDA deltas ──────────────────────────────────────────
        kda_deltas = {}
        for p in all_players:
            pn = p.get("riotIdGameName") or p.get("summonerName", "")
            sc = p.get("scores", {})
            k, d, a = sc.get("kills",0), sc.get("deaths",0), sc.get("assists",0)
            if pn in prev_players:
                pk, pd2, pa = prev_players[pn]
                kda_deltas[pn] = (max(0,k-pk), max(0,d-pd2), max(0,a-pa))
            else:
                kda_deltas[pn] = (0, 0, 0)
            prev_players[pn] = (k, d, a)

        # ── HP deltas ───────────────────────────────────────────
        hp_deltas = {}
        for p in all_players:
            pn  = p.get("riotIdGameName") or p.get("summonerName", "")
            sv  = p.get("stats_vitales", {})
            mhp = sv.get("maxHealth") or 1
            chp = sv.get("currentHealth") or 0
            hp  = chp / mhp if mhp > 0 else 0
            hp_deltas[pn] = hp - prev_hp.get(pn, hp)
            prev_hp[pn]   = hp

        # ── Stats de equipo ─────────────────────────────────────
        team_stats = {
            "ORDER": {"gold":0,"kills":0,"cs":0,"levels":[],"count":0,"dead_count":0,"respawn_total":0},
            "CHAOS": {"gold":0,"kills":0,"cs":0,"levels":[],"count":0,"dead_count":0,"respawn_total":0},
        }
        for p in all_players:
            t = p.get("team","")
            if t not in team_stats: continue
            sc = p.get("scores",{})
            team_stats[t]["gold"]   += sc.get("gold",0)
            team_stats[t]["kills"]  += sc.get("kills",0)
            team_stats[t]["cs"]     += sc.get("creepScore",0)
            team_stats[t]["levels"].append(p.get("level",1))
            team_stats[t]["count"]  += 1
            resp = p.get("respawnTimer", 0) or 0
            if p.get("isDead",False) or resp > 0:
                team_stats[t]["dead_count"] += 1
                team_stats[t]["respawn_total"] += resp

        # ── Fila por jugador ────────────────────────────────────
        for p in all_players:
            pos    = get_pos(p)
            scores = p.get("scores",{})
            sv     = p.get("stats_vitales",{})
            team   = p.get("team","")
            role   = p.get("position","")
            champ  = p.get("championName","")
            p_name = p.get("riotIdGameName") or p.get("summonerName","")

            px = pos[0] if pos else 0
            pz = pos[1] if pos else 0
            zona     = obtener_zona(px, pz) if pos else "Muerto"
            zona_cat = ZONA_CATEGORIA.get(zona, "OTHER")

            # Vitales
            mhp     = sv.get("maxHealth") or 0
            chp     = sv.get("currentHealth") or 0
            hp_pct  = chp / mhp if mhp > 0 else 0
            res_cur = sv.get("mana") or sv.get("energia") or sv.get("furia") or 0
            res_max = sv.get("max_mana") or sv.get("max_energia") or sv.get("max_furia") or 0
            res_pct = res_cur / res_max if res_max and res_max > 0 else 0

            # KDA
            kills    = scores.get("kills",0)
            deaths   = scores.get("deaths",0)
            assists  = scores.get("assists",0)
            cs       = scores.get("creepScore",0)
            gold     = scores.get("gold",0)
            ward_score = scores.get("wardScore",0)

            cs_per_min   = cs   / game_min if game_min > 0.5 else 0
            gold_per_min = gold / game_min if game_min > 0.5 else 0

            allies_near       = count_nearby(p, all_players, True)
            enemies_near      = count_nearby(p, all_players, False)
            dist_near_ally    = nearest_dist(p, all_players, True)
            dist_near_enemy   = nearest_dist(p, all_players, False)

            dist_drake = dist_2d(px, pz, *DRAGON_PIT) if pos else 99999
            dist_baron = dist_2d(px, pz, *BARON_PIT)  if pos else 99999

            ally_team_id  = 100 if team == "ORDER" else 200
            enemy_team_id = 200 if team == "ORDER" else 100
            dist_torre_enemiga = nearest_tower_dist(px, pz, towers_alive[enemy_team_id]) if pos else 99999
            dist_torre_aliada  = nearest_tower_dist(px, pz, towers_alive[ally_team_id])  if pos else 99999

            # Items
            items_list  = p.get("items",[])
            item_by_slot = {it.get("slot",-1): it.get("itemID",0) for it in items_list}
            n_items     = len([it for it in items_list if it.get("slot",7) < 6])
            item_quest  = item_by_slot.get(7, 0)
            if item_quest: n_items += 1
            total_item_gold = items_gold(p)
            pocket_gold     = max(0, gold - total_item_gold)
            has_boots       = has_item_type(p, BOOTS_IDS)

            is_dead        = int(p.get("isDead",False) or p.get("respawnTimer",0) > 0)
            respawn_timer  = p.get("respawnTimer",0)

            # Runes & Spells
            runes    = p.get("runes",{})
            keystone = runes.get("keystone",{}).get("displayName","")
            prim     = runes.get("primaryRuneTree",{}).get("displayName","")
            sec      = runes.get("secondaryRuneTree",{}).get("displayName","")
            ss       = p.get("summonerSpells",{})
            spell1   = ss.get("summonerSpellOne",{}).get("displayName","")
            spell2   = ss.get("summonerSpellTwo",{}).get("displayName","")

            # Equipo
            my_team    = team_stats.get(team,{})
            enemy_key  = "CHAOS" if team == "ORDER" else "ORDER"
            enemy_team = team_stats.get(enemy_key,{})

            team_gold  = my_team.get("gold",0)
            team_kills = my_team.get("kills",0)
            levels_list = my_team.get("levels", [])
            team_avg_level = np.mean(levels_list) if levels_list else 1.0
            
            enemy_gold      = enemy_team.get("gold",0)
            enemy_kills     = enemy_team.get("kills",0)
            enemy_levels    = enemy_team.get("levels", [])
            enemy_avg_level = np.mean(enemy_levels) if enemy_levels else 1.0

            allies_dead  = max(0, my_team.get("dead_count",0) - is_dead)
            enemies_dead = enemy_team.get("dead_count",0)

            gold_share = gold / team_gold if team_gold > 0 else 0.2
            kp = (kills + assists) / team_kills if team_kills > 0 else 0
            enemy_avg_gold = enemy_gold / max(enemy_team.get("count",1),1)
            gold_diff_ind  = gold - enemy_avg_gold

            # Label
            p_team_id = 100 if team == "ORDER" else 200
            win       = 1 if winner_team_id == p_team_id else 0
            label     = get_action_label(
                p, all_players, all_events, t_now, t_next, kda_deltas, hp_deltas
            )

            row = {
                # Identificadores
                "game_id":   game_id,
                "game_time": round(t_now, 1),
                "champion":  champ,
                "role":      role,
                "team":      team,
                "win":       win,

                # Temporal
                "game_min":  round(game_min, 2),
                "time_norm": round(norm(t_now, 0, 2400), 4),

                # Posición
                "x":              round(px, 1),
                "z":              round(pz, 1),
                "x_norm":         round(norm(px, MAP_X_MIN, MAP_X_MAX), 4),
                "z_norm":         round(norm(pz, MAP_Z_MIN, MAP_Z_MAX), 4),
                "zona":           zona,
                "zona_categoria": zona_cat,
                "dist_al_centro": round(dist_2d(px, pz, 7500, 7500), 0),

                # Nivel
                "level":      p.get("level",1),
                "level_norm": round(norm(p.get("level",1), 1, 18), 4),

                # Vitales
                "hp_pct":           round(hp_pct, 3),
                "resource_pct":     round(res_pct, 3),
                "current_hp":       chp,
                "max_hp":           mhp,
                "current_resource": res_cur if isinstance(res_cur,(int,float)) else 0,
                "max_resource":     res_max if isinstance(res_max,(int,float)) else 0,

                # Combate
                "ad":           sv.get("ad") or 0,
                "ap":           sv.get("ap") or 0,
                "armor":        sv.get("armor") or 0,
                "mr":           sv.get("mr") or 0,
                "attack_speed": sv.get("attack_speed") or 0,
                "move_speed":   sv.get("speed") or 0,

                # KDA
                "kills":             kills,
                "deaths":            deaths,
                "assists":           assists,
                "cs":                cs,
                "cs_delta":          0,  # calculado abajo con pandas
                "hp_delta":          0,  # calculado abajo con pandas
                "gold":              gold,
                "pocket_gold":       pocket_gold,
                "ward_score":        round(ward_score, 1),
                "cs_per_min":        round(cs_per_min, 2),
                "gold_per_min":      round(gold_per_min, 1),
                "kill_participation": round(kp, 3),

                # Estado
                "is_dead":       is_dead,
                "respawn_timer": round(respawn_timer, 1),
                "allies_dead":   allies_dead,
                "enemies_dead":  enemies_dead,

                # Cercanía
                "allies_nearby":     allies_near,
                "enemies_nearby":    enemies_near,
                "dist_nearest_ally":  round(min(dist_near_ally,  15000), 0),
                "dist_nearest_enemy": round(min(dist_near_enemy, 15000), 0),

                # Distancias objetivos
                "dist_drake": round(min(dist_drake, 20000), 0),
                "dist_baron": round(min(dist_baron, 20000), 0),
                "dist_torre_enemiga": round(min(dist_torre_enemiga, 20000), 0),
                "dist_torre_aliada":  round(min(dist_torre_aliada, 20000), 0),

                # ── OBJETIVOS ÉPICOS ────────────────────────────
                "dragon_aliados":     dragon_order if team=="ORDER" else dragon_chaos,
                "dragon_enemigos":    dragon_chaos  if team=="ORDER" else dragon_order,
                "elder_aliados":      elder_order if team=="ORDER" else elder_chaos,
                "elder_enemigos":     elder_chaos if team=="ORDER" else elder_order,
                "ventaja_drakes":     (dragon_order - dragon_chaos) * (1 if team=="ORDER" else -1),
                "alma_equipo":        int((soul_order and team=="ORDER") or (soul_chaos and team=="CHAOS")),
                "proximo_es_alma":    int((dragon_order == 3 and team=="ORDER") or (dragon_chaos == 3 and team=="CHAOS")),
                "dragon_disponible":  dragon_disponible,
                "tiempo_hasta_dragon": round(tiempo_hasta_dragon, 1),

                "grubs_aliados":    grubs_order if team=="ORDER" else grubs_chaos,
                "grubs_enemigos":   grubs_chaos  if team=="ORDER" else grubs_order,
                "grubs_disponibles": grubs_disponibles,

                "herald_disponible": herald_disponible,
                "herald_tomado":     int((herald_order and team=="ORDER") or (herald_chaos and team=="CHAOS")),

                "baron_buff_activo": int((baron_order and team=="ORDER") or (baron_chaos and team=="CHAOS")),
                "baron_disponible":   baron_disponible,
                "tiempo_hasta_baron": round(tiempo_hasta_baron, 1),

                # Items
                "item_0":        item_by_slot.get(0, 0),
                "item_1":        item_by_slot.get(1, 0),
                "item_2":        item_by_slot.get(2, 0),
                "item_3":        item_by_slot.get(3, 0),
                "item_4":        item_by_slot.get(4, 0),
                "item_5":        item_by_slot.get(5, 0),
                "trinket":       item_by_slot.get(6, 0),
                "item_7":        item_by_slot.get(7, 0), # Slot extra / ADC boots
                "item_quest":    item_quest,
                "n_items":       n_items,
                "item_gold_total": total_item_gold,
                "has_boots":     has_boots,

                # Economía equipo
                "team_gold":          team_gold,
                "enemy_gold":         enemy_gold,
                "gold_diff_team":     team_gold - enemy_gold,
                "gold_diff_individual": round(gold_diff_ind, 0),
                "gold_share":         round(gold_share, 3),

                # Nivel equipo
                "team_avg_level":  round(team_avg_level, 2),
                "enemy_avg_level": round(enemy_avg_level, 2),
                "team_kills":      team_kills,
                "enemy_kills":     enemy_kills,

                # Runes & Spells
                "keystone":       keystone,
                "primary_tree":   prim,
                "secondary_tree": sec,
                "spell1":         spell1,
                "spell2":         spell2,

                # Map
                "map_terrain": snap.get("game_data",{}).get("mapTerrain","Default"),
                "en_lado_aliado": int((team == "ORDER" and (px + pz) < 15000) or (team == "CHAOS" and (px + pz) > 15000)),
                "dist_fuente_aliada": round(
                    dist_2d(px, pz, 560, 560) if team == "ORDER"
                    else dist_2d(px, pz, 14340, 14390), 0
                ),

                # Estructuras destruidas (macro)
                "torres_destruidas_aliado":  towers_order if team == "ORDER" else towers_chaos,
                "torres_destruidas_enemigo": towers_chaos if team == "ORDER" else towers_order,
                "ventaja_torres": (towers_order - towers_chaos) * (1 if team == "ORDER" else -1),
                "torres_top_aliado":  towers_order_lane["TOP_LANE"] if team == "ORDER" else towers_chaos_lane["TOP_LANE"],
                "torres_mid_aliado":  towers_order_lane["MID_LANE"] if team == "ORDER" else towers_chaos_lane["MID_LANE"],
                "torres_bot_aliado":  towers_order_lane["BOT_LANE"] if team == "ORDER" else towers_chaos_lane["BOT_LANE"],
                "torres_top_enemigo": towers_chaos_lane["TOP_LANE"] if team == "ORDER" else towers_order_lane["TOP_LANE"],
                "torres_mid_enemigo": towers_chaos_lane["MID_LANE"] if team == "ORDER" else towers_order_lane["MID_LANE"],
                "torres_bot_enemigo": towers_chaos_lane["BOT_LANE"] if team == "ORDER" else towers_order_lane["BOT_LANE"],
                "inhibs_destruidos_aliado":  inhibs_order if team == "ORDER" else inhibs_chaos,
                "inhibs_destruidos_enemigo": inhibs_chaos if team == "ORDER" else inhibs_order,

                # Respawn enemigo (ventana de oportunidad)
                "enemy_respawn_total": round(enemy_team.get("respawn_total", 0), 1),

                # Target
                "label": label,
            }
            rows.append(row)

    # ── Post-procesado con pandas (deltas precisos) ─────────────
    if rows:
        df_temp = pd.DataFrame(rows)
        df_temp = df_temp.sort_values(["champion", "game_time"])
        grp     = df_temp.groupby("champion")

        df_temp["cs_delta"] = grp["cs"].diff().fillna(0).clip(lower=0)
        df_temp["hp_delta"] = grp["hp_pct"].diff().fillna(0)

        df_temp["x_delta"]  = grp["x"].diff().fillna(0)
        df_temp["z_delta"]  = grp["z"].diff().fillna(0)
        df_temp["is_moving"] = ((df_temp["x_delta"].abs() > 10) | (df_temp["z_delta"].abs() > 10)).astype(int)

        # ── RE-ETIQUETADO RETROSPECTIVO: PUSH_TOWER / PUSH_INHIB ────
        en_zona_push = (
            (df_temp["zona_categoria"] == "LANE") |
            ((df_temp["zona_categoria"] == "BASE") & (df_temp["en_lado_aliado"] == 0))
        )

        seen_bf = set()
        for ev in all_events:
            ename = ev.get("EventName")
            if ename not in ("TowerKill", "InhibitorKill"):
                continue
            key = (ename, round(ev.get("EventTime", 0), 2))
            if key in seen_bf:
                continue
            seen_bf.add(key)

            etime = ev.get("EventTime", 0)
            participantes = [ev.get("KillerName", "")] + list(ev.get("Assisters", []))
            new_label = "PUSH_INHIB" if ename == "InhibitorKill" else "PUSH_TOWER"

            mask_bf = (
                df_temp["champion"].isin(participantes)
                & (df_temp["game_time"] >= etime - PUSH_BACKFILL_WINDOW)
                & (df_temp["game_time"] < etime)
                & df_temp["label"].isin(PUSH_RELABEL_SAFE)
                & en_zona_push
            )
            df_temp.loc[mask_bf, "label"] = new_label

        # ── RE-ETIQUETADO INTELIGENTE: JUNGLE_FARM ──────────────────
        mask_jungle_move = (df_temp["role"] == "JUNGLE") & (df_temp["label"] == "MOVE")
        
        # Caso A: Sube el CS en la jungla
        df_temp.loc[mask_jungle_move & (df_temp["cs_delta"] > 0), "label"] = "JUNGLE_FARM"
        
        # Caso B: Pierde vida notablemente (>2%) sin enemigos cerca -> Tankeando campamento
        # (Usamos 0 enemigos cerca para no confundir con escaramuzas)
        mask_tanking = (df_temp["hp_delta"] < -0.02) & (df_temp["enemies_nearby"] == 0)
        df_temp.loc[mask_jungle_move & mask_tanking, "label"] = "JUNGLE_FARM"

        rows = df_temp.to_dict("records")

    return rows


# ── Main ──────────────────────────────────────────────────────────
def main():
    os.makedirs(os.path.dirname(OUTPUT_FILE), exist_ok=True)
    files = sorted(glob.glob(os.path.join(INPUT_DIR, "*.json")))
    print(f"[INFO] {len(files)} archivos JSON en {INPUT_DIR}")

    if not files: return

    all_data = []
    for idx, f in enumerate(files):
        name = os.path.basename(f)
        print(f"  [{idx+1:>3}/{len(files)}] {name}...", end=" ")
        try:
            rows = process_game(f)
            rows = [r for r in rows if r.get("label") != "END_GAME"]
            all_data.extend(rows)
            print(f"{len(rows)} filas")
        except Exception as e:
            print(f"ERROR: {e}")

    df = pd.DataFrame(all_data)
    df.to_csv(OUTPUT_FILE, index=False)

    print(f"\n{'='*60}")
    print(f"[OK] Dataset: {OUTPUT_FILE}")
    print(f"     Filas:     {len(df):,}")
    print(f"     Partidas:  {df['game_id'].nunique()}")
    print(f"     Columnas:  {len(df.columns)}")
    print(f"     Campeones: {df['champion'].nunique()}")

    print(f"\n--- Distribución de Labels ---")
    for label, count in df["label"].value_counts().items():
        pct = count / len(df) * 100
        bar = "#" * int(pct / 2)
        print(f"  {label:<25} {count:>6} ({pct:>5.1f}%) {bar}")

    print(f"\n--- Nuevas labels (Grubs/Herald/Inhib) ---")
    for lbl in ["CONTEST_GRUBS", "CONTEST_HERALD", "PUSH_INHIB"]:
        n = (df["label"] == lbl).sum()
        print(f"  {lbl:<25} {n:>6}")

    print(f"\n--- Columnas generadas ({len(df.columns)}) ---")
    for c in df.columns:
        print(f"  {c}")

if __name__ == "__main__":
    main()
