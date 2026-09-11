---
id: deploy.domain-mail-migration-ionos-inwx-mailbox-brevo
title: "Architektur & Historie: Domain-/Mail-Migration IONOS zu INWX"
doc_type: reference
status: active
summary: >
  Aktuelle Providerarchitektur, historische IONOS-zu-INWX-Referenz und
  Beweisgrenzen für den commonThing-Mailidentitäts-Cutover.
relations:
  - type: relates_to
    target: docs/deploy/README.md
  - type: relates_to
    target: docs/deploy/DRIFT_POLICY.md
  - type: relates_to
    target: docs/deployment.md
  - type: relates_to
    target: docs/deployment_governance.md
  - type: relates_to
    target: docs/runbooks/domain-mail-cutover.md
  - type: relates_to
    target: docs/runbooks/weltgewebe-ddns-runtime-verification.md
  - type: relates_to
    target: docs/adr/ADR-0008__domain-mail-provider-boundaries.md
---

# Domain- und Providerarchitektur

## 1. Erreichter Zustand und laufender Cutover

Der Registrar-/DNS-Cutover für `weltgewebe.net` ist abgeschlossen. INWX ist
Registrar der bisher dokumentierten Domains und autoritativer DNS-Provider für
`weltgewebe.net`. Die IONOS-Verträge sind gekündigt.

Die Repository-Konfiguration dieses Changes setzt `commonthing.net` als neue
öffentliche Produktdomain. Das ist zunächst **Zielzustand**, nicht automatisch
Live-Evidence: Bis Merge, Deployment, DNS-Cutover und öffentlichem Readback ist
`weltgewebe.net` weiterhin der belegte produktive Web-Origin. Registrar-,
Nameserver- und Live-DNS-Evidence für `commonthing.net` muss im Cutover separat
erbracht werden. Für den ersten Webcutover ist die operative Reihenfolge nach
dem Merge eindeutig: `commonthing.net`/`www` auf die ausgewählte VPS-Adresse
setzen und autoritativ zurücklesen, danach den exakten Merge-Commit deployen und
öffentlich über HTTPS verifizieren.

Bis zum noch offenen DNS-Cutover verweisen `weltweb.net` und `weltweberei.org`
weiterhin auf alte UI-DNS-/IONOS-Nameserver.

Die IONOS-Kündigung wurde nach menschlicher Freigabe durchgeführt. Ein reproduzierbarer 48-Stunden-Nachweis ist nicht Bestandteil dieses Repository-Artefakts.

## 2. Domainrollen nach abgeschlossenem commonThing-Cutover

Die folgenden Webrollen sind der normative Zielzustand. Vor erfolgreichem
Runtime- und DNS-Readback beschreiben sie **keinen bereits erreichten Livezustand**.


### commonthing.net

- **Public Web:** kanonischer commonThing-App-Origin `https://commonthing.net`.
- **www:** `www.commonthing.net` leitet permanent und URI-erhaltend auf den Apex.
- **Beweisgrenze:** Registrar, autoritative Delegation und Live-A-/AAAA-Records
  sind keine belegten Repository-Fakten und benötigen externe Betriebsbelege.

### weltgewebe.net

- **Registrar:** INWX
- **Autoritative DNS-Verwaltung:** INWX
- **Web/API:** Apex und `www` bleiben Legacy-Webhosts und leiten permanent sowie
  URI-erhaltend auf `https://commonthing.net`; `api.weltgewebe.net` bleibt der
  API-Host. Heimberry besitzt keinen aktiven DNS-Schreibpfad mehr. Die konkrete
  VPS-Adresse ist zu jedem Prüfzeitpunkt aus einer unabhängigen, freigegebenen
  Deployment-Identität zu bestimmen und anschließend gegen autoritative DNS-
  und Runtime-Evidence zu prüfen.

### weltweb.net

- **Registrar:** INWX
- **Autoritative DNS-Verwaltung:** Die Domain ist weiterhin an alte UI-DNS-/IONOS-Nameserver delegiert. INWX ist für diese Domain noch nicht autoritativ. Es existiert noch kein belegter öffentlicher Zielzustand.
- **Ziel:** INWX-Delegation; permanente Weiterleitung auf `https://commonthing.net` (Pfad und Query nach Möglichkeit erhalten); defensive No-Mail-Records; HTTPS-Nachweis.

### weltweberei.org

- **Registrar:** INWX
- **Autoritative DNS-Verwaltung:** Die Domain ist weiterhin an alte UI-DNS-/IONOS-Nameserver delegiert. INWX ist für diese Domain noch nicht autoritativ. Es existiert noch kein belegter öffentlicher Zielzustand.
- **Ziel:** INWX-Delegation; eigenständige Informationsseite; defensive No-Mail-Records; HTTPS-Nachweis. Die frühere WordPress-/IONOS-Fläche ist kein zu erhaltender Zielzustand.

## 3. Mailrollen und commonThing-Cutover

Normativer Zielzustand:

- **mailbox.org:** `kontakt@commonthing.net` als kanonische menschliche Inbound-/Outbound-Adresse.
- **Brevo:** `noreply@login.commonthing.net` als kanonischer technischer Magic-Link-Absender.

Belegter Phase-3-Stand vom 29. August 2026 vor dem **Runtime-Cutover**:

- `kontakt@commonthing.net` ist bei mailbox.org parallel zum Legacy-Alias eingerichtet;
  ein realer Inbound an diese Adresse wurde empfangen.
- `commonthing.net` liefert die mailbox.org-MX-Records, SPF, DKIM-Verweise und DMARC.
  Ein frischer Abschluss-Readback bestätigte die MX-Ziele auf allen drei autoritativen
  INWX-Nameservern und über einen öffentlichen Resolver sowie SPF und DMARC.
- `login.commonthing.net` ist bei Brevo als Senderdomain vorbereitet; Brevos
  Verifikationsrecord und Subdomain-DMARC sind weiterhin autoritativ sichtbar. Ein
  isolierter Versand mit `From: noreply@login.commonthing.net` wurde vom bestehenden
  Brevo-Relay akzeptiert.
<!-- commonthing-naming: legacy -->
- `kontakt@weltgewebe.net` bleibt als menschlicher Legacy-Zustellweg erhalten.
<!-- commonthing-naming: legacy -->
- `noreply@login.weltgewebe.net` bleibt bis zum Runtime-Cutover der aktive
  Produktions-Absender.

Die Provider-/DNS-Seite der commonThing-Mailidentität ist damit vorbereitet und
teilweise bereits real zustellbar; die **Produktionsruntime ist noch nicht auf den
technischen commonThing-Absender umgestellt**. Vor Ready/Merge bleiben deshalb die
noch fehlenden Ende-zu-Ende-Beweise aus
`docs/runbooks/commonthing-mail-identity-cutover.md` bindend: sichtbarer Eingang der
anwendungsseitig erzeugten Magic-Link-Mail sowie menschlicher Outbound von
`kontakt@commonthing.net` an einen unabhängigen kontrollierten Empfänger.

## 4. DNS- und historischer DDNS-Pfad

Der kanonische öffentliche Produktionspfad ist `commonserver` gemäß
`runtime/README.md` und `docs/deploy/vps.md`. Die A-Records für
`commonthing.net`, `www.commonthing.net`, `weltgewebe.net`,
`www.weltgewebe.net` und `api.weltgewebe.net` müssen auf den ausdrücklich
freigegebenen VPS zeigen. Eine wechselnde Heim-WAN-Adresse ist kein zulässiges
Produktionsziel.

### Historischer Implementierungsbesitz

Der dauerhafte Sollvertrag ist im Fremdrepository `heimgewebe/heimserver`
durch Commit `heimgewebe/heimserver@15dfbd6cc1c8899ec030ac6666464db4bc132c71` gebunden. In diesem Commit
verlangen `ops/systemd/weltgewebe-ddns.service` und
`ops/systemd/weltgewebe-ddns.timer` den expliziten Marker
`/etc/weltgewebe-ddns/ENABLE_RETIRED_RUNTIME`; das Installationsskript
`scripts/heimberry/install_weltgewebe_ddns.sh` weist den früheren
`--activate`-Pfad fail-closed ab und bietet nur die kontrollierte Stilllegung
`--retire` an.

Diese Fremdrepo-Referenz belegt die versionierte Implementierung, nicht ihren
fortdauernden Livezustand. Credentials und installierter Runtime-Zustand bleiben
außerhalb dieses Repositories und müssen separat geprüft werden.

Die ehemalige öffentliche Host-Allowlist war exakt:

- `weltgewebe.net`,
- `www.weltgewebe.net`,
- `api.weltgewebe.net`.

Diese historische Begrenzung erteilt keine aktuelle DNS-Ownership. Eine spätere
Reaktivierung erfordert eine neue Architekturentscheidung, eine explizite
Freigabe des DNS-Zielbilds und frische Ende-zu-Ende-Belege.

### Beweisgrenze

Ein vollständiger aktueller Runtime-Nachweis erfordert separat:

1. `runtime/README.md`, `docs/deploy/vps.md` und eine aktuelle Deployment-Identität weisen `commonserver` sowie die unabhängig bestimmte erwartete VPS-Adresse aus,
2. die jeweils autoritativen Nameserver liefern für alle fünf Public-Hosts genau diesen erwarteten VPS-A-Record,
3. öffentliches HTTPS liefert commonThing am neuen Apex, permanente URI-erhaltende Redirects für beide `www`- und beide Legacy-Hosts sowie die API am erhaltenen API-Host,
4. die kanonischen API-Health-Pfade antworten erfolgreich,
5. `weltgewebe-ddns.timer` auf Heimberry ist `disabled` und `inactive`,
6. seit der Stilllegung wurden keine weiteren DDNS-Schreibereignisse des Heimberry-Dienstes protokolliert.

Ein Repository-Test, Merge-Dump oder früherer DDNS-Abnahmebericht ersetzt diese
Livebelege nicht.

### Zeitgebundene Livebeobachtung vom 16. Juli 2026

Zwischen 03:58 und 04:00 Uhr CEST wurde auf Heimberry folgender Zustand gelesen:

- installierte Service-Unit: SHA-256 `b1ecad62a69ce94ff203e2a4e4d0d387f88efe5abed58cf27309fd88f81ea8f8`,
- installierte Timer-Unit: SHA-256 `72da42bdd9aa83e89c5f248f6977a047469c8fe415527994591a6383b5b191c5`,
- beide Units enthalten den Marker-Schutz aus Heimserver-Commit `15dfbd6cc1c8899ec030ac6666464db4bc132c71`,
- `weltgewebe-ddns.timer` ist `disabled` und `inactive`,
- der Aktivierungsmarker fehlt,
- `systemctl --failed` meldet keine fehlgeschlagenen Units,
- seit dem dokumentierten Timer-Stopp am 16. Juli 2026 um 02:40:05 Uhr CEST
  wurden bis 04:00 Uhr keine Ereignisse `dyndns.update_started` oder
  `dyndns.host_updated` mehr gefunden.

Dieser Abschnitt ist ein datierter Beobachtungsbeleg, keine dauerhafte
Wahrheitsgarantie. Für eine spätere Betriebsentscheidung muss die obige
Beweiskette erneut ausgeführt werden.

## 5. Wiederherstellungsgrenze

Ein IONOS-Rollback ist nicht mehr verfügbar. Wiederherstellung erfolgt durch Korrektur der INWX-Zone oder der aktuellen VPS-Runtime. Der Heimberry-DDNS-Pfad ist kein Standardrollback und darf nur nach einer neuen, ausdrücklich freigegebenen Architekturentscheidung reaktiviert werden.

## 6. Offener Restbestand

Die Nebendomains `weltweb.net` und `weltweberei.org` besitzen aktuell keinen belegten DNS-/Web-/No-Mail-Endzustand. Ihre endgültige Delegation an INWX sowie die Einrichtung der Weiterleitungs- bzw. Informationsdienste ist Teil des ausstehenden Task-Restbestands.

## 7. Historische Einordnung

Die frühere IONOS→INWX-Migration ist abgeschlossen. Detaillierte damalige Phasen, Aktivierungsfenster und Rollbackannahmen sind nicht mehr operativ und bleiben ausschließlich über die Git-Historie nachvollziehbar.
