---
id: adr.ADR-0010__kubernetes-kanonische-plattform
title: ADR-0010 — Kubernetes als kanonische Zielplattform
doc_type: reference
status: active
summary: >
  Entscheidet Kubernetes als langfristige Zielplattform für commonThing, ohne den heutigen Compose-Betrieb oder den Single-Instance-Schutz vorzeitig abzulösen.
relations:
  - type: relates_to
    target: architecture/commonthing-os.md
  - type: relates_to
    target: docs/blueprints/weltgewebe-os-masterplan.md
  - type: relates_to
    target: docs/reports/domain-postgres-instance-coherence-decision.md
---

# ADR-0010 — Kubernetes als kanonische Zielplattform

Datum: 2026-07-15
Status: Accepted

## Kontext

commonThing soll langfristig hochverfügbar, regional betreibbar, föderierbar und durch Grabowski kontrolliert operierbar sein. Der heutige Produktions- und Entwicklungsbetrieb verwendet Docker Compose und Caddy. Diese Laufzeit ist real und bleibt so lange maßgeblich, bis ein neuer Pfad belegt und kontrolliert umgestellt wurde.

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

**Nachtrag vom 13.10.2026 — historischer Experiment-B-Stand:** Die oben als
„nächste Architekturentscheidung“ bezeichnete, temporäre Experiment-B-Probe
wurde unter Bureau T085 auf dem älteren Commit
`5d509feda6db24e294698cb42501fd41ec8e0fbe` terminal belegt und
die damalige Testumgebung laut Retirement-Receipt anschließend abgebaut.

**Verifizierbarer historischer T085-Abschluss (08.10.2026, nur lokaler
Operator-Belegbestand):**

- Terminal-Proof: `~/.local/state/commonthing/experiment-b-evidence/5d509fed-final-20261008/terminal-closeout-20261008/terminal-closeout.json`;
  SHA-256 `97392ceb9f911cdc8eea2cf21604bfacf487b0cb824387b11e7d5fcbb1c8aa5e`,
  `status=terminal_pass`, `source_commit=5d509feda6db24e294698cb42501fd41ec8e0fbe`.
- Sein gebundener **Pre-Teardown-Gate-Beleg** liegt unter
  `~/.local/state/commonthing/experiment-b-evidence/5d509fed-final-20261008/terminal-pass-pre-teardown-20261008/manifest.json`;
  SHA-256 `4aaef1af5edd223e08fafa6cc7da58a946a910c8abe17c033d895ec7e77302b8`,
  `experiment_b_gates=PASS`. Erst dieser Manifestbeleg (nicht allein
  `teardown`) bestätigt erfolgreiche historische Funktions-, T048-,
  Recovery- und Portabilitätsprüfungen für den genannten Testcommit.
- Kanonischer Retirement-Receipt:
  `~/.local/state/commonthing/experiment-b-retirement.json`;
  SHA-256 `e0e804a864439e58e99c503e41e5b62da561bd98b6ed6310b4125190bed0aab2`,
  `status=retired`. Der Terminal-Proof bindet dessen Hash sowie den
  Manifest-Hash und bestätigt entfernte VM, Volumes, Pool und Run-State.

Diese drei Datei-Hashes wurden gegen den real vorhandenen lokalen
Operator-Belegbestand erneut berechnet; die Belege sind **nicht im Git-Repository
veröffentlicht** und somit ohne Zugriff auf den ursprünglichen Operator-State
nicht unabhängig reproduzierbar. Historischer `terminal_pass` und
`retired` sind keine Freigabe für ein aktuelles T044- oder Produktions-Green.
Dies ist **kein** Nachweis, dass die danach vorgesehene Extraktion
portabler Invarianten oder der Abbau kind-/Host-spezifischer Mechanismen
erledigt wurde. Vor allem ersetzt T085 **nicht** die eigenständige neue,
revisionsgebundene T044-B5-Staging-Abnahme gegen ein von Blue, der lokalen
Referenz und der späteren produktiven Green-Zelle getrenntes Zieltestbed.
Dafür fehlen weiterhin negative Blue-/Writer-Isolation sowie Last-, Backup-,
Restore-, Recovery- und Suchkontinuitätsnachweise. Jede spätere produktive
Green-Zielruntime muss erneut eigenständig gegen B1–B6 geprüft werden.
Maßgebliche aktuelle Sperren und Beweisgrenzen:
[T044 R5-Vorabnahme](../deploy/kubernetes-production-cutover-t044.md).
Dies aktualisiert nur den Status der ehemaligen Experiment-B-Planung;
die ursprüngliche Architekturentscheidung und die Produktionssperre
bleiben unverändert.

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
