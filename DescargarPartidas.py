"""
#  GRABADOR DE REPLAYS VIA SPECTATOR API - MEJORADO PARA IA (PRO) v2.0
#  ----------------------------------------------------------------------
#  CORRECCIONES:
#    ✅ Timing EXACTO cada 20 segundos (usa timestamps absolutos)
#    ✅ Documentación clara de fuentes de datos
#    ✅ Mejor manejo de Timeline API con reintentos
#
#  FUENTES DE DATOS:
#  =================
#  
#  DURANTE LA GRABACIÓN (Spectator API):
#  ────────────────────────────────────────
#  - getGameMetaData: Info básica (gameId, participants con PUUIDs, championIds)
#  - getLastChunkInfo: Estado actual (chunk disponible, si terminó)
#  - Chunks/Keyframes: Datos binarios del replay
#  
#  DESPUÉS DE TERMINAR (Match API v5):
#  ────────────────────────────────────────
#  - /matches/{matchId}/timeline: ← AQUÍ VIENEN TODOS LOS DATOS DETALLADOS
#      * Gold por minuto (totalGold, currentGold)
#      * Posiciones (x, y)
#      * Nivel (level)
#      * CS (minionsKilled + jungleMinionsKilled)
#      * Items (en cada frame)
#      * XP, damage, etc.
#  
#  Por eso durante la grabación todo está en 0 - se rellena AL FINAL
#  usando el Timeline que Riot genera cuando termina la partida.
"""

import requests
import os
import sys
import time
import random
import json
import struct
import threading
import argparse
import csv
from datetime import datetime
from urllib3.exceptions import InsecureRequestWarning
from urllib.parse import quote

requests.packages.urllib3.disable_warnings(InsecureRequestWarning)

# ══════════════════════════════════════════════════════════════
# CONFIGURACIÓN
# ══════════════════════════════════════════════════════════════

API_KEY = os.getenv('RIOT_API_KEY', 'RGAPI-8c330923-c471-4596-bd99-1ad6aa220d50')
REGION_PLATFORM = "kr"
REGION_ROUTING = "asia"
PLATFORM_ID = "KR"

SPECTATOR_SERVERS = {
    "KR":   "spectator.kr.lol.pvp.net:8080",
    "NA1":  "spectator.na1.lol.pvp.net:8080",
    "EUW1": "spectator.euw1.lol.pvp.net:8080",
}

SPECTATOR_SERVERS_ALT = {
    "KR":   "spectator.kr.lol.pvp.net:80",
    "NA1":  "spectator.na1.lol.pvp.net:80",
    "EUW1": "spectator.euw1.lol.pvp.net:80",
}

SAVE_PATH = "Replays_Recorded_KR"
LOG_FILE = "spectator_recorder.log"
METADATA_FILE = "recorded_games_metadata.json"
STATE_FILE = "recorder_state.json"

CHECK_INTERVAL = 120
CHUNK_POLL_INTERVAL = 10
MAX_CONSECUTIVE_ERRORS = 30
MIN_GAME_DURATION = 15

# Estado global
os.makedirs(SAVE_PATH, exist_ok=True)
active_recordings = {}
completed_recordings = set()
challenger_players = []

GLOBAL_SPECTATOR_LOCK = threading.Lock()
LAST_SPECTATOR_REQUEST = 0
LAST_GAME_END_TIME = 0
MIN_SPECTATOR_INTERVAL = 1.5

def log(message, level="INFO"):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_msg = f"[{timestamp}] [{level}] {message}"
    try:
        print(log_msg)
    except:
        pass
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(log_msg + "\n")
    except:
        pass

# ══════════════════════════════════════════════════════════════
# METADATA COLLECTOR PARA IA - VERSIÓN CORREGIDA
# ══════════════════════════════════════════════════════════════

class MetadataCollector:
    """
    Extrae datos detallados para entrenamiento de IA cada X segundos.
    
    FUENTES DE DATOS:
    ─────────────────
    1. DURANTE GRABACIÓN: Solo captura timestamps y estructura base
       - Participants (PUUIDs, championIds) del Spectator API
       - Stats quedan en 0 porque el Spectator no los provee en tiempo real
    
    2. AL TERMINAR: Descarga Timeline de Match API v5
       - /matches/{matchId}/timeline contiene TODOS los datos detallados
       - Gold, posiciones, nivel, CS frame por frame (cada 1 minuto)
       - Se interpola para llenar los snapshots cada 20s
    """
    
    def __init__(self, recorder, interval=20):
        self.recorder = recorder
        self.interval = interval  # Segundos entre snapshots
        self.metadata_history = []
        
        # Sistema de timing ABSOLUTO para captura exacta
        self.game_start_real_time = None  # Timestamp real cuando empezó
        self.next_snapshot_time = None    # Próximo snapshot programado (absoluto)

    def collect_metadata(self):
        """
        Captura snapshot de la estructura del juego.
        Los datos dinámicos (gold, pos, etc.) se rellenan AL FINAL con Timeline API.
        """
        current_time = time.time()
        
        # ═══ INICIALIZACIÓN: Primera llamada ═══
        if self.game_start_real_time is None:
            self.game_start_real_time = current_time
            self.next_snapshot_time = current_time + self.interval
            log(f"🕐 Sistema de snapshots iniciado (cada {self.interval}s)", "DEBUG")
            return False
        
        # ═══ VERIFICAR SI TOCA CAPTURAR (timing absoluto) ═══
        if current_time < self.next_snapshot_time:
            return False  # Todavía no es hora
        
        # ═══ PROGRAMAR PRÓXIMO SNAPSHOT (acumulativo) ═══
        # Esto garantiza intervalos exactos incluso si hay delays
        self.next_snapshot_time += self.interval
        
        # Si nos atrasamos mucho (>2 intervalos), resetear
        if current_time > self.next_snapshot_time + self.interval:
            self.next_snapshot_time = current_time + self.interval
            log(f"⚠️ Retraso detectado, resincronizando timing", "DEBUG")
            
        try:
            # ═══ OBTENER PARTICIPANTS ═══
            # Prioridad: extra_metadata (viene de active-games) > game_metadata (spectator)
            participants = self.recorder.extra_metadata.get('participants', [])
            
            if not participants and self.recorder.game_metadata:
                participants = self.recorder.game_metadata.get('participants', [])

            if not participants:
                # Primer snapshot: pedir metadata
                response = self.recorder._spectator_request("getGameMetaData", "1/")
                if response and response.status_code == 200:
                    self.recorder.game_metadata = response.json()
                    participants = self.recorder.game_metadata.get('participants', [])
            
            if not participants:
                log(f"⚠️ No hay participantes disponibles aún", "DEBUG")
                return False
            
            # ═══ CALCULAR TIEMPO DE JUEGO ═══
            game_length = 0
            
            # Opción 1: desde game_metadata (spectator)
            if self.recorder.game_metadata:
                game_length = self.recorder.game_metadata.get('gameLength', 0)
            
            # Opción 2: calcular desde gameStartTime (extra_metadata)
            if game_length == 0 and self.recorder.extra_metadata:
                start_time = self.recorder.extra_metadata.get('gameStartTime', 0)
                if start_time > 0:
                    game_length = int(time.time() * 1000 - start_time)
            
            # Opción 3: calcular desde nuestro start_time
            if game_length == 0 and self.recorder.start_time:
                game_length = int((time.time() - self.recorder.start_time) * 1000)
            
            # ═══ CONSTRUIR SNAPSHOT ═══
            snapshot = {
                "captured_at_iso": datetime.now().isoformat(),
                "captured_at_unix": current_time,
                "game_time_ms": game_length,
                "game_time_min": round(game_length / 60000, 2),
                "snapshot_number": len(self.metadata_history) + 1,
                "participants": []
            }
            
            # ═══ AGREGAR PARTICIPANTS (estructura base) ═══
            for idx, p in enumerate(participants):
                # Obtener PUUID (prioridad: del participant > buscar en summoner)
                puuid = p.get('puuid') or p.get('summonerId')
                
                # Si viene de active-games, tiene bot y puuid directo
                # Si viene de spectator metadata, puede no tenerlo
                if not puuid and 'summonerId' in p:
                    # Intentar obtener de extra_metadata que tiene más info
                    summoner_id = p.get('summonerId')
                    # Buscar en extra_metadata
                    for ep in self.recorder.extra_metadata.get('participants', []):
                        if ep.get('summonerId') == summoner_id:
                            puuid = ep.get('puuid')
                            break
                
                p_data = {
                    "participant_id": p.get('participantId', idx + 1),
                    "puuid": puuid,  # ← Ahora debería tener valor
                    "name": p.get('summonerName') or p.get('riotId', 'Unknown'),
                    "champion_id": p.get('championId', 0),
                    "team_id": p.get('teamId', 0),
                    # Stats en 0 - se llenarán con Timeline
                    "stats": {
                        "level": 0,
                        "gold": 0,
                        "minions_killed": 0,
                        "pos": {"x": 0, "y": 0},
                        "items": []
                    }
                }
                snapshot["participants"].append(p_data)
                
            self.metadata_history.append(snapshot)
            
            # ═══ LOG DE PROGRESO ═══
            elapsed_real = current_time - self.game_start_real_time
            log(f"   📋 Snapshot #{len(self.metadata_history)} @ {snapshot['game_time_min']:.1f}min "
                f"(real: {elapsed_real:.0f}s, esperado: {len(self.metadata_history) * self.interval}s)", "DEBUG")
            
            return True
            
        except Exception as e:
            log(f"   ⚠️ Error capturando metadata: {e}", "DEBUG")
            import traceback
            traceback.print_exc()
            return False

    def enrich_with_timeline(self):
        """
        PASO CRÍTICO: Descarga el Timeline completo de Riot Match API
        y rellena TODOS los datos dinámicos COMPLETOS para el análisis.
        
        DATOS EXTRAÍDOS:
        ────────────────
        POR JUGADOR:
        - Gold, nivel, CS, posiciones, XP
        - Items (inventario completo)
        - KDA (kills, deaths, assists)
        - Daño total causado/recibido
        - Estado de muerte (is_dead, respawn_time)
        - En teamfight (bool)
        
        POR EQUIPO:
        - Dragones matados (tipo y timestamp)
        - Barones matados
        - Torres destruidas
        - Inhibidores destruidos
        - Herald matado
        
        DETECCIÓN:
        - Teamfights activos
        """
        match_id = f"{PLATFORM_ID}_{self.recorder.game_id}"
        log(f"\n🕵️ ENRIQUECIENDO DATOS CON TIMELINE API COMPLETO")
        log(f"   Match ID: {match_id}")
        log(f"   Snapshots a rellenar: {len(self.metadata_history)}")
        
        # ═══ REINTENTOS ═══
        timeline = None
        max_attempts = 6
        
        for attempt in range(max_attempts):
            log(f"   ⏳ Intento {attempt+1}/{max_attempts} de obtener timeline...")
            
            if attempt > 0:
                time.sleep(20)
            
            url = f"https://{REGION_ROUTING}.api.riotgames.com/lol/match/v5/matches/{match_id}/timeline"
            headers = {"X-Riot-Token": API_KEY}
            
            try:
                r = safe_api_call(url, headers)
                if r and r.status_code == 200:
                    timeline = r.json()
                    log(f"   ✅ Timeline obtenido exitosamente")
                    break
                elif r and r.status_code == 404:
                    log(f"   ⏳ Timeline aún no disponible (404)", "DEBUG")
                    continue
                else:
                    log(f"   ⚠️ Error HTTP {r.status_code if r else 'Timeout'}", "DEBUG")
            except Exception as e:
                log(f"   ⚠️ Error en request: {e}", "DEBUG")
        
        if not timeline:
            log(f"❌ TIMELINE NO DISPONIBLE tras {max_attempts} intentos", "WARN")
            return False
            
        try:
            # ═══ PARSEAR TIMELINE ═══
            info = timeline.get('info', {})
            frames = info.get('frames', [])
            participants_map = info.get('participants', [])
            
            log(f"   📊 Timeline tiene {len(frames)} frames")
            
            # Mapear PUUID ↔ participantId
            v5_id_to_puuid = {p['participantId']: p['puuid'] for p in participants_map}
            puuid_to_v5_id = {p['puuid']: p['participantId'] for p in participants_map}
            
            # ═══ TRACKING DE ESTADO POR PARTICIPANTE ═══
            participant_inventories = {i: [] for i in range(1, 11)}
            participant_trinkets = {i: 3340 for i in range(1, 11)}  # Stealth Ward por defecto
            participant_kills = {i: 0 for i in range(1, 11)}
            participant_deaths = {i: 0 for i in range(1, 11)}
            participant_assists = {i: 0 for i in range(1, 11)}
            participant_death_info = {i: {"is_dead": False, "respawn_time": 0} for i in range(1, 11)}
            
            # ═══ TRACKING DE OBJETIVOS POR EQUIPO (DETALLADO) ═══
            team_dragons = {100: [], 200: []}  # [{type, timestamp, minute}, ...]
            team_barons = {100: [], 200: []}   # [{timestamp, minute}, ...]
            team_towers = {100: [], 200: []}   # [{lane, tier, timestamp}, ...]
            team_inhibitors = {100: [], 200: []}  # [{lane, timestamp}, ...]
            team_heralds = {100: [], 200: []}  # [{timestamp}, ...]
            
            # ═══ TRACKING DE MUERTES RECIENTES (para teamfights) ═══
            recent_deaths = []
            
            # ═══ PROCESAR TODOS LOS EVENTOS FRAME POR FRAME ═══
            for frame_idx, frame in enumerate(frames):
                frame_timestamp = frame.get('timestamp', frame_idx * 60000)
                
                for event in frame.get('events', []):
                    event_type = event.get('type')
                    event_timestamp = event.get('timestamp', frame_timestamp)
                    participant_id = event.get('participantId')
                    
                    # ─── KILLS/DEATHS/ASSISTS ───
                    if event_type == 'CHAMPION_KILL':
                        killer_id = event.get('killerId')
                        if killer_id and 1 <= killer_id <= 10:
                            participant_kills[killer_id] += 1
                        
                        victim_id = event.get('victimId')
                        if victim_id and 1 <= victim_id <= 10:
                            participant_deaths[victim_id] += 1
                            
                            # Registrar muerte para teamfights
                            recent_deaths.append({
                                "timestamp": event_timestamp,
                                "victim_id": victim_id - 1  # 0-indexed
                            })
                            
                            # Calcular respawn time
                            victim_frame = frame.get('participantFrames', {}).get(str(victim_id), {})
                            victim_level = victim_frame.get('level', 1)
                            death_timer = self._calculate_death_timer(victim_level)
                            respawn_timestamp = event_timestamp + (death_timer * 1000)
                            
                            participant_death_info[victim_id] = {
                                "is_dead": True,
                                "respawn_time": respawn_timestamp
                            }
                        
                        # Assists
                        for assist_id in event.get('assistingParticipantIds', []):
                            if assist_id and 1 <= assist_id <= 10:
                                participant_assists[assist_id] += 1
                    
                    # ─── ITEMS (con trinkets y evoluciones) ───
                    if participant_id and 1 <= participant_id <= 10:
                        item_id = event.get('itemId')
                        
                        if event_type == 'ITEM_PURCHASED':
                            if self._is_trinket(item_id):
                                participant_trinkets[participant_id] = item_id
                            elif item_id and item_id not in participant_inventories[participant_id]:
                                if len(participant_inventories[participant_id]) < 6:
                                    participant_inventories[participant_id].append(item_id)
                        
                        elif event_type == 'ITEM_DESTROYED':
                            # Trinkets
                            if self._is_trinket(item_id):
                                if participant_trinkets[participant_id] == item_id:
                                    participant_trinkets[participant_id] = 3340  # Reset a Stealth Ward
                            # Items normales
                            elif item_id and item_id in participant_inventories[participant_id]:
                                participant_inventories[participant_id].remove(item_id)
                            
                            # Evoluciones automáticas
                            evolutions = {
                                3004: 3042,  # Manamune → Muramana
                                3003: 3040,  # Archangel's → Seraph's Embrace
                                3865: 3866,  # World Atlas → Runic Compass
                                3866: 3867,  # Runic Compass → Bounty of Worlds
                                3010: 3013,  # Mejai's Soulstealer (evolución)
                            }
                            
                            if item_id in evolutions:
                                new_id = evolutions[item_id]
                                if new_id not in participant_inventories[participant_id]:
                                    if len(participant_inventories[participant_id]) < 6:
                                        participant_inventories[participant_id].append(new_id)
                            
                            # Bounty of Worlds (3867) → Evolución específica por campeón
                            elif item_id == 3867:
                                # Obtener champion name (necesitamos extra_metadata)
                                champ_name = "Unknown"
                                if hasattr(self.recorder, 'extra_metadata'):
                                    participants = self.recorder.extra_metadata.get('participants', [])
                                    for p in participants:
                                        if p.get('participantId') == participant_id:
                                            champ_name = p.get('championName', 'Unknown')
                                            break
                                
                                evolved_item = self._get_support_evolution(champ_name)
                                if item_id in participant_inventories[participant_id]:
                                    participant_inventories[participant_id].remove(item_id)
                                if evolved_item not in participant_inventories[participant_id]:
                                    if len(participant_inventories[participant_id]) < 6:
                                        participant_inventories[participant_id].append(evolved_item)
                        
                        elif event_type == 'ITEM_SOLD':
                            if item_id and item_id in participant_inventories[participant_id]:
                                participant_inventories[participant_id].remove(item_id)
                        
                        elif event_type == 'ITEM_UNDO':
                            before_id = event.get('beforeId')
                            if before_id:
                                if self._is_trinket(before_id):
                                    if participant_trinkets[participant_id] == before_id:
                                        participant_trinkets[participant_id] = 3340
                                elif before_id in participant_inventories[participant_id]:
                                    participant_inventories[participant_id].remove(before_id)
                    
                    # ─── DRAGONES (con tipo y detalles) ───
                    if event_type == 'ELITE_MONSTER_KILL':
                        monster_type = event.get('monsterType')
                        killer_team = event.get('killerTeamId')
                        
                        if monster_type == 'DRAGON':
                            dragon_subtype = event.get('monsterSubType', 'UNKNOWN_DRAGON')
                            if killer_team in [100, 200]:
                                team_dragons[killer_team].append({
                                    "type": dragon_subtype,
                                    "timestamp": event_timestamp,
                                    "minute": round(event_timestamp / 60000, 1)
                                })
                        
                        elif monster_type == 'BARON_NASHOR':
                            if killer_team in [100, 200]:
                                team_barons[killer_team].append({
                                    "timestamp": event_timestamp,
                                    "minute": round(event_timestamp / 60000, 1)
                                })
                        
                        elif monster_type == 'RIFTHERALD':
                            if killer_team in [100, 200]:
                                team_heralds[killer_team].append({
                                    "timestamp": event_timestamp,
                                    "minute": round(event_timestamp / 60000, 1)
                                })
                    
                    # ─── TORRES (con lane y tier) ───
                    if event_type == 'BUILDING_KILL':
                        building_type = event.get('buildingType')
                        killer_team = event.get('killerTeamId')
                        lane = event.get('laneType', 'UNKNOWN')
                        tower_type = event.get('towerType', 'UNKNOWN')
                        
                        if building_type == 'TOWER_BUILDING':
                            if killer_team in [100, 200]:
                                team_towers[killer_team].append({
                                    "lane": lane,
                                    "tier": tower_type,
                                    "timestamp": event_timestamp,
                                    "minute": round(event_timestamp / 60000, 1)
                                })
                        
                        elif building_type == 'INHIBITOR_BUILDING':
                            if killer_team in [100, 200]:
                                team_inhibitors[killer_team].append({
                                    "lane": lane,
                                    "timestamp": event_timestamp,
                                    "minute": round(event_timestamp / 60000, 1)
                                })
            
            # ═══ CORREGIR PUUIDs EN SNAPSHOTS ═══
            for snap in self.metadata_history:
                for p_data in snap['participants']:
                    if not p_data['puuid']:
                        pid = p_data['participant_id']
                        if pid in v5_id_to_puuid:
                            p_data['puuid'] = v5_id_to_puuid[pid]
            
            # ═══ RELLENAR CADA SNAPSHOT CON DATOS COMPLETOS ═══
            for snap in self.metadata_history:
                ms = snap['game_time_ms']
                frame_idx = min(int(ms / 60000), len(frames) - 1)
                if frame_idx < 0:
                    continue
                
                frame = frames[frame_idx]
                frame_timestamp = frame.get('timestamp', frame_idx * 60000)
                p_frames = frame.get('participantFrames', {})
                
                # ─── DETECTAR TEAMFIGHT ───
                teamfight_status = self._detect_teamfight(frame, recent_deaths, frame_timestamp)
                
                # ─── ACTUALIZAR ESTADOS DE RESPAWN ───
                for pid in range(1, 11):
                    death_info = participant_death_info[pid]
                    if death_info["is_dead"] and frame_timestamp >= death_info["respawn_time"]:
                        participant_death_info[pid]["is_dead"] = False
                
                # ─── RELLENAR STATS POR PARTICIPANTE ───
                for p_data in snap['participants']:
                    puuid = p_data['puuid']
                    
                    if not puuid:
                        continue
                    
                    v5_pid = puuid_to_v5_id.get(puuid)
                    if not v5_pid:
                        continue
                    
                    v5_pid_str = str(v5_pid)
                    if v5_pid_str in p_frames:
                        pf = p_frames[v5_pid_str]
                        
                        # Stats de frame
                        p_data['stats'].update({
                            "level": pf.get('level', 0),
                            "gold": pf.get('totalGold', 0),
                            "current_gold": pf.get('currentGold', 0),
                            "minions_killed": pf.get('minionsKilled', 0) + pf.get('jungleMinionsKilled', 0),
                            "pos": {
                                "x": pf.get('position', {}).get('x', 0),
                                "y": pf.get('position', {}).get('y', 0)
                            },
                            "xp": pf.get('xp', 0),
                            
                            # Daño
                            "damage_done": pf.get('damageStats', {}).get('totalDamageDone', 0),
                            "damage_taken": pf.get('damageStats', {}).get('totalDamageTaken', 0),
                            "damage_to_champions": pf.get('damageStats', {}).get('totalDamageDoneToChampions', 0),
                            
                            # Items + Trinket
                            "items": participant_inventories.get(v5_pid, []),
                            "trinket": participant_trinkets.get(v5_pid, 3340),
                            
                            # KDA
                            "kills": participant_kills.get(v5_pid, 0),
                            "deaths": participant_deaths.get(v5_pid, 0),
                            "assists": participant_assists.get(v5_pid, 0),
                            
                            # Estado
                            "is_dead": participant_death_info[v5_pid]["is_dead"],
                            "respawn_time": participant_death_info[v5_pid]["respawn_time"],
                            
                            # Teamfight
                            "in_teamfight": teamfight_status.get(v5_pid - 1, False)  # 0-indexed
                        })
                
                # ─── AÑADIR OBJETIVOS DE EQUIPO AL SNAPSHOT (DETALLADO) ───
                snap['objectives'] = {
                    "blue_team": {
                        "dragons": [
                            {
                                "type": d['type'],
                                "minute": d['minute']
                            }
                            for d in team_dragons[100] if d['timestamp'] <= frame_timestamp
                        ],
                        "barons": [
                            {"minute": b['minute']}
                            for b in team_barons[100] if b['timestamp'] <= frame_timestamp
                        ],
                        "towers": [
                            {
                                "lane": t['lane'],
                                "tier": t['tier'],
                                "minute": t['minute']
                            }
                            for t in team_towers[100] if t['timestamp'] <= frame_timestamp
                        ],
                        "inhibitors": [
                            {
                                "lane": i['lane'],
                                "minute": i['minute']
                            }
                            for i in team_inhibitors[100] if i['timestamp'] <= frame_timestamp
                        ],
                        "heralds": [
                            {"minute": h['minute']}
                            for h in team_heralds[100] if h['timestamp'] <= frame_timestamp
                        ]
                    },
                    "red_team": {
                        "dragons": [
                            {
                                "type": d['type'],
                                "minute": d['minute']
                            }
                            for d in team_dragons[200] if d['timestamp'] <= frame_timestamp
                        ],
                        "barons": [
                            {"minute": b['minute']}
                            for b in team_barons[200] if b['timestamp'] <= frame_timestamp
                        ],
                        "towers": [
                            {
                                "lane": t['lane'],
                                "tier": t['tier'],
                                "minute": t['minute']
                            }
                            for t in team_towers[200] if t['timestamp'] <= frame_timestamp
                        ],
                        "inhibitors": [
                            {
                                "lane": i['lane'],
                                "minute": i['minute']
                            }
                            for i in team_inhibitors[200] if i['timestamp'] <= frame_timestamp
                        ],
                        "heralds": [
                            {"minute": h['minute']}
                            for h in team_heralds[200] if h['timestamp'] <= frame_timestamp
                        ]
                    }
                }
            
            log(f"✅ {len(self.metadata_history)} snapshots enriquecidos con TODO")
            return True
            
        except Exception as e:
            log(f"❌ Error procesando Timeline: {e}", "ERROR")
            import traceback
            traceback.print_exc()
            return False
    
    def _calculate_death_timer(self, level):
        """Calcula tiempo de respawn según nivel"""
        if level <= 6:
            return 4 + (2 * level)
        else:
            return 21 + (2.5 * (level - 6))
    
    def _is_trinket(self, item_id):
        """Verifica si un item es trinket"""
        return item_id in [3340, 3363, 3364, 3362, 3361]
    
    def _get_support_evolution(self, champion_name):
        """Retorna la evolución correcta de Bounty of Worlds según campeón"""
        SUPPORT_EVOLUTIONS = {
            "Lulu": 3877, "Janna": 3877, "Nami": 3877, "Sona": 3877,
            "Seraphine": 3877, "Milio": 3877, "Yuumi": 3877, "Soraka": 3877,
            "Leona": 3876, "Nautilus": 3876, "Rell": 3876, "Alistar": 3876,
            "Blitzcrank": 3876, "Braum": 3876, "Taric": 3876, "Tahm Kench": 3876,
            "Zyra": 3870, "Brand": 3870, "Velkoz": 3870, "Xerath": 3870, "Karma": 3870,
            "Bard": 3871, "Renata Glasc": 3871, "Thresh": 3871,
            "Senna": 3869,
        }
        return SUPPORT_EVOLUTIONS.get(champion_name, 3869)  # Default: 3869
    
    def _detect_teamfight(self, frame, recent_deaths, current_timestamp):
        """
        Detecta teamfights activos.
        Retorna dict {participant_id_0indexed: bool}
        """
        TEAMFIGHT_DISTANCE = 3000
        TEAMFIGHT_TIME_WINDOW = 10000
        
        participant_frames = frame.get("participantFrames", {})
        
        # Separar por equipos
        team_100 = []
        team_200 = []
        
        for pid_str, pf in participant_frames.items():
            pid = int(pid_str)
            team_id = 100 if pid <= 5 else 200
            position = pf.get("position", {"x": 0, "y": 0})
            
            if team_id == 100:
                team_100.append({"id": pid - 1, "pos": position})
            else:
                team_200.append({"id": pid - 1, "pos": position})
        
        # Contar jugadores cercanos
        def count_nearby_players(team_players):
            if len(team_players) < 4:
                return []
            
            nearby_groups = []
            for i, player1 in enumerate(team_players):
                group = [player1["id"]]
                for j, player2 in enumerate(team_players):
                    if i != j:
                        dx = player1["pos"]["x"] - player2["pos"]["x"]
                        dy = player1["pos"]["y"] - player2["pos"]["y"]
                        dist = (dx**2 + dy**2) ** 0.5
                        
                        if dist <= TEAMFIGHT_DISTANCE:
                            group.append(player2["id"])
                
                if len(group) >= 4:
                    nearby_groups.append(group)
            
            return nearby_groups
        
        blue_groups = count_nearby_players(team_100)
        red_groups = count_nearby_players(team_200)
        
        # Muertes recientes
        recent_deaths_in_window = [
            d for d in recent_deaths 
            if current_timestamp - d["timestamp"] <= TEAMFIGHT_TIME_WINDOW
        ]
        
        # Determinar teamfight
        participants_in_tf = set()
        
        if blue_groups and red_groups and len(recent_deaths_in_window) > 0:
            for group in blue_groups:
                participants_in_tf.update(group)
            for group in red_groups:
                participants_in_tf.update(group)
        
        # Retornar resultado
        result = {}
        for i in range(10):
            result[i] = i in participants_in_tf
        
        return result
        """
        PASO CRÍTICO: Descarga el Timeline completo de Riot Match API
        y rellena TODOS los datos dinámicos (gold, pos, nivel, CS, ITEMS).
        
        CÓMO FUNCIONA:
        ──────────────
        1. Espera hasta que la partida aparezca en Match API (puede tardar 1-2 min)
        2. Descarga /matches/{matchId}/timeline
        3. El Timeline tiene frames cada 60 segundos con todos los stats
        4. Parsea eventos de items para reconstruir inventario
        5. Interpola los datos para llenar nuestros snapshots cada 20s
        """
        match_id = f"{PLATFORM_ID}_{self.recorder.game_id}"
        log(f"\n🕵️ ENRIQUECIENDO DATOS CON TIMELINE API")
        log(f"   Match ID: {match_id}")
        log(f"   Snapshots a rellenar: {len(self.metadata_history)}")
        
        # ═══ REINTENTOS: La API puede tardar en tener la partida lista ═══
        timeline = None
        max_attempts = 6
        
        for attempt in range(max_attempts):
            log(f"   ⏳ Intento {attempt+1}/{max_attempts} de obtener timeline...")
            
            if attempt > 0:
                time.sleep(20)  # Esperar 20s entre intentos
            
            url = f"https://{REGION_ROUTING}.api.riotgames.com/lol/match/v5/matches/{match_id}/timeline"
            headers = {"X-Riot-Token": API_KEY}
            
            try:
                r = safe_api_call(url, headers)
                if r and r.status_code == 200:
                    timeline = r.json()
                    log(f"   ✅ Timeline obtenido exitosamente")
                    break
                elif r and r.status_code == 404:
                    log(f"   ⏳ Timeline aún no disponible (404), esperando...", "DEBUG")
                    continue
                else:
                    log(f"   ⚠️ Error HTTP {r.status_code if r else 'Timeout'}", "DEBUG")
            except Exception as e:
                log(f"   ⚠️ Error en request: {e}", "DEBUG")
        
        if not timeline:
            log(f"❌ TIMELINE NO DISPONIBLE tras {max_attempts} intentos", "WARN")
            log(f"   Los datos quedarán en 0. Posibles causas:")
            log(f"   - La partida fue muy corta (remake)")
            log(f"   - Riot API está con problemas")
            log(f"   - El match_id es incorrecto")
            return False
            
        try:
            # ═══ PARSEAR TIMELINE ═══
            info = timeline.get('info', {})
            frames = info.get('frames', [])
            participants_map = info.get('participants', [])
            
            log(f"   📊 Timeline tiene {len(frames)} frames (cada 60s)")
            
            # Mapear PUUID → participantId del Timeline
            v5_id_to_puuid = {p['participantId']: p['puuid'] for p in participants_map}
            puuid_to_v5_id = {p['puuid']: p['participantId'] for p in participants_map}
            
            # ═══ TRACKEAR INVENTARIOS POR PARTICIPANTE ═══
            # {participantId: [item1, item2, ...]}
            participant_inventories = {i: [] for i in range(1, 11)}
            
            # Procesar eventos de items frame por frame
            for frame in frames:
                for event in frame.get('events', []):
                    event_type = event.get('type')
                    participant_id = event.get('participantId')
                    item_id = event.get('itemId')
                    
                    if not participant_id or participant_id < 1 or participant_id > 10:
                        continue
                    
                    # Gestión de inventario (igual que tu script)
                    if event_type == 'ITEM_PURCHASED':
                        if item_id and item_id not in participant_inventories[participant_id]:
                            if len(participant_inventories[participant_id]) < 6:
                                participant_inventories[participant_id].append(item_id)
                    
                    elif event_type == 'ITEM_DESTROYED':
                        if item_id and item_id in participant_inventories[participant_id]:
                            participant_inventories[participant_id].remove(item_id)
                    
                    elif event_type == 'ITEM_SOLD':
                        if item_id and item_id in participant_inventories[participant_id]:
                            participant_inventories[participant_id].remove(item_id)
            
            # ═══ CORREGIR PUUIDs EN SNAPSHOTS ═══
            for snap in self.metadata_history:
                for p_data in snap['participants']:
                    # Si el PUUID es null, intentar obtenerlo del participantId
                    if not p_data['puuid']:
                        # Buscar en extra_metadata o game_metadata
                        pid = p_data['participant_id']
                        
                        # Buscar en v5_id_to_puuid
                        if pid in v5_id_to_puuid:
                            p_data['puuid'] = v5_id_to_puuid[pid]
                            log(f"   🔧 PUUID corregido para participant {pid}", "DEBUG")
            
            # ═══ RELLENAR CADA SNAPSHOT CON DATOS DEL FRAME MÁS CERCANO ═══
            for snap in self.metadata_history:
                ms = snap['game_time_ms']
                
                # Encontrar frame más cercano (Timeline tiene frames cada 60s = 60000ms)
                frame_idx = min(int(ms / 60000), len(frames) - 1)
                if frame_idx < 0:
                    continue
                
                frame = frames[frame_idx]
                p_frames = frame.get('participantFrames', {})
                
                # Para cada participante en el snapshot
                for p_data in snap['participants']:
                    puuid = p_data['puuid']
                    
                    if not puuid:
                        log(f"   ⚠️ Participante sin PUUID: {p_data['name']}", "DEBUG")
                        continue
                    
                    # Buscar el participantId correcto en el Timeline
                    v5_pid = puuid_to_v5_id.get(puuid)
                    
                    if not v5_pid:
                        log(f"   ⚠️ No se encontró participantId para PUUID {puuid[:8]}...", "DEBUG")
                        continue
                    
                    # Si encontramos el participante en el frame, actualizar stats
                    v5_pid_str = str(v5_pid)
                    if v5_pid_str in p_frames:
                        pf = p_frames[v5_pid_str]
                        
                        # AQUÍ ESTÁN TODOS LOS DATOS DETALLADOS
                        p_data['stats'].update({
                            "level": pf.get('level', 0),
                            "gold": pf.get('totalGold', 0),
                            "current_gold": pf.get('currentGold', 0),
                            "minions_killed": pf.get('minionsKilled', 0) + pf.get('jungleMinionsKilled', 0),
                            "pos": {
                                "x": pf.get('position', {}).get('x', 0),
                                "y": pf.get('position', {}).get('y', 0)
                            },
                            "xp": pf.get('xp', 0),
                            "time_enemy_spent_controlled": pf.get('timeEnemySpentControlled', 0),
                            "damage_stats": {
                                "total_damage_done": pf.get('damageStats', {}).get('totalDamageDone', 0),
                                "total_damage_taken": pf.get('damageStats', {}).get('totalDamageTaken', 0)
                            },
                            # ═══ ITEMS (del tracking de eventos) ═══
                            "items": participant_inventories.get(v5_pid, [])
                        })
                    else:
                        log(f"   ⚠️ Participante {v5_pid} no encontrado en frame {frame_idx}", "DEBUG")
            
            log(f"✅ {len(self.metadata_history)} snapshots enriquecidos con éxito")
            return True
            
        except Exception as e:
            log(f"❌ Error procesando Timeline: {e}", "ERROR")
            import traceback
            traceback.print_exc()
            return False

    def save_final_reports(self):
        """Genera archivos finales: JSON completo + CSV para IA."""
        if not self.metadata_history:
            log("⚠️ No hay snapshots para guardar", "WARN")
            return
        
        log(f"\n💾 Generando archivos finales...")
            
        # ═══ 1. JSON COMPLETO ═══
        history_path = os.path.join(self.recorder.temp_dir, "ai_metadata_history.json")
        with open(history_path, 'w', encoding='utf-8') as f:
            json.dump(self.metadata_history, f, indent=2, ensure_ascii=False)
        log(f"   ✅ JSON: ai_metadata_history.json ({len(self.metadata_history)} snapshots)")
            
        # ═══ 2. CSV PARA ENTRENAMIENTO (COMPLETO) ═══
        csv_path = os.path.join(self.recorder.temp_dir, "ai_training_timeline.csv")
        try:
            with open(csv_path, 'w', newline='', encoding='utf-8') as f:
                writer = csv.writer(f)
                
                # Header COMPLETO
                writer.writerow([
                    'ms', 'min', 'p_idx', 'team', 'champ',
                    'gold', 'current_gold', 'level', 'cs', 'xp',
                    'x', 'y',
                    'kills', 'deaths', 'assists',
                    'damage_done', 'damage_taken', 'damage_to_champions',
                    'is_dead', 'in_teamfight',
                    'items', 'trinket',
                    # Objetivos de equipo (counts)
                    'team_dragons_count', 'team_barons_count', 'team_towers_count', 'team_inhibitors_count', 'team_heralds_count',
                    'enemy_dragons_count', 'enemy_barons_count', 'enemy_towers_count', 'enemy_inhibitors_count', 'enemy_heralds_count',
                    # Dragones por tipo
                    'team_dragons_types', 'enemy_dragons_types'
                ])
                
                # Datos
                for snap in self.metadata_history:
                    ms = snap['game_time_ms']
                    minutes = snap['game_time_min']
                    
                    # Objetivos por equipo
                    objectives = snap.get('objectives', {})
                    blue_obj = objectives.get('blue_team', {})
                    red_obj = objectives.get('red_team', {})
                    
                    for idx, p in enumerate(snap['participants']):
                        s = p['stats']
                        team_id = p['team_id']
                        
                        # Determinar objetivos del equipo y del enemigo
                        if team_id == 100:  # Blue team
                            team_obj = blue_obj
                            enemy_obj = red_obj
                        else:  # Red team
                            team_obj = red_obj
                            enemy_obj = blue_obj
                        
                        # Items
                        items_str = ','.join([str(item_id) for item_id in s.get('items', [])])
                        if not items_str:
                            items_str = 'none'
                        
                        # Trinket
                        trinket = s.get('trinket', 3340)
                        
                        # Dragones (tipos separados por coma)
                        team_dragons_types = ','.join([d['type'] for d in team_obj.get('dragons', [])])
                        if not team_dragons_types:
                            team_dragons_types = 'none'
                        
                        enemy_dragons_types = ','.join([d['type'] for d in enemy_obj.get('dragons', [])])
                        if not enemy_dragons_types:
                            enemy_dragons_types = 'none'
                        
                        writer.writerow([
                            ms,
                            minutes,
                            idx,
                            team_id,
                            p['champion_id'],
                            
                            # Gold & Level
                            s.get('gold', 0),
                            s.get('current_gold', 0),
                            s.get('level', 0),
                            s.get('minions_killed', 0),
                            s.get('xp', 0),
                            
                            # Posición
                            s['pos']['x'],
                            s['pos']['y'],
                            
                            # KDA
                            s.get('kills', 0),
                            s.get('deaths', 0),
                            s.get('assists', 0),
                            
                            # Daño
                            s.get('damage_done', 0),
                            s.get('damage_taken', 0),
                            s.get('damage_to_champions', 0),
                            
                            # Estado
                            1 if s.get('is_dead', False) else 0,
                            1 if s.get('in_teamfight', False) else 0,
                            
                            # Items + Trinket
                            items_str,
                            trinket,
                            
                            # Objetivos del equipo (counts)
                            len(team_obj.get('dragons', [])),
                            len(team_obj.get('barons', [])),
                            len(team_obj.get('towers', [])),
                            len(team_obj.get('inhibitors', [])),
                            len(team_obj.get('heralds', [])),
                            
                            # Objetivos del enemigo (counts)
                            len(enemy_obj.get('dragons', [])),
                            len(enemy_obj.get('barons', [])),
                            len(enemy_obj.get('towers', [])),
                            len(enemy_obj.get('inhibitors', [])),
                            len(enemy_obj.get('heralds', [])),
                            
                            # Dragones por tipo
                            team_dragons_types,
                            enemy_dragons_types
                        ])
            
            log(f"   ✅ CSV: ai_training_timeline.csv (DATASET COMPLETO)")
            
            # ═══ ESTADÍSTICAS FINALES ═══
            total_rows = len(self.metadata_history) * 10
            duration_min = self.metadata_history[-1]['game_time_min'] if self.metadata_history else 0
            
            log(f"\n📊 ESTADÍSTICAS:")
            log(f"   Snapshots: {len(self.metadata_history)}")
            log(f"   Rows en CSV: {total_rows}")
            log(f"   Duración juego: {duration_min:.1f} min")
            log(f"   Columnas: 32 (gold, KDA, daño, items, trinket, objetivos, teamfights)")
            
        except Exception as e:
            log(f"❌ Error generando CSV: {e}", "ERROR")
            import traceback
            traceback.print_exc()

# ══════════════════════════════════════════════════════════════
# SPECTATOR RECORDER (CORE ROBUSTO)
# ══════════════════════════════════════════════════════════════

class SpectatorRecorder:
    def __init__(self, game_id, encryption_key, platform_id=PLATFORM_ID, 
                 player_name="Unknown", extra_metadata=None, metadata_only=False):
        self.game_id = game_id
        self.encryption_key = encryption_key
        self.platform_id = platform_id
        self.player_name = player_name
        self.extra_metadata = extra_metadata or {}
        self.metadata_only = metadata_only
        
        self.spectator_url = f"http://{SPECTATOR_SERVERS.get(platform_id, 'spectator.kr.lol.pvp.net:8080')}"
        
        self.chunks = {}
        self.keyframes = {}
        self.game_metadata = None
        
        self.recording = False
        self.start_time = None
        self.end_time = None
        self.consecutive_errors = 0
        self.total_chunks = 0
        self.total_keyframes = 0
        self.game_length_ms = 0
        
        self.start_game_chunk_id = 1
        self.end_startup_chunk_id = 0
        self.keyframe_interval = 60000
        
        self.temp_dir = os.path.join(SAVE_PATH, f"temp_{game_id}")
        os.makedirs(self.temp_dir, exist_ok=True)
        
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "Mozilla/5.0 RiotClient/12.0.0"})

    def _spectator_request(self, method, params="", timeout=15):
        """Request con manejo de 429 y fallback de port."""
        global LAST_SPECTATOR_REQUEST
        
        max_retries = 3
        for attempt in range(max_retries + 1):
            with GLOBAL_SPECTATOR_LOCK:
                elapsed = time.time() - LAST_SPECTATOR_REQUEST
                if elapsed < MIN_SPECTATOR_INTERVAL:
                    time.sleep(MIN_SPECTATOR_INTERVAL - elapsed)
                LAST_SPECTATOR_REQUEST = time.time()

            url = f"{self.spectator_url}/observer-mode/rest/consumer/{method}/{self.platform_id}/{self.game_id}/{params}token"
            try:
                r = self.session.get(url, timeout=timeout)
                if r.status_code == 429:
                    wait = 5 * (attempt + 1)
                    log(f"⚠️ 429 RateLimit. Esperando {wait}s...", "WARN")
                    time.sleep(wait)
                    continue
                return r
            except:
                if attempt == 0:  # Primer fallo, probar puerto 80
                    alt = SPECTATOR_SERVERS_ALT.get(self.platform_id)
                    if alt:
                        self.spectator_url = f"http://{alt}"
                        continue
                if attempt < max_retries:
                    time.sleep(2)
                    continue
        return None

    def start_recording(self):
        log(f"\n🔴 INICIANDO GRABACIÓN: {self.game_id} ({self.player_name})")
        self.recording = True
        self.start_time = time.time()
        
        collector = MetadataCollector(self, interval=20)
        
        # Metadata inicial
        r_meta = self._spectator_request("getGameMetaData", "1/")
        if r_meta and r_meta.status_code == 200:
            self.game_metadata = r_meta.json()
            self.start_game_chunk_id = self.game_metadata.get('startGameChunkId', 1)
            self.end_startup_chunk_id = self.game_metadata.get('endStartupChunkId', 0)
            self.keyframe_interval = self.game_metadata.get('interestScore', 60000)
            with open(os.path.join(self.temp_dir, "metadata.json"), 'w') as f:
                json.dump(self.game_metadata, f, indent=2)

        last_chunk_reported = 0
        while self.recording:
            # ═══ CAPTURA DE METADATA PARA IA ═══
            collector.collect_metadata()
            
            # ═══ POLLING DE CHUNKS ═══
            r_info = self._spectator_request("getLastChunkInfo", "0/")
            if not r_info or r_info.status_code != 200:
                self.consecutive_errors += 1
                if self.consecutive_errors > MAX_CONSECUTIVE_ERRORS:
                    break
                time.sleep(10)
                continue
            
            self.consecutive_errors = 0
            info = r_info.json()
            chunk_id = info.get('chunkId', 0)
            kf_id = info.get('keyFrameId', 0)
            end_id = info.get('endGameChunkId', 0)
            
            # Descargar Chunks pendientes
            for cid in range(max(1, chunk_id - 5), chunk_id + 1):
                if cid not in self.chunks:
                    r_c = self._spectator_request("getGameDataChunk", f"{cid}/")
                    if r_c and r_c.status_code == 200:
                        self.chunks[cid] = r_c.content
                        with open(os.path.join(self.temp_dir, f"chunk_{cid}.bin"), 'wb') as f:
                            f.write(r_c.content)
                        self.total_chunks += 1

            # Descargar Keyframes pendientes
            for kid in range(max(1, kf_id - 1), kf_id + 1):
                if kid not in self.keyframes:
                    r_k = self._spectator_request("getKeyFrame", f"{kid}/")
                    if r_k and r_k.status_code == 200:
                        self.keyframes[kid] = r_k.content
                        with open(os.path.join(self.temp_dir, f"kf_{kid}.bin"), 'wb') as f:
                            f.write(r_k.content)
                        self.total_keyframes += 1

            if chunk_id > last_chunk_reported:
                elapsed_min = (time.time() - self.start_time) / 60
                log(f"   📡 Chunk {chunk_id} | KF {kf_id} | {elapsed_min:.1f}min grabando")
                last_chunk_reported = chunk_id

            if end_id > 0 and chunk_id >= end_id:
                log(f"🏁 Partida terminada (Chunk {end_id})")
                break
                
            time.sleep(min(info.get('nextAvailableChunk', 10000) / 1000, 20))

        self.end_time = time.time()
        self.recording = False
        self.game_length_ms = int((self.end_time - self.start_time) * 1000)
        
        # ═══ PASO CRÍTICO: ENRIQUECER CON TIMELINE ═══
        log(f"\n{'='*65}")
        log(f"📊 FASE DE ENRIQUECIMIENTO DE DATOS")
        log(f"{'='*65}")
        collector.enrich_with_timeline()
        collector.save_final_reports()
        
        # ═══ ENSAMBLAR ROFL ═══
        return self.assemble_rofl()

    def assemble_rofl(self):
        """Ensamblado binario completo del archivo .rofl."""
        if not self.chunks:
            log("⚠️ No hay chunks para ensamblar", "WARN")
            return None
            
        log(f"\n🔧 Ensamblando .rofl...")
        try:
            # Metadata
            meta = {
                "gameId": self.game_id,
                "platformId": self.platform_id,
                "encryptionKey": self.encryption_key
            }
            if self.game_metadata:
                meta.update(self.game_metadata)
            meta_b = json.dumps(meta, separators=(',', ':')).encode('utf-8')
            
            # Entradas de Datos
            entries = []
            for k in sorted(self.keyframes.keys()):
                entries.append({'id': k, 'type': 1, 'data': self.keyframes[k]})
            for i, c in enumerate(sorted(self.chunks.keys())):
                nxt = sorted(self.chunks.keys())[i+1] if i+1 < len(self.chunks) else 0
                entries.append({'id': c, 'type': 2, 'data': self.chunks[c], 'next': nxt})

            # Payload
            payload_data = bytearray()
            h_size = len(entries) * 17
            off = 0
            for e in entries:
                payload_data += struct.pack('<IBIII', e['id'], e['type'], len(e['data']), e.get('next', 0), off + h_size)
                off += len(e['data'])
            for e in entries:
                payload_data += e['data']

            # Payload Header
            enc_b = self.encryption_key.encode('utf-8')
            p_header = struct.pack('<QIIIIIIH', self.game_id, self.game_length_ms, len(self.keyframes), len(self.chunks), 
                                   self.end_startup_chunk_id, self.start_game_chunk_id, 60000, len(enc_b)) + enc_b
            
            # File Header
            h_len = 288
            m_off = h_len
            m_len = len(meta_b)
            ph_off = m_off + m_len
            ph_len = len(p_header)
            p_off = ph_off + ph_len
            f_len = p_off + len(payload_data)
            
            f_header = b'RIOT\x00\x00' + b'\x00' * 256 + struct.pack('<HIIIIIII', h_len, f_len, m_off, m_len, ph_off, ph_len, p_off, 0)
            
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            path = os.path.join(SAVE_PATH, f"KR_{self.game_id}_{timestamp}.rofl")
            with open(path, 'wb') as f:
                f.write(f_header)
                f.write(meta_b)
                f.write(p_header)
                f.write(payload_data)
            
            log(f"✅ Replay guardado: {path} ({f_len/1024/1024:.2f} MB)")
            return path
        except Exception as e:
            log(f"❌ Error ensamblado: {e}", "ERROR")
            import traceback
            traceback.print_exc()
            return None

# ══════════════════════════════════════════════════════════════
# FUNCIONES RIOT API
# ══════════════════════════════════════════════════════════════

def safe_api_call(url, headers, max_retries=3):
    for attempt in range(max_retries):
        try:
            r = requests.get(url, headers=headers, timeout=12)
            if r.status_code == 429:
                time.sleep(10)
                continue
            return r
        except:
            time.sleep(2)
    return None

def get_summoner_name_from_puuid(puuid):
    """Utiliza la Account-V1 para obtener el nombre real (Riot ID #TAG)."""
    url = f"https://{REGION_ROUTING}.api.riotgames.com/riot/account/v1/accounts/by-puuid/{puuid}"
    r = safe_api_call(url, {"X-Riot-Token": API_KEY})
    if r and r.status_code == 200:
        data = r.json()
        return f"{data.get('gameName')}#{data.get('tagLine')}"
    return "Unknown"

def check_active_game(puuid):
    url = f"https://{REGION_PLATFORM}.api.riotgames.com/lol/spectator/v5/active-games/by-summoner/{puuid}"
    r = safe_api_call(url, {"X-Riot-Token": API_KEY})
    return r.json() if r and r.status_code == 200 else None

def get_challenger_and_grandmaster_list():
    log("🏆 Cargando el Top de jugadores de KR (Challenger + GM)...")
    players = []
    headers = {"X-Riot-Token": API_KEY}
    
    r_c = safe_api_call(f"https://{REGION_PLATFORM}.api.riotgames.com/lol/league/v4/challengerleagues/by-queue/RANKED_SOLO_5x5", headers)
    if r_c and r_c.status_code == 200:
        players.extend(r_c.json()['entries'])
        
    r_gm = safe_api_call(f"https://{REGION_PLATFORM}.api.riotgames.com/lol/league/v4/grandmasterleagues/by-queue/RANKED_SOLO_5x5", headers)
    if r_gm and r_gm.status_code == 200:
        players.extend(sorted(r_gm.json()['entries'], key=lambda x: x['leaguePoints'], reverse=True)[:200])
        
    log(f"✅ {len(players)} jugadores en lista de monitoreo.")
    return players

# ══════════════════════════════════════════════════════════════
# MODO MONITOR
# ══════════════════════════════════════════════════════════════

def record_game_thread(game_id, enc_key, name, full_info, metadata_only):
    global LAST_GAME_END_TIME
    recorder = SpectatorRecorder(game_id, enc_key, player_name=name, extra_metadata=full_info, metadata_only=metadata_only)
    path = recorder.start_recording()
    
    if path:
        completed_recordings.add(str(game_id))
        with open(STATE_FILE, 'w') as f:
            json.dump({"completed": list(completed_recordings)}, f)
    
    if str(game_id) in active_recordings:
        del active_recordings[str(game_id)]
    
    LAST_GAME_END_TIME = time.time()
    log(f"❄️ Periodo de enfriamiento activado tras terminar Game {game_id}")

def monitor_mode(metadata_only=False):
    log("🚀 MODO MONITOR COREA (KR) ACTIVADO")
    global challenger_players, LAST_GAME_END_TIME
    challenger_players = get_challenger_and_grandmaster_list()
    
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, 'r') as f:
                completed_recordings.update(json.load(f).get('completed', []))
        except:
            pass

    while True:
        time_since_last = time.time() - LAST_GAME_END_TIME
        if time_since_last < 120:
            wait = int(120 - time_since_last)
            log(f"❄️ Enfriando... {wait}s restantes", "DEBUG")
            time.sleep(20)
            continue

        for p in challenger_players:
            if len(active_recordings) >= 1:
                break

            puuid = p.get('puuid')
            if not puuid:
                continue
            
            game = check_active_game(puuid)
            if game:
                game_id = str(game['gameId'])
                if game_id not in completed_recordings and game_id not in active_recordings:
                    if game.get('gameQueueConfigId') in [420, 440]:
                        start = game.get('gameStartTime', 0)
                        elapsed_sec = (time.time() * 1000 - start) / 1000
                        
                        if elapsed_sec < 300:
                            real_name = get_summoner_name_from_puuid(puuid)
                            log(f"\n🆕 NUEVA PARTIDA: {real_name} | Game {game_id}")
                            
                            active_recordings[game_id] = True
                            t = threading.Thread(target=record_game_thread, args=(game_id, game['observers']['encryptionKey'], real_name, game, metadata_only))
                            t.daemon = True
                            t.start()
                            
                            log(f"🔒 Grabando (1 partida máxima)")
                            time.sleep(5)
                            break
            time.sleep(0.5)
            
        log(f"😴 Activas: {len(active_recordings)}. Esperando {CHECK_INTERVAL}s...", "DEBUG")
        time.sleep(CHECK_INTERVAL)

def main():
    parser = argparse.ArgumentParser(description="LoL Replay Recorder PRO (IA + Replay) v2.0")
    parser.add_argument('--puuid', type=str, help='PUUID para grabar partida específica')
    parser.add_argument('--metadata-only', action='store_true', help='Solo extraer datos de IA, no bajar replay')
    args = parser.parse_args()
    
    print("\n" + "═"*65)
    print("   GRABADOR DE REPLAYS PRO v2.0 - TIMING EXACTO")
    print("   ───────────────────────────────────────────────")
    print(f"   Modo: {'METADATA ONLY' if args.metadata_only else 'REPLAY + METADATA'}")
    print(f"   Intervalo: 20 segundos exactos (timing absoluto)")
    print(f"   Región: {PLATFORM_ID}")
    print("═"*65 + "\n")
    
    if args.puuid:
        info = check_active_game(args.puuid)
        if info:
            name = get_summoner_name_from_puuid(args.puuid)
            record_game_thread(info['gameId'], info['observers']['encryptionKey'], name, info, args.metadata_only)
        else:
            print("❌ El jugador no está en partida.")
    else:
        monitor_mode(args.metadata_only)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("👋 Detenido.")
        sys.exit(0)