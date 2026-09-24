---
id: docs.policies.agent-reading-protocol
title: Agent Reading Protocol
doc_type: policy
status: canonical
verifies_with: []
depends_on: []
review_after: 2026-10-11
last_reviewed: 2026-07-11
owner: governance
organ: governance
role: norm
lifecycle_state: active
canonicality: normative
relations:
  - type: relates_to
    target: repo.meta.yaml
  - type: relates_to
    target: AGENTS.md
scope: global
description: "Bindendes Lese- und Entscheidungsprotokoll für Agentenarbeit im commonThing-Repository"
summary: "Bindendes Lese- und Entscheidungsprotokoll für Agentenarbeit im commonThing-Repository"
---

# Agent Reading Protocol

## Zweck

Dieses Dokument definiert das **verbindliche Lese-, Entscheidungs- und Abbruchverhalten**
für alle Agenten, die mit diesem Repository arbeiten.

Ziel:

- deterministische Entscheidungen
- keine stille Interpolation
- klare Konfliktauflösung

---

## 1. Lesereihenfolge (bindend)

Agenten MÜSSEN in dieser Reihenfolge lesen:

1. `agent-contract.json` (kanonische maschinenlesbare Agent-Norm)
2. `repo.meta.yaml` (Repo-Truth-Model; Pfad-/Check-Felder sind Compatibility Projection)
3. `AGENTS.md` (progressive menschliche Eintrittskarte)
4. `agent-policy.yaml` (Compatibility Projection der Schreibgrenzen)
5. die in `manifest/repo-index.yaml` registrierten kanonischen Dokumente sowie einschlägige Contracts und ADRs
6. `docs/index.md` (nur Navigation)
7. `docs/_generated/*` (nur Diagnose; direkte Agent-Edits verboten)

---

## 2. Quellenrangfolge bei Widerspruch (Canonical Hierarchy)

The canonical hierarchy lives in `repo.meta.yaml` (`truth_model.precedence`).

Bei Konflikten gilt strikt diese dort definierte absteigende Priorität (z. B. schlagen `domain_contracts` die `canonical_policies` und diese wiederum `runtime_configs_and_code`).

`generated_diagnostics` zeigen Probleme und Drift, sind aber NIEMALS kanonisch.

---

## 3. Navigation ist keine Wahrheit

`docs/index.md` dient ausschließlich der Orientierung.

Es darf NICHT als Entscheidungsquelle verwendet werden.

---

## 4. _generated ist Diagnose, nicht Ursprung

Artefakte unter `docs/_generated/*`:

- spiegeln Zustand
- zeigen Drift
- sind NICHT kanonisch
- dürfen von Agents nicht direkt editiert werden
- dürfen nur durch in `agent-contract.json` deklarierte trusted generators geschrieben werden

---

## 5. Abbruchregel (kritisch)

Agenten MÜSSEN abbrechen, wenn:

- relevante Dateien fehlen
- Widersprüche nicht auflösbar sind
- Runtime-Wahrheit unklar ist
- kein Target-Proof möglich ist

Form:

- „X fehlt, nötig für Y“

---

## 6. Interpolationsregel

Interpolation ist VERBOTEN, wenn Informationen nachlieferbar sind.

Stattdessen:

- explizite Leerstelle benennen

---

## 7. Arbeitsprinzip

- kleine PRs
- klare Scope-Grenzen
- keine stille Glättung
- Widersprüche sichtbar halten

---

## 8. Aktivierung kognitiver Protokolle

Kognitive Module sind NICHT Teil der Reading Order.
Sie werden ausschließlich über Aktivierungslogik geladen und angewendet.

### Architekturkritik-Skill

Der Architekturkritik-Skill wird NUR aktiviert, wenn mindestens eine der folgenden Bedingungen erfüllt ist:

- Der Task beinhaltet:
  - Architektur
  - Systemdesign
  - strukturelle Analyse
  - Bewertung oder Kritik

- oder:
  - widersprüchliche Aussagen zu Architektur, Datenmodell oder Systemverhalten vorliegen
  - fehlende Informationen eine strukturelle Bewertung verhindern (nicht nur Detailfragen betreffen)

- oder:
  - explizite Analyse- oder Kritik-Trigger enthalten sind
    (z. B. „analysiere“, „bewerte“, „kritisiere“)

### Deaktivierung

Wenn keine der Bedingungen erfüllt ist:

→ Der Architekturkritik-Skill wird NICHT aktiviert.

Der Architekturkritik-Skill wird insbesondere NICHT aktiviert bei:

- rein operativen Tasks (z. B. Ausführung, Formatierung, einfache Transformation)
- eindeutig beantwortbaren Fragen ohne strukturelle Implikationen
- fehlenden Details, die keine Auswirkungen auf Architektur oder Systemverhalten haben

### Fail-Safe

Wenn die Klassifikation des Tasks unsicher ist:

→ partielle Anwendung:

- keine vollständige Pflichtstruktur (z. B. keine vollständige Dialektik)
- nur gezielte Nutzung einzelner Analyseelemente, wenn sie direkt zur Klärung beitragen


---

## Essenz

Dieses Protokoll priorisiert:

- Entscheidbarkeit über Vollständigkeit
- Wahrheit über Lesbarkeit
- Abbruch über Halluzination
