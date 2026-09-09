---
id: platform.readme
title: Weltgewebe Kubernetes- und GitOps-Plattform
summary: Kanonischer Plattformvertrag für Kustomize, Flux, Gateway API, Cilium und den isolierten kind-Referenzbeweis.
role: norm
organ: ops
status: canonical
canonicality: normative
lifecycle_state: active
owner: ops
review_after: 2026-09-30
last_reviewed: 2026-08-30
depends_on: []
relations:
  - type: relates_to
    target: architecture/weltgewebe-os.md
  - type: relates_to
    target: docs/reports/kubernetes-platform-foundation-status.md
  - type: relates_to
    target: docs/runbooks/gewebezelle-manual-pilot.md
  - type: verifies
    target: scripts/platform/validate_platform.py
  - type: verifies
    target: scripts/platform/kind_reference.py
verifies_with:
  - scripts/platform/validate_platform.py
  - scripts/platform/kind_reference.py
---

# Weltgewebe Kubernetes- und GitOps-Plattform

`platform/` ist die kanonische deklarative Zielplattform für Weltgewebe. Docker Compose bleibt die gegenwärtige Produktions- und Recovery-Laufzeit, bis ein eigener Produktionsfreigabevertrag abgeschlossen ist.

## Wahrheitsschichten

- `apps/weltgewebe/base/` enthält den gemeinsamen Anwendungsvertrag.
- `apps/weltgewebe/overlays/` enthält ausschließlich kleine Umgebungsdeltas.
- `infrastructure/local-data/` stellt PostgreSQL und JetStream nur für lokale und CI-Beweise bereit.
- `infrastructure/gateway/` definiert Gateway API und HTTPRoute.
- `clusters/local/` definiert die Flux-Abhängigkeitskette `data → migration → app → gateway`.
- `toolchain.lock.json` bindet Werkzeuge, Clusterimage und Drittartefakte an SHA-256.
- `oci-proof-mirror.seed.json` und `oci-proof-mirror.lock.json` binden den privaten
  Proof-OCI-Mirror: Seed-Inventar, generierter Lock, Quellcommit (`generation.source_head`),
  Seed-SHA-256 und Publisher-Evidenz. Erlaubte Abstammung ist ausschließlich ein in
  diesem Clone erreichbarer Commit, der Vorfahre von `HEAD` ist und dessen Seed-Blob
  dem gelockten `seed_sha256` entspricht; veraltete, fremde, manipulierte oder nicht
  erreichbare Quellcommits scheitern vor der Inventar-Vollvalidierung
  (`scripts/platform/oci_proof_mirror.py`). Lock-Updates müssen `source_head` und
  `seed_sha256` gemeinsam mit der Publisher-Evidenz neu binden.
- `cell-profile.contract.json` definiert das erste manuelle, nicht selbstbedienbare GewebeZelle-Pilotprofil.
- `cell-pilot/two-operator-pilot.contract.json` definiert den fail-closed strukturellen Vorprüfvertrag für genau zwei unabhängige Betreiber; die `.invalid`-Vorlage bleibt nicht aktivierbar.
- `apps/weltgewebe/cell-pilot/federation-delivery-egress.yaml` ist ein nicht eingebundenes, fail-closed Cilium-FQDN-Template für exakt benannte ausgehende Peerziele.

## Sicherheitsgrenzen

- Keine Secret-Objekte oder Secretwerte werden versioniert.
- Local und CI verwenden für Datenzelle und lokale App eine deterministische, ausdrücklich öffentliche Test-Fixture als ConfigMap; sie ist kein Produktionsgeheimnis.
- Der Referenzrunner verwendet für Local/CI einen deklarativen Migration-only-Job mit öffentlicher ConfigMap-Fixture. Staging und Produktion bleiben durch den externen Secret- und Image-Promotionsvertrag blockiert.
- Staging und Production benötigen einen externen, auditierten Secretpfad.
- Eigene Container laufen ohne Root, ohne Service-Account-Token, ohne Privilege Escalation und mit Default-Deny-Netzpolitik.
- Der Referenzrunner übernimmt oder löscht niemals einen bereits vorhandenen Cluster.
- Proof-Cluster werden lokal unter einem pro Cluster serialisierten Ownership-Lock reserviert; Cleanup verlangt den exakten Commit und dieselbe Owner-ID. Verwaiste Marker werden fail-closed nicht automatisch entfernt.
- Werkzeugarchive werden vor jeder Schreibwirkung vollständig geprüft; nur reguläre Dateien und Verzeichnisse sind zulässig. Symlinks, Hardlinks, Devices, FIFOs, Traversal und widersprüchliche Member werden fail-closed abgewiesen; die ausführbare Datei wird anschließend atomisch installiert.
- Produktionsdeployments, DNS, Compose und reale Replikazahlen werden durch diesen Vertrag nicht verändert.

## Persistente Staging-Zelle

`scripts/platform/staging_cell.py` verwaltet genau eine owner- und commitgebundene
Staging-Zelle namens `weltgewebe-staging`. Der öffentliche CLI-Vertrag bietet
bewusst keinen frei wählbaren Cluster- oder State-Root: der Zustand liegt unter
`~/.local/state/weltgewebe/staging-cell`, und der Clustername ist fest.

Vor dem ersten `up` müssen die in `platform/toolchain.lock.json` gepinnten
Werkzeuge und Drittartefakte exakt in den T084-Toolchain-Cache installiert werden:

```bash
export WELTGEWEBE_STAGING_OWNER_ID="owner-t084-staging"
uv run --project tools/py --locked python scripts/platform/bootstrap_tools.py --cache "$HOME/.local/state/weltgewebe/staging-cell/toolchain"
uv run --project tools/py --locked python scripts/platform/staging_cell.py up --owner-id "$WELTGEWEBE_STAGING_OWNER_ID"
```

`bootstrap_tools.py` schreibt dabei das von `staging_cell.py` verlangte
`toolchain/receipt.json`; ein anderer Cachepfad wird fail-closed abgewiesen.

Beim ersten `up --owner-id <id>` wird die externe Secretquelle vor jeder
Clustererzeugung erzeugt bzw. validiert. Anschließend bindet ein
`bootstrap-in-progress`-Receipt Owner, exakten Commit und Secretquellen-Hash,
bevor Kind erzeugt wird. Dadurch bleiben auch abgebrochene Bootstraps
wiederaufnehmbar oder über `down --owner-id <id>` kontrolliert abbaubar. Ein
späteres `up` nach `down` bleibt am Bootstrap-Commit und am ursprünglichen Owner
gebunden; ein stilles Umbinden des Datenpfads an ein inzwischen weitergelaufenes
`main` ist verboten. Sobald eine App-Aktivierung läuft oder erfolgreich
abgeschlossen ist, verweigert `up` fail-closed eine Rückschreibung auf den
Bootstrap-Zustand; Wiederherstellung einer aktivierten Zelle bleibt ein eigener,
ausdrücklich geprüfter Recovery-Pfad.

Ein **App-Release** ist davon getrennt: `activate --owner-id <id>
--source-commit <sha>` akzeptiert nur einen exakten aktuellen Public-`main`-Commit
mit passendem Staging-Image-Promotion-Receipt. PostgreSQL/NATS und ihre
`weltgewebe-staging-source`-/Data-Kustomization bleiben dabei am Bootstrap-Commit.
Für die App wird eine eigene commitgebundene GitRepository-Quelle
`weltgewebe-staging-app-source` angelegt. Die statische Staging-Überlagerung behält
weiter `promotion-required`; erst die Laufzeit-Kustomization ersetzt API und Web
durch die im Promotion-Receipt gebundenen unveränderlichen Digests. Vor der ersten
Clusteränderung schreibt `activate` ein nicht-geheimes
`app-activation-in-progress`-Receipt. Ein Abbruch bleibt dadurch in `status` als
degradiert sichtbar, und ein Wiederanlauf darf nur exakt denselben pending Commit
fortsetzen. Dieser bereits vor der ersten Clusteränderung geprüfte Commit darf zur
Recovery weiterverwendet werden, auch wenn Public `main` inzwischen fortgeschritten
ist; ein anderer Commit bleibt verboten.

PostgreSQL und NATS verwenden statische, klassenlose und vorgebundene HostPath-PVs
mit `Retain`. Persistente Daten werden ausschließlich in den ersten Kind-Worker
`weltgewebe-staging-worker` gemountet. PV-Node-Affinity und Pod-NodeSelector
erzwingen denselben Daten-Worker. Das ist bewusst **kein HA-Failover**: bei
Node-Ausfall bleibt der Datendienst lieber unavailable, statt ohne externes
Fencing einen zweiten Schreiber auf dieselben Dateien zu starten.

Volume-Rechte werden nur für leere Volume-Wurzeln initialisiert. Ein gesundes
oder bereits befülltes Datenverzeichnis wird bei erneutem `up` ausschließlich
geprüft; rekursive `chown`-/`chmod`-Änderungen über laufende oder erhaltene Daten
sind verboten. `fsGroupChangePolicy: OnRootMismatch` begrenzt zusätzlich
unbeabsichtigte rekursive Rechtearbeit durch Kubernetes.

Die Data-NetworkPolicies erlauben PostgreSQL (`5432`) und NATS (`4222`) nur Pods
mit `app.kubernetes.io/name=weltgewebe-api` im exakten Namespace
<!-- commonthing-naming: legacy -->
`weltgewebe-staging`. Die frühere namespaceweite Freigabe über das Legacy-Label
`weltgewebe.net/data-client` ist für diese Staging-Datenpfade nicht maßgeblich.
Neue Secret-Binding-Metadaten verwenden gemäß Naming-Policy den kanonischen
Schlüssel `commonthing.net/external-secret-source-sha256`. Für private GHCR-Images
verlangt `activate` zusätzlich die externe, nicht von Git erzeugte Datei
`~/.local/state/weltgewebe/staging-cell/secrets/staging-registry.json` als
owner-private Datei mit Modus `0600`. Sie enthält ausschließlich den Pull-Zugang;
der Controller prüft damit beide exakten promoted Digests **vor** der ersten
Cluster-Mutation und injiziert anschließend ein server-side-applied
`kubernetes.io/dockerconfigjson`-Secret. Im Receipt bleiben nur Quell- und
Dockerconfig-Hashes, Secretname und Registry; der Credentialwert wird nicht
protokolliert oder in Git/Argumentlisten übernommen. Auch Datenbank- und
Runtime-Secret werden serverseitig angewandt, damit ihre Klarwerte nicht als
`kubectl.kubernetes.io/last-applied-configuration` dupliziert werden.

Nach dem Apply fordert jedes `up` über Flux' kanonische
`reconcile.fluxcd.io/requestedAt`-Annotation zuerst eine neue Source-Reconciliation
an und akzeptiert sie erst, wenn `status.lastHandledReconcileAt`, aktuelle Generation
und exakte Receipt-Revision übereinstimmen. Danach wird mit demselben eindeutigen
Token die Daten-Kustomization angestoßen. PVC-Sichtbarkeit und -Bindung bleiben im
gemeinsamen 8-Minuten-Kustomization-Budget; sobald beide Claims sichtbar sind, gilt
weiterhin der 45-Sekunden-Bindefehler. Erst danach muss die Kustomization denselben
Reconcile-Token, aktuelle Generation, `Ready=True` und die exakte angewandte
Receipt-Revision melden. Zusätzlich müssen PostgreSQL, NATS, `source-controller` und
`kustomize-controller` als aktuelle Deployments die gewünschten verfügbaren,
bereiten und aktualisierten Replikas melden. `status` verwendet dieselben
Live-Workload-Schranken und degradiert bei fehlenden, stale oder nicht verfügbaren
Ressourcen statt einen früheren Ready-Zustand fortzuschreiben.

Die Staging-Zelle kann damit eine erfolgreich promovierte API/Web-Version
staging-only aktivieren und deren App-Source, Kustomization, Workloads, exakte
Image-Digests sowie Registry-Secret-Bindung live zurücklesen. Sie etabliert
weiterhin **keinen** Gateway-/DNS-/TLS-Außenbeweis, kein Delete-to-Prove, keine
NATS-Authentisierung/TLS und keinen Produktions-Kubernetes-Cutover. Diese Grenzen
sind getrennt zu beweisen.

## Beweise

```bash
make platform-check
make platform-render
make platform-kind-proof
# equivalent direct call (same uv-locked tools/py environment):
uv run --project tools/py --locked python scripts/platform/validate_platform.py
uv run --project tools/py --locked python scripts/platform/oci_proof_mirror.py validate
```

Der unprivilegierte Workflow `kubernetes-platform` prüft Pull Requests gegen den exakt ausgecheckten Merge-Zustand, ohne Zugriff auf private OCI-Pakete. Der getrennte Workflow `kubernetes-platform-proof` läuft nach passenden Pushes auf `main` oder bei einem ausdrücklich an den vollständigen aktuellen Main-Commit gebundenen Handstart. Er prüft den privaten OCI-Mirror sowie die vollständige Flux-/GitOps- und HA-Wiederherstellungskette gegen eindeutig benannte, kurzlebige kind-Cluster. Wiederverwendete Beweise sind an Commit, Eingabemanifest, Werkzeug-Lock, OCI-Lock, Image- und Knotenbindungen sowie Registry-Sperren gebunden.

## Manuelles GewebeZelle-Pilotprofil

Eine eigenständige Pilotzelle kann die gemeinsame Anwendungsbasis mit einem zelleigenen Overlay, externer Secretbereitstellung, eigener Zellidentität und ausdrücklich konfigurierten Peerbeziehungen verwenden. Die automatische Auslieferung ist standardmäßig deaktiviert und wird nur mit PostgreSQL, vollständiger Identität, mindestens einem gültigen HTTPS-Ziel und einer auf dessen exakten DNS-Host und TCP-Port begrenzten Cilium-Egress-Regel gestartet. Die Basis erhält keine allgemeine Internetfreigabe.

Der Betreibervertrag steht in `docs/runbooks/gewebezelle-manual-pilot.md`. Für die gemeinsame Freigabe zweier unabhängiger Betreiber ergänzt `docs/runbooks/gewebezelle-two-operator-pilot-v1.md` einen commit-, image-, peer-, egress-, restore- und rollbackgebundenen strukturellen Vorprüfvertrag. Der statische Validator bescheinigt niemals Aktivierbarkeit; dafür fehlen bewusst die externe Receipt-Prüfung, Trust-Anker und ein autoritativer Replay-Ledger. Beide Verträge etablieren weder Self-Service noch einen GewebeZelle-Operator und ersetzen nicht die getrennte Kubernetes-Produktionsfreigabe.
