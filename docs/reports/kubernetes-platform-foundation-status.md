---
id: docs.reports.kubernetes-platform-foundation-status
title: Kubernetes- und GitOps-Grundlage — Status und Beweisgrenzen
summary: Trennt deklarative Plattform-, kind/HA- und Staging-E2E-Evidenz commitgebunden und macht den Proof-Lag des Legacy-Staging-Controllers sichtbar.
doc_type: status
status: active
owner_task: WELTGEWEBE-OS-006
review_after: 2026-10-19
relations:
  - type: depends_on
    target: docs/adr/ADR-0010__kubernetes-kanonische-plattform.md
  - type: relates_to
    target: platform/README.md
  - type: relates_to
    target: docs/tasks/board.md
  - type: relates_to
    target: docs/reports/domain-postgres-instance-coherence-decision.md
  - type: verifies
    target: scripts/platform/validate_platform.py
  - type: verifies
    target: scripts/platform/kind_reference.py
  - type: verifies
    target: scripts/platform/ha_reference.py
---

# Kubernetes- und GitOps-Grundlage — Status und Beweisgrenzen

Dieser Bericht verwendet keinen einzelnen Datumsstempel als Wahrheitsanker. Die Plattform hat mehrere Beweisebenen, die zu unterschiedlichen Commits gehören und deshalb getrennt gelesen werden müssen.

## Evidenzschichten

| Wahrheitsschicht | commitgebundene Evidenz | Aussage |
| --- | --- | --- |
| Deklarative Plattform / aktueller Governance-Schnitt | Controller- und Proof-Identity-Inhalt: **1bc0b729304461e65b4f0adf0b054a5d9dc6fc88**; lokaler Render-/Contract-Pfad auf exakt diesem Inhalt grün. Veröffentlichung und GitHub-PR-CI stehen für diesen Schnitt noch aus. | Manifeste, Renderer, Proof-Identity- und Contraction-Vertrag sind für diesen Inhalt konsistent. Das ist kein Runtime-Beweis. |
| CI-kind / GitOps | Main **9a8a8ed49211219b8b4e18345b7529c6cf666b2d**, Workflow-Run **35455115045**, Job **kind-gitops-proof** erfolgreich und tatsächlich ausgeführt. | Commitgebundene kind-/Flux-/GitOps-Referenz einschließlich kontrollierter OCI-Eingaben. |
| CI-kind / HA-Recovery | Main **9a8a8ed49211219b8b4e18345b7529c6cf666b2d**, Workflow-Run **35455115045**, Job **kind-ha-recovery-proof** erfolgreich und tatsächlich ausgeführt. | Single-Host-kind-Failover und Blank-Cluster-Recovery für diesen Commit; keine unabhängigen physischen Fehlerdomänen. |
| Staging-Cell E2E, letzter vollständiger Zyklus | Implementierungscommit **bb1e26d47b50d38ec720b123d55255c05436100b**; private Runtime-Receipts unter anderem cell-bootstrap, cell-rebuild, gateway-proof, cell-down und delete-to-prove; terminales delete-to-prove-Receipt SHA-256 **625f5cc1471013be4b960afe4fb7a0ca5b6466add904de4a1827aafb09a9314b**. | Ein realer Delete-to-Prove wurde ausgeführt. Dieser Beweis gilt nicht automatisch für spätere Controlleränderungen. |
| Neuerer Backup-/Recovery-Zyklus | backup-delete-to-prove-down-Receipt SHA-256 **fa33fee67768b63d524f70c0cbf4371829bb9b448cda6eef12eec7d6acdb980d**, Controller **c7eb255c7aaaa8ac384ed7cd8e063c10ca1be52a**. | Der Zyklus wurde begonnen und bis Backup + Clusterdelete + leere Restore-Roots belegt; ein terminales Backup-Rebuild-/Backup-Delete-to-Prove-Receipt lag beim letzten Readback noch nicht vor. Er ersetzt deshalb den älteren vollständigen E2E-Beweis nicht. |

## Proof-Lag des Legacy-Staging-Controllers

Der letzte vollständige Staging-E2E-Proof ist älter als der aktuelle Controller-Inhalt.

- letzter vollständiger E2E-Implementierungscommit: **bb1e26d47b50d38ec720b123d55255c05436100b**
- aktueller Controller-Inhaltscommit dieses Governance-Schnitts: **1bc0b729304461e65b4f0adf0b054a5d9dc6fc88**
- proof_lag_commits: **66**
- proof_lag_changed_lines_added: **4744**
- proof_lag_changed_lines_removed: **1851**
- proof_lag_changed_lines_total: **6595**
- Contraction-Ratchet für scripts/platform/staging_cell.py: **9201 Zeilen**, also unter der historischen harten Obergrenze von 9708.

Die Zeilendistanz ist Churn, nicht Nettowachstum: sie zählt Hinzufügungen und Löschungen. Der neue Ratchet-Vertrag verhindert künftig erneutes Wachstum über die jeweils kleinere Basis.

## Neue Evidence-Bindung

**scripts/platform/proof_identity.py** trennt nun drei Proofklassen: kind-gitops, ha-recovery und staging-cell.

Für kind/HA wird die Cache-Identity nur noch aus tatsächlich gebundenen Eingaben gebildet. Der Quellcommit bleibt im Proof-Record sichtbar, ist aber nicht mehr selbst Teil des Cache-Fingerabdrucks. Dadurch invalidiert eine Änderung ausschließlich an **staging_cell.py** die teuren kind-/HA-Caches nicht mehr.

Für staging-cell gilt die Zweistufigkeit:

1. **H** ist der tatsächlich öffentliche Post-Merge-Main-Commit mit Controller- und Governancecode.
2. Proof(H) muss gegen exakt H laufen und die terminale Staging-Receipt-Kette hashen; production_changed muss false bleiben.
3. Großer Adler prüft H und Proof(H) read-only und erzeugt ein Finding in seinem eigenen append-only Store.
4. **H+1** ist ein direkter Evidence-only-Kindcommit von H und darf exakt vier Repo-Dateien unter **docs/proofs/kubernetes-staging-cell/** hinzufügen: identity.json, record.json, proof.json und attestation.json.
5. PR-CI prüft H+1 maschinell: direkter Parent H, keine Änderung an Proof-Inputs, exakte Evidence-Dateimenge, Record-/Proof-Hashes und Adler-Checkpoint H.
6. finding_sha256 ist nur ein Integritätswert. Vor dem Merge von H+1 muss Grabowski zusätzlich live im unabhängigen Adler-State prüfen, dass finding_id, finding_sha256 und checkpoint wirklich existieren und exakt zusammengehören.

Damit muss ein Beweis sich nicht selbst enthalten.

## Contraction Mode

**scripts/platform/staging_cell.py** ist ein Legacy Experimental Controller. Die abgeschlossene mutierende Legacy-State-Migration wurde aus dem Controller entfernt; die read-only Validierung des historischen Migrationsreceipts bleibt erhalten.

Mechanisch gilt:

- LOC darf nur gleich bleiben oder sinken; historische harte Obergrenze 9708, aktuelle Basis 9201.
- CLI-Kommandos dürfen nur gleich bleiben oder weniger werden.
- staging-spezifische Receipt-Arten dürfen nur gleich bleiben oder weniger werden.
- neue staging-spezifische Python-Module oder lokale Modulabhängigkeiten dürfen die Sperrklinke nicht umgehen.
- Regressionstests dürfen wachsen.
- zulässig bleiben konkrete Fehlerkorrekturen, Abschluss bereits begonnener Recovery-/Proof-Zyklen, Reduktion, Vereinfachung und portable Extraktion.

## Belegter Plattformumfang

Belegt bleiben insbesondere:

- gemeinsame Kustomize-Basis und deklarative Overlays;
- digestgebundene Images, SHA-verifizierte Tool-/Artefaktlocks und kontrollierte OCI-Eingaben;
- restricted Pod Security, Default-Deny und explizite Datenpfade;
- externer Secretvertrag ohne versionierte Secretwerte;
- Gateway API sowie Flux-Abhängigkeits- und Driftkorrekturverträge;
- direkte und GitOps-basierte Blank-Cluster-Rekonstruktion;
- Single-Host-kind-HA mit PostgreSQL-, Barman-/WAL- und JetStream-Recovery;
- der historische reale Staging-Delete-to-Prove-Zyklus.

## Weiterhin nicht behauptet

- Kubernetes ist nicht die laufende Weltgewebe-Produktion.
- Ein kind-Proof ist kein Beweis für mehrere physische Hosts, reale Providerfehlerdomänen, externes DNS/TLS oder einen produktiven Load Balancer.
- Der begonnene neuere Backup-Zyklus ist kein terminaler Staging-E2E-Beweis.
- Der Compose-Produktionspfad bleibt die aktuelle Laufzeit, bis ein getrennter Produktionscutover belegt ist.
- Ein permanentes Kubernetes-Staging ist nicht beschlossen.
- Die Existenz des Legacy-Controllers ist kein Auftrag, daraus einen zukünftigen GewebeZelle-Operator auszubauen.

## Nächster Architekturtest

**Experiment B** ist ein separater Folgetask: temporäres reales Kubernetes-Zieltestbed, möglichst ohne **staging_cell.py**, mit Wiederverwendung der vorhandenen Kustomize-/Flux- und portablen Proofteile. Danach werden tatsächlich benötigte portable Invarianten extrahiert und unnötige kind-/Host-spezifische Mechanismen weiter abgebaut.

Ein permanentes Staging wird erst dann neu bewertet, wenn reale Produktionsrisiken seinen dauerhaften Betrieb rechtfertigen, etwa irreversible Migrationen, mehrere Produktionsinstanzen, Multi-Host-HA, Fremdbetreiber oder föderierte Zellen, verbindliche SLOs oder regelmäßige Provider-/DNS-/TLS-/Load-Balancer-Cutover.
