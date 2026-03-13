#!/usr/bin/env python3
"""
GRABADOR DE REPLAYS VIA SPECTATOR API v3.1
==========================================
FIXES vs versión anterior:
  ✅ Items de los 10 jugadores (no solo el espectado)
  ✅ Snapshots exactamente cada 20s (thread independiente)
  ✅ Sistema de items lineal: una pasada por PURCHASED/SOLD/UNDO
  ✅ Participants siempre desde getGameMetaData (tiene los 10)
"""

import argparse
import binascii
import csv
import json
import mmap
import os
import struct
import sys
import threading
import time
from datetime import datetime
from typing import List

import requests
from urllib3.exceptions import InsecureRequestWarning

requests.packages.urllib3.disable_warnings(InsecureRequestWarning)

# ══════════════════════════════════════════════════════════════
# CONFIGURACIÓN
# ══════════════════════════════════════════════════════════════

API_KEY         = os.getenv('RIOT_API_KEY', 'RGAPI-f3ed243c-dea4-4d4c-8302-52dd9105fa93')
REGION_PLATFORM = "kr"
REGION_ROUTING  = "asia"
PLATFORM_ID     = "KR"

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

SAVE_PATH              = "F:\Replays_Recorded_KR"
LOG_FILE               = "spectator_recorder.log"
STATE_FILE             = "recorder_state.json"

CHECK_INTERVAL         = 120
MAX_CONSECUTIVE_ERRORS = 30

TRINKETS        = {3340, 3363, 3364, 3362, 3361}
STEALTH_WARD_ID = 3340

VERBOSE        = False
DUMP_SNAPSHOTS = False

os.makedirs(SAVE_PATH, exist_ok=True)

active_recordings    = {}
completed_recordings = set()
challenger_players   = []

GLOBAL_SPECTATOR_LOCK  = threading.Lock()
LAST_SPECTATOR_REQUEST = 0
LAST_GAME_END_TIME     = 0
MIN_SPECTATOR_INTERVAL = 1.5


# ══════════════════════════════════════════════════════════════
# LOGGING
# ══════════════════════════════════════════════════════════════

def log(message, level="INFO"):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_msg   = f"[{timestamp}] [{level}] {message}"
    if level != "DEBUG" or VERBOSE:
        try:
            print(log_msg)
        except Exception:
            pass
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(log_msg + "\n")
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════
# RIOT API HELPERS
# ══════════════════════════════════════════════════════════════

def safe_api_call(url, headers, max_retries=3):
    for attempt in range(max_retries):
        try:
            r = requests.get(url, headers=headers, timeout=12)
            if r.status_code == 429:
                time.sleep(10)
                continue
            return r
        except Exception:
            time.sleep(2)
    return None


def get_timeline(game_id: int):
    match_id = f"{PLATFORM_ID}_{game_id}"
    url      = f"https://{REGION_ROUTING}.api.riotgames.com/lol/match/v5/matches/{match_id}/timeline"
    headers  = {"X-Riot-Token": API_KEY}
    for attempt in range(6):
        if attempt > 0:
            time.sleep(20)
        log(f"   ⏳ Timeline intento {attempt+1}/6...")
        try:
            r = safe_api_call(url, headers)
            if r and r.status_code == 200:
                log("   ✅ Timeline obtenido")
                return r.json()
            elif r and r.status_code == 404:
                log("   ⏳ No disponible aún (404)")
            else:
                log(f"   ⚠️ HTTP {r.status_code if r else 'Timeout'}")
        except Exception as e:
            log(f"   ⚠️ Error: {e}")
    log("❌ Timeline no disponible tras 6 intentos", "WARN")
    return None


CHAMPIONS_DATA = {}
def get_champions_data():
    global CHAMPIONS_DATA
    if CHAMPIONS_DATA: return CHAMPIONS_DATA
    try:
        res = requests.get("https://ddragon.leagueoflegends.com/api/versions.json", timeout=5)
        latest_version = res.json()[0]
        res = requests.get(f"https://ddragon.leagueoflegends.com/cdn/{latest_version}/data/en_US/champion.json", timeout=5)
        champs = res.json()["data"]
        for k, v in champs.items():
            CHAMPIONS_DATA[int(v["key"])] = v["name"]
    except Exception as e:
        log(f"   ⚠️ Error al obtener datos de campeones DDragon: {e}", "DEBUG")
    return CHAMPIONS_DATA

ITEMS_DATA = {}
def get_items_data():
    global ITEMS_DATA
    if ITEMS_DATA: return ITEMS_DATA
    try:
        res = requests.get("https://ddragon.leagueoflegends.com/api/versions.json", timeout=5)
        latest_version = res.json()[0]
        res = requests.get(f"https://ddragon.leagueoflegends.com/cdn/{latest_version}/data/en_US/item.json", timeout=5)
        ITEMS_DATA = res.json().get("data", {})
        log(f"   📦 {len(ITEMS_DATA)} items cargados de DDragon ({latest_version})")
    except Exception as e:
        log(f"   ⚠️ Error al obtener datos de items DDragon: {e}", "DEBUG")
    return ITEMS_DATA

def get_match_info(game_id: int):
    match_id = f"{PLATFORM_ID}_{game_id}"
    url      = f"https://{REGION_ROUTING}.api.riotgames.com/lol/match/v5/matches/{match_id}"
    headers  = {"X-Riot-Token": API_KEY}
    for attempt in range(6):
        if attempt > 0:
            time.sleep(20)
        log(f"   ⏳ Match Info intento {attempt+1}/6...")
        try:
            r = safe_api_call(url, headers)
            if r and r.status_code == 200:
                log("   ✅ Match Info obtenido")
                return r.json()
            elif r and r.status_code == 404:
                log("   ⏳ No disponible aún (404)")
            else:
                log(f"   ⚠️ HTTP {r.status_code if r else 'Timeout'}")
        except Exception as e:
            log(f"   ⚠️ Error: {e}")
    log("❌ Match Info no disponible tras 6 intentos", "WARN")
    return None


def get_summoner_name_from_puuid(puuid):
    url = f"https://{REGION_ROUTING}.api.riotgames.com/riot/account/v1/accounts/by-puuid/{puuid}"
    r   = safe_api_call(url, {"X-Riot-Token": API_KEY})
    if r and r.status_code == 200:
        d = r.json()
        return f"{d.get('gameName')}#{d.get('tagLine')}"
    return "Unknown"


def check_active_game(puuid):
    url = f"https://{REGION_PLATFORM}.api.riotgames.com/lol/spectator/v5/active-games/by-summoner/{puuid}"
    r   = safe_api_call(url, {"X-Riot-Token": API_KEY})
    return r.json() if r and r.status_code == 200 else None


def get_challenger_and_grandmaster_list():
    log("🏆 Cargando Top jugadores KR (Challenger + GM)...")
    players = []
    headers = {"X-Riot-Token": API_KEY}
    r_c = safe_api_call(
        f"https://{REGION_PLATFORM}.api.riotgames.com/lol/league/v4/challengerleagues/by-queue/RANKED_SOLO_5x5",
        headers
    )
    if r_c and r_c.status_code == 200:
        players.extend(r_c.json()['entries'])
    r_gm = safe_api_call(
        f"https://{REGION_PLATFORM}.api.riotgames.com/lol/league/v4/grandmasterleagues/by-queue/RANKED_SOLO_5x5",
        headers
    )
    if r_gm and r_gm.status_code == 200:
        players.extend(sorted(r_gm.json()['entries'], key=lambda x: x['leaguePoints'], reverse=True)[:200])
    log(f"✅ {len(players)} jugadores en lista de monitoreo.")
    return players


# ══════════════════════════════════════════════════════════════
# ITEM TRACKER — pasada lineal
# ══════════════════════════════════════════════════════════════

def extract_item_events(timeline: dict) -> list:
    """
    Extrae ITEM_PURCHASED / ITEM_SOLD / ITEM_DESTROYED / ITEM_UNDO del Timeline,
    ordenados por timestamp.
    """
    events = []
    for frame in timeline.get("info", {}).get("frames", []):
        for ev in frame.get("events", []):
            t = ev.get("type", "")
            if t in ("ITEM_PURCHASED", "ITEM_SOLD", "ITEM_DESTROYED", "ITEM_UNDO"):
                events.append({
                    "timestamp":     ev.get("timestamp", 0),
                    "participantId": ev.get("participantId"),
                    "type":          t,
                    "itemId":        ev.get("itemId"),
                    "beforeId":      ev.get("beforeId"),
                })
    events.sort(key=lambda e: e["timestamp"])
    bought    = sum(1 for e in events if e["type"] == "ITEM_PURCHASED")
    sold      = sum(1 for e in events if e["type"] == "ITEM_SOLD")
    destroyed = sum(1 for e in events if e["type"] == "ITEM_DESTROYED")
    undos     = sum(1 for e in events if e["type"] == "ITEM_UNDO")
    log(f"   📦 Eventos items: PURCHASED={bought} SOLD={sold} DESTROYED={destroyed} UNDO={undos}")
    return events


def build_inventories(item_events: list, snapshot_timestamps_ms: list) -> dict:
    """
    Avanza linealmente por los eventos y para cada timestamp
    guarda una COPIA del inventario en ese momento exacto.

    Retorna:
      { timestamp_ms: { participantId(1-10): {"items":[...], "trinket": id} } }
    """
    inventory = {
        pid: {"items": [], "trinket": STEALTH_WARD_ID}
        for pid in range(1, 11)
    }

    result       = {}
    sorted_ts    = sorted(snapshot_timestamps_ms)
    ev_idx       = 0
    total_events = len(item_events)

    for snap_ts in sorted_ts:
        # Aplicar todos los eventos hasta este timestamp
        while ev_idx < total_events and item_events[ev_idx]["timestamp"] <= snap_ts:
            ev  = item_events[ev_idx]
            pid = ev["participantId"]
            ev_idx += 1

            if pid is None or not (1 <= pid <= 10):
                continue

            item_id = ev["itemId"]
            inv     = inventory[pid]

            if ev["type"] == "ITEM_PURCHASED":
                if item_id in TRINKETS:
                    inv["trinket"] = item_id
                elif len(inv["items"]) < 6:
                    inv["items"].append(item_id)

            elif ev["type"] == "ITEM_SOLD":
                if item_id in inv["items"]:
                    inv["items"].remove(item_id)

            elif ev["type"] == "ITEM_DESTROYED":
                if item_id in TRINKETS:
                    pass  # Los trinkets destruidos se manejan con PURCHASED del nuevo
                elif item_id in inv["items"]:
                    inv["items"].remove(item_id)

            elif ev["type"] == "ITEM_UNDO":
                before_id = ev.get("beforeId")
                if before_id:
                    if before_id in TRINKETS:
                        inv["trinket"] = STEALTH_WARD_ID
                    elif before_id in inv["items"]:
                        inv["items"].remove(before_id)

        # Guardar COPIA (no referencia)
        result[snap_ts] = {
            pid: {
                "items":   list(inventory[pid]["items"]),
                "trinket": inventory[pid]["trinket"],
            }
            for pid in range(1, 11)
        }

    return result


# ══════════════════════════════════════════════════════════════
# METADATA COLLECTOR
# ══════════════════════════════════════════════════════════════

class MetadataCollector:
    """
    Captura snapshots cada 20s en un THREAD PROPIO (independiente
    del polling de chunks). Así el intervalo es siempre exacto.

    Al terminar la partida, enrich_with_timeline() rellena todos
    los datos desde la Match API v5 Timeline.
    """

    def __init__(self, recorder, interval: int = 20):
        self.recorder         = recorder
        self.interval         = interval
        self.metadata_history = []
        self._stop_event      = threading.Event()
        self._thread          = None
        # Sesion propia: NO usa GLOBAL_SPECTATOR_LOCK, no bloquea el chunk polling
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": "Mozilla/5.0 RiotClient/12.0.0"})

    # ─────────────────────────────────────────
    # Control del thread de captura
    # ─────────────────────────────────────────

    def start(self):
        """Lanza el thread de captura en background."""
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._capture_loop, daemon=True)
        self._thread.start()
        log(f"🕐 Thread de snapshots iniciado (cada {self.interval}s)")

    def stop(self):
        """Detiene el thread de captura."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
        log(f"🛑 Thread de snapshots detenido ({len(self.metadata_history)} snapshots capturados)")

    def _capture_loop(self):
        """
        Bucle interno: cada `interval` segundos captura un snapshot.
        Si no hay participants aun, reintenta cada 5s hasta tenerlos
        antes de entrar al ritmo normal de 20s.
        """
        # Debug: mostrar que hay en extra_metadata al arrancar
        extra = self.recorder.extra_metadata
        extra_parts = extra.get('participants', [])
        log(f"   🔍 extra_metadata: {len(extra_parts)} participants, keys={list(extra.keys())[:8]}")
        if extra_parts:
            log(f"      Ejemplo P1: {extra_parts[0]}", "DEBUG")

        # Esperar participants con reintentos cada 5s (sin bloquear stop_event)
        log("   🔍 Esperando participants...")
        while not self._stop_event.is_set():
            parts = self._get_participants()
            if parts:
                log(f"   ✅ {len(parts)} participants listos, iniciando snapshots")
                break
            log("   ⏳ Participants no disponibles aun, reintentando en 5s")
            for _ in range(5):
                if self._stop_event.is_set():
                    return
                time.sleep(1)

        # Bucle principal de snapshots cada 20s
        while not self._stop_event.is_set():
            self._capture_one()
            for _ in range(self.interval):
                if self._stop_event.is_set():
                    break
                time.sleep(1)

    # ─────────────────────────────────────────
    # Obtener los 10 participants
    # ─────────────────────────────────────────

    def _get_participants(self) -> list:
        """
        Orden de prioridad para obtener los 10 participants:
        1. extra_metadata (active-games): siempre disponible desde el inicio
        2. game_metadata (getGameMetaData Spectator): disponible algo despues
        """
        # ── FUENTE 1: active-games (disponible desde el primer momento) ──
        # El endpoint active-games devuelve los 10 participantes siempre.
        extra = self.recorder.extra_metadata.get('participants', [])
        if len(extra) == 10:
            return extra

        # Si tiene menos de 10 pero algo, usarlo igual (bots, custom games)
        if extra:
            return extra

        # ── FUENTE 2: getGameMetaData del Spectator (puede tardar) ──
        if self.recorder.game_metadata:
            parts = self.recorder.game_metadata.get('participants', [])
            if parts:
                return parts

        # ── FUENTE 3: pedir getGameMetaData con sesion propia (sin lock) ──
        rec = self.recorder
        url = (f"{rec.spectator_url}/observer-mode/rest/consumer/getGameMetaData"
               f"/{rec.platform_id}/{rec.game_id}/1/token")
        try:
            r = self._session.get(url, timeout=10)
            if r and r.status_code == 200:
                rec.game_metadata = r.json()
                parts = rec.game_metadata.get('participants', [])
                if parts:
                    log(f"   📋 Metadata obtenida por collector ({len(parts)} participants)", "DEBUG")
                    return parts
        except Exception as e:
            log(f"   ⚠️ Error obteniendo metadata: {e}", "DEBUG")

        return []

    def _enrich_puuids(self, participants: list):
        """
        Rellena PUUIDs faltantes en la lista de participants del Spectator
        usando los datos de extra_metadata (active-games).
        """
        extra_parts = self.recorder.extra_metadata.get('participants', [])
        if not extra_parts:
            return
        # Índice por summonerId para búsqueda rápida
        by_summoner = {ep.get('summonerId'): ep for ep in extra_parts}
        for p in participants:
            if not p.get('puuid'):
                sid = p.get('summonerId')
                ep  = by_summoner.get(sid)
                if ep:
                    p['puuid'] = ep.get('puuid', '')

    # ─────────────────────────────────────────
    # Captura de un snapshot
    # ─────────────────────────────────────────

    def _capture_one(self):
        try:
            participants = self._get_participants()
            if not participants:
                log("   ⚠️ Sin participants, snapshot omitido", "DEBUG")
                return

            # Calcular tiempo de juego
            game_length_ms = self._get_game_time_ms()

            snapshot = {
                "captured_at_iso":  datetime.now().isoformat(),
                "captured_at_unix": time.time(),
                "game_time_ms":     game_length_ms,
                "game_time_min":    round(game_length_ms / 60000, 2),
                "snapshot_number":  len(self.metadata_history) + 1,
                "participants":     [],
            }

            for idx, p in enumerate(participants):
                puuid = p.get('puuid') or ''
                team_id = p.get('teamId', 0)
                p_data = {
                    "participant_id": p.get('participantId', idx + 1),
                    "puuid":          puuid,
                    "name":           p.get('summonerName') or p.get('riotId', 'Unknown'),
                    "champion_id":    p.get('championId', 0),
                    "team_id":        team_id,
                    "team_color":     "Blue" if team_id == 100 else "Red" if team_id == 200 else str(team_id),
                    "stats": {
                        "level": 0, "gold": 0, "current_gold": 0,
                        "minions_killed": 0, "xp": 0,
                        "health": 0, "health_max": 0,
                        "pos": {"x": 0, "y": 0},
                        "kills": 0, "deaths": 0, "assists": 0,
                        "recent_kills": [],
                        "is_dead": False, "in_teamfight": False,
                        "items": [], "trinket": STEALTH_WARD_ID,
                    },
                }
                snapshot["participants"].append(p_data)

            self.metadata_history.append(snapshot)
            log(f"   📋 Snapshot #{len(self.metadata_history)} "
                f"@ {snapshot['game_time_min']:.1f}min "
                f"({len(participants)} jugadores)", "DEBUG")

        except Exception as e:
            log(f"   ⚠️ Error capturando snapshot: {e}", "DEBUG")

    def _get_game_time_ms(self) -> int:
        """Calcula el tiempo de juego en ms con varias fuentes."""
        # Fuente 1: gameLength del metadata del Spectator
        if self.recorder.game_metadata:
            gl = self.recorder.game_metadata.get('gameLength', 0)
            if gl > 0:
                return gl

        # Fuente 2: gameStartTime del active-games
        start = self.recorder.extra_metadata.get('gameStartTime', 0)
        if start > 0:
            return int(time.time() * 1000 - start)

        # Fuente 3: tiempo desde que empezamos a grabar
        if self.recorder.start_time:
            return int((time.time() - self.recorder.start_time) * 1000)

        return 0

    # ─────────────────────────────────────────
    # Enriquecimiento con Timeline (al final)
    # ─────────────────────────────────────────

    def enrich_with_timeline(self):
        log(f"\n🕵️ ENRIQUECIENDO CON TIMELINE API v3.1")
        log(f"   Snapshots capturados: {len(self.metadata_history)}")

        if not self.metadata_history:
            log("⚠️ Sin snapshots", "WARN")
            return False

        timeline = get_timeline(self.recorder.game_id)
        if not timeline:
            return False

        match_info = get_match_info(self.recorder.game_id)
        
        champs_db = get_champions_data()
        puuid_to_role = {}
        puuid_to_champname = {}
        winning_team = 0
        
        if match_info:
            for t in match_info.get("info", {}).get("teams", []):
                if t.get("win"):
                    winning_team = t.get("teamId")
                    
            for p in match_info.get("info", {}).get("participants", []):
                puuid = p.get("puuid")
                role_raw = p.get("teamPosition", "")
                
                role = ""
                if role_raw == "TOP": role = "top"
                elif role_raw == "JUNGLE": role = "jungla"
                elif role_raw == "MIDDLE": role = "mid"
                elif role_raw == "BOTTOM": role = "adc"
                elif role_raw == "UTILITY": role = "support"
                
                if puuid:
                    puuid_to_role[puuid] = role
                    puuid_to_champname[puuid] = p.get("championName", "")

        try:
            info   = timeline.get('info', {})
            frames = info.get('frames', [])
            log(f"   📊 Timeline: {len(frames)} frames")

            # ── Mapeo PUUID <-> participantId del Timeline ──
            tl_parts     = info.get('participants', [])
            puuid_to_pid = {p['puuid']: p['participantId'] for p in tl_parts if isinstance(p, dict)}
            pid_to_puuid = {p['participantId']: p['puuid']  for p in tl_parts if isinstance(p, dict)}
            log(f"   🔗 {len(puuid_to_pid)} PUUIDs mapeados desde Timeline")

            # ── Detectar PUUIDs duplicados (jugadores con nombre oculto) ──
            # Cuando hay PUUIDs duplicados, mapeamos por posición (participant_id)
            puuid_counts = {}
            for p_data_check in (self.metadata_history[0]['participants'] if self.metadata_history else []):
                pu = p_data_check.get('puuid', '')
                if pu:
                    puuid_counts[pu] = puuid_counts.get(pu, 0) + 1
            duplicated_puuids = {pu for pu, count in puuid_counts.items() if count > 1}
            if duplicated_puuids:
                log(f"   ⚠️ {len(duplicated_puuids)} PUUIDs duplicados detectados, usando mapeo por participant_id")

            # ── Corregir PUUIDs faltantes en snapshots usando el Timeline ──
            all_champ_names = set(champs_db.values())  # Para detectar nombres ocultos
            for snap in self.metadata_history:
                for p_data in snap['participants']:
                    snap_pid = p_data.get('participant_id', 0)
                    puuid = p_data.get('puuid', '')

                    # Si el PUUID está duplicado, asignar el PUUID correcto del Timeline por posición
                    if puuid in duplicated_puuids:
                        correct_puuid = pid_to_puuid.get(snap_pid, '')
                        if correct_puuid:
                            p_data['puuid'] = correct_puuid
                            puuid = correct_puuid
                    elif not puuid and snap_pid in pid_to_puuid:
                        p_data['puuid'] = pid_to_puuid[snap_pid]
                        puuid = p_data['puuid']
                    
                    c_id  = p_data.get('champion_id', 0)
                    
                    c_name = puuid_to_champname.get(puuid)
                    if not c_name:
                        c_name = champs_db.get(c_id, "Unknown")
                        
                    p_data['champion_name'] = c_name
                    p_data['role'] = puuid_to_role.get(puuid, "")

                    # Usar champion_name como nombre si el nombre actual parece ser un champion
                    current_name = p_data.get('name', 'Unknown')
                    if current_name in all_champ_names or current_name == 'Unknown':
                        p_data['name'] = c_name

            # ── Sincronizar timestamps ──
            # El Timeline empieza cuando los jugadores salen de la base (t≈0).
            # Nuestros snapshots pueden incluir la pantalla de carga (tiempo negativo).
            if frames:
                first_tl_ts   = frames[0].get('timestamp', 0)
                first_valid   = next((s for s in self.metadata_history
                                      if s['game_time_ms'] >= first_tl_ts), None)
                if first_valid:
                    offset = first_valid['game_time_ms'] - first_tl_ts
                    log(f"   🔄 Offset: {offset}ms ({offset/60000:.1f}min)")
                    for snap in self.metadata_history:
                        snap['game_time_ms']  -= offset
                        snap['game_time_min']  = round(snap['game_time_ms'] / 60000, 2)
                    before = len(self.metadata_history)
                    self.metadata_history = [s for s in self.metadata_history
                                             if s['game_time_ms'] >= 0]
                    removed = before - len(self.metadata_history)
                    if removed:
                        log(f"   ✂️  {removed} snapshots pre-juego eliminados")
                    log(f"   ✅ {len(self.metadata_history)} snapshots válidos tras sincronización")

            # ══════════════════════════════════════════
            # ITEMS: pasada lineal (los 10 jugadores)
            # ══════════════════════════════════════════
            item_events        = extract_item_events(timeline)
            snap_timestamps    = [s['game_time_ms'] for s in self.metadata_history]
            inventories        = build_inventories(item_events, snap_timestamps)

            # ── KDA acumulativo + kill damage ──
            kda_history, kill_damage_log = self._build_kda_history(frames)

            # ── Tracking de objetivos y sus tiempos de reaparición ──
            # Tiempos base en ms: Dragón 300000ms (5m), Baron 360000ms (6m), Inhib 300000ms (5m)
            # El Heraldo reaparece una vez si muere antes del min 13:45, con cd de 360000 (6m)
            team_dragons    = {100: [], 200: []}
            team_barons     = {100: [], 200: []}
            team_towers     = {100: [], 200: []}
            team_inhibitors = {100: [], 200: []}
            team_heralds    = {100: [], 200: []}
            
            global_dragon_respawn = 0
            global_baron_respawn  = 0
            global_herald_respawn = 0
            
            # Para inhibidores, guardamos el respawn por línea y equipo: "100_MID": timestamp
            inhibitor_respawns = {}
            
            recent_deaths   = []
            participant_death_history = {i: [] for i in range(1, 11)}

            for frame in frames:
                frame_ts = frame.get('timestamp', 0)
                for ev in frame.get('events', []):
                    et    = ev.get('type')
                    ev_ts = ev.get('timestamp', frame_ts)

                    if et == 'CHAMPION_KILL':
                        vid = ev.get('victimId')
                        if vid and 1 <= vid <= 10:
                            recent_deaths.append({"timestamp": ev_ts, "victim_id": vid - 1})
                            # Calcular respawn time con formula Patch 14.16+
                            victim_pf = frame.get('participantFrames', {}).get(str(vid), {})
                            victim_level = victim_pf.get('level', 1)
                            death_timer = self._calculate_death_timer(victim_level, ev_ts)
                            participant_death_history[vid].append({
                                "death_timestamp": ev_ts,
                                "respawn_time": ev_ts + (death_timer * 1000)
                            })

                    elif et == 'ELITE_MONSTER_KILL':
                        monster = ev.get('monsterType')
                        team    = ev.get('killerTeamId')
                        
                        if monster == 'DRAGON':
                            global_dragon_respawn = ev_ts + 300000 # 5 min
                            if team in (100, 200):
                                team_dragons[team].append({
                                    "type":      ev.get('monsterSubType', 'UNKNOWN'),
                                    "timestamp": ev_ts,
                                    "minute":    round(ev_ts / 60000, 1),
                                })
                        elif monster == 'BARON_NASHOR':
                            global_baron_respawn = ev_ts + 360000 # 6 min
                            if team in (100, 200):
                                team_barons[team].append({"timestamp": ev_ts, "minute": round(ev_ts / 60000, 1)})
                        elif monster == 'RIFTHERALD':
                            if ev_ts < 825000: # Si muere antes del 13:45 puede reaparecer
                                global_herald_respawn = ev_ts + 360000
                            if team in (100, 200):
                                team_heralds[team].append({"timestamp": ev_ts, "minute": round(ev_ts / 60000, 1)})

                    elif et == 'BUILDING_KILL':
                        building = ev.get('buildingType')
                        team     = ev.get('killerTeamId')
                        victim_team = ev.get('teamId') # Quien pierde el edificio
                        
                        if building == 'TOWER_BUILDING':
                            if team in (100, 200):
                                team_towers[team].append({
                                    "lane": ev.get('laneType', 'UNKNOWN'),
                                    "tier": ev.get('towerType', 'UNKNOWN'),
                                    "timestamp": ev_ts, "minute": round(ev_ts / 60000, 1),
                                })
                        elif building == 'INHIBITOR_BUILDING':
                            lane = ev.get('laneType', 'UNKNOWN')
                            if victim_team in (100, 200):
                                inhibitor_respawns[f"{victim_team}_{lane}"] = ev_ts + 300000 # 5 min
                            if team in (100, 200):
                                team_inhibitors[team].append({
                                    "lane": lane,
                                    "timestamp": ev_ts, "minute": round(ev_ts / 60000, 1),
                                })
                    
                    elif et == 'BUILDING_RESPAWN': # Si el inhibidor revive por Riot API explícitamente
                        building = ev.get('buildingType')
                        if building == 'INHIBITOR_BUILDING':
                            lane = ev.get('laneType', 'UNKNOWN')
                            revived_team = ev.get('teamId')
                            key = f"{revived_team}_{lane}"
                            if key in inhibitor_respawns:
                                del inhibitor_respawns[key]

            # ── Rellenar cada snapshot ──
            prev_ms = -1
            tf_state = {"active": False}
            for snap in self.metadata_history:
                ms        = snap['game_time_ms']
                frame_idx = min(max(int(ms / 60000), 0), len(frames) - 1)
                frame     = frames[frame_idx]
                frame_ts  = frame.get('timestamp', frame_idx * 60000)
                p_frames  = frame.get('participantFrames', {})

                inv_at_ts = inventories.get(ms, {})
                kda_at_ts       = self._get_kda_at(kda_history, ms)
                kill_events_window = self._get_kill_events_in_window(kill_damage_log, prev_ms, ms)
                tf_status       = self._detect_teamfight(frame, recent_deaths, frame_ts, tf_state)

                timeline_pid_to_snap_id = {}
                for p_data in snap['participants']:
                    if p_data.get('puuid'):
                        t_pid = puuid_to_pid.get(p_data['puuid'])
                        if t_pid:
                            timeline_pid_to_snap_id[t_pid] = p_data.get('participant_id')

                for p_data in snap['participants']:
                    puuid = p_data.get('puuid')
                    snap_pid = p_data.get('participant_id', 0)

                    # Determinar el timeline pid: por PUUID si es único, por participant_id si está duplicado
                    if puuid and puuid not in duplicated_puuids:
                        pid = puuid_to_pid.get(puuid)
                    else:
                        pid = snap_pid  # Mapeo directo por posición

                    if not pid:
                        continue
                    pf = p_frames.get(str(pid), {})
                    if not pf:
                        continue

                    player_inv = inv_at_ts.get(pid, {"items": [], "trinket": STEALTH_WARD_ID})
                    kda        = kda_at_ts.get(pid, {"kills": 0, "deaths": 0, "assists": 0})

                    # Obtener vida actual
                    health = pf.get('championStats', {}).get('health', 0)
                    health_max = pf.get('championStats', {}).get('healthMax', 0)

                    recent_death = None
                    for d_ev in participant_death_history.get(pid, []):
                        if d_ev["death_timestamp"] <= ms:
                            recent_death = d_ev
                        else:
                            break

                    is_dead = False
                    respawn_remaining = 0

                    if recent_death and ms < recent_death["respawn_time"]:
                        is_dead = True
                        respawn_remaining = max(0, round((recent_death["respawn_time"] - ms) / 1000, 1))
                        # Forzamos la vida a 0 mientras el jugador esta verdaderamente muerto, 
                        # ignorando la vida falsa proporcionada por Riot
                        health = 0
                    elif health == 0 and health_max > 0:
                        is_dead = True

                    # Mapear IDs de kills recientes para usar el participant_id del snapshot
                    mapped_recent_kills = []
                    for rk in kill_events_window.get(pid, []):
                        mapped_assistants = []
                        for asi in rk.get("assistants", []):
                            mapped_assistants.append({
                                "id": timeline_pid_to_snap_id.get(asi["id"], asi["id"]),
                                "damage": asi.get("damage", 0)
                            })
                        mapped_recent_kills.append({
                            "victim_id": timeline_pid_to_snap_id.get(rk["victim_id"], rk["victim_id"]),
                            "damage_dealt": rk["damage_dealt"],
                            "timestamp_ms": rk["timestamp_ms"],
                            "assistants": mapped_assistants
                        })

                    # Calcular gold_spent sumando coste de items del inventario
                    items_db = get_items_data()
                    gold_spent = 0
                    items_detailed = []
                    for iid in player_inv["items"]:
                        item_info = items_db.get(str(iid), {})
                        item_name = item_info.get("name", f"Item_{iid}")
                        item_cost = item_info.get("gold", {}).get("total", 0)
                        gold_spent += item_cost
                        items_detailed.append(f"[{iid}] |{item_name}")

                    trinket_id = player_inv["trinket"]
                    trinket_info = items_db.get(str(trinket_id), {})
                    trinket_name = trinket_info.get("name", f"Item_{trinket_id}")
                    trinket_detailed = f"[{trinket_id}] |{trinket_name}"

                    p_data['stats'].update({
                        "level":          pf.get('level', 0),
                        "gold":           pf.get('totalGold', 0),
                        "current_gold":   pf.get('currentGold', 0),
                        "gold_spent":     gold_spent,
                        "minions_killed": pf.get('minionsKilled', 0) + pf.get('jungleMinionsKilled', 0),
                        "xp":             pf.get('xp', 0),
                        "health":         health,
                        "health_max":     health_max,
                        "pos": {
                            "x": pf.get('position', {}).get('x', 0),
                            "y": pf.get('position', {}).get('y', 0),
                        },
                        # Kills recientes en este intervalo mapeadas a IDs reales
                        "recent_kills": mapped_recent_kills,
                        # Items del tracker lineal (todos los jugadores)
                        "items":          player_inv["items"],
                        "items_detailed": items_detailed,
                        "trinket":         player_inv["trinket"],
                        "trinket_detailed": trinket_detailed,
                        # KDA acumulativo
                        "kills":   kda["kills"],
                        "deaths":  kda["deaths"],
                        "assists": kda["assists"],
                        # Estado de muerte
                        "is_dead":                 is_dead,
                        "respawn_timer_remaining":  respawn_remaining,
                        "in_teamfight": tf_status.get(pid - 1, False),
                    })

                snap['objectives'] = self._build_objectives(
                    ms, frame_ts, team_dragons, team_barons, team_towers,
                    team_inhibitors, team_heralds, global_dragon_respawn, global_baron_respawn, global_herald_respawn, inhibitor_respawns
                )
                snap['winning_team'] = winning_team

                # Team gold totals y diferencia
                blue_gold = sum(p['stats'].get('gold', 0) for p in snap['participants'] if p['team_id'] == 100)
                red_gold  = sum(p['stats'].get('gold', 0) for p in snap['participants'] if p['team_id'] == 200)
                diff = blue_gold - red_gold
                if diff > 0:
                    diff_label = f"Blue +{diff}"
                elif diff < 0:
                    diff_label = f"Red +{abs(diff)}"
                else:
                    diff_label = "Even"
                snap['team_gold'] = {
                    "blue_total": blue_gold,
                    "red_total":  red_gold,
                    "gold_diff":  diff,
                    "gold_diff_label": diff_label,
                }

                prev_ms = ms



            # Volcado debug
            if DUMP_SNAPSHOTS:
                self._dump_debug()

            self._validate()
            log(f"\n✅ {len(self.metadata_history)} snapshots enriquecidos (v3.1)")
            return True

        except Exception as e:
            log(f"❌ Error procesando Timeline: {e}", "ERROR")
            import traceback
            traceback.print_exc()
            return False

    # ─────────────────────────────────────────
    # Helpers internos
    # ─────────────────────────────────────────

    def _build_kda_history(self, frames: list) -> list:
        running = {pid: {"kills": 0, "deaths": 0, "assists": 0} for pid in range(1, 11)}
        # kill_damage_log: list of {timestamp, killer_id, victim_id, damage_dealt}
        kill_damage_log = []
        history = []
        for frame in frames:
            for ev in frame.get('events', []):
                if ev.get('type') != 'CHAMPION_KILL':
                    continue
                ev_ts  = ev.get('timestamp', 0)
                killer = ev.get('killerId')
                victim = ev.get('victimId')
                if killer and 1 <= killer <= 10:
                    running[killer]["kills"] += 1
                if victim and 1 <= victim <= 10:
                    running[victim]["deaths"] += 1
                for a in ev.get('assistingParticipantIds', []):
                    if 1 <= a <= 10:
                        running[a]["assists"] += 1

                # Extraer daño que el killer y asistentes hicieron a la víctima
                victim_dmg_received = ev.get('victimDamageReceived', [])
                dmg_by_participant = {}
                for d in victim_dmg_received:
                    pid = d.get('participantId')
                    if pid and 1 <= pid <= 10:
                        dmg = d.get('magicDamage', 0) + d.get('physicalDamage', 0) + d.get('trueDamage', 0)
                        dmg_by_participant[pid] = dmg_by_participant.get(pid, 0) + dmg

                if killer and 1 <= killer <= 10:
                    killer_dmg = dmg_by_participant.get(killer, 0)
                    assists_data = []
                    for a in ev.get('assistingParticipantIds', []):
                        if 1 <= a <= 10:
                            assists_data.append({"id": a, "damage": dmg_by_participant.get(a, 0)})

                    kill_damage_log.append({
                        "timestamp":    ev_ts,
                        "killer_id":    killer,
                        "victim_id":    victim,
                        "damage_dealt": killer_dmg,
                        "assistants":   assists_data
                    })

                history.append({
                    "timestamp": ev_ts,
                    "kda": {pid: dict(running[pid]) for pid in range(1, 11)},
                })
        return history, kill_damage_log

    def _get_kda_at(self, kda_history: list, ts_ms: int) -> dict:
        result = {pid: {"kills": 0, "deaths": 0, "assists": 0} for pid in range(1, 11)}
        for entry in kda_history:
            if entry["timestamp"] <= ts_ms:
                result = entry["kda"]
            else:
                break
        return result

    def _get_kill_events_in_window(self, kill_damage_log: list, start_ms: int, end_ms: int) -> dict:
        """Devuelve las kills (con asistentes y daño) donde el participantId fue el killer, en el intervalo dado."""
        result = {pid: [] for pid in range(1, 11)}
        for entry in kill_damage_log:
            if start_ms < entry["timestamp"] <= end_ms:
                pid = entry["killer_id"]
                result[pid].append({
                    "victim_id":    entry["victim_id"],
                    "damage_dealt": entry["damage_dealt"],
                    "assistants":   entry["assistants"],
                    "timestamp_ms": entry["timestamp"],
                })
        return result

    def _build_objectives(self, snap_ms, frame_ts, td, tb, tt, ti, th, d_respawn, b_respawn, h_respawn, inhib_respawns) -> dict:
        def filt(lst, keys):
            return [{k: x[k] for k in keys} for x in lst if x['timestamp'] <= frame_ts]
            
        def get_inhib_timers(team_id):
            timers = {}
            for lane in ["TOP_LANE", "MID_LANE", "BOT_LANE"]:
                key = f"{team_id}_{lane}"
                if key in inhib_respawns and inhib_respawns[key] > snap_ms:
                    timers[lane] = round((inhib_respawns[key] - snap_ms) / 60000, 2)
            return timers
            
        # El dragón base aparece al min 5.0 (300000ms), Baron al 20.0 (1200000ms), Heraldo al 8.0 (480000)
        dragon_timer = 0
        if d_respawn > snap_ms: dragon_timer = round((d_respawn - snap_ms) / 60000, 2)
        elif snap_ms < 300000 and d_respawn == 0: dragon_timer = round((300000 - snap_ms) / 60000, 2)
        
        baron_timer = 0
        if b_respawn > snap_ms: baron_timer = round((b_respawn - snap_ms) / 60000, 2)
        elif snap_ms < 1200000 and b_respawn == 0: baron_timer = round((1200000 - snap_ms) / 60000, 2)
        
        herald_timer = 0
        if h_respawn > snap_ms: herald_timer = round((h_respawn - snap_ms) / 60000, 2)
        elif snap_ms < 480000 and h_respawn == 0: herald_timer = round((480000 - snap_ms) / 60000, 2)
            
        return {
            "global_respawns_remaining_min": {
                "dragon": dragon_timer,
                "baron_nashor": baron_timer,
                "rift_herald": herald_timer,
            },
            "blue_team": {
                "dragons":    filt(td[100], ["type", "minute"]),
                "barons":     filt(tb[100], ["minute"]),
                "towers":     filt(tt[100], ["lane", "tier", "minute"]),
                "inhibitors": filt(ti[100], ["lane", "minute"]),
                "heralds":    filt(th[100], ["minute"]),
                "dead_inhibitors_respawn_min": get_inhib_timers(100)
            },
            "red_team": {
                "dragons":    filt(td[200], ["type", "minute"]),
                "barons":     filt(tb[200], ["minute"]),
                "towers":     filt(tt[200], ["lane", "tier", "minute"]),
                "inhibitors": filt(ti[200], ["lane", "minute"]),
                "heralds":    filt(th[200], ["minute"]),
                "dead_inhibitors_respawn_min": get_inhib_timers(200)
            },
        }

    def _detect_teamfight(self, frame, recent_deaths, current_ts, tf_state) -> dict:
        DISTANCE    = 3000
        TF_TIMEOUT  = 20000  # 20 segundos
        
        pf = frame.get("participantFrames", {})
        players = []
        for pid_str, data in pf.items():
            pid = int(pid_str)
            pos = data.get("position", {"x": 0, "y": 0})
            health = data.get("championStats", {}).get("health", 0)
            is_dead = (health == 0)
            players.append({"id": pid - 1, "pos": pos, "is_dead": is_dead, "team": 100 if pid <= 5 else 200})

        # Última muerte global
        past_deaths = [d["timestamp"] for d in recent_deaths if d["timestamp"] <= current_ts]
        last_death_ts = max(past_deaths) if past_deaths else 0
        time_since_death = current_ts - last_death_ts if last_death_ts > 0 else 9999999

        alive_blue = [p for p in players if p["team"] == 100 and not p["is_dead"]]
        alive_red  = [p for p in players if p["team"] == 200 and not p["is_dead"]]

        survivors_near = False
        for b in alive_blue:
            for r in alive_red:
                dist = ((b["pos"]["x"] - r["pos"]["x"])**2 + (b["pos"]["y"] - r["pos"]["y"])**2)**0.5
                if dist <= DISTANCE:
                    survivors_near = True
                    break
            if survivors_near:
                break

        if not tf_state["active"]:
            has_recent_death = (time_since_death <= TF_TIMEOUT)
            clump_found = False
            for center_p in players:
                blue_count = 0
                red_count  = 0
                for p in players:
                    dist = ((center_p["pos"]["x"] - p["pos"]["x"])**2 + (center_p["pos"]["y"] - p["pos"]["y"])**2)**0.5
                    if dist <= DISTANCE:
                        if p["team"] == 100: blue_count += 1
                        else: red_count += 1
                if blue_count >= 4 and red_count >= 4:
                    clump_found = True
                    break
            
            if clump_found and has_recent_death:
                tf_state["active"] = True
        else:
            if time_since_death > TF_TIMEOUT and not survivors_near:
                tf_state["active"] = False

        in_tf = {i: False for i in range(10)}
        if tf_state["active"]:
            for p in players:
                if not p["is_dead"]:
                    enemies = alive_red if p["team"] == 100 else alive_blue
                    near_enemy = any(((p["pos"]["x"] - e["pos"]["x"])**2 + (p["pos"]["y"] - e["pos"]["y"])**2)**0.5 <= DISTANCE for e in enemies)
                    if near_enemy:
                        in_tf[p["id"]] = True
                else:
                    my_deaths = [d["timestamp"] for d in recent_deaths if d["victim_id"] == p["id"] and d["timestamp"] <= current_ts]
                    if my_deaths and (current_ts - max(my_deaths) <= TF_TIMEOUT):
                        in_tf[p["id"]] = True

        return in_tf

    def _calculate_death_timer(self, level, game_time_ms=0):
        """
        Calcula tiempo de respawn según nivel y tiempo de partida.
        Fórmula actualizada Patch 14.16+ (Summoner's Rift).
        Total Death Time = BRW + (BRW × TIFx)
        """
        import math
        BRW_TABLE = {
            1: 10, 2: 10, 3: 12, 4: 12, 5: 14, 6: 16,
            7: 20, 8: 25, 9: 28, 10: 32.5, 11: 35,
            12: 37.5, 13: 40, 14: 42.5, 15: 45,
            16: 47.5, 17: 50, 18: 52.5
        }
        brw = BRW_TABLE.get(level, 52.5)
        game_minutes = game_time_ms / 60000
        if game_minutes < 15:
            tifx = 0
        elif game_minutes < 30:
            tifx = math.ceil(2 * (game_minutes - 15)) * 0.425 / 100
        elif game_minutes < 45:
            tifx = (12.75 + math.ceil(2 * (game_minutes - 30)) * 0.30) / 100
        elif game_minutes < 55:
            tifx = (21.75 + math.ceil(2 * (game_minutes - 45)) * 1.45) / 100
        else:
            tifx = 0.50
        tifx = min(tifx, 0.50)
        return round(brw + (brw * tifx), 1)

    def _validate(self):
        if len(self.metadata_history) < 3:
            return
        log("\n   🔍 VALIDACIÓN (Jugador 1 y Jugador 6):")
        checkpoints = [
            self.metadata_history[0],
            self.metadata_history[len(self.metadata_history) // 2],
            self.metadata_history[-1],
        ]
        for snap in checkpoints:
            for idx in [0, 5]:  # Jugador 1 (blue) y jugador 6 (red)
                if idx < len(snap['participants']):
                    p  = snap['participants'][idx]
                    s  = p['stats']
                    log(f"      @ {snap['game_time_min']:.1f}min P{idx+1} ({p['name'][:12]}): "
                        f"items={s['items']} trinket={s['trinket']} "
                        f"KDA={s['kills']}/{s['deaths']}/{s['assists']} gold={s['gold']}")

    def _dump_debug(self):
        try:
            dump_path = os.path.join(self.recorder.temp_dir, "debug_snapshots.json")
            with open(dump_path, 'w', encoding='utf-8') as f:
                json.dump(self.metadata_history, f, ensure_ascii=False, indent=2)
            log(f"   💾 Debug volcado: {dump_path}", "DEBUG")
        except Exception as e:
            log(f"   ⚠️ Error volcando debug: {e}", "DEBUG")

    # ─────────────────────────────────────────
    # Guardado de archivos finales
    # ─────────────────────────────────────────

    def save_final_reports(self):
        if not self.metadata_history:
            log("⚠️ Sin snapshots para guardar", "WARN")
            return
        log("\n💾 Generando archivos finales...")

        # JSON completo
        json_path = os.path.join(self.recorder.temp_dir, "ai_metadata_history.json")
        with open(json_path, 'w', encoding='utf-8') as f:
            json.dump(self.metadata_history, f, indent=2, ensure_ascii=False)
        log(f"   ✅ JSON: {len(self.metadata_history)} snapshots")

        # CSV para entrenamiento
        csv_path = os.path.join(self.recorder.temp_dir, "ai_training_timeline.csv")
        try:
            with open(csv_path, 'w', newline='', encoding='utf-8') as f:
                w = csv.writer(f)
                w.writerow([
                    'ms', 'min', 'p_idx', 'team', 'champ', 'champ_name', 'role',
                    'gold', 'current_gold', 'gold_spent', 'level', 'cs', 'xp',
                    'health', 'health_max',
                    'x', 'y',
                    'kills', 'deaths', 'assists',
                    'recent_kills',
                    'is_dead', 'respawn_timer_remaining', 'in_teamfight',
                    'items', 'items_detailed', 'trinket', 'trinket_detailed',
                    'team_gold_total', 'gold_diff',
                    'team_dragons', 'team_barons', 'team_towers',
                    'team_inhibitors', 'team_heralds',
                    'enemy_dragons', 'enemy_barons', 'enemy_towers',
                    'enemy_inhibitors', 'enemy_heralds',
                    'team_dragon_types', 'enemy_dragon_types',
                    'team_tower_kills', 'enemy_tower_kills',
                    'team_inhibitor_kills', 'enemy_inhibitor_kills',
                    'winning_team'
                ])
                for snap in self.metadata_history:
                    ms   = snap['game_time_ms']
                    mins = snap['game_time_min']
                    obj  = snap.get('objectives', {})
                    blue = obj.get('blue_team', {})
                    red  = obj.get('red_team', {})
                    tg   = snap.get('team_gold', {})
                    for idx, p in enumerate(snap['participants']):
                        s     = p['stats']
                        tid   = p['team_id']
                        t_obj = blue if tid == 100 else red
                        e_obj = red  if tid == 100 else blue
                        items_str     = ','.join(str(i) for i in s.get('items', [])) or 'none'
                        items_det_str = ' | '.join(s.get('items_detailed', [])) or 'none'
                        t_drag_types  = ','.join(d['type'] for d in t_obj.get('dragons', [])) or 'none'
                        e_drag_types  = ','.join(d['type'] for d in e_obj.get('dragons', [])) or 'none'
                        
                        t_tower_types = ','.join(f"{d['tier']}_{d['lane']}" for d in t_obj.get('towers', [])) or 'none'
                        e_tower_types = ','.join(f"{d['tier']}_{d['lane']}" for d in e_obj.get('towers', [])) or 'none'
                        t_inhib_types = ','.join(d['lane'] for d in t_obj.get('inhibitors', [])) or 'none'
                        e_inhib_types = ','.join(d['lane'] for d in e_obj.get('inhibitors', [])) or 'none'
                        
                        # Team gold según equipo
                        my_team_gold  = tg.get('blue_total', 0) if tid == 100 else tg.get('red_total', 0)
                        my_gold_diff  = tg.get('gold_diff', 0) if tid == 100 else -tg.get('gold_diff', 0)
                        
                        # Serializar recent_kills como string JSON
                        recent_kills_str  = json.dumps(s.get('recent_kills', []), separators=(',', ':'))
                        w.writerow([
                            ms, mins, idx, tid, p['champion_id'], p.get('champion_name', ''), p.get('role', ''),
                            s.get('gold', 0), s.get('current_gold', 0), s.get('gold_spent', 0),
                            s.get('level', 0), s.get('minions_killed', 0), s.get('xp', 0),
                            s.get('health', 0), s.get('health_max', 0),
                            s['pos']['x'], s['pos']['y'],
                            s.get('kills', 0), s.get('deaths', 0), s.get('assists', 0),
                            recent_kills_str,
                            1 if s.get('is_dead') else 0,
                            s.get('respawn_timer_remaining', 0),
                            1 if s.get('in_teamfight') else 0,
                            items_str, items_det_str,
                            s.get('trinket', STEALTH_WARD_ID), s.get('trinket_detailed', ''),
                            my_team_gold, my_gold_diff,
                            len(t_obj.get('dragons', [])),    len(t_obj.get('barons', [])),
                            len(t_obj.get('towers', [])),     len(t_obj.get('inhibitors', [])),
                            len(t_obj.get('heralds', [])),
                            len(e_obj.get('dragons', [])),    len(e_obj.get('barons', [])),
                            len(e_obj.get('towers', [])),     len(e_obj.get('inhibitors', [])),
                            len(e_obj.get('heralds', [])),
                            t_drag_types, e_drag_types,
                            t_tower_types, e_tower_types,
                            t_inhib_types, e_inhib_types,
                            snap.get('winning_team', 0)
                        ])
            duration = self.metadata_history[-1]['game_time_min']
            log(f"   ✅ CSV: {len(self.metadata_history)*10} rows | "
                f"{len(self.metadata_history)} snapshots | {duration:.1f}min")
        except Exception as e:
            log(f"❌ Error generando CSV: {e}", "ERROR")
            import traceback
            traceback.print_exc()


# ══════════════════════════════════════════════════════════════
# SPECTATOR RECORDER
# ══════════════════════════════════════════════════════════════

class SpectatorRecorder:
    def __init__(self, game_id, encryption_key, platform_id=PLATFORM_ID,
                 player_name="Unknown", extra_metadata=None, metadata_only=False):
        self.game_id        = game_id
        self.encryption_key = encryption_key
        self.platform_id    = platform_id
        self.player_name    = player_name
        self.extra_metadata = extra_metadata or {}
        self.metadata_only  = metadata_only

        self.spectator_url = (
            f"http://{SPECTATOR_SERVERS.get(platform_id, 'spectator.kr.lol.pvp.net:8080')}"
        )

        self.chunks    = {}
        self.keyframes = {}
        self.game_metadata = None

        self.recording          = False
        self.start_time         = None
        self.end_time           = None
        self.consecutive_errors = 0
        self.total_chunks       = 0
        self.total_keyframes    = 0
        self.game_length_ms     = 0

        self.start_game_chunk_id  = 1
        self.end_startup_chunk_id = 0
        self.keyframe_interval    = 60000

        self.temp_dir = os.path.join(SAVE_PATH, f"temp_{game_id}")
        os.makedirs(self.temp_dir, exist_ok=True)

        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "Mozilla/5.0 RiotClient/12.0.0"})

    def _spectator_request(self, method, params="", timeout=15):
        global LAST_SPECTATOR_REQUEST
        for attempt in range(4):
            with GLOBAL_SPECTATOR_LOCK:
                elapsed = time.time() - LAST_SPECTATOR_REQUEST
                if elapsed < MIN_SPECTATOR_INTERVAL:
                    time.sleep(MIN_SPECTATOR_INTERVAL - elapsed)
                LAST_SPECTATOR_REQUEST = time.time()
            url = (f"{self.spectator_url}/observer-mode/rest/consumer/{method}"
                   f"/{self.platform_id}/{self.game_id}/{params}token")
            try:
                r = self.session.get(url, timeout=timeout)
                if r.status_code == 429:
                    wait = 5 * (attempt + 1)
                    log(f"⚠️ 429 RateLimit. Esperando {wait}s...", "WARN")
                    time.sleep(wait)
                    continue
                return r
            except Exception:
                if attempt == 0:
                    alt = SPECTATOR_SERVERS_ALT.get(self.platform_id)
                    if alt:
                        self.spectator_url = f"http://{alt}"
                        continue
                time.sleep(2)
        return None

    def start_recording(self):
        log(f"\n🔴 INICIANDO GRABACIÓN: {self.game_id} ({self.player_name})")
        self.recording  = True
        self.start_time = time.time()

        # ── Metadata inicial: PRIMERO pedimos esto, LUEGO arrancamos el thread ──
        # Es crítico tener game_metadata antes de que el collector empiece,
        # porque _get_participants() lo necesita para obtener los 10 jugadores.
        for attempt in range(5):
            r_meta = self._spectator_request("getGameMetaData", "1/")
            if r_meta and r_meta.status_code == 200:
                self.game_metadata        = r_meta.json()
                self.start_game_chunk_id  = self.game_metadata.get('startGameChunkId', 1)
                self.end_startup_chunk_id = self.game_metadata.get('endStartupChunkId', 0)
                self.keyframe_interval    = self.game_metadata.get('interestScore', 60000)
                with open(os.path.join(self.temp_dir, "metadata.json"), 'w') as f:
                    json.dump(self.game_metadata, f, indent=2)
                n = len(self.game_metadata.get('participants', []))
                log(f"   📋 Metadata inicial: {n} participants")
                break
            else:
                log(f"   ⏳ Esperando metadata (intento {attempt+1}/5)...")
                time.sleep(5)

        # ── Lanzar thread de snapshots DESPUÉS de tener metadata ──
        collector = MetadataCollector(self, interval=20)
        collector.start()

        last_chunk_reported = 0
        try:
            while self.recording:
                r_info = self._spectator_request("getLastChunkInfo", "0/")
                if not r_info or r_info.status_code != 200:
                    self.consecutive_errors += 1
                    if self.consecutive_errors > MAX_CONSECUTIVE_ERRORS:
                        log("❌ Demasiados errores, abortando", "ERROR")
                        break
                    time.sleep(10)
                    continue

                self.consecutive_errors = 0
                info     = r_info.json()
                chunk_id = info.get('chunkId', 0)
                kf_id    = info.get('keyFrameId', 0)
                end_id   = info.get('endGameChunkId', 0)

                if not self.metadata_only:
                    # Chunks
                    for cid in range(max(1, chunk_id - 5), chunk_id + 1):
                        if cid not in self.chunks:
                            r_c = self._spectator_request("getGameDataChunk", f"{cid}/")
                            if r_c and r_c.status_code == 200:
                                self.chunks[cid] = r_c.content
                                with open(os.path.join(self.temp_dir, f"chunk_{cid}.bin"), 'wb') as fh:
                                    fh.write(r_c.content)
                                self.total_chunks += 1
                    # Keyframes
                    for kid in range(max(1, kf_id - 1), kf_id + 1):
                        if kid not in self.keyframes:
                            r_k = self._spectator_request("getKeyFrame", f"{kid}/")
                            if r_k and r_k.status_code == 200:
                                self.keyframes[kid] = r_k.content
                                with open(os.path.join(self.temp_dir, f"kf_{kid}.bin"), 'wb') as fh:
                                    fh.write(r_k.content)
                                self.total_keyframes += 1

                if chunk_id > last_chunk_reported:
                    elapsed_min = (time.time() - self.start_time) / 60
                    log(f"   📡 Chunk {chunk_id} | KF {kf_id} | {elapsed_min:.1f}min | "
                        f"Snaps: {len(collector.metadata_history)}")
                    last_chunk_reported = chunk_id

                if end_id > 0 and chunk_id >= end_id:
                    log(f"🏁 Partida terminada (Chunk {end_id})")
                    break

                # Sleep del chunk polling — NO afecta al thread de snapshots
                sleep_s = min(info.get('nextAvailableChunk', 10000) / 1000, 20)
                time.sleep(sleep_s)

        finally:
            collector.stop()

        self.end_time       = time.time()
        self.recording      = False
        self.game_length_ms = int((self.end_time - self.start_time) * 1000)

        log(f"\n{'='*65}")
        log(f"📊 FASE DE ENRIQUECIMIENTO v3.1")
        log(f"{'='*65}")
        collector.enrich_with_timeline()
        collector.save_final_reports()

        log("✅ Grabación completada y archivos generados (No ROFL)")
        return self.temp_dir


# ══════════════════════════════════════════════════════════════
# MODO MONITOR
# ══════════════════════════════════════════════════════════════

def record_game_thread(game_id, enc_key, name, full_info, metadata_only):
    global LAST_GAME_END_TIME
    recorder = SpectatorRecorder(game_id, enc_key, player_name=name,
                                 extra_metadata=full_info, metadata_only=metadata_only)
    path = recorder.start_recording()
    if path or metadata_only:
        completed_recordings.add(str(game_id))
        with open(STATE_FILE, 'w') as f:
            json.dump({"completed": list(completed_recordings)}, f)
    if str(game_id) in active_recordings:
        del active_recordings[str(game_id)]
    LAST_GAME_END_TIME = time.time()
    log(f"❄️ Enfriamiento activado tras Game {game_id}")


def monitor_mode(metadata_only=False):
    log("🚀 MODO MONITOR KR — v3.1")
    global challenger_players, LAST_GAME_END_TIME
    challenger_players = get_challenger_and_grandmaster_list()
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, 'r') as f:
                completed_recordings.update(json.load(f).get('completed', []))
        except Exception:
            pass

    while True:
        since_last = time.time() - LAST_GAME_END_TIME
        if since_last < 120:
            log(f"❄️ Enfriando... {int(120-since_last)}s", "DEBUG")
            time.sleep(20)
            continue

        for p in challenger_players:
            if len(active_recordings) >= 1:
                break
            puuid = p.get('puuid')
            if not puuid:
                continue
            game = check_active_game(puuid)
            if not game:
                time.sleep(0.5)
                continue
            game_id = str(game['gameId'])
            if game_id in completed_recordings or game_id in active_recordings:
                continue
            if game.get('gameQueueConfigId') not in (420, 440):
                continue
            elapsed_sec = (time.time() * 1000 - game.get('gameStartTime', 0)) / 1000
            if elapsed_sec >= 300:
                continue

            real_name = get_summoner_name_from_puuid(puuid)
            log(f"\n🆕 NUEVA PARTIDA: {real_name} | Game {game_id}")
            active_recordings[game_id] = True
            t = threading.Thread(
                target=record_game_thread,
                args=(game_id, game['observers']['encryptionKey'],
                      real_name, game, metadata_only),
                daemon=True,
            )
            t.start()
            log("🔒 Grabando (máx 1 partida simultánea)")
            time.sleep(5)
            break

        log(f"😴 Activas: {len(active_recordings)}. Esperando {CHECK_INTERVAL}s...", "DEBUG")
        time.sleep(CHECK_INTERVAL)


# ══════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════

def main():
    ap = argparse.ArgumentParser(description="LoL Replay Recorder v3.1")
    ap.add_argument('--puuid',          type=str,           help='PUUID para partida específica')
    ap.add_argument('--metadata-only',  action='store_true', help='Solo datos IA, sin .rofl')
    ap.add_argument('--verbose',        action='store_true', help='Log DEBUG activado')
    ap.add_argument('--dump-snapshots', action='store_true', help='Volcar snapshots a JSON para debug')
    args = ap.parse_args()

    global VERBOSE, DUMP_SNAPSHOTS
    VERBOSE        = args.verbose
    DUMP_SNAPSHOTS = args.dump_snapshots

    print("\n" + "═" * 65)
    print("   GRABADOR DE REPLAYS v3.1")
    print("   ─────────────────────────────────────────────")
    print(f"   Modo:      {'METADATA ONLY' if args.metadata_only else 'REPLAY + METADATA'}")
    print(f"   Snapshots: cada 20s exactos (thread independiente)")
    print(f"   Items:     10 jugadores, pasada lineal PURCHASED/SOLD/UNDO")
    print(f"   Región:    {PLATFORM_ID}")
    print("═" * 65 + "\n")

    if args.puuid:
        info = check_active_game(args.puuid)
        if info:
            name = get_summoner_name_from_puuid(args.puuid)
            record_game_thread(info['gameId'], info['observers']['encryptionKey'],
                               name, info, args.metadata_only)
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