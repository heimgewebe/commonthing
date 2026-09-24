---
id: adr.ADR-0009__garnrolle-verortung-sichtbarkeit
title: ADR-0009 — Garnrolle, Verortung und Sichtbarkeit
doc_type: reference
status: active
summary: >
  Entscheidung zum Zielmodell: Jeder Account hat eine Garnrolle; privater
  Kartenanker und öffentliche Sichtbarkeit sind getrennte Eigenschaften dieser
  Garnrolle, keine Identitäts- oder Onboardingmodi.
relations:
  - type: supersedes
    target: docs/adr/ADR-0003__privacy-ungenauigkeitsradius-ron.md
  - type: relates_to
    target: docs/specs/garnrolle-knoten-faden.md
  - type: relates_to
    target: docs/specs/privacy-api.md
  - type: relates_to
    target: docs/specs/ui-interaction.md
---

# ADR-0009 — Garnrolle, Verortung und Sichtbarkeit

Datum: 2026-07-09
Aktualisiert: 2026-07-24
Status: Accepted

## Entscheidung

commonThing verwendet künftig ein einziges Accountmodell:

> Jeder Account hat genau eine Garnrolle.

Die Garnrolle ist die sichtbare und handelnde Rolle eines Menschen oder Akteurs
im Gewebe. Sie ist der Ursprung von Fäden und kann Knoten knüpfen.

Verortung und öffentliche Sichtbarkeit sind Eigenschaften dieser Garnrolle. Sie
sind kein Identitätsmodus und keine zweite Art von Account.

Ein privater Kartenanker und seine öffentliche Darstellung sind zwei getrennte
Entscheidungen:

1. Eine Garnrolle kann keine oder eine privat gespeicherte Koordinate besitzen.
2. Unabhängig davon kann sie privat bleiben, exakt oder ungefähr öffentlich
   dargestellt werden.

## Zielmodell

Eine Garnrolle hat einen Profilkern und optional einen privaten Kartenanker. Die
öffentliche Kartenposition ist eine daraus abgeleitete, ausdrücklich gewählte
Darstellung.

```text
Garnrolle
├─ Profil
│  ├─ Anzeigename
│  ├─ Beschreibung
│  ├─ Fähigkeiten
│  ├─ Güter
│  └─ Interessen
└─ Kartenbezug
   ├─ privater Kartenanker
   │  ├─ keiner
   │  └─ Koordinate
   ├─ private Adresse oder Ortsnotiz
   │  ├─ keine
   │  └─ Freitext
   └─ öffentliche Darstellung
      ├─ privat: keine öffentliche Position
      ├─ exakt sichtbar
      └─ im Umkreis sichtbar
```

Die Adresse oder Ortsnotiz ist optional, bleibt privat und wird nicht
automatisch in eine Koordinate umgewandelt. Eine öffentliche Darstellung setzt
einen vorhandenen privaten Kartenanker voraus, aber keine Adresse.

Die zentrale Nutzerhandlung lautet:

> Privaten Kartenanker wählen und öffentliche Sichtbarkeit bestimmen.

Nicht:

> Identitätsmodus wechseln.

## Sichtbarkeit

Sichtbarkeit ist in commonThing ein positiver Wert. Ein genauer öffentlicher Ort
ist kein Fehlerfall, sondern kann selbst ein Gemeingut sein: Menschen,
Initiativen und Orte werden auffindbar, ansprechbar und anschlussfähig.

Die Nutzerführung kennt drei einfache Entscheidungen:

| Zustand | Bedeutung |
|---|---|
| Privat | Die Garnrolle hat keine öffentliche Kartenposition. Ein privater Kartenanker kann trotzdem gespeichert sein. |
| Öffentlich exakt | Der private Kartenanker wird öffentlich exakt angezeigt. |
| Öffentlich ungefähr | Eine versetzte Position innerhalb des gewählten Umkreises wird öffentlich angezeigt. |

Die exakte Sichtbarkeit ist der einfache öffentliche Fall. Die ungefähre
Sichtbarkeit ist eine bewusste Option, nicht der normative Default.

Der technische Zustand `map_state=not_on_map` bedeutet ausschließlich, dass
keine öffentliche Kartenposition projiziert wird. Er sagt nicht aus, ob ein
privater Kartenanker vorhanden ist, ob ein Account neu ist oder ob Onboarding
abgeschlossen wurde. Nutzerführung darf aus diesem Zustand daher keinen
Erstnutzerstatus ableiten.

Ein späterer Onboarding-Abschlusszustand muss als eigenes, datensparsames Signal
modelliert werden. Er darf nicht aus Sichtbarkeit, Rolle oder vorhandener
Koordinate erraten werden.

## Knoten und Fäden

Eine Garnrolle knüpft Knoten.

Ein Knoten ist ein verortetes oder thematisch fassbares Bündel im Gewebe: Ort,
Projekt, Bedarf, Angebot, Werkzeug, Ereignis, Gruppe oder Commons.

Beim Knüpfen eines Knotens entsteht mindestens ein Faden. Ein Faden beschreibt,
was zwischen Garnrolle und Knoten gilt, zum Beispiel:

- gebaut von
- betreut von
- vorgeschlagen von
- nutzt
- braucht
- bietet an
- gehört zu

Die Nutzerführung sagt **Faden** und **Fadenart**. Technische Implementierungen
dürfen intern weiter `edge` oder `edge_kind` verwenden, solange die Produkt- und
Dokumentationssprache konsistent bleibt.

## Beispiel: erstes organisches Weben

1. Alexander legt seinen Account an.
2. Seine Garnrolle entsteht.
3. Er ergänzt Profilinformationen.
4. Er wählt den Poelsweg 2 als privaten Kartenanker.
5. Er entscheidet, den Punkt exakt öffentlich zu zeigen.
6. Er knüpft den Knoten "Fairschenkbox Caspar-Voght-Straße" an der exakten
   Position der Box.
7. Das System erzeugt einen Gestaltungsfaden:
   "Alexander hat die Fairschenkbox gebaut / betreut sie".

Damit entsteht kein Demo-Datensatz von außen, sondern der erste echte
commonThing-Akt aus einer Garnrolle heraus.

## API-Zielrichtung

Das Zielmodell trennt private Daten und öffentliche Projektion ausdrücklich:

```text
garnrolle.private_location = null | {
  coordinates
}

garnrolle.private_address_note = null | string

garnrolle.map_state = "not_on_map" | "exact" | "radius"
garnrolle.radius_m = 0 | 50..5000
```

Dabei gilt:

- `private_location=null` bedeutet: Es existiert kein Kartenanker.
- `map_state=not_on_map` bedeutet: Es existiert keine öffentliche Projektion;
  `private_location` kann dennoch gesetzt sein.
- `map_state=exact` veröffentlicht den privaten Kartenanker exakt.
- `map_state=radius` veröffentlicht eine stabile, versetzte Projektion innerhalb
  des gewählten Radius.
- Nicht übermittelte private Felder bleiben erhalten.
- Explizite Löschsignale entfernen private Adresse oder privaten Kartenanker.
- Eine öffentliche Sichtbarkeit ohne vorhandenen Kartenanker wird fail-closed
  abgewiesen.

Die aktuelle Persistenz darf diese Semantik mit getrennten Spalten, privaten
Payload-Feldern und `map_state` ausdrücken. Entscheidend ist die fachliche
Trennung; die konkrete weitere Migration ist nicht Bestandteil dieser ADR.

## Konsequenzen

- Onboarding beginnt mit "Deine Garnrolle", wird aber nicht aus `map_state`
  abgeleitet.
- Die Nutzerführung trennt Beschreiben, privaten Kartenanker und öffentliche
  Sichtbarkeit.
- Sichtbarkeit wird als positive, verständliche Auswahl geführt.
- Eine private Adresse ist optional; ein exakter öffentlicher Punkt ist ein
  regulärer Fall.
- Bewusst private Garnrollen werden nicht als unfertig oder neu bezeichnet.
- Das erste echte Produktziel ist organisches Weben aus der eigenen Garnrolle.
- UI, Konzepte und Roadmaps verwenden **Fäden** statt **Kanten/Edges**.
- Alte technische Begriffe bleiben nur als Migrations- oder
  Implementierungsdetail zulässig.

## Nicht-Ziele

- Kein Trust-Score.
- Keine Pflichtverifikation als Einstiegshürde.
- Kein anonymer Alternativaccount neben der Garnrolle.
- Kein Privacy-Modus als Ersatz für klare Sichtbarkeit.
- Kein aus Sichtbarkeit oder Rolle erratener Onboardingstatus.
- Keine seedartige Inhaltseinspielung als Ersatz für organisches Weben.

## Abgelöste Entscheidung

Diese ADR löst ADR-0003 als Zielmodell ab. ADR-0003 bleibt nur als historischer
Migrationskontext erhalten. Neue Produkt- und Konzeptarbeit folgt dieser ADR.
