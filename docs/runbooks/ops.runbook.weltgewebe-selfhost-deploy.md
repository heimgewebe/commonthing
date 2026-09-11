---
id: runbooks.ops.runbook.weltgewebe-selfhost-deploy
title: Selfhost-Deploy Runbook
doc_type: reference
status: deprecated
summary: Historische Referenz der Netlify-/IONOS-Selfhost-Migration.
relations:
  - type: relates_to
    target: docs/runbooks/README.md
  - type: relates_to
    target: docs/deployment.md
  - type: relates_to
    target: docs/deploy/heimserver.deployment.md
  - type: relates_to
    target: docs/runbooks/weltgewebe-ddns-runtime-verification.md
---
# Ops Runbook: Weltgewebe Self-Hosted Deployment

## Kontext

Dieses Runbook dokumentiert die historische Migration des Weltgewebe-Deployments von **Netlify**
zu einem **self-hosted Heimserver-Deployment mit edge-caddy**.

> [!NOTE]
> **Historische DNS-Phase**
> Dieses Dokument beschreibt den historischen Schritt von Netlify zu IONOS.
> Der heutige Zustand von `weltgewebe.net` nutzt INWX und den kanonischen
> Public-VPS-Pfad `commonserver`. Der frühere Heimberry-DDNS-Schreibpfad ist
> stillgelegt; die Nebendomains sind DNS-seitig noch offen.
>
> Die aktuelle Prüfanweisung für die Stilllegung steht in
> `docs/runbooks/weltgewebe-ddns-runtime-verification.md`. Die folgenden
> IONOS-, Heimserver- und Portforward-Beispiele sind keine heutige
> Betriebsanweisung.

Die damalige Migration beinhaltete:

- DNS-Umstellung
- Router-Portfreigaben
- Edge-Gateway-Validierung
- Mail-DNS-Fix

Ziel ist ein reproduzierbares Deployment-Runbook.

## Scope

- DNS-Migration
- Router-Portforward
- Caddy-Gateway
- typische Fehler

## DNS-Migration (Historisch)

Domain:

`weltgewebe.net`

### vorher

`Netlify DNS (nsone.net)`

### nachher

`IONOS DNS`
`ns1121.ui-dns.com`
`ns1044.ui-dns.org`
`ns1086.ui-dns.biz`
`ns1036.ui-dns.de`

### Historische Altwerte (ehemals notwendige IONOS-Records)

```text
# A-Records
@       A      <public-ip>       # weltgewebe.net
www     A      <public-ip>       # www.weltgewebe.net
api     A      <public-ip>       # api.weltgewebe.net

# MX (Mail-Empfang über IONOS)
@       MX     10 mx00.ionos.de.
@       MX     10 mx01.ionos.de.

# SPF
@       TXT    "v=spf1 include:_spf.perfora.net include:_spf.kundenserver.de ~all"

# DKIM
<selector>._domainkey TXT "v=DKIM1; k=rsa; p=<public-key>"

# DMARC
_dmarc  TXT    "v=DMARC1; p=quarantine; rua=mailto:postmaster@weltgewebe.net"
```

Dabei:

- `<public-ip>` bleibt Platzhalter
- `<selector>` bleibt generisch
- keine provider-internen Details einbauen

## Router-Konfiguration (kritisch)

Für self-hosted Deployments müssen folgende Ports freigegeben werden:

```text
TCP 80  → Heimserver
TCP 443 → Heimserver
```

Beispiel (FritzBox):

```text
Internet → Freigaben → Portfreigaben
HTTP   TCP 80
HTTPS  TCP 443
```

Ohne diese Ports ist das System öffentlich nicht erreichbar.

## Edge Gateway

Edge-Gateway:

`edge-caddy`

Container:

`caddy:2.x`

Ports:

```text
80
443
```

Routing:

```text
weltgewebe.net
api.weltgewebe.net
```

## Hairpin-NAT / Host-Header Fallstrick

Ein Test mit

```bash
curl http://<public-ip>
```

kann fehlschlagen, obwohl Port-Forwarding korrekt funktioniert.

Grund:

Der HTTP-Host-Header lautet dann `<public-ip>`, während Caddy-vHosts typischerweise nur auf

- `commonthing.net`
- `api.weltgewebe.net`

matchen.

### Korrekte Tests

**Option 1 – Domain testen:**

```bash
curl -I http://commonthing.net
```

**Option 2 – Host-Header setzen:**

```bash
curl -H "Host: commonthing.net" http://<public-ip>
```

**Option 3 – extern testen (empfohlen).**

Hairpin-NAT kann zusätzlich lokale Tests beeinflussen.

## Validierungschecks

### DNS

```bash
dig A weltgewebe.net
dig MX weltgewebe.net
```

### HTTP

```bash
curl -I http://commonthing.net
```

Expected:

```text
308 Permanent Redirect
Server: Caddy
```

### HTTPS

```bash
curl -I https://commonthing.net
```

Erwartung:

- HTTPS antwortet
- Zertifikat wird sauber ausgeliefert (kein SSL/TLS-Fehler)
- Response kommt über Caddy

## Ergebnis

Deployment-Kette:

```text
weltgewebe.net / api.weltgewebe.net
  ↓
INWX DNS / DDNS (für weltgewebe.net)
  ↓
Router Portforward
  ↓
edge-caddy
  ↓
Weltgewebe-Stack
```

System ist jetzt öffentlich erreichbar.

## Motivation

Ohne diese Dokumentation sind typische Fehlerszenarien schwer zu diagnostizieren:

- DNS korrekt, aber Router blockiert
- NAT-Loopback-Fehler
- fehlende MX-Records
- falsche DNS-Authority

Das Runbook verhindert zukünftige Deployment-Blocker.

## Optionale weitere Härtung

- DNS Healthcheck CI
- Deploy-Guard für fehlende A-Records
- Mail-Delivery Test
