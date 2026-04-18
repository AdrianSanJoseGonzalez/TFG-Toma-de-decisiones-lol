import os
import time
import json
import requests
import urllib3
import glob
import re
import ctypes
from pathlib import Path
from base64 import b64encode

# Desactivar warnings de certificados SSL inseguros para la API de League
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# -------------- HACK DE CAMARA ROTATIVA (VUESTRO CTYPES) --------------
PUL = ctypes.POINTER(ctypes.c_ulong)
class KeyBdInput(ctypes.Structure):
    _fields_ = [("wVk", ctypes.c_ushort),
                ("wScan", ctypes.c_ushort),
                ("dwFlags", ctypes.c_ulong),
                ("time", ctypes.c_ulong),
                ("dwExtraInfo", PUL)]

class HardwareInput(ctypes.Structure):
    _fields_ = [("uMsg", ctypes.c_ulong),
                ("wParamL", ctypes.c_short),
                ("wParamH", ctypes.c_short)]

class MouseInput(ctypes.Structure):
    _fields_ = [("dx", ctypes.c_long),
                ("dy", ctypes.c_long),
                ("mouseData", ctypes.c_ulong),
                ("dwFlags", ctypes.c_ulong),
                ("time", ctypes.c_ulong),
                ("dwExtraInfo", PUL)]

class Input_I(ctypes.Union):
    _fields_ = [("ki", KeyBdInput),
                ("mi", MouseInput),
                ("hi", HardwareInput)]

class Input(ctypes.Structure):
    _fields_ = [("type", ctypes.c_ulong),
                ("ii", Input_I)]

# Virtual Key & Scan Codes (DirectInput)
VK_KEYS = {
    '1': 0x31, '2': 0x32, '3': 0x33, '4': 0x34, '5': 0x35,
    'q': 0x51, 'w': 0x57, 'e': 0x45, 'r': 0x52, 't': 0x54
}
# Códigos de hardware para asegurar que el motor del juego sienta la pulsación
SCAN_CODES = {
    '1': 0x02, '2': 0x03, '3': 0x04, '4': 0x05, '5': 0x06,
    'q': 0x10, 'w': 0x11, 'e': 0x12, 'r': 0x13, 't': 0x14
}
INDEX_TO_KEY = ['1', '2', '3', '4', '5', 'q', 'w', 'e', 'r', 't']

def press_key(vk_code, scan_code=0):
    extra = ctypes.c_ulong(0)
    ii_ = Input_I()
    # 0x0008 = KEYEVENTF_SCANCODE
    flags = 0x0008 if scan_code else 0
    ii_.ki = KeyBdInput(vk_code, scan_code, flags, 0, ctypes.pointer(extra))
    x = Input(ctypes.c_ulong(1), ii_)
    ctypes.windll.user32.SendInput(1, ctypes.pointer(x), ctypes.sizeof(x))

def release_key(vk_code, scan_code=0):
    extra = ctypes.c_ulong(0)
    ii_ = Input_I()
    # 0x0002 = KEYEVENTF_KEYUP, 0x0008 = KEYEVENTF_SCANCODE
    flags = 0x0002 | (0x0008 if scan_code else 0)
    ii_.ki = KeyBdInput(vk_code, scan_code, flags, 0, ctypes.pointer(extra))
    x = Input(ctypes.c_ulong(1), ii_)
    ctypes.windll.user32.SendInput(1, ctypes.pointer(x), ctypes.sizeof(x))

def tap_key(key_char, double=True):
    vk = VK_KEYS.get(key_char)
    scan = SCAN_CODES.get(key_char, 0)
    if vk:
        # Primera pulsación
        press_key(vk, scan)
        time.sleep(0.05)
        release_key(vk, scan)
        if double:
            time.sleep(0.05)
            # Segunda pulsación (Lock de cámara)
            press_key(vk, scan)
            time.sleep(0.05)
            release_key(vk, scan)

def scroll_mouse(clicks):
    """Simula la rueda del ratón. clicks>0 = zoom in, clicks<0 = zoom out."""
    MOUSEEVENTF_WHEEL = 0x0800
    WHEEL_DELTA = 120
    for _ in range(abs(clicks)):
        extra = ctypes.c_ulong(0)
        ii_ = Input_I()
        direction = WHEEL_DELTA if clicks > 0 else -WHEEL_DELTA
        ii_.mi = MouseInput(0, 0, ctypes.c_ulong(direction & 0xFFFFFFFF), MOUSEEVENTF_WHEEL, 0, ctypes.pointer(extra))
        x = Input(ctypes.c_ulong(0), ii_)  # type 0 = INPUT_MOUSE
        ctypes.windll.user32.SendInput(1, ctypes.pointer(x), ctypes.sizeof(x))
        time.sleep(0.03)

# CAMERA_Y_FIXED: altura fija que se fuerza por API para eliminar la variabilidad del zoom.
# El mapa de LoL tiene ~180 unidades de altura de terreno como máximo.
# Con Y=200 la cámara queda prácticamente encima del personaje, minimizando el offset X/Z.
CAMERA_Y_FIXED = 200.0

def focus_league_window():
    user32 = ctypes.windll.user32
    def callback(hwnd, extra):
        length = user32.GetWindowTextLengthW(hwnd)
        if length > 0:
            buff = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buff, length + 1)
            if "League of Legends" in buff.value and ("Client" in buff.value or "(TM)" in buff.value):
                user32.ShowWindow(hwnd, 9)
                user32.SetForegroundWindow(hwnd)
                return False
        return True
    EnumWindows = ctypes.windll.user32.EnumWindows
    EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_int, ctypes.c_int)
    proc = EnumWindowsProc(callback)
    EnumWindows(proc, 0)
    return True
# -------------------------------------------------------------

def get_routing_value(region: str) -> str:
    region = region.upper()
    if region in ["EUW1", "EUN1", "TR1", "RU", "ME1"]: return "europe"
    if region in ["NA1", "BR1", "LA1", "LA2"]: return "americas"
    if region in ["KR", "JP1"]: return "asia"
    if region in ["OC1", "PH2", "SG2", "TH2", "TW2", "VN2"]: return "sea"
    return "europe"


class ReplayExtractor:
    def __init__(self, league_path: str = r"I:\Riot Games\League of Legends"):
        self.league_path = Path(league_path)
        self.lockfile_path = self.league_path / "lockfile"
        self.replay_api_url = "https://127.0.0.1:2999"
        
        self.lcu_port = None
        self.lcu_token = None
        self.lcu_headers = None
        self.riot_api_key = os.getenv('RIOT_API_KEY', 'RGAPI-02d50cba-5f08-44a6-9233-a119face5ced')
        self._load_lcu_credentials()
        
        # Carga dinámica de la última versión de Data Dragon para asegurar precios actualizados
        try:
            versions = requests.get("https://ddragon.leagueoflegends.com/api/versions.json", timeout=10).json()
            latest_version = versions[0]
            print(f"[INFO] Cargando items de la versión {latest_version}...")
            res_dd = requests.get(f"https://ddragon.leagueoflegends.com/cdn/{latest_version}/data/en_US/item.json", timeout=10)
            self.dd_items = res_dd.json().get("data", {})
        except Exception as e:
            print(f"[ERROR] Error al cargar Data Dragon: {e}. Usando fallback de API.")
            self.dd_items = {}

    def _load_lcu_credentials(self):
        if not self.lockfile_path.exists():
            print(f"[WARN] lockfile no encontrado. Aseg\u00farate de que el cliente de LoL est\u00e1 abierto.")
            return
        with open(self.lockfile_path, 'r') as f:
            data = f.read().split(':')
            self.lcu_port = data[2]
            self.lcu_token = data[3]
        auth_string = f"riot:{self.lcu_token}"
        auth_b64 = b64encode(auth_string.encode('ascii')).decode('ascii')
        self.lcu_headers = {"Authorization": f"Basic {auth_b64}", "Accept": "application/json"}
        print(f"[INFO] LCU conectado en puerto {self.lcu_port}")

    def get_match_timeline(self, rofl_path: str) -> list:
        game_id_str = Path(rofl_path).stem
        match = re.search(r'([A-Za-z0-9]+)[_-](\d{8,})', game_id_str)
        if not match: return []
        region = match.group(1).upper()
        game_id = match.group(2)
        match_id = f"{region}_{game_id}"
        routing = get_routing_value(region)
        url = f"https://{routing}.api.riotgames.com/lol/match/v5/matches/{match_id}/timeline"
        try:
            res = requests.get(url, headers={"X-Riot-Token": self.riot_api_key}, timeout=10)
            if res.status_code == 200:
                frames = res.json().get('info', {}).get('frames', [])
                epic_monsters = []
                for f in frames:
                    for ev in f.get('events', []):
                        if ev.get('type') == 'ELITE_MONSTER_KILL':
                            epic_monsters.append({
                                "EventName": "DragonKill" if ev.get('monsterType') == 'DRAGON' else "HeraldKill" if ev.get('monsterType') == 'RIFTHERALD' else "BaronKill" if ev.get('monsterType') == 'BARON_NASHOR' else "EpicMonsterKill",
                                "EventTime": ev.get('timestamp', 0) / 1000.0,
                                "KillerId": ev.get('killerId', 0),
                                "KillerTeamId": ev.get('killerTeamId', 0)
                            })
                return epic_monsters
        except: pass
        return []

    def watch_replay(self, rofl_path: str):
        game_id_str = Path(rofl_path).stem
        match = re.search(r'\d{8,}', game_id_str)
        if not match: return False
        game_id = match.group()
        url = f"https://127.0.0.1:{self.lcu_port}/lol-replays/v1/rofls/{game_id}/watch"
        try:
            res = requests.post(url, headers=self.lcu_headers, json={}, verify=False)
            return res.status_code in (200, 204)
        except: return False

    def obtener_equipo_ganador(self, match_id_full: str):
        """Retorna cuál equipo ha ganado la partida (100 para Azul, 200 para Rojo)."""
        url_match = f"https://{get_routing_value(match_id_full.split('_')[0])}.api.riotgames.com/lol/match/v5/matches/{match_id_full}"
        try:
            res = requests.get(url_match, headers={"X-Riot-Token": self.riot_api_key}, timeout=10)
            if res.status_code != 200:
                return None
                
            match_data = res.json()
            teams = match_data.get("info", {}).get("teams", [])
            for t in teams:
                if t.get("win"):
                    return t["teamId"]
            return None
        except Exception:
            return None

    def obtener_role_bound_items(self, match_id_full: str):
        """Retorna el item ligado al rol (botas de la S15) para cada campeón."""
        url_match = f"https://{get_routing_value(match_id_full.split('_')[0])}.api.riotgames.com/lol/match/v5/matches/{match_id_full}"
        try:
            print(f"[DEBUG] Solicitando RoleBoundItems a Riot: {url_match}")
            res = requests.get(url_match, headers={"X-Riot-Token": self.riot_api_key}, timeout=10)
            print(f"    [DEBUG] Status Code: {res.status_code}")
            
            if res.status_code != 200:
                print(f"    [DEBUG] Error en API: {res.text}")
                return {}
                
            match_data = res.json()
            botas_dict = {}
            for p in match_data.get("info", {}).get("participants", []):
                champ = p.get("championName", "").lower()
                bota = p.get("roleBoundItem")
                if bota:
                    botas_dict[champ] = bota
            
            print(f"    [DEBUG] Diccionario de botas creado: {botas_dict}")
            return botas_dict
        except Exception as e:
            print(f"    [DEBUG] Excepción en obtener_role_bound_items: {e}")
            return {}

    def obtener_tiempos_mision_botas(self, match_id_full: str):
        """Encuentra exactamente en qué segundo el ADC completó la misión evolutiva de S15.
           Lo detecta cuando se destruye el item 1001 (Tier 1) y/o el item base (ej: 1202)."""
        routing = get_routing_value(match_id_full.split('_')[0])
        url_timeline = f"https://{routing}.api.riotgames.com/lol/match/v5/matches/{match_id_full}/timeline"
        url_match = f"https://{routing}.api.riotgames.com/lol/match/v5/matches/{match_id_full}"
        try:
            headers = {"X-Riot-Token": self.riot_api_key}
            res_m = requests.get(url_match, headers=headers, timeout=10)
            if res_m.status_code != 200: return {}
            p_map = {}
            for p in res_m.json().get("info", {}).get("participants", []):
                p_map[p["participantId"]] = p.get("championName", "").lower()

            res_t = requests.get(url_timeline, headers=headers, timeout=10)
            if res_t.status_code != 200: return {}
            
            tiempos_mision = {}
            for frame in res_t.json().get("info", {}).get("frames", []):
                for e in frame.get("events", []):
                    # El roleBoundItem 'consume' las botas T1 (1001) para darnos las mejoradas
                    if e.get("type") == "ITEM_DESTROYED" and e.get("itemId") in [1001, 1202, 1203]:
                        pid = e.get("participantId")
                        champ = p_map.get(pid, "")
                        if champ:
                            tiempos_mision[champ] = e.get("timestamp", 0) / 1000.0
            return tiempos_mision
        except Exception:
            return {}

    def get_champion_position(self, key_char: str, bounce_key: str) -> dict:
        """
        Salta al campeón objetivo via teclado, luego fuerza la Y de la cámara
        a un valor fijo via POST /replay/render para anular la variabilidad del zoom.
        """
        # 1. Rebote: desancla la cámara del campeón anterior
        tap_key(bounce_key, double=True)
        time.sleep(0.2)

        # 2. Saltar al campeón objetivo
        tap_key(key_char, double=True)
        time.sleep(0.3)

        # 3. Leer posición actual (Punto inicial)
        try:
            res = requests.get(f"{self.replay_api_url}/replay/render", verify=False, timeout=2)
            if res.status_code != 200:
                return {"error": f"GET render falló: {res.status_code}"}
            cam = res.json().get("cameraPosition", {})
            x = cam.get("x", 0.0)
            z = cam.get("z", 0.0)
        except Exception as e:
            return {"error": str(e)}

        # 4. FORZAR Y FIJA vía POST: Esto es lo que alinea las coordenadas con el suelo 2D.
        try:
            requests.post(
                f"{self.replay_api_url}/replay/render",
                json={"cameraPosition": {"x": x, "y": CAMERA_Y_FIXED, "z": z}},
                verify=False,
                timeout=2
            )
            time.sleep(0.15) 
        except Exception:
            pass

        # 5. Lectura final con altura normalizada
        try:
            res2 = requests.get(f"{self.replay_api_url}/replay/render", verify=False, timeout=2)
            if res2.status_code == 200:
                cam2 = res2.json().get("cameraPosition", {})
                return {
                    "x": cam2.get("x", x),
                    "z": cam2.get("z", z)
                }
        except Exception:
            pass

        return {"x": x, "z": z}

    def wait_for_game_launch(self, timeout_secs=120):
        start_time = time.time()
        print("[INFO] Esperando respuesta de la Replay API (2999)...")
        while time.time() - start_time < timeout_secs:
            try:
                res = requests.get(f"{self.replay_api_url}/liveclientdata/allgamedata", verify=False, timeout=2)
                if res.status_code == 200: return True
            except: pass
            time.sleep(3)
        return False

    def apply_boots_fix(self, data_snapshots):
        # Mapeo de componentes "delatores" a Botas de Tier 2
        COMPONENT_TO_BOOT = {
            1042: {"itemID": 3006, "displayName": "Grebas de berserker"},
            1029: {"itemID": 3047, "displayName": "Botas blindadas"},
            1033: {"itemID": 3111, "displayName": "Botas de Mercurio"},
            3145: {"itemID": 3158, "displayName": "Botas jonias de la lucidez"}
        }
        BOOTS_TIER_1 = 1001
        TIER2_IDS = [3006, 3047, 3111, 3158, 3020, 3009, 3110, 3142]

        player_states = {}

        for snapshot in data_snapshots:
            for p in snapshot.get("all_players", []):
                name = p.get("summonerName", "Unknown")
                if name not in player_states:
                    player_states[name] = {"hidden_boot": None, "last_items_ids": []}
                
                state = player_states[name]
                current_items = p.get("items", [])
                current_item_ids = [item.get("itemID") for item in current_items]

                has_tier2_visible = any(b_id in current_item_ids for b_id in TIER2_IDS)
                has_tier1_visible = BOOTS_TIER_1 in current_item_ids

                # 1. Detectar activación de quest (botas tier 1 desaparecen)
                if not has_tier1_visible and not has_tier2_visible:
                    if BOOTS_TIER_1 in state["last_items_ids"] and state["hidden_boot"] is None:
                        state["hidden_boot"] = {"itemID": 1001, "displayName": "Botas de velocidad (Ocultas)"}

                # 2. Detectar Upgrade de Tier 1 a Tier 2 (desaparecen componentes delatores)
                if state["hidden_boot"] is not None and state["hidden_boot"]["itemID"] == 1001:
                    for comp_id in COMPONENT_TO_BOOT:
                        if comp_id in state["last_items_ids"] and comp_id not in current_item_ids:
                            state["hidden_boot"] = COMPONENT_TO_BOOT[comp_id]
                            break

                # 3. Inyectar bota oculta reconstruida en el inventario actual
                if state["hidden_boot"] is not None and not has_tier2_visible:
                    p["items"].append({
                        "itemID": state["hidden_boot"]["itemID"],
                        "displayName": state["hidden_boot"]["displayName"],
                        "slot": 7,  # Slot 7 (ya que 0-5 son items y 6 es el trinket)
                        "virtual": True
                    })

                state["last_items_ids"] = current_item_ids

        return data_snapshots

    def _save_json(self, output_file: str, data: list):
        if not data: return
        try:
            # Aplicamos heuristicas post-extracción antes de volcar al disco
            data = self.apply_boots_fix(data)
            
            with open(output_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            # print(f"    [Guardado] {len(data)} snapshots en disco.")
        except Exception as e:
            print(f"[ERROR] No se pudo guardar JSON: {e}")

    def extract_game_data(self, output_file: str, sample_interval: float = 10.0, timeline_events: list = None, equipo_ganador=None, botas_dict=None, botas_tiempos=None):
        if timeline_events is None: timeline_events = []
        if botas_tiempos is None: botas_tiempos = {}
        data_snapshots = []
        is_game_ended = False
        current_playback_time = 0.0

        print(f"\n[!] Iniciando extracción SEGURA con saltos de {sample_interval}s in-game.")
        print("[TIP] Puedes pulsar Ctrl+C en cualquier momento; se guardará lo que lleves capturado.")

        try:
            while not is_game_ended:
                try:
                    # 1. SALTO TEMPORAL (PLAYBACK)
                    print(f"--- Snapshot T={current_playback_time:.1f}s ---")
                    
                    # Pedimos salto y damos Play un momento para que el motor cargue la escena
                    requests.post(f"{self.replay_api_url}/replay/playback", 
                                 json={"time": current_playback_time, "paused": False}, 
                                 verify=False, timeout=3)
                    time.sleep(1.0) # Tiempo de carga/buffer (Acelerado)
                    
                    # Pausamos para capturar a los 10 jugadores con calma
                    requests.post(f"{self.replay_api_url}/replay/playback", 
                                 json={"paused": True}, 
                                 verify=False, timeout=3)
                    time.sleep(0.4)

                    # 2. Datos generales
                    focus_league_window()
                    res_all = requests.get(f"{self.replay_api_url}/liveclientdata/allgamedata", verify=False, timeout=3)
                    if res_all.status_code != 200:
                        time.sleep(1)
                        continue
                    game_data = res_all.json()
                    all_players = game_data.get("allPlayers", [])

                    # 3. CICLO DE CÁMARA (ROTACIÓN)
                    BLUE_KEYS = ['1', '2', '3', '4', '5']
                    RED_KEYS  = ['q', 'w', 'e', 'r', 't']
                    blue_idx, red_idx = 0, 0
                    
                    for p in all_players:
                        team = p.get("team", "")
                        champ = p.get("championName", "?")
                        if team == "ORDER" and blue_idx < len(BLUE_KEYS):
                            key_char = BLUE_KEYS[blue_idx]
                            bounce_key = 'r' if blue_idx == 0 else 'q'
                            blue_idx += 1
                        elif team == "CHAOS" and red_idx < len(RED_KEYS):
                            key_char = RED_KEYS[red_idx]
                            bounce_key = '4' if red_idx == 0 else '1'
                            red_idx += 1
                        else:
                            print(f"    [WARN] Jugador {champ} equipo '{team}' no mapeado")
                            p["position_exact"] = {"error": f"team '{team}' desconocido"}
                            continue
                        
                        print(f"    [{team}] {champ} -> tecla '{key_char}'")
                        
                        # Capturar posición con técnica de altura fija (Calibración Paradox)
                        p["position_exact"] = self.get_champion_position(key_char, bounce_key)


                    # 4. Check Final y Snapshot
                    events_list = game_data.get("events", {}).get("Events", [])
                    for ev in events_list:
                        if ev.get("EventName") == "GameEnd":
                            is_game_ended = True
                            break
                    
                    if botas_dict is None:
                        botas_dict = {}
                        
                    # 5. Inyectar botas faltantes (RoleBoundItem) y calcular Oro Dinámico
                    botas_ids = {1001, 3006, 3047, 3111, 3158, 3020, 3117, 3009, 3115}
                    oro_equipo_azul = 0
                    oro_equipo_rojo = 0
                    oro_por_persona = {}
                    
                    for p in all_players:
                        # ===== PARTE A: INYECTAR BOTAS =====
                        items = p.get("items", [])
                        current_item_ids = [item.get("itemID") for item in items]
                        # Comprobar si tiene alguna bota de la lista tradicional
                        tiene_botas = any(b_id in current_item_ids for b_id in botas_ids)
                        
                        # Solo inyectamos para los ADC (BOTTOM) si han completado la misión
                        if not tiene_botas and p.get("position") == "BOTTOM":
                            champ_name = p.get("championName", "").lower()
                            raw_name = p.get("rawChampionName", "").split("_")[-1].lower()
                            
                            bota_real_id = botas_dict.get(champ_name) or botas_dict.get(raw_name)
                            tiempo_completado = botas_tiempos.get(champ_name) or botas_tiempos.get(raw_name, 0)
                            
                            # Solo inyectar si el tiempo de replay actual >= al momento en que terminaron la quest
                            if bota_real_id and (current_playback_time >= tiempo_completado or tiempo_completado == 0):
                                dd_info = self.dd_items.get(str(bota_real_id), {})
                                bota_falsa = {
                                    "canUse": False,
                                    "consumable": False,
                                    "count": 1,
                                    "displayName": dd_info.get("name", "RoleBound Boots"),
                                    "itemID": bota_real_id,
                                    "price": dd_info.get("gold", {}).get("total", 0),
                                    "rawDescription": f"game_item_description_{bota_real_id}",
                                    "rawDisplayName": f"game_item_displayname_{bota_real_id}",
                                    "slot": 7
                                }
                                items.append(bota_falsa)
                                p["items"] = items
                                print(f"    [INFO] Bota {bota_real_id} inyectada para {champ_name}")
                                
                        # ===== PARTE B: CALCULAR ORO DINÁMICO (Sincronizando Precios con Data Dragon) =====
                        oro_jugador = 0
                        for item in p.get("items", []):
                            i_id = str(item.get("itemID", 0))
                            if i_id in self.dd_items:
                                # Sobrescribimos el precio en el objeto para que el JSON sea correcto
                                real_total = self.dd_items[i_id].get("gold", {}).get("total", 0)
                                item["price"] = real_total
                                oro_jugador += real_total
                            else:
                                oro_jugador += item.get("price", 0)
                        
                        if "scores" not in p:
                            p["scores"] = {}
                        p["scores"]["gold"] = oro_jugador
                        
                        team = p.get("team", "")
                        if team == "ORDER":
                            oro_equipo_azul += oro_jugador
                        elif team == "CHAOS":
                            oro_equipo_rojo += oro_jugador

                    # Cálculo de ventaja a nivel de Snapshot (padre)
                    diff_oro = abs(oro_equipo_azul - oro_equipo_rojo)
                    if oro_equipo_azul > oro_equipo_rojo:
                        equipo_ventaja = "ORDER (Azul)"
                    elif oro_equipo_rojo > oro_equipo_azul:
                        equipo_ventaja = "CHAOS (Rojo)"
                    else:
                        equipo_ventaja = "EMPATE"

                    data_snapshots.append({
                        "game_time": current_playback_time,
                        "diferencia_oro": diff_oro,
                        "equipo_ventaja": equipo_ventaja,
                        "oro_equipo_azul": oro_equipo_azul,
                        "oro_equipo_rojo": oro_equipo_rojo,
                        "all_players": all_players,
                        "events": events_list + [e for e in timeline_events if e.get("EventTime", 0) <= current_playback_time],
                        "game_data": game_data.get("gameData"),
                        "equipo_ganador": equipo_ganador
                    })
                    
                    # GUARDADO PROGRESIVO cada 10 snapshots
                    if len(data_snapshots) % 10 == 0:
                        self._save_json(output_file, data_snapshots)
                    
                    if is_game_ended: break
                    current_playback_time += sample_interval

                except Exception as e:
                    print(f"[WARN] Error en bucle: {e}")
                    time.sleep(1)
                    
        except KeyboardInterrupt:
            print("\n[!] INTERRUPCIÓN DETECTADA (Ctrl+C). Finalizando y guardando de emergencia...")
        finally:
            # VOLCADO DE EMERGENCIA FINAL
            if data_snapshots:
                self._save_json(output_file, data_snapshots)
                print(f"[OK] Volcado final completado: {len(data_snapshots)} capturas guardadas en {output_file}")
            else:
                print("[WARN] No se habían capturado snapshots para guardar.")

def process_batch(replays_dir: str, output_dir: str):
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    rofl_files = glob.glob(os.path.join(replays_dir, "*.rofl"))
    extractor = ReplayExtractor()
    for idx, rofl_file in enumerate(rofl_files):
        game_id = Path(rofl_file).stem
        json_salida = Path(output_dir) / f"{game_id}_data.json"
        
        print(f"\n--- Procesando Replay {idx+1}/{len(rofl_files)} ---")
        if json_salida.exists(): 
            print(f"    [SKIP] Ya procesado: {json_salida.name}")
            continue

        cloud_timeline = extractor.get_match_timeline(rofl_file)
        
        # Obtener los datos extra de oro y victoria
        match_id_str = f"{Path(rofl_file).stem.split('_')[0].upper()}_{Path(rofl_file).stem.split('-')[-1].split('_')[0]}" 
        # Formato EUW1-1234567_01 o EUW1_1234567. Reparamos parseo para match_id
        match_re = re.search(r'([A-Za-z0-9]+)[_-](\d{8,})', Path(rofl_file).stem)
        if match_re:
            match_id_full = f"{match_re.group(1).upper()}_{match_re.group(2)}"
            ganador = extractor.obtener_equipo_ganador(match_id_full)
            botas_dict = extractor.obtener_role_bound_items(match_id_full)
            botas_tiempos = extractor.obtener_tiempos_mision_botas(match_id_full)
        else:
            ganador = None
            botas_dict = None
            botas_tiempos = None
        
        if extractor.watch_replay(rofl_file):
            if extractor.wait_for_game_launch(timeout_secs=120):
                extractor.extract_game_data(str(json_salida), sample_interval=10.0, timeline_events=cloud_timeline, equipo_ganador=ganador, botas_dict=botas_dict, botas_tiempos=botas_tiempos)
                
                # Cerrar el proceso
                os.system("taskkill /F /IM \"League of Legends.exe\" >nul 2>&1")
                
                print("\n[INFO] Partida terminada con éxito.")
                print("[TIP] Ahora es el momento SEGURO para cancelar (Pulsa Ctrl+C para detenerte).")
                for i in range(30, 0, -1):
                    print(f"    Siguiente partida en {i} segundos...   ", end='\r')
                    time.sleep(1)
                print("")

if __name__ == "__main__":
    REPLAYS_DIR = r"I:\Riot Games\lol Replays"
    OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "replays_data_extracted")
    
    print("Iniciando Herramienta de Extracción Robusta...")
    try:
        process_batch(REPLAYS_DIR, OUTPUT_DIR)
    except KeyboardInterrupt:
        print("\n[OK] Script detenido por el usuario.")
