---
id: reports.domain-provider-role-finding
title: "Finding: Aktuelle Domain- und Provider-Rollen"
doc_type: report
status: active
lifecycle: audit
lifecycle_state: active
owner_task: DEPLOY-DNS-001
review_after: 2026-07-23
summary: >
  Redigierter Statusbericht zu Registrar, DNS, E-Mail und Web für
  weltgewebe.net, weltweb.net und weltweberei.org.
relations:
  - type: relates_to
    target: docs/deploy/domain-mail-migration-ionos-to-inwx-mailbox-brevo.md
  - type: relates_to
    target: docs/runbooks/domain-mail-cutover.md
  - type: relates_to
    target: docs/tasks/board.md
---

# Finding: Aktuelle Domain- und Provider-Rollen

> Dieser Bericht erfasst den operativen Endzustand für die Hauptdomain sowie den offenen Restbestand für die Nebendomains.

## Provider-Basis

- **INWX**: Registrar aller drei Domains (`weltgewebe.net`, `weltweb.net`, `weltweberei.org`).
- **INWX**: DNS-Autorität derzeit nur belegt für `weltgewebe.net`.
- Die beiden Nebendomains sind weiterhin an alte UI-DNS-/IONOS-Nameserver delegiert. INWX ist für diese Domains noch nicht autoritativ.

## Domain-Status

### weltgewebe.net

- **Web/API**: Betriebsfähig über den kanonischen Public-VPS-Pfad `commonserver`. Der frühere Heimberry-DDNS-Schreibpfad ist stillgelegt.
- **Mail (Human)**: mailbox.org. (Betriebsfähig).
- **Mail (Technical Login)**: Brevo (`noreply@login.weltgewebe.net`). (Betriebsfähig).

### weltweb.net

- **Status**: Die Domain ist weiterhin an alte UI-DNS-/IONOS-Nameserver delegiert. INWX ist für diese Domain noch nicht autoritativ. Es existiert noch kein belegter öffentlicher Zielzustand (aktuell nicht abgeschlossener DNS-/Web-/No-Mail-Zustand).
- **Zielrolle**: INWX-Delegation, permanente Weiterleitung auf `https://weltgewebe.net` (Pfad und Query nach Möglichkeit erhalten), defensive No-Mail-Records, HTTPS-Nachweis.

### weltweberei.org

- **Status**: Die Domain ist weiterhin an alte UI-DNS-/IONOS-Nameserver delegiert. INWX ist für diese Domain noch nicht autoritativ. Es existiert noch kein belegter öffentlicher Zielzustand (aktuell nicht abgeschlossener DNS-/Web-/No-Mail-Zustand).
- **Zielrolle**: INWX-Delegation, eigenständige Informationsseite, defensive No-Mail-Records, HTTPS-Nachweis. (Die frühere WordPress-/IONOS-Fläche ist kein zu erhaltender Zielzustand).

## Review-Frist

Dieser Report trägt ein `review_after`-Datum als reinen Review-Backstop, um die Nachverfolgung sicherzustellen. Dies ist kein voraussichtliches Fertigstellungsdatum oder Betriebsbeleg. Nach Abschluss der Nebendomains wird der Report vor dem Backstop-Datum erneut geprüft.

## Sicherheitsvermerk

Dieser Bericht enthält absichtlich:

- keine Secrets;
- keine Auth- oder Transfercodes;
- keine privaten Vertragsdaten;
- keine privaten Provider-Rohdaten;
- keine sensiblen internen Routingdetails;
- keine dynamische WAN-IP als Vertragswert.
