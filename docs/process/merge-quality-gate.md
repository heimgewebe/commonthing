---
id: process.merge-quality-gate
title: Merge-Qualitätsgate
doc_type: policy
status: active
summary: Verbindlicher, risikogewichteter Mergeprozess mit exakten Diff- und Reviewbelegen.
relations:
  - type: relates_to
    target: .github/PULL_REQUEST_TEMPLATE.md
  - type: relates_to
    target: .github/grabowski-required-checks.json
  - type: relates_to
    target: .github/workflows/ci.yml
  - type: relates_to
    target: .github/workflows/review-evidence.yml
  - type: relates_to
    target: scripts/quality/review_governance.py
  - type: relates_to
    target: scripts/quality/tests/test_review_governance.py
---

# Merge-Qualitätsgate

Dieses Dokument beschreibt den verbindlichen Qualitätsprozess für nichttriviale
Änderungen an commonThing. Ziel ist nicht möglichst viel Bürokratie, sondern eine
prüfbare Bindung zwischen dem tatsächlich gemergten Code und genau dem Diff, der
geprüft wurde.

## Einen PR zügig mergefähig machen

Der Normalfall soll ohne manuelle Hasharbeit auskommen:

1. **R0 – kleine reine Markdown-Änderung:** Risikomarker setzen und CI abwarten.
   Eine externe Review ist nicht erforderlich. Dies gilt auch für Markdown-Dateien
   unter `apps/`, solange höchstens 50 Zeilen geändert werden und kein sensibler
   Pfad betroffen ist.
2. **R1 – kleine Konfiguration, Metadaten oder sichere Rastergrafik:** Risikomarker
   setzen und eine normale GitHub-Review mit `Approve` auf dem aktuellen Head
   einholen. Ein JSON-Belegblock ist nicht erforderlich. Bei internen Branches
   wird der Status automatisch neu berechnet. Bei Fork- und Dependabot-PRs postet
   ein Maintainer danach `/review-evidence recheck`; dieser sichere
   Default-Branch-Pfad besitzt die nötige Statusberechtigung.
3. **R2/R3 – Produktlogik oder sicherheitsrelevante Änderung:** Das automatisch
   erzeugte Reviewpaket verwenden und die vollständigen hashgebundenen Berichte
   als PR-Kommentare attestieren. Zwei verschiedene Prüfer bleiben der bevorzugte
   Normalfall. Ist ein Fremdreview nach einem dokumentierten Versuch wegen Quote,
   Authentifizierung, Transport oder Werkzeugausfall nicht verfügbar, darf derselbe
   Prüfer einen zweiten, methodisch getrennten Self-Review durchführen. Dafür müssen
   die Belege `review_mode: self-primary` und `review_mode: self-fallback`,
   unterschiedliche `review_session`-Werte, unterschiedliche Reviewachsen und
   Berichtshashes sowie ein konkreter `fallback_reason` vorliegen. Diese Ausnahme
   behauptet keine personelle oder technische Unabhängigkeit.
   Alternativ darf ein in der Attester-Liste autorisierter Maintainer einen von
   außen eingeholten und an den Operator weitergereichten vollständigen Reviewbericht
   mit `weltgewebe-forwarded-review` attestieren. Dieser vereinfachte Beleg muss
   `base_sha`, `head_sha` und `diff_sha256` sowie Prüferidentität, Reviewachse,
   Urteil und den Status der Befundauflösung enthalten; der Gate-Code berechnet
   den Berichtshash selbst. Ein Basis-, Head- oder Diff-Wechsel macht den Beleg
   automatisch ungültig. Die Hashfelder kann der Operator aus dem aktuellen
   Reviewpaket übernehmen; der Nutzer muss sie nicht manuell berechnen.

Bei jedem Push werden alte Freigaben nur dann weitergezählt, wenn GitHub sie
ausdrücklich an den neuen Head bindet. Für R2 und R3 bleiben frühere Berichte
immer ungültig, sobald sich Head, Basis oder Diff ändern.

## Grundprinzip

Ein Merge ist nur zulässig, wenn:

1. Basis- und Head-Commit eindeutig feststehen;
2. ein vollständiges Reviewpaket für genau diesen Diff vorliegt;
3. alle erforderlichen Prüfungen grün sind;
4. die risikogewichteten Reviewbelege exakt zu Basis, Head und Diff passen;
5. unmittelbar vor dem Merge kein Basis-, Head- oder Diffwechsel stattgefunden hat;
6. der Merge und gegebenenfalls der Produktionsabschluss separat zurückgelesen wurden.

## Risikoklassen

Jeder Pull Request enthält genau einen maschinenlesbaren Marker:

```text
<!-- weltgewebe-risk: R0|R1|R2|R3 -->
```

| Klasse | Beispiele | Erforderliche Reviews |
| --- | --- | --- |
| R0 | kleine reine Markdown-Änderung, höchstens 50 geänderte Zeilen | keine externe Reviewpflicht |
| R1 | Konfiguration, Metadaten, sichere Rastergrafiken | eine native GitHub-Freigabe auf dem aktuellen Head oder ein exakter PASS-Beleg |
| R2 | Produktlogik, API, UI, Persistenz, nichtprivilegierte CI | zwei hashgebundene Berichte und zwei Reviewachsen; zwei Prüfer oder explizites Self-Review-Fallback |
| R3 | Authentifizierung, Datenschutz, Sicherheit, Migration, Deployment, privilegierte Workflows | zwei Berichte und zwei Reviewachsen; zwei Prüfer oder explizites Self-Review-Fallback; mindestens eine Hochrisikoachse |

Bestimmte Pfade erzwingen automatisch eine Mindestklasse. Eine niedrigere
Deklaration führt fail-closed zum Gatefehler. R0 ist nur für kleine reine
Markdown-Änderungen zulässig. Der Dateipfad allein macht eine README unter
`apps/` nicht zu Produktcode; sensible Dokumentationspfade wie Deploy- und
Runbook-Dokumente sowie das PR-Template und die Governance-Implementierung
bleiben dagegen R3.

## Reviewpaket

Der Workflow `Review Evidence Gate` führt ausschließlich Code aus dem wörtlich
festgelegten Branch `main` aus. Er checkt keinen PR-Code aus, lädt keine PR-Gitobjekte
und führt keinen PR-Code aus. Das ist wichtig, weil `pull_request_target` und
`issue_comment` einen Token besitzen, der Commitstatus setzen darf.

Der PR wird ausschließlich über die GitHub-API als Diff-, Patch- und JSON-Datenstrom
gelesen. Vor und nach dem Download werden Basis- und Head-SHA verglichen. Der
Workflow erzeugt:

- `weltgewebe-pr-<nr>-<head>-<diffhash>.diff` – den von GitHub gelieferten
  Textdiff, dessen Dateiblockzahl gegen die vollständige Dateiliste geprüft wird;
- `*.patch` – die von GitHub gelieferte Patchdarstellung;
- `*.review.json` – Bindung, Hashes, Dateiliste, Umfang und nicht im Textdiff
  dargestellte Dateien;
- `*.review-request.md` – fertiger Auftrag für einen externen Prüfer;
- `evaluation.json` – maschinenlesbares Gate-Urteil.

Der Reviewbeleg muss mindestens enthalten:

```json
{
  "schema_version": 1,
  "pr_number": 123,
  "base_sha": "40-stellige SHA",
  "head_sha": "40-stellige SHA",
  "diff_sha256": "64-stellige SHA-256",
  "risk_class": "R2",
  "reviewer": "eindeutige Prüferidentität",
  "report_sha256": "SHA-256 des Reviewtexts vor dem Marker",
  "review_axis": "correctness",
  "verdict": "PASS",
  "findings_resolved": true
}
```

Der vollständige Reviewbericht steht als normaler Kommentartext vor dem Marker.
Der Beleg folgt genau einmal im selben Kommentar:

```text
<VOLLSTÄNDIGER REVIEWBERICHT>
<!-- weltgewebe-review-evidence
{...}
-->
```

`report_sha256` muss dem SHA-256 des getrimmten UTF-8-Texts vor dem Marker
entsprechen. Vor dem Hashen werden `CRLF` und einzelne `CR` auf `LF` normalisiert,
damit Browser und Betriebssysteme denselben Berichtshash erzeugen. Berichte unter
120 Byte, mehrere Belegblöcke in einem Kommentar oder doppelt verwendete
Berichtshashes zählen nicht als getrennte Reviews.

Normale und weitergeleitete Fremdreviews benötigen keine Zusatzfelder. Für die
Self-Review-Ausnahme sind im selben Beleg zusätzlich `review_mode`,
`review_session` und beim Fallback `fallback_reason` erforderlich. Zulässig sind
`self-primary` und `self-fallback`. Der Sitzungsbezeichner ist kein Identitätsbeweis,
sondern erzwingt zwei ausdrücklich getrennte Prüfpässe. Ein Self-Review-Fallback
ersetzt nur die zweite Prüferidentität; zwei Achsen, zwei Berichtshashes, die
Hochrisikoachse bei R3 und alle exakten SHA-Bindungen bleiben unverändert.

Für R1 kann statt des Belegblocks eine native GitHub-Review mit `Approve` zählen.
Sie muss von einem durch GitHub als `OWNER`, `MEMBER` oder `COLLABORATOR`
ausgewiesenen Prüfer stammen und exakt den aktuellen Head-Commit betreffen. Eine
aktuelle `Changes requested`-Review blockiert. Native Freigaben ersetzen die
ausführlichen Berichte für R2 und R3 ausdrücklich nicht.

GitHub stuft Tokens für Reviewereignisse aus Forks sowie für Review- und
`pull_request_target`-Ereignisse von Dependabot auf read-only herab. Diese Läufe
werden deshalb bewusst übersprungen, statt mit einem irreführenden 403 zu
scheitern. Ein Maintainer löst nach der Freigabe mit `/review-evidence recheck`
einen `issue_comment`-Lauf auf dem vertrauenswürdigen Default-Branch aus.
Kommentare ohne Repositoryrolle sowie gewöhnliche Diskussionskommentare ohne
Belegmarker oder Recheck-Befehl starten keinen privilegierten Lauf. Bearbeitung
oder Löschung eines Belegkommentars löst dagegen eine erneute Auswertung aus,
damit ein früherer Erfolgsstatus nicht stehen bleibt.

Die PR-bezogene Concurrency liegt absichtlich auf Job-Ebene. Erst die obige
Berechtigungsprüfung lässt einen Evaluator in die gemeinsame PR-Gruppe eintreten.
Irrelevante oder schreibgeschützte Ereignisse können daher weder einen laufenden
noch einen wartenden berechtigten Evaluator verdrängen.

Nicht textuell dargestellte Dateien erscheinen als `opaque_files`. Häufige
Rasterformate (`png`, `jpg`, `jpeg`, `gif`, `webp`, `avif`, `ico`) sind in den
festgelegten Doku- und Web-Assetpfaden visuell über den GitHub-Dateidiff prüfbar
und erzwingen mindestens R1. PDF, SVG, Archive, ausführbare Dateien und Rasterbilder
außerhalb dieser Pfade blockieren weiterhin fail-closed. Damit sind alltägliche
Bildänderungen möglich, ohne undurchsichtige Artefakte pauschal freizugeben.

Ein neuer Push, ein Basiswechsel oder ein anderer Diffhash entwertet den Beleg
automatisch. Für dieselbe Kombination aus Prüfer und Reviewachse zählt nur der
zeitlich neueste exakte Beleg. Ein späteres `BLOCKED` oder `FAIL` hebt einen früheren
PASS derselben Kombination auf. Der Workflow liest Kommentare und native Reviews
unmittelbar vor der Statusveröffentlichung erneut. Gleichzeitige Reviewereignisse
konvergieren dadurch auf den frischesten GitHub-Zustand; Zeitstempel werden als
UTC-Datumswerte und bei Gleichstand über stabile GitHub-IDs geordnet.

## Erforderliche Prüfungen

Die Datei `.github/grabowski-required-checks.json` enthält ausschließlich die
**primären** Checks, die Grabowski vor einem Merge direkt auswertet. Abgeleitete
Review-Statusprojektionen dürfen dort nicht erneut als Eingabe erscheinen, weil
sonst das Review-Gate von seinem eigenen Ergebnis abhängen würde. Aktuell enthält
der Katalog deshalb:

- `Required merge gate`.

Der Required-Merge-Gate-Job bleibt der vorhandene CI-Aggregator. Das
`Review evidence gate` ergänzt ihn weiterhin um die exakte Reviewbindung, ist aber
bewusst **kein Eintrag** im Grabowski-Eingabekatalog. Es bleibt unabhängig davon
ein eigener harter Commitstatus des GitHub-Rulesets. Der GitHub-Ruleset wird
separat verwaltet; eine Datei im Repository kann ihn weder beweisen noch ändern.

## Bootstrap

Der Workflow kann seine eigene Einführung nicht vor seinem ersten Merge aus dem
Default-Branch ausführen. Für den Bootstrap gelten daher dieselben Bindungen
manuell:

1. finalen Head und aktuellen Basiscommit lesen;
2. vollständigen aktuellen GitHub-Diff und Patch erzeugen;
3. SHA-256 und Dateiliste sichern;
4. zwei R3-Reviews einholen; beim dokumentierten Ausfall eines Fremdreviews darf das explizite Self-Review-Fallback verwendet werden;
5. jeden Beleg an Basis, Head und Diff-SHA-256 binden;
6. erst danach mergen;
7. den neuen Workflow gegen einen offenen PR live auslösen;
8. Artefakte, Commitstatus und Ruleset separat zurücklesen.

Die Datei `.github/grabowski-required-checks.json` ist die maschinenlesbare
Sollvorgabe für Grabowski. Der reale GitHub-Ruleset-Zustand muss nach Änderungen
separat zurückgelesen werden; die Repositorydatei allein ändert keinen Ruleset.

## Sicherheitsgrenze des Workflows

Der privilegierte Workflow darf niemals:

- den PR-Branch auschecken oder PR-Gitobjekte abrufen;
- Python-, Shell-, Node- oder Buildcode aus dem PR ausführen;
- Secrets an PR-Inhalte übergeben;
- Reviewbelege nichtberechtigter Kommentarautoren zählen;
- Reviewbelege zählen, deren Kommentarautor nicht zusätzlich in
  `.github/review-evidence-authorities.json` freigegeben ist;
- bei internen Fehlern erfolgreich enden.

Für R1 ist der GitHub-Reviewautor zugleich Prüfer und Attestierer. Für R2/R3 kann
ein Review dagegen von einem externen Prüfer stammen und durch einen freigegebenen
Attestierer in den PR übertragen werden. Das Feld `reviewer` bezeichnet dann den
vom Attestierer benannten Prüfer; der GitHub-Kommentarautor muss zusätzlich in der
versionierten Allowlist `.github/review-evidence-authorities.json` stehen.
Repositoryrollen allein reichen nicht aus. Der Hash bindet die Attestation an den
sichtbaren vollständigen Bericht. Für R2 und R3 müssen Reviewachse und Berichtshash jeweils verschieden sein.
Im Normalfall müssen auch die Prüferidentitäten verschieden sein. Nur das explizite
`self-primary`/`self-fallback`-Paar darf dieselbe Prüferidentität verwenden; es
benötigt zwei verschiedene Sitzungsbezeichner und einen konkreten Ausfallgrund.
Technisch bewiesen werden die Identität des Attestierers, der Berichtshash, die
Diffbindung und die formale Trennung der Prüfpässe – nicht eine unabhängige
Authentifizierung der frei benannten externen Prüferidentität und auch nicht die
Unabhängigkeit zweier Self-Review-Pässe.

Alle Parser- und Bundlefunktionen stammen aus `main`. API-Daten werden nur als
Bytes oder JSON geparst. Eine unvollständige Dateiliste, ein SHA-Wechsel, eine
fehlende Patchdarstellung oder ein interner Fehler führt zu `evaluation.json` mit
`pass: false` und zu einem fehlgeschlagenen Commitstatus. Dateien ohne GitHub-
Textdarstellung werden im Manifest in visuell prüfbare Raster-Assets und
blockierende undurchsichtige Dateien getrennt. Nur die eng freigegebenen
Rasterformate und Pfade dürfen über eine aktuelle R1-Freigabe passieren.

## Merge- und Produktionsabschluss

Vor dem Merge wird der vollständige aktuelle GitHub-Diff erneut erzeugt und mit
SHA-256 gesichert. Der Mergeoperator vergleicht:

- PR-Nummer;
- Basis-SHA;
- Head-SHA;
- Diff-SHA-256;
- Reviewbelege;
- Required Checks.

Nach dem Merge werden Mergecommit und Zielbranch zurückgelesen. Wenn die Änderung
den Produktionsbetrieb betrifft, folgt getrennt davon der Deploy- und Livebeweis.
Ein erfolgreicher Merge ist kein Beweis für einen erfolgreichen Rollout.
