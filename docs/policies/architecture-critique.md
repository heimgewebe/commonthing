---
id: docs.policies.architecture-critique
title: "Architekturkritik-Skill: weltgewebe.architecture.critique"
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
summary: Kognitives Protokoll für strukturelle Architekturkritik, Evidenzdisziplin und Architekturreviews.
relations:
  - type: relates_to
    target: docs/policies/agent-reading-protocol.md
  - type: relates_to
    target: repo.meta.yaml
  - type: relates_to
    target: docs/weltgewebe-agenten-manifest.md
  - type: relates_to
    target: docs/specs/garnrolle-knoten-faden.md
---

# weltgewebe.architecture.critique

Status: canonical — maßgebliches kognitives Protokoll für Architekturkritik im Weltgewebe.

Begründung der Kanonisierung:
Dieser Skill wird als canonical geführt, da Architekturkritik als verbindliches kognitives Protokoll für Agenten im Weltgewebe etabliert wird. Ziel ist die systematische Aufdeckung struktureller Schwächen, semantischer Brüche und epistemischer Inkonsistenzen über alle Module hinweg. Der Skill ersetzt keine fachlichen Entscheidungen, sondern standardisiert deren kritische Prüfung.

Einordnung im Truth Model:
Dieses Dokument ist ein kanonisches Policy-Dokument. Seine Einordnung und sein normativer Vorrang ergeben sich ausschließlich aus der in `repo.meta.yaml` definierten Truth-Model-Precedence.

---

## 0. Meta-Definition

Architekturkritik = Prüfung der Denkstruktur, nicht der Implementierungsdetails.

Bewertet wird:

- nicht primär, ob etwas funktioniert
- sondern, ob es sinnvoll ist, dass es so gebaut wird
- und ob es so gebaut wird, wie beschlossen wurde

Dieses Skill operiert im Rahmen des Truth-Modells aus `repo.meta.yaml`:

```text
domain_contracts
  > canonical_policies
  > runtime_configs_and_code
  > normative_specifications
  > diagnostic_reports
  > navigational_indices
  > generated_diagnostics
```

Jede Aussage muss einer dieser Klassen zugeordnet sein.
„Das System tut X" → belegt in `runtime_configs_and_code`.
„Das System soll X" → belegt in `normative_specifications` oder `canonical_policies`.
Klassenverwechslung ist der häufigste epistemische Fehler — muss explizit sichtbar gemacht werden.

---

## 0.1 Geltungsbereich (Pflicht, vor jeder Analyse deklarieren)

| Wert | Bedeutung | Kritiktiefe |
|------|-----------|-------------|
| `System` | Gesamtes Weltgewebe-System | Vollständig |
| `Modul` | Einzelnes Subsystem (API, Web, Auth, Map, Identity) | Strukturell |
| `Entscheidung` | Einzelner ADR oder Blueprint | Strukturell |
| `PR/Diff` | Konkrete Implementierungsänderung | Essenziell |

---

## 0.2 Kritiktiefe (Pflicht, automatisch bestimmt und deklariert)

| Tiefe | Aktive Sektionen |
|-------|-----------------|
| `Vollständig` | 1–10 |
| `Strukturell` | 1, 2, 6, 7, 10 |
| `Essenziell` | 2, 10 |

Sektionen außerhalb der gewählten Tiefe werden weggelassen — ohne Erklärung.

---

## 1. Zweck

Erzwingt:

- strukturelle Architekturkritik
- Aufdeckung impliziter Annahmen
- Identifikation systemischer Schwächen
- Kohärenzprüfung: Truth Model / Semantik / Runtime

Ziel: Selbsttäuschung verhindern.

ADR-gebundene Entscheidungen: Implementierungstreue prüfen, nicht Entscheidungsqualität neu bewerten.
Wiederöffnung eines `accepted` ADR erfordert expliziten Vermerk:
„ADR-[Nr] wird zur Revision vorgeschlagen, weil [konkreter Befund]."

---

## 2. Diagnose-Gate

Wenn zwingend notwendige (nicht nur hilfreiche) Quellen fehlen:
→ Abbruch: „X fehlt, nötig für Y"

Wenn Quellen vorhanden, aber für eine starke Aussage nicht ausreichen:
→ nur belegte Teilanalyse;
→ fehlende Evidenz als Leerstelle markieren;
→ Schlussfolgerung ggf. als „nicht entscheidbar“ ausweisen

---

## 3. Evidenz- und Ableitungsregeln

- **Faktische Interpolation**:
  Erfinden von Implementierungsdetails oder Tatsachen, die aus vorhandenen Dateien, Contracts oder Outputs ableitbar oder nachlesbar wären.
  → VERBOTEN, Abbruch

- **Strukturelle Ableitung**:
  Schlussfolgerung aus belegten Mustern oder dokumentierten Zusammenhängen.
  → ERLAUBT, aber als „plausibel“ markieren; darf keine belegten Aussagen ersetzen

- **Konzeptuelle Hypothese**:
  Gedankliche Prüfung einer möglichen Systemannahme oder Zielstruktur.
  → ERLAUBT nur als „spekulativ“; darf keine tragende Diagnosebasis sein

Nur faktische Interpolation ist verboten. Ableitungen und Hypothesen müssen klar als solche markiert werden. Wo keine tragfähige Evidenz vorliegt: „nicht entscheidbar auf Basis der vorliegenden Quellen“.

---

## 4. Analysesequenz (verpflichtend, sequenziell)

Reihenfolge bindend. Ebene 3 darf nicht ohne Ebene 1 und 2 einsetzen.

### Ebene 1 — Evidenz (belegt)

Was ist nachweisbar vorhanden? Jede Aussage mit Dateipfad oder Verhaltensbeschreibung verankern. Truth-Klasse benennen.

### Ebene 2 — Modellierung (plausibel)

Wie ist das System gedacht? Nur Ableitungen aus vorhandener Evidenz — keine freien Hypothesen.

### Ebene 3 — Urteil (kritisch)

Ist diese Modellierung tragfähig? Darf und soll widersprechen. Unsicherheitsgrad explizit machen.

---

## 5. Kritische Selbstdisziplin

Diese Disziplinregeln steuern, wie die folgenden Prüfachsen und Pflichtsektionen anzuwenden sind.

### 5.1 Evidenz- und Härtedisziplin

- **Klassifikation:** Jeder Hauptbefund ist nach Evidenz zu klassifizieren (belegt, plausibel, spekulativ). Zusätzlich kann ein Befund mit dem Revisions-Flag „zu korrigieren“ markiert werden. Dieses kennzeichnet explizit zurückzunehmende frühere Fehlannahmen oder überdehnte Diagnosen. Beweisart und fehlende Informationen („X fehlt, nötig für Y“) sind zu benennen.
- **Saubere Härte:** Begriffe wie „toter Code“, „unbenutzt“ oder „Legacy-Rest“ dürfen nur bei explizitem Nachweis (z. B. Runtime-Unerreichbarkeit) verwendet werden. Sonst: „Verwendung nicht belegt“ oder „möglicherweise obsolet“.

### 5.2 Gegenlesart und Revisionsprüfung

- **Gegenhypothese:** Zu kritischen Befunden soll eine alternative, nicht-pathologische Erklärung bedacht werden (z. B. God Component → bewusster Orchestrator).
- **Zeitstand:** Es ist verpflichtend zu prüfen, ob Befunde zeitstandabhängig sind oder durch neueren Code bereits überholt sein könnten.

### 5.3 Normativitäts- und Problemtypprüfung

- **Referenztyp:** Klären, ob eine Referenz kanonisch bindend, operativ maßgeblich, diagnostisch oder ein Draft ist. Draft-Blueprints sind keine harten Architekturverstöße.
- **Problemtyp:** Befunde sind nach Art zu benennen (Namensproblem, Abstraktionsproblem, Kopplungsproblem, Laufzeitproblem, Dokumentationsdrift).

### 5.4 Test- und Selbstkritikdisziplin

- **Test-Evidenz:** Tests sind nach Evidenztyp zu unterscheiden; E2E, reale Integration, Mock-Integration und Strukturtests sind nicht gleichwertig.
- **Selbstkritik:** Jede Kritik endet mit einer Reflexion über mögliche Überdehnungen der Diagnose.

---

## 6. Prüfachsen

Achsen A–D und G: immer aktiv.
Achsen E und F: konditioniert — wenn inaktiv, explizit als „nicht anwendbar" deklarieren.

### A. Truth Model

- Widersprüche zwischen Wahrheitsquellen?
- Implizite statt explizite Wahrheit?
- Klassenverwechslung (`normative_specifications` vs. `runtime_configs_and_code`)?

→ Pflichtstruktur §7 / Punkt 2 (Diagnose) + Punkt 5 (Architekturkritik)

### B. Contracts

- Fehlend, zu schwach, zu strikt oder umgangen?
- ADR-Status: `accepted` → nur Fidelity-Check; kein ADR → offene Frage markieren; `superseded` → Treue zum Nachfolger-ADR prüfen

→ Pflichtstruktur §7 / Punkt 2 (Diagnose) + Punkt 5 (Architekturkritik)

### C. Semantik

- Begriff ≠ Verhalten?
- Bedeutungsdrift zwischen Docs und Code?

→ Pflichtstruktur §7 / Punkt 1 (Dialektik) + Punkt 4 (Versteckte Annahmen)

### D. Runtime vs. Dokumentation

- Behauptung ≠ Implementierung? Tote Dokumentation?
- Zeigt `docs/_generated/impl-index.md` nicht dokumentierte Implementierungen?
  Es gibt keinen automatischen Drift-Report: strukturelle Drift ist hier von
  Hand zu belegen.

→ Pflichtstruktur §7 / Punkt 2 (Diagnose) + Punkt 7 (Risikoanalyse: epistemisch)

### E. Kartenarchitektur *(aktiv nur bei: Kartenrendering, Basemap-Pipeline, Overlay-Architektur, räumliche Semantik)*

- Rendering oder semantischer Kern?
- Basemap = Infrastruktur, Overlay = Weltgewebe-Semantik — korrekt getrennt?

→ Pflichtstruktur §7 / Punkt 5 (Architekturkritik) + Punkt 8 (Alternative Sinnachse)

### F. Garnrollen- und Sichtbarkeitssystem *(aktiv nur bei: Garnrolle, Verortung, Auth-Identität, Sichtbarkeitslogik)*

- Garnrollen-Sichtbarkeit konsistent (nicht auf Karte, exakt sichtbar, im Umkreis sichtbar)?
- Vertrauen als Systemwert modelliert? (im Weltgewebe verboten)

→ Pflichtstruktur §7 / Punkt 5 (Architekturkritik) + Punkt 8 (Alternative Sinnachse)

### G. Komplexität

- Notwendig vs. künstlich? Overengineering?
- Durch Systemgrenzen gerechtfertigt?

→ Pflichtstruktur §7 / Punkt 3 (Kontrastprüfung) + Punkt 6 (Alternativpfad)

---

## 7. Pflichtstruktur (je nach Kritiktiefe)

### 1. Dialektik *(Vollständig, Strukturell)*

- **These:** Stärkste wohlwollende Lesart
- **Antithese:** Stärkste kritische Lesart (muss mindestens eine valide Gegenlesart enthalten, nicht nur Abschwächung)
- **Synthese:** Tragfähige Einordnung

---

### 2. Diagnose *(immer aktiv)*

Beginnt mit **Befundklasse** (Pflicht, Befundklasse muss mit Evidenzlage begründet werden, nicht nur summarisch):

| Klasse | Bedeutung |
|--------|-----------|
| **A** | Kritisch: strukturelle Fehler, Tragfähigkeit in Frage |
| **B** | Warnung: potenzielle Schwächen, kontextabhängig |
| **C** | Bedingt sound: Schwächen bekannt und bewusst akzeptiert |
| **D** | Sound: keine tragfähigkeitsgefährdenden Befunde (Belegpflicht) |

Befundklasse D: Soundness muss earned sein — „X ist sound, weil Y nachweisbar ist."
Keine blanken Zertifikate.

Dann Evidenzgradierung:

- **Belegt** (mit Quellenangabe)
- **Plausibel** (als strukturelle Ableitung markieren)
- **Spekulativ** (als konzeptuelle Hypothese markieren)

---

### 3. Kontrastprüfung *(Vollständig)*

- **Lesart A:** System funktioniert — warum?
- **Lesart B:** System ist grundlegend fehlerhaft — warum?

---

### 4. Versteckte Annahmen *(Vollständig)*

- Welche Prämissen werden still angenommen?
- Welche könnten falsch sein?

---

### 5. Architekturkritik *(Vollständig, Strukturell)*

Mit Achsenzuordnung (A–G, nur aktive Achsen):

- Strukturelle Schwächen
- Semantische Brüche
- Unnötige Komplexität
- Mögliche Sackgassen

(Befunde sollen Evidenzklasse, Problemtyp und Gegenhypothese benennen)

---

### 6. Alternativpfad *(Vollständig, Strukturell — nur bei Befundklasse A oder B)*

Grundlegend andere Denkweise — kein Variantenwechsel (erfordert strukturelles Neu-Denken, nicht nur eine andere Implementierungsvariante).

---

### 7. Risikoanalyse *(Vollständig)*

- technisch
- semantisch
- epistemisch
- organisatorisch

Inkl. Folgenabschätzung.

---

### 8. Alternative Sinnachse *(Vollständig)*

Mindestens eine der folgenden Fragen beantworten:

- Ist die Karte das falsche Zentrum?
- Ist Identität falsch modelliert?
- Ist Vertrauen falsch gedacht?
- Wird Komplexität mit Tiefe verwechselt?

---

### 9. Essenz + Folgepfad *(immer aktiv)*

**Essenz:** Hebel — Entscheidung — nächste Aktion

Strukturierung der Hebel:

1. Größter Hebel (geringer Aufwand, hohe Wirkung)
2. Struktureller Hebel
3. Später Ausbaupfad
4. Wahrscheinlichste Überkorrektur

#### Folgepfad

| Befundklasse | Aktion |
|-------------|--------|
| A | ADR-Entwurf vorschlagen oder zur Revision markieren |
| B | Blueprint-Ergänzung oder Spec-Update vorschlagen |
| C | Drift in `docs/_generated/` adressierbar? Markieren. |
| D | Keine Aktion. Befundklasse D und Begründung dokumentieren. |

#### Unsicherheits- und Evidenzlage

- Unsicherheitsgrad (0–1) + Ursachen
- Evidenzstatus:
  - vollständig belegt
  - teilweise belegt
  - nicht entscheidbar
- Offene Lücken:
  - X fehlt, nötig für Y

#### Selbstkritische Restprüfung

- Wahrscheinlichste Überdehnung der Diagnose
- Unsicherste Aussage
- Stelle mit größtem Interpretationsanteil

---

## 8. Anti-Fehler-Regeln

Darf NICHT:

- glätten
- implizit zustimmen
- faktisch fehlende Daten ergänzen (→ Abbruch)
- nur Code reviewen
- `accepted` ADRs ohne expliziten Vermerk zur Revision vorschlagen
- Befundklasse D ohne Belegpflicht vergeben

Muss:

- widersprechen können
- Unsicherheit sichtbar machen
- alternative Deutungen liefern
- Denkfehler korrigieren
- zwischen Diagnose und Entscheidung unterscheiden

---

## 9. Eskalation

Bei widersprüchlicher Architektur, fragilen Kernprämissen oder erkennbarer Sackgasse:

→ „Dieses System ist in seiner aktuellen Form wahrscheinlich nicht tragfähig, weil …"

Eskalation ist Diagnose (Befundklasse A), kein Governance-Beschluss.
Welche Konsequenz gezogen wird, liegt beim menschlichen Projekt-Collective.
Der Agent markiert und begründet — er entscheidet nicht.

---

## 10. Prämissencheck *(Vollständig)*

Was müsste wahr sein, damit das System so funktioniert?
Was ist belegt — was ist Annahme?

---

## Essenz des Skills

Kritik vor Lösung.
Struktur vor Detail.
Wahrheit vor Komfort.

---

## Unsicherheitsgrad

0.08

Ursachen:

- Achsen-zu-Sektionen-Zuordnung ist normativ, nicht empirisch validiert
- Kritiktiefe-Schwellen könnten in der Praxis anders kalibriert werden müssen

---

## Evidenz und Ableitungen

**0.11** · Typ: konzeptuelle Hypothese

Annahmen:

- LLMs befolgen sequentielle Analysesequenzen kohärenter als parallele Anweisungen
- Befundklasse D wird nicht als Ausrede für oberflächliche Analyse missbraucht
- Kritiktiefe `Essenziell` reicht für PR-Diffs ohne kritische Befunde zu unterdrücken
