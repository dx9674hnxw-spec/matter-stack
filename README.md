# Matter Bridge pour Homepage

Bridge léger en Python permettant de connecter un serveur [Matter](https://www.home-assistant.io/integrations/matter_server/) à [Homepage](https://gethomepage.dev/).

Le projet détecte automatiquement les appareils Matter connus de `python-matter-server`, expose leurs valeurs via une API REST FastAPI et génère dynamiquement la configuration `services.yaml` de Homepage. L'ajout d'un nouveau capteur Matter ne nécessite donc plus de modifier manuellement le dashboard.

## Fonctionnalités

- Connexion persistante à `python-matter-server` via WebSocket.
- Découverte automatique des nœuds Matter.
- API REST pour lister les appareils et consulter leurs valeurs.
- Cache en mémoire afin de conserver la dernière valeur connue en cas de lecture momentanément indisponible.
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
git clone https://github.com/<utilisateur>/<depot>.git
cd <depot>
mkdir -p bridge
cp app.py requirements.txt Dockerfile bridge/
```

Adaptez ensuite les paramètres du fichier `docker-compose.yml`, notamment :

```yaml
MATTER_SERVER_URL: "ws://localhost:5580/ws"
HOMEPAGE_OUTPUT_DIR: "/homepage-config"
BRIDGE_PUBLIC_URL: "http://192.168.1.100:8182"
POLL_INTERVAL_SECONDS: "30"
```

Remplacez `192.168.1.100` par l'adresse IP réellement accessible depuis le conteneur Homepage ou depuis l'hôte qui l'exécute.

Démarrez les services :

```bash
docker compose up -d --build
```

Consultez les journaux :

```bash
docker compose logs -f matter-bridge
```

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
    }
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

## Ajouter un type de mesure

Les clusters Matter sont associés à des lecteurs dans `CLUSTER_READERS` du fichier `app.py` :

```python
CLUSTER_READERS = {
    "TemperatureMeasurement": lambda c: {
        "temp_c": round(c.measuredValue / 100, 2)
    },
}
```

Si un cluster n'est pas reconnu, son nom apparaît dans les logs. Ajoutez ensuite un lecteur adapté à la version de `python-matter-server` utilisée.

Les libellés affichés dans Homepage sont définis séparément dans `FIELD_LABELS`, et les noms personnalisés des appareils dans `DEVICE_NAME_OVERRIDES`.

## Variables d'environnement

| Variable | Valeur par défaut | Description |
|---|---:|---|
| `MATTER_SERVER_URL` | `ws://localhost:5580/ws` | URL WebSocket du serveur Matter |
| `HOMEPAGE_OUTPUT_DIR` | `/output` | Répertoire de génération de `services.yaml` |
| `BRIDGE_PUBLIC_URL` | `http://localhost:8182` | URL utilisée par Homepage pour joindre le bridge |
| `POLL_INTERVAL_SECONDS` | `30` | Intervalle de rafraîchissement du cache et du fichier Homepage |

## Dépannage

### Le bridge ne se connecte pas au serveur Matter

Vérifiez que `python-matter-server` écoute bien sur le port `5580` et que les deux services utilisent le même mode réseau. Consultez les logs avec `docker compose logs matter-bridge`.

### Homepage n'affiche pas les valeurs

Vérifiez `BRIDGE_PUBLIC_URL`. Avec `network_mode: host`, le nom DNS Docker `matter-bridge` n'est généralement pas utilisable depuis Homepage ; utilisez l'adresse IP ou le nom DNS réellement accessible.

### Un appareil n'affiche aucune valeur

Vérifiez les clusters présents dans les logs. Le cluster peut nécessiter une entrée dans `CLUSTER_READERS`, ou le capteur n'a peut-être pas encore publié de valeur.

### Le fichier Homepage est invalide

Vérifiez la syntaxe de `services.manual.yaml`. En cas d'erreur, le bridge l'ignore temporairement et continue de générer les appareils Matter.

## Sécurité

L'API n'intègre actuellement aucune authentification. Évitez de l'exposer directement sur Internet. Utilisez un réseau local de confiance, un reverse proxy avec authentification ou une politique d'accès réseau appropriée.

## Licence

MIT License

Copyright (c) 2025 https://github.com/dx9674hnxw-spec

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
