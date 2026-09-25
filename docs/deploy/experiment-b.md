---
id: deploy.experiment-b
title: "Experiment B: isolierter Kubernetes-Zieltest"
doc_type: runbook
status: active
summary: Revisionsgebundener Betriebs- und Beweisvertrag für den isolierten T085-k3s-Zieltest.
last_reviewed: "2026-09-25"
relations:
  - type: relates_to
    target: platform/README.md
  - type: relates_to
    target: scripts/platform/experiment_b_runtime.py
---

# Experiment B: isolierter Kubernetes-Zieltest

Experiment B ist der revisionsgebundene Zieltest für `WELTGEWEBE-OS-V1-T085`.
Er prüft die Portierbarkeit des commonThing-Kubernetes-/GitOps-Vertrags auf einer
temporären realen Kubernetes-Distribution, ohne Produktionsverkehr oder
Produktionsdaten zu berühren.

## Grenze

Der Test besitzt ausschließlich die VM `commonthing-experiment-b` auf dem
Heim-PC und State unter
`~/.local/state/commonthing/experiment-b`. Ein expliziter `--state-root`
darf nur genau dieses Verzeichnis oder einen seiner Nachfahren bezeichnen.

Nicht Bestandteil des Tests sind:

- Produktions-DNS, -TLS oder -Traffic;
- Produktionsdaten;
- commonserver oder andere Hosts;
- ein Produktions-Cutover;
- Aussagen über Produktionskapazität oder Hochverfügbarkeit.

## Substrat

Die Zielzelle verwendet eine temporäre libvirt/KVM-Ubuntu-VM mit begrenzten
CPU-, RAM- und Disk-Ressourcen. Darin läuft die in
`platform/clusters/experiment-b/config.json` exakt gepinnte k3s-Version.
Cilium stellt Netzwerk/Gateway API bereit, Flux/Kustomize die deklarative
Reconciliation. `kind` und `staging_cell.py` sind hier keine
Runtime-Controller.

API und Web werden nur als immutable GHCR-Digests aktiviert, die zum selben
geschützten `main`-Commit gehören. Registry- und Datenbank-Secrets werden
außerhalb von Git injiziert; der Lifecycle-Controller schreibt keine
Secretwerte in seine Receipts oder Terminalfehler.

## Beweisfolge

Die operative Reihenfolge ist:

1. `preflight` und `prepare`;
2. `create-vm`;
3. `install-k3s` und `install-platform`;
4. `inject-secrets`;
5. `apply-release` mit exaktem `main`-Commit sowie API-/Web-Digests;
6. `semantic-activate` als reale Ollama-Providerprobe;
7. `seed-t048-fixture` für die kanonische T048-Domain-Fixture;
8. `functional-readback`;
9. `t048-load-proof`;
10. `recovery-proof`;
11. `status` und `portability-report`;
12. `teardown`.

Jeder Schritt erzeugt einen begrenzten Receipt unter dem Experiment-B-State.
Der Portability-Report akzeptiert nur erfolgreiche, zum selben Release
gehörende Pflicht-Receipts.

## Semantische Suche und T048

Zwei verschiedene Fragen werden absichtlich getrennt:

- `semantic-activate` zieht das gepinnte Ollama-Modell, prüft dessen Digest und
  erzeugt einen echten endlichen Embedding-Vektor mit der erwarteten Dimension.
- `seed-t048-fixture` übernimmt die kanonische T048-Logik mit deterministischen
  synthetischen Projektionen. Die Projektionen und ihre Jobs werden in derselben
  Datenbanktransaktion fertiggestellt und die Generation wird erst dann
  aktiviert.

Damit misst T048 API-/Datenbankverhalten bei der kanonischen 20k/100k-Fixture,
ohne die Messung mit 20.000 Modellinferenz-Aufrufen zu vermischen. Die reale
Providerprobe belegt separat, dass das gepinnte Ollama-Modell tatsächlich
ansprechbar ist. Keiner der beiden Beweise wird als Produktionskapazitätsbeweis
interpretiert.

## Recovery

`recovery-proof` sichert PostgreSQL und JetStream, löscht die zugehörigen PVCs
und stellt beide aus einer leeren Zielzustandsbasis wieder her. Danach werden
Flux und die Anwendung wieder aufgenommen.

`RTO` endet erst, wenn API und Web wieder verfügbar sind. `RPO=0` wird nur
ausgegeben, wenn Datenbank- und JetStream-Signaturen vor und nach der
Wiederherstellung übereinstimmen. Bei einem Fehler versucht der Controller die
betroffenen Flux-Kustomizations erneut zu suspendieren und schreibt einen
fehlgeschlagenen Receipt statt eines Erfolgssignals.

## Teardown

`teardown` entfernt nur die Experiment-B-Domain, ihren libvirt-Pool und den
Experiment-B-State. Vorher werden die vorhandenen Receipt-Hashes in einen
dauerhaften Retirement-Receipt übernommen.

Ein erfolgreicher Teardown beweist die Rückbaubarkeit dieses Testbeds. Er
beweist weder einen Produktions-Cutover noch Produktions-Hochverfügbarkeit.