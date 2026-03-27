"""
rofl_downloader.py — v2.0
-------------------------
Descarga replays (.rofl) via LCU API.

MODOS DE USO:
  1. --auto   → Descarga automáticamente partidas de los mejores de EUW
               (Challenger + Grandmaster) usando Riot API + LCU.
         python 01_download_replays.py --auto

  2. Sin argumentos → historial propio via LCU (interactivo)
         python 01_download_replays.py

  3. Con match_id → descarga directa
         python 01_download_replays.py EUW1_7123456789

  4. Importado desde el recorder
         from rofl_downloader import download_and_wait
         rofl_path = download_and_wait("EUW1_7123456789")

Requisitos:
    pip install requests watchdog
"""

import os
import sys
import time
import re
import json
import threading
import logging
from pathlib import Path

import requests
import urllib3
from requests.auth import HTTPBasicAuth
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Configuración ─────────────────────────────────────────────────────────────
REPLAYS_DIR      = Path("I:/Riot Games/lol Replays")
LOCKFILE_PATH    = Path("I:/Riot Games/League of Legends/lockfile")
DOWNLOAD_TIMEOUT = 300  # segundos máximos esperando el .rofl
POLL_INTERVAL    = 2    # segundos entre comprobaciones
REGION_PREFIX    = "EUW1"

# ── Riot API (Modo Monitor EUW) ───────────────────────────────────────────────
API_KEY          = os.getenv('RIOT_API_KEY', 'RGAPI-c69b048f-c19f-4530-a6f7-768dbd43513e')
REGION_PLATFORM  = "euw1"
REGION_ROUTING   = "europe"
PLATFORM_ID      = "EUW1"

STATE_FILE       = "recorder_state.json"
CHECK_INTERVAL   = 120
AUTO_STATE_FILE  = "auto_download_state.json"
AUTO_CHECK_INTERVAL = 180                    # segundos entre chequeos de nuevas partidas
MAX_MATCHES_PER_PLAYER = 5                   # últimas N partidas por jugador
TOP_PLAYERS_COUNT      = 100                 # cuántos GM cargar (los top N GM)


# ══════════════════════════════════════════════════════════════════════════════
# 1. LCU — conexión
# ══════════════════════════════════════════════════════════════════════════════

def find_lockfile() -> Path:
    candidates = [
        LOCKFILE_PATH,
        Path(os.environ.get("LOCALAPPDATA", "")) / "Riot Games" / "League of Legends" / "lockfile",
    ]
    for p in candidates:
        if p.exists():
            return p
    raise FileNotFoundError(
        "No se encontró el lockfile. Asegúrate de que el cliente de LoL está abierto."
    )


def parse_lockfile(lockfile: Path) -> dict:
    content = lockfile.read_text(encoding="utf-8").strip()
    parts = content.split(":")
    if len(parts) != 5:
        raise ValueError(f"Formato de lockfile inesperado: {content!r}")
    return {
        "process":  parts[0],
        "pid":      parts[1],
        "port":     parts[2],
        "password": parts[3],
        "protocol": parts[4],
    }


def get_lcu_session() -> tuple:
    """Devuelve (base_url, auth) listos para usar con requests."""
    lf   = find_lockfile()
    data = parse_lockfile(lf)
    base_url = f"https://127.0.0.1:{data['port']}"
    auth     = HTTPBasicAuth("riot", data["password"])
    log.info("LCU encontrado — puerto %s", data["port"])
    return base_url, auth


# ══════════════════════════════════════════════════════════════════════════════
# 2. LCU — historial de partidas (fuente automática de IDs propios)
# ══════════════════════════════════════════════════════════════════════════════

def get_match_history(count: int = 20) -> list:
    """
    Obtiene las últimas `count` partidas del historial via LCU.
    No necesita Riot API key — usa el cliente directamente.
    """
    base_url, auth = get_lcu_session()

    # PUUID del jugador logueado
    r = requests.get(
        f"{base_url}/lol-summoner/v1/current-summoner",
        auth=auth, verify=False, timeout=10
    )
    if r.status_code != 200:
        raise RuntimeError(f"No se pudo obtener el summoner actual: {r.status_code}")

    puuid = r.json().get("puuid")
    if not puuid:
        raise RuntimeError("PUUID no disponible.")

    # Historial
    r = requests.get(
        f"{base_url}/lol-match-history/v1/products/lol/{puuid}/matches",
        params={"begIndex": 0, "endIndex": count},
        auth=auth, verify=False, timeout=10
    )
    if r.status_code != 200:
        raise RuntimeError(f"Error obteniendo historial: {r.status_code} — {r.text}")

    raw_games = r.json().get("games", {}).get("games", [])
    if not raw_games:
        raise RuntimeError("Historial vacío.")

    queue_names = {
        420: "Ranked Solo", 440: "Ranked Flex",
        400: "Normal Draft", 430: "Normal Blind", 450: "ARAM",
    }

    games = []
    for g in raw_games:
        game_id  = g.get("gameId")
        match_id = f"{REGION_PREFIX}_{game_id}"
        queue    = queue_names.get(g.get("queueId", 0), f"Queue {g.get('queueId', '?')}")
        duration = g.get("gameDuration", 0)
        ts       = g.get("gameCreation", 0)

        # Resultado: el primer participante siempre es el jugador local
        participants = g.get("participants", [])
        result = "?"
        if participants:
            win = participants[0].get("stats", {}).get("win", False)
            result = "Victoria" if win else "Derrota"

        games.append({
            "match_id":  match_id,
            "game_id":   game_id,
            "queue":     queue,
            "duration":  duration,
            "timestamp": ts,
            "result":    result,
        })

    return games


def get_recent_match_ids(count: int = 20) -> list:
    """Devuelve solo los match_ids. Útil para scripts externos."""
    return [g["match_id"] for g in get_match_history(count)]


# ══════════════════════════════════════════════════════════════════════════════
# 3. LCU — descarga del replay
# ══════════════════════════════════════════════════════════════════════════════

def normalize_match_id(raw: str) -> str:
    if "_" in raw:
        return raw
    return f"{REGION_PREFIX}_{raw}"


def extract_game_id(match_id: str) -> int:
    """Extrae el ID numérico de un match_id como EUW1_7799124028."""
    if "_" in match_id:
        return int(match_id.split("_")[-1])
    return int(match_id)


def request_download(match_id: str) -> None:
    base_url, auth = get_lcu_session()
    game_id = extract_game_id(match_id)
    url  = f"{base_url}/lol-replays/v1/rofls/{game_id}/download"
    log.info("Solicitando descarga: %s (gameId=%s)", match_id, game_id)

    # El LCU requiere componentType en el body
    resp = requests.post(
        url, auth=auth, verify=False, timeout=10,
        json={"componentType": "replay-button_match-history"}
    )

    if resp.status_code in (200, 204):
        log.info("Descarga iniciada correctamente.")
    elif resp.status_code == 409:
        log.info("El replay ya está descargado o en progreso.")
    elif resp.status_code == 404:
        raise RuntimeError(
            f"Match {match_id} no encontrado. "
            "Puede que no esté disponible (>14 días) o la partida no haya terminado."
        )
    else:
        raise RuntimeError(f"LCU respondió {resp.status_code}: {resp.text}")


def get_download_status(match_id: str) -> str:
    """Estado: 'checking' | 'downloading' | 'watch' | 'error' | 'lost'"""
    base_url, auth = get_lcu_session()
    game_id = extract_game_id(match_id)
    resp = requests.get(
        f"{base_url}/lol-replays/v1/rofls/{game_id}",
        auth=auth, verify=False, timeout=10
    )
    if resp.status_code == 200:
        return resp.json().get("state", "unknown")
    return "unknown"


# ══════════════════════════════════════════════════════════════════════════════
# 4. Watcher — detectar el .rofl en disco
# ══════════════════════════════════════════════════════════════════════════════

class RoflHandler(FileSystemEventHandler):
    def __init__(self, expected_id: str):
        super().__init__()
        numeric_id   = expected_id.split("_")[-1]
        self.pattern = re.compile(rf"{re.escape(numeric_id)}.*\.rofl$", re.IGNORECASE)
        self.found_path = None

    def on_created(self, event):
        if not event.is_directory and self.pattern.search(event.src_path):
            self.found_path = Path(event.src_path)
            log.info("✅ Archivo detectado: %s", self.found_path.name)

    def on_modified(self, event):
        self.on_created(event)


def wait_for_rofl(match_id: str, timeout: int = DOWNLOAD_TIMEOUT) -> Path:
    REPLAYS_DIR.mkdir(parents=True, exist_ok=True)

    # ¿Ya existe?
    numeric_id = match_id.split("_")[-1]
    for existing in REPLAYS_DIR.glob(f"*{numeric_id}*.rofl"):
        log.info("El .rofl ya existe en disco: %s", existing.name)
        return existing

    handler  = RoflHandler(match_id)
    observer = Observer()
    observer.schedule(handler, str(REPLAYS_DIR), recursive=False)
    observer.start()

    log.info("Esperando .rofl en %s (máx. %ds)…", REPLAYS_DIR, timeout)
    deadline = time.time() + timeout

    try:
        while time.time() < deadline:
            if handler.found_path:
                time.sleep(1)
                return handler.found_path
            time.sleep(POLL_INTERVAL)
    finally:
        observer.stop()
        observer.join()

    raise TimeoutError(
        f"El .rofl de {match_id} no apareció en {timeout}s. "
        "Comprueba la carpeta o aumenta DOWNLOAD_TIMEOUT."
    )


# ══════════════════════════════════════════════════════════════════════════════
# 5. Pipeline principal — importable desde el recorder
# ══════════════════════════════════════════════════════════════════════════════

def download_and_wait(raw_match_id) -> Path:
    """
    Usado por el recorder al terminar una partida:
        from rofl_downloader import download_and_wait
        rofl_path = download_and_wait(game_id)           # int o str
        rofl_path = download_and_wait("EUW1_7123456789") # con prefijo
    """
    match_id = normalize_match_id(str(raw_match_id))
    request_download(match_id)

    # Logger de estado en background
    def _status_logger():
        for _ in range(10):
            time.sleep(3)
            try:
                state = get_download_status(match_id)
                log.info("Estado LCU: %s", state)
                if state in ("watch", "error", "lost"):
                    break
            except Exception:
                break

    threading.Thread(target=_status_logger, daemon=True).start()
    return wait_for_rofl(match_id)


# ══════════════════════════════════════════════════════════════════════════════
# 6. Riot API — helpers para modo automático
# ══════════════════════════════════════════════════════════════════════════════

def _riot_api_call(url: str, retries: int = 3) -> requests.Response | None:
    """Llama a la Riot API con reintentos y manejo de rate-limit."""
    headers = {"X-Riot-Token": API_KEY}
    for attempt in range(retries):
        try:
            r = requests.get(url, headers=headers, timeout=10)
            if r.status_code == 200:
                return r
            elif r.status_code == 429:  # Rate limited
                retry_after = int(r.headers.get("Retry-After", 5))
                log.warning("⏳ Rate limited, esperando %ds...", retry_after)
                time.sleep(retry_after)
                continue
            elif r.status_code == 403:
                log.error("🔑 API Key inválida o expirada (403).")
                return r
            else:
                log.warning("⚠️ Riot API %d en %s (intento %d/%d)",
                            r.status_code, url[:80], attempt + 1, retries)
                if attempt < retries - 1:
                    time.sleep(2)
                return r
        except requests.exceptions.RequestException as e:
            log.warning("⚠️ Request error: %s (intento %d/%d)", e, attempt + 1, retries)
            if attempt < retries - 1:
                time.sleep(2)
    return None


def get_top_players_euw() -> list:
    """Obtiene Challenger + Grandmaster de EUW."""
    log.info("🏆 Cargando Top jugadores EUW (Challenger + GM)...")
    players = []
    headers = {"X-Riot-Token": API_KEY}
    
    # Challenger
    url_c = f"https://{REGION_PLATFORM}.api.riotgames.com/lol/league/v4/challengerleagues/by-queue/RANKED_SOLO_5x5"
    r_c = requests.get(url_c, headers=headers)
    if r_c.status_code == 200:
        players.extend(r_c.json().get('entries', []))
        log.info("   👑 %d Challengers", len(r_c.json().get('entries', [])))
        
    # Grandmaster
    url_gm = f"https://{REGION_PLATFORM}.api.riotgames.com/lol/league/v4/grandmasterleagues/by-queue/RANKED_SOLO_5x5"
    r_gm = requests.get(url_gm, headers=headers)
    if r_gm.status_code == 200:
        gm_entries = r_gm.json().get('entries', [])
        top_gm = sorted(gm_entries, key=lambda x: x.get('leaguePoints', 0), reverse=True)[:100]
        players.extend(top_gm)
        log.info("   💎 %d Grandmasters", len(top_gm))
        
    log.info("✅ %d jugadores en lista total.", len(players))
    return players


def check_active_game(puuid):
    url = f"https://{REGION_PLATFORM}.api.riotgames.com/lol/spectator/v5/active-games/by-summoner/{puuid}"
    headers = {"X-Riot-Token": API_KEY}
    r = requests.get(url, headers=headers)
    return r.json() if r.status_code == 200 else None


def get_puuid_from_summoner_id(summoner_id: str) -> str:
    """Obtiene el PUUID a partir del summonerId encrypted."""
    base = f"https://{REGION_PLATFORM}.api.riotgames.com"
    r = _riot_api_call(f"{base}/lol/summoner/v4/summoners/{summoner_id}")
    if r and r.status_code == 200:
        return r.json().get("puuid", "")
    log.warning("⚠️ Fallo al resolver PUUID de %s: %s", summoner_id, r.status_code if r else "Timeout")
    return ""


def get_recent_match_ids_riot(puuid: str, count: int = MAX_MATCHES_PER_PLAYER,
                               queue: int = 420) -> list:
    """
    Obtiene los últimos match_ids de un jugador via Match v5 API.
    Por defecto solo Ranked Solo (queue=420).
    Devuelve lista de strings como ["EUW1_7799124028", ...].
    """
    url = (f"https://{REGION_ROUTING}.api.riotgames.com/lol/match/v5/matches"
           f"/by-puuid/{puuid}/ids?queue={queue}&start=0&count={count}")
    r = _riot_api_call(url)
    if r and r.status_code == 200:
        return r.json()  # lista de match_ids
    return []


# ══════════════════════════════════════════════════════════════════════════════
# 7. Modo automático — descarga partidas top EUW
# ══════════════════════════════════════════════════════════════════════════════

def _load_auto_state() -> set:
    """Carga los match_ids ya descargados/intentados."""
    if os.path.exists(AUTO_STATE_FILE):
        try:
            with open(AUTO_STATE_FILE, 'r') as f:
                return set(json.load(f).get("downloaded", []))
        except Exception:
            pass
    return set()


def _save_auto_state(downloaded: set):
    """Persiste los match_ids ya procesados."""
    with open(AUTO_STATE_FILE, 'w') as f:
        json.dump({"downloaded": list(downloaded)}, f)


def auto_download_mode():
    """
    Modo monitor: busca partidas recientes de Challenger/GM en EUW
    y las descarga automáticamente via LCU.
    """
    print("\n" + "═" * 65)
    print("   🤖 MODO AUTO-DESCARGA — Top EUW")
    print("   ─────────────────────────────────────────────")
    print(f"   Región:       {REGION_PREFIX}")
    print(f"   Replays dir:  {REPLAYS_DIR}")
    print(f"   Intervalo:    cada {AUTO_CHECK_INTERVAL}s")
    print(f"   Cola:         Ranked Solo (420)")
    print("═" * 65 + "\n")

    downloaded = _load_auto_state()
    log.info("📁 %d partidas ya procesadas en estado previo.", len(downloaded))

    players = get_top_players_euw()
    if not players:
        log.error("❌ No se pudieron obtener jugadores. Comprueba la API key.")
        return

    # Cargar caché de PUUIDs
    puuid_cache_file = "puuid_cache_euw.json"
    puuid_cache = {}
    if os.path.exists(puuid_cache_file):
        try:
            with open(puuid_cache_file, 'r') as f:
                puuid_cache = json.load(f)
        except Exception: pass

    log.info("🔑 Resolviendo PUUIDs (%d en caché)...", len(puuid_cache))
    
    player_puuids = []
    new_resolutions = 0

    for p in players:
        sid = p.get("summonerId", "")

        # ── Riot API now includes puuid directly in league entries ──
        puuid = p.get("puuid", "")  # available in current API responses

        if not puuid:
            # Fall back to cache keyed by summonerId
            puuid = puuid_cache.get(sid, "")

        if not puuid and sid:
            # Last resort: call summoner v4
            log.info("   🔍 Resolviendo via summoner API: %s", sid)
            puuid = get_puuid_from_summoner_id(sid)
            if puuid:
                puuid_cache[sid] = puuid
                new_resolutions += 1
                if new_resolutions % 5 == 0:
                    with open(puuid_cache_file, 'w') as f:
                        json.dump(puuid_cache, f)
            else:
                log.warning("   ⚠️ No se pudo resolver PUUID para summonerId=%s", sid)
            time.sleep(1.2)  # Rate limit
        elif puuid and sid and sid not in puuid_cache:
            # Cache the puuid we got directly from the entry
            puuid_cache[sid] = puuid
            new_resolutions += 1

        if puuid:
            player_puuids.append({
                "summonerId": sid,
                "puuid": puuid,
                "name": p.get("summonerName", p.get("riotIdGameName", "Unknown"))
            })

    # Guardar caché final
    if new_resolutions > 0:
        with open(puuid_cache_file, 'w') as f:
            json.dump(puuid_cache, f)
            
    log.info("👥 %d jugadores listos para monitoreo.", len(player_puuids))

    log.info("👥 %d jugadores con PUUID listo.", len(player_puuids))

    # ── Bucle principal ──
    cycle = 0
    while True:
        cycle += 1
        log.info("\n🔄 Ciclo #%d — Buscando partidas nuevas...", cycle)

        new_matches = []
        checked = 0

        for pp in player_puuids:
            match_ids = get_recent_match_ids_riot(pp["puuid"])
            for mid in match_ids:
                if mid not in downloaded:
                    new_matches.append(mid)
                    downloaded.add(mid)  # marcar como procesada para no repetir
            checked += 1
            time.sleep(0.8)  # rate-limit

            # Log progreso cada 10 jugadores
            if checked % 10 == 0:
                log.info("   🔍 %d/%d jugadores revisados, %d nuevas encontradas...",
                         checked, len(player_puuids), len(new_matches))

        # Eliminar duplicados manteniendo orden
        unique_matches = list(dict.fromkeys(new_matches))
        log.info("📋 %d partidas nuevas encontradas en ciclo #%d", len(unique_matches), cycle)

        # Descargar cada partida nueva
        success = 0
        errors  = 0
        for i, match_id in enumerate(unique_matches, 1):
            log.info("\n⬇️  [%d/%d] Descargando %s...", i, len(unique_matches), match_id)
            try:
                request_download(match_id)
                # Esperar a que el .rofl aparezca
                rofl = wait_for_rofl(match_id, timeout=DOWNLOAD_TIMEOUT)
                log.info("✅ Descargado: %s", rofl.name)
                success += 1
            except FileNotFoundError as e:
                log.warning("⚠️ LCU no disponible: %s", e)
                log.info("💤 Esperando 30s antes de reintentar...")
                time.sleep(30)
                errors += 1
            except RuntimeError as e:
                log.warning("⚠️ No se pudo descargar %s: %s", match_id, e)
                errors += 1
            except TimeoutError as e:
                log.warning("⏰ Timeout %s: %s", match_id, e)
                errors += 1
            except Exception as e:
                log.error("❌ Error inesperado %s: %s", match_id, e)
                errors += 1

            # Guardar estado tras cada descarga
            _save_auto_state(downloaded)
            time.sleep(2)  # pausa entre descargas

        log.info("\n📊 Ciclo #%d completado: %d descargadas, %d errores",
                 cycle, success, errors)
        log.info("📁 Total procesadas: %d", len(downloaded))
        _save_auto_state(downloaded)

        if not unique_matches:
            log.info("😴 Sin partidas nuevas. Esperando %ds...", AUTO_CHECK_INTERVAL)
        else:
            log.info("⏳ Siguiente chequeo en %ds...", AUTO_CHECK_INTERVAL)

        time.sleep(AUTO_CHECK_INTERVAL)


# ══════════════════════════════════════════════════════════════════════════════
# 8. CLI interactivo (historial propio)
# ══════════════════════════════════════════════════════════════════════════════

def _fmt_duration(s: int) -> str:
    m, sec = divmod(s, 60)
    return f"{m}:{sec:02d}"


def _fmt_date(ts_ms: int) -> str:
    import datetime
    if not ts_ms:
        return "?"
    return datetime.datetime.fromtimestamp(ts_ms / 1000).strftime("%d/%m %H:%M")


def cli_interactive():
    print("\n📋 Obteniendo historial de partidas via LCU...\n")
    try:
        games = get_match_history(20)
    except Exception as e:
        print(f"❌ {e}")
        sys.exit(1)

    print(f"{'#':<4} {'Match ID':<22} {'Resultado':<12} {'Cola':<16} {'Dur.':<8} {'Fecha'}")
    print("─" * 72)
    for i, g in enumerate(games, 1):
        print(f"{i:<4} {g['match_id']:<22} {g['result']:<12} {g['queue']:<16} "
              f"{_fmt_duration(g['duration']):<8} {_fmt_date(g['timestamp'])}")

    print("\nOpciones: número (1-20) | 'all' | 'q' para salir")
    choice = input("¿Qué descargar? ").strip().lower()

    if choice == "q":
        sys.exit(0)

    elif choice == "all":
        for g in games:
            print(f"\n⬇️  {g['match_id']}...")
            try:
                rofl = download_and_wait(g["match_id"])
                print(f"   ✅ {rofl}")
            except Exception as e:
                print(f"   ⚠️  {e}")

    elif choice.isdigit() and 1 <= int(choice) <= len(games):
        g = games[int(choice) - 1]
        print(f"\n⬇️  Descargando {g['match_id']}...")
        try:
            rofl = download_and_wait(g["match_id"])
            print(f"\n✅ Rofl listo en: {rofl}")
        except Exception as e:
            print(f"\n❌ {e}")
            sys.exit(1)

    else:
        # Asumir match_id directo
        print(f"\n⬇️  Descargando {choice}...")
        try:
            rofl = download_and_wait(choice)
            print(f"\n✅ Rofl listo en: {rofl}")
        except Exception as e:
            print(f"\n❌ {e}")
            sys.exit(1)


# ══════════════════════════════════════════════════════════════════════════════
# 9. Entry point
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    if "--auto" in sys.argv:
        try:
            auto_download_mode()
        except KeyboardInterrupt:
            log.info("👋 Auto-descarga detenida.")
            sys.exit(0)

    elif len(sys.argv) < 2:
        cli_interactive()

    else:
        try:
            rofl = download_and_wait(sys.argv[1])
            print(f"\n✅ Rofl listo en: {rofl}")
        except FileNotFoundError as e:
            log.error("%s", e); sys.exit(2)
        except RuntimeError as e:
            log.error("%s", e); sys.exit(3)
        except TimeoutError as e:
            log.error("%s", e); sys.exit(4)