---
id: blueprints.auth-roadmap
title: Auth Roadmap
doc_type: roadmap
status: active
summary: >
  Exekutive Roadmap zur schrittweisen Kanonisierung, Verifikation und
  Vollendung der Auth-Architektur in commonThing.
relations:
  - type: relates_to
    target: docs/adr/ADR-0006__auth-magic-link-session-passkey.md
  - type: relates_to
    target: docs/adr/ADR-0007__auth-persistence-production-db-path.md
  - type: relates_to
    target: docs/reports/auth-status-matrix.md
  - type: relates_to
    target: docs/specs/auth-blueprint.md
---

# Auth Roadmap – commonThing

> Diese Roadmap ist der exekutive Pfad für Auth in commonThing.
> Sie ergänzt den normativen Zielrahmen aus ADR-0006 und den zugehörigen
> Specs um Reihenfolge, Stop-Kriterien, Drift-Schutz und
> Implementierungsprioritäten.
>
> Ziel ist nicht, Auth neu zu erfinden, sondern:
> **alte und neue Wahrheitsschichten zu ordnen, Runtime gegen Zielzustand
> zu prüfen und danach die realen Lücken systematisch zu schließen.**

## 0. Ziel dieses Dokuments

Diese Roadmap dient zugleich als:

- Integrationspunkt für alle Auth-Dokumente
- exekutiver Pfad zwischen Soll und Ist
- Drift-Detektor zwischen ADRs, Specs, Runtime und Betrieb
- Priorisierungsgrundlage für Implementierung und Review

---

## 1. Kanonische Referenzen

### Führender Zielrahmen

Diese Dokumente definieren den kanonischen Zielzustand:

- `docs/adr/ADR-0006__auth-magic-link-session-passkey.md`
- `docs/adr/ADR-0007__auth-persistence-production-db-path.md`
- `docs/specs/auth-api.md`
- `docs/specs/auth-state-machine.md`
- `docs/specs/auth-ui.md`

### Basis / Historie

Diese Dokumente bleiben relevant, sind aber nicht mehr alleiniger Zielrahmen:

- `docs/adr/ADR-0005-auth.md`
- `docs/specs/auth-blueprint.md`

### Brückendokumente

Diese Dokumente müssen berücksichtigt, aber nicht mit der Zielarchitektur verwechselt werden:

- `docs/blueprints/weltgewebe.auth-and-ui-routing.md`
- `docs/blueprints/auth-persistence-runtime-proof.md`
- `docs/index.md`

### Betriebs- und Kontextbelege

- `docs/runbook.md`
- `apps/api/README.md`

### Runtime- / Belegquellen

Diese Artefakte liefern reale Nachweise des implementierten Zustands:

- `apps/api/src/routes/auth.rs`
- `apps/api/src/auth/session.rs`
- `apps/api/src/mailer.rs`
- `apps/web/src/routes/login/+page.svelte`
- `apps/web/src/lib/auth/store.ts`
- `verification/verify_magic_link.py`

### Diagnoseartefakte

- `docs/reports/auth-status-matrix.md`
- `docs/reports/auth-status-matrix.json`

---

## 2. Leitprinzipien

- Magic Link ist Einstieg und Recovery, nicht die gesamte Auth-Architektur.
- Session ist der primäre Alltagszustand.
- Passkey ist optionaler Komfort- und Sicherheitsgewinn, nicht Pflicht.
- Step-up Auth ist aktionsgebunden und session-neutral.
- Auth ist strikt getrennt von Garnrollen-Verortung und öffentlicher Sichtbarkeit.
- Keine Architekturentscheidung ohne Runtime-Beleg oder explizite Offen-Markierung.
- Kein Feature-Ausbau auf unstabiler Session-Basis.
- Drift-Sichtbarkeit geht vor Vollständigkeitsrhetorik.

---

## 3. Statuslogik

Alle Auth-Bereiche werden in der Statusmatrix mit genau einem der folgenden Zustände geführt:

- `OK` — durch Code, Test oder Runtime ausreichend belegt
- `Teil` — teilweise belegt, aber nicht end-to-end
- `Offen` — Zielzustand dokumentiert, Runtime-Beleg fehlt
- `Drift` — Docs widersprechen sich oder der Runtime

### Mindestregel für OK

Ein Bereich darf nur dann als `OK` gelten, wenn mindestens zwei der folgenden Belegtypen vorliegen:

- Route / Code
- Test / Verification
- Runbook / Betriebsbeleg
- Runtime-Proof / Smoke / Log / UI-Nachweis

---

## 4. Phase 0 — Kanonisierung und Drift-Stopp

**Ziel:** Eine eindeutige Auth-Wahrheitsordnung herstellen.

### Maßnahmen

1. `ADR-0006` als führendes Auth-Zieldokument explizit behandeln.
2. `ADR-0005` als historisches Fundament / Minimalbasis markieren.
3. `auth-blueprint.md` als ältere, an `ADR-0005` gebundene Implementierungslinie kenntlich machen.
4. `weltgewebe.auth-and-ui-routing.md` als Routing-/Diagnose-Blueprint einordnen, nicht als Endarchitektur.
5. `docs/index.md` und weitere Übersichtsseiten so anpassen, dass keine gleichrangigen Auth-Wahrheiten nebeneinander stehen.

### Artefakte

- diese Roadmap
- `docs/reports/auth-status-matrix.md`
- `docs/reports/auth-status-matrix.json`

### Stop-Kriterium für Phase 0

- Es ist eindeutig sichtbar, welches Dokument Zielrahmen ist.
- Historische Dokumente sind weiterhin referenzierbar, aber nicht mehr missverständlich führend.

---

## 5. Phase 1 — Ist-Zustand gegen Zielzustand beweisen

**Ziel:** Keine Annahmen, nur belegter Zustand.

### Pflichtachsen

- Magic-Link-Request / Consume
- Session-Check
- Session-Refresh
- Logout / Logout-All
- Device-Liste / Device-Removal
- Passkey register / auth / list / remove
- Step-up challenge / request / consume
- UI-Reaktionen auf Session, Step-up, Devices, Passkey
- Sicherheitsmechaniken:
  - Anti-Enumeration
  - CSRF / Origin
  - Trusted Proxy
  - Rate Limit
  - session-neutraler Step-up-Link

### Artefakt für Phase 1

- `docs/reports/auth-status-matrix.md`
- `docs/reports/auth-status-matrix.json`

### Stop-Kriterium für Phase 1

Jeder relevante Bereich ist entweder:

- mit Belegen als `OK` / `Teil` markiert,
- oder explizit als `Offen`,
- oder als `Drift` mit benanntem Widerspruch.

---

## 6. Phase 2 — Session- und Device-Modell vervollständigen

**Ziel:** Vom Magic-Link-MVP zur alltagstauglichen Auth.

> **Wichtig ab Phase 2:** Für jeden Teilbereich gilt ab hier „Diagnose vor Umsetzung“. Ab Phase 2 wird die Statusmatrix zunächst zur Orientierung herangezogen. Maßgeblich für jede Umsetzung bleiben jedoch die konkreten Repo-, Code-, Test- und Runtime-Belege. Erst nach diesem Abgleich wird implementiert oder verfeinert.

### Arbeitspakete Phase 2

- [x] `GET /auth/session` belegen oder implementieren — Route aktiv, Tests in `api_auth.rs`
- [x] `POST /auth/session/refresh` belegen oder implementieren — Route aktiv, Tests in `api_auth.rs`
- [x] Device-ID-Strategie vereinheitlichen — dynamische `device_id` pro Session
- [x] Device-Liste bereitstellen — `GET /auth/devices` aktiv
- [x] Current-Device-Markierung einführen — `current` Flag in Device-Liste
- [x] `DELETE /auth/devices/:id` — Self-Delete aktiv, Fremdgeräte-Guard erzeugt Challenge
- [x] `POST /auth/logout-all` — Challenge-Erzeugung aktiv, Consume-Pfad in Phase 3 implementiert
- [x] Session-Persistenzentscheidung explizit festgezogen — In-Memory als bewusste Wahl für Single-Instance-Betrieb; Migrationspfad auf persistenten Store architektonisch vorgesehen; konkrete Persistenzanbindung bleibt nachzuliefern; siehe Begründung unten

### Risiken

- inkonsistente Session-Realität
- fehlende Gerätehoheit
- spätere Sicherheits- und UX-Drift

### Persistenzentscheidung (Session)

**Entscheidung:** In-Memory `SessionStore` ist die bewusste Wahl für den aktuellen Single-Instance-Betrieb.

**Begründung:**

- Die Session-TTL (24 h) und das serverseitige State-Modell funktionieren im Single-Instance-Modus korrekt.
- `ApiState` enthält bereits ein optionales `db_pool: Option<PgPool>` als generelle DB-Infrastruktur; eine Session-spezifische Anbindung existiert noch nicht.
- Die `SessionStore`-Schnittstelle (`create`, `get`, `delete`, `list_by_account`, `delete_by_device`, `delete_all_by_account`) ist abstrakt genug, um ohne Route-Änderungen auf einen persistenten Adapter umgestellt zu werden.
- Challenge- und Token-Stores unterliegen derselben Einschätzung: kurzlebig (5 min TTL), sicherheitsunkritisch bei Verlust durch Restart.

### Produktionspfad für persistenten Store

**Entscheidung:** ADR-0007 legt den Produktionspfad für Auth-Persistenz auf direkten
PostgreSQL-Zugriff via `DATABASE_URL` fest.

**Folgen:**

- `DbSessionStore` / `SessionBackend` wird gegen den direkten SQLx/Postgres-Pfad geplant.
- PgBouncer bleibt Dev-/Spezialpfad und ist kein Produktions-Gate.
- Kein `DbSessionStore` ohne direkten SQLx/Postgres-Persistenzpfad-Nachweis.
- Eine spätere Rückkehr zu PgBouncer als Produktionspfad erfordert ein neues ADR.

**Trigger für Migration auf persistenten Store:**

- Multi-Instance-Deployment erforderlich
- Zero-Downtime-Restarts müssen Sessions bewahren
- Persistente Session-Audits benötigt

### Stop-Kriterium für Phase 2

- Session, Refresh, Logout-All und Devices sind nicht mehr `Offen`
- Device-Bindung ist technisch und dokumentarisch konsistent

---

## 7. Phase 3 — Step-up Auth vollständig realisieren

**Ziel:** Sensitive Aktionen sauber absichern.

### Arbeitspakete Phase 3

- [x] Challenge-Erzeugung — `ChallengeStore` in `apps/api/src/auth/challenges.rs`
- [x] TTL für Challenges — 5-Minuten-TTL, Cleanup bei Zugriff
- [x] Intent-Bindung — `ChallengeIntent::LogoutAll`, `RemoveDevice`
- [x] `POST /auth/step-up/magic-link/request` — separater `StepUpTokenStore`, defensive Fehlersemantik (400/500/503), Mailer-Pfad, Tests
- [x] `POST /auth/step-up/magic-link/consume` — konsumiert den Step-up-Token, validiert Challenge-Bindung und Session, führt den Intent aus: bei `LogoutAll` werden alle Sessions des Accounts beendet und das Cookie geleert; bei `RemoveDevice` wird nur das Zielgerät entfernt, die aktuelle Session bleibt erhalten
- [ ] Passkey als bevorzugter Step-up-Pfad
- [x] Minimaler Step-up-Consume-Pfad in der UI
- [x] Fehlerpfade im Step-up-Request ohne unnötigen Session-Abbruch — der Request-Pfad invalidiert keine bestehende Session
- [x] Nachweis, dass Step-up keine neue allgemeine Session erzeugt — der Consume-Handler erstellt in keinem Pfad eine Session; `LogoutAll` löscht alle Account-Sessions (inkl. der aktuellen), `RemoveDevice` löscht nur die Sessions des Zielgeräts; beide Invarianten sind durch `test_step_up_consume_logout_all_success` und `test_step_up_consume_remove_device_success` in `apps/api/tests/api_auth.rs` belegt

### Nicht verhandelbare Regel

Step-up bleibt aktionsgebunden und session-neutral.

### Stop-Kriterium für Phase 3

- `challenge_id`-Pfad ist end-to-end nachgewiesen
- sensitive Aktionen können nicht mehr ohne Step-up ausgeführt werden

---

## 8. Phase 4 — Passkeys/WebAuthn realisieren

**Ziel:** Komfort und Sicherheit erhöhen, ohne Recovery zu opfern.

### Architekturentscheidung: WebAuthn-Identität

- Jeder Account erhält eine dedizierte `webauthn_user_id` (UUID v4).
- Diese wird **nicht** aus `account_id` abgeleitet, sondern unabhängig verwaltet.
- `account_id` bleibt die interne Fachidentität; `webauthn_user_id` ist die vom WebAuthn-Protokoll verwendete opake Nutzerkennung.
- `rp_id` und `rp_origin` kommen aus `AppConfig` (Env: `WEBAUTHN_RP_ID`, `WEBAUTHN_RP_ORIGIN`). Keine hardcodierten Defaults.
- **Persistenzstatus:** Der Wert wird aus der Datenquelle gelesen, wenn vorhanden. Bei Accounts ohne persistierten Wert wird er beim Laden erzeugt (Lazy Backfill) und ist für die Laufzeit des Prozesses stabil. Eine `AccountStore`-Mutation (`update_webauthn_user_id`) ist als Voraussetzung vorhanden; der tatsächliche Datenquellen-Writeback folgt mit `register/verify`.

### Arbeitspakete Phase 4

1. [x] Statusbeweis: Was existiert bereits?
2. [x] Register-Options (`POST /auth/passkeys/register/options`) — Endpunkt, Step-up-403, Grant-Handoff implementiert; `BeginPasskeyRegistration`-Consume erzeugt `registration_grant_id`; `register/options` konsumiert Grant und startet WebAuthn-Ceremony
3. [x] Register-Verify (`POST /auth/passkeys/register/verify`) — API-seitig implementiert mit echter `webauthn.finish_passkey_registration(...)`-Verifikation, single-use-Consume der `registration_id`, Credential-Speicherung über `PasskeyStore` (Duplicate-Detection → `409 CONFLICT`), `webauthn_user_id`-Writeback und session-neutralem Erfolgspfad (`200 OK`, kein Cookie). Negativpfade (401, 503, 400 unknown/mismatch/invalid credential, kein Set-Cookie) sind getestet. Passkey Register-Verify ist durch CI auf `main` belegt.
   Beleg: [Run 27487642565](https://github.com/heimgewebe/weltgewebe/actions/runs/27487642565), Commit `cc54460`, Workflow `auth-passkey-register-proof`, Conclusion `success`.
4. [x] Voraussetzungen für Register-Verify — PasskeyStore + Writeback-Mutation implementiert; `register/options` vollständiger Grant-Handoff implementiert
5. [ ] Auth-Options
6. [ ] Auth-Verify
7. [ ] Passkeys auflisten
8. [ ] Passkey entfernen
9. [ ] UI-Aktivierung erst nach erfolgreichem Login anbieten

### Aktueller Stand

- `webauthn_user_id` als dediziertes Feld am Account-Modell eingeführt (Lazy Backfill, prozessstabil; Writeback im Verify-Pfad implementiert, Datenquellen-Persistenz folgt mit persistenter Account-Ablage)
- WebAuthn-Konfiguration (`rp_id`, `rp_origin`, optional `rp_name`) aus `AppConfig` mit Validierung
- `Webauthn`-Instanz wird beim Start einmalig gebaut (optional, nur wenn konfiguriert)
- `PasskeyRegistrationGrantStore` (In-Memory, TTL 5 Min, single-use, account- und device-gebunden) eingeführt
- Register-Options-Endpunkt: ohne Grant fail-closed `403 STEP_UP_REQUIRED` mit `challenge_id`; mit gültigem Grant wird die WebAuthn-Ceremony gestartet (`200 OK` mit `registration_id` + `options`)
- Register-Verify-Endpunkt: prüft Session/Konfiguration, konsumiert `registration_id` single-use, verifiziert die Credential-Antwort über `webauthn.finish_passkey_registration(...)` (echte Krypto, kein Mock), legt das `Passkey` über `PasskeyStore.insert(...)` mit Duplicate-Detection ab, schreibt `webauthn_user_id` zurück und antwortet session-neutral mit `200 OK {"ok": true}` — kein Cookie, kein neuer Session-Token
- `BeginPasskeyRegistration`-Consume erzeugt einen kurzlebigen `registration_grant_id` (TTL 5 Min)
- In-Memory-Store für laufende Registrierungen (`PasskeyRegistrationStore`, TTL 5 Min) wird nach Grant-Consume aktiv genutzt
- Langlebiger In-Memory-`PasskeyStore` (account-gebunden, duplicate detection, list/find/remove)
- `AccountStore.update_webauthn_user_id(account_id, uuid)` für gezielten Writeback vorbereitet und im Verify-Pfad aktiv aufgerufen
- Step-up-Intent `BeginPasskeyRegistration` ergänzt (vollständiger Handoff mit Grant-Erzeugung und -Konsum)
- Unit- und Integrationstests belegen PasskeyStore, AccountStore-Writeback-Mutation, Grant-Handoff, fail-closed `register/options` ohne Grant, erfolgreichen Ceremony-Start mit Grant sowie alle dokumentierten Negativpfade von `register/verify` (401, 503, 400 unknown/mismatch/invalid credential, kein Session-Cookie); CSRF-Drift-Guard erfasst die neue Route
- Passkey Register-Verify ist durch CI auf `main` belegt.
  Beleg: [Run 27487642565](https://github.com/heimgewebe/weltgewebe/actions/runs/27487642565), Commit `cc54460`, Workflow `auth-passkey-register-proof`, Conclusion `success`.
- **Offen:** persistente Account-Datenquelle für den `webauthn_user_id`-Writeback, Passkey-Login-Flow (`auth/options`, `auth/verify`), Passkey-Management, UI

### Voraussetzungen

- stabile Session ✅
- definierter Geräte- und Step-up-Pfad ✅

### Stop-Kriterium für Phase 4

- Passkeys sind nicht mehr nur dokumentiert, sondern in Runtime und UI belegbar
- Magic Link bleibt Recovery-/Neugerät-Pfad

---

## 9. Phase 5 — UI von Minimal-Login zu Wiederkehr-UX ausbauen

**Ziel:** Alltagstauglichkeit.

### Arbeitspakete Phase 5

1. [x] Session-Status sichtbar machen — `apps/web/src/lib/components/AccountSection.svelte` zeigt Konto, Rolle und aktive Sitzung in `/settings`; Tests in `apps/web/tests/account-section.spec.ts`.
2. [ ] Zustand „Session abgelaufen“
3. [~] Passkey-Aktivierung mit verständlicher Erklärung — Eintragspunkt und Erklärtext stehen als deaktivierter Stub in der Settings-Sektion; CTA aktiviert sich erst, wenn Auth-Phase 4 (Register-Verify, Auth-Optionen, Auth-Verify) im Backend nachgewiesen ist.
4. [ ] Step-up-Dialog
5. [x] Geräteansicht / Geräteverwaltung — Read-only Liste der aktiven Geräte mit „Dieses Gerät"-Badge in Settings; Frontend ruft `GET /api/auth/devices` auf und markiert das aktuelle Gerät anhand des `current`-Flags. Schreiboperationen (Geräte-Removal) bleiben weiterhin step-up-getrieben über die bestehenden Endpunkte.
6. [ ] AuthStore / AuthStatus auf reale Zustände erweitern

### Leitfrage

Die UI darf nicht nur wissen:

- „eingeloggt / ausgeloggt“

Sie muss auch wissen:

- Session gültig?
- Step-up nötig?
- Passkey verfügbar?
- Device-Management erreichbar?

### Stop-Kriterium für Phase 5

- `auth-ui.md` ist in den wesentlichen Zuständen nicht mehr nur Soll, sondern durch UI-Pfade gedeckt

---

## 10. Phase 6 — Sensitive Intents finalisieren

**Ziel:** offene API-Semantik schließen.

### Kernpunkt gelöst

- `/me/email` (abgeschlossen über `PUT /auth/me/email` mit Doppel-Nachweis: Step-up-Request an die neue E-Mail beweist deren Besitz, Consume beweist Besitz des bestehenden authentifizierten Geräte-Kontexts (`account_id` + `device_id`); inkl. Validierung und Eindeutigkeits-Prüfung)

### Stop-Kriterium für Phase 6

- `/me/email` ist nicht mehr offene Architekturentscheidung
- Matrix führt diesen Punkt nicht mehr als `Offen`

---

## 11. Phase 7 — Reproduzierbare Sicherheits- und Betriebsnachweise aufbauen

**Ziel:** Sicherheit nicht nur besitzen, sondern reproduzierbar beweisen.

### Check-Gruppen

- Anti-Enumeration
- Trusted Proxy
- Rate Limit
- SMTP-/Magic-Link-Delivery
- CSRF / Origin
- Step-up Challenge
- Devices / Logout-All
- Passkey-Smoke (sobald vorhanden)

### Bisher belegt (reproduzierbar)

- **CSRF / Origin:** Systematische Abdeckung aller aktuell gelisteten
  CSRF-pflichtigen mutierenden Endpunkte —
  ein Cross-Site-Request mit Session-Cookie, aber ohne passenden
  Origin/Referer wird mit `403` (leerer Body) abgewiesen; eine Positivkontrolle
  mit gültigem Origin passiert die Middleware. Beleg:
  `apps/api/tests/auth_security_invariants.rs::csrf_blocks_all_mutating_endpoints_without_origin`.
- **Anti-Enumeration:** Bekannte und unbekannte E-Mail liefern identischen
  Status, einen byte-identischen Body und Parität der sicherheitsrelevanten
  Header (kein `Set-Cookie`-Seitenkanal, identischer
  `Content-Type`/`Cache-Control`) — ohne E-Mail-/Token-Leakage. Beleg:
  `apps/api/tests/auth_security_invariants.rs::magic_link_request_is_indistinguishable_for_known_and_unknown_email`.

Offen: Rate-Limit-Runtime-Smoke gegen einen laufenden Server,
SMTP-/Magic-Link-Delivery-Nachweis, Passkey-Smoke.

### Optionale spätere Absicherung

Automatisierte Guards oder Smokescripts sind eine mögliche spätere Absicherung (falls die manuelle Matrixpflege und Review-Praxis nicht mehr ausreichen), aber noch nicht zwingend als Architektur-Artefakt beschlossen.

### Stop-Kriterium für Phase 7

- die wichtigsten Auth-Invarianten sind reproduzierbar nachweisbar

---

## 12. Phase 8 — Dokumentationsdrift bereinigen

**Ziel:** Nach der Umsetzung keine höflich widersprechenden Dokumente zurücklassen.

### Zu synchronisieren

- `docs/adr/ADR-0005-auth.md`
- `docs/adr/ADR-0006__auth-magic-link-session-passkey.md`
- `docs/specs/auth-blueprint.md`
- `docs/specs/auth-api.md`
- `docs/specs/auth-state-machine.md`
- `docs/specs/auth-ui.md`
- `docs/blueprints/weltgewebe.auth-and-ui-routing.md`
- `docs/runbook.md`
- `apps/api/README.md`
- `docs/index.md`

### Stop-Kriterium für Phase 8

- Keine relevante Auth-Datei behauptet still einen anderen Zielzustand als der führende Rahmen.

---

## 13. Empfohlene Reihenfolge

1. Kanonisierung / Drift-Stopp
2. Statusmatrix / Ist-Beleg
3. Session / Devices
4. Step-up Auth
5. Passkeys
6. UI-Wiederkehr
7. Sensitive Intents finalisieren
8. Reproduzierbare Sicherheits- und Betriebsnachweise
9. Dokumentations-Sync

---

## 14. Alternative Priorisierung: drift-risikogetrieben

Statt nach Features kann auch nach Lügenpotenzial priorisiert werden:

### Höchstes Drift-Risiko

- konkurrierende Auth-Dokumente
- Session / Devices
- Step-up
- `/me/email`

### Mittleres Drift-Risiko

- Passkeys
- Wiederkehr-UX

### Niedrigeres Drift-Risiko

- Formulierungs- und Glossarpolitur

---

## 15. Entscheidungsregel

Kein Ausbau von Passkeys, UI-Polish oder anderen Komfortpfaden, wenn:

- Session-Modell unklar,
- Step-up nicht belastbar,
- offene API-Fragen nicht sichtbar markiert,
- oder Drift zwischen Docs und Runtime unerklärt bleibt.

---

## 16. Essenz

Auth ist in commonThing kein einzelnes Feature.
Auth ist ein Systemzustand.

Der größte Fehler wäre, neue Auth-Teile zu bauen,
ohne den bestehenden Zustand sauber zu ordnen, zu belegen und zu schließen.
