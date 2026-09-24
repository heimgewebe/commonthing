---
id: specs.auth-blueprint
title: Auth Blueprint
doc_type: reference
status: active
summary: Ältere Implementierungslinie für Auth (an ADR-0005 gebunden), abgelöst durch ADR-0006.
relations:
  - type: relates_to
    target: docs/adr/ADR-0006__auth-magic-link-session-passkey.md
  - type: relates_to
    target: docs/adr/ADR-0005-auth.md
  - type: relates_to
    target: docs/blueprints/auth-roadmap.md
---
# Blaupause: Schrittweise Implementierung von Account- und Login-Logik in commonThing

> **Hinweis:** Diese Blaupause ist eine ältere, an [ADR-0005](../adr/ADR-0005-auth.md)
> gebundene Implementierungslinie und nicht mehr das kanonische Zieldokument.
> Siehe [ADR-0006](../adr/ADR-0006__auth-magic-link-session-passkey.md) und die
> [Auth Roadmap](../blueprints/auth-roadmap.md) für die aktuelle Architektur.
>
> Ziel dieses Dokuments ist eine **dump- und ADR-konforme**, schrittweise Abarbeitung
> von **Account → Auth → Login → Rollen → Schutzmechanismen**.
>
> Reihenfolge ist kein Stilmittel, sondern Architektur.

## Status & Geltungsbereich

- **Typ:** Implementierungsblaupause
- **Bindung:** ADR-0005
- **Verändert keine bestehenden Contracts**
- **Zielgruppe:** Backend, Frontend, Review
- **Implementierungs-Status:** Phase 2 war vorhanden. Implementiert: Phase 3 (Session),
  Phase 4 (Middleware), Phase 5 (Write-Gates), Phase 6 (Dev-Login), Phase 7 (Frontend-Minimum),
  Phase 8 (Hardening). **Status: Abgeschlossen.**

> Diese Blaupause definiert **keinen neuen Contract**, sondern beschreibt
> die schrittweise Aktivierung bereits beschlossener Architektur.

---

## 0. Ausgangslage (aus dem Weltgewebe-Dump)

### Was bereits existiert

- **Accounts als Domänenobjekte**
  - Laden aus `GEWEBE_IN_DIR` (`demo.accounts.jsonl`)
  - Öffentliche Projektion (`AccountPublic`)
  - **Privacy-Invariante:** interne `location` wird **nie** ausgeliefert, nur `public_pos`
- **ADR-0005 (Auth & Rollen)**
  - Cookie-basierte Sessions (kein JWT-first)
  - Rollen: `Gast`, `Weber`, `Admin`
  - Gast = angemeldeter Mitwirkender mit eigenen Webungsrechten
- **Auth-Middleware existiert**, ist aber explizit **Platzhalter** (lässt alles durch)
- **Keine `/auth/*`-Routen**
- Schreibpfade (z. B. `PATCH /nodes/:id`) sind aktuell **ungeschützt**

### Zentrale Leitplanke

> **Login ist UX.
> Auth ist Infrastruktur.
> Accounts sind Domäne.**
>
> Wir bauen sie in genau dieser Reihenfolge.

---

## 1. Phase: Invarianten festschreiben (Vorarbeit)

### Ziel: Invarianten

Verhindern, dass spätere Implementierung bestehende Architektur bricht.

### Nicht verhandelbare Invarianten

1. **Session-basiertes Auth-Modell**
   - Server hält Sessions
   - Cookie enthält nur Session-ID
2. **Cookie-Policy**
   - `HttpOnly = true`
   - `SameSite = Strict`
   - `Secure = true` (immer! Dev läuft über HTTPS oder localhost)
3. **Rollenmodell**
   - nicht angemeldet → öffentlich lesen
   - `Gast` → eigene Webungsaktionen und eigene Inhalte pflegen
   - `Weber` → zusätzlich fremde gemeinschaftliche Inhalte pflegen
   - `Admin` → administrativ und moderativ
4. **Privacy bleibt erhalten**
   - `/auth/me` liefert **keine** internen Account-Felder
   - Orientierung an `AccountPublic` oder Minimal-Subset

### Artefakt

- **Optional, noch nicht vorhanden:** kurzer Zielabschnitt in `docs/auth.md` oder Ergänzung ADR-0005:
  > „Diese Invarianten dürfen durch keine Auth-Implementierung verletzt werden.“

---

## 2. Phase: Account-Grundlage (Domäne, kein Login)

### Ziel: Account-Grundlage

Accounts sind **existierende Identitäten**, nicht Login-Artefakte.

### Scope: Account-Grundlage

- Accounts bleiben:
  - seeded (Datei-basiert)
  - ohne Registrierung
- Keine Passwörter
- Keine Auth-Logik

### Ergebnis: Account-Grundlage

- Accounts können referenziert werden
- Rollen sind Account-Eigenschaften
- Auth kann sich später **darauf beziehen**, ohne Accounts zu verändern

> **Wichtig:**
> In dieser Phase wird _kein_ Login gebaut.

---

## 3. Phase: Session-Kern (Backend-Infrastruktur)

### Ziel: Session-Kern

Technische Auth-Wirkung herstellen – unabhängig vom Frontend.

### To-dos: Session-Kern

- SessionStore (Dev-Start):
  - In-Memory
  - explizit flüchtig (Neustart = Logout)
- Session-Datensatz:
  - `session_id`
  - `account_id`
  - `expires_at`
  - _(Rolle wird dynamisch via `account_id` geladen)_
- Neue Routen:
  - `POST /auth/login` (Dev-Mechanik)
  - `POST /auth/logout`
  - `GET /auth/me`

### `/auth/me` – Rückgabe (Minimal)

```json
{
  "authenticated": true,
  "role": "weber",
  "account": {
    "id": "...",
    "name": "..."
  }
}
```

> ⚠️ `/auth/me` ist **kein Account-Endpoint**.
> Er liefert Auth-Status, nicht Account-Wahrheit.

### Ergebnis: Session-Kern

- Server kann Identität **merken**
- Browser trägt nur ein Cookie
- Noch keine Autorisierung

---

## 4. Phase: Auth-Middleware realisieren

### Ziel: Auth-Middleware

Jede Anfrage bekommt einen **AuthContext**.

### To-dos: Auth-Middleware

- Platzhalter-Middleware ersetzen
- Ablauf:
  1. Cookie lesen
  2. Session validieren
  3. AuthContext setzen

- Fallback:
  - kein Cookie / ungültig → `role = Gast`

### Ergebnis: Auth-Middleware

- **Zentrale Wahrheit** pro Request:

  ```rust
  AuthContext {
    role,
    account_id?,
  }
  ```

> Ab hier existiert echte Auth-Information im System.

---

## 5. Phase: Autorisierung (Gates auf Schreibpfade)

### Ziel: Autorisierung

Login hat **reale Konsequenzen**.

### To-dos: Autorisierung

- Gates auf schreibenden Endpunkten:
  - z. B. `PATCH /nodes/:id`

- **CSRF-Minimum (erste Schutzlinie):**
  - `SameSite=Strict` (via Cookie)
  - Origin/Referer-Check für state-changing Requests

- Regel:
  - **401 Unauthorized** → keine gültige angemeldete Sitzung
  - **403 Forbidden** → authentifiziert, aber für die konkrete Handlung nicht berechtigt
  - Gespräche und sonstige ausdrücklich freigegebene Webungsaktionen → jeder angemeldete Account
  - Veto und Abstimmung bei fremden Weberanträgen → `Weber/Admin`
  - formale Selbstentscheidung über den eigenen Weberantrag → ausgeschlossen
  - fremde gemeinschaftliche Inhaltspflege → `Weber/Admin`

### Ergebnis: Autorisierung

- Ohne Login: read-only
- Mit Login: schreibfähig

> **Ohne diese Phase ist Login wertlos.**

---

## 6. Phase: Dev-Login-Mechanik (kontrolliert)

### Ziel: Dev-Login

Entwicklung ermöglichen, ohne Registrierung.

### Scope: Dev-Login

- Login z. B. über:
  - `account_id`
  - oder Handle

- Optional:
  - `GET /auth/dev/accounts` (nur Dev, nur Public-Daten)

### Schutz: Dev-Login

- Feature-Flag:
  - `AUTH_DEV_LOGIN=1` (oder `true`)
  - Ausgeschaltet bei `0`, `false` oder ungesetzt.

- In Prod:
  - Route deaktiviert oder 404 (selbst wenn Flag versehentlich gesetzt).

### Ergebnis: Dev-Login

- Reproduzierbare Dev-Identitäten
- Kein Sicherheitsleck Richtung Prod

---

## 7. Phase: Frontend-Minimum

### Ziel: Frontend-Minimum

UX sichtbar machen, nicht perfektionieren.

### To-dos: Frontend-Minimum

- Beim App-Start:
  - `GET /auth/me`

- UI-Zustände:
  - Gast
  - Eingeloggt (Rolle sichtbar)

- Buttons:
  - Login (Dev)
  - Logout

### Ergebnis: Frontend-Minimum

- Benutzer sieht, **wer er ist**
- UI reagiert auf Rollen

---

## 8. Phase: Hardening & Vorbereitung auf Prod

### Ziel: Hardening

Dev-Abkürzungen sauber absichern.

### To-dos: Hardening

- Session TTL + Cleanup
- Feature-Flags prüfen
- Cookie-Secure nur bei HTTPS
- Erste CSRF-Überlegungen (Cookie-Auth!)

### Dev-Login Hardening

- **Guardrail:** `/auth/dev/accounts` zusätzlich auf `localhost` einschränken oder explizites
  `AUTH_DEV_LOGIN_ALLOW_REMOTE` Flag fordern.
- **Auditability:** Zugriff auf Dev-Endpunkte loggen (Warn-Level), um versehentliche Aktivierung sichtbar zu machen.
- **UX:** `window.location.reload()` im Frontend durch reaktive `checkAuth()`-Updates ersetzen (vermeidet State-Verlust).

### Ergebnis: Hardening

- Kein stilles Durchrutschen von Dev-Auth in Prod
- Saubere Basis für spätere:
  - Registrierung
  - Passkeys / OAuth
  - Invite-Flows

---

## Architektur-Nachtrag: Daten-Cache & Persistenz

Seit **Optimierung der List-Endpoints** gilt:

- **Cache ist Wahrheit:** Die API (`ApiState`) hält den aktuellen Datenbestand (z. B. Nodes) im Arbeitsspeicher (`Arc<RwLock<Vec<Node>>>`).
  - Alle Lese-Zugriffe (`GET /nodes`, `GET /nodes/:id`) werden aus dem Cache bedient.
- **Datei ist Persistenz:** Die JSONL-Datei (`demo.nodes.jsonl`) dient ausschließlich der Persistenz über Neustarts hinweg.
- **Dual-Write:** Schreiboperationen (`PATCH`) aktualisieren zuerst die Datei (atomar) und bei Erfolg sofort den Cache.
- **Read-Your-Writes:** Während `PATCH /nodes/:id` wird innerhalb einer Instanz der Nodes-Read-Zugriff kurz blockiert (`RwLock`),
  um read-your-writes zu garantieren.
- **Konsequenz:** Externe Änderungen an der Datei (z. B. durch Deployment oder manuelles Editieren) werden von der
  laufenden API **nicht** erkannt. Ein Neustart des API-Prozesses ist erforderlich, um externe Änderungen zu laden.

### Observability & Metriken (Locking)

Um die Performance der Cache-Konsistenz zu überwachen, werden bei Schreibvorgängen (`PATCH`) folgende Zeiten gemessen und
geloggt:

- **`lock_contention_ms`**: Zeit, die der Request warten musste, um den exklusiven Schreib-Lock (`RwLock::write`) zu
  erhalten.
  - Hohe Werte deuten auf Konkurrenz durch viele gleichzeitige Reads oder Writes hin.
- **`lock_hold_ms`**: Zeit, die der Schreib-Lock gehalten wurde.
  - Dies umfasst: File-I/O (Persistenz) und Cache-Update.
  - Während dieser Zeit sind **alle** Lesezugriffe auf Nodes blockiert (Read-Your-Writes Garantie).
- **`persist_ms`**: Reine Dauer der Datei-Operationen (Write + Rename).

Zusätzlich existiert eine Prometheus-Metrik `nodes_cache_count` (Gauge), die die aktuelle Anzahl der Nodes im
Arbeitsspeicher anzeigt.

---

## Explizit nicht Teil dieser Blaupause

- Registrierung / Signup
- Passwort- oder Passkey-Design
- OAuth / externe Identitäten
- Ownership-Logik auf Node-Ebene

---

## Typische Fehler (präventiv markiert)

- ❌ Login bauen **vor** Middleware
- ❌ `/auth/me` leakt interne Account-Felder
- ❌ Dev-Login ohne Feature-Flag
- ❌ Secure-Cookie im lokalen HTTP
- ❌ Auth ohne Gates (Scheinsicherheit)

---

## Verdichtete Essenz

> Wir bauen **erst Wirkung**, dann Komfort.
>
> **Accounts → Sessions → Middleware → Gates → Login → UX → Hardening**
>
> Alles andere ist Kosmetik.

---

## Unsicherheitsgrad & Ursachen

**Unsicherheitsgrad:** 0.21 (niedrig)

### Ursachen

- Persistenzform von Sessions (Memory vs Datei/DB) ist bewusst offen
- Frontend-Details nicht vollständig spezifiziert
- ADR lässt Spielraum bei konkreter Login-Mechanik

Diese Unsicherheit ist **produktiver Spielraum**, kein Architekturrisiko.

---

## Abschlussfrage (∴fore)

1. Ist dies die kritischste Abfolge?
   → Ja, weil sie Scheinsicherheit systematisch verhindert.
2. Was fehlt noch?
   → Ownership-Regeln („eigene Inhalte“) und Registrierungslogik – **bewusst nachgelagert**.
