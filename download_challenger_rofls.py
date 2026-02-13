import requests
import os
import time
import json
import base64
import psutil
import threading
from datetime import datetime
from urllib3.exceptions import InsecureRequestWarning

# Desactivar warnings SSL
requests.packages.urllib3.disable_warnings(InsecureRequestWarning)

# === CONFIGURACIÓN ===
API_KEY = "RGAPI-14acd660-5abe-4316-a45f-f74a89bc14ff"
REGION_PLATFORM = "EUW1"
REGION_ROUTING = "europe"
SAVE_PATH = "Replays_Live"
LOG_FILE = "live_monitor.log"

# Configuración de monitoreo
CHECK_INTERVAL = 180  # Revisar cada 3 minutos
NUM_CHALLENGERS = 50  # Top 50 Challengers
MAX_WAIT_TIME = 120   # Esperar máximo 2 horas por partida

# Estado del sistema
os.makedirs(SAVE_PATH, exist_ok=True)
active_games = {}  # {game_id: {puuid, start_time, player_name}}
downloaded_games = set()  # IDs de partidas ya descargadas
challenger_players = []  # Lista de jugadores a monitorear

# Cliente LCU
lcu_session = None
lcu_base_url = None

# === LOGGING ===
def log(message, level="INFO"):
    """Log con timestamp"""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_msg = f"[{timestamp}] [{level}] {message}"
    print(log_msg)
    
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(log_msg + "\n")

# === FUNCIONES LCU ===
def get_lcu_credentials():
    """Obtiene credenciales del cliente de LoL"""
    for proc in psutil.process_iter(['name', 'cmdline']):
        try:
            if proc.info['name'] == 'LeagueClientUx.exe':
                cmdline = proc.info['cmdline']
                port = None
                token = None
                
                for arg in cmdline:
                    if '--app-port=' in arg:
                        port = arg.split('=')[1]
                    elif '--remoting-auth-token=' in arg:
                        token = arg.split('=')[1]
                
                if port and token:
                    return port, token
        except:
            continue
    return None, None

def init_lcu_session():
    """Inicializa conexión con el cliente local"""
    global lcu_session, lcu_base_url
    
    port, token = get_lcu_credentials()
    
    if not port or not token:
        return False
    
    lcu_session = requests.Session()
    auth = base64.b64encode(f"riot:{token}".encode()).decode()
    lcu_session.headers.update({
        'Authorization': f'Basic {auth}',
        'Content-Type': 'application/json'
    })
    lcu_session.verify = False
    lcu_base_url = f"https://127.0.0.1:{port}"
    
    # Verificar conexión
    try:
        r = lcu_session.get(f"{lcu_base_url}/lol-summoner/v1/current-summoner", timeout=5)
        if r.status_code == 200:
            summoner = r.json()
            log(f"✅ Conectado al cliente como: {summoner['displayName']}")
            return True
    except:
        pass
    
    return False

# === FUNCIONES API RIOT ===
def get_summoner_name_from_puuid(puuid):
    """Obtiene el Riot ID completo (gameName#tagLine) desde un PUUID"""
    url = f"https://{REGION_ROUTING}.api.riotgames.com/riot/account/v1/accounts/by-puuid/{puuid}"
    headers = {"X-Riot-Token": API_KEY}

    try:
        r = requests.get(url, headers=headers, timeout=10)
        if r.status_code == 200:
            data = r.json()
            game_name = data.get("gameName", "Unknown")
            tag_line = data.get("tagLine", "")
            return f"{game_name}#{tag_line}" if tag_line else game_name
        elif r.status_code == 404:
            return "Desconocido"
        else:
            log(f"⚠️ Error {r.status_code} obteniendo Riot ID para {puuid}", "WARN")
    except Exception as e:
        log(f"❌ Excepción obteniendo Riot ID: {e}", "ERROR")

    return "Unknown"

def get_challenger_list():
    """Obtiene lista de jugadores Challenger"""
    log("Actualizando lista de Challengers...")
    
    url = f"https://{REGION_PLATFORM}.api.riotgames.com/lol/league/v4/challengerleagues/by-queue/RANKED_SOLO_5x5"
    headers = {"X-Riot-Token": API_KEY}
    
    try:
        r = requests.get(url, headers=headers, timeout=10)
        r.raise_for_status()
        
        entries = r.json()["entries"][:NUM_CHALLENGERS]
        
        players = []
        for i, entry in enumerate(entries):
            if "puuid" in entry:
                puuid = entry["puuid"]
                
                # Obtener el nombre usando el PUUID
                player_name = get_summoner_name_from_puuid(puuid)
                
                players.append({
                    "puuid": puuid,
                    "name": player_name,
                    "lp": entry.get("leaguePoints", 0)
                })
                
                # Log de progreso cada 10 jugadores
                if (i + 1) % 10 == 0:
                    log(f"  Procesados {i + 1}/{NUM_CHALLENGERS}...")
                
                # Rate limit
                time.sleep(0.8)
        
        log(f"✅ {len(players)} Challengers cargados con nombres")
        return players
        
    except Exception as e:
        log(f"❌ Error obteniendo Challengers: {e}", "ERROR")
        return []

def check_active_game(puuid):
    """Verifica si un jugador está en partida activa"""
    url = f"https://{REGION_PLATFORM}.api.riotgames.com/lol/spectator/v5/active-games/by-summoner/{puuid}"
    headers = {"X-Riot-Token": API_KEY}
    
    try:
        r = requests.get(url, headers=headers, timeout=10)
        if r.status_code == 200:
            return r.json()
        elif r.status_code == 404:
            return None  # No está en partida
    except:
        pass
    return None

def download_replay_lcu(game_id, region_code):
    """Descarga replay usando LCU"""
    if not lcu_session or not lcu_base_url:
        log(f"⚠️  Cliente no conectado, no se puede descargar {game_id}", "WARN")
        return False
    
    download_url = f"{lcu_base_url}/lol-replays/v1/rofls/{game_id}/download"
    
    payload = {
        "componentType": "replay-button_match-history",
        "gameId": int(game_id),
        "platformId": region_code
    }
    
    try:
        response = lcu_session.post(download_url, json=payload, timeout=30)
        
        if response.status_code == 200:
            log(f"✅ Descarga iniciada: {game_id}")
            
            # Esperar a que se complete
            if wait_for_download(game_id):
                copy_rofl_file(game_id)
                return True
            
        elif response.status_code == 204:
            log(f"⚠️  Replay no disponible todavía: {game_id}", "WARN")
            return False
        else:
            log(f"❌ Error {response.status_code} descargando {game_id}", "ERROR")
            return False
            
    except Exception as e:
        log(f"❌ Excepción descargando {game_id}: {e}", "ERROR")
        return False

def wait_for_download(game_id, max_wait=120):
    """Espera a que se complete la descarga"""
    check_url = f"{lcu_base_url}/lol-replays/v1/rofls/{game_id}"
    
    waited = 0
    while waited < max_wait:
        try:
            r = lcu_session.get(check_url, timeout=5)
            
            if r.status_code == 200:
                data = r.json()
                state = data.get('state', '')
                
                if state == 'ready':
                    return True
                elif state == 'downloading':
                    progress = data.get('downloadProgress', 0)
                    if waited % 10 == 0:  # Log cada 10 segundos
                        log(f"  📊 Descargando {game_id}... {progress}%")
        except:
            pass
        
        time.sleep(2)
        waited += 2
    
    return False

def copy_rofl_file(game_id):
    """Copia el archivo .rofl descargado"""
    replays_dir = os.path.expanduser("~/Documents/League of Legends/Replays")
    
    if not os.path.exists(replays_dir):
        log(f"⚠️  Carpeta de replays no encontrada", "WARN")
        return
    
    for filename in os.listdir(replays_dir):
        if str(game_id) in filename and filename.endswith('.rofl'):
            src = os.path.join(replays_dir, filename)
            dst = os.path.join(SAVE_PATH, filename)
            
            try:
                import shutil
                shutil.copy2(src, dst)
                log(f"✅ Archivo guardado: {filename}")
                
                # Guardar metadata
                save_metadata(game_id, filename)
                return True
            except Exception as e:
                log(f"❌ Error copiando: {e}", "ERROR")
                return False
    
    log(f"⚠️  Archivo .rofl no encontrado para {game_id}", "WARN")
    return False

def save_metadata(game_id, filename):
    """Guarda información adicional del replay"""
    metadata = {
        "game_id": game_id,
        "filename": filename,
        "download_time": datetime.now().isoformat(),
        "region": REGION_PLATFORM
    }
    
    meta_path = os.path.join(SAVE_PATH, f"{game_id}_meta.json")
    with open(meta_path, "w") as f:
        json.dump(metadata, f, indent=2)

# === GESTIÓN DE PARTIDAS ===
def monitor_game(game_id, puuid, player_name):
    """Thread que monitorea una partida específica"""
    log(f"🎮 Monitoreando partida: {game_id} ({player_name})")
    
    start_time = time.time()
    
    # Esperar a que termine la partida
    while time.time() - start_time < MAX_WAIT_TIME * 60:
        time.sleep(60)  # Revisar cada minuto
        
        # Verificar si sigue en partida
        active = check_active_game(puuid)
        
        if active is None or active.get('gameId') != game_id:
            # La partida terminó
            elapsed = int((time.time() - start_time) / 60)
            log(f"✅ Partida terminada: {game_id} (duró ~{elapsed} min)")
            
            # Esperar 3 minutos para que Riot procese (aumentado de 2 a 3)
            log(f"⏳ Esperando 3 min para que Riot procese el replay...")
            time.sleep(180)
            
            # Intentar descargar
            region_code = REGION_PLATFORM.upper()
            
            # Intentar hasta 5 veces (aumentado de 3 a 5)
            for attempt in range(5):
                if download_replay_lcu(game_id, region_code):
                    downloaded_games.add(game_id)
                    log(f"🎉 REPLAY DESCARGADO: {game_id}")
                    break
                else:
                    if attempt < 4:
                        wait_time = 90  # Esperar 1.5 minutos entre intentos
                        log(f"⏳ Reintento {attempt + 1}/5 en {wait_time}s...")
                        time.sleep(wait_time)
            else:
                log(f"❌ No se pudo descargar {game_id} después de 5 intentos", "ERROR")
                log(f"   Posibles razones:", "ERROR")
                log(f"   - Partida de patch antiguo", "ERROR")
                log(f"   - Riot no guardó el replay", "ERROR")
                log(f"   - Replay expiró (>48h)", "ERROR")
            
            # Remover de activos
            if game_id in active_games:
                del active_games[game_id]
            
            return
    
    log(f"⚠️  Timeout monitoreando {game_id}", "WARN")
    if game_id in active_games:
        del active_games[game_id]

def monitoring_cycle():
    """Un ciclo de monitoreo"""
    log("\n" + "="*70)
    log("🔍 CICLO DE MONITOREO")
    log("="*70)
    
    new_games = 0
    in_game_now = 0
    
    for player in challenger_players:
        puuid = player['puuid']
        name = player['name']
        
        # Verificar si está en partida AHORA
        game_info = check_active_game(puuid)
        
        if game_info:
            game_id = game_info['gameId']
            in_game_now += 1
            
            # Verificar que la partida esté realmente en curso
            game_start_time = game_info.get('gameStartTime', 0)
            game_length = game_info.get('gameLength', 0)
            
            # Si gameLength > 0, significa que la partida está en curso
            # Si gameStartTime es muy reciente (menos de 2 horas)
            current_time = int(time.time() * 1000)  # En milisegundos
            time_since_start = (current_time - game_start_time) / 1000 / 60  # En minutos
            
            # Solo monitorear si la partida comenzó hace menos de 60 minutos
            if time_since_start > 60:
                log(f"  ⏭️  Saltando partida antigua {game_id} ({name}) - Comenzó hace {int(time_since_start)} min")
                continue
            
            # Si no la estamos monitoreando ya
            if game_id not in active_games and game_id not in downloaded_games:
                log(f"🆕 Nueva partida detectada: {game_id} ({name}) - En curso hace {int(time_since_start)} min")
                
                # Agregar a monitoreo
                active_games[game_id] = {
                    'puuid': puuid,
                    'start_time': time.time(),
                    'player_name': name
                }
                
                # Iniciar thread de monitoreo (pasar puuid directamente)
                thread = threading.Thread(
                    target=monitor_game,
                    args=(game_id, puuid, name),
                    daemon=True
                )
                thread.start()
                
                new_games += 1
        
        time.sleep(0.5)  # Evitar rate limit
    
    log(f"✅ Ciclo completado. En partida ahora: {in_game_now}")
    log(f"   Nuevas partidas monitoreadas: {new_games}")
    log(f"   Partidas activas monitoreando: {len(active_games)}")
    log(f"   Replays descargados (total): {len(downloaded_games)}")

# === MAIN ===
def main():
    global challenger_players
    
    log("="*70)
    log("🚀 MONITOR EN TIEMPO REAL - DESCARGA DE REPLAYS")
    log("="*70)
    
    # Verificar cliente
    log("\n[1/3] Conectando al cliente de LoL...")
    if not init_lcu_session():
        log("❌ Cliente de LoL no encontrado. Abre el cliente y reinicia.", "ERROR")
        return
    
    # Cargar Challengers
    log("\n[2/3] Cargando lista de Challengers...")
    challenger_players = get_challenger_list()
    
    if not challenger_players:
        log("❌ No se pudo cargar la lista de Challengers", "ERROR")
        return
    
    # Mostrar top 5
    log("\nTop 5 Challengers monitoreados:")
    for i, p in enumerate(challenger_players[:5], 1):
        log(f"  {i}. {p['name']} ({p['lp']} LP)")
    
    # Iniciar monitoreo
    log(f"\n[3/3] Iniciando monitoreo continuo (cada {CHECK_INTERVAL}s)")
    log("="*70)
    log("Presiona Ctrl+C para detener\n")
    
    try:
        cycle_count = 0
        while True:
            cycle_count += 1
            log(f"\n>>> CICLO #{cycle_count} <<<")
            
            monitoring_cycle()
            
            # Actualizar lista de Challengers cada 10 ciclos
            if cycle_count % 10 == 0:
                log("\n🔄 Actualizando lista de Challengers...")
                new_list = get_challenger_list()
                if new_list:
                    challenger_players = new_list
            
            # Esperar hasta el próximo ciclo
            log(f"\n⏸️  Esperando {CHECK_INTERVAL} segundos...")
            time.sleep(CHECK_INTERVAL)
            
    except KeyboardInterrupt:
        log("\n⚠️  Deteniendo monitor...")
        log(f"Total de replays descargados: {len(downloaded_games)}")
        log("✅ Monitor detenido correctamente")

if __name__ == "__main__":
    main()
