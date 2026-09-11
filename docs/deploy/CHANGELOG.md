---
id: deploy.CHANGELOG
title: Deploy Changelog
doc_type: reference
status: active
summary: Änderungsprotokoll der Deployment-Konfiguration.
relations:
  - type: relates_to
    target: docs/deploy/README.md
---
# Deployment-Änderungsprotokoll

## 2026-09-10 - Produktionshost zu `commonserver` kanonisieren

**Geänderte Bereiche:**

- kanonische Produktionshost-Identität und Deployment-/Recovery-Dokumentation;
- `.env.prod.example`;
- täglicher Off-Host-PostgreSQL-Pull auf `heim-pc`.

**Beschreibung:**

Der bisherige Hostname `wg-prod-1` bleibt in datierten historischen Belegen als
damalige Identität erhalten. Für aktuelle Betriebsverträge heißt derselbe
Produktionshost künftig `commonserver`. Der Default des Off-Host-Backup-Pulls
folgt dieser Identität; `REMOTE_HOST` bleibt als expliziter
Kompatibilitäts-Override bestehen.

**Produktionswirkung:**

Die laufende Produktion wurde durch diesen Repository-Patch nicht neu
deployt. Ein Readback am 2026-09-10 bestätigte `commonserver` als Hostnamen,
Batch-SSH inklusive `sudo -n` vom Pull-Host sowie alle vier Domain-Read/Write-
Quellen der laufenden API mit `postgres`. Vor einer Neuinstallation des
Off-Host-Timers verlangt das Recovery-Runbook denselben SSH-Preflight.

**Prüfung:**

Naming Guard, Repository-/Dokument-Guards, Core-/Shell-Tests und der
Off-Host-Backup-Vertrag werden auf dem PR-Head geprüft. Datierte Juli-Claims
und Runtime-Belege werden nicht rückwirkend auf den neuen Hostnamen
umgeschrieben.

## 2026-08-02 - Erste Ortsweberei mit Webgemeindezentrum im Hammer Park verankern

**Geänderte Bereiche:**

- PostgreSQL-Domänenschema und Migrationspfad;
- öffentliche Ortsweberei-/Webgemeindezentrum-API;
- Kartenprojektion, Fokuspanel, Suche und Deep Links;
- PostgreSQL-, Browser-, Tastatur- und Mobilprüfungen.

**Beschreibung:**

Die bisherige einzelne Gewebezelle erhält erstmals ihre kanonische lokale
Produktgestalt: `Ortsweberei Hamm`, die stabile Gewebezelle
`hamm.weltgewebe.net` und genau ein dauerhaftes
`Webgemeindezentrum Hammer Park`. Der Kartenanker liegt bewusst ungefähr auf
einer Grünfläche im Hammer Park, damit dort reale gemeinsame Treffen möglich
sind. Sein Zustand lautet zunächst **„Gewünschter Treffort“**. Damit behauptet
das System weder Reservierung noch Genehmigung, Barrierefreiheit oder
regelmäßige Verfügbarkeit.

Datenbankregeln erzwingen die Eins-zu-eins-Zuordnung zwischen Ortsweberei,
Gewebezelle und aktivem Zentrum. Standortänderungen behalten die stabile
Zentrum-ID und ergänzen eine unveränderliche Chronik. Die API liefert Listen und
Detailansichten; die Karte behandelt das Zentrum als eigene dauerhafte
Domänenentität statt als gewöhnlichen Knoten, Accountmarker oder verblassenden
Faden. Gewünschte und vorgeschlagene Standorte sind durch eine gestrichelte
Markerbegrenzung von bestätigten Orten unterscheidbar. Tastaturfokus, mobile
Kompaktansicht und direkte URL-Fokussierung sind Teil desselben Vertrags.

**Produktionswirkung:**

Die neue Migration legt die drei stabilen Startidentitäten und den ersten
Standorteintrag an. Nach dem Rollout müssen Migration, API-Projektion und
Frontend gemeinsam auf demselben Commit laufen. Die spätere Bestätigung oder
Verlegung des Trefforts bleibt eine eigene nachvollziehbare
Governance-Entscheidung; dieser Schnitt stellt dafür das Datenmodell und die
Historiengrenze bereit, simuliert aber noch keinen beschlossenen Webrat-Befehl.

**Prüfung:**

Ein echter Wegwerf-PostgreSQL-Lauf belegt Eindeutigkeit, gleiche
Ortsweberei-Zuordnung, stabile Zentrum-ID, Zustandsvalidierung und
append-only-Standortchronik. Browserprüfungen belegen Marker, Deep Link,
Tastaturöffnung, Panelwahrheit und mobile Kerninformation. Der Karten-Build
bleibt innerhalb des bestehenden CSS-Leistungsbudgets.

## 2026-08-02 - Compose-Entwicklungsstack an repositoryweite Basemap-Stile binden

**Geänderte Bereiche:**

- `infra/compose/compose.core.yml`;
- `.github/workflows/compose-smoke.yml`;
- Compose-Vertragstests.

**Beschreibung:**

Der Web-Entwicklungscontainer bindet weiterhin `apps/web` als `/workspace` ein.
Der Basemap-Generator löst die repositoryweiten Stildateien dadurch absichtlich
unter `/map-style` auf. Seit der regionalen Deutschland-Variante benötigte der
Compose-Pfad dort sowohl `style.json` als auch `style-germany.json`, hatte das
Verzeichnis aber nicht eingebunden. Der Dev- und CI-Container brach deshalb beim
Vite-Start mit einer fehlenden `/map-style/style.json` ab.

`map-style` wird nun read-only nach `/map-style` eingebunden. Änderungen an den
Stildateien lösen den Compose-Smoke-Test aus. Die Einbindung betrifft nur den
Entwicklungs-/Smoke-Service in `compose.core.yml`; produktive Basemap-Mounts und
Aktivierungsverträge bleiben unverändert.

Der PostgreSQL-Healthcheck verlässt sich nicht mehr darauf, dass `pg_isready`
einen übergebenen Datenbanknamen validiert: Das Werkzeug meldet einen
verbindungsbereiten Server auch bei einer nicht vorhandenen Datenbank als bereit.
Der Check wartet nun zunächst auf den endgültigen, über TCP erreichbaren
PostgreSQL-Prozess und führt anschließend in der konfigurierten Datenbank
`${POSTGRES_DB:-weltgewebe}` eine echte `SELECT 1`-Abfrage aus. Damit wird die
Anwendungsdatenbank tatsächlich geöffnet und abgefragt; Schema- oder
API-Bereitschaft behauptet dieser begrenzte Check weiterhin nicht.

**Prüfung:**

Die gerenderte Compose-Konfiguration, der read-only-Mount, die ausführbare
Datenbankabfrage und der Workflow-Trigger werden strukturell aus dem YAML-Modell
geprüft. Eine Containerprobe liest beide Stildateien über `/map-style` und
erzeugt daraus die commitgebundene regionale Basemap-Buildidentität. Eine
separate PostgreSQL-Probe belegt außerdem: `pg_isready` liefert für eine fehlende
Datenbank weiterhin Erfolg, während `psql ... SELECT 1` dort fehlschlägt und nur
für die konfigurierte Datenbank erfolgreich ist.

## 2026-07-24 - Erstnutzerführung an der bestehenden Garnrolle ausrichten

**Geänderte Bereiche:**

- Garnrollen-Einstellungen und Karten-Einstieg;
- Gast- und Weberstatus-Texte;
- browsergestützte Erstnutzer- und Selbstbedienungstests.

**Beschreibung:**

Ein registrierter Account besitzt bereits genau eine Garnrolle. Die Oberfläche
führt deshalb nicht mehr durch eine vermeintliche Identitätserzeugung, sondern
durch drei getrennte Entscheidungen: Garnrolle beschreiben, freiwillig einen
privaten Kartenanker wählen und anschließend bestimmen, ob keine, eine
ungefähre oder die exakte Position öffentlich wird. Adresse oder Ortsnotiz
bleiben optional und privat; Weboberfläche und API behandeln sie nicht mehr als
Voraussetzung für eine öffentliche Kartenposition. Nicht übermittelte private
Felder bleiben unverändert; ein gespeicherter Kartenanker kann ausdrücklich und
dauerhaft entfernt werden.

Der vorhandene Garnrollenknopf auf der Karte führt direkt in diese Einrichtung.
Die abschließende Aktion heißt eindeutig „Garnrolle speichern“. Der Weberantrag
beschreibt nur zusätzliche Rechte und erzeugt keine zweite Garnrolle.

## 2026-07-24 - RoN-Identität und Legacy-Modus endgültig entfernen

**Geänderte Bereiche:**

- PostgreSQL-Schema und Account-Migrationen;
- API-Lese-, Schreib- und Backfillpfade;
- Garnrollen-Vertrag, Tests und aktive Dokumentation.

**Beschreibung:**

Weltgewebe besitzt nur noch eine persönliche Identität: die Garnrolle. Der
Kartenstatus `not_on_map`, `exact` oder `radius` ist eine Eigenschaft dieser
Garnrolle und kein eigener Kontotyp. Die frühere nullable Spalte `mode` wird
entfernt. API und Backfill interpretieren `type=ron`, `mode`, `ron_flag`,
`visibility` oder `suppress_public_pos` nicht länger, sondern weisen solche
Datensätze fail-closed ab.

Die Migration prüft vor dem `DROP COLUMN`, dass keine abweichenden Kontotypen
oder alten privaten Identitäts- und Sichtbarkeitsmarker mehr vorhanden sind.
Bedeutungslose Restwerte in der ohnehin entfernten Spalte `mode` blockieren den
Cutover nicht. Ein echter Legacy-Marker lässt die Migration dagegen ohne
Schemaänderung abbrechen. Der belegte Produktionsstand vom 24. Juli 2026 enthält
acht Accounts und null Legacy-Treffer in allen geprüften Kategorien.

Auch der lokale JSONL-Pfad ist fail-closed: Ein syntaktisch ungültiger oder nicht
kanonischer Accountdatensatz stoppt den API-Start mit Datei und Zeilennummer,
statt das Konto still aus dem Speicher zu entfernen. Die Bash- und
JavaScript-Demodatenproduzenten schreiben nur noch `type=garnrolle`, einen
expliziten `map_state` und keine entfernten Legacy-Felder.

**Produktionswirkung:**

Der commitgebundene Rollout muss den vorgesehenen Migration-Scope verwenden,
der die API des Zielcommits für die Migration neu erstellt und anschließend im
Modus `verify-applied` wiederherstellt. Ein älterer API-Prozess darf nach dem
Spaltenabbau nicht erneut gestartet werden. Die Down-Migration stellt für einen
Code-Rollback eine nullable Spalte `mode` wieder her; sie rekonstruiert bewusst
keine entfernte Identität. Positionen und `map_state` werden durch den Cutover
nicht verändert.

## 2026-07-23 - Magic-Link-Bestätigungsformular am Edge wieder zulassen

**Geänderte Bereiche:**

- `infra/caddy/Caddyfile`, `Caddyfile.heim` und `Caddyfile.vps`;
- CSP-Vertragsprüfung und Security-Header-Guard.

**Beschreibung:**

Die globale Nicht-Dokument-CSP für `/api/*` setzte `form-action 'none'` auch auf
die absichtlich als HTML-Formular ausgelieferte Magic-Link-Bestätigungsseite.
Browser wenden mehrere CSP-Header gleichzeitig an; dadurch blockierte die
Edge-Richtlinie sowohl die Formularübermittlung als auch das Inline-Styling der
Bestätigungsseite, obwohl der API-Handler selbst den engeren Formularvertrag
korrekt deklarierte.

Der Edge behandelt deshalb ausschließlich `GET /api/auth/magic-link/consume`
als schmale Dokument-Ausnahme. Caddy ist für öffentliche Antworten der
kanonische CSP-Eigentümer und setzt die Richtlinie verzögert, sodass eine vom
API-Upstream gelieferte CSP beim Schreiben der Response ersetzt statt mit ihr
kombiniert wird. Die Ausnahme behält `default-src 'none'`, `base-uri 'none'` und
`frame-ancestors 'none'`, erlaubt kein Script und gibt nur das bereits benötigte
Inline-Styling sowie `form-action 'self'` frei. `POST` auf demselben Pfad und alle
übrigen API- und Health-Antworten werden am Edge ebenfalls verzögert auf die
strikte `form-action 'none'`-Richtlinie gesetzt. Die API-eigene GET-CSP bleibt als
Defense-in-Depth für direkte interne Nutzung bestehen, bestimmt aber nicht die
öffentliche Edge-Response.

**Produktionswirkung:**

Beim Rollout muss die geprüfte VPS-Caddy-Konfiguration gemeinsam mit dem
Merge-Commit veröffentlicht und Caddy erfolgreich neu geladen werden. Danach
muss ein GET auf den Magic-Link-Consume-Pfad genau eine schmale CSP mit
`form-action 'self'` liefern, während ein gewöhnlicher API-Pfad und ein POST auf
den Consume-Pfad genau eine strikte CSP mit `form-action 'none'` ausliefern. Der
Runtime-Vertrag wird gegen einen Mock-Upstream geprüft, der absichtlich eine
widersprechende CSP setzt; diese Upstream-CSP darf am Edge nicht mehr sichtbar
sein. Ein frischer Magic-Link kann anschließend das Bestätigungsformular wieder
same-origin absenden; Token-, Nonce- und Sessionlogik werden durch diese Änderung
nicht verändert.

## 2026-07-14 - Schleswig-Holstein sichtbar kartografieren

**Geänderte Bereiche:**

- `map-style/style.json`;
- Web-Basemap-Auflösung, Buildidentität und regionale Browserproofs;
- alle repo-internen `/local-basemap/`-Caddy-Routen;
- öffentliche Live-Readiness für den Cache-Vertrag.

**Beschreibung:**

Der bisherige Regionalstil lud Schleswig-Holstein technisch, zeichnete jedoch nur
Wasser, Straßen, Gebäude und Ortsnamen. Große ländliche Flächen blieben deshalb
identisch zum grauen Hintergrund und wirkten wie fehlende Tiles. Style `0.3.0`
zeichnet nun die vorhandenen `landcover`- und `landuse`-Layer beider regionaler
PMTiles-Quellen. Der Browserproof verlangt diese Layer künftig ausdrücklich als
decodierte und sichtbar gerenderte Evidenz.

Der erste Rollout verwendete weiter `/local-basemap/style.json?v=0.3.0` und
lieferte die veränderliche Stildatei ohne ausdrückliche Cache-Regel aus. Dadurch
konnten bestehende Browserprofile eine früh geladene Antwort behalten, während
neue Inkognito-Profile bereits den Schleswig-Holstein-Stil sahen.

Style `0.3.1` erhält deshalb zusätzlich die konkrete Web-Buildkennung im
Anfragepfad: `/local-basemap/style.json?v=0.3.1&build=<commit>`. Alle
repo-internen `/local-basemap/`-Routen liefern außerdem
`Cache-Control: no-cache, must-revalidate`. Der Browser darf die Dateien lokal
halten, muss ihre Gültigkeit aber vor Wiederverwendung beim Server prüfen.

**Produktionswirkung:**

Style, Web-Artefakt und `Caddyfile.vps` müssen gemeinsam veröffentlicht werden.
Danach werden die öffentliche Buildkennung, der buildgebundene Style-URL, echte
Schleswig-Holstein-Range-Antworten sowie sichtbar gerenderte Landbedeckung und
Landnutzung erneut über den normalen Kartenpfad geprüft. Zusätzlich muss
`curl -sS -D - -o /dev/null https://weltgewebe.net/local-basemap/style.json`
den Header `Cache-Control: no-cache, must-revalidate` zeigen. Die öffentliche
Live-Readiness schlägt künftig fehl, wenn dieser Revalidierungsvertrag fehlt.
Die PMTiles-Archive selbst bleiben unverändert.

## 2026-07-13 - PMTiles-Repräsentationsvertrag und tiefe Archivprüfung

**Geänderte Bereiche:**

- `infra/caddy/Caddyfile`, `Caddyfile.heim`, `Caddyfile.vps` und `Caddyfile.proof`;
- Basemap-Runtime-Guard und Public-Live-Readiness;
- regionale PMTiles-Content-Proofs für Hamburg und Schleswig-Holstein.

**Beschreibung:**

Alle `.pmtiles`-Antworten unter `/local-basemap/` erhalten ausdrücklich den
Medientyp `application/octet-stream`. Voll-/HEAD- und Range-Pfade müssen
zusätzlich HTTP 200 beziehungsweise 206, `Accept-Ranges: bytes`, ein passendes
`Content-Range` und die PMTiles-Signatur belegen. Der Vertrag gilt für stabile
Aliasse und versionierte Dateien beider Regionen.

Vor Veröffentlichung traversiert ein begrenzter Validator alle erreichbaren
PMTiles-Root- und Leaf-Verzeichnisse, gleicht die Header-Zählwerte ab und
decodiert bis zu 96 deterministisch über Zoomstufen und Raum verteilte reale
MVT-Payloads. Die beobachteten Layer müssen zu Metadaten und Kartenstil passen.
Ein Vollscan jeder Kachel, kartografische Vollständigkeit und Datenfrische
werden weiterhin nicht behauptet.

**Produktionswirkung:**

Beim Rollout wird ausschließlich die geprüfte `Caddyfile.vps` installiert und
Caddy nach erfolgreicher Konfigurationsvalidierung neu geladen. Die PMTiles-
Artefaktbytes und Aliasse werden nicht verändert. Danach müssen die vier
öffentlichen stabilen/versionierten Pfade für Hamburg und Schleswig-Holstein
den 200-/206-Headervertrag erfüllen; die bestehenden SHA-256-Werte und der
Browser-Renderingpfad werden erneut gelesen. Bei Fehlern wird die vorherige
Caddy-Konfiguration wiederhergestellt.

## 2026-07-12 - Audit-Remediation für reproduzierbare und wiederherstellbare Deployments

**Geänderte Bereiche:**

- alle produktionsrelevanten Compose- und Caddy-Verträge;
- `scripts/weltgewebe-up` und der neue Webartefakt-Installer;
- PostgreSQL-Backup, isolierter Restore-Proof und Off-Host-Kopie;
- GitHub-Actions-, Containerimage-, Securityheader- und Buildheader-Guards.

**Beschreibung:**

Externe Compose-Images sind nun mit lesbarem Tag und Multiarch-SHA256-Digest
gebunden. Das eigene API-Image verlangt eine konkrete `API_VERSION`; ein
`latest`-Fallback ist nicht mehr zulässig. Der Deploywrapper exportiert die
aktuelle Commitkennung zusätzlich als `WELTGEWEBE_BUILD`, sodass Caddy den
beobachtbaren Header `X-Weltgewebe-Build` nicht mehr leer ausliefert.

Die HTTPS-Caddyfiles liefern HSTS mit einem Jahr Laufzeit und
`includeSubDomains`, bewusst noch ohne Preload. Die bestehende CSP-Ausnahme
`script-src 'unsafe-inline'` bleibt dokumentiert und wird nicht stillschweigend
als behoben ausgegeben.

Für VPS ohne Node/pnpm steht ein commit-, versions- und hashgebundener Installer
für lokal vorgebaute Webartefakte bereit. Er prüft das Archiv, installiert über
ein Staging-Verzeichnis, validiert und erneuert Caddy, liest die öffentliche
Version samt Buildheader zurück und stellt bei Abweichung das vorherige reale
Buildverzeichnis wieder her.

PostgreSQL erhält einen täglichen logischen Backupvertrag mit Gzip- und
SHA256-Prüfung, einen wöchentlichen Restore-Proof in einem netzlosen
Wegwerfcontainer sowie eine täglich verifizierte Off-Host-Kopie nach `heim-pc`.
Der bestehende Restic-Pfad nimmt diese Kopie anschließend in das entfernte
Repository auf. WAL/PITR und Object Lock werden ausdrücklich nicht behauptet.

**Produktionswirkung:**

Beim Rollout müssen die neuen digestgebundenen Images kontrolliert gezogen und
die Backup-/Restore-Timer separat installiert werden. Vor dem ersten Deploy
wird ein frischer Datenbank- und Webstand gesichert. Nach dem Deploy müssen
Commit, API-Image, `/_app/version.json`, `X-Weltgewebe-Build`, HSTS, Health,
Migrationen und Domänenzähler übereinstimmend zurückgelesen werden.

## 2026-07-11 - Eigene Garnrolle dauerhaft beschreiben und verorten

**Geänderte Bereiche:**

- privates Eigenprofil `GET/PATCH /accounts/me/profile`
- PostgreSQL- und JSONL-Account-Schreibpfad
- Einstellungen „Meine Garnrolle“
- nutzergesteuerte Kartenpunktwahl über `compose=garnrolle`

**Beschreibung:**

Ein angemeldeter Weber oder Admin kann ausschließlich die eigene Garnrolle
beschreiben und deren Kartensichtbarkeit speichern. Für `exact` und `radius`
wird kein Ort aus der Adresse geraten: Der Nutzer wählt den Punkt selbst durch
einen langen Druck auf die Karte und bestätigt ihn anschließend im
Einstellungsformular. Der ungespeicherte Entwurf und die einmalig übergebene
Koordinate bleiben im sitzungsgebundenen Browserspeicher; Koordinaten erscheinen
nicht in der URL. Abmeldung, Sitzungsverlust oder Kontowechsel entfernen diese
sensiblen Entwurfsdaten. `not_on_map` entfernt die öffentliche Projektion,
bewahrt aber die private interne Koordinate. Lokale Fokuspanels verwenden nun
die tatsächlich angebotenen pluralen Detailrouten (`/nodes`, `/accounts`,
`/edges`) und zeigen die gespeicherten Kategorien ohne technische Präfixe.

Im PostgreSQL-Modus wird zuerst `domain_accounts` aktualisiert und erst danach
der In-Memory-Cache. E-Mail, Rolle und WebAuthn-Identität sind nicht Teil des
PATCH-Schemas und bleiben unverändert. Die öffentliche Account-Antwort enthält
weiterhin weder Adresse noch interne Koordinate.

**Produktionswirkung:**

Nach Deployment wird der bisher deaktivierte Speicherbutton funktionsfähig.
Eine Garnrollen- oder Webungsaktion wird dadurch nicht automatisch ausgeführt;
der Nutzer löst Kartenwahl und Speicherung selbst aus.

## 2026-07-11 - Dev-Compose-Verträge für PgBouncer und Caddy korrigiert

**Geänderte Dateien:**

- `infra/compose/compose.core.yml`
- `infra/caddy/Caddyfile`
- `infra/caddy/Caddyfile.dev`
- `docs/deploy/README.md`

**Beschreibung:**

Der auf PgBouncer 1.25.2 aktualisierte Dev-Stack verwendet den tatsächlichen
Container-Listener `5432`, veröffentlicht ihn optional als Hostport `6432` und
authentifiziert PostgreSQL-16-SCRAM-Passwörter mit `scram-sha-256`. Der alte
`trust`/MD5-Mischvertrag führte zu `wrong password type` und wird nicht mehr
verwendet. Gleichzeitig wurden von Caddy 2.7 nicht mehr akzeptierte globale
`experimental_http3`-Serveroptionen aus den repo-internen Dev-Caddyfiles
entfernt. Der vollständige isolierte Stack wurde mit gesunder API über
SQLx → PgBouncer → PostgreSQL sowie laufendem Web- und Caddy-Service geprüft.

**Produktionswirkung:**

Keine direkte. Produktion auf `wg-prod-1` nutzt weiterhin den kanonischen
direkten PostgreSQL-Pfad und `Caddyfile.vps`; die Änderung repariert den
Dev-/CI-Compose-Smoke und verhindert falsche Rückschlüsse aus einem roten Main.

## 2026-05-24 - Account-Erstellung v0: compose.prod.yml reicht Bootstrap-Variablen durch

**Geänderte Dateien:**

- `infra/compose/compose.prod.yml`
- `apps/api/entrypoint.sh`
- `apps/api/Dockerfile`
- `scripts/dev/bootstrap-first-account.sh` (neue Datei)
- `scripts/dev/smoke-seed.sh` (neue Datei)
- `scripts/dev/smoke-account-create.sh` (neue Datei)

**Beschreibung:**

1. **Normaler Account-Erstellungspfad** ist `POST /api/accounts` durch eingeloggte
   Admins. Admin-only; erfordert eine gültige Admin-Session.

2. **Bootstrap nur für Henne-Ei-Situationen**: `GEWEBE_SEED_REAL=true` aktiviert
   den Erst-Admin-Bootstrap beim Container-Start. Dieser Pfad ist ausschließlich
   für den allerersten Admin gedacht, wenn noch kein Account in der Datenbank
   existiert.

3. **`infra/compose/compose.prod.yml`** reicht folgende Variablen an den
   API-Container durch:
   - `GEWEBE_SEED_REAL` (default: `false`) — aktiviert Bootstrap.
   - `GEWEBE_SEED_DEMO` — Demo-Daten ein/aus. Für echte Deployments auf `false`
     setzen.
   - `GEWEBE_SEED_REAL` und `GEWEBE_SEED_DEMO` sind gegenseitig ausgeschlossen:
     beide gleichzeitig sind verboten; der Entrypoint bricht ab. Es gibt keinen
     Mischbetrieb und kein stilles Überspringen bei widersprüchlicher Konfiguration.

4. **`bootstrap-first-account.sh` ohne jq**: `bootstrap-first-account.sh` benötigt
   Bash sowie Standard-CLI-Tools (`grep`, `awk`, `sed`, `printf`); kein `jq`.

5. **Smoke/Dev-Hinweis**: `smoke-account-create.sh` benötigt `jq` für die
   Auswertung der JSON-API-Antworten. Der Smoke ist für Entwicklungsumgebungen
   gedacht, nicht für den Container-Start. `bootstrap-first-account.sh` benötigt
   kein `jq`.

6. **Dateinamen historisch**: Die JSONL-Datendateien heißen weiterhin
   `demo.accounts.jsonl`, `demo.nodes.jsonl`, `demo.edges.jsonl` — auch für reale
   Daten. Die Namen sind historisch und bleiben aus Kompatibilitätsgründen
   unverändert.

## 2026-05-24 - CSP-Härtung für repo-interne local-sovereign Caddyfiles

**Geänderte Dateien:**

- `infra/caddy/Caddyfile`
- `infra/caddy/Caddyfile.dev`

**Beschreibung:**

Die repo-internen Caddy-Konfigurationen entfernen externe CARTO-Tile-Provider
aus `connect-src` und `img-src`. WebSocket-Quellen (`ws:`/`wss:`) bleiben für
Dev-/Proxy-Betrieb erlaubt; Basemap-Bild- und Tile-Quellen bleiben lokal.

Die operative Heimserver-Edge-Caddyfile kann extern unter
`/opt/heimgewebe/edge/Caddyfile` liegen und wird durch `weltgewebe-up`
bevorzugt, wenn vorhanden. Diese Änderung härtet die repo-internen
Referenz-/Dev-Caddyfiles und verhindert künftige CSP-Drift, ersetzt aber nicht
den Live-Edge-Abgleich.


## 2026-05-23 - CARTO-freies local-sovereign-Bundle via Build-Time-Generator

**Geänderte Dateien:**

- `apps/web/scripts/generate-basemap-config.js` (neue Datei)
- `apps/web/basemap-mode.policy.json` (neue Policy-Datei)
- `apps/web/.gitignore` (ergänzt: `src/lib/generated/`)
- `apps/web/src/lib/generated/basemapConfig.ts` (generiert, nicht eingecheckt — gitignored, von `generate-basemap-config.js` erzeugt)
- `apps/web/src/lib/map/config/basemap.current.ts`
- `apps/web/src/lib/map/config/basemap.current.test.ts`
- `apps/web/package.json`
- `apps/web/vite.config.ts`

**Referenzierte bestehende Guards (unverändert):**

- `scripts/weltgewebe-up`
- `scripts/tests/test_basemap_mode_guard.sh`
- `Makefile`

**Beschreibung:**

Korrektur des vorherigen Eintrags: `envPrefix` allein entfernt den CARTO-String
**nicht** zuverlässig aus dem Client-Bundle. Der Remote-Zweig samt
`https://basemaps.cartocdn.com/...` blieb trotz `PUBLIC_BASEMAP_MODE=local-sovereign`
statisch im Bundle (`apps/web/build/_app/immutable/nodes/11.*.js`), weil sich der
Bundler nicht auf Dead-Code-Elimination des Remote-Zweigs verlassen ließ. Der
Leak-Guard in `weltgewebe-up` schlug deshalb korrekt fehl.

1. **Build-Time-Basemap-Config-Generator**: Neues Script
   `apps/web/scripts/generate-basemap-config.js` liest `PUBLIC_BASEMAP_MODE` und
   erzeugt `apps/web/src/lib/generated/basemapConfig.ts`. Erlaubte Werte:
   `unset`, `local-sovereign`, `remote-style`; jeder andere Wert führt zu einem
   harten Fehler (Schutz vor stillen Tippfehlern). Der CARTO-`styleUrl` wird
   **ausschließlich** für `remote-style` emittiert. Bei `local-sovereign` und beim
   ungesetzten Default enthält die generierte Datei keinen Remote-String.

2. **Sovereign-by-default**: Ein ungesetztes `PUBLIC_BASEMAP_MODE` erzeugt eine
   CARTO-freie `local-sovereign`-Konfiguration. `remote-style` ist damit nur noch
   explizit wählbar.

3. **`basemap.current.ts` entkoppelt**: `REMOTE_STYLE_URL` und der CARTO-String
   wurden entfernt. `currentBasemap` wird aus `baseConfig` und dem generierten
   `BUILD_BASEMAP_CONFIG` (Modus + optional `styleUrl`) zusammengesetzt. Der
   Remote-Zweig liest den URL als Property-Zugriff aus dem generierten Modul —
   es existiert kein CARTO-String-Literal mehr in dieser Datei. Damit hängt die
   Souveränität nicht mehr an Dead-Code-Elimination, sondern daran, dass der
   String im local-sovereign-Importgraph schlicht nicht vorkommt.

4. **Build- und Test-Verdrahtung**: `generate-basemap-config.js` läuft über das
   gemeinsame Script `generate:basemap-config` im `sync`-Script (und damit in
   `prebuild`/`precheck`/`precheck:ci`) neben `generate-version.js` sowie im
   `dev`/`dev:cs`-Start. Zusätzlich erzeugen jetzt `pretest`, `pretest:ci`,
   `pretest:proof:basemap-real` und `pretest:unit` die Datei vor Test-Discovery.
   Das verhindert ein fehlendes `basemapConfig.ts` bei `pnpm test`,
   `pnpm test:ci` und `pnpm test:proof:basemap-real` auf frischem Checkout.
   Auch `prelint` und `preci` erzeugen die Datei, damit `pnpm lint` und `pnpm ci`
   auf frischem Checkout nicht scheitern, wenn TypeScript-Imports die generierte
   Datei prüfen.

5. **Guard bleibt hart**: Der Leak-Guard in `scripts/weltgewebe-up` wurde **nicht**
   abgeschwächt. In `local-sovereign` muss
   `grep -R 'basemaps.cartocdn.com\|voyager-gl-style\|cartocdn' apps/web/build/_app/immutable`
   leer sein und `local-basemap` vorhanden; sonst Deploy-Abbruch.

6. **`envPrefix` bleibt, ist aber nicht der Sicherheitsanker**: Der Kommentar in
   `vite.config.ts` wurde korrigiert. `envPrefix` exponiert nur den Env-Wert; die
   CARTO-Freiheit garantiert der Generator.

7. **CSP unverändert**: keine Lockerung, `basemaps.cartocdn.com` bleibt nicht
   erlaubt. Keine Caddy-/DNS-/PMTiles-/Glyphs-/API-Änderungen.

**Verifikation:**

- `PUBLIC_BASEMAP_MODE=local-sovereign pnpm build` → CARTO-Grep im Bundle leer,
  `local-basemap` vorhanden.
- ungesetzt `pnpm build` → ebenfalls CARTO-frei (sovereign default).
- `PUBLIC_BASEMAP_MODE=remote-style pnpm build` → CARTO erwartungsgemäß vorhanden.

## 2026-05-23 - Souveräner Basemap-Modus im Frontend erzwungen, CARTO-Leak-Guard (gehärtet)

> **Hinweis (korrigiert durch den Eintrag oben):** Punkt 1 dieses Eintrags
> beschrieb `envPrefix` + Inlining + Dead-Code-Elimination als Mechanismus zur
> Entfernung des CARTO-Strings aus dem `local-sovereign`-Bundle. Das war nicht
> ausreichend — der CARTO-String blieb im Bundle. Maßgeblich ist jetzt die
> generierte Build-Time-Konfiguration (siehe Eintrag oben).

**Geänderte Dateien:**

- `apps/web/src/lib/map/config/basemap.current.ts`
- `apps/web/src/lib/map/config/basemap.current.test.ts` (neue Datei)
- `apps/web/vite.config.ts`
- `scripts/weltgewebe-up`
- `scripts/tests/test_basemap_mode_guard.sh` (neue Datei)
- `Makefile`

**Beschreibung:**

1. **SvelteKit-konforme Env-Lesung**: `PUBLIC_BASEMAP_MODE` wird via
   `import.meta.env.PUBLIC_BASEMAP_MODE` mit sicherer Fallback-Logik gelesen.
   Dies vermeidet Probleme mit `$env/static/public` in Test-Kontexten (Playwright)
   und bleibt kompatibel mit Vite/Browser-Build-Kontexten. Der Wert wird bei
   gesetztem Wert als String-Literal inlined — Voraussetzung für das
   Dead-Code-Eliminieren des Remote-Zweigs durch den Bundler.

2. **Mode-Resolution vor Build-Entscheidung**: `scripts/weltgewebe-up` validiert
   und setzt `PUBLIC_BASEMAP_MODE` BEVOR die Build-Entscheidung getroffen wird.
   Dies stellt sicher:
   - Ungültige Env-Werte werden früh erkannt und hart blockiert
   - Edge/Heimserver-Deployments nutzen standardmäßig `local-sovereign`
   - Der Leak-Guard kann auch bestehende Bundles prüfen, auch wenn kein Rebuild nötig ist

3. **Heimserver/Edge-Default**: Wenn `PUBLIC_BASEMAP_MODE` ungesetzt ist und
   ein Edge-/Heimserver-Deploy erkannt wird (`DEPLOY_FRONTEND_MODE=edge` oder
   laufender Edge-Gateway-Container), wird der Modus auf `local-sovereign` gesetzt.
   Ein explizit gesetzter Wert wird nicht überschrieben. Der effektive Modus wird
   geloggt: `Frontend basemap mode: <mode> (<source>)`.

4. **local-sovereign Build-Guard (auch bei Build-Skip)**: Der Guard läuft NACH der
   Build-Entscheidung, unabhängig davon, ob ein Rebuild stattgefunden hat oder nicht.
   Bei effektivem Modus `local-sovereign` prüft `weltgewebe-up`
   `apps/web/build/_app/immutable` auf `basemaps.cartocdn.com`/`voyager-gl-style`/
   `cartocdn`. Treffer → harte Fehlermeldung, Deploy-Abbruch. Zusätzlich muss
   `local-basemap` im Bundle vorhanden sein, sonst Abbruch.
   Dies verhindert, dass ein mit `remote-style` gebautes Bundle mit passendem
   `version.json` durchrutscht, wenn die Build-Entscheidung es als „nicht stale" einstuft.

5. **CSP bleibt unverändert hart**: keine Lockerung, `basemaps.cartocdn.com` bleibt
   nicht erlaubt. Keine Caddy-/DNS-/PMTiles-/Glyphs-/API-Änderungen.

**Risiko:**

- `remote-style` ist weiterhin nur explizit wählbar und behält den CARTO-Default-URL
  (als `REMOTE_STYLE_URL`), der ausschließlich im Remote-Zweig landet. In
  `local-sovereign`-Builds wird dieser Zweig samt URL durch den Bundler entfernt.
- **Atomizität**: Wenn der Guard nach `pnpm build` fehlschlägt, ist `apps/web/build`
  bereits geschrieben. Dies ist nicht zwingend ein Blocker (CSP blockiert CARTO
  sowieso), sollte aber bewusst sein. Folge-PR könnte Pre-Build-Validierung oder
  Staging-Verzeichnis erwägen.

## 2026-05-23 - Frontend-Version-Guard und stale Web-Build-Erkennung gehärtet

**Geänderte Dateien:**

- `scripts/weltgewebe-up`
- `scripts/lib/parse-version-json.py` (neue Datei)
- `scripts/tests/test_version_guard.sh`
- `Makefile`

**Beschreibung:**

Das Frontend-Deployment wurde um stale-Version-Erkennung ausgebaut:

1. **Parser-Robustheit**: Version-JSON-Parsing aus Inline-Python in gemeinsamen Helper
   `scripts/lib/parse-version-json.py` ausgelagert.
   Syntaxfehler im Frontend-Guard behoben (f-string mit escaped quotes).

2. **Auto-Build-Staleness**: `BUILD_WEB=auto` erkennt jetzt stale `apps/web/build/_app/version.json`:
   - Fehlendes `version.json` → Rebuild
   - Ungültiges JSON → Rebuild
   - `version` leer/blank → Rebuild
   - `version != CURRENT_SHORT_SHA` (aktueller Deploy-HEAD) → Rebuild

   Beispiel: Heimserver mit Version `17314c6a`, neuer Deploy-HEAD `c67aaa67` → Auto-Rebuild.

3. **Live-Guard**: `/_app/version.json` wird nach dem Deploy gegen `CURRENT_SHORT_SHA` verglichen.
   Stale-Match → harte Fehlermeldung:

   ```text
   Frontend Guard failed: /_app/version.json is stale: version <X>, expected <Y>.
   ```

4. **Failure-Bundle-Erweiterung**: Bei Version-Guard-Fehler speichert das Bundle nun Kontext-Datei
   `version_guard_context.txt` mit live-version, expected SHA, build_id, commit, raw JSON und Headers.

5. **Test-Verbesserung**: 14 Regressions-Tests via Contract-Test-Ansatz (gemeinsamer Helper).
   Tests in `make validate-shell-tests` integriert.

**Risiko:**

Auto-Deploy kann häufiger ein Frontend-Build auslösen; Deployment-Dauer kann sich erhöhen.
Nutzen: verhindert stale Frontend bei neuem Backend/API-Deploy — das Fenster für API/Frontend-Mismatch ist schmal.

## 2026-05-23 - API-Dockerfile: konfigurierbare Cargo-Build-Parallelität

**Geänderte Dateien:**

- `apps/api/Dockerfile`

**Beschreibung:**

Das API-Dockerfile nutzt jetzt `CARGO_BUILD_JOBS` als explizites Build-Arg,
um die Cargo-Build-Parallelität stabil und nachvollziehbar zu steuern.
Der Default ist auf `2` gesetzt, um die Build-Last auf dem Heimserver zu glätten
und harte Abbrüche bei `docker compose up --build` zu reduzieren.
Auf CI oder größeren Buildern kann der Wert gezielt per
`--build-arg CARGO_BUILD_JOBS=<n>` überschrieben werden.

**Risiko:** Build kann länger dauern; dafür stabilerer Heimserver-Deploy.

## 2026-05-23 - `weltgewebe-up` Deploy-Branch-Vertrag explizit gemacht

**Geänderte Dateien:**

- `scripts/weltgewebe-up`
- `scripts/tests/test_weltgewebe_up_git_branch.sh`
- `Makefile`
- `docs/deploy/heimserver.deployment.md`

**Beschreibung:**

Das Deploy-Skript `weltgewebe-up` verwendet jetzt einen expliziten Deploy-Branch-Vertrag:

- Standard-Deploy-Branch ist `main`.
- Abweichende Branches sind nur bewusst möglich (`--branch <name>` oder `WELTGEWEBE_DEPLOY_BRANCH=<name>`).
- Optional kann für gezieltes Debugging `--current-branch` verwendet werden.
- `--no-pull` bleibt Offline-Recovery und überspringt Git-Sync-/Branchwechsel-Operationen (kein fetch/switch/pull).

Die Git-Preflight-Semantik wurde vereinheitlicht: Konflikte/Dirty Worktree blockieren weiterhin,
Branchwechsel erfolgt nur auf den expliziten Zielbranch, Pull nur via `--ff-only`.
Zusätzlich wurde die Failure-Bundle-Git-Diagnostik um Deploy-Branch-/Switch-Metadaten erweitert.

**Risiko:** Mittel (geänderte Default-Semantik für Branch-Auswahl).

**Nutzen:** Verhindert stillen Heimserver-Branch-Drift und macht Deploy-Zielbranch diagnostisch transparent.

## 2026-05-23 - API-Container auf Bookworm/OpenSSL 3 umgestellt

**Geänderte Dateien:**

- `apps/api/Dockerfile`
- `infra/compose/compose.core.yml`

**Beschreibung:**

Das API-Container-Image wurde von Bullseye auf Bookworm gehoben, damit
OpenSSL-3-abhängige WebAuthn/Attestation-Dependencies zuverlässig bauen.
Der Builder installiert die OpenSSL-Build-Abhängigkeiten explizit; die Runner-Stage
enthält `libssl3` für dynamisch gegen OpenSSL gelinkte Runtime-Abhängigkeiten.
Der Dev-Compose-API-Service nutzt ebenfalls `rust:1.89.0-bookworm`, damit Dev/CI
nicht auf der alten Bullseye/OpenSSL-1.1-Basis verbleiben.

**Auswirkung auf Deployment:**

- Betrifft API-Image-Build, Runtime-Stage und Dev-Compose-API-Service.
- Keine Änderung an Applikationslogik, Datenbank oder Volumes.

**Risiko:** Niedrig bis mittel. Die Basisimages wechseln auf Bookworm; Build- und
Runtime-Stage bleiben dabei konsistent.

## 2026-05-16 - CI-only Caddyfile.proof hinzugefügt

**Geänderte Dateien:**

- `infra/caddy/Caddyfile.proof`

**Beschreibung:**

Neue minimale Caddy-Konfiguration ausschließlich für den CI-Job `basemap-range-delivery-proof`.
Die Datei bindet auf Port 8081, serviert `/local-basemap/*` aus `/srv/weltgewebe-basemap` und
aktiviert `file_server`, damit Caddy HTTP-Range-Requests (206 Partial Content)
korrekt beantwortet. Kein TLS, keine CSP, keine CORS-Header — bewusst minimal gehalten, da
der Scope ausschließlich der Verifikation der Range-Delivery-Kette (`curl` → Caddy → `.pmtiles`)
im CI gilt.

Diese Datei ist **nicht** für Produktions-Deployments vorgesehen und wird von keinem
`compose.prod.yml`-Service referenziert.

**Risiko:** Kein Produktionsrisiko. Nur im CI-Job aktiv.

## 2026-05-09

- `infra/caddy/Caddyfile`, `Caddyfile.dev`, `Caddyfile.heim`, `Caddyfile.prod`: Neuer optionaler Response-Header `X-Weltgewebe-Build` aus Environment-Variable `WELTGEWEBE_BUILD`. Wird in jedem Header-Block emittiert; die Variable wird vom Deploy-Setup gesetzt (noch ausstehend) und matcht dann das `version`-Feld aus `/_app/version.json`. Beobachtbare Verhaltensänderung: alle Antworten enthalten den Header (ggf. leer, solange `WELTGEWEBE_BUILD` ungesetzt ist). Begleitkommentare neutralisiert: keine unbelegten Aussagen mehr über Deploy-Verhalten oder Caddys Umgang mit ungesetzten Variablen.

## 2026-03-31

- `infra/caddy/Caddyfile.heim`: CSP-Härtungsfix — `https://basemaps.cartocdn.com` aus `connect-src` und `img-src` entfernt. Die Heim-Instanz operiert im `local-sovereign`-Modus; externe Tile-Provider haben dort keinen Platz in der CSP. Fail-Fast-Nebeneffekt: ein irrtümlich remote gebautes Bundle erzeugt jetzt sichtbare CSP-Violations statt stillschweigend CartoDB zu kontaktieren.

## 2026-03-30

- `infra/caddy/Caddyfile`: Caddy-Route von `/basemap/*` auf `/local-basemap/*` korrigiert (Contract-Drift-Fix). Die Route entspricht jetzt dem öffentlichen Edge-Contract, der übereinstimmend von Frontend (`basemap.ts`), Vite-Middleware (`vite.config.ts`) und Blueprint (`map-blaupause.md §7`) als `/local-basemap/` definiert wird. Zuvor fielen Anfragen an `/local-basemap/` durch den Catch-All an Vite (im Dev-Kontext unauffällig); der souveräne Hosting-Pfad war faktisch inaktiv. — Neuer Regressions-Guard: `scripts/guard/caddy-basemap-route-guard.sh` sichert den Vertrag ab.

## 2026-03-21

- `infra/caddy/Caddyfile.heim` und `compose.prod.yml`: Routing für `/local-basemap/*` (inklusive `.pmtiles` und `.meta.json`) sowie Volume-Mount für `map-style` ergänzt, um produktiv souveräne Basemap-Assets auszuliefern.

## 2026-03-18

- `infra/caddy/Caddyfile.heim`: Added explicit `Cache-Control: no-store` header for `/_app/version.json` to enforce the no-store version.json delivery contract.

Dieses Dokument protokolliert Infrastruktur-Änderungen, die Auswirkungen auf das Deployment haben.

---

## 2026-03-08 - NATS Healthcheck & JetStream-Persistenz korrigiert

**Ursprung / Referenz:** Heimserver-Entscheidung (vollwertiger Stack)

**Geänderte Dateien:**

- `infra/compose/compose.prod.yml`

**Beschreibung:**

Der NATS-Healthcheck wurde korrigiert, da das bisherige `nats:2.10`-Image als Scratch-Image keine Shell
(`sh`) oder Utilities (`wget`) enthielt, was dazu führte, dass der `CMD-SHELL` Healthcheck fehlschlug.
Zur Lösung wurde auf `nats:2.10-alpine` umgestellt und ein strikt image-kompatibler Exec-Form-Healthcheck
(`["CMD", "wget", "-qO-", "http://localhost:8222/healthz"]`) implementiert.
Zusätzlich wurde die JetStream-Speicherpfad-Konfiguration (`-sd /data`) zum `command` hinzugefügt,
sodass persistierte Daten nun korrekt in das gemountete Docker-Volume `nats_js:/data` fließen.

---

## 2026-03-07 - NATS wieder als kanonischer Stack-Bestandteil integriert

**Ursprung / Referenz:** Heimserver-Entscheidung (vollwertiger Stack)

**Geänderte Dateien:**

- `infra/compose/compose.prod.yml`
- `infra/compose/compose.prod.override.yml`

**Beschreibung:**

Die Infrastruktur-Konfiguration wies einen Drift bezüglich des Message Brokers NATS auf:
Während Dokumentation und API-Health-Checks (bei Konfiguration von `NATS_URL`) bereits von NATS als integralem
Bestandteil ausgingen und der Service temporär nur über Overrides bereitgestellt wurde, fehlte er im primären
Produktions-Compose-File.
Zur Wiederherstellung der kanonischen Deckungsgleichheit (gemäß der Entscheidung: "Weltgewebe auf dem Heimserver soll
als vollwertiger Stack mit NATS/JetStream betrieben werden") wurde der `nats`-Service nun wieder fest in die
`compose.prod.yml` integriert. Die API bezieht `NATS_URL` nun per Default.
Redundante Deklarationen in der `compose.prod.override.yml` wurden bereinigt. Der Deployment-Pfad (`weltgewebe-up`)
startet somit den vollständigen, in sich konsistenten Stack und verifiziert NATS implizit mit über den API-Health-Check.
Zudem wurde die Startup-Robustheit verbessert: NATS verfügt nun über einen eigenen HTTP-Healthcheck auf Port 8222,
und die API wartet via `condition: service_healthy` explizit auf die Readiness von NATS.
Die `NATS_URL` ist zudem default-basiert overridefähig, ohne den kanonischen lokalen Stack zu verändern.

**Risiko:** Niedrig. (Auflösung von Drift / Konsistenzherstellung).

---

## 2026-03-05 - Fix CSP for SvelteKit Inline Bootstrap

**Ursprung / Referenz:** Fix für Whitepage (CSP-Problem)

**Geänderte Dateien:**

- `infra/caddy/Caddyfile`

**Beschreibung:**

Die Content-Security-Policy (CSP) in Caddy wurde angepasst, um SvelteKit's Inline-Bootstrap-Script zu
erlauben. Der Direktive `script-src` wurde `'unsafe-inline'` hinzugefügt, da andernfalls das
Svelte-Frontend mit einer leeren Seite (Whitepage) blockiert wurde.

Ein Preflight-Guard (`csp_contract_static.sh`) wurde ebenfalls dem Deployment hinzugefügt, um sicherzustellen, dass
Inline-Scripts zukünftig nicht versehentlich wieder durch die CSP blockiert werden.

**Risiko:** Mittel. Die CSP ist weniger streng (`'unsafe-inline'`), was XSS-Risiken potenziell erhöht, jedoch
im aktuellen Kontext (Heimnetz, funktionierendes UI priorisiert) als pragmatische Lösung akzeptiert wurde.
Hardening via Nonce/Hash sollte später folgen.

---

## 2026-02-21 - Fix: SMTP Env Injection via `env_file`

**Ursprung / Referenz:** Fix für AUTH_PUBLIC_LOGIN (Deployment-Drift)

**Geänderte Dateien:**

- `infra/compose/compose.prod.override.yml`

**Beschreibung:**

Um sicherzustellen, dass SMTP-Variablen (und andere Secrets) zuverlässig im `weltgewebe-api`-Container ankommen,
wurde dem Service `api` die Direktive `env_file: - /opt/weltgewebe/.env` hinzugefügt.

Hintergrund: `docker compose --env-file` stellt Variablen primär für die Interpolation im Compose-File bereit.
Ohne explizites Mapping im `environment`-Block landen diese nicht automatisch im Container-Runtime-Environment.
Die Ergänzung von `env_file:` im Service stellt sicher, dass alle Variablen aus der Datei in den Container injiziert werden.
Annahme: Der Pfad `/opt/weltgewebe/.env` ist eine bewusste Heimserver-Layout-Abhängigkeit.
Override: `WELTGEWEBE_ENV_FILE` kann den Pfad überschreiben.

Zusätzlich wurde im CI-Workflow das ungültige Argument `--max-cache-age` für den `lychee` Link-Checker entfernt,
um Build-Fehler zu beheben.

**Risiko:** Niedrig (Deployment-Korrektur).

---

## 2026-02-20 - Option C vollständig implementiert (Auto-Provision im Auth-Flow)

**Ursprung / Referenz:** (feat/auth-open-registration)

**Geänderte Dateien:**

- `apps/api/src/config.rs`
- `apps/api/src/routes/auth.rs`

**Beschreibung:**

Option C war bisher nur im Config-Layer aktiv. Unknown emails wurden weiterhin mit `policy_denied` behandelt.
Jetzt wird Auto-Provision + Magic-Link-Versand im Auth-Flow durchgeführt, wenn `is_open_registration()` true ist.
Damit entspricht Runtime-Verhalten der Konfigurationsvalidierung.

**Risiko:** Mittel–Hoch (öffentlicher Login-Endpunkt).

---

## 2026-02-20 - Public Magic-Link Login aktiviert (Option C)

**Ursprung / Referenz:** (feat/auth-public-magic-link)

**Geänderte Dateien:**

- `infra/compose/compose.prod.override.yml`
- `docs/deploy/heimserver.deployment.md`

### Added

- Public Magic-Link Login aktiviert (Option C)
- Allowlist deaktiviert (erfordert strikte Rate-Limits im Environment)
- Rate-Limits für Open Registration mandatory gemacht (Code-Enforcement)
- Token-Log-Deaktivierung für Prod stark empfohlen (Warnung im Log)

### Risiko

Mittel bis hoch (öffentlicher Login-Endpunkt)

---

## 2026-02-19 - compose.prod.override.yml: `environment: null` → `environment: {}` (caddy)

**Ursprung / Referenz:** PR #746 (chore: remove bak artifacts, fix compose environment null)

**Geänderte Dateien:**

- `infra/compose/compose.prod.override.yml`

**Beschreibung:**

Der `caddy`-Service-Block in `compose.prod.override.yml` verwendete `environment: null`, was kein gültiger
Docker-Compose-Wert ist (erwartet wird eine Map oder Liste). Dies wurde durch `environment: {}` (leere Map) ersetzt,
was semantisch äquivalent ist (keine Umgebungsvariablen für caddy), aber syntaktisch korrekt.

**Auswirkung auf Deployment:**

Keine funktionalen Änderungen. `docker compose config` läuft ohne Parser-Fehler durch.

**Risiko:** Niedrig.

---

## 2026-02-10 - compose.prod.yml auf stabilen Stand zurückgesetzt

**Ursprung / Referenz:** f6e19c5 (revert: remove compose topology changes) – (Commit-Hash kann bei Squash abweichen)

**Geänderte Dateien:**

- `infra/compose/compose.prod.yml`
- `policies/limits.yaml`
- `apps/api/Dockerfile`

**Aktueller Stand:**

- Caddy-Service als Gateway (Ports auf localhost)
- DB-Credentials über Umgebungsvariablen
- Keine `NATS_URL` (Service existiert nicht)
- `policies/limits.yaml` mit Schema-konformen Limitwerten
- `Dockerfile` mit optimierter Layer-Caching-Reihenfolge

**Hintergrund der Rücksetzung:**

Die Änderungen aus der damaligen Änderung (Referenz oben) wurden zurückgesetzt, um Risiken zu vermeiden:

- Fehlender Caddy-Service (Gateway-Prinzip verletzt)
- `NATS_URL` ohne NATS-Service gesetzt (Readiness-Checks schlugen fehl)
- Hardcodierte DB-Credentials statt env-basiert
- API auf `0.0.0.0:8081` exponiert (Sicherheitsrisiko)
- `policies/limits.yaml` Semantik geändert (Schema-Bruch)

**Risiko:** Niedrig. Stabiler, bewährter Stand.

---

## 2026-02-10 - Caddy Rate-Limiting vollständig deaktiviert

**Ursprung / Referenz:** 5e94a21 (ops: stabilize prod runtime) – (Commit-Hash kann bei Squash abweichen)

**Geänderte Dateien:**

- `infra/caddy/Caddyfile.prod`

**Beschreibung:**

Rate-Limiting-Konfiguration in Caddy wurde vollständig auskommentiert, da die `rate_limit`-Direktive ein externes
Plugin erfordert, das in der Standard-Caddy-Distribution nicht enthalten ist.

**Syntax-Korrektur (dieser PR):**

Die ursprüngliche Deaktivierung in der damaligen Änderung (Referenz oben) war syntaktisch unvollständig (nur die
öffnende Zeile `rate_limit {` war auskommentiert, während die Subdirektiven aktiv blieben). Dies führte zu einem
Parse-Fehler beim Caddy-Start. Die Syntax wurde vollständig korrigiert, indem der gesamte Block inkl. aller Subdirektiven
und schließender Klammer auskommentiert wurde.

**Änderungen im Detail:**

- `order rate_limit before basicauth` → auskommentiert
- `rate_limit { ... }` Block vollständig auskommentiert (alle Zeilen)
- Alle `rate_limit @matcher zone` Direktiven → auskommentiert

**Auswirkung auf Deployment:**

- Keine Änderungen am Deployment-Prozess erforderlich
- Caddy läuft weiterhin mit Standard-Distribution
- Rate-Limiting muss bei Bedarf auf Infrastruktur-Ebene (z.B. Firewall, Load Balancer) implementiert werden

**Risiko:**

- Login-Endpunkte sind ohne Rate-Limiting anfälliger für Brute-Force-Angriffe
- Mitigation: Monitoring der Login-Versuche, ggf. Fail2Ban oder ähnliche Host-Level-Lösungen

**Verifikation:**

```bash
docker compose -f infra/compose/compose.prod.yml config
# Sollte ohne Fehler durchlaufen

docker run --rm -v "$PWD/infra/caddy/Caddyfile.prod:/etc/caddy/Caddyfile:ro" caddy:2.7 caddy validate --config /etc/caddy/Caddyfile
# Sollte "Valid configuration" ausgeben
```

**Follow-up:**

- Evaluation von Caddy mit custom build inkl. rate_limit Plugin
- Oder: Implementierung von Rate-Limiting auf Applikationsebene (API-Service)

## 2026-03-19 - Basemap Hosting Vorbereitung

**Ursprung / Referenz:** Basemap Souveränität (Blueprint)

**Geänderte Dateien:**

- `infra/caddy/Caddyfile*` (Ergänzung für `/basemap/*` inkl. CORS & Range Requests)
- `infra/compose/compose.core.yml` und `compose.prod.yml` (Volume Mount für `/srv/weltgewebe-basemap`)

**Zusammenfassung:**
Um die souveräne Kartengrundlage als Artefakt (Phase 1) verteilen zu können, muss der Edge-Proxy PMTiles Dateien unterstützen und via File Server anbieten.

---

## 2026-03-08 - Parameterized policy bind path in compose.prod.override.yml

**Ursprung / Referenz:** Fix für Deployment-Drift (REPO_DIR)

**Geänderte Dateien:**

- `infra/compose/compose.prod.override.yml`

**Beschreibung:**

Der harte Pfad `/opt/weltgewebe/policies/limits.yaml` für den Volume-Mount in der Datei
`infra/compose/compose.prod.override.yml` wurde durch dynamische Interpolation mittels der `REPO_DIR`-Variable ersetzt
(`${REPO_DIR:-/opt/weltgewebe}/policies/limits.yaml`).
Dies ermöglicht ein flexibles Deployment in beliebigen Verzeichnissen, wenn `weltgewebe-up` die `REPO_DIR` exportiert,
während die Rückwärtskompatibilität zum bisherigen Standardpfad (`/opt/weltgewebe`) für native Docker Compose Aufrufe
erhalten bleibt.

**Risiko:** Niedrig. (Flexibilisierung des Deployments ohne funktionale Änderungen für bestehende Setups).

## 2026-04-04 - Entfernung des Legacy-Alias /auth/login/consume

**Beschreibung:**

Der temporäre Legacy-Alias `/auth/login/consume` wurde entfernt. Der Magic-Link-Consume-Pfad läuft nun ausschließlich über `/auth/magic-link/consume`.

**Risiko:** Niedrig.

- 2026-07-22: Audit remediation T023-T026 hardens Caddy CSP and security-header handling across default, Heim and VPS edge configurations; deployment contract acknowledgement for the coordinated infra/caddy change set.
