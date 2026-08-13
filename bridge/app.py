"""
matter-bridge
=============
Remplace les API codées à la main (/api/alpstuga, /api/grillplats) par une
seule API générique qui lit TOUS les appareils connus de ton serveur Matter
(python-matter-server, celui de ta capture "Node 8") et qui:

  1. Expose /api/devices           -> liste de tous les appareils + valeurs
  2. Expose /api/devices/{node_id} -> détail d'un appareil
  3. Régénère automatiquement un fichier services.yaml pour Homepage,
     donc plus besoin d'éditer la config Homepage à la main quand tu
     commissionnes un nouveau capteur.

NOTE: les noms exacts des attributs (measured_value, etc.) dépendent de la
version de `python-matter-server`. Si un device type n'est pas reconnu,
regarde `node.endpoints[x].clusters` pour adapter le mapping ci-dessous.
"""

import asyncio
import logging
import os
import aiohttp
import yaml
from fastapi import FastAPI, HTTPException
from matter_server.client import MatterClient
from matter_server.client.exceptions import CannotConnect

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("matter-bridge")

MATTER_SERVER_URL = os.environ.get("MATTER_SERVER_URL", "ws://localhost:5580/ws")
OUTPUT_DIR = os.environ.get("HOMEPAGE_OUTPUT_DIR", "/output")
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL_SECONDS", "30"))
# URL par laquelle Homepage doit joindre CE bridge. En network_mode: host,
# le nom de service Docker "matter-bridge" n'est plus résolvable : il faut
# l'IP/host réel (ex: http://192.168.1.100:8182).
BRIDGE_PUBLIC_URL = os.environ.get("BRIDGE_PUBLIC_URL", "http://localhost:8182")

app = FastAPI(title="Matter Bridge")

# cache en mémoire, rafraîchi par refresh_loop() depuis les données déjà
# en mémoire côté client (maintenues à jour par la connexion permanente)
_devices_cache: dict[int, dict] = {}
_client: MatterClient | None = None


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
    "OnOff": lambda c: {"allume": "true" if c.onOff else "false"},
}


def read_node(node, previous_values: dict | None = None) -> dict:
    """Extrait toutes les valeurs lisibles d'un node Matter.

    `previous_values` : dernières valeurs connues de ce node (cycle
    précédent). Si une lecture échoue ponctuellement ce cycle-ci (capteur
    pas encore réveillé, valeur pas encore poussée...), on garde l'ancienne
    valeur au lieu de faire disparaître le champ du dashboard.
    """
    values: dict = dict(previous_values or {})
    name = None
    for endpoint in node.endpoints.values():
        for cluster in endpoint.clusters.values():
            cluster_name = type(cluster).__name__
            if cluster_name == "BasicInformation":
                name = getattr(cluster, "nodeLabel", None) or getattr(cluster, "productName", None)
            reader = CLUSTER_READERS.get(cluster_name)
            if reader:
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
        "name": name or f"Node {node.node_id}",
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
    par matter_connection_loop() (pas de reconnexion ici) et régénère
    services.yaml pour Homepage."""
    while True:
        if _client is not None:
            try:
                nodes = _client.get_nodes()
                new_cache = {}
                for node in nodes:
                    previous = _devices_cache.get(node.node_id, {}).get("values")
                    new_cache[node.node_id] = read_node(node, previous)
                _devices_cache.clear()
                _devices_cache.update(new_cache)
                write_homepage_config(new_cache)
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
                "icon": "mdi-thermometer",
                "widget": {
                    "type": "customapi",
                    "url": f"{BRIDGE_PUBLIC_URL}/api/devices/{node_id}",
                    "method": "GET",
                    "mappings": [
                        {"field": f, "label": f, "format": "text"} for f in fields
                    ],
                },
            }
        }
        group.append(entry)

    services = [{"Capteurs Matter (auto)": group}]

    manual_path = os.path.join(OUTPUT_DIR, "services.manual.yaml")
    if os.path.exists(manual_path):
        with open(manual_path) as f:
            manual_services = yaml.safe_load(f) or []
        services = manual_services + services

    with open(os.path.join(OUTPUT_DIR, "services.yaml"), "w") as f:
        yaml.safe_dump(services, f, allow_unicode=True, sort_keys=False)


@app.on_event("startup")
async def startup():
    asyncio.create_task(matter_connection_loop())
    asyncio.create_task(refresh_loop())


@app.get("/api/devices")
async def list_devices():
    return list(_devices_cache.values())


@app.get("/api/devices/{node_id}")
async def get_device(node_id: int):
    dev = _devices_cache.get(node_id)
    if not dev:
        raise HTTPException(status_code=404, detail="Appareil inconnu")
    return dev["values"]
