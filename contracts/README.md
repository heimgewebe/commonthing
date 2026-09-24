## Domain-Contracts lokal validieren

commonThing nutzt JSON-Schemas, um die zentralen Domänenobjekte (`node`, `edge`, `conversation`, `message`)
konsistent zu halten. Zu jedem Schema existiert mindestens ein Beispiel unter `contracts/domain/examples/`.
CI prüft bei jedem Push sowohl die Schema-Syntax als auch die Beispieldateien – dieselbe Prüfung lässt sich
lokal ausführen.

### Voraussetzungen

- Node.js ≥ 20 (Vite & SvelteKit erfordern dies ohnehin)
- `ajv-cli` und `ajv-formats` global installiert:

  ```sh
  pnpm install -g ajv-cli ajv-formats
  ```

- Shell-Zugriff auf das Repo

### Ausführung

Die komplette Prüfung läuft über das interne Script:

```sh
just contracts-domain-check
```

oder ohne Just:

```sh
bash ./scripts/contracts-domain-check.sh
```

Das Script führt zwei Schritte aus:

1. **Schemas kompilieren**
   Alle Dateien unter `contracts/domain/*.schema.json` werden mit `ajv compile` gegen `ajv-formats` geprüft.

2. **Beispiele validieren**
   Jede Datei unter `contracts/domain/examples/*.example.json` wird automatisch dem passenden Schema
   zugeordnet und validiert.

Sind alle Checks erfolgreich, ist der Stand kompatibel zur CI-Validierung.

## Semantic-Search-Goldset validieren

Der Vertrag unter `contracts/search/relevance-goldset.schema.json` beschreibt synthetische, sichtbarkeitsgebundene Relevanzfälle für die geplante hybride Knotensuche. Das eingecheckte Beispiel wird ohne externe Python-Abhängigkeit geprüft:

```sh
python3 -m scripts.search.validate_relevance_goldset
```

Oder über Just:

```sh
just contracts-search-check
```

Der Validator prüft Schemaform, eindeutige Fall-IDs, sichtbare relevante Knoten, unsichtbare Ausschlüsse und das Verbot E-Mail-ähnlicher Testdaten. Er bewertet noch keine reale Suchqualität; Baselines und Modellkandidaten folgen in T002.

### Typische Fehler & Hinweise

- **„ajv: command not found“**
  → `ajv-cli` fehlt global. Installieren wie oben beschrieben.
- **„no schemas found“**
  → Ordnerstruktur prüfen (Pfad muss exakt `contracts/domain` lauten).
- **„strict mode violation“**
  → Das Schema enthält Felder, die nicht definiert oder verboten sind.
  Schema überarbeiten oder `additionalProperties` explizit setzen.

### Warum dieser Check?

Er verhindert Schema-Drift: commonThing ist ein eigenständiges Projekt, aber die Domain-Contracts sind eine
stabile, externe Schnittstelle. Durch lokale Validierung bleibt alles synchron zu CI und Dokumentation.

## Föderationsvertrag v1 prüfen

Die öffentliche Grenze zwischen unabhängig betriebenen commonThing-Zellen ist unter `contracts/federation/v1/` beschrieben. Sie umfasst die öffentliche Zellbeschreibung und signierte Objekt-Ereignisse für Knoten, Kanten und gemeinsame Räume.

Die statische Prüfung läuft ohne zusätzliche Python-Abhängigkeiten:

```sh
python3 -m unittest scripts.ci.tests.test_federation_contract
```

Die Prüfung bestätigt geschlossene Schemas, vollständige Beispiele, die Trennung von angewandter Inbox und Quarantäne sowie die öffentliche HTTP-Grenze ohne NATS-, Datenbank- oder Kubernetes-Begriffe. Kryptografische Signatur-, Replay-, Versions-, Partitions- und Persistenzbeweise liegen in den Rust-Tests unter `apps/api/tests/`.

Der normative Ablauf ist in `docs/specs/federation-wire-v1.md` dokumentiert. Beispielschlüssel und -signaturen sind ausschließlich formale Schemafixtures und keine verwendbaren Betreibergeheimnisse.
