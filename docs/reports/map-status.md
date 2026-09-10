---
id: reports.map-status
title: Kartenstatus
summary: Kurzer, überprüfbarer Ist-Stand der Kartenimplementierung mit ausdrücklich begrenzten Runtimeaussagen.
doc_type: report
status: active
canonicality: diagnostic
lifecycle_state: active
lifecycle: audit
owner: product-map
owner_task: DOCMETA-REPORT-LIFECYCLE-001
last_reviewed: 2026-07-13
review_after: 2026-08-12
evidence_required_for_live_claims:
  - exact Git commit and image tag on commonserver
  - rendered Compose/env source
  - API health/readiness
  - PostgreSQL domain source flags
  - migration status
  - representative authenticated readback
  - backup and restore-proof status
relations:
  - type: supersedes
    target: docs/reports/map-status-matrix.md
  - type: verifies
    target: docs/specs/map-experience.md
---

# Kartenstatus

Stand: 13.07.2026. Dieses Dokument ist ein zeitgebundener Diagnosebericht. Der
dauerhafte Produktvertrag steht in
[`docs/specs/map-experience.md`](../specs/map-experience.md).

## Belegt im Repository

| Bereich                      | Ist-Stand                                                                                                                                                                                                                                                          | Beleg                                                                                                               |
| ---------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------- |
| Datenladung                  | `ok`, `partial` und `failed` werden getrennt                                                                                                                                                                                                                       | `apps/web/src/routes/map/+page.ts`, `map-load-fallback.spec.ts`                                                     |
| Szenenmodell                 | Rohdaten werden vor dem Rendering in eine explizite Szene übersetzt                                                                                                                                                                                                | `apps/web/src/lib/map/scene.ts`, `scene.test.ts`                                                                    |
| Entitäten                    | Knoten und Garnrollen besitzen typisierte Kartenmodelle                                                                                                                                                                                                            | `apps/web/src/lib/map/types.ts`                                                                                     |
| Interaktion                  | Auswahl öffnet das Fokuspanel; Schließen und Fokuswiederherstellung sind getestet                                                                                                                                                                                  | `map-interaction.spec.ts`                                                                                           |
| Suche und Filter             | getrennte, gegenseitig ausschließende Kartenlinsen                                                                                                                                                                                                                 | `overlayManager.ts`, `ui-filter.spec.ts`                                                                            |
| URL-Einstieg                 | `focus`, `lens` und `compose` werden typisiert ausgewertet                                                                                                                                                                                                         | `urlState.ts`, `map-url-state.spec.ts`                                                                              |
| Garnrolle                    | `not_on_map`, `exact` und `radius` sind im Browserpfad vorhanden                                                                                                                                                                                                   | `garnrolle-self-service.spec.ts`                                                                                    |
| Knoten und Faden             | Komposition erzeugt Knoten und den zugehörigen Faden                                                                                                                                                                                                               | `KompositionPanel.svelte`, `komposition.spec.ts`                                                                    |
| Produktionsvertrag           | PostgreSQL ist für Accounts/Garnrollen, Knoten und Fäden die Produktionswahrheit                                                                                                                                                                                   | `.env.prod.example`, `runtime/README.md`, Compose- und API-Verträge                                                 |
| Regionale Basemap-Integrität | Hamburg und Schleswig-Holstein werden vor Veröffentlichung durch alle erreichbaren PMTiles-Verzeichnisse traversiert; bis zu 96 reale MVT-Kacheln werden deterministisch über Zoomstufen und Raum verteilt decodiert und gegen Metadaten sowie Style-Layer geprüft | `apps/web/scripts/validate-pmtiles.mjs`, `validate-pmtiles.test.mjs`, `basemap-runtime-proof.yml`                   |
| PMTiles-HTTP-Vertrag         | Stabile und versionierte Regionalpfade müssen bei HTTP 200 und 206 `application/octet-stream`, `Accept-Ranges: bytes`, korrektes `Content-Range` und die PMTiles-Signatur liefern                                                                                  | `infra/caddy/Caddyfile.vps`, `scripts/guard/basemap-runtime-proof.sh`, `scripts/ops/check_public_live_readiness.py` |

## Datierter Livebeleg

Der Runtime-Audit vom 11./12.07.2026 belegte für `wg-prod-1` vor dieser
Remediation:

- Commit und API-Image `ee4efbf5…`;
- gesunde API-, PostgreSQL-, NATS- und Caddy-Container;
- `WELTGEWEBE_DOMAIN_*_SOURCE=postgres` für Lesen und alle drei Schreibpfade;
- `WELTGEWEBE_API_STARTUP_MIGRATIONS=verify-applied`;
- keine fehlgeschlagene Migration, letzte registrierte Migration
  `20260711000001`;
- je eine persistierte Garnrolle, ein Knoten und ein Faden;
- Anmeldung, Garnrollenbearbeitung, Knoten-/Fadenerzeugung, Neuladen und
  Neustartpersistenz im realen Browserpfad.

Das ist ein datierter Beleg, keine zeitlose Hostwahrheit. Nach jedem Deploy oder
relevanten Runtimewechsel muss er neu erhoben werden.

## Bewusst nicht als dauerhaft bewiesen geführt

- repräsentatives Verhalten bei wachsendem Datenbestand;
- Edge-Referenzintegrität über einen größeren realen Graphen;
- visuelle Korrektheit über repräsentative Zoomstufen, Browser und Geräte;
- URL-Bindung lokaler Panel-Tabs;
- dauerhaft gemessene Kartenleistung;
- ein Vollscan jeder einzelnen PMTiles-Kachel; der Validator prüft alle Verzeichnisse, aber bewusst nur eine begrenzte, deterministische Payload-Stichprobe;
- ein aktueller Backup-/Restore- und Off-Host-Beleg nach jeder Änderung.

## Nächste Beweise

Der erste vertikale Persistenzschnitt ist abgeschlossen. Die nächsten sinnvollen
Beweise sind:

1. Backup, Restore-Proof und Off-Host-Kopie nach dem Remediation-Deploy;
2. wiederholte Browserabnahme über repräsentative Desktop- und Touch-Profile;
3. Edge-Orphan-/Referenzaudit mit repräsentativen Daten;
4. Last- und Darstellungsprüfung bei wachsendem Gewebe;
5. Entfernung der Legacy-`mode`-Rollbackspalte erst nach eigenem Daten- und
   Rückfallbeleg.
