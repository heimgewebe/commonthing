---
id: specs.auth-state-machine
title: Auth State Machine
doc_type: reference
status: active
summary: Beschreibt die kanonischen Zustände und Transitionen des commonThing-Auth-Systems.
relations:
  - type: relates_to
    target: docs/adr/ADR-0006__auth-magic-link-session-passkey.md
  - type: relates_to
    target: docs/specs/auth-api.md
  - type: relates_to
    target: docs/specs/auth-ui.md
---

# Auth State Machine

## Zustände

- `unauthenticated`: Kein gültiger Zugang auf dem Gerät. (Startzustand oder nach Logout/Ablauf)
- `link_requested`: Ein Magic Link wurde erfolgreich angefordert und per E-Mail versendet
  (für Erstlogin, Wiederherstellung oder neues Gerät).
- `authenticated_session`: Eine gültige Session ist etabliert.
  Sowohl `session_access_token` als auch `session_refresh_token` sind aktiv.
- `step_up_required`: Eine aktive Session liegt vor, jedoch erfordert die angefragte Aktion eine zusätzliche Bestätigung.

## Transitionen (Trigger -> Pfad)

- **`unauthenticated` -> `link_requested`:**
  - *Trigger*: User gibt E-Mail ein und klickt "Senden".
  - *Aktion*: System schickt Magic Link E-Mail und geht in Wartezustand.

- **`link_requested` -> `authenticated_session`:**
  - *Trigger*: User klickt auf gültigen Magic Link (`consume`).
  - *Aktion*: System etabliert Session für das jeweilige Device.
    Der Hash wird validiert und der Token für weitere Versuche invalidiert.

- **`unauthenticated` -> `authenticated_session`:**
  - *Trigger*: User authentifiziert sich via Passkey.
  - *Aktion*: System validiert Passkey Response und etabliert Session.

- **`authenticated_session` -> `authenticated_session` (Refresh):**
  - *Trigger*: Gültiger Session-Refresh (`POST /auth/session/refresh`).
  - *Aktion*: Der Access-Token wird erneuert, der Refresh-Token wird rotiert und die alte Instanz invalidiert.
    Das Gerät behält seinen authentifizierten Status.

- **`authenticated_session` -> `step_up_required`:**
  - *Trigger*: User versucht eine sensible Aktion (z.B. Verortung, E-Mail-Änderung, Geräteentfernung) auszuführen.
  - *Aktion*: System antwortet mit `403` und Payload `{"error": "STEP_UP_REQUIRED", "challenge_id": "..."}`.

- **`step_up_required` -> `authenticated_session`:**
  - *Trigger*: User validiert erfolgreich über Passkey oder einen frischen Step-up-Magic-Link.
  - *Aktion*: Die an die `challenge_id` gebundene Aktion wird freigegeben.
    Alternativ wird ein sehr kurzes Zeitfenster für sensible Aktionen geöffnet.
    Es wird explizit **keine** neue Session etabliert und das System notiert das Step-up-Event,
    ohne die Basis-Session dauerhaft anzuheben.

- **`authenticated_session` -> `unauthenticated`:**
  - *Trigger*: Zeitlicher Ablauf des `session_refresh_token` ODER manueller/serverseitiger Widerruf
    (z.B. Logout, Session-Invalidierung).
  - *Aktion*: Die Session wird serverseitig ungültig, clientseitige Tokens verfallen.
    Der Nutzer erhält beim nächsten App-Start oder der nächsten Aktion einen Prompt zur Neuanmeldung
    (Magic Link oder optionaler Passkey).

## Fehlerpfade

- **`link_requested` -> `unauthenticated`:**
  - *Fehler*: Link abgelaufen (TTL > 15m), bereits genutzt, oder strukturell invalid.
  - *Ergebnis*: Token-Konsumierung scheitert mit `401 Unauthorized` und `TOKEN_INVALID` / `TOKEN_EXPIRED`.

- **`step_up_required` -> `unauthenticated`:**
  - *Fehler*: Extrem fehlerhafte Step-up-Versuche (Missbrauchsverdacht, Admin-Eingriff).
  - *Ergebnis*: Möglicher Sicherheits-Fallback durch serverseitigen Widerruf der Session.
    Dies führt zum Fallback zu `unauthenticated` (erfordert vollständigen Re-Login).
    **Hinweis:** Ein einzelner abgelaufener Step-up-Link bricht die Session nicht ab.
