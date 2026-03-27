import os
import time
import json
import requests
import urllib3
import glob
from pathlib import Path
from base64 import b64encode

# Desactivar warnings de certificados SSL inseguros para la API de League
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

class ReplayExtractor:
    def __init__(self, league_path: str = r"I:\Riot Games\League of Legends"):
        self.league_path = Path(league_path)
        self.lockfile_path = self.league_path / "lockfile"
        self.replay_api_url = "https://127.0.0.1:2999"
        
        self.lcu_port = None
        self.lcu_token = None
        self.lcu_headers = None
        
        self._load_lcu_credentials()
        self._check_config_enabled()

    def _check_config_enabled(self):
        """Verifica que EnableReplayApi=1 está en el game.cfg."""
        cfg_path = self.league_path / "Config" / "game.cfg"
        if not cfg_path.exists():
            print(f"[WARN] No se encontró el archivo {cfg_path}")
            return
            
        try:
            with open(cfg_path, 'r', encoding='utf-8') as f:
                content = f.read()
            if "EnableReplayApi=1" not in content:
                print(f"[WARN] EnableReplayApi=1 NO ENCONTRADO en {cfg_path}")
                print(r"[INFO] Por favor abre C:\Riot Games\League of Legends\Config\game.cfg")
                print("[INFO] Y agrega EnableReplayApi=1 debajo de la sección [General].")
        except Exception as e:
            print(f"[ERROR] No se pudo leer la configuración: {e}")

    def _load_lcu_credentials(self):
        """Lee el lockfile del cliente de LoL para conectarse a la LCU API."""
        if not self.lockfile_path.exists():
            print(f"¡El cliente de LoL debe estar abierto! No se encontró: {self.lockfile_path}")
            return
            
        with open(self.lockfile_path, 'r') as f:
            data = f.read().split(':')
            # Formato: LeagueClient:PID:Port:AuthToken:Protocol
            self.lcu_port = data[2]
            self.lcu_token = data[3]
            
        auth_string = f"riot:{self.lcu_token}"
        auth_b64 = b64encode(auth_string.encode('ascii')).decode('ascii')
        self.lcu_headers = {
            "Authorization": f"Basic {auth_b64}",
            "Accept": "application/json"
        }
        print(f"[INFO] Conectado a LCU API en puerto {self.lcu_port}")

    def watch_replay(self, rofl_path: str):
        """Envía el comando al LCU para comenzar a reproducir un archivo .rofl."""
        if not self.lcu_port:
            self._load_lcu_credentials()
            if not self.lcu_port:
                print("[ERROR] No hay conexión LCU API. Abre el cliente de LoL.")
                return False

        import re
        game_id_str = Path(rofl_path).stem  # Ejemplo: 14-22_EUW1-7190363375_01
        
        # El endpoint de LCU espera un uint64 para el gameId (ej: 7190363375)
        # Buscamos un número de al menos 8 dígitos para evitar pescar fechas como '14'
        match = re.search(r'\d{8,}', game_id_str)
        if not match:
            print(f"[ERROR] No se pudo encontrar un gameId numérico válido en {game_id_str}")
            return False
            
        game_id = match.group()
        url = f"https://127.0.0.1:{self.lcu_port}/lol-replays/v1/rofls/{game_id}/watch"
        print(f"[INFO] Solicitando reproducción de la partida: {game_id}")
        
        try:
            res = requests.post(url, headers=self.lcu_headers, json={}, verify=False)
            if res.status_code in (200, 204):
                print(f"[OK] Replay {game_id} iniciado correctamente. Esperando que cargue el juego...")
                return True
            else:
                print(f"[ERROR] Falló al iniciar el replay. Código: {res.status_code}, Res: {res.text}")
                return False
        except requests.ConnectionError:
            print("[ERROR] No se pudo conectar a la LCU. Asegúrate de que el cliente de LoL esté abierto.")
            return False

    def wait_for_game_launch(self, timeout_secs=120):
        """Espera a que el proceso del juego cargue y la Replay API responda."""
        start_time = time.time()
        print("[INFO] Esperando a que el cliente del juego (API en puerto 2999) responda...")
        while time.time() - start_time < timeout_secs:
            try:
                res = requests.get(f"{self.replay_api_url}/liveclientdata/allgamedata", verify=False, timeout=2)
                if res.status_code == 200:
                    data = res.json()
                    game_time = data.get("gameData", {}).get("gameTime", 0)
                    if game_time >= 0:
                        print(f"[OK] ¡Juego en marcha! Tiempo actual: {game_time:.1f}s")
                        return True
            except requests.ConnectionError:
                pass
            except Exception:
                pass
            time.sleep(3)
            
        print("[ERROR] Timeout esperando a que el juego cargue.")
        return False

    def extract_game_data(self, output_file: str, sample_interval: float = 10.0):
        """
        Bucle de extracción. Intenta ajustar la velocidad a 8x y samplea los datos de
        la API LiveClientData según el intervalo especificado.
        """
        import traceback
        print(f"\n[!] Iniciando extracción de datos. Archivo de salida: {output_file}")
        
        data_snapshots = []
        last_sample_time = -100.0  # Para forzar el primer sample si gameTime=0
        is_game_ended = False
        
        # Una vez que el juego responde, intentamos forzar la cámara libre o pause/speed:
        try:
            print("[INFO] Ajustando velocidad a 8x y play...")
            requests.post(f"{self.replay_api_url}/replay/playback", json={"speed": 8.0, "paused": False}, verify=False, timeout=3)
        except Exception as e:
            print(f"[WARN] No se pudo cambiar la velocidad de ReplayAPI via puerto 2999: {e}")
            # Si `EnableReplayApi=1` no estaba seteada, esto fallará por timeout o ConnectionRefused.

        print(f"[INFO] Bucle de sampleo iniciado. Frecuencia solicitada: cada {sample_interval}s in-game.")
        while not is_game_ended:
            try:
                res = requests.get(f"{self.replay_api_url}/liveclientdata/allgamedata", verify=False, timeout=3)
                if res.status_code != 200:
                    time.sleep(1)
                    continue
                    
                game_data = res.json()
                game_info = game_data.get("gameData", {})
                current_time = game_info.get("gameTime", 0.0)
                
                # Para saber si la partida terminó verificamos los eventos o la reconexión:
                events_list = game_data.get("events", {}).get("Events", [])
                for ev in events_list:
                    if ev.get("EventName") == "GameEnd":
                        is_game_ended = True
                
                # Revisamos si pasaron 10 segundos in-game
                if current_time >= last_sample_time + sample_interval or is_game_ended:
                    print(f"  [+] Guardando Snapshot en el {int(current_time // 60)}:{int(current_time % 60):02d}")
                    
                    snapshot = {
                        "game_time": current_time,
                        "active_player": game_data.get("activePlayer"),
                        "all_players": game_data.get("allPlayers"),
                        "events": events_list,
                        "game_data": game_info
                    }
                    data_snapshots.append(snapshot)
                    last_sample_time = current_time

                # Si el juego va a 8x, 0.5s en la vida real son 4s in-game.
                # Si dormimos 0.5s cada ciclo, verificamos current_time sin saturar la CPU.
                time.sleep(0.5)

            except requests.ConnectionError:
                print("[INFO] El juego se cerró o se perdió la conexión API. Fin de la partida.")
                is_game_ended = True
                break
            except Exception as e:
                print(f"[WARN] Error procesando datos: {e}")
                time.sleep(1)

        # Volcar snapshots al terminar la iteración de la partida
        if data_snapshots:
            try:
                with open(output_file, 'w', encoding='utf-8') as f:
                    json.dump(data_snapshots, f, ensure_ascii=False, indent=2)
                print(f"[OK] Datos guardados: {output_file} ({len(data_snapshots)} capturas)")
            except Exception as e:
                print(f"[ERROR] No se pudo guardar JSON: {e}")
        else:
            print("[WARN] Terminó la extracción pero no se capturaron datos.")

def process_batch(replays_dir: str, output_dir: str):
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    
    rofl_files = glob.glob(os.path.join(replays_dir, "*.rofl"))
    if not rofl_files:
        print(f"[WARN] No se encontraron archivos .rofl en '{replays_dir}'")
        return

    print(f"[INFO] Procesar batch: {len(rofl_files)} partidas.")
    
    extractor = ReplayExtractor()

    for idx, rofl_file in enumerate(rofl_files):
        game_id = Path(rofl_file).stem
        json_salida = Path(output_dir) / f"{game_id}_data.json"
        
        print(f"\n{'='*50}")
        print(f"--- Procesando Replay {idx+1}/{len(rofl_files)}: {game_id} ---")
        
        if json_salida.exists():
            print(f"[SKIP] Ya procesado: {json_salida.name}")
            continue
            
        # Lanza el juego a través de LCU
        success = extractor.watch_replay(rofl_file)
        if not success:
            continue
            
        # Espera que el exe inicie
        opened = extractor.wait_for_game_launch(timeout_secs=120)
        if opened:
            extractor.extract_game_data(str(json_salida), sample_interval=10.0)
            
            # Cierra el cliente .exe de League of Legends forzosamente
            print("\n[INFO] Cerrando League of Legends.exe para pasar a la siguiente...")
            os.system("taskkill /F /IM \"League of Legends.exe\" >nul 2>&1")
            
            # Espera 5 segundos para que Riot Client limpie procesos
            time.sleep(5)
            
if __name__ == "__main__":
    REPLAYS_DIR = r"I:\Riot Games\lol Replays"
    
    # Podrías crear una carpeta 'replays_data' junto a 01_download_replays.py
    OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "replays_data_extracted")
    
    print("Iniciando Herramienta de Extracción de Datos de Replay a JSON...")
    process_batch(REPLAYS_DIR, OUTPUT_DIR)
