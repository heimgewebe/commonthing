---
id: deploy.heimserver.integration
title: Heimserver Integration
doc_type: reference
status: deprecated
summary: Historischer Heimserver-Integrationsvertrag; nicht mehr Produktionsziel.
relations:
  - type: relates_to
    target: docs/deploy/README.md
  - type: relates_to
    target: docs/deploy/heimserver.deployment.md
  - type: relates_to
    target: docs/deployment.md
---

> [!WARNING]
> Der Heimserver ist kein aktives Produktionsziel mehr. Kanonische Produktion
> ist `commonserver` über den Public-VPS-Pfad. Dieses Dokument bleibt nur als
> historischer/Legacy-Kontext erhalten.

# Weltgewebe API – Heimserver Integration

## 0. Zweck

**Status:** Temporäre Integrationsumgebung.
Dieses Dokument beschreibt die Integration der API im Rahmen der aktuellen Entwicklungsphase auf dem Heimserver.
Es definiert den Contract, nicht die endgültige Produktionsinfrastruktur.

Beschreibt die referenzielle Integration der Weltgewebe-API auf dem Heimserver.
Dieses Dokument ist ein normativer Contract; die Heimserver-Enforcement-Details liegen außerhalb des Repos.

> **Hinweis:** Dieses Repo definiert nur den Contract; DNS/Caddy-Enforcement liegt im ops/heimserver Repo.

**Nicht enthalten:**

* Enforcement (Firewall/WireGuard/Router) ist out of scope dieses Repos.

**Nur:**

* API-Entry-Point
* DNS
* Reverse Proxy
* TLS
* CORS

---

## 1. Kanonischer API-Endpunkt

`https://api.weltgewebe.home.arpa`

**Invarianten:**

* Kein `.home` als TLD (erlaubt ist ausschließlich `.home.arpa`; RFC 8375).
* Kein `.lan`
* Kein `weltgewebe.api`
* Kein direkter Port-Zugriff (Direct :8080 access is not part of the supported contract; use reverse proxy FQDN).
* Kein http-only Betrieb

---

## 2. Reverse-Proxy-Vertrag

### Frontend-Auslieferung im Heimserver-Betrieb

* Die UI wird im Heimserver-Betrieb nicht durch den internen Stack-Caddy ausgeliefert
* Stattdessen erfolgt die Auslieferung über einen externen Edge-Gateway (z. B. edge-caddy)
* Das Weltgewebe-Repo stellt:
  * Build-Artefakte (`apps/web/build`)
  * Referenzkonfigurationen (`infra/caddy/...`)
* Die operative Frontdoor liegt außerhalb des Repos

**Kritisch:** Ein erfolgreicher Build im Repo ist nicht ausreichend. Der neue Frontend-Stand
wird erst wirksam, wenn die externe Edge-Instanz den aktualisierten Build neu einliest
(z. B. durch Container-Recreate oder Reload).

#### Konfigurationsvertrag: `DEPLOY_FRONTEND_MODE` und `EDGE_GATEWAY_CONTAINER`

Zur Steuerung der Frontend-Relevanz und der Edge-Aktualisierung wertet das Deploy-Skript
die Variablen `DEPLOY_FRONTEND_MODE` und `EDGE_GATEWAY_CONTAINER` (Default: `edge-caddy`) aus.

Zulässige Werte für `DEPLOY_FRONTEND_MODE`:

* **`auto` (Default):** Nutzt eine Heuristik. Explizites `BUILD_WEB=no` deaktiviert die
  Frontend-Relevanz vorrangig. Andernfalls gilt das Frontend als deploy-relevant (`REQUIRE_FRONTEND=1`),
  wenn der interne Caddy aktiv ist oder das Verzeichnis `apps/web` existiert. Dies bedeutet lediglich,
  dass Build-Artefakte erzeugt werden müssen. Build-Artefakte zu erzeugen heißt nicht automatisch,
  dass sie schon operativ ausgeliefert werden. Das Skript garantiert nicht die operative Auslieferung durch
  den Stack selbst. Zusätzlich wird heuristisch (naming-dependent) geprüft, ob ein externer Container
  entsprechend `EDGE_GATEWAY_CONTAINER` läuft; ist dies der Fall, wird ein best-effort Edge-Aktualisierungs-Pfad
  ausgelöst (mit Warnung, dass eine explizite Konfiguration bevorzugt wird).
* **`edge`:** Explizite, kanonische Deklaration der externen Liefertopologie. Das Frontend ist relevant,
  und Edge-Aktualisierungs-Checks (inklusive Recreate-Guard) werden zwingend und normativ ausgeführt.
  **Wichtig:** Der Edge-Gateway wird in diesem Modus verbindlich erwartet. Falls der Container fehlt oder
  ein Mount-Drift festgestellt wird, versucht der Guard zunächst aktiv einen Reparaturpfad (Recreate).
  Erst wenn dieser Reparaturpfad scheitert, schlägt das Deployment mit einem Fehler fehl.
* **`internal`:** Die UI wird durch den Stack-internen Caddy ausgeliefert. Das Frontend ist relevant,
  Edge-Checks werden übersprungen. Setzt zwingend voraus, dass der stack-interne Caddy aktiviert ist
  (`ENABLE_CADDY=1` oder `--with-caddy`); andernfalls bricht das Deployment mit einem Fehler ab.
* **`off`:** Das Frontend ist für diesen Deploy irrelevant (z. B. reine API-Umgebung).
  Frontend-Build und Edge-Checks werden übersprungen.

**Hinweis zum Edge-Refresh-Pfad:**
Die Variable `EDGE_GATEWAY_CONTAINER` parametrisiert lediglich die Runtime-Erkennung und Mount-Prüfung
des Edge-Gateways. Der automatische Reparaturpfad (Recreate) bleibt jedoch weiterhin fest an die kanonische
Heimserver-Edge-Topologie unter `/opt/heimgewebe/edge` gebunden und macht nicht den gesamten Edge-Stack
beliebig konfigurierbar.

Der Reverse-Proxy (Edge-Caddy) läuft im Heimserver-Betrieb außerhalb des Weltgewebe-Stacks.
`docs/reference/caddy.heimserver.caddy` und `infra/caddy/Caddyfile.heim` dienen hierbei primär
als Referenzkonfigurationen für das Routing. Die operativ wirksame Frontdoor wird im Heimserver-Repository
konfiguriert.

*(Hinweis: Für lokale Test-/Debug-Szenarien ohne Edge-Infrastruktur kann der Stack-Caddy über
`infra/compose/compose.heimserver.override.yml` angewiesen werden, die Referenzkonfiguration
lokal zu mounten. In Produktion routet der Edge-Caddy.)*

```caddy
api.weltgewebe.home.arpa {
    reverse_proxy api:8080
    tls internal
}
```

> **Hinweis:** Der Upstream-Name (z.B. `api`) ist exemplarisch und folgt der jeweiligen Compose-Benennung.

**Upstream darf:**

* Nicht öffentlich gebunden sein
* Nur Container-intern erreichbar sein
* Host-Match erforderlich (sonst 404/Drift).

---

## 3. TLS-Policy

* `tls internal`
* Interne CA
* Keine öffentliche ACME-Integration

---

## 4. CSRF / Origin-Policy

Die Weltgewebe-API verwendet einen CSRF-Origin-Check für
session-authentifizierte mutierende Requests.

Die kanonische Browser-Origin der Weltgewebe-UI ist:

`https://weltgewebe.home.arpa`

Wenn ein Session-Cookie vorhanden ist, erzwingt die Runtime einen
CSRF-Origin-Check: `Origin` oder `Referer` müssen mit dem Host der API
übereinstimmen.

Abweichende Origins sind kein Teil des kanonischen Deployment-Contracts
und dürfen nur über eine explizite Runtime-Konfiguration
(`CSRF_ALLOWED_ORIGINS`) zugelassen werden.

**Nicht vorgesehen:**

* Wildcard-Origin (`*`)
* mehrere parallele Browser-Domains
* Cross-System-Origins (z.B. `leitstand.heimgewebe.home.arpa`)

> **Hinweis:** Andere Heimgewebe-Systeme (z.B. Leitstand) gehören nicht zum
> Weltgewebe-Deployment-Contract. Falls solche Systeme API-Zugriffe
> benötigen, sollten diese über authentifizierte Service-Aufrufe oder
> interne APIs erfolgen und nicht über Browser-CORS.

---

## 5. Runtime-Annahmen

Die API erwartet:

* DNS-Auflösung über Pi-hole
* Kein Splitbrain
* Keine parallelen Domains
* Kein HTTP/3 ohne Dokumentation

---

## 6. Drift-Indikatoren

| Symptom | Ursache |
| :--- | :--- |
| TLS Fehler | falscher Host |
| CORS Fehler | falsche Domain |
| 502 | Container nicht im selben Docker-Netz |
| 404 | Host-Mismatch |

---

## 7. Essenz

API ist kein Server.
Sie ist ein hinter dem Proxy lebender Dienst mit exakt einem kanonischen Namen.

---

## 8. Contract Checks

* HTTPS only + `tls internal`
* FQDN Host-Match (404 bei falschem Host)
* Kein direkter :8080 Zugriff (Proxy-FQDN ist der Contract)

---

## 9. Data Seeding (Heimserver)

Die Heimserver-Umgebung aktiviert explizit `GEWEBE_SEED_DEMO=true`.
Dies stellt sicher, dass für Integrationstests und manuelle Prüfung definierte Test-Entitäten
(z.B. Garnrolle, Knoten) vorhanden sind.

---

## 10. SMTP Authentication Policy

Der API-Mailer unterstützt eine explizite Steuerung der SMTP-Authentifizierung via `SMTP_AUTH`.
Dies verhindert Authentifizierungsfehler bei lokalen Relays (z.B. auf Port 1025), wenn Credentials konfiguriert,
aber nicht gewünscht sind.

* `auto` (Default): Nutzt Credentials (`SMTP_USER`/`SMTP_PASS`), wenn diese vorhanden sind.
* `on`: Erzwingt Authentifizierung; Fehler, wenn Credentials fehlen.
* `off`: Ignoriert Credentials, auch wenn diese gesetzt sind (nützlich für lokale Dev-Relays).

**Beispiel (Mailpit/Mailhog):**
`SMTP_HOST=mailpit`, `SMTP_PORT=1025`, `SMTP_AUTH=off`

---

## 11. Interne DNS-Identität

Invariante: Der interne Docker-DNS-Name `weltgewebe-api` MUSS stabil auflösbar sein.
Edge Caddy und andere Dienste hängen davon ab.
Service-Suffixe wie `weltgewebe-api-1` sind nicht zulässig als Routing-Ziel.
Diese Invariante wird durch Guard + weltgewebe-up verifiziert.
In Ausnahmefällen kann die Verifikation im Deploy per ALLOW_UNSTABLE_API_DNS=1 übersteuert werden (nicht empfohlen).
