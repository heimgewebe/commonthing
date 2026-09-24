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
| Zielarchitektur | `commonThing OS` (`architecture/commonthing-os.md`) | `Weltgewebe OS` nur in historischen Identitäten (siehe 4.1); `architecture/weltgewebe-os.md` nur als Verweis auf den neuen Pfad |
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
| interne Domains | `*.commonthing.home.arpa` als reservierter Namespace; derzeit kein aktiver Service-Layer | `*.weltgewebe.home.arpa` nur historisch/Legacy, ohne aktive Produkt-DNS-Zuordnung |
| Schema-`$id` | noch nicht festgelegt; wird mit der ersten neuen Contract-Version bestimmt | `https://weltgewebe.org/...` und `https://weltgewebe.net/contracts/...` bleiben für bestehende Contract-Versionen stabil |

Die Tabelle beschreibt den Zielzustand. Ein Zielname darf nicht als bereits live
behauptet werden, solange DNS, Runtime oder Providerzustand noch nicht entsprechend
zurückgelesen wurden.

## 3. Migrationsregel

Jede technische Identitätsmigration folgt derselben Reihenfolge:

> **add new -> verify -> switch canonical -> observe -> remove old**

Der alte Name wird nie zuerst gelöscht. Für jede Migration müssen vor dem Abbau
des Legacy-Namens mindestens die neue Identität, der tatsächliche Consumer-Pfad
und ein Readback des Zielzustands belegt sein.

Diese Reihenfolge erzwingt keinen Scheindienst. Wird ein bisheriger Zielhost
vollständig stillgelegt und existiert kein aktueller Consumer-Pfad bzw. kein
zugewiesener Service-Layer, werden tote DNS-Aliase entfernt statt auf einen
unbelegten Ersatzhost umgebogen. Eine spätere Reaktivierung beginnt wieder mit
`add new` und braucht neue Runtime-Evidenz.

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

### 4.1 Dauerhafte Identitäten

Diese Namen tragen Historie, Datenintegrität oder Protokollidentität. Sie werden
**nicht** umbenannt. Eine Ablösung ist nur über eine neue Version mit eigener,
kompatibler Migration zulässig (neue Migration, neue Contract-Version, neuer
Consumer), nie durch Umschreiben des Bestands:

- Task- und Serien-IDs wie `WELTGEWEBE-OS-V1-T…`, `WELTGEWEBE-OS-0…` und
  `WELTGEWEBE-SEMANTIC-SEARCH-V1-T…` sowie die Proofs, Reports und Receipts, die
  sie tragen;
- Objekte in bereits angewendeten Datenbankmigrationen (z. B. SQL-Funktionen
  `weltgewebe_*`), weil angewendete Migrationen nicht nachträglich geändert werden;
- Hash-Seeds und Lock-Namespaces (z. B. `weltgewebe:node-conversation:v1:`,
  `weltgewebe:node-mutation:v1`), weil sie deterministische IDs und Sperren
  erzeugen;
- NATS-Subjects und Durable-Consumer-Namen (`weltgewebe.domain.>`,
  `weltgewebe-api-domain-receipts-v1`), weil ihr Zustand im Stream liegt;
- Browser-Speicherschlüssel (`weltgewebe.theme`, `weltgewebe:garnrolle-*`), solange
  kein Migrationscode Nutzerdaten überträgt;
- Such-Revisionen (`weltgewebe-search-normalization-v1`,
  `weltgewebe-hybrid-ranking-v2`), die an Receipts und Contracts gebunden sind;
- Schema-`$id`s bestehender Contract-Versionen;
- Dokument-IDs im Frontmatter (`id:`), z. B. `architecture.weltgewebe-os` für
  `architecture/commonthing-os.md`; Pfad und Titel dürfen sich ändern, die ID
  bleibt als stabiler Verweisschlüssel;
- Dateien, deren Inhalt per Hash in einem eingecheckten Receipt gebunden ist
  (z. B. `architecture/semantic-search.md` im T004-Ranking-Receipt); ihr Text
  ändert sich erst mit einem neu erzeugten Receipt.

Treffer dieser Klasse gelten als klassifiziert; sie brauchen keinen
Zeilenmarker `commonthing-naming: legacy`.

### 4.2 Zielarchitektur

Die kanonische Zielarchitektur heißt **commonThing OS** und steht in
`architecture/commonthing-os.md`. Das ist eine Namensentscheidung, keine
Architekturänderung. `Weltgewebe OS` bleibt nur dort bestehen, wo der Name selbst
historische Identität trägt (4.1). Aktive Dokumente und UI-Texte verwenden
`commonThing OS`. Bestehende ID-Serien wie `WELTGEWEBE-OS-V1-T…` laufen
unverändert weiter, damit Belege und Querverweise stabil bleiben; Titel und
Beschreibungen neuer Tasks verwenden den neuen Namen.

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

Der DNS-Schritt vom 20. September 2026 hatte `commonthing.home.arpa`,
`api.commonthing.home.arpa` sowie die beiden Legacy-Namen
`weltgewebe.home.arpa` und `api.weltgewebe.home.arpa` noch auf den früheren
Heimserver-Edge gelegt. Der frische Infrastruktur- und Runtime-Readback am
21. September 2026 hat diese Annahme verworfen: Der Heimserver ist ausdrücklich
außer Betrieb, die kanonische Infrastruktur weist derzeit keinen
`service-layer`-Host zu, und die kanonische Produktion läuft auf
`commonserver` unter `https://commonthing.net`. Die vier Produkt-DNS-Einträge
auf den stillgelegten Heimserver wurden deshalb aus Pi-hole entfernt.

`*.commonthing.home.arpa` bleibt als interner Namespace reserviert, ist aber
derzeit **nicht aktiv**. Eine spätere Aktivierung setzt zuerst eine explizite
Service-Layer-Zuweisung in der kanonischen Infrastruktur und danach frische
DNS-, TLS- und Runtime-Evidenz voraus. Der Namespace darf nicht aus Bequemlichkeit
auf `heim-pc` oder den öffentlichen VPS umgebogen werden. Die
`*.weltgewebe.home.arpa`-Namen sind nur noch historische/Legacy-Referenzen und
haben keine aktive Produkt-DNS-Zuordnung. Der explizite
`DEPLOY_TARGET=heimserver`-Pfad bleibt ein Legacy-Vertrag und ist kein aktueller
Produktionspfad.

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
   klassifiziert sind, entweder über eine Klasse aus 4.1 oder über den
   Zeilenmarker aus Abschnitt 6;
4. Legacy-Fallbacks nur dort bestehen, wo ihr Nutzen bewusst belegt ist;
5. der Naming Guard verhindert, dass der alte Name wieder als aktueller Name
   zurückkehrt.
