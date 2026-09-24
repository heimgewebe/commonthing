---
id: docs.reports.weltgewebe-os-foundation-status
title: commonThing OS Foundation — Status und Beweisgrenzen
doc_type: status
status: active
summary: >
  Trennt den belegten aktuellen commonThing-OS- und Referenzplattformstand von der weiterhin nicht erfolgten Kubernetes-Produktionsaktivierung.
owner_task: WELTGEWEBE-OS-001
review_after: 2026-08-15
relations:
  - type: depends_on
    target: architecture/commonthing-os.md
  - type: relates_to
    target: docs/blueprints/weltgewebe-os-masterplan.md
  - type: relates_to
    target: docs/reports/domain-postgres-instance-coherence-decision.md
  - type: relates_to
    target: docs/reports/kubernetes-platform-foundation-status.md
  - type: relates_to
    target: docs/proofs/weltgewebe-os-v1-t032-federation-delivery.md
  - type: relates_to
    target: docs/tasks/board.md
---

# commonThing OS Foundation — Status und Beweisgrenzen

## Zweck und Gegenwartsgrenze

Dieser Bericht verhindert, dass langfristige Architektur, Referenzbeweise und laufende Produktion vermischt werden. Stand 29. Juli 2026 ist Docker Compose weiterhin der reale Produktions- und Recoverypfad. Kubernetes ist die kanonische Zielplattform und eine reproduzierbar geprüfte Referenz- und Abnahmeplattform, aber noch nicht die laufende Produktion.

## Belegt

- commonThing besitzt eine SvelteKit-Webanwendung und eine Rust/Axum-API.
- PostgreSQL trägt die wesentlichen Domain-, Auth- und Projektionspfade.
- Multi-Instance-Zwischenzustände, Transactional Outbox, idempotente Konsumenten und Zwei-API-Kohärenz sind revisionsgebunden belegt.
- Die Kubernetes-/GitOps-Grundlage ist aus versionierten Artefakten direkt und über Flux reproduzierbar.
- Gateway API, Cilium/Hubble, Network Policies, restricted Pod Security, Secret- und Imagepromotionsverträge sind vorhanden.
- Eine logisch zonierte kind-Referenzzelle auf einem einzelnen CI-Host hat PostgreSQL-/JetStream-Failover, Barman-Backup, WAL-Archivierung, PITR und Blank-Cluster-Restore bestanden.
- RTO, archivierungsgebundene RPO-Obergrenze, Upgrade, Rollback und Referenzfehlerbudget wurden gemessen.
- Proofkritische OCI-Eingaben werden aus einem kontrollierten privaten Digest-Mirror geladen und anschließend offline verifiziert.
- Der Föderationskern mit Signaturen, Inbox/Outbox, Quarantäne und unabhängigen Zellgrenzen ist implementiert. Die Outbox besitzt einen dauerhaften, begrenzten und multi-instanzsicheren Delivery-Worker; Aktivierung realer Betreiberzellen bleibt davon getrennt.
- Ein manuelles GewebeZelle-Pilotprofil bindet Zellidentität, externe Secrets, Peers, Delivery, Backup, Restore und Betreiberpflichten, ohne Self-Service oder Operator zu behaupten.
- Task-Control, Agent-Safety und beweisgebundene Dokumentationsmechanismen sind vorhanden.

## Status der Grundlagenwelle

| Bestandteil | Status | Beleggrenze |
|---|---|---|
| commonThing-OS-Verfassung | belegt | Architektur und ADRs sind kanonisch; spätere Aktivierung bleibt gategebunden |
| Multi-Instance- und Ereignisgrundlage | belegt | gemeinsamer Zustand, Outbox und Zwei-API-Kohärenz; Produktionskapazität separat |
| Kubernetes-/GitOps-Referenzpfad | belegt | Kustomize, Flux, Gateway, Policies, Direct-/GitOps-Proof; kein Produktionscluster |
| HA- und Recovery-Referenzzelle | teilweise | logische Zonen in einem Single-Host-kind-Setup belegen Failover, PITR, Blank-Cluster-Restore und Messwerte; ein physisch verteilter Fehlerdomänenbeweis fehlt |
| Föderationskern und automatische Auslieferung | belegt | signierter Protokollkern, dauerhafte Outbox-Delivery und logischer Zwei-Zellen-Beweis; kein realer Betreiber-Dauerbetrieb |
| Manuelles GewebeZelle-Pilotprofil | belegt | GitOps-, Secret-, Peer-, Backup- und Betreibervertrag; kein Self-Service und kein Operator |
| Observability- und Operator-Spine | teilweise | Telemetrieverträge und Proof-Receipts vorhanden; vollständige SLO-/Chronik-/Leitstandprojektion offen |
| Kubernetes-Staging | offen | echter Cluster, externe Secrets, Imagepromotion, Storage, TLS/DNS/LB und produktionsnahe Lastbelege fehlen |
| Kubernetes-Produktion | offen | `WELTGEWEBE-OS-V1-T044` bleibt der einzige autoritative Cutoverpfad |

## Erfüllte Beweisgates

### Gate A — Verfassung und Registrierung

Architektur, ADRs, Masterplan und Bureau-Initiative sind vorhanden und unterscheiden Zielbild, Referenzbeweise und Produktionsrealität.

### Gate B/C — Multi-Instanz, Outbox und Kohärenz

Gemeinsame Auth-/Domainzustände, atomare Outbox, fortsetzbarer Relaypfad, Deduplikation und Zwei-API-/Restartbeweise sind umgesetzt.

### Gate D — Kubernetes-Reproduzierbarkeit

Ein leerer Cluster wird aus versionierten Artefakten aufgebaut; CI und Umgebungsziele verwenden dieselbe Anwendungsbasis; Probes, Shutdown, Secrets, Policies, Ressourcen und Telemetrieverträge werden geprüft.

### Gate E — HA und Recovery

Mehrere logisch getrennte Fehlerdomänen, PostgreSQL-/JetStream-Failover, PITR, Blank-Cluster-Restore sowie RTO/RPO-, Upgrade- und Rollbackmessungen sind in einer Single-Host-kind-Referenz belegt. Das erfüllt nicht das unveränderte Akzeptanzkriterium eines über unabhängige physische Hosts verteilten HA-Belegs.

### Gate F — Föderationsreferenz

Zellidentität, signierte Ereignisse, Reichweiten, Quarantäne und unabhängige Zellgrenzen sind implementiert. Öffentliche Aktivierung und Produktionsbetrieb bleiben eigene Entscheidungen.

## Weiterhin ausgeschlossene Aussagen

Dieser Bericht etabliert nicht:

- Produktionsfreigabe oder laufenden Betrieb auf Kubernetes;
- einen ausgewählten endgültigen Kubernetes-Anbieter;
- einen über mehrere physische Hosts oder vergleichbar unabhängige Fehlerdomänen verteilten HA-Beweis;
- reale Multi-Region- oder Multi-Cloud-HA;
- RTO/RPO unter repräsentativer Produktionslast;
- die sichere gleichzeitige Zerstörung zweier Fehlerdomänen;
- einen produktiven externen Secret-, Load-Balancer- oder Object-Store-Vertrag;
- Berechtigung, Compose, DNS oder produktive Datenpfade ohne den T044-Cutoververtrag zu verändern.

## Nächste wirksame Schnitte

1. Kanonische Performance- und Kapazitätswahrheit samt realen Messgates abschließen.
2. Observability-, Chronik-, Leitstand- und Operatorprojektionen für SLO, RTO/RPO und Deploymentidentität vervollständigen.
3. Einen kleinen realen Staging-Cluster mit externen Secrets, Imagepromotion und produktionsnaher Recovery betreiben.
4. Danach den gestuften Produktionscutover ausschließlich über `WELTGEWEBE-OS-V1-T044` vorbereiten und ausführen.
