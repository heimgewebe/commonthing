---
id: runbooks.weltgewebe-ddns-runtime-verification
title: Weltgewebe Heimberry-DDNS-Stilllegung verifizieren
doc_type: runbook
status: active
summary: Aktive, wiederholbare Sicherheitsprüfung des stillgelegten Heimberry-DDNS-Schreibpfads.
relations:
  - type: relates_to
    target: docs/runbooks/README.md
  - type: relates_to
    target: docs/deploy/domain-mail-migration-ionos-to-inwx-mailbox-brevo.md
  - type: relates_to
    target: docs/deploy/vps.md
  - type: relates_to
    target: docs/runbooks/incident-response.md
---

# Weltgewebe Heimberry-DDNS-Stilllegung verifizieren

## Zweck

Der DDNS-Dienst ist stillgelegt; dieses Runbook bleibt aktiv. Es prüft, dass der
frühere Heimberry-Schreibpfad weiterhin inaktiv und fail-closed ist, während die
öffentliche Weltgewebe-Runtime auf `commonserver` läuft.

Die versionierte Härtung ist an Commit
`heimgewebe/heimserver@15dfbd6cc1c8899ec030ac6666464db4bc132c71`
gebunden. Maßgeblich sind dort die beiden Systemd-Units unter `ops/systemd/`
sowie `scripts/heimberry/install_weltgewebe_ddns.sh`. Diese Referenz belegt den
Sollvertrag, nicht den fortdauernden Livezustand.

## Beweisquellen

1. `runtime/README.md` und `docs/deploy/vps.md` weisen `commonserver` als
   kanonischen Produktionspfad aus.
2. Die erwartete VPS-Adresse stammt aus einer aktuellen, freigegebenen
   Deployment-Identität oder einem Deployment-Receipt. Sie darf nicht aus den
   DNS-Antworten abgeleitet werden, die gerade geprüft werden.
3. `weltgewebe-ddns.timer` auf Heimberry ist `disabled` und `inactive`.
4. Service und Timer enthalten den Schutz
   `ConditionPathExists=/etc/weltgewebe-ddns/ENABLE_RETIRED_RUNTIME`.
5. Der Aktivierungsmarker fehlt.
6. Die delegierten Nameserver entsprechen exakt den drei erwarteten
   INWX-Nameservern. Diese liefern für Apex, `www` und `api` jeweils genau den
   unabhängig bestimmten VPS-A-Record.
7. Apex, `www` und die kanonischen API-Health-Pfade antworten ohne TLS- oder
   HTTP-Fehler.
8. Seit dem Stilllegungszeitpunkt wurden keine neuen DDNS-Schreibereignisse des
   Heimberry-Dienstes protokolliert.
9. Credential-Werte werden weder gelesen noch in Belege übernommen.

## Read-only-Prüfung

`EXPECTED_VPS_A` muss aus einer unabhängigen Deployment-Quelle stammen.
`RETIRED_SINCE` ist der belegte Stilllegungszeitpunkt mit Zeitzone.

```bash
set -euo pipefail

: "${EXPECTED_VPS_A:?set from the approved commonserver deployment identity}"
: "${RETIRED_SINCE:?set to the evidenced retirement timestamp}"

expected_nameservers=(ns.inwx.de ns2.inwx.de ns3.inwx.eu)
hosts=(weltgewebe.net www.weltgewebe.net api.weltgewebe.net)
urls=(
  https://weltgewebe.net/
  https://www.weltgewebe.net/
  https://weltgewebe.net/api/health/live
  https://api.weltgewebe.net/health/live
)

mapfile -t delegated_nameservers < <(
  dig +short NS weltgewebe.net | sed 's/\.$//' | sed '/^$/d' | sort -u
)
mapfile -t sorted_expected_nameservers < <(
  printf '%s\n' "${expected_nameservers[@]}" | sort -u
)
[[ "${delegated_nameservers[*]}" == "${sorted_expected_nameservers[*]}" ]]

state=$(systemctl is-enabled weltgewebe-ddns.timer 2>/dev/null || true)
[[ "$state" == disabled ]]
state=$(systemctl is-active weltgewebe-ddns.timer 2>/dev/null || true)
[[ "$state" == inactive ]]

for unit in weltgewebe-ddns.service weltgewebe-ddns.timer; do
  systemctl cat "$unit" |
    grep -Fqx 'ConditionPathExists=/etc/weltgewebe-ddns/ENABLE_RETIRED_RUNTIME'
done
sudo test ! -e /etc/weltgewebe-ddns/ENABLE_RETIRED_RUNTIME

for nameserver in "${expected_nameservers[@]}"; do
  for host in "${hosts[@]}"; do
    mapfile -t answers < <(
      dig +short @"$nameserver" "$host" A | sed '/^$/d' | sort -u
    )
    [[ ${#answers[@]} -eq 1 ]]
    [[ "${answers[0]}" == "$EXPECTED_VPS_A" ]]
  done
done

for url in "${urls[@]}"; do
  curl --fail --silent --show-error --location --output /dev/null "$url"
done

journal_args=(
  -u weltgewebe-ddns.service
  --since "$RETIRED_SINCE"
  --grep 'dyndns.update_started|dyndns.host_updated'
  --no-pager
  --output cat
)
if journalctl "${journal_args[@]}" | grep -q .; then
  echo 'unexpected DDNS write event after retirement' >&2
  exit 1
fi
```

Die Prüfung schreibt weder DNS noch Systemd-Zustand. Ein Fehler beendet sie
fail-closed. Der Abschlussbeleg enthält die eingesetzten Erwartungswerte, den
Zeitstempel, die delegierten Nameserver, die neun autoritativen DNS-Antworten,
die vier HTTPS-Prüfungen und die Unit-Zustände, aber keine Credentials.

## Letzter datierter Beobachtungsbeleg

Am 16. Juli 2026 zwischen 03:58 und 04:00 Uhr CEST wurden die installierten
Unit-Hashes `b1ecad62…a8f8` und `72da42bd…1c5`, der fehlende Marker, der Zustand
`disabled`/`inactive`, null fehlgeschlagene Units und keine neuen, durch den
Heimberry-Dienst protokollierten Schreibereignisse seit 02:40:05 Uhr CEST
beobachtet. Die vollständigen Hashes stehen in
`docs/deploy/domain-mail-migration-ionos-to-inwx-mailbox-brevo.md`.

Dieser Beleg altert und ersetzt keine frische Ausführung der Prüfung.

## Fail-closed Kriterien

Die Prüfung scheitert bei aktivem oder aktiviertem Timer, vorhandenem Marker,
fehlendem Unit-Schutz, unerwarteten Nameservern oder A-Records, TLS-/HTTP-Fehlern
oder neuen DDNS-Schreibereignissen. Ein fehlender Beleg gilt nicht als Erfolg.

## Wiederherstellung

Die Standardmaßnahme ist die Korrektur der INWX-Zone oder der VPS-Runtime. Eine
Reaktivierung des Heimberry-DDNS-Pfads ist kein operativer Rollback. Sie verlangt
eine neue Architekturentscheidung, explizite DNS-Freigabe und einen separaten,
reviewten Patch. Providerdateien werden bei der Stilllegung nicht gelöscht.
