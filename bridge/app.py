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

# cache en mémoire, rafraîchi par la boucle de fond
_devices_cache: dict[int, dict] = {}


# --- Mapping "cluster Matter" -> "champ lisible" -------------------------
# Ajoute ici de nouveaux types de capteurs au fur et à mesure de tes achats.
CLUSTER_READERS = {
    "TemperatureMeasurement": lambda c: {"temp_c": round(c.measuredValue / 100, 2)},
    "RelativeHumidity": lambda c: {"humidite_pct": round(c.measuredValue / 100, 2)},
    "CarbonDioxideConcentrationMeasurement": lambda c: {"co2_ppm": round(c.measuredValue)},
    "Pm25ConcentrationMeasurement": lambda c: {"pm25": round(c.measuredValue, 1)},
    "ElectricalPowerMeasurement": lambda c: {"puissance_w": round(getattr(c, "activePower", 0) / 1000, 2)},
    "ElectricalEnergyMeasurement": lambda c: {
        "energie_kwh": round(getattr(c.cumulativeEnergyImported, "energy", 0) / 1_000_000, 3)
        if getattr(c, "cumulativeEnergyImported", None) else None
    },
    "OnOff": lambda c: {"allume": bool(c.onOff)},
}


def read_node(node) -> dict:
    """Extrait toutes les valeurs lisibles d'un node Matter."""
    values: dict = {}
    name = None
    for endpoint in node.endpoints.values():
        for cluster in endpoint.clusters.values():
            cluster_name = type(cluster).__name__
            if cluster_name == "BasicInformation":
                name = getattr(cluster, "nodeLabel", None) or getattr(cluster, "productName", None)
            reader = CLUSTER_READERS.get(cluster_name)
            if reader:
                try:
                    values.update(reader(cluster))
                except Exception as exc:  # valeur pas encore lue / cluster vide
                    log.debug("Cluster %s illisible sur node %s: %s", cluster_name, node.node_id, exc)
    return {
        "node_id": node.node_id,
        "name": name or f"Node {node.node_id}",
        "values": values,
    }


async def refresh_loop():
    while True:
        try:
            async with aiohttp.ClientSession() as session:
                async with MatterClient(MATTER_SERVER_URL, session) as client:
                    await client.connect()
                    await client.start_listening()
                    nodes = client.get_nodes()
                    new_cache = {}
                    for node in nodes:
                        new_cache[node.node_id] = read_node(node)
                    _devices_cache.clear()
                    _devices_cache.update(new_cache)
                    write_homepage_config(new_cache)
                    log.info("Rafraîchi %d appareils Matter", len(new_cache))
        except CannotConnect:
            log.warning("Impossible de joindre le Matter server (%s)", MATTER_SERVER_URL)
        except Exception:
            log.exception("Erreur pendant le rafraîchissement")
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
