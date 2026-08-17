"""
matter-bridge
=============
Remplace les API codées à la main (/api/alpstuga, /api/grillplats) par une
seule API générique qui lit TOUS les appareils connus de ton serveur Matter
(python-matter-server, celui de ta capture "Node 8") et qui:

  1. Expose /api/devices           -> liste de tous les appareils + valeurs
  2. Expose /api/devices/{node_id} -> détail d'un appareil
  3. Expose /api/registry          -> registre persistant node_id <-> noms
  4. Expose /api/health            -> état de connexion / dernier refresh
  5. Régénère automatiquement un fichier services.yaml pour Homepage,
     donc plus besoin d'éditer la config Homepage à la main quand tu
     commissionnes un nouveau capteur.

Renommage manuel d'un appareil (sans toucher au code / sans redémarrer) :
  PUT /api/registry/{node_id}/name   body: {"name": "Capteur CO2 Bureau"}

NOTE: les noms exacts des attributs (measured_value, etc.) dépendent de la
version de `python-matter-server`. Si un device type n'est pas reconnu,
regarde `node.endpoints[x].clusters` pour adapter le mapping ci-dessous.
"""

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from threading import Lock

import aiohttp
import yaml
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from matter_server.client import MatterClient
from matter_server.client.exceptions import CannotConnect

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("matter-bridge")

MATTER_SERVER_URL = os.environ.get("MATTER_SERVER_URL", "ws://localhost:5580/ws")
OUTPUT_DIR = os.environ.get("HOMEPAGE_OUTPUT_DIR", "/output")
REGISTRY_PATH = os.environ.get("DEVICE_REGISTRY_FILE", "/config/device_registry.json")

_raw_poll = os.environ.get("POLL_INTERVAL_SECONDS", "30")
try:
    POLL_INTERVAL = int(_raw_poll)
except ValueError:
    log.warning("POLL_INTERVAL_SECONDS=%r invalide, retombe sur 30s", _raw_poll)
    POLL_INTERVAL = 30

STALE_AFTER_SECONDS = int(os.environ.get("STALE_AFTER_SECONDS", "3600"))

# URL par laquelle Homepage doit joindre CE bridge. En network_mode: host,
# le nom de service Docker "matter-bridge" n'est plus résolvable : il faut
# l'IP/host réel (ex: http://192.168.1.100:8182).
BRIDGE_PUBLIC_URL = os.environ.get("BRIDGE_PUBLIC_URL", "http://localhost:8182")

# cache en mémoire, rafraîchi par refresh_loop() depuis les données déjà
# en mémoire côté client (maintenues à jour par la connexion permanente)
_devices_cache: dict[int, dict] = {}
_client: MatterClient | None = None
_registry_lock = Lock()
_last_refresh_ok: str | None = None
_background_tasks: list[asyncio.Task] = []


# --- Mapping "cluster Matter" -> "champ lisible" -------------------------
# Ajoute ici de nouveaux types de capteurs au fur et à mesure de tes achats.
CLUSTER_READERS = {
    "TemperatureMeasurement": lambda c: {"temp_c": round(c.measuredValue / 100, 2)},
    "RelativeHumidityMeasurement": lambda c: {"humidite_pct": round(c.measuredValue / 100, 2)},
    "CarbonDioxideConcentrationMeasurement": lambda c: {"co2_ppm": round(c.measuredValue)},
    "Pm25ConcentrationMeasurement": lambda c: {"pm25": round(c.measuredValue, 1)},
    "ElectricalPowerMeasurement": lambda c: {"puissance_w": round(getattr(c, "activePower", 0) / 1000, 2)},
    "ElectricalEnergyMeasurement": lambda c: {
        "energie_kwh": round(getattr(c.cumulativeEnergyImported, "energy", 0) / 1_000_000, 3)
        if getattr(c, "cumulativeEnergyImported", None) else None
    },
    "OnOff": lambda c: {"allume": "ON" if c.onOff else "OFF"},
}

# --- Libellés affichés sur Homepage (indépendants du nom de champ interne)
# Modifie ici pour changer ce qui s'affiche sous chaque valeur.
FIELD_LABELS = {
    "temp_c": "temperature",
    "humidite_pct": "Humidity",
    "co2_ppm": "CO2",
    "pm25": "PM2.5",
    "allume": "Status",
    "puissance_w": "W",
    "energie_kwh": "kW/h",
}

# --- Icônes Homepage par type de mesure présent sur le device ------------
DEVICE_TYPE_ICONS = {
    "puissance_w": "mdi-power-plug",
    "energie_kwh": "mdi-power-plug",
    "co2_ppm": "mdi-molecule-co2",
    "pm25": "mdi-air-filter",
    "humidite_pct": "mdi-water-percent",
    "temp_c": "mdi-thermometer",
}
DEFAULT_ICON = "mdi-chip"


def pick_icon(fields: list[str]) -> str:
    for field in fields:
        if field in DEVICE_TYPE_ICONS:
            return DEVICE_TYPE_ICONS[field]
    return DEFAULT_ICON


class RenameRequest(BaseModel):
    name: str


# --- Registre persistant node_id -> noms ---------------------------------
# Contrairement à un simple dict codé en dur dans le script, ce registre :
#   - s'auto-remplit avec le nom détecté (nodeLabel/productName) dès qu'un
#     nouveau capteur est commissionné, sans intervention manuelle
#   - garde ce nom "détecté" même après avoir fixé un nom personnalisé,
#     pratique pour retrouver le modèle physique derrière un renommage
#   - survit aux redémarrages du container car monté en volume
#   - peut être modifié soit via l'API (PUT /api/registry/{id}/name),
#     soit à la main dans le fichier JSON (rechargé à chaque cycle)

def load_registry() -> dict[str, dict]:
    if not os.path.exists(REGISTRY_PATH):
        return {}
    try:
        with open(REGISTRY_PATH) as f:
            return json.load(f)
    except Exception:
        log.exception("device_registry.json invalide -> registre vide ce cycle")
        return {}


def save_registry(registry: dict[str, dict]):
    os.makedirs(os.path.dirname(REGISTRY_PATH), exist_ok=True)
    tmp_path = REGISTRY_PATH + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(registry, f, indent=2, ensure_ascii=False, sort_keys=True)
    os.replace(tmp_path, REGISTRY_PATH)


def upsert_registry_entry(registry: dict[str, dict], node_id: int, detected_name: str, device_type: str | None):
    key = str(node_id)
    now = datetime.now(timezone.utc).isoformat()
    if key not in registry:
        registry[key] = {
            "node_id": node_id,
            "detected_name": detected_name,
            "custom_name": None,
            "device_type": device_type,
            "first_seen": now,
            "last_seen": now,
        }
    else:
        registry[key]["detected_name"] = detected_name
        registry[key]["device_type"] = device_type
        registry[key]["last_seen"] = now
    return registry


def read_node(node, previous_values: dict | None = None) -> dict:
    """Extrait toutes les valeurs lisibles d'un node Matter.

    `previous_values` : dernières valeurs connues de ce node (cycle
    précédent). Si une lecture échoue ponctuellement ce cycle-ci (capteur
    pas encore réveillé, valeur pas encore poussée...), on garde l'ancienne
    valeur au lieu de faire disparaître le champ du dashboard.
    """
    values: dict = dict(previous_values or {})
    detected_name = None
    device_type = None
    for endpoint in node.endpoints.values():
        for cluster in endpoint.clusters.values():
            cluster_name = type(cluster).__name__
            if cluster_name == "BasicInformation":
                detected_name = getattr(cluster, "nodeLabel", None) or getattr(cluster, "productName", None)
            reader = CLUSTER_READERS.get(cluster_name)
            if reader:
                if device_type is None:
                    device_type = cluster_name
                try:
                    result = reader(cluster)
                    # on n'écrase une valeur existante que si la nouvelle
                    # lecture est valide (pas None) -> évite le clignotement
                    for k, v in result.items():
                        if v is not None:
                            values[k] = v
                except Exception as exc:  # valeur pas encore lue / cluster vide
                    log.debug("Cluster %s illisible sur node %s: %s", cluster_name, node.node_id, exc)
            elif cluster_name not in ("BasicInformation", "Descriptor", "PowerSource"):
                # cluster présent mais pas encore mappé dans CLUSTER_READERS
                # -> visible dans les logs pour savoir quoi ajouter
                log.info("Cluster non mappé sur node %s: %s (ajoute-le à CLUSTER_READERS si utile)",
                         node.node_id, cluster_name)

    # "allume" (OnOff) n'a de sens que sur un vrai appareil pilotable
    # (prise, ampoule...) -> on ne le garde que s'il y a aussi une mesure
    # de puissance/énergie à côté. Sinon c'est un cluster accessoire (ex:
    # LED d'un capteur ALPSTUGA) qui n'apporte rien à afficher.
    if "allume" in values and "puissance_w" not in values and "energie_kwh" not in values:
        del values["allume"]

    return {
        "node_id": node.node_id,
        "detected_name": detected_name,
        "device_type": device_type,
        "values": values,
    }


async def matter_connection_loop():
    """Maintient une connexion PERMANENTE au Matter server.

    `start_listening()` est une boucle qui tourne indéfiniment pour
    recevoir les évènements en temps réel (nouvelle valeur de capteur,
    nouvel appareil commissionné, etc.) — elle ne "revient" jamais tant que
    la connexion est active. C'est normal : c'est elle qui garde le cache
    interne du client à jour. On la relance juste si la connexion tombe.
    """
    global _client
    while True:
        session = aiohttp.ClientSession()
        try:
            client = MatterClient(MATTER_SERVER_URL, session)
            await client.connect()
            _client = client
            init_ready = asyncio.Event()
            listen_task = asyncio.create_task(client.start_listening(init_ready))
            await init_ready.wait()
            log.info("Connecté au Matter server, écoute des évènements en cours")
            await listen_task  # bloque tant que la connexion Matter reste ouverte
        except CannotConnect:
            log.warning("Impossible de joindre le Matter server (%s)", MATTER_SERVER_URL)
        except Exception:
            log.exception("Connexion Matter perdue, nouvelle tentative dans 10s")
        finally:
            _client = None
            await session.close()
        await asyncio.sleep(10)


async def refresh_loop():
    """Toutes les POLL_INTERVAL secondes : relit le cache déjà tenu à jour
    par matter_connection_loop() (pas de reconnexion ici), fusionne avec le
    registre de noms persistant, et régénère services.yaml pour Homepage."""
    global _last_refresh_ok
    while True:
        client = _client
        if client is not None:
            try:
                nodes = client.get_nodes()
                new_cache = {}
                now_iso = datetime.now(timezone.utc).isoformat()
                with _registry_lock:
                    registry = load_registry()
                    for node in nodes:
                        previous = _devices_cache.get(node.node_id, {}).get("values")
                        info = read_node(node, previous)
                        detected = info["detected_name"] or f"Node {node.node_id}"
                        registry = upsert_registry_entry(registry, node.node_id, detected, info["device_type"])
                        entry = registry[str(node.node_id)]
                        display_name = entry.get("custom_name") or entry["detected_name"]
                        new_cache[node.node_id] = {
                            "node_id": node.node_id,
                            "name": display_name,
                            "values": info["values"],
                            "last_seen": now_iso,
                        }
                    save_registry(registry)
                _devices_cache.clear()
                _devices_cache.update(new_cache)
                write_homepage_config(new_cache)
                _last_refresh_ok = now_iso
                log.info("Rafraîchi %d appareils Matter", len(new_cache))
            except Exception:
                log.exception("Erreur pendant le rafraîchissement")
        else:
            log.debug("Pas encore connecté au Matter server, on attend")
        await asyncio.sleep(POLL_INTERVAL)


def write_homepage_config(devices: dict[int, dict]):
    """Génère services.yaml pour Homepage : fusionne les appareils Matter
    auto-détectés avec un éventuel fichier services.manual.yaml (pour tout
    ce que tu ajoutes à la main : NAS, Proxmox, etc.), afin de ne jamais
    écraser tes services non-Matter."""
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    group = []
    for node_id, dev in devices.items():
        fields = list(dev["values"].keys())
        if not fields:
            continue
        entry = {
            dev["name"]: {
                "icon": pick_icon(fields),
                "widget": {
                    "type": "customapi",
                    "url": f"{BRIDGE_PUBLIC_URL}/api/devices/{node_id}",
                    "method": "GET",
                    "mappings": [
                        {"field": f, "label": FIELD_LABELS.get(f, f), "format": "text"} for f in fields
                    ],
                },
            }
        }
        group.append(entry)

    services = [{"Capteurs Matter (auto)": group}]

    manual_path = os.path.join(OUTPUT_DIR, "services.manual.yaml")
    if os.path.exists(manual_path):
        try:
            with open(manual_path) as f:
                manual_services = yaml.safe_load(f) or []
            if not isinstance(manual_services, list):
                raise ValueError("services.manual.yaml doit être une liste de groupes")
            services = manual_services + services
        except Exception:
            log.exception(
                "services.manual.yaml invalide -> ignoré ce cycle (corrige-le, "
                "les capteurs Matter restent affichés en attendant)"
            )

    final_path = os.path.join(OUTPUT_DIR, "services.yaml")
    tmp_path = final_path + ".tmp"
    with open(tmp_path, "w") as f:
        yaml.safe_dump(services, f, allow_unicode=True, sort_keys=False, default_flow_style=False)
    os.replace(tmp_path, final_path)  # opération atomique -> jamais de fichier à moitié écrit


@asynccontextmanager
async def lifespan(app: FastAPI):
    conn_task = asyncio.create_task(matter_connection_loop())
    refresh_task = asyncio.create_task(refresh_loop())
    _background_tasks.extend([conn_task, refresh_task])
    yield
    for task in _background_tasks:
        task.cancel()
    await asyncio.gather(*_background_tasks, return_exceptions=True)


app = FastAPI(title="Matter Bridge", lifespan=lifespan)


@app.get("/api/devices")
async def list_devices():
    return list(_devices_cache.values())


@app.get("/api/devices/{node_id}")
async def get_device(node_id: int):
    dev = _devices_cache.get(node_id)
    if not dev:
        raise HTTPException(status_code=404, detail="Appareil inconnu")
    return dev["values"]


@app.get("/api/registry")
async def get_registry():
    """Liste tous les node_id déjà vus, avec leur nom détecté et leur nom
    personnalisé (si défini). Pratique pour retrouver le node_id d'un
    capteur physique avant de le renommer."""
    with _registry_lock:
        return load_registry()


@app.put("/api/registry/{node_id}/name")
async def set_custom_name(node_id: int, req: RenameRequest):
    """Fixe un nom personnalisé pour ce node_id, appliqué immédiatement
    (cache + prochaine génération de services.yaml) sans redémarrage."""
    with _registry_lock:
        registry = load_registry()
        key = str(node_id)
        if key not in registry:
            raise HTTPException(status_code=404, detail="Node jamais vu, attends le prochain cycle de refresh")
        registry[key]["custom_name"] = req.name
        save_registry(registry)
    if node_id in _devices_cache:
        _devices_cache[node_id]["name"] = req.name
    return registry[key]


@app.get("/api/health")
async def health():
    """État de connexion Matter, nombre d'appareils en cache, et
    horodatage du dernier refresh réussi -> utile pour ton monitoring."""
    now = datetime.now(timezone.utc)
    stale = False
    if _last_refresh_ok is not None:
        last = datetime.fromisoformat(_last_refresh_ok)
        stale = (now - last).total_seconds() > STALE_AFTER_SECONDS
    return {
        "matter_connected": _client is not None,
        "devices_count": len(_devices_cache),
        "last_refresh_ok": _last_refresh_ok,
        "stale": stale,
    }
