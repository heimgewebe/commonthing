---
id: docs.specs.federation-core
title: Föderationskern — normative Fachinvarianten
doc_type: specification
status: canonical
summary: "Definiert die unveränderlichen Fachgrenzen für Zellidentität, Objektursprung, Reichweite, Ereignisse und gemeinsame Räume."
role: norm
organ: governance
canonicality: normative
lifecycle_state: active
owner: governance
review_after: 2026-10-15
last_reviewed: 2026-07-15
depends_on:
  - architecture.weltgewebe-os
verifies_with: []
relations:
  - type: relates_to
    target: docs/adr/ADR-0011__foederierte-gewebezellen.md
  - type: relates_to
    target: docs/adr/ADR-0012__ereignisrueckgrat-transactional-outbox.md
  - type: relates_to
    target: docs/specs/garnrolle-knoten-faden.md
attention_source_status: none
attention_source_rationale: "Definiert zellübergreifende Fachinvarianten und Objektursprung, keine betrachterbezogene Attention-Wahrheit."
---

# Föderationskern

## 1. Geltung

Diese Spezifikation definiert die fachlichen Invarianten, die bereits vor einer Protokollimplementierung gelten. Sie ist kein Wire-Format und behauptet keine vorhandene Föderationsruntime.

## 2. Zellidentität

Jede Gewebe-Zelle besitzt eine stabile Zell-ID und mindestens einen überprüfbaren aktiven Schlüssel. Schlüsselrotation darf die historische Prüfbarkeit bereits akzeptierter Ereignisse nicht still zerstören.

## 3. Globale Objektadresse

Ein föderierbares Objekt besitzt eine global eindeutige, dereferenzierbare oder auflösbare Adresse. Die Adresse bindet das Objekt an seine Ursprungszelle, ohne interne Datenbank-IDs oder Infrastrukturadressen offenzulegen.

## 4. Ursprungseigentum

Für jedes kanonische Objekt existiert genau eine Ursprungszelle. Nur sie darf den kanonischen Zustand, die Objektversion, die reguläre Löschung und die primären Berechtigungen führen.

Fremde Zellen dürfen:

- das Objekt referenzieren,
- erlaubte Projektionen speichern,
- lokale Suchindizes bilden,
- eigene Fäden zum Objekt führen,
- erlaubte Vorschläge oder Antworten senden.

Sie dürfen nicht still die kanonische Objektversion ersetzen.

## 5. Reichweite

Jedes föderierbare Objekt und Ereignis besitzt genau eine Grundreichweite:

- `private`,
- `local`,
- `neighbourhood`,
- `global`.

`neighbourhood` benötigt eine explizit auflösbare Zielmenge oder Beziehungsklasse. `global` bedeutet öffentlich föderierbar, nicht verpflichtend von jedem Index zu übernehmen.

## 6. Ereignisinvarianten

Jedes externe Ereignis besitzt:

- stabile Ereignis-ID,
- Ereignistyp und Schema-Version,
- Ursprungszelle,
- Akteur oder Systemursprung,
- Objektadresse,
- Objektversion oder erwartete Vorgängerversion,
- Erzeugungszeit,
- Reichweite,
- Signatur und Schlüsselreferenz.

Empfänger müssen Wiederholung erkennen können. Unbekannte Versionen dürfen nicht still als bekannte Semantik verarbeitet werden.

## 7. Annahmeentscheidung

Die Signaturprüfung beweist Herkunft, nicht Vertrauen oder Berechtigung. Jede Zielzelle führt zusätzlich lokale Regeln für:

- Vertrauensstufe,
- erlaubte Ereignisklasse,
- Rate,
- Reichweite,
- Moderation,
- Quarantäne,
- Blockierung.

## 8. Objektaktualisierung

Eine fremde Projektion wird nur durch ein gültiges Ereignis der Ursprungszelle oder durch erneute verifizierte Auflösung aktualisiert. Versionen dürfen nicht rückwärts laufen. Konflikte oder Lücken werden sichtbar gehalten.

## 9. Löschung

Die Ursprungszelle kann ein Lösch- oder Widerrufsereignis ausgeben. Konforme Zellen entfernen oder sperren ihre Projektionen gemäß Vertrag. Eine technische Rückholung aus bösartigen Fremdsystemen kann nicht garantiert werden.

## 10. Gemeinsame Räume

Ein gemeinsamer Raum besitzt:

- globale Raumadresse,
- definierte Heim- oder Trägerzellen,
- Teilnehmer- und Rollenregeln,
- erlaubte Ereignistypen,
- Moderations- und Konfliktregeln,
- Aufbewahrungsregeln,
- Austritts- und Schließungsregeln.

Gemeinsame Räume dürfen eine eigene Ereignisordnung definieren. Sie ändern nicht automatisch das Ursprungseigentum gewöhnlicher Knoten oder Garnrollen.

## 11. Zellbeziehungen

Zellbeziehungen sind explizit und versioniert. Sie können Vertrauensstufe, Nachbarschaftstyp, erlaubte Importe/Exporte, Raten und gemeinsame Räume referenzieren.

## 12. Datenschutzgrenze

Private Kontodaten, Zugangsdaten, interne Moderationsnotizen und nicht freigegebene Beziehungen werden nicht als öffentliche Föderationsobjekte modelliert. Minimierung und Zweckbindung gelten auch für Caches und globale Projektionen.

## 13. Infrastrukturtrennung

Öffentliche Föderation darf keinen direkten Zugriff auf interne Datenbanken, Kubernetes-APIs, Cluster-Netze oder interne NATS-Subjects voraussetzen.

## 14. Konformitätsziel

Eine spätere Protokollsuite muss mindestens positive und negative Beweise für Signatur, Wiederholung, Reichweite, Versionierung, Quarantäne, Aktualisierung, Löschung und Netzpartition enthalten.
