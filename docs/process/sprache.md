---
id: process.sprache
title: Sprache
doc_type: reference
status: active
summary: Sprachrichtlinien und Konventionen für Dokumentation und Kommunikation.
relations:
  - type: relates_to
    target: docs/process/README.md
---
# Sprache & Ton in commonThing

## 1. Grundsatz

- Primärsprache Deutsch (Duden-nah), Du-Form, präzise, knapp.
- Keine Gender-Sonderzeichen (Stern, Doppelpunkt, Binnen-I, Mediopunkt, Slash).
- Anglizismen nur bei echten Fachbegriffen ohne gutes deutsches Pendant.

## 2. Formatkonventionen

- UI: 24-h-Zeit, TT.MM.JJJJ, Dezimalkomma.
- Code/Protokolle: ISO-8601, Dezimalpunkt, SI-Einheiten.

## 3. Artefakte

- Commits: Conventional Commits; Kurzbeschreibung deutsch.
- Code-Kommentare: Englisch (knapp); ADRs/Domäne: Deutsch.
- PRs: deutsch, mit Evidenz-Verweisen.

## 4. Verbote & Alternativen

- Verboten: Schüler:innen, Schüler*innen, SchülerInnen, Schüler/innen, Schüler·innen.
- Nutze Alternativen: Lernende, Team, Ansprechperson, Beteiligte.

## 5. Prüfung

- Vale als Prose-Linter; PR blockt bei Verstößen.
