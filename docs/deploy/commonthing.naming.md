---
id: deploy.commonthing.naming
title: commonThing Naming Policy
doc_type: reference
status: active
summary: Kanonische Namens- und Legacy-Regeln für commonThing.
relations:
  - type: relates_to
    target: docs/deploy/README.md
  - type: supersedes
    target: docs/deploy/weltgewebe.naming.md
---
# commonThing Naming Policy

## 1. Grundsatz

`commonThing` ist der kanonische Name des lebenden Produkts und aller neu
eingeführten öffentlichen Identitäten. `Weltgewebe` ist kein zweiter aktueller
Produktname mehr.

Die zulässige Restrolle von `Weltgewebe` ist auf zwei Klassen begrenzt:

1. **Historie**: alte Commits, abgeschlossene Proofs, archivierte Reports,
   Migrationsnamen und andere unveränderliche Belege dürfen ihren damaligen Namen
   behalten.
2. **Legacy-Kompatibilität**: bestehende technische Namen dürfen während einer
   kontrollierten Migration vorübergehend weiter funktionieren, wenn der neue
   commonThing-Name bereits als Ziel bzw. kanonischer Name festgelegt ist.

Neue Produkt-, Architektur- oder Betriebsbegriffe werden nicht mehr unter dem
Namen Weltgewebe eingeführt.

## 2. Kanonische Zielnamen

| Bereich | Kanonisch | Legacy / Übergang |
| --- | --- | --- |
| Produkt | `commonThing` | `Weltgewebe` nur historisch |
| Hauptdomain | `commonthing.net` | `weltgewebe.net` als permanenter Redirect |
| www | `www.commonthing.net` -> `commonthing.net` | `www.weltgewebe.net` als permanenter Redirect |
| API | `api.commonthing.net` | `api.weltgewebe.net` als zeitlich begrenzter Kompatibilitätsname |
| Kontakt | `kontakt@commonthing.net` | `kontakt@weltgewebe.net` als Alias während/nach der Migration |
| technische Mail | `noreply@login.commonthing.net` | `noreply@login.weltgewebe.net` als Legacy-Absender/Alias während der Migration |
| API-Binary | `commonthing-api` | `weltgewebe-api` nur als Migrationskompatibilität |
| Env-Präfix | `COMMONTHING_*` | `WELTGEWEBE_*` nur als gemessener Legacy-Fallback |
| Build-Header | `X-CommonThing-*` | `X-Weltgewebe-*` nur als Übergangsheader |
| Repository | `heimgewebe/commonthing` | `heimgewebe/weltgewebe` nur als GitHub-Redirect und historische Referenz |
| Betriebsnamen | `commonthing-*` | `weltgewebe-*` bis zur jeweiligen Unit-/Pfad-Migration |
| interne Domains | `*.commonthing.home.arpa` | `*.weltgewebe.home.arpa` bis zur kontrollierten Heimnetz-Migration |

Die Tabelle beschreibt den Zielzustand. Ein Zielname darf nicht als bereits live
behauptet werden, solange DNS, Runtime oder Providerzustand noch nicht entsprechend
zurückgelesen wurden.

## 3. Migrationsregel

Jede technische Identitätsmigration folgt derselben Reihenfolge:

> **add new -> verify -> switch canonical -> observe -> remove old**

Der alte Name wird nie zuerst gelöscht. Für jede Migration müssen vor dem Abbau
des Legacy-Namens mindestens die neue Identität, der tatsächliche Consumer-Pfad
und ein Readback des Zielzustands belegt sein.

## 4. Was `Weltgewebe` noch heißen darf

Zulässig sind insbesondere:

- historische Dokumenttitel und abgeschlossene Proof-Artefakte;
- alte Datenbankmigrationen und unveränderliche Persistenzbezeichner, wenn eine
  Umbenennung die Historie oder Datenintegrität gefährden würde;
- Legacy-Domains und -Mailadressen, solange sie ausdrücklich als Redirect, Alias
  oder Kompatibilitätsvertrag geführt werden;
- vorübergehende technische Namen wie `WELTGEWEBE_*`, alte Header, Binary-,
  systemd-, Docker- oder Pfadnamen, solange ihre Migration noch nicht terminal
  abgeschlossen ist.

Nicht zulässig sind neue Verwendungen als:

- Produkt- oder Markenname;
- aktuelle UI-Bezeichnung;
- neue Architekturbezeichnung;
- neue kanonische Domain, Mailadresse oder API-Identität;
- neuer Service-, Binary-, Variablen- oder Betriebsname.

## 5. Aktueller Übergangszustand

Seit PR #1803 ist `https://commonthing.net` im Repository der kanonische
öffentliche Web-Origin. `weltgewebe.net` und `www.weltgewebe.net` sind als
permanente Legacy-Redirects vorgesehen. Der tatsächliche Livezustand bleibt
separat über DNS-, TLS-, Deployment- und Runtime-Readback zu belegen.

`api.weltgewebe.net`, `kontakt@weltgewebe.net`,
`noreply@login.weltgewebe.net`, `WELTGEWEBE_*`, `weltgewebe-api` und weitere
Betriebsidentitäten sind ausdrücklich **Übergangsverträge**, nicht der gewünschte
Endzustand.

Seit dem kontrollierten Repository-Rename am 1. September 2026 ist `heimgewebe/commonthing` die kanonische GitHub-Repository-Identität. `heimgewebe/weltgewebe` bleibt ausschließlich als GitHub-Redirect und historische Referenz erhalten; der alte Slug darf nicht für ein neues Repository wiederverwendet werden.

Für das Heimnetz ist `commonthing.home.arpa` seit dem kontrollierten DNS-Schritt vom 20. September 2026 der kanonische Root-Name. Pi-hole löst `commonthing.home.arpa` und den vorbereiteten optionalen Namen `api.commonthing.home.arpa` auf denselben bisherigen Edge-Zielhost auf. Der verpflichtende Heimserver-Deployvertrag bleibt jedoch bewusst beim belegten Root-Pfad `https://commonthing.home.arpa`; API-Readiness wird über `/api/health/ready` auf diesem Host geprüft. Der separate `api.commonthing.home.arpa`-Endpunkt wird erst nach eigenem Edge-/TLS-Readback verpflichtend. Die alten `*.weltgewebe.home.arpa`-Namen bleiben während der Observe-Phase als Legacy-Kompatibilität erhalten; ihr Abbau ist noch kein abgeschlossener Zustand.

## 6. CI-Regel

Der commonThing Naming Guard verhindert neue unmarkierte Produktverwendungen von
`Weltgewebe` und neue unmarkierte Verwendung der Legacy-Webdomain als kanonische
URL in geänderten Zeilen.

Eine technisch notwendige Legacy-Erwähnung muss in derselben Zeile mit
`commonthing-naming: legacy` gekennzeichnet sein oder in einem ausdrücklich vom
Guard ausgenommenen Naming-Policy- oder abgeleiteten Dokumentationspfad liegen.
Die Kennzeichnung ist kein dauerhafter Freibrief; sie macht verbleibende
Kompatibilität lediglich maschinenlesbar und auffindbar.

## 7. Abschlusskriterium

Die Umbenennung ist vollständig, wenn:

1. alle öffentlichen kanonischen Identitäten commonThing verwenden;
2. neue technische Identitäten commonThing verwenden;
3. alle verbleibenden Weltgewebe-Treffer als Historie oder Legacy-Kompatibilität
   klassifiziert sind;
4. Legacy-Fallbacks nur dort bestehen, wo ihr Nutzen bewusst belegt ist;
5. der Naming Guard verhindert, dass der alte Name wieder als aktueller Name
   zurückkehrt.
