---
id: runbook.gewebezelle-manual-pilot
title: Manuelles GewebeZelle-Pilotprofil
doc_type: runbook
status: active
summary: >
  Beschreibt den ersten revisionsgebundenen Betreiberpfad für eine eigenständige GewebeZelle mit GitOps, externer Secretbereitstellung und automatischer Föderationsauslieferung.
relations:
  - type: depends_on
    target: platform/cell-profile.contract.json
  - type: relates_to
    target: docs/specs/federation-core.md
  - type: relates_to
    target: docs/proofs/weltgewebe-os-v1-t005-two-cell-proof.md
  - type: relates_to
    target: platform/README.md
  - type: relates_to
    target: docs/runbooks/gewebezelle-two-operator-pilot-v1.md
  - type: relates_to
    target: platform/cell-pilot/two-operator-pilot.contract.json
---

# Manuelles GewebeZelle-Pilotprofil

## Zweck und Grenze

Dieses Profil ermöglicht einem technisch betreuten Orts-, Regional- oder Institutionsgewebe, eine eigenständige commonThing-Instanz als Pilot aufzubauen und mit ausdrücklich freigegebenen Nachbarzellen zu verbinden. Es ist ein manueller GitOps-Betreibervertrag, keine Selbstbedienung, kein GewebeZelle-Operator und keine allgemeine Produktionsfreigabe.

Die öffentliche Föderationsgrenze verwendet ausschließlich HTTPS, JSON und Ed25519. Sie benötigt keinen gegenseitigen Kubernetes-, PostgreSQL- oder NATS-Zugriff.

## Erforderliche Plattform

Eine Pilotzelle benötigt mindestens:

- ein eigentumsgebundenes Kubernetes-Namespace oder einen eigenen Cluster;
- die kanonische commonThing-Kustomize-Basis mit umgebungseigenem Overlay;
- digestgebundene API- und Webimages aus einem geprüften Commit;
- PostgreSQL mit angewandten Migrationen;
- NATS JetStream;
- Gateway API, öffentliches DNS und gültiges TLS;
- einen externen Secretpfad;
- ein vom Cluster unabhängiges Backupziel sowie einen gemessenen Restore;
- SLO-, Alarm-, Upgrade- und Rollbackverantwortung.

`platform/cell-profile.contract.json` ist die maschinenlesbare Mindestgrenze. Für die strukturelle Vorbereitung einer gemeinsamen Aktivierungsentscheidung zweier unabhängiger Betreiber gilt zusätzlich `platform/cell-pilot/two-operator-pilot.contract.json` mit dem Runbook [Zwei-Betreiber-GewebeZelle-Pilotvertrag v1](gewebezelle-two-operator-pilot-v1.md). Die öffentliche `.invalid`-Vorlage ist absichtlich nicht aktivierbar und ersetzt keinen realen WAN-Pilot.

## Zellidentität

Nicht geheime Identitätsdaten werden im zelleigenen Overlay gesetzt:

```yaml
FEDERATION_CELL_ID: hamburg.example
FEDERATION_PUBLIC_BASE_URL: https://hamburg.example
FEDERATION_KEY_ID: key-2026-01
AUTH_TRUSTED_PROXIES: none
```

`AUTH_TRUSTED_PROXIES` muss bewusst auf die tatsächlichen Gateway-/Proxyadressen oder ausdrücklich auf `none` gesetzt werden. Eine implizite Vertrauensentscheidung ist verboten.

Der private 32-Byte-Ed25519-Seed wird base64url-kodiert ausschließlich als Secret-Key `federation-signing-key-b64` in `weltgewebe-runtime` injiziert. Er darf weder im Overlay noch in `FEDERATION_PEERS_JSON`, Logs oder Receipts erscheinen.

## Peer- und Liefervertrag

Peerbeziehungen werden vollständig und versionsgebunden über `FEDERATION_PEERS_JSON` beschrieben. Öffentliche Schlüssel sind keine Geheimnisse. `delivery_base_url` aktiviert ausschließlich den ausgehenden Transport zu diesem Peer; ohne dieses Feld bleibt die Beziehung eingehend nutzbar.

```json
[
  {
    "cell_id": "schleswig-holstein.example",
    "state": "trusted",
    "allow_neighbourhood": true,
    "allowed_event_types": ["object.upserted", "object.deleted"],
    "delivery_base_url": "https://schleswig-holstein.example",
    "keys": [
      {
        "key_id": "key-2026-01",
        "public_key": "<base64url-public-key>",
        "active": true
      }
    ]
  }
]
```

Es gibt keine automatische Peer-Discovery oder Vertrauensbildung. Beide Betreiber vergleichen Zell-ID, öffentliche URL, Key-ID und Public Key über einen getrennten Kanal, bevor `state: trusted` aktiviert wird.

## Ausgehende Netzwerkfreigabe

Die Kubernetes-Basis bleibt egress-default-deny und enthält bewusst keine allgemeine Internetfreigabe. Das manuelle Zelloverlay muss deshalb für jeden ausgehenden Peer eine explizite Cilium-FQDN-Regel enthalten. Ausgangspunkt ist `platform/apps/weltgewebe/cell-pilot/federation-delivery-egress.yaml`. Vor der Aufnahme in das Overlay werden `peer.example.invalid`, Policy-Name und gegebenenfalls Port durch den exakt verifizierten DNS-Host und TCP-Port aus `delivery_base_url` ersetzt.

Für ausgehende Peerziele sind ausschließlich exakte `toFQDNs.matchName`-Einträge zulässig. Das L7-DNS-Proxy-Element `rules.dns.matchPattern: "*"` ist ausschließlich für Abfragen an den ausgewählten Cluster-DNS-Dienst erforderlich. `toFQDNs.matchPattern`, `toEntities: world`, pauschale CIDR-Freigaben und ein unveränderter Beispielhost sind Aktivierungsblocker. IP-Literale benötigen einen gesondert geprüften CIDR-Vertrag; das Standardprofil behauptet dafür keine Freigabe. Der gerenderte Zellstand muss daher denselben Zielhost- und Portsatz wie `FEDERATION_PEERS_JSON` enthalten.

## Automatische Auslieferung

Der Delivery-Worker wird explizit eingeschaltet:

```yaml
FEDERATION_DELIVERY_ENABLED: "true"
FEDERATION_DELIVERY_POLL_SECONDS: "5"
FEDERATION_DELIVERY_REQUEST_TIMEOUT_SECONDS: "10"
FEDERATION_DELIVERY_BATCH_SIZE: "20"
FEDERATION_DELIVERY_MAX_ATTEMPTS: "8"
```

Beim Start gelten folgende Stopbedingungen:

- Ohne vollständige Zellidentität startet kein Worker.
- Ohne PostgreSQL gibt es keinen flüchtigen Ersatzbetrieb.
- Ohne mindestens einen Peer mit gültiger HTTPS-`delivery_base_url` wird die Aktivierung abgewiesen.
- Redirects, Zugangsdaten in URLs, HTTP, IP-Literale, Queryparameter und Fragmente sind verboten.

Beim lokalen Fachcommit werden Outbox-Ereignis und Zielreservierungen in derselben PostgreSQL-Transaktion erzeugt. Dadurch gibt es keinen periodischen Vollscan der historischen Outbox. Wenn ein HTTPS-Endpoint oder sein gebundener Vertrauens-, Reichweiten- oder Ereignisvertrag erstmals aktiviert oder geändert wird, reserviert eine einmalige transaktionale Rückfüllung die für Vertrauens- und Reichweitenvertrag relevante Objektgeschichte. Ereignisklassen werden beim Claim geprüft: Eine nicht freigegebene Zwischenversion wird sichtbar als Policy-`dead` markiert und hält spätere Versionen geordnet zurück, bis eine revisionsgebundene Policy-Erweiterung sie reaktiviert. Mehrere API-Replikate beanspruchen fällige Einträge mit `SKIP LOCKED` und einer Lease, die das Request-Timeout um einen Sicherheitsabstand übersteigt. Transiente Netzwerkfehler, `429`, `5xx`, Ablehnung und Quarantäne führen zu begrenztem Backoff. `Applied` und `Duplicate` schließen die Zustellung nur ab, wenn bestätigte Event-ID und Objektversion exakt zur gesendeten Hülle passen. Fremde Bestätigungen, ungültige Erfolgsantworten, beschädigte Outbox-Hüllen und andere Protokollfehler werden als `dead` erhalten und nicht still verworfen. Wird ein bereits reserviertes Ziel nachträglich blockiert oder eingeschränkt, verarbeitet der Worker den Eintrag ohne Netzwerkzugriff zu einem sichtbaren Policy-`dead` statt ihn unclaimbar stehen zu lassen. Eine explizite Änderung des Endpoint-/Policy-Fingerprints reaktiviert nur freigegebene Policy-Fehler sowie ausgeschöpfte Ablehnungs-, Quarantäne- und Retryzustände; beliebige terminale Fehler bleiben unangetastet.

Vor einer einzelnen, zeitlich begrenzten HTTP-Zustellung prüft der Worker die signierte Outbox-Hülle, ihren gespeicherten Digest und einen Snapshot der aktuellen Peerpolicy. Während des Netzwerkzugriffs bleibt kein Datenbanklock offen. Eine nach dem Versand eintretende Blockierung kann die bereits ausgesandte idempotente Anfrage nicht zurückholen, verhindert aber weitere Claims und blockiert keine Konfigurationsänderung.

## Aktivierungsbeweis

Vor einer Nachbarschaftsfreigabe sind mindestens zu belegen:

1. `/federation/v1/cell` liefert die beabsichtigte Zell-ID, HTTPS-Basis-URL, Key-ID und den unabhängig verglichenen Public Key.
2. Eine globale Testmutation erzeugt einen Outbox- und einen Delivery-Eintrag.
3. Die Zielzelle bestätigt `Applied`; ein identischer Replay wird als `Duplicate` abgeschlossen.
4. Ein kontrollierter transienter Fehler führt zu `retry` und später zu `delivered`.
5. Zwei parallele Worker senden denselben Zielzustand nicht doppelt wirksam.
6. Der gerenderte Zell-Overlay enthält für jeden ausgehenden Peer genau dessen verifizierten DNS-Host und TCP-Port als Cilium-FQDN-Egress; Beispielhost, Wildcard und allgemeiner Welt-Egress fehlen.
7. Ein blockierter Peer oder entfernter Delivery-Endpoint erhält keine neuen Zustellungen.
8. Backup, PITR oder Blank-Cluster-Restore erhalten Outbox- und Deliveryzustände.
9. Ein Rollback auf den vorherigen API-Commit lässt bereits bestätigte Ereignisse bestätigt und offene Ereignisse weiter bearbeitbar.

## Betrieb und Diagnose

Der Betreiber überwacht mindestens:

- Anzahl `pending`, `retry`, `in_flight`, `delivered` und `dead` je Zielzelle;
- Alter des ältesten offenen Eintrags;
- letzte HTTP-Status- und Fehlerklasse;
- Quarantäneanstieg und Signaturfehler auf der Empfangsseite;
- Schlüsselablauf, Peerblockierung und Konfigurationsdrift;
- Backup- und Restorefrische.

Ein `dead`-Eintrag wird nicht durch Polling oder Zeitablauf erneut aktiviert. Nur eine explizite, revisionsgebundene Änderung des Endpoint-/Policy-Fingerprints darf die fest definierten Policy-, Ablehnungs-, Quarantäne- oder Ausschöpfungsklassen zurück auf `pending` setzen. Andere terminale Fehler benötigen weiterhin einen gesonderten, geprüften Reparaturpfad.

## Rücknahme

Die sichere erste Rücknahme ist `FEDERATION_DELIVERY_ENABLED: "false"`. Dadurch bleiben Outbox- und Deliveryzustände erhalten, während keine neuen ausgehenden Netzwerkoperationen gestartet werden. Eine Peerblockierung verhindert zusätzlich Annahme und neue Zielauswahl. Schlüssel dürfen erst nach einer koordinierten Rotation deaktiviert werden; Datenbanktabellen oder offene Zustände werden nicht als Rollbackmaßnahme gelöscht.

## Nichtaussagen

Dieses Profil belegt nicht:

- einen produktiven Kubernetes-Cutover von weltgewebe.net;
- einen bereits ausgeführten Zwei-Betreiber-WAN-Pilot;
- Self-Service-Provisionierung;
- einen GewebeZelle-Operator oder eine Zell-API;
- automatische Peer-Discovery;
- Multi-Region-HA;
- Identitätsmigration zwischen Zellen;
- eine öffentliche Quarantäneverwaltung.

Erst reale Pilotbetriebe liefern die stabilen Profile, aus denen später eine GewebeZelle-API oder ein Operator abgeleitet werden darf.
