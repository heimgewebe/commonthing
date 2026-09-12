---
id: process.report-lifecycle
title: Report Lifecycle Policy
doc_type: policy
status: active
summary: >
  Policy für Lebenszyklus, Status, Pflichtfelder, Archivierung und Löschung
  von Reports.
relations:
  - type: relates_to
    target: docs/process/README.md
  - type: relates_to
    target: docs/_generated/report-lifecycle-inventory.md
  - type: relates_to
    target: docs/process/report-lifecycle-contract-alignment.md
---

# Report Lifecycle Policy

## Zweck

Diese Datei ist die aktive, report-spezifische Report-Lifecycle-Regel.
Sie erweitert den globalen DocMeta-Contract nicht.
Globale DocMeta-Felder oder Statuswerte benötigen einen separaten Contract-PR.

Reports sollen nicht dauerhaft als scheinbar aktuelle Wahrheit im Repo liegen.
Jeder Report soll beantworten können:

- Wozu existiert er?
- Welcher Task oder welches Vorhaben gehört dazu?
- Ist er aktiv?
- Wann muss er überprüft werden?
- Wodurch wurde er abgelöst?
- Darf er archiviert werden?
- Darf er gelöscht werden?

Diese Policy ist eine Regelgrundlage, keine rückwirkende Bereinigung.

## Geltungsbereich

**Discovery-Surface:** Alle Markdown-Dateien unter docs/reports werden rekursiv inventarisiert.

**Policy-Scope:** Die Report-Lifecycle-Regeln gelten für Report-Dateien unter docs/reports.

**Aktueller Validator-Scope:** Der Validator prüft Report-Dateien unter docs/reports rekursiv.
Er setzt die Policy weiterhin schrittweise technisch durch.

Andere `doc_type`-Werte direkt unter `docs/reports/` erscheinen im Inventory,
werden aber nicht als Reports validiert.

## Kontrollierte Nachweisflächen

Für generierte Diagnosen und den kuratierten Task-Index ist
`.wgx/generated-artifacts.yml` die einzige kanonische maschinenlesbare
Kontrollfläche. Das generierte Lifecycle-Inventar zeigt daraus nur eine
menschenlesbare Projektion und darf keine abweichende Wahrheit einführen.

Jeder Registry-Eintrag muss zusätzlich zu Quelle, Generator und Check folgende
Grenzen ausdrücklich erklären:

- `scope`: welche Daten und Aussagen tatsächlich erfasst werden;
- `consumers`: existierende Repositorypfade und der konkrete Nutzungszweck;
- `claims`: die exklusiven positiven Aussagen dieser Fläche;
- `does_not_establish`: Aussagen, die aus der Fläche nicht abgeleitet werden dürfen;
- `overlaps`: wechselseitige Verweise auf ähnliche Flächen mit fachlicher
  Abgrenzung und Verbraucherbegründung.

Dabei gelten folgende Regeln:

1. Eine fachliche Aussage darf nur von einer Kontrollfläche als eigene
   `claim` beansprucht werden. Doppelte Claims blockieren.
2. Verbraucher müssen als vorhandene, symlinkfreie Repositorypfade belegt sein.
   Ein bloß behauptetes Publikum oder ein freier Rollenname genügt nicht.
3. Überschneidungen müssen von beiden Flächen erklärt werden. Einseitige oder
   unbegründete Überlappungen blockieren.
4. Ähnliche Flächen werden nicht allein wegen Namens- oder Themenähnlichkeit
   gelöscht. Entfernen ist erst zulässig, wenn Verbraucher umgestellt,
   Sicherheits- und Revisionsbelege reproduziert und negative Rückfalltests
   vorhanden sind.
5. `_generated` bleibt rein abgeleitet. Änderungen erfolgen an Registry,
   Quellen oder Generatoren und werden anschließend reproduziert.

Damit wird vorhandene Komplexität sichtbar und prüfbar, ohne eine zusätzliche
Registry oder einen zweiten Wahrheitsvertrag einzuführen.

## Aktuelle Nicht-Ziele

- kein Massen-Backfill bestehender Reports,
- keine Erweiterung des globalen DocMeta-Contracts,
- keine automatische Archivierung,
- keine Löschung,
- keine automatische `owner_task`-Zuordnung,
- keine automatisch berechneten `review_after`-Daten,
- keine fachliche Wahrheitsbewertung von Reportinhalten.

## Implementierungsstand

### Umgesetzt

- Inventory,
- Policy,
- Alignment-Entscheidung,
- Pilot,
- report-spezifischer Validator,
- Modi `report`, `warn`, `strict`,
- changed-only strict im PR-Kontext,
- generierte Overview,
- Teil-Backfills.

### Aktueller Rollout-Stand

- vollständige Dokument-Reconciliation,
- CI global strict,
- deterministische Erzeugung beider Lifecycle-Flächen,
- Task-Control-Registrierung.

### Nachgelagert

- restliche Reportklassifikation,
- semantische Validatorhärtung,
- Archivierungsprozess,
- separate Löschprüfung.

## Geltungs- und Durchsetzungsgrenze

- Lifecycle-Felder sind als report-spezifisches Modell implementiert.
- Der globale DocMeta-Contract bleibt unverändert.
- Inventory und Validator lesen `lifecycle_state`.
- Der CI-Strict-Modus ist aktiv.
- Technische Validatorfehler wie ungefangene Import- oder Laufzeitfehler
  blockieren.
- Lifecycle-Findings sind im globalen Strict-Modus blockierend.
- Die vollständige blockierende Erkennung fehlenden oder nicht parsebaren
  Report-Frontmatters ist noch nicht umgesetzt.
- Lifecycle-Enums, `review_after` als ISO-Datum und einfache `superseded_by`-Zielpfade werden technisch geprüft.
- Relationskonsistenz bleibt nachgelagert.

## Begriffe

- **Report**: Markdown-Dokument unter docs/reports, das einen Befund,
  Audit, Proof, Status oder eine entscheidungsvorbereitende Auswertung
  beschreibt.
- **Lifecycle**: Rolle eines Reports im Lebenszyklus: warum er existiert, wie
  lange er handlungsleitend ist und wann er geprüft oder abgelöst wird.
- **Status**: globaler DocMeta-Status, zum Beispiel `draft`, `active`,
  `deprecated` oder `canonical`.
- **Lifecycle-Zustand**: report-spezifischer Zustand in `lifecycle_state`, zum
  Beispiel `active`, `superseded` oder `archived`.
- **Supersession**: explizite Ablösung durch ein anderes Artefakt.
- **Archivierung**: Report bleibt erhalten, ist aber nicht mehr
  handlungsleitend.
- **Löschung**: physisches Entfernen eines Reports aus dem Repo.
- **Primary Reference**: handgeschriebene Referenz aus fachlich relevanten
  Dokumenten.
- **Derived Reference**: generierte Referenz aus `docs/_generated/**`.

## Report-Klassen

- **audit**: prüft einen Bestand, Datenzustand oder Prozesszustand.
- **proof**: belegt eine technische oder organisatorische Eigenschaft.
- **status**: verdichtet den aktuellen Stand eines Vorhabens.
- **decision-prep**: bereitet eine Entscheidung vor, ersetzt sie aber nicht.
- **generated**: wird automatisch erzeugt und nicht manuell editiert.
- **planning**: beschreibt geplante Arbeit oder offene Schritte.
- **legacy**: historisch nützlich, aber nicht mehr handlungsleitend.

Die Klasse `proof` kann für Reports unter `docs/reports/*.md` genutzt werden.
Dateien unter `docs/proofs/**` bleiben in dieser Phase vom Geltungsbereich
ausgenommen und können später eine eigene Lifecycle-Regel bekommen.

## Status-Semantik

Bestehende DocMeta-Statuswerte behalten ihre Contract-Bedeutung.
Report-spezifische Zustände werden in `lifecycle_state` modelliert.

- **active**: aktuell handlungsleitend oder als gültiger Bezugspunkt verwendbar.
- **deferred**: bewusst zurückgestellt.
- **superseded**: durch ein anderes Artefakt ersetzt.
- **archived**: historisch erhalten, aber nicht mehr handlungsleitend.

`active` kann sowohl als DocMeta-Status als auch als `lifecycle_state`
vorkommen. Der DocMeta-Status beschreibt die allgemeine Dokumentgültigkeit;
`lifecycle_state` beschreibt die fachliche Handlungsrelevanz.

`deprecated` ist kein Papierkorb. `archived` ist keine Löschung.
`superseded` braucht eine nachvollziehbare Ablösung.

## Lifecycle-Felder

- **lifecycle**: Report-Klasse oder Lifecycle-Rolle.
- **owner_task**: verantwortlicher Task, Vorhaben, Kontrollpunkt oder Prozess.
  - für neue operative Arbeit bevorzugt eine kanonische externe `BUREAU-*`-Task-ID,
  - historische lokale IDs bleiben während der Migration über die unten genannten Repo-Quellen auflösbar,
  - abgeschlossene Tasks dürfen Proof- oder Audit-Reports weiter besitzen,
  - keine Pseudo-Task-ID nur zur Befüllung des Feldes,
  - unbelegte Eigentümerschaft bleibt als Leerstelle sichtbar.
- **review_after**: ISO-Datum `YYYY-MM-DD`, ab dem erneute Prüfung fällig wird.
  - fachlich durch Anlass, Meilenstein oder Review-Rhythmus begründet,
  - kein pauschaler 30-Tage-Default,
  - überfällig bedeutet reviewbedürftig, nicht automatisch falsch,
  - externe Provider-, DNS- oder Runtime-Claims brauchen frischen Live-Check.
- **lifecycle_state**: report-spezifischer Lifecycle-Zustand. Der Validator
  verarbeitet das Feld für Anwesenheits-, zustandsabhängige Pflichtfeld- und
  Enum-Prüfungen.
- **superseded_by**: Pfad zum ablösenden Artefakt. Der Validator prüft, dass
  ein gesetzter Pfad existiert und nicht auf denselben Report zeigt. Das Feld
  darf aber nicht alleinige Supersession-Wahrheit sein; die Relationenkonsistenz
  wird noch nicht vollständig geprüft.

`lifecycle` ersetzt `doc_type` nicht. `doc_type` beschreibt die Dokumentart;
`lifecycle` beschreibt die Rolle des Reports im Lebenszyklus.
Die Feldnamen sind verbindlich snake_case:
`lifecycle`, `owner_task`, `review_after`, `lifecycle_state`, `superseded_by`.

## Owner resolution

Das Feld `owner_task` referenziert eine stabile Arbeits- oder Prozess-ID für den verantwortlichen Task, das Vorhaben, den Kontrollpunkt oder den Prozess. Der Feldname bleibt unverändert.

Das Feld `owner` hingegen kann eine Rolle, Gruppe oder organisatorische Zuständigkeit bezeichnen. Beide Felder sind strikt getrennt und nicht austauschbar. Ein Eintrag wie `owner: docs-mechanik` macht `docs-mechanik` nicht automatisch zu einer gültigen `owner_task`-ID.

### Normative Registrierungsquellen und externe Bureau-Ownership

Für historische lokale `owner_task`-IDs bleiben während der Migration diese Repo-Quellen normativ:

- `docs/tasks/index.json`: Registriert historische strukturierte Task-Control-IDs.
- `docs/reports/optimierungsstatus.md`: Die kanonische menschliche Wahrheitsquelle für OPT-IDs.

Für kanonische `BUREAU-*`-IDs liegt die Task-Wahrheit dagegen **außerhalb dieses Repositories** bei Bureau. Eine solche ID muss fachlich einen realen Bureau-Task bezeichnen; Commonthing prüft jedoch nur die wohlgeformte Namespace-Bindung und spiegelt weder Existenz noch Status, Queue, Claims oder Priorität in `docs/tasks/index.json` oder die OPT-Matrix. Live-Auflösung und Statusprüfung gehören an die Bureau-/Operatorgrenze. Ein syntaktisch gültiges `BUREAU-*` ist deshalb im Repo eine externe Referenz, aber für sich allein kein Existenz- oder Statusbeweis.

Diese Trennung ist absichtlich: Würde Commonthing Bureau-Tasks lokal nachregistrieren oder ihren Status nachführen, entstünde erneut eine zweite Arbeitswahrheit.

### Maschinenlesbare Lookup-Fläche

Die Datei `docs/reports/optimierungsstatus.json` ist ein maschinenlesbarer Zwilling der OPT-Statusmatrix. Sie dient als Lookup-Fläche und für bestehende Driftprüfungen, besitzt aber **keine eigenständige normative Wahrheit**. Sie darf vor vollständigem Paritätsnachweis nicht allein über die fachliche Gültigkeit einer Owner-ID entscheiden.

### Historische Ownership

Ein erledigter oder geschlossener Task (Status `done`) darf weiterhin Owner eines historischen Reports bleiben. Die Auflösbarkeit der ID ist von ihrem aktuellen Status zu trennen. Eine spätere Prüfung der Statuskompatibilität zwischen Report und Owner ist nicht Teil dieses Richtlinienstandes.

### Legacy-Backfill-Ownership

Wenn ein historischer Report keinen registrierten fachlichen Task besitzt und keine
normative Quelle eine bessere Owner-ID auflöst, darf `DOCMETA-REPORT-LIFECYCLE-001`
als **Lifecycle-Pflegeowner** gesetzt werden. Diese Fallback-Ownership bedeutet
nur: Der Report wird durch den Report-Lifecycle-Rollout klassifiziert und künftig
wieder auffindbar gemacht. Sie bedeutet nicht, dass der Lifecycle-Task die
fachlichen Claims des Reports bestätigt, aktualisiert oder als Produkt-Roadmap
übernimmt.

Diese Regel ist nur für Legacy-Backfill-Slices zulässig. Neue Reports sollen
weiterhin eine fachliche, registrierte Owner-ID tragen oder die Leerstelle
explizit sichtbar lassen.

### Ungültige Platzhalter und unregistrierte Kontrollpunkte

Nicht als aufgelöst gelten:

- `TBD`, `none`, `null`
- `pending`, `pending-namespace`
- `docs-mechanik` oder andere freie Rollenbezeichnungen
- Bloß präfixförmig plausible **lokale** IDs
- Unregistrierte lokale Kontrollpunkte

Insbesondere lokale IDs wie `MAP-PROOF-001` oder `MAP-PROOF-002` sind erst gültige Owner, wenn sie in einer zugelassenen lokalen normativen Quelle registriert wurden. Für `BUREAU-*` gilt stattdessen die externe Authority-Grenze oben: der Präfix ist nur die maschinenlesbare Referenzklasse und kein Beweis, dass der referenzierte Task tatsächlich existiert.

### Künftige Erweiterbarkeit

Weitere Registrierungsquellen benötigen zwingend:

- Eine kanonische Quelle
- Eine dokumentierte ID-Semantik
- Eine maschinenlesbare Oberfläche oder einen deterministischen Parser
- Einen vollständigen Drift-/Paritätsnachweis
- Eine klare Konfliktregel

### Enforcement-Grenze

Dieser Schritt entscheidet nur die Policy. Lifecycle-Enums, `review_after` als ISO-Datum, technische `owner_task`-Existenz und einfache `superseded_by`-Zielpfade werden technisch geprüft. Owner-Statuskompatibilität wird technisch geprüft. Neue Lifecycle-States sind nicht implementiert; globale Strict-Aktivierung ist implementiert.

### Gültige und ungültige Owner

Gültig:

```yaml
owner_task: DOCMETA-REPORT-LIFECYCLE-001
owner_task: OPT-API-002
```

Nur gültig, weil die IDs in einer normativen Quelle registriert sind.

Ungültig (Rolle/Platzhalter ohne Task-Verknüpfung):

```yaml
owner_task: TBD
owner_task: docs-mechanik
owner_task: MAP-PROOF-001
```

## Pflichtfelder nach Lifecycle-Zustand

| lifecycle_state | lifecycle | owner_task | review_after | superseded_by | Bemerkung |
| --- | --- | --- | --- | --- | --- |
| active | erforderlich | erforderlich | erforderlich | nein | Zweck, Verantwortung und Review-Zeitpunkt |
| deferred | erforderlich | erforderlich | erforderlich | nein | Prüfung oder Reaktivierung ausstehend |
| superseded | erforderlich | erforderlich | optional | erforderlich | Ablösung explizit nachvollziehbar |
| archived | erforderlich | erforderlich | nein | optional | Bei tatsächlicher Ablösung Zielpfad angeben; Legacy ohne Ersatz bleibt gesondert zu begründen |

Die Tabelle ist aktive Policy. Der Validator setzt davon derzeit nur einen
Teil technisch durch.

## Referenzklassen

Primary references kommen aus handgeschriebenen Artefakten, zum Beispiel:

- `docs/tasks/**`
- `docs/blueprints/**`
- `docs/reports/**`
- `docs/adr/**`
- `docs/specs/**`
- `docs/proofs/**`
- `docs/roadmap.md`

Derived references kommen aus `docs/_generated/**`.

Regeln:

- Primary references können Archivierung und Löschung blockieren.
- Derived references allein blockieren keine Archivierung.
- Vor physischer Archivierung oder Löschung muss ein Referenzcheck laufen.

Ob eine Primary Reference blockiert, hängt vom Status und Zweck des
referenzierenden Dokuments ab.

## Archivierungsregeln

Standard ist `status: deprecated` mit `lifecycle_state: archived`.

Lifecycle-State-only-Archivierung ist der derzeit unterstützte Weg. Die Datei
bleibt an ihrem bestehenden Pfad; dadurch bleiben Links und die aktuelle
Scannerreichweite stabil.

Physische Archivierung ist technisch vorbereitet: Validator, Inventory und
Overview erfassen Report-Dateien rekursiv. Eine Verschiebung bleibt trotzdem
reviewpflichtig, weil Links und Referenzflächen bewusst geprüft werden müssen.
und durch Tests für verschachtelte Archivpfade abgesichert sein.

Für eine spätere physische Archivierung gelten zusätzlich folgende
Voraussetzungen:

- keine aktiven Primary References brechen,
- `superseded_by` geprüft ist oder eine historische Begründung existiert,
- relevante Tasks, Proofs und Blueprints angepasst sind,
- generierte Dokumente reproduziert wurden,
- ein eigener PR erstellt wird.

Der aktuelle Rollout archiviert oder löscht keine Reports automatisch.

## Löschregeln

Direktes Löschen aus `status: draft`, `status: active` oder
`lifecycle_state: active` ist nicht erlaubt.

Löschen ist nur zulässig, wenn:

- `lifecycle_state` `archived` oder `superseded` ist,
- `superseded_by` existiert oder eine explizite Ausnahme begründet ist,
- keine aktive Primary Reference existiert,
- kein Audit-, Compliance- oder Rekonstruktionswert besteht,
- generierte Artefakte reproduzierbar bleiben,
- die Löschung in einem eigenen PR erfolgt.

## Rollout-Modell

1. Warnmodus in CI,
2. evidenzbasierte Resttriage,
3. kleine Backfill-Slices,
4. semantische Validatorhärtung,
5. global strict bei bereinigtem Bestand,
6. Archivierung nach Policy- und Referenzprüfung,
7. Löschung nur separat und nach vollständigem Referenzcheck.

Changed-only strict ist der Zwischenzustand: geänderte Report-Dateien werden
blockierend geprüft, ohne jeden Feature-PR durch unberührte Altlasten zu blockieren.

## Beispiele

### Active audit

```yaml
status: active
lifecycle: audit
owner_task: OPT-ARC-001
review_after: 2026-07-13
lifecycle_state: active
```

### Superseded proof

```yaml
status: deprecated
lifecycle: proof
owner_task: OPT-ARC-001
lifecycle_state: superseded
superseded_by: docs/reports/new-proof.md
```

### Archived legacy report

Archivierte Legacy-Dokumente dürfen ohne künstlichen `superseded_by`-Pfad
modelliert werden.
Wenn kein Ersatzartefakt existiert, muss dies bis zur Einführung einer
maschinenlesbaren Ausnahme fachlich nachvollziehbar im Report oder im
zugehörigen Task begründet werden.

## Offene Entscheidungen

- zulässige Werte für `lifecycle`,
- zulässige Werte für `lifecycle_state`,
- zusätzliche Regeln für `review_after`,
- Verhältnis von `deprecated` und `superseded`,
- physische Archivierung,
- eigene Lifecycle-Policy für `docs/proofs/**`,
- Review-Logik für generierte Reports,
- technische Owner-Auflösung,
- Enforcement-Zeitpunkt,
- spätere Statuskompatibilität.
