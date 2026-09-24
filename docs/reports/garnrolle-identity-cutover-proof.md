---
id: reports.garnrolle-identity-cutover-proof
title: Garnrolle Identity Cutover Proof
doc_type: report
status: active
lifecycle_state: active
lifecycle: proof
owner_task: OPT-ARC-001
review_after: 2026-10-24
lang: de
canonicality: evidence
summary: >
  Produktions-, Migrations- und Testbeleg für die endgültige Entfernung der
  früheren RoN-Identität und der Datenbankspalte mode.
relations:
  - type: relates_to
    target: docs/domain/vocabulary.md
  - type: relates_to
    target: docs/datenmodell.md
  - type: relates_to
    target: apps/api/migrations/20260724000001_remove_ron_legacy.up.sql
  - type: relates_to
    target: scripts/ci/tests/test_garnrolle_ontology_contract.py
---

# Garnrolle Identity Cutover Proof

## Entscheidung

commonThing kennt genau eine persönliche Identität: die Garnrolle. Nicht auf der
Karte zu erscheinen ist mit `map_state=not_on_map` eine Eigenschaft derselben
Garnrolle. Es gibt weder fachlich noch technisch eine „Rolle ohne Namen“ als
zweite Identität.

## Produktionsvorbedingung

Am 24. Juli 2026 wurde `wg-prod-1` ausschließlich lesend geprüft. Der Checkout
zeigte dabei Commit `1b1fa1b26b1141699d8bf1ffea2bc50af76c6689`; die
Datenprüfung bezog sich auf die laufende Produktionsdatenbank.

| Prüfung | Ergebnis |
|---|---:|
| Accounts insgesamt | 8 |
| `kind` ungleich `garnrolle` | 0 |
| `mode` nicht NULL | 0 |
| `mode=ron` | 0 |
| Titel „Rolle ohne Namen“ | 0 |
| privates Feld `ron_flag` | 0 |
| privates Feld `visibility` | 0 |
| privates Feld `suppress_public_pos` | 0 |
| ungültiger oder fehlender `map_state` | 0 |
| `map_state=exact` | 1 |
| `map_state=not_on_map` | 7 |

Der Checkout-Sauberkeitsstatus wurde wegen geschützter Betriebsverzeichnisse
nicht zuverlässig bestimmt und ist ausdrücklich kein Teil dieses Belegs.

## Migrationsvertrag

`20260724000001_remove_ron_legacy.up.sql` prüft die semantischen
Legacy-Kategorien erneut in der Zieltransaktion. Abweichende Kontotypen sowie
`ron_flag`, `visibility` oder `suppress_public_pos` lösen eine Ausnahme aus; die
Spalte bleibt dann bestehen. Ein bloßer Restwert in `mode` blockiert nicht, weil
diese Spalte ohne fachliche Auswertung vollständig entfernt wird.

Ein PostgreSQL-16-Gegentest belegt beide Richtungen: Eine kanonische Garnrolle
mit altem `mode='ron'` passiert den Cutover und verliert nur die obsolete Spalte.
Ein Datensatz mit privatem `visibility`-Marker blockiert die Migration, lässt das
Schema unverändert und kann erst nach expliziter Bereinigung migriert werden.

Die Down-Migration fügt eine nullable Spalte `mode` wieder hinzu. Das genügt für
einen Code-Rollback auf den unmittelbar vorherigen lesekompatiblen Stand, ohne
alte Identitäten oder vermeintliche Sichtbarkeitswerte zu erfinden.

## Laufzeitvertrag

- Account-Lese- und Schreibpfade akzeptieren nur `type=garnrolle`.
- `map_state` ist explizit und auf `not_on_map`, `exact` oder `radius` begrenzt.
- `mode`, `ron_flag`, `visibility` und `suppress_public_pos` werden abgewiesen.
- Der JSONL-Startpfad überspringt solche Zeilen nicht: Er bricht mit Datei und
  Zeilennummer ab, damit kein Konto unbemerkt verschwindet und keine Sperre durch
  automatische Neuanlage umgangen werden kann.
- Fehlende private Orts- oder Radiusbindungen bleiben privacy-sicher und können
  keine öffentliche Position erzeugen.
- Die automatische Erstregistrierung erzeugt eine Garnrolle mit
  `map_state=not_on_map`.

## Reproduzierbare Belege

Die folgenden Beweisklassen liefen gegen eine frische PostgreSQL-16-Datenbank:

- Schema-Migrationen einschließlich des zweiachsigen finalen Cutover-Gegentests:
  6 Tests grün;
- deterministischer Backfill: 7 Tests grün;
- PostgreSQL-Lesepfad: 9 Tests grün;
- Account-Schreibpfad: 8 Tests grün;
- automatische Registrierung: 3 Tests grün;
- Knoten-Schreibpfad: 28 Tests grün;
- Governance: 10 Tests grün;
- Passkey- und WebAuthn-Audits: 11 Tests grün.

Zusätzlich sind sechs Garnrollen-Ontologie-Contracttests, 14
Account-Routen-Unit-Tests, fünf JSONL-Provisionierungs- und Neustarttests sowie
die kanonische Erzeugung der Bash-Demodaten grün. Die vollständige
Rust-Testkompilierung und die übrigen Domain-DB-Unit-Tests bleiben Bestandteil
des Repository-Gates.

## Grenzen

Dieser Vorabbeleg beweist Datenfreiheit, Migrations- und Anwendungskompatibilität.
Er beweist noch nicht, dass der neue Commit öffentlich ausgerollt wurde. Der
öffentliche Abschluss benötigt nach Merge den commitgebundenen Migration-Scope,
API-Readiness und einen erneuten Datenbank-Readback, der die Abwesenheit der
Spalte `mode` bestätigt.
