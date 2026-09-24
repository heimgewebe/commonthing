---
id: runbook.kubernetes-ha-recovery-proof
title: Kubernetes-HA- und Recovery-Beweis
doc_type: runbook
status: active
summary: >
  Führt den eigentumsgebundenen T004-Beweis für Zonenverteilung, API-Upgrade/Rollback, PostgreSQL- und JetStream-Failover sowie PITR in einen leeren kind-Cluster aus.
relations:
  - type: relates_to
    target: docs/adr/ADR-0013__ha-referenzzelle-und-wiederherstellung.md
---

# Kubernetes-HA- und Recovery-Beweis

## Voraussetzungen

- sauberer, commitgebundener Worktree,
- Docker mit Zugriff auf den lokalen Daemon,
- ausreichend Speicher für zwei kurzzeitig parallele Vier-Knoten-kind-Cluster,
- keine vorhandenen Cluster, Container oder Volumes mit demselben Beweisnamen.

## Ausführung

```bash
python scripts/platform/ha_reference.py proof --cluster weltgewebe-ha-reference
```

Der Runner installiert Werkzeuge ausschließlich aus `platform/toolchain.lock.json`, baut die commonThing-Images aus dem aktuellen Commit und erzeugt alle kurzlebigen Secrets erst zur Laufzeit.

## Beweisphasen

1. Primärcluster mit drei Datenzonen erzeugen.
2. Cilium, Gateway API, Flux, cert-manager, drei auf unterschiedliche Knoten verteilte CloudNativePG-Operatorreplikate und das Barman-Cloud-Plugin installieren.
3. Das Plugin- und Instanz-Sidecar-Image digestgebunden zurücklesen, den pluginbasierten `ObjectStore` anwenden und PostgreSQL, NATS JetStream sowie drei API-Replikate über die Zonen verteilen.
4. Eine gültige `domain_nodes`-Mutation schreiben und über die API zurücklesen.
5. Ein getrenntes unveränderliches API-Artefakt aus denselben Runtime-Bits laden, alle drei Replikate darauf ausrollen, jeden beworbenen Gateway-Listener sekündlich aus dem Netzraum seines zugehörigen Kind-Node-Owners prüfen und per `rollout undo` auf die vorherige Revision zurückkehren.
6. Upgrade-, Rollback- und Fehlerbudgetwerte festhalten. Ein globaler Gateway-Ausfall der Referenzzelle liegt nur vor, wenn in einer Stichprobe kein owner-lokal geprüfter Gateway-Listener die bestätigte API-Mutation liefert; einzelne ausgefallene Listener werden getrennt als Pfaddegradation ausgewiesen. Cross-Node-Kind-/Cilium-Pfadstörungen sind ein separater Infrastrukturbefund. Externe Host- oder Load-Balancer-Erreichbarkeit außerhalb der Kind-Bridge wird damit ausdrücklich nicht bewiesen.
7. Den Worker des PostgreSQL-Primary stoppen.
8. PostgreSQL-Failover, API-Readback und JetStream-Quorum messen.
9. Worker wieder aufnehmen und vollständige Bereitschaft abwarten.
10. Basisbackup erstellen, PITR-Zeitpunkt festhalten und eine spätere Mutation schreiben.
11. Das danach erzwungene WAL-Segment exakt bestimmen und seine Archivierung samt Latenz abwarten.
12. Einen zweiten leeren kind-Cluster erzeugen.
13. Datenbank auf den Zielzeitpunkt wiederherstellen und den `Ready`-RTO erfassen.
14. Im Restorecluster dieselbe Barman-Sidecar-Bindung sowie fortgesetzte WAL-Archivierung prüfen und danach die Zeilengrenze vergleichen.

## Ergebnisse

Bei Erfolg entsteht unter `.cache/weltgewebe-platform/receipts/` ein JSON-Receipt mit:

- Commit und Tool-Lock-Hash,
- Image-IDs einschließlich der in Haupt- und Restorecluster injizierten Barman-Sidecars,
- Pod-, Knoten- und Zonenbelegung einschließlich der verteilten CloudNativePG-Operatorreplikate,
- gemessenen Failover- und Restore-RTO-Werten,
- Upgrade- und Rollbackdauer, vollständig ersetzten API-Pod-UIDs, Gateway-Verfügbarkeitsstichproben und Referenz-Fehlerbudget,
- Backup- und PITR-Zeitpunkt,
- erforderlichen und über PostgreSQLs `pg_stat_archiver` tatsächlich bestätigten WAL-Segmenten sowie Archivlatenzen in Primär- und Restorecluster,
- archivierungsgebundener RPO-Referenzobergrenze ohne verlorene bestätigte Fach- oder JetStream-Mutation,
- getrenntem Restore-Ready-RTO und nachgelagerter Kontinuitätsvalidierungsdauer,
- Datenvergleich des Restoreclusters,
- expliziten Nichtaussagen.

## Fehlerdiagnose

Bei einem Fehler werden Clusterzustand, Pods, Events, Flux, Gateway, CloudNativePG-Status, Barman-Plugin- und Pod-Logs, Zertifikats- sowie Storagezustand unter `.cache/weltgewebe-platform/failures/<cluster>/` gesichert. Einzelne Diagnosekommandos sind zeitlich begrenzt; Timeouts und Startfehler werden als Diagnoseevidenz vermerkt. Der Runner startet einen gestoppten Knoten wieder und entfernt standardmäßig nur seine eigenen markierten Ressourcen.

## Manuelles eigentumsgebundenes Cleanup

Nur für einen abgebrochenen Lauf mit bekanntem Commit und der im Proof-Receipt gebundenen Owner-ID:

```bash
python scripts/platform/ha_reference.py down \
  --cluster weltgewebe-ha-reference \
  --commit <exakter-commit> \
  --owner-id <exakte-owner-id>
```

Ohne exakt passende Eigentumsbindung für Repository, Cluster, Commit und Owner-ID verweigert der Befehl das Löschen. Das gilt sowohl für die kind-Cluster als auch für Container und Volume des externen Proof-Object-Stores. Das JSON-Ergebnis weist jede Ressource als `deleted`, `absent` oder `error` aus und meldet bei gemischtem Ergebnis `partial`. Verwaiste oder unlesbare Marker werden nicht automatisch entfernt.
