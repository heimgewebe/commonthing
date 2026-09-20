---
id: adr.ADR-0010__kubernetes-kanonische-plattform
title: ADR-0010 — Kubernetes als kanonische Zielplattform
doc_type: reference
status: active
summary: >
  Entscheidet Kubernetes als langfristige Zielplattform für Weltgewebe, ohne den heutigen Compose-Betrieb oder den Single-Instance-Schutz vorzeitig abzulösen.
relations:
  - type: relates_to
    target: architecture/weltgewebe-os.md
  - type: relates_to
    target: docs/blueprints/weltgewebe-os-masterplan.md
  - type: relates_to
    target: docs/reports/domain-postgres-instance-coherence-decision.md
---

# ADR-0010 — Kubernetes als kanonische Zielplattform

Datum: 2026-07-15
Status: Accepted

## Kontext

Weltgewebe soll langfristig hochverfügbar, regional betreibbar, föderierbar und durch Grabowski kontrolliert operierbar sein. Der heutige Produktions- und Entwicklungsbetrieb verwendet Docker Compose und Caddy. Diese Laufzeit ist real und bleibt so lange maßgeblich, bis ein neuer Pfad belegt und kontrolliert umgestellt wurde.

Eine spätere ungeplante Migration würde invasive Umbauten an Zuständen, Probes, Deployments, Secrets, Observability, Datenbanken und Rolloutmechanismen erzwingen. Umgekehrt würde ein sofortiger Lift-and-Shift nur heutige Grenzen in einen komplexeren Orchestrator übertragen.

## Entscheidung

Kubernetes ist die kanonische Zielplattform für Staging und Produktion.

Die Entscheidung bedeutet ab sofort:

- neue Dienste müssen stateless oder mit explizitem gemeinsamem Zustandsvertrag entworfen werden,
- Images müssen unveränderlich und versionsgebunden sein,
- Readiness, Liveness, Shutdown und Draining sind Teil des Dienstvertrags,
- Plattformzustand wird deklarativ und GitOps-fähig beschrieben,
- Netz-, Secret-, Policy-, Backup- und Telemetrieverträge werden Kubernetes-kompatibel gehalten,
- eine lokale Kubernetes-Referenzumgebung wird vor der Produktionsmigration aufgebaut,
- Produktion wird erst nach Multi-Instanz-, Restore-, Failover- und Rolloutbeweisen migriert.

## Gegenwartsgrenze

Diese ADR ändert nicht automatisch die heutige Laufzeit.

- Compose bleibt aktueller realer Betriebsweg.
- Der Multi-Instance-Guard bleibt aktiv; Produktionsreplikation folgt erst mit dem Plattformrollout.
- Kein Produktionscluster wird allein aufgrund dieser ADR installiert.
- Keine bestehende Deploymentlane wird ungeprüft übernommen oder umgestellt.
- „Kanonische Zielplattform“ verlangt Kubernetes-Kompatibilität und einen belegten späteren Cutoverpfad, aber nicht automatisch eine dauerhaft laufende Staging-Stufe.

## Umgebungsspezifischer Lifecycle-Code

Neuer umgebungsspezifischer Lifecycle-Code benötigt eine reale Zielumgebung und einen automatisierbaren oder maschinenprüfbar attestierten Proof-Pfad. Eine lokale Referenz darf portable Invarianten entwickeln und testen; sie rechtfertigt aber nicht durch sich selbst immer neue Controller-Sonderlogik.

**scripts/platform/staging_cell.py** ist eine bestehende **Legacy Experimental Controller**-Ausnahme und steht im **Contraction Mode**. Dieser Status ist kein Grandfathering für weiteres Wachstum:

- keine neue Lifecycle-Funktionalität, keine neuen Subkommandos und keine neuen staging-spezifischen persistenten Zustands- oder Receipt-Arten;
- zulässig sind Abschluss bereits begonnener Recovery-/Proof-Zyklen, konkrete Fehlerkorrekturen, Reduktion, Vereinfachung und die Extraktion nachweislich allgemein benötigter portabler Invarianten;
- die mechanische Contraction-Ratchet darf nur gleich bleiben oder enger werden; Tests dürfen wachsen;
- neue staging-spezifische Hilfsmodule dürfen die Ratchet nicht durch bloße Codeverlagerung umgehen.

Die nächste Architekturentscheidung über reale Kubernetes-Betriebsmechanismen fällt durch **Experiment B**: ein zeitlich begrenztes reales Kubernetes-Zieltestbed, möglichst ohne **staging_cell.py**, das vorhandene Kustomize-/Flux- und portable Proofteile wiederverwendet. Ziel ist festzustellen, welche Invarianten auf einer echten Zielplattform tatsächlich nötig sind; unnötige kind-/Host-spezifische Mechanismen werden danach abgebaut.

Eine permanente Staging-Umgebung ist damit ausdrücklich nicht beschlossen. Ihr Nutzen muss aus realen Produktionsrisiken folgen, etwa irreversiblen Datenmigrationen, mehreren Produktionsinstanzen, Multi-Host-HA, Fremdbetreibern oder föderierten Zellen, verbindlichen SLOs oder wiederkehrenden DNS-/TLS-/Load-Balancer-/Provider-Cutovern.

## Kanonische Plattformstruktur

```text
platform/
  apps/
    weltgewebe/
      base/
      overlays/
        local/
        ci/
        staging/
        production/
  clusters/
  infrastructure/
    networking/
    data/
    messaging/
    observability/
    security/
  policies/
  recovery/
```

Kustomize-Basen und kleine Overlays sind für eigene Anwendungen bevorzugt. Helm darf für klar abgegrenzte Drittkomponenten verwendet werden.

## Geplanter Plattformkern

- Flux für GitOps-Reconciliation,
- Gateway API für Eingangs- und Trafficverträge,
- Cilium und Hubble für Netzwerk, Policies und Sichtbarkeit,
- OpenTelemetry für Logs, Metriken und Traces,
- Policy as Code für Plattforminvarianten,
- External-Secrets-artige Integration ohne Secrets im Repository,
- CloudNativePG als frühe portable HA-Referenz in Nichtproduktion,
- NATS JetStream als Ereignisrückgrat,
- KEDA für nachgewiesene ereignisbasierte Worker,
- Kueue und Dynamic Resource Allocation erst bei realen Agenten-, Batch- oder Beschleunigeranforderungen.

Die Auswahl einzelner Implementierungen bleibt durch Belege revidierbar. Der Architekturvertrag — deklarativ, portabel, beobachtbar und wiederherstellbar — ist verbindlich.

## Compose-Vertrag

Compose bleibt als Entwicklungs-, Kleinprofil- und Recoverypfad zulässig, wenn es:

- aus derselben kanonischen Konfiguration abgeleitet wird oder
- durch Tests gegen dieselben Invarianten geprüft wird.

Eine manuell unabhängig gepflegte zweite Plattformwahrheit ist nicht zulässig.

## Alternativen

### Compose dauerhaft als Primärplattform

Verworfen, weil regionale Hochverfügbarkeit, deklarative Multi-Cluster-Verwaltung, isolierte Agentenworkloads und standardisierte Policy-/Observability-Verträge später einen invasiven Plattformwechsel erzwingen würden.

### Nomad als Primärorchestrierung

Nicht weiter Zielrichtung. Bestehende historische Hinweise dürfen nicht als aktuelle Architekturentscheidung gelesen werden.

### Sofortiger Produktions-Lift-and-Shift

Verworfen. Die aktuelle API ist noch nicht multiinstanzfähig; ein Lift-and-Shift würde den Single-Instance-Zustand nur verpacken.

## Konsequenzen

- Plattformkompatibilität wird Teil neuer Architektur- und Code-Reviews.
- Multi-Instanz-Korrektheit hat Vorrang vor Replica-Skalierung.
- lokale Cluster und CI-Proofs entstehen vor Produktionsmigration.
- Produktionscutover benötigt gemessene SLOs, RTO, RPO, Restore und Rollback.
- ein späterer GewebeZelle-Operator wird erst nach stabilen realen Zellprofilen gebaut.

## Akzeptanz für die spätere Produktionsumstellung

- zwei oder mehr API-Instanzen sind fachlich kohärent,
- ein leerer Cluster ist aus versionierten Artefakten rekonstruierbar,
- Datenbank- und Messagingausfälle wurden geprobt,
- Backup und Point-in-Time-Recovery wurden ausgeführt,
- RTO und RPO sind gemessen,
- Canary, Abbruch und Rollback sind belegt,
- Produktion besitzt keine unregistrierte manuelle Konfigurationswahrheit.
