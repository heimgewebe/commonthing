---
id: runtime.readme
title: Runtime Reality
summary: Kanonischer Vertrag für Laufzeitprofile, beobachtbare Wahrheit und Konfigurationsschalter.
role: reality
organ: runtime
status: canonical
canonicality: reality
lifecycle_state: active
owner: runtime
review_after: 2026-08-12
last_reviewed: 2026-07-12
depends_on: []
relations:
  - type: relates_to
    target: docs/deploy/vps.md
  - type: relates_to
    target: runbooks/README.md
  - type: relates_to
    target: docs/datenmodell.md
verifies_with: []
---

# Runtime Reality

## Was dieses Dokument belegt

Dieses Dokument beschreibt die im Repository unterstützten Laufzeitprofile und
ihre Verträge. Es belegt **nicht**, dass ein konkreter Host im Moment gesund ist.
Livezustand entsteht erst durch frische Beobachtung von Host, Compose-Render,
Containern, Health-Endpunkten, Migrationen und Logs.

## Kanonischer Produktionspfad

Der öffentliche Zielpfad ist `commonserver` gemäß
[`docs/deploy/vps.md`](../docs/deploy/vps.md). Der historische Heimserverpfad ist
kein Produktionsziel mehr, bleibt aber als Referenzvertrag dokumentiert.

Der Deploy-Einstieg ist:

```text
scripts/weltgewebe-up
```

Zielbezogene Hilfsskripte dürfen keinen zweiten Deployvertrag pflegen, sondern
müssen an diesen Pfad delegieren.

## Laufzeitprofile

| Profil | Dateien | Zweck |
|---|---|---|
| lokal/core | `compose.core.yml` | Web, API, Caddy, PostgreSQL und PgBouncer für Entwicklung/Integration |
| produktiv | `compose.prod.yml` | API, Caddy, PostgreSQL und NATS |
| VPS | `compose.prod.yml` + `compose.vps.override.yml` | öffentlicher Frontdoor auf `commonserver` |
| Beobachtung | `compose.observ.yml` | optionale Monitoringkomponenten |
| SMTP | `compose.smtp.override.yml` | explizite Mailkonfiguration ohne Secretwerte im Repository |

Die exakte Service- und Env-Wahrheit ergibt sich aus dem gerenderten
Compose-Modell, nicht aus dieser Tabelle allein.

## Webbereitstellung

Die Webanwendung ist ein statischer SvelteKit-Build. Caddy kann zu einem
lokalen Build-Artefakt unter `/srv/weltgewebe-web` routen. Für `commonserver` ist
dieser statische interne Caddy-Pfad der kanonische Produktionspfad; externe
Web-Upstreams sind Vorschau-/Legacy-Flächen und keine Produktionswahrheit.
Der Deployvertrag verlangt eine konkrete Buildkennung über
`WELTGEWEBE_BUILD`, `X-Weltgewebe-Build` und `/_app/version.json`.

## Domänenquellen

| Schalter | lokaler Default | Produktionswert `commonserver` | Bedeutung |
|---|---|---|
| `WELTGEWEBE_DOMAIN_READ_SOURCE` | `jsonl` | `postgres` | Quelle für Accounts/Garnrollen, Knoten und Fäden |
| `WELTGEWEBE_DOMAIN_ACCOUNT_WRITE_SOURCE` | `jsonl` | `postgres` | Account-/Garnrollen-Erzeugung und Account-Mutationen |
| `WELTGEWEBE_DOMAIN_NODE_WRITE_SOURCE` | `jsonl` | `postgres` | Knotenänderungen |
| `WELTGEWEBE_DOMAIN_EDGE_WRITE_SOURCE` | `jsonl` | `postgres` | Fadenerzeugung |

Die lokalen Defaults bleiben aus Rückwärtskompatibilität JSONL. In Produktion
ist PostgreSQL jedoch die Lese- und Schreibwahrheit; JSONL ist dort kein offener
Cutover-Blocker, sondern nur Legacy-/Rollback-/Importmaterial. Jeder
PostgreSQL-Schreibpfad verlangt auch `domain_read_source=postgres` und einen
verfügbaren Pool. Ungültige Kombinationen führen zu einem Konfigurations- oder
Startfehler.

## Auth- und Sessionpersistenz

Sitzungen und Passkey-Credentials laufen im Produktionspfad über PostgreSQL
(`sessions`, `passkey_credentials`). Ohne Datenbankpfad verwendet die API lokale
Entwicklungs-/Fallback-Stores; diese sind kein Produktionsvertrag. Die effektive
Quelle muss aus Konfiguration und Startlogs redigiert beobachtet werden.

| Schalter | Standard | Bedeutung |
|---|---|---|
| `AUTH_AUTO_PROVISION` | `false` | unbekannte, zugelassene E-Mail-Adressen beim Magic-Link-Login als Garnrolle anlegen |
| `AUTH_AUTO_PROVISION_ROLE` | `gast` | Rolle neuer Garnrollen: `gast` oder `weber` |

`AUTH_AUTO_PROVISION_ROLE=weber` ist nur mit einer konkreten
`AUTH_ALLOW_EMAILS`- oder `AUTH_ALLOW_EMAIL_DOMAINS`-Liste zulässig. Offene
Registrierung darf ausschließlich `gast` provisionieren; `admin` ist für diese
Variable grundsätzlich ungültig. Persistiert wird vor Cache-Aktualisierung und
vor Versand des Magic Links.

## Migrationen

`WELTGEWEBE_API_STARTUP_MIGRATIONS` besitzt drei unterschiedliche Bedeutungen:

- `run`: Migrationen anwenden,
- `verify-applied`: nur nachweisen, dass der erwartete Verlauf bereits vorliegt,
- `skip`: bewusst keine Migrationsprüfung ausführen.

Ein Route- oder Health-Smoke erteilt keine Migrationsfreigabe.

## Proxyentscheidung

Bei aktivem IP-Ratelimit muss `AUTH_TRUSTED_PROXIES` ausdrücklich gesetzt sein:

- `none` für direkten Betrieb,
- Netzliste für einen kontrollierten Proxybetrieb.

Die API darf Proxyheader nur von diesen Quellen übernehmen.

## Minimaler Beobachtungssatz

Für eine belastbare Runtimeaussage sind mindestens nötig:

1. erwarteter Git-Commit beziehungsweise Image-Digest,
2. gerendertes Compose-Modell ohne unerklärte Warnungen,
3. Containerstatus und Health,
4. `/health/live` und `/health/ready`,
5. Migrationsmodus und Datenquellschalter,
6. redigierte Secret-Quellen beziehungsweise Vorhandenseinsbelege,
7. Backup-/Restore-Proof-Status für `scripts/ops/postgres-backup.sh` und
   `scripts/ops/postgres-restore-proof.sh`,
8. Logs ohne Token- oder Secretlecks.
