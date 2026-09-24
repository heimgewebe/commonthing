---
id: domain.modules
title: Modul-IDs
doc_type: reference
status: active
summary: Kanonische Policy für Modul-IDs in commonThing (profile, forum, responsibilities).
relations:
  - type: relates_to
    target: docs/domain/vocabulary.md
---
# Modul-IDs in commonThing

## Zweck

Dieses Dokument definiert die kanonische Policy für Modul-IDs in commonThing.
Ziel ist maximale Zukunftsfestigkeit, klare Trennung von Identität und Darstellung
sowie Konsistenz über Backend, Frontend, Tests und Contracts hinweg.

## Grundprinzip

### ID ≠ Label

- `id` ist ein **stabiler, technischer Schlüssel**
- `label` ist **UI-Text** (menschenlesbar, deutsch)
- Änderungen am Label dürfen **keine** Änderungen an der ID erfordern

## Sprach-Policy für IDs

- Sprache: **Englisch**
- Format: `lowercase`, `ascii`, `kebab-case` oder `snake_case`
- Keine Umlaute, keine Sonderzeichen
- IDs sind **nicht** identisch mit UI-Labels
- IDs werden **nach Einführung nicht mehr umbenannt**

Begründung:

- IDs sind Teil von Contracts, Events, Persistenz, Tests
- Sprache entwickelt sich, Identitäten dürfen es nicht

## Sprach-Policy für Labels

- Sprache: **Deutsch**
- Präzise, knapp, ohne Gender-Sonderzeichen
- Labels dürfen sich ändern (UX, Redaktion, Kontext)

## Kanonische Standard-Module (v1)

| ID                 | Label (de)      | Bedeutung                          |
| ------------------ | --------------- | ---------------------------------- |
| `profile`          | Übersicht       | Beschreibung, Kontext, Selbstbild  |
| `forum`            | Gespräch        | Diskussion, Austausch, Besprechung |
| `responsibilities` | Verantwortungen | Zuständigkeiten, Rollen, Aufgaben  |

## Verwendung

- Backend liefert `modules[].id` als stabilen Schlüssel
- Frontend rendert anhand von `label`
- Tests referenzieren bevorzugt `id`, nicht Text

## Änderungen & Erweiterungen

- Neue Module erfordern:
  - Ergänzung dieser Tabelle
  - ggf. Contract-Anpassung
- Umbenennung von IDs ist **verboten**
  - Ausnahme nur via expliziter Migration + Alias-Phase

## Geltungsbereich

Diese Policy gilt für:

- Backend-API
- Frontend (UI, Tests)
- Demo-Daten
- Contracts & Schemas
