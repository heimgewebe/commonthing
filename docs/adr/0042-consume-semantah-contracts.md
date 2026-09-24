---
id: adr.0042-consume-semantah-contracts
title: ADR-0042 — SemanticAH-Contracts konsumieren
doc_type: reference
status: active
summary: Entscheidung zur Integration und zum Konsum von SemanticAH-Contracts.
relations:
  - type: relates_to
    target: docs/x-repo/semantAH.md
---
# ADR-0042: semantAH-Contracts konsumieren

Status: superseded

Beschluss:

- commonThing liest JSONL-Dumps (Nodes/Edges) als Infoquelle (read-only).
- Kein Schreibpfad zurück. Eventuelle Events: getrennte Domain.

Konsequenzen:

- CI validiert nur Formate; Import-Job später.
- **Diese Entscheidung ist vorerst ausgesetzt.**
  - semantAH wird aktuell nicht als Datenquelle in commonThing konsumiert.
  - Import-Jobs und CI-Validierungen wurden entfernt.
