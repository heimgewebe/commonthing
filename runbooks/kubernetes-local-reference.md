---
id: runbooks.kubernetes-local-reference
title: Kubernetes- und GitOps-Referenzzelle lokal prüfen
summary: Fail-closed Ablauf für den isolierten kind-, Cilium-, Gateway- und Flux-Beweis ohne Produktionswirkung.
role: runbooks
organ: ops
status: canonical
canonicality: operational
lifecycle_state: active
owner: ops
review_after: 2026-08-16
last_reviewed: 2026-07-16
depends_on:
  - platform.readme
relations:
  - type: relates_to
    target: docs/reports/kubernetes-platform-foundation-status.md
  - type: verifies
    target: scripts/platform/kind_reference.py
verifies_with:
  - scripts/platform/kind_reference.py
---

# Kubernetes- und GitOps-Referenzzelle lokal prüfen

## Vorbedingungen

- sauberer, isolierter commonThing-Worktree;
- funktionierender Docker-Daemon;
- Linux amd64;
- kein bestehender Cluster mit dem gewählten Namen;
- keine laufende Produktionsänderung in diesem Pfad.

Die lokale Datenzelle, der Migrationsjob und die lokale App verwenden eine deterministische, öffentliche Test-Fixture als ConfigMap; reale Geheimnisse werden weder benötigt noch geschrieben. Staging und Produktion behalten unverändert den externen Secretvertrag.

Die Werkzeuge werden nicht global installiert. `bootstrap_tools.py` lädt sie in den ignorierten Repositorycache und verifiziert jeden SHA-256.
Gateway API ist zusätzlich an den Cilium-Vertrag gebunden: Für Cilium 1.19.5 werden aus Gateway API 1.4.1 ausschließlich GatewayClass, Gateway, HTTPRoute, ReferenceGrant und GRPCRoute installiert. TLSRoute bleibt uninstalled, weil commonThing es nicht benötigt und Cilium fehlende optionale TLSRoute-Unterstützung sauber deaktiviert. Der Bootstrap weist zusätzliche oder falsch gebundene CRDs fail-closed zurück.

## Statischer Vertrag

```bash
make platform-check
make platform-render
```

## Direkter lokaler Beweis

```bash
python3 scripts/platform/kind_reference.py proof   --cluster weltgewebe-reference   --mode direct
```

Der Runner baut API und Web, lädt sie in kind, installiert Gateway API, Cilium/Hubble und Flux, führt den deklarativen Migration-only-Job kontrolliert vor dem Replica-Start aus, startet zwei API-Pods, prüft Restart und Gateway und entfernt ausschließlich den selbst markierten Cluster. Der kind-Cluster startet ohne Standard-CNI und ohne kube-proxy; Cilium übernimmt den Kubernetes-Serviceverkehr ausdrücklich über `kubeProxyReplacement=true`, weil sein Gateway-Controller sonst nicht aktiv wird. Für diesen Bootstrap liest der Runner die IPv4-Adresse des eigenen kind-Control-Plane-Containers aus und übergibt sie Cilium zusammen mit Port `6443`; die virtuelle Service-Adresse ist vor Ciliums Start noch nicht erreichbar. Weil kind keinen externen Cloud-LoadBalancer bereitstellt, aktiviert der lokale Referenzbeweis Ciliums Node IPAM (`nodeIPAM.enabled=true`, `defaultLBServiceIPAM=nodeipam`). Der erzeugte LoadBalancer-Service erhält dadurch reale kind-Node-Adressen und kann `Programmed=True` erreichen; ein bloß akzeptiertes Gateway ohne Statusadresse gilt nicht als bestanden. Der HTTP-Readback läuft von einem kind-Knoten an die im Gateway-Status veröffentlichte Adresse auf dem konfigurierten HTTP-Listener- und LoadBalancer-Port `80`. Er nutzt ausdrücklich weder NodePort noch Port-Forwarding. Damit ist der Cilium-LoadBalancer-, Gateway-, HTTPRoute- und Backend-Datenpfad innerhalb der kind-Zelle belegt; Erreichbarkeit vom Docker-Host oder aus einem externen Netz wird nicht behauptet.

## GitOps-Beweis

Ein Git-Branch muss bereits veröffentlicht sein:

```bash
python3 scripts/platform/kind_reference.py proof   --cluster weltgewebe-reference-gitops   --mode gitops   --source-ref <branch>

# Für einen unveränderlichen CI-/Releasebeweis statt eines beweglichen Branches:
python3 scripts/platform/kind_reference.py proof   --cluster weltgewebe-reference-commit   --mode gitops   --source-commit <vollständiger-git-commit>
```

Zusätzlich wird ein absichtlicher Replica-Drift gesetzt und durch Flux korrigiert.

## Bedienoberfläche

Der Runner bietet nur den vollständigen `proof` und das besitzgeprüfte `down`.
Es gibt keine veröffentlichten Zwischenphasen-Subcommands.

## Abbruch und Diagnose

Bei Fehlern liegen redigierte Kubernetesdiagnosen unter `.cache/weltgewebe-platform/failures/<cluster>/`. Secretwerte werden nicht in Receipts geschrieben. Ein Cluster ohne gültigen Marker wird niemals durch `down` gelöscht.

## Manuelles eigentumsgebundenes Cleanup

Nur für einen abgebrochenen Lauf mit dem exakten Commit und der im Proof-Receipt gebundenen Owner-ID:

```bash
python3 scripts/platform/kind_reference.py down \
  --cluster weltgewebe-reference \
  --commit <exakter-commit> \
  --owner-id <exakte-owner-id> \
  --receipt .cache/weltgewebe-platform/cleanup.json
```

`--receipt` ist optional, aber für einen revisionsfähigen manuellen Cleanup empfohlen. Ohne exakt passende Bindung für Repository, Cluster, Commit und Owner-ID verweigert der Runner jede Löschung. Verwaiste, fremde oder unlesbare Marker werden nicht übernommen oder automatisch entfernt.
