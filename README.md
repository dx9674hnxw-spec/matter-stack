# Matter Bridge pour Homepage

Bridge léger en Python permettant de connecter un serveur [Matter](https://www.home-assistant.io/integrations/matter_server/) à [Homepage](https://gethomepage.dev/).

Le projet détecte automatiquement les appareils Matter connus de `python-matter-server`, expose leurs valeurs via une API REST FastAPI et génère dynamiquement la configuration `services.yaml` de Homepage. L'ajout d'un nouveau capteur Matter ne nécessite donc plus de modifier manuellement le dashboard.

## Fonctionnalités

- Connexion persistante à `python-matter-server` via WebSocket.
- Découverte automatique des nœuds Matter.
- API REST pour lister les appareils et consulter leurs valeurs.
- Registre persistant associant chaque `node_id` à son nom Matter détecté et à un nom personnalisé.
- Cache en mémoire afin de conserver la dernière valeur connue en cas de lecture momentanément indisponible.
- Horodatage `last_seen` pour connaître la fraîcheur des données.
- Endpoint `/api/health` pour vérifier l'état de la connexion et du rafraîchissement.
- Icônes Homepage choisies automatiquement selon les mesures disponibles.
- Support des mesures courantes :
  - température ;
  - humidité relative ;
  - CO₂ ;
  - particules PM2.5 ;
  - puissance électrique ;
  - énergie cumulée ;
  - état On/Off.
- Génération automatique de `services.yaml` pour Homepage.
- Conservation des services personnalisés via `services.manual.yaml`.
- Écriture atomique du fichier Homepage et du registre JSON.
- Exécution dans Docker avec redémarrage automatique.
- Journalisation des clusters Matter non encore pris en charge.

## Architecture

```text
┌──────────────────────┐      WebSocket       ┌────────────────────┐
│ python-matter-server │ ◄──────────────────► │    matter-bridge   │
│      port 5580       │                      │ FastAPI / Uvicorn  │
└──────────────────────┘                      │      port 8182      │
                                              └─────────┬──────────┘
                                                        │
                                      services.yaml     │ REST API
                                                        ▼
                                              ┌────────────────────┐
                                              │      Homepage       │
                                              └────────────────────┘
```

Les deux conteneurs utilisent `network_mode: host`. Cette configuration permet au bridge de joindre le serveur Matter via `localhost`, tandis que Homepage doit utiliser l'adresse IP réelle du serveur pour accéder à l'API du bridge.

## Prérequis

- Docker et Docker Compose.
- Un serveur `python-matter-server` fonctionnel.
- Un répertoire de configuration Homepage, par exemple `/srv/homepage`.
- Un hôte Linux disposant de l'accès réseau au serveur Matter.

## Installation

```bash
git clone https://github.com/dx9674hnxw-spec/matter-stack.git
cd matter-stack
```

Le dossier `bridge/` contient déjà `app.py`, `requirements.txt` et `Dockerfile`.

Créez le répertoire persistant du registre sur l'hôte :

```bash
sudo mkdir -p /srv/matter-bridge/config
sudo chown -R "$USER":"$USER" /srv/matter-bridge
```

Adaptez ensuite les paramètres du fichier `docker-compose.yml` :

```yaml
services:
  matter-bridge:
    environment:
      MATTER_SERVER_URL: "ws://localhost:5580/ws"
      HOMEPAGE_OUTPUT_DIR: "/homepage-config"
      BRIDGE_PUBLIC_URL: "http://192.168.1.100:8182"
      POLL_INTERVAL_SECONDS: "30"
      DEVICE_REGISTRY_FILE: "/config/device_registry.json"
      STALE_AFTER_SECONDS: "3600"
    volumes:
      - /srv/homepage:/homepage-config
      - /srv/matter-bridge/config:/config
```

Remplacez `192.168.1.100` par l'adresse IP réellement accessible depuis Homepage ou depuis l'hôte qui l'exécute.

Démarrez les services :

```bash
docker compose up -d --build
```

Consultez les journaux :

```bash
docker compose logs -f matter-bridge
```

Vérifiez l'état du bridge :

```bash
curl -s http://127.0.0.1:8182/api/health | jq
```

## Registre des appareils

Le registre actif est stocké sur l'hôte dans :

```text
/srv/matter-bridge/config/device_registry.json
```

Le fichier est monté dans le conteneur à l'emplacement suivant :

```text
/config/device_registry.json
```

Le registre est créé automatiquement dès que le bridge détecte un appareil. Il conserve notamment :

- `node_id` : identifiant Matter stable utilisé par l'API ;
- `detected_name` : nom remonté par le cluster `BasicInformation` ;
- `custom_name` : nom manuel affiché dans Homepage ;
- `device_type` : premier type de cluster reconnu ;
- `first_seen` et `last_seen` : dates de détection.

Exemple :

```json
{
  "8": {
    "node_id": 8,
    "detected_name": "TIMMERFLOTTE temp/hmd sensor",
    "custom_name": "TIMMERFLOTTE Salon",
    "device_type": "TemperatureMeasurement",
    "first_seen": "2026-08-17T20:28:50+00:00",
    "last_seen": "2026-08-17T20:30:46+00:00"
  }
}
```

### Versionner le registre

Le registre actif contient de l'état runtime (`last_seen`, dates de détection). Il est donc recommandé de ne pas le versionner directement dans Git. Conservez plutôt un modèle dans le dépôt :

```text
config/device_registry.example.json
```

Ajoutez le registre actif au `.gitignore` :

```gitignore
config/device_registry.json
config/device_registry.json.backup
```

Exemple de fichier modèle :

```json
{
  "3": {
    "node_id": 3,
    "detected_name": "ALPSTUGA air quality monitor",
    "custom_name": "Qualité air Salon",
    "device_type": "AirQuality",
    "first_seen": null,
    "last_seen": null
  },
  "4": {
    "node_id": 4,
    "detected_name": "GRILLPLATS Plug",
    "custom_name": "Prise Grill Terrasse",
    "device_type": "Plug",
    "first_seen": null,
    "last_seen": null
  },
  "8": {
    "node_id": 8,
    "detected_name": "TIMMERFLOTTE temp/hmd sensor",
    "custom_name": "TIMMERFLOTTE Salon",
    "device_type": "TemperatureMeasurement",
    "first_seen": null,
    "last_seen": null
  }
}
```

Le modèle est versionné, tandis que `/srv/matter-bridge/config/device_registry.json` reste la source active sur le serveur.

## API

### Lister les appareils

```http
GET /api/devices
```

Exemple de réponse :

```json
[
  {
    "node_id": 8,
    "name": "TIMMERFLOTTE Salon",
    "values": {
      "temp_c": 21.5,
      "humidite_pct": 48.2
    },
    "last_seen": "2026-08-17T20:30:46+00:00"
  }
]
```

### Consulter un appareil

```http
GET /api/devices/{node_id}
```

Exemple :

```bash
curl http://127.0.0.1:8182/api/devices/8
```

La réponse contient uniquement les valeurs de l'appareil, ce qui facilite leur utilisation dans les widgets `customapi` de Homepage.

### Consulter le registre

```http
GET /api/registry
```

Exemple :

```bash
curl -s http://127.0.0.1:8182/api/registry | jq
```

Cette route permet de retrouver le `node_id` associé à un capteur physique ainsi que son nom détecté et son nom personnalisé.

### Renommer un appareil

```http
PUT /api/registry/{node_id}/name
```

Exemple :

```bash
curl -X PUT http://127.0.0.1:8182/api/registry/8/name \
  -H "Content-Type: application/json" \
  -d '{"name": "TIMMERFLOTTE Salon"}'
```

Le nom est écrit dans le registre, appliqué au cache en mémoire et repris dans le prochain `services.yaml`. Aucun redémarrage du conteneur n'est nécessaire.

Il est également possible de modifier directement `custom_name` dans le fichier JSON :

```bash
sudo nano /srv/matter-bridge/config/device_registry.json
sudo jq empty /srv/matter-bridge/config/device_registry.json
```

Utilisez `null` pour revenir au nom automatiquement détecté :

```json
"custom_name": null
```

### Vérifier l'état du bridge

```http
GET /api/health
```

Exemple :

```json
{
  "matter_connected": true,
  "devices_count": 3,
  "last_refresh_ok": "2026-08-17T20:30:46+00:00",
  "stale": false
}
```

`stale` passe à `true` si le dernier rafraîchissement réussi date de plus de `STALE_AFTER_SECONDS`.

## Configuration Homepage

Le bridge écrit automatiquement le fichier :

```text
/srv/homepage/services.yaml
```

Les appareils détectés sont regroupés sous :

```yaml
Capteurs Matter (auto):
```

Pour conserver vos services non-Matter, créez un fichier `services.manual.yaml` dans le même répertoire :

```yaml
- Infrastructure:
    - Proxmox:
        href: https://proxmox.example.local
        description: Hyperviseur
```

Le bridge fusionne ce fichier avec les services Matter générés automatiquement. Le fichier `services.yaml` est remplacé de manière atomique pour éviter qu'Homepage ne lise une configuration partiellement écrite.

Les icônes sont choisies automatiquement selon les mesures disponibles : température, humidité, CO₂, PM2.5, puissance ou énergie.

## Ajouter un type de mesure

Les clusters Matter sont associés à des lecteurs dans `CLUSTER_READERS` du fichier `bridge/app.py` :

```python
CLUSTER_READERS = {
    "TemperatureMeasurement": lambda c: {
        "temp_c": round(c.measuredValue / 100, 2)
    },
}
```

Si un cluster n'est pas reconnu, son nom apparaît dans les logs. Ajoutez ensuite un lecteur adapté à la version de `python-matter-server` utilisée.

Les libellés affichés dans Homepage sont définis dans `FIELD_LABELS`, et les icônes dans `DEVICE_TYPE_ICONS`.

## Variables d'environnement

| Variable | Valeur par défaut | Description |
|---|---:|---|
| `MATTER_SERVER_URL` | `ws://localhost:5580/ws` | URL WebSocket du serveur Matter |
| `HOMEPAGE_OUTPUT_DIR` | `/output` | Répertoire de génération de `services.yaml` |
| `BRIDGE_PUBLIC_URL` | `http://localhost:8182` | URL utilisée par Homepage pour joindre le bridge |
| `POLL_INTERVAL_SECONDS` | `30` | Intervalle de rafraîchissement du cache et de `services.yaml` |
| `DEVICE_REGISTRY_FILE` | `/config/device_registry.json` | Emplacement du registre persistant |
| `STALE_AFTER_SECONDS` | `3600` | Délai avant de considérer le dernier refresh comme obsolète |

## Dépannage

### Le bridge ne se connecte pas au serveur Matter

Vérifiez que `python-matter-server` écoute sur le port `5580` et que les deux services utilisent le même mode réseau :

```bash
docker compose logs -f matter-bridge
docker compose logs -f matter-server
curl -s http://127.0.0.1:8182/api/health | jq
```

### Homepage n'affiche pas les valeurs

Vérifiez `BRIDGE_PUBLIC_URL`. Avec `network_mode: host`, le nom DNS Docker `matter-bridge` n'est généralement pas utilisable depuis Homepage ; utilisez l'adresse IP ou le nom DNS réellement accessible.

### Un appareil n'affiche aucune valeur

Vérifiez les clusters présents dans les logs. Le cluster peut nécessiter une entrée dans `CLUSTER_READERS`, ou le capteur n'a peut-être pas encore publié de valeur.

### Le nom personnalisé ne s'affiche pas

Vérifiez le registre :

```bash
curl -s http://127.0.0.1:8182/api/registry | jq
```

Le champ `custom_name` doit contenir une chaîne et non `null`. Après modification, attendez au maximum `POLL_INTERVAL_SECONDS`, puis rechargez Homepage.

### Le fichier Homepage est invalide

Vérifiez la syntaxe de `services.manual.yaml`. En cas d'erreur, le bridge l'ignore temporairement et continue de générer les appareils Matter.

### Erreur de permissions sur `/srv/matter-bridge/config`

Le répertoire hôte doit exister et être accessible au processus Docker :

```bash
sudo mkdir -p /srv/matter-bridge/config
sudo chown -R "$USER":"$USER" /srv/matter-bridge
```

Vérifiez le montage utilisé par le conteneur :

```bash
docker inspect matter-bridge \
  --format '{{range .Mounts}}{{println .Source "->" .Destination}}{{end}}'
```

### Conflit avec un autre conteneur

Lancez Compose depuis le répertoire du projet Matter :

```bash
cd ~/matter-stack
docker compose config
docker compose up -d --build
```

Le fichier Matter doit uniquement déclarer `matter-bridge` et `matter-server`. Un conteneur comme `outline-redis` appartient à un autre stack et ne doit pas être inclus dans ce compose.

## Sécurité

L'API n'intègre actuellement aucune authentification, y compris pour le renommage des appareils. Évitez de l'exposer directement sur Internet. Utilisez un réseau local de confiance, un reverse proxy avec authentification ou une politique d'accès réseau appropriée.

## Licence

MIT License

Copyright (c) 2025 [https://github.com/dx9674hnxw-spec](https://github.com/dx9674hnxw-spec)

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
