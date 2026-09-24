---
id: reports.auth-status-matrix
title: Auth Status Matrix
doc_type: reference
status: active
summary: Wahrheitsfilter und Statusmatrix der Auth-Architektur (Alt-/Ist-Linie vs Ziel-/Soll-Linie) zur Erkennung von Architekturdrift.
relations:
  - type: relates_to
    target: docs/adr/ADR-0006__auth-magic-link-session-passkey.md
  - type: relates_to
    target: docs/adr/ADR-0007__auth-persistence-production-db-path.md
  - type: relates_to
    target: docs/blueprints/auth-roadmap.md
---

# Auth Status Matrix – commonThing

Status: aktiv
Zweck: Verifikation der Auth-Architektur gegen ADR-0006 + Specs
Letzte Aktualisierung: 2026-07-10 (Issue #1378, persistente Browser-Sessions)
Pflegeregel: Diese Matrix ist bei jedem Auth-bezogenen PR zu aktualisieren, der Zielrahmen, Runtime-Verhalten oder Sicherheitsinvarianten verändert.

> Diese Matrix dient als Diagnoseartefakt zur Roadmap.
> Sie ersetzt nicht den normativen Zielrahmen aus ADR-0006 und den zugehörigen Specs, sondern verdichtet deren Sollzustand gegen den belegten Ist-Zustand.
> Siehe: `docs/blueprints/auth-roadmap.md`

---

## 0. Referenzen & Wahrheitslinien

### Ziel-/Soll-Linie (Kanonischer Zielzustand)

Diese Dokumente beschreiben die finale Architektur, auf die hingearbeitet wird:

- `docs/adr/ADR-0006__auth-magic-link-session-passkey.md`
- `docs/adr/ADR-0007__auth-persistence-production-db-path.md`
- `docs/specs/auth-api.md`
- `docs/specs/auth-state-machine.md`
- `docs/specs/auth-ui.md`

### Brückendokumente / Alt-MVP-Linie

- `docs/blueprints/weltgewebe.auth-and-ui-routing.md`

### Alt-/Ist-Linie (Historische / Implementierte Basis)

Diese Dokumente beschreiben das minimale Fundament und bisher umgesetzte Schritte:

- `docs/adr/ADR-0005-auth.md`
- `docs/specs/auth-blueprint.md`

### Dokumentations- und Betriebsbelege

- `docs/runbook.md`

### Code-, Test- und Verifikationsbelege

- `apps/web/src/routes/login/+page.svelte`
- `verification/verify_magic_link.py`
- `apps/api/src/routes/auth.rs`
- `apps/api/src/auth/session.rs`
- `apps/web/src/lib/auth/store.ts`

---

## 1. Gesamtübersicht

Ein Bereich erhält den Status `Teil` auch dann, wenn ein funktional verwandter Alt-/MVP- oder Codepfad existiert, der Zielcontract aus ADR-0006/Specs aber noch nicht deckungsgleich nachgewiesen ist.

| Bereich                | Soll (Spec) | Ist (Beleg)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   | Status | Risiko             |
| ---------------------- | ----------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------ | ------------------ |
| Magic Link             | vorhanden   | Ziel-Contract migriert, Legacy-Alias aktiv, Runtime-Beleg offen                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               | Teil   | mittel             |
| Session                | required    | PostgreSQL-/In-Memory-Sessionvertrag mit konfigurierbarer TTL; PostgreSQL ist Zeitautorität für persistierte Sessions; Chromium-Profil-, Neustart-, Isolations- und serverseitiger Logout-Proof auf Code-Head `494abe72` in GitHub CI grün                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    | Teil   | niedrig bis mittel |
| Session Refresh        | required    | Route aktiv, Session-Rotation belegt, Token-Split offen                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       | Teil   | mittel             |
| Logout                 | required    | Route und Server-Löschung belegt; Chromium-Proof injiziert den alten Cookie nach Logout erneut und beweist dessen serverseitige Ungültigkeit                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  | Teil   | niedrig bis mittel |
| Logout All             | required    | Challenge belegt, Consume implementiert (LogoutAll-Intent via Step-up-Consume), kein E2E-Email-Flow-Test                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      | Teil   | mittel             |
| Devices                | required    | API aktiv (Liste, Self-Delete), RemoveDevice-Intent via Step-up-Consume implementiert, kein E2E-Email-Flow-Test                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               | Teil   | mittel             |
| Step-up Auth           | required    | Challenge-Store, Request, Consume für Magic-Link implementiert; `BeginPasskeyRegistration`-Consume erzeugt jetzt `registration_grant_id` (TTL 5 Min, single-use, account/device-gebunden); Handoff vollständig                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                | Teil   | mittel             |
| Passkeys               | optional    | Register-Options mit Grant-Handoff: Step-up erzeugt Grant, `register/options` konsumiert Grant und startet WebAuthn-Ceremony; langlebige Credentials laufen über die Passkey-Runtime-Facade (Default `in_memory`, optional `postgres`) plus `webauthn_user_id`-Writeback-Mutation; `register/verify` API-seitig implementiert (echte `finish_passkey_registration`, single-use, Duplicate-Detection, Writeback) — Negativpfade getestet; positiver Verify-Pfad ist durch CI belegt ([Run 27487642565](https://github.com/heimgewebe/weltgewebe/actions/runs/27487642565), Commit `cc54460`, Workflow `auth-passkey-register-proof`); Passkey-Login-Flow (`auth/options`, `auth/verify`) ist API-seitig implementiert und über den Route-Level-Register→Reload→Auth-Proof (AUTH-PG-002-C1, PR-CI-Job `db-passkey-persistence-proof`) belegt; Browser-Login-E2E, Management-Pfade und Produktions-Cutover offen | Teil   | mittel             |
| Sicherheitsinvarianten | required    | Codepfade für alle fünf Aspekte implementiert; Anti-Enumeration-Parität und systematische CSRF-Abdeckung aller aktuell gelisteten CSRF-pflichtigen mutierenden Endpunkte reproduzierbar belegt (Phase 7), ergänzt um eine quelltextbasierte CSRF-Routen-Drift-Prüfung gegen neue unklassifizierte Mutationsrouten; CI-Prüfung für den Runtime-Smoke des E-Mail-Rate-Limits bei `/auth/magic-link/request` hinzugefügt (READY_FOR_CI_PROOF), IP-Rate-Limit-Runtime-Beweis und End-to-End-Token-Leak-Test offen                                                                                                                                                                                                                                                                                                                                                                                                 | Teil   | mittel             |

---

## 2. Detailprüfung

### 2.1 Magic Link

**Soll:** POST `/auth/magic-link/request`, POST `/auth/magic-link/consume`, Anti-Enumeration, Token TTL.
**Ist:** Kanonischer Zielcontract ist auf `/auth/magic-link/*` migriert. Der temporäre Legacy-Alias `/auth/login/consume` wurde entfernt; der kanonische Contract liegt nun ausschließlich unter `/auth/magic-link/*`. API-level Round-Trip-Proof eingeführt: `magic_link_full_round_trip_request_to_session` in `apps/api/tests/api_auth.rs` ruft `POST /auth/magic-link/request` auf, entnimmt den generierten Token aus `TokenStore`, führt die komplette Consume-Sequenz aus (GET nonce → POST consume) und verifiziert anschließend via `GET /auth/session`, dass die entstandene Session als `{authenticated: true}` erkannt wird. Kein manuelles Token-Seeding — der Token kommt aus dem tatsächlichen Request-Handler. CI-Job `magic-link-flow-proof` in `.github/workflows/api.yml` ist verpflichtend (kein `if:`-Guard). Grüner CI-Run steht aus (READY_FOR_CI_PROOF). Browser-/Mailer-E2E-Nachweis bleibt offen.
**Dokumentationsbelege:** `docs/runbook.md`
**Code-, Test- und Verifikationsbelege:** `apps/api/src/routes/auth.rs`, `apps/api/src/auth/tokens.rs` (`latest_raw_for_email` — nur kompiliert mit Cargo-Feature `integration-testing`, nicht im Production-Build), `apps/api/tests/api_auth.rs` (`magic_link_full_round_trip_request_to_session` — gegated mit `#[cfg(feature = "integration-testing")]`), `.github/workflows/api.yml` (Job `magic-link-flow-proof`, verwendet `--features integration-testing`), `apps/web/src/routes/login/+page.svelte`, `verification/verify_magic_link.py`
**Fehlende Belege:** Grüner CI-Run für `magic-link-flow-proof` (lokal grün, CI-Nachweis ausstehend); Browser-/Mailer-E2E-Nachweis des vollständigen Flows (inkl. UI und echtem Mailzustellpfad)
**Status:** Teil
**Risiko:** mittel

### 2.2 Session

**Soll:** GET `/auth/session`, Session Cookie (secure, httpOnly), belastbares Persistenzmodell.
**Ist:** `GET /auth/session` ist implementiert (inkl. `expires_at` und `device_id`). `AUTH_SESSION_TTL_SECONDS` definiert einen gemeinsamen, fail-closed validierten Ablaufvertrag für In-Memory- und PostgreSQL-Sessions (Standard 30 Tage, zulässig 1 Stunde bis 90 Tage). PostgreSQL ist für persistierte Sessions die alleinige Zeitautorität: Erstellung, Ablaufprüfung und verbleibende Cookie-Laufzeit werden aus derselben Datenbankuhr berechnet. Positive `gewebe_session`-Cookies erhalten `Max-Age` und `Expires` aus der tatsächlich gespeicherten `session.expires_at` und enden mit einem fünfminütigen Sicherheitsabstand vor der serverseitigen Session. Löschcookies verwenden denselben Scope (`Path=/`, `HttpOnly`, `SameSite=Lax`, `Secure` gemäß Konfiguration). Abgelaufene Sessions werden beim Lesen physisch entfernt. Die Auth-Middleware entfernt stale Cookies sowie Sessions fehlender oder deaktivierter Accounts und normalisiert Session-Cookies nach dem Route-Handler, ohne weitere `Set-Cookie`- oder Sicherheitsheader zu verlieren. Nichtkanonische Session-Cookie-Scopes werden fail-closed abgelehnt.

Der gehärtete Chromium-Proof läuft gegen einen direkten PostgreSQL-Service. Er belegt eine gemeinsame Anmeldung auf einer zweiten Seite desselben Browserprofils, Wiederherstellung nach Browser-Neustart und eigenständige Sessions mit unterschiedlichen Cookie- und Geräte-IDs für ein separates Profil und einen privaten Kontext. Nach dem Logout wird der alte Cookie gezielt in einen frischen Client reinjiziert; nur die serverseitige Löschung kann verhindern, dass dieser Client erneut authentifiziert wird. Der Proof prüft außerdem `HttpOnly`, `SameSite=Lax`, `Path=/` sowie die exakte konservative Ableitung der Cookie-Ablaufzeit aus `expires_at`. Im lokalen HTTP-Proof ist `Secure` bewusst deaktiviert; der produktive `Secure`-Vertrag ist separat durch Rust-Tests belegt. Der gehärtete Proof ist lokal und in GitHub CI auf dem aktuellen Implementierungs-Head `494abe72e59c8061955f7657fe1899c50122d7d3` grün. Dieser Head enthält auch die explizite PostgreSQL-CTE-Vertragspräzisierung und den einmalig beim Start erfassten Cookie-Sicherheits-Scope.
**Dokumentationsbelege:** `docs/adr/ADR-0007__auth-persistence-production-db-path.md`, `docs/blueprints/auth-roadmap.md` (Persistenzentscheidung), `docs/specs/auth-blueprint.md`, `docs/blueprints/weltgewebe.auth-and-ui-routing.md`
**Code-, Test- und Verifikationsbelege:** `apps/api/src/routes/auth.rs`, `apps/api/src/routes/mod.rs`, `apps/api/src/middleware/auth.rs`, `apps/api/src/middleware/authz.rs`, `apps/api/tests/api_auth.rs` (inkl. Phase 6 Tests: `session_cookie_has_secure_attributes_on_magic_link_consume`, `session_cookie_insecure_when_auth_cookie_secure_disabled`), `apps/api/src/auth/session.rs`, `apps/api/src/auth/session_db.rs`, `apps/api/tests/db_session_store_persistence.rs`
**CI-Gate:** `.github/workflows/api.yml` Job `db-session-persistence-proof` — führt `apps/api/tests/db_session_store_persistence.rs` verbindlich gegen direkten PostgreSQL-Service (Port `5432`) aus. Phase 5 CI-Gate: **PROVEN** — Run-ID [`26394569642`](https://github.com/heimgewebe/weltgewebe/actions/runs/26394569642), Job-ID [`77692063785`](https://github.com/heimgewebe/weltgewebe/actions/runs/26394569642/job/77692063785), Commit `00a43a009c53c546355a14c08086131bd84cf8ad` (Branch `main`); direkter PostgreSQL-Port `5432` (nicht PgBouncer `6432`); `test db_session_store_persistence ... ok`, `6 passed; 0 failed`. Phase 6 Cookie-Proof-CI: **PROVEN** — Workflow `api`, Event `pull_request`, Branch `chore/auth-phase6-cookie-proof-ci`, Run-ID [`26455010837`](https://github.com/heimgewebe/weltgewebe/actions/runs/26455010837), Job `cookie session proof (phase 6)`/Job-ID [`77886363989`](https://github.com/heimgewebe/weltgewebe/actions/runs/26455010837/job/77886363989), headSha `20c7e30136fc5872e286ab17738a64b0d03aec56`; `CARGO_TERM_COLOR=never`; `cargo test --locked -p weltgewebe-api --test api_auth session_cookie_ -- --test-threads=1 --color never`; `running 2 tests`; `session_cookie_has_secure_attributes_on_magic_link_consume ... ok`; `session_cookie_insecure_when_auth_cookie_secure_disabled ... ok`; `test result: ok. 2 passed; 0 failed`; `PROVEN: cookie/session proof tests passed (phase 6)`. Scope: nur Cookie/session proof, kein vollständiger Browser-E2E-/Auth-Verify-/Passkey-Proof.

**Issue-#1378-Evidence auf dem aktuellen Implementierungs-Head:** Workflow `Auth Session Persistence Proof`, Run-ID [`29113201920`](https://github.com/heimgewebe/weltgewebe/actions/runs/29113201920), Job `auth session persistence browser proof`/Job-ID [`86430203225`](https://github.com/heimgewebe/weltgewebe/actions/runs/29113201920/job/86430203225), headSha `494abe72e59c8061955f7657fe1899c50122d7d3`, Ergebnis `success`. Derselbe Head bestand im Workflow `api` den Job `db session persistence proof (direct postgres)` (Run-ID [`29113201426`](https://github.com/heimgewebe/weltgewebe/actions/runs/29113201426), Job-ID [`86430201641`](https://github.com/heimgewebe/weltgewebe/actions/runs/29113201426/job/86430201641)) sowie `cookie session proof (phase 6)` (Job-ID [`86430201638`](https://github.com/heimgewebe/weltgewebe/actions/runs/29113201426/job/86430201638)). Beweisgrenze: Der Browser-Proof verwendet einen Integrationstest-Bootstrap gegen eine lokale HTTP-API; er beweist keine reale Mailzustellung, kein produktives TLS-Terminal und keinen manuellen Login über einen externen Mailclient.
**Statusformulierung:** DbSessionStore-, Cookie- und gehärteter Issue-#1378-Chromium-Proof sind auf dem aktuellen Implementierungs-Head CI-PROVEN. Reale Mailzustellung, produktiver Browser-Live-Test und der weitergehende Token-Split bleiben offen.
**Fehlende Belege:** Reale Mailzustellung und produktiver Browser-Live-Test über den externen Mailclient; ein vollständiger Access-/Refresh-Token-Split bleibt außerhalb dieses Session-Cookie-Vertrags offen. Session-Rotation, Cookie-Refresh, Logout, Self-Device-Removal sowie mehrere `Set-Cookie`-Header sind API-seitig getestet; Profil-/Neustart-/Isolationsverhalten und die Ablehnung des reinjizierten alten Logout-Cookies sind im Chromium-Proof belegt.
**Status:** Teil
**Risiko:** mittel

### 2.3 Session Refresh

**Soll:** POST `/auth/session/refresh`, verlängert TTL ohne neue Auth.
**Ist:** POST `/auth/session/refresh` ist implementiert und durch API-Tests belegt. Aktuell wird die Session rotiert (alte gelöscht, neue mit gleichem `account_id`/`device_id` erstellt); der Zielrahmen mit separatem Access/Refresh-Token-Split ist noch offen. Persistenz folgt dem Session-Store (Phase 5: `DbSessionStore` wenn `DATABASE_URL` gesetzt, sonst In-Memory).
**Dokumentationsbelege:** keine
**Code-, Test- und Verifikationsbelege:** `apps/api/src/routes/auth.rs`, `apps/api/src/routes/mod.rs`, `apps/api/tests/api_auth.rs`
**Fehlende Belege:** Vollständiger Token-Split (Access/Refresh), E2E-Nachweis.
**Status:** Teil
**Risiko:** mittel

### 2.4 Logout

**Soll:** POST `/auth/logout`
**Ist:** `POST /auth/logout` löscht die serverseitige Session und sendet ein sofort abgelaufenes Cookie mit identischem Scope. Der Chromium-Proof aus Issue #1378 belegt die profilweite Cookie-Löschung und die Wirkung auf eine bereits offene zweite Seite. Zusätzlich reinjiziert er den vor dem Logout gespeicherten Cookie in einen frischen Client: Dieser bleibt unauthentifiziert und erhält erneut ein Löschcookie. Dadurch ist die serverseitige Session-Ungültigkeit falsifizierbar belegt; eine reine Browser-Cookie-Löschung würde den Test nicht bestehen.
**Dokumentationsbelege:** `docs/blueprints/weltgewebe.auth-and-ui-routing.md`
**Code-, Test- und Verifikationsbelege:** `apps/api/src/routes/auth.rs`, `apps/api/tests/api_auth.rs`
**Fehlende Belege:** Produktiver Live-Test über den realen Login-/Mailpfad; der CI-Browser-Proof verwendet den Integrationstest-Bootstrap.
**Status:** Teil
**Risiko:** niedrig bis mittel

### 2.5 Logout All

**Soll:** POST `/auth/logout-all`
**Ist:** POST `/auth/logout-all` gibt bei authentifizierten Requests 403 STEP_UP_REQUIRED mit einer gültigen `challenge_id` zurück. Challenge-Erzeugung und Gerätebindung belegt. Die tatsächliche Session-Löschung erfolgt über `POST /auth/step-up/magic-link/consume` mit Intent `LogoutAll` — dieser Pfad ist implementiert und durch Tests belegt.
**Dokumentationsbelege:** keine
**Code-, Test- und Verifikationsbelege:** `apps/api/src/routes/auth.rs`, `apps/api/tests/api_auth.rs`
**Fehlende Belege:** End-to-End-Test (logout-all → step-up-request → consume via E-Mail-Flow), keine echte E-Mail in Tests
**Status:** Teil
**Risiko:** mittel

### 2.6 Devices

**Soll:** GET `/auth/devices`, DELETE `/auth/devices/:id`, Device-Bindung an Session.
**Ist:** Device-Management (Liste, Self-Delete) funktional implementiert. Fremdgeräte-Guard erzeugt Challenge mit Ziel- und Gerätebindung. Die Ausführung der Fremdgeräte-Löschung erfolgt über `POST /auth/step-up/magic-link/consume` mit Intent `RemoveDevice` — dieser Pfad ist implementiert und durch Tests belegt.
**Dokumentationsbelege:** keine
**Code-, Test- und Verifikationsbelege:** `apps/api/src/routes/auth.rs`, `apps/api/src/auth/session.rs`, `apps/api/src/auth/challenges.rs`, `apps/api/tests/api_auth.rs`
**Fehlende Belege:** E2E Step-up Auth Integration für Löschung fremder Geräte (vollständiger E-Mail-Flow im Test)
**Status:** Teil
**Risiko:** mittel

### 2.7 Step-up Auth

**Soll:** Challenge-System, TTL, Intent-Binding, Magic Link + Passkey, keine neue Session.
**Ist:** Challenge-Store (In-Memory) implementiert. `/auth/logout-all` und `DELETE /auth/devices/:id` erzeugen Challenges. `POST /auth/step-up/magic-link/request` validiert die Challenge gegen die aktuelle Session und nutzt einen separaten Step-up-Token-Pfad; Mailer-Codepfad ist implementiert. `POST /auth/step-up/magic-link/consume` konsumiert den Step-up-Token (single-use, SHA256-gehasht, 5-Min-TTL), prüft Challenge-Bindung und Session-Bindung, führt den Intent aus (LogoutAll / RemoveDevice / UpdateEmail / BeginPasskeyRegistration), erzeugt dabei keine neue Session. Für den `BeginPasskeyRegistration`-Intent erzeugt Consume jetzt einen kurzlebigen `registration_grant_id` (TTL 5 Min, single-use, account- und device-gebunden); `register/options` verlangt und konsumiert diesen Grant vor dem Ceremony-Start. Minimaler Consume-UI-Pfad implementiert.
**Dokumentationsbelege:** `docs/specs/auth-api.md`
**Code-, Test- und Verifikationsbelege:** `apps/api/src/auth/challenges.rs`, `apps/api/src/routes/auth.rs`, `apps/api/tests/api_auth.rs`, `apps/api/src/auth/step_up_tokens.rs`, `apps/api/src/auth/passkeys.rs` (PasskeyRegistrationGrantStore), `apps/api/src/mailer.rs`, `apps/web/src/routes/auth/step-up/consume/+page.svelte`
**Fehlende Belege:** UI E2E Test
**Status:** Teil
**Risiko:** mittel

### 2.8 Passkeys

**Soll:** register (options + verify), auth (options + verify), list/remove.
**Ist:**

- `webauthn_user_id` als dedizierte UUID pro Account eingeführt (nicht aus `account_id` abgeleitet); wenn in der Datenquelle vorhanden: dauerhaft stabil; sonst: lazy-backfill/prozessstabil. Writeback im Verify-Pfad implementiert (siehe unten); reale Datenquellen-Persistenz folgt mit persistenter Account-Ablage.
- WebAuthn-Konfiguration (`rp_id`, `rp_origin`) aus `AppConfig` mit Validierung und Env-Override
- `POST /auth/passkeys/register/options` implementiert: ohne `registration_grant_id` fail-closed mit `STEP_UP_REQUIRED` + `challenge_id`; mit gültigem Grant wird die WebAuthn-Ceremony gestartet und `registration_id` + `options` zurückgegeben
- `POST /auth/passkeys/register/verify` API-seitig implementiert: prüft Session, fail-closed bei fehlender WebAuthn-Konfiguration (`503 PASSKEYS_NOT_CONFIGURED`), konsumiert `registration_id` single-use über `PasskeyRegistrationStore.consume(...)` (non-destructive bei Account-Mismatch), ruft `webauthn.finish_passkey_registration(...)` mit echter Kryptoprüfung auf, legt das resultierende `Passkey` über die Runtime-Facade im gewählten Credential-Store ab (Duplicate-Detection → `409 CONFLICT`), revalidiert den Account direkt vor der Persistenz und schreibt `webauthn_user_id` via `AccountStore.update_webauthn_user_id(...)` zurück. Erfolg liefert `200 OK` mit `{"ok": true}` — keine Session, kein Cookie. Negativpfade (401, 503, 400 unknown/mismatch, 400 invalid credential, kein Session-Cookie) sind durch Integrationstests in `apps/api/tests/api_auth.rs` belegt; der positive Verify-Pfad ist durch CI belegt ([Run 27487642565](https://github.com/heimgewebe/weltgewebe/actions/runs/27487642565), Commit `cc54460`, Workflow `auth-passkey-register-proof`).
- `PasskeyRegistrationGrantStore` (In-Memory, TTL 5 Min, single-use, account- und device-gebunden) eingeführt; Consume für `BeginPasskeyRegistration` erzeugt einen Grant
- `PasskeyRegistrationStore` für laufende Registrierungen (In-Memory, TTL 5 Min) aktiv genutzt (nach Grant-Consume)
- Langlebiger `PasskeyStore` für registrierte Credentials (In-Memory, account-gebunden, duplicate detection, list/find/remove)
- `AccountStore.update_webauthn_user_id(account_id, uuid)` als Writeback-Mutation implementiert
- AUTH-PG-002 Store-Slice: PostgreSQL-Migration `passkey_credentials` und isolierter `DbPasskeyStore` (`apps/api/src/auth/passkeys_db.rs`) mit Insert/List/Find/Update/Remove; Counter-Update owner-gebunden in Transaktion mit `FOR UPDATE`; Duplicate über PK. Store-Ebene Restart-Stabilität ist durch PR-CI belegt (`db passkey persistence proof`). Details: [reports/auth-pg-002-passkey-db-store.md](auth-pg-002-passkey-db-store.md)
- AUTH-PG-002-C1 Route-Level-Register→Reload→Auth-Proof: `passkey_register_reload_auth_route_proof` in `apps/api/tests/db_passkey_store_persistence.rs` beweist gegen die echten Routen hinter der echten Auth-Middleware, dass ein per `register/verify` in PostgreSQL persistiertes Credential nach vollständiger State-Reinitialisierung (frischer Pool, Accounts erneut über `load_accounts_from_postgres` geladen) von `auth/options` gefunden wird, `auth/verify` den Signatur-Counter in PostgreSQL fortschreibt und erst danach eine DB-gestützte Session gemintet wird; die WebAuthn-Ceremony wird durch einen Software-Authenticator mit echter ES256-Kryptografie erfüllt, die Server-Verifikation läuft ungekürzt (Fixture gepinnt durch den nicht-ignorierten Selbsttest `soft_authenticator_passes_bare_webauthn_ceremonies`). Lokal gegen direktes PostgreSQL grün; maßgeblicher Merge-Beleg ist der PR-CI-Job `db-passkey-persistence-proof`. Details: [reports/auth-pg-002-cutover-plan.md](auth-pg-002-cutover-plan.md), Gate B.
- AUTH-PG-002-C2 CI-Schema-Preflight (heimserverfrei): `apps/api/tests/db_passkey_schema_preflight.rs` pinnt gegen frisches PostgreSQL die Invarianten, die die kontrollierte Runtime-Migration später reproduzieren muss — Einbettung der Migration `20260630000001` in den compile-time Produktions-Migrator (`sqlx::migrate!("./migrations")`, offline-Test nicht ignored), Registrierung in `_sqlx_migrations` mit `success = true`, exakte Spalten/Typen/Nullability, PK genau `credential_id`, nicht-unique Index `passkey_credentials_account_id` plus semantische `(account_id)`-Indexdeckung, bewusste FK-Abwesenheit (still ergänzter FK schlägt fehl) sowie DB-seitige NOT-NULL- (23502) und Unique-Durchsetzung (23505). Im PR-CI-Job `db-passkey-persistence-proof` vor den Verhaltens-Proofs verdrahtet; verändert keinen Heimserver-Runtime-Stand und ersetzt keinen kontrollierten Runtime-Schritt. Details: [reports/auth-pg-002-schema-preflight-ci.md](auth-pg-002-schema-preflight-ci.md)

**Dokumentationsbelege:** auth-roadmap.md (Phase 4 aktualisiert), [reports/auth-pg-002-cutover-plan.md](auth-pg-002-cutover-plan.md) (aktueller Cutover-Plan); [reports/passkey-register-verify-prep.md](passkey-register-verify-prep.md) bleibt der archivierte Vorbereitungsbericht
**Code-, Test- und Verifikationsbelege:**

- `apps/api/src/auth/passkeys.rs` — Modul mit Builder, Store, Registrierung
- `apps/api/src/routes/auth.rs` — `passkey_register_options`- und `passkey_register_verify`-Endpunkte
- `apps/api/src/routes/mod.rs` — Router-Eintrag `POST /auth/passkeys/register/verify`
- `apps/api/src/config.rs` — `webauthn_rp_id`, `webauthn_rp_origin`, `webauthn_rp_name`
- `apps/api/src/routes/accounts.rs` — `webauthn_user_id` am Account-Modell
- Unit-Tests in `apps/api/src/auth/passkeys.rs` und `apps/api/src/auth/accounts.rs`; Integrationstests in `apps/api/tests/api_auth.rs` (inkl. `passkey_register_verify_*`-Negativpfade) und `apps/api/tests/auth_security_invariants.rs` (CSRF-Drift-Guard erfasst `POST /auth/passkeys/register/verify`)
- Browser-Proof in `apps/web/tests/proofs/passkey-register-positive.proof.ts` ist durch CI belegt ([Run 27487642565](https://github.com/heimgewebe/weltgewebe/actions/runs/27487642565), Commit `cc54460`, Workflow `auth-passkey-register-proof`).

**Fehlende Belege:** Passkey-Management-UI/List/Remove; kontrollierte Runtime-Anwendung der Migration `20260630000001` in der Zielumgebung (AUTH-PG-002-R2; der CI-Schema-Preflight AUTH-PG-002-C2 ist belegt, ersetzt aber keinen Runtime-Schritt); erneuter redigierter Runtime-Audit nach vorhandener Tabelle; Produktions-Cutover; `webauthn_user_id`-Backfill/NOT-NULL (AUTH-PG-003); Browser-/Authenticator-E2E über einen echten OS-Prozess-Neustart (der Playwright-Proof startet die API genau einmal). Store, Runtime-Facade, Browser-Route-Proof, Route-Level-Register→Reload→Auth-Proof (AUTH-PG-002-C1), CI-Schema-Preflight (AUTH-PG-002-C2), CI-FK-Audit und Runtime-Audit-Werkzeug sind vorbereitet, bleiben aber bis Produktions-Cutover Teilstatus von AUTH-PG-002.
**Status:** Teil
**Risiko:** mittel

### 2.9 Sicherheitsinvarianten

**Soll:** Anti-Enumeration, Rate Limit, Trusted Proxy Handling, CSRF / Origin, Token Leak Prevention.
**Ist:** Codepfade für alle fünf Aspekte sind implementiert. Anti-Enumeration: `request_login` gibt identische 200-Responses unabhängig von der Account-Existenz. Rate Limiting: Dual-Layer (IP + E-Mail-Hash) via `AuthRateLimiter`. CSRF: Origin-/Referer-Middleware (`apps/api/src/middleware/csrf.rs`) implementiert. Trusted Proxy: `effective_client_ip()` mit RFC-7239-Forwarded-Parsing und konfigurierbarer Allowlist (`AUTH_TRUSTED_PROXIES`). Token Leak Prevention: SHA-256-Hashing für Magic-Link- und Step-up-Tokens; Constant-Time-Vergleich punktuell im Magic-Link-Consume-Flow (`apps/api/src/routes/auth.rs`). **Phase 7 (reproduzierbare Sicherheitsnachweise):** Anti-Enumeration ist als Paritätstest belegt (bekannte vs. unbekannte E-Mail liefern identischen Status, einen byte-identischen Body und Parität der sicherheitsrelevanten Header — kein `Set-Cookie`-Seitenkanal, identischer `Content-Type`/`Cache-Control` — ohne E-Mail-/Token-Leakage); CSRF ist systematisch über alle aktuell gelisteten CSRF-pflichtigen mutierenden Endpunkte belegt (Cross-Site-Request ohne Origin/Referer → 403 mit leerem Body, plus Positivkontrolle mit gültigem Origin) gegen denselben Middleware-Stack wie in `apps/api/src/lib.rs`. Zusätzlich erzwingt eine quelltextbasierte CSRF-Routen-Drift-Prüfung (`csrf_mutating_route_drift_guard_matches_router_declarations` in `apps/api/tests/auth_security_invariants.rs`), dass jede neu deklarierte mutierende Route explizit als CSRF-geschützt oder bewusst ausgenommen klassifiziert wird. Die CI-Prüfung für den Runtime-Smoke des E-Mail-Rate-Limits bei `/auth/magic-link/request` ist eingerichtet (READY_FOR_CI_PROOF); grüner CI-Lauf steht noch aus. IP-Rate-Limit-Runtime-Beweis und End-to-End-Token-Leak-Test bleiben offen.
**Dokumentationsbelege:** `docs/runbook.md` (Rate Limits, Trusted Proxies), `docs/adr/ADR-0006__auth-magic-link-session-passkey.md`
**Code-, Test- und Verifikationsbelege:** `apps/api/src/routes/auth.rs` (Anti-Enumeration in `request_login`, Trusted Proxy in `effective_client_ip`, Constant-Time-Vergleich in `consume_login_post`), `apps/api/src/middleware/csrf.rs`, `apps/api/tests/auth_security_invariants.rs` (`csrf_blocks_all_mutating_endpoints_without_origin`, `magic_link_request_is_indistinguishable_for_known_and_unknown_email`, `csrf_mutating_route_drift_guard_matches_router_declarations`), `apps/api/tests/api_auth.rs` (`test_session_refresh_csrf_rejected`), `apps/api/tests/auth_ratelimit.rs`, `apps/api/src/auth/rate_limit.rs`, `apps/api/src/auth/tokens.rs` (SHA-256-Hashing), `apps/api/src/auth/step_up_tokens.rs` (SHA-256-Hashing), `scripts/guard/auth-rate-limit-runtime-smoke.sh`, `.github/workflows/api-smoke.yml`
**Fehlende Belege:** Kein Runtime-Beweis für IP-Rate-Limiting und weitere Rate-Limit-Dimensionen. CI-Prüfung für den E-Mail-Rate-Limit-Smoke bei `/auth/magic-link/request` ist eingerichtet; grüner CI-Lauf steht noch aus. Kein Runtime-Beweis für Magic-Link-Mailzustellung. Kein End-to-End-Token-Leak-Prevention-Test (Response-Non-Leakage belegt und SHA-256-Hashing unit-getestet; Speicher-/Log-Pfad-Beweis bleibt offen).
**Status:** Teil
**Risiko:** mittel

---

## 3. Offene Architekturentscheidungen

### /me/email

**Soll:** Route, Methode, Step-up Pflicht, Session-Verhalten danach.
**Ist:** `PUT /auth/me/email` validiert und normalisiert die E-Mail (Format, Eindeutigkeit). Erfordert Step-up Auth (`403 STEP_UP_REQUIRED` mit `challenge_id`). `request_step_up` routet den Token an die NEUE E-Mail. Consume des Intents `UpdateEmail` prüft Eindeutigkeit erneut und ändert die E-Mail. Damit ist der doppelte Besitznachweis aus bestehendem authentifiziertem Geräte-Kontext (`account_id` + `device_id`) und neuer E-Mail-Adresse erbracht.
**Status:** OK

---

## 4. Entscheidungsregel

Kein Feature darf implementiert werden, wenn die Basis (Session) nicht stabil ist oder Step-up/API unklar ist.
Diese Matrix macht Drift sichtbar und verhindert, dass offene Punkte stillschweigend als voll implementiert behandelt werden.
