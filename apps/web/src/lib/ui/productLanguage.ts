export const NODE_KIND_OPTIONS = [
  { id: "place", label: "Ort", description: "Räumlicher Bezugspunkt" },
  {
    id: "resource",
    label: "Ressource",
    description: "Nutzbares oder teilbares Gut",
  },
  {
    id: "project",
    label: "Projekt",
    description: "Vorhaben mit einem gemeinsamen Ziel",
  },
  {
    id: "event",
    label: "Ereignis",
    description: "Zeitlich bestimmtes Geschehen",
  },
  {
    id: "need",
    label: "Bedarf",
    description: "Gesuchte Ressource oder Unterstützung",
  },
  {
    id: "offer",
    label: "Angebot",
    description: "Angebotene Leistung oder Unterstützung",
  },
] as const;

export type NodeKindId = (typeof NODE_KIND_OPTIONS)[number]["id"];

export function isNodeKind(value: string): value is NodeKindId {
  return NODE_KIND_OPTIONS.some((option) => option.id === value);
}

export function nodeKindLabel(kind?: string | null): string {
  return (
    NODE_KIND_OPTIONS.find((option) => option.id === kind)?.label ??
    "Ungültige Knotenart"
  );
}
