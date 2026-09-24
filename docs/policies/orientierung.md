---
id: policies.orientierung
title: Orientierung
doc_type: reference
status: deprecated
summary: Governance-Orientierung und Leitprinzipien für das Weltgewebe-Projekt.
relations:
  - type: relates_to
    target: docs/vision.md
  - type: relates_to
    target: docs/weltgewebe-agenten-manifest.md
---
> **Historischer Orientierungsstand:** Dieser Text vermischt Vision, abgeleitete Governancewerte und frühere technische Zielbilder. Maßgeblich sind heute `architecture/commonthing-os.md`, `docs/vision.md`, `docs/specs/federation-core.md`, `docs/specs/garnrolle-knoten-faden.md`, `docs/specs/ui-interaction.md`, `docs/specs/ui-state-machine.md` und `docs/specs/map-experience.md`.

# Leitfaden · Ethik & Systemdesign (Weltgewebe)

**Stand:** 2025-10-06
**Quelle:**

- [inhalt.md](../overview/inhalt.md)
- [zusammenstellung.md](../overview/zusammenstellung.md)
- [geist-und-plan.md](../geist-und-plan.md)
- [fahrplan.md](../process/fahrplan.md)
- [techstack.md](../techstack.md)

---

## 1 · Zweck

Dieses Dokument verdichtet Geist, Plan und technische Architektur des Weltgewebes zu einer
verbindlichen Orientierung für
Entwicklung, Gestaltung und Governance.
Es beschreibt:

- **Was** ethisch gilt.
- **Wie** daraus technische und gestalterische Konsequenzen folgen.
- **Woran** sich Teams bei Entscheidungen künftig messen lassen.

---

## 2 · Philosophie („Geist“)

- **Freiwilligkeit**
  - Bedeutung: Keine Handlung ohne bewusste Zustimmung.
  - Operative Konsequenz: Opt-in-Mechanismen, keine versteckten Datenflüsse.
- **Transparenz**
  - Bedeutung: Alles Sichtbare ist verhandelbar; nichts Geschlossenes.
  - Operative Konsequenz: Offene APIs, nachvollziehbare Governance-Entscheide.
- **Vergänglichkeit**
  - Bedeutung: Informationen altern sichtbar; kein endloses Archiv.
  - Operative Konsequenz: Zeitliche Sichtbarkeit („Fade-out“), Lösch- und Verblassungsprozesse.
- **Commons-Orientierung**
  - Bedeutung: Engagement ≠ Geld; Beiträge = Währung.
  - Operative Konsequenz: Spenden (Goldfäden) optional, sonst Ressourcen-Teilung.
- **Föderation**
  - Bedeutung: Lokale Autonomie + globale Anschlussfähigkeit.
  - Operative Konsequenz: Ortswebereien mit eigenem Konto + föderalen Hooks.
- **Privacy by Design**
  - Bedeutung: Sichtbar nur freiwillig Eingetragenes.
  - Operative Konsequenz: Keine Werbe- oder Trackingcookies; notwendige sichere Sitzungscookies; Sichtbarkeit nur durch bewusst gesetzte Garnrollen-Verortung.

---

## 3 · Systemlogik („Plan“)

### 3.1 Domänenmodell

- **Rolle / Garnrolle**
  - Beschreibung: Verifizierter Nutzer (Account) + Position + Privat-/Öffentlich-Bereich.
- **Knoten**
  - Beschreibung: Informations- oder Ereignis-Bündel (Idee, Ressource, Ort …).
- **Faden**
  - Beschreibung: Verbindung zwischen Rolle und Knoten (Handlung).
- **Garn**
  - Beschreibung: Dauerhafte, verzwirnte Verbindung = Bestandsschutz.

### 3.2 Zeit und Prozesse

- **7-Sekunden-Rotation** → sichtbares Feedback nach Aktion.
- **7-Tage-Verblassen** → nicht verzwirnte Fäden/Knoten lösen sich auf.
- **7 + 7-Tage-Modell** → Anträge: Einspruch → Abstimmung.
- **Delegation (Liquid Democracy)** → verfällt nach 4 Wochen Inaktivität.
- **Tombstone-/Anonymisierungsprozess** → Beiträge verlieren nach gewählter Frist die sichtbare Rückbindung an die Garnrolle.

---

## 4 · Ethisch-technische Defaults

- Sichtbarkeit neu abgeleiteter, unverzwirnter Fäden
  - Verfassungsfester Wert: 7 Tage.
  - Kein Runtime-Schalter: `fade_days` und `HA_FADE_DAYS` werden als entfernte Scheinsteuerung abgewiesen.
  - Herkunft: Domänenvertrag und fest gebundene Serverlogik.
- Anonymisierung (`anonymization_valid_days`)
  - Richtwert: 28 Tage (Delegations-Analogon).
  - Herkunft: Geist & Plan-Ableitung.
- Anonymisierung (`default_anonymized`)
  - Richtwert: *nicht festgelegt*, nur „Opt-in möglich“.
  - Herkunft: zusammenstellung.md, Abschnitt III.
- Ortsdaten (`ungenauigkeitsradius_m`)
  - Richtwert: individuell einstellbar.
  - Herkunft: zusammenstellung.md, Abschnitt III.
- Delegation
  - Richtwert: 28 Tage (4 Wochen).
  - Kein Runtime-Schalter: `delegation_expire_days` und `HA_DELEGATION_EXPIRE_DAYS` werden als wirkungslose Scheinsteuerung abgewiesen.
  - Herkunft: § IV Delegation; eine technische Frist benötigt zuerst einen eindeutigen Runtime-Verbraucher.

> **Hinweis:** Die Fadenfrist von 7 Tagen ist verfassungsfest; das 7+7-Tage-Modell
> für Anträge ist im kanonischen Governance-Vertrag normativ festgelegt. Die
> 28-Tage-Werte für Anonymisierung und Delegation bleiben hier Richtwerte.
> Änderungen erfordern den jeweils einschlägigen Governance-Beschluss und einen
> Changelog-Eintrag.

---

## 5 · Governance-Matrix

- Antrag
  - Dauer: 7 Tage + 7 Tage.
  - Sichtbarkeit: öffentlich.
  - Trigger: Timer oder Einspruch.
- Delegation
  - Dauer: 4 Wochen.
  - Sichtbarkeit: transparent (gestrichelte Linien).
  - Trigger: Inaktivität.
- Meldung / Freeze
  - Dauer: 24 h.
  - Sichtbarkeit: eingeklappt.
  - Trigger: Moderations-Vote.
- Tombstone-/Anonymisierungsprozess
  - Dauer: variable x Tage.
  - Sichtbarkeit: ohne sichtbare Rückbindung an die ursprüngliche Garnrolle.
  - Trigger: User-Opt-in.

---

## 6 · Technische Leitplanken

- **Aktueller Kern:** Rust/Axum-API, statisches SvelteKit-Web, Docker Compose und Caddy.
- **Datenwahrheit:** Der Produktionsvertrag nutzt PostgreSQL für Domänen-Lesen,
  alle vorhandenen Domänenschreibpfade und Passkey-Credentials. JSONL bleibt
  lokaler Code-Default sowie dokumentierter Import-, Test- und Rückfallpfad;
  Liveaussagen brauchen datierte Runtime-Evidence.
- **Events und Beobachtung:** NATS sowie Prometheus-/Grafana-/Loki-/Tempo-Konfigurationen sind vorhanden, aber kein vollständiges Event-Sourcing- oder Observability-System ist allgemein produktiv belegt.
- **Security:** sichere Sitzungen, Proxygrenze, CI-/Contract-Checks und secret-sichere Betriebsabläufe sind reale Leitplanken; SBOM, Signaturen, automatische Rotation und Forget-Pipeline bleiben gesondert nachzuweisende Ziele.
- **Skalierung:** Compose ist die aktuelle Betriebsrealität. Kubernetes ist nach ADR-0010 die kanonische Zielplattform, aber noch keine heutige Runtime- oder HA-Wahrheit. Nomad ist keine neue Primärzielrichtung.
- **Garnrollen-Sichtbarkeit:** Sichtbarkeit und Verortung sind ausschließlich Eigenschaften einer Garnrolle (`not_on_map`, `exact`, `radius`); eine zweite Identität existiert nicht.

---

## 7 · Design-Ethik → UX-Richtlinien

1. **Transparente Zeitlichkeit:** Fade-Animationen zeigen Vergänglichkeit, nicht Löschung.
2. **Partizipative Interface-Metaphern:** Rollen drehen, Fäden fließen – Verantwortung wird
   sichtbar.
3. **Reversible Aktionen:** Alles ist änder- oder verzwirnbar, aber nicht heimlich.
4. **Sichtbarkeitskontrollen direkt im Profil:** Noch nicht auf Karte, exakt sichtbar, im Umkreis sichtbar.
5. **Lokale Sichtbarkeit:** Sichtbarkeit wird bewusst gewählt; Distanz- und Umkreislogik bleibt nachvollziehbar.
6. **Keine versteckte Gamification:** Engagement wird nicht bewertet, nur sichtbar gemacht.

---

## 8 · Weiterer Fahrplan (Querschnitt aus fahrplan.md)

- Phase A
  - Ziel: Minimal-Web (SvelteKit + Map).
  - Ethik-Bezug: Transparenz sichtbar machen – Karte hallo sagen.
- Phase B
  - Ziel: API + Health + Contracts.
  - Ethik-Bezug: Nachvollziehbarkeit von Aktionen.
- Phase C
  - Ziel: Privacy UI + 7-Tage-Verblassen.
  - Ethik-Bezug: Privacy by Design erlebbar machen.
- Phase D
  - Ziel: Persistenz + Outbox-Hook.
  - Ethik-Bezug: Integrität von Ereignissen.
- Phase …
  - Ziel: Langfristig Föderation + Delegations-Audits.
  - Ethik-Bezug: Verantwortung skaliert halten.

## 9 · Governance / Changelog-Pflicht

Alle Änderungen an:

- Datenschutz- oder Ethikparametern.
- Governance-Timern.
- Sichtbarkeits-Mechaniken.

---

## 10 · Build- und CI-Policy

Folgende Grundsätze gelten für lokale Entwicklung und CI:

- **Lokales Tooling (`scripts/tools/yq-pin.sh`)** hält `yq` ohne Root-Rechte aktuell. Das Skript
  erkennt Binär-/Tarball-Varianten, prüft Checksums und nutzt erweiterte Curl-Retries, um
  Entwickler:innen auf Workstations oder Codespaces unabhängig vom Runner-Setup zu machen.
- **GitHub Actions** installieren `yq` über eine eigene, schlanke Routine in
  `.github/workflows/ci.yml`. Sie arbeiten auf frischen Images, installieren direkt nach
  `/usr/local/bin` und vermeiden Seiteneffekte aus `$HOME`.
- **Link-Checks:** Das CI setzt auf eine „flake-freie“ Konfiguration (`--retry`, limitierte
  Parallelität) als Blocker. Der separate Watchdog `links.yml` läuft nachts bzw. manuell und
  meldet Ausfälle, bricht aber keine Deployments.
