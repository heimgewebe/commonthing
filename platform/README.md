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
last_reviewed: 2026-09-10
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
- Der Referenzrunner verwendet für Local/CI einen deklarativen Migration-only-Job mit öffentlicher ConfigMap-Fixture. Die persistente Staging-Zelle führt denselben Migrationsmodus ausschließlich mit dem exakt promovierten API-Digest und externem Staging-Secret aus; Produktion bleibt separat freigabepflichtig.
- Staging und Production benötigen einen externen, auditierten Secretpfad.
- Eigene Container laufen ohne Root, ohne Service-Account-Token, ohne Privilege Escalation und mit Default-Deny-Netzpolitik.
- Der Referenzrunner übernimmt oder löscht niemals einen bereits vorhandenen Cluster.
- Proof-Cluster werden lokal unter einem pro Cluster serialisierten Ownership-Lock reserviert; Cleanup verlangt den exakten Commit und dieselbe Owner-ID. Verwaiste Marker werden fail-closed nicht automatisch entfernt.
- Werkzeugarchive werden vor jeder Schreibwirkung vollständig geprüft; nur reguläre Dateien und Verzeichnisse sind zulässig. Symlinks, Hardlinks, Devices, FIFOs, Traversal und widersprüchliche Member werden fail-closed abgewiesen; die ausführbare Datei wird anschließend atomisch installiert.
- Produktionsdeployments, DNS, Compose und reale Replikazahlen werden durch diesen Vertrag nicht verändert.

## Persistente Staging-Zelle

`scripts/platform/staging_cell.py` verwaltet genau eine owner- und commitgebundene
Staging-Zelle namens `commonthing-staging`. Der öffentliche CLI-Vertrag bietet
bewusst keinen frei wählbaren Cluster- oder State-Root: der kanonische Zustand
liegt unter `~/.local/state/commonthing/staging-cell`. Technische Kubernetes-Namen
der lokalen Staging-Laufzeit verwenden `commonthing-*`.

Vor dem ersten neuen `up` werden die in `platform/toolchain.lock.json` gepinnten
Werkzeuge und Drittartefakte exakt im kanonischen Cache installiert:

```bash
export COMMONTHING_STAGING_OWNER_ID="owner-t084-staging"
uv run --project tools/py --locked python scripts/platform/bootstrap_tools.py --cache "$HOME/.local/state/commonthing/staging-cell/toolchain"
uv run --project tools/py --locked python scripts/platform/staging_cell.py up --owner-id "$COMMONTHING_STAGING_OWNER_ID"
```

`bootstrap_tools.py` schreibt dabei das von `staging_cell.py` verlangte
`toolchain/receipt.json`; ein anderer Cachepfad wird fail-closed abgewiesen.

### Einmaliger Cutover einer bestehenden Legacy-Zelle

Eine vorhandene, noch nicht aktivierte Legacy-Zelle unter
`~/.local/state/weltgewebe/staging-cell` wird **nicht** durch einen Fallback oder
Symlink weiterbenutzt. Der einmalige Befehl

```bash
uv run --project tools/py --locked python scripts/platform/staging_cell.py migrate-legacy-state --owner-id "$COMMONTHING_STAGING_OWNER_ID"
```

prüft den alten Cell-Receipt und Owner, verweigert aktivierte oder mehrdeutige
Zustände, löscht nur den exakt gebundenen alten Kind-Cluster `weltgewebe-staging`
und verifiziert dessen Abwesenheit. Erst danach wird `data/` auf demselben
Dateisystem atomar in den kanonischen State-Root verschoben. Dadurch bleiben
Inodes, UID/GID und Dateimodi von PostgreSQL und NATS erhalten. Promotion-Receipts
und private Secrets werden bytegleich übernommen und nach SHA-256 verifiziert;
Secrets müssen reguläre owner-eigene Dateien mit Modus `0600` sein. Die alten
Cell-/Toolchain-Receipts werden unverändert unter `legacy-evidence/` erhalten. Der
alte Toolchain-Receipt wird **nicht** als aktive Toolchain übernommen, weil er
absolute Legacy-Pfade bindet. Anschließend ist die Toolchain im neuen State-Root
neu zu bootstrappen und `up` zu starten.

Vor dem Daten-Move hält ein erhaltenes Legacy-Receipt den Bootstrap-Commit fest;
damit ist der alte Cluster rekonstruierbar. Vor dem ersten Schreibzugriff des
neuen Clusters kann `data/` außerdem per umgekehrtem Same-Filesystem-Rename in
den Legacy-Root zurückgeführt werden. Ein erfolgreicher Migrations-Receipt bindet
beide Roots, beide Clusteridentitäten, Owner, alte Receipt-Hashes, Secret-Hashes,
Promotion-Dateien und die Inode-/Ownership-Identität der Daten. Kein normaler
Controllerpfad fällt still auf den alten Root zurück.

Beim ersten `up --owner-id <id>` wird die externe Secretquelle vor jeder
Clustererzeugung erzeugt bzw. validiert. Anschließend bindet ein
`bootstrap-in-progress`-Receipt Owner, exakten Commit und Secretquellen-Hash,
bevor Kind erzeugt wird. Dadurch bleiben auch abgebrochene Bootstraps
wiederaufnehmbar oder über `down --owner-id <id>` kontrolliert abbaubar. Ein
späteres `up` nach `down` bleibt am Bootstrap-Commit und am ursprünglichen Owner
gebunden; ein stilles Umbinden des Datenpfads an ein inzwischen weitergelaufenes
`main` ist verboten. Sobald eine App-Aktivierung läuft oder erfolgreich
abgeschlossen ist, verweigert `up` fail-closed eine Rückschreibung auf den
Bootstrap-Zustand.

Ein **App-Release** ist davon getrennt: `activate --owner-id <id>
--source-commit <sha>` akzeptiert nur einen exakten aktuellen Public-`main`-Commit
mit passendem Staging-Image-Promotion-Receipt. PostgreSQL/NATS und ihre
`commonthing-staging-source`-/Data-Kustomization bleiben dabei am Bootstrap-Commit.
Für die App wird `commonthing-staging-app-source` angelegt. Die statische
Staging-Überlagerung behält `promotion-required`; die Laufzeit-Kustomization bindet
API und Web an die promovierten Digests und transformiert die effektiven
Staging-Objekte auf `commonthing-api`/`commonthing-web`. Der Repo-Pfad
`platform/apps/weltgewebe` bleibt vorläufig nur als gemeinsame Source-Layout-
Kompatibilität mit Produktion bestehen; er ist keine kanonische Runtimeidentität.

Vor dem ersten Kubernetes-Read validiert `activate` den Owner- und
Bootstrap-Marker und setzt die dedizierte Kubeconfig der `commonthing-staging`-Zelle.
Nach Commit-, Promotion- und Registry-Preflight schreibt der Controller vor der
ersten Clusteränderung ein nicht-geheimes `app-activation-in-progress`-Receipt.
Ein Wiederanlauf darf nur denselben pending Commit, dieselbe Promotion-Evidenz und
dieselben gespeicherten Registry-Secret-Hashes fortsetzen.

Nach Secret- und Registry-Injektion wendet `activate` `default-deny`, `allow-dns`
und `allow-api-data-egress` im Staging-Namespace an und wartet auf den Cilium-
Policy-Beweis. Danach läuft der einmalige, digestgebundene Migrations-Job. Die
Anwendungsvariablen `WELTGEWEBE_API_MIGRATION_ONLY` und
`WELTGEWEBE_API_STARTUP_MIGRATIONS` bleiben bewusst Anwendungs-/Protokollvertrag;
sie werden durch diesen Runtime-Namenscutover nicht umbenannt.

PostgreSQL und NATS verwenden statische, klassenlose und vorgebundene HostPath-PVs
`commonthing-staging-postgres` und `commonthing-staging-nats` mit `Retain`. Die
Daten werden ausschließlich in `commonthing-staging-worker` unter
`/var/local/commonthing-staging` gemountet. PV-Node-Affinity und Pod-NodeSelector
erzwingen denselben Daten-Worker. Das ist bewusst kein HA-Failover.

Die interne PostgreSQL-Datenbank und der Datenbankbenutzer `weltgewebe` bleiben
beim Cutover als persistierte Datenkompatibilität unverändert; ihre Umbenennung
wäre eine eigene, transaktionale Datenmigration und ist keine Voraussetzung für
eine kanonische Staging-Runtimeidentität. Ebenso bleiben `weltgewebe.net/*`-Keys
als bestehender Anwendungs-/Protokollvertrag bestehen.

Die Data-NetworkPolicies erlauben PostgreSQL (`5432`) und NATS (`4222`) nur
`commonthing-api`-Pods im exakten Namespace `commonthing-staging`. Neue
Secret-Binding-Metadaten verwenden `commonthing.net/external-secret-source-sha256`.
Für private GHCR-Images verlangt `activate` zusätzlich die externe Datei
`~/.local/state/commonthing/staging-cell/secrets/staging-registry.json` als
owner-private Datei mit Modus `0600`. Der Credentialwert wird weder protokolliert
noch in Git, Argumentlisten oder Receipts übernommen. Fehlt der echte
`read:packages`-Credential, bleibt die Zelle gesund und aktivierungsbereit; das
Registry-Gate wird nicht umgangen.

Nach dem Apply erzwingt `up` weiterhin exakte Flux-Revisionen, gebundene PVCs und
gesunde PostgreSQL-, NATS-, `source-controller`- und `kustomize-controller`-
Deployments. `status` prüft denselben Livezustand. Der Cell-Receipt hält erfolgreiche
Migration-/Aktivierungsbeweise fest; Produktion bleibt unverändert
(`production_changed=false`). Die Zelle etabliert weiterhin keinen
Gateway-/DNS-/TLS-Außenbeweis, kein Delete-to-Prove, keine NATS-Authentisierung/TLS
und keinen Produktions-Kubernetes-Cutover.

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
