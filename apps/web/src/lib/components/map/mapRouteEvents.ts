import type { MapEntityViewModel, Node } from "$lib/map/types";

export type RelatedMapSelection = {
  type: "node" | "garnrolle";
  id: string;
  title?: string;
  data?: MapEntityViewModel;
};

export type MapDomainChanged =
  | {
      kind: "node";
      id: string;
      action: "updated";
      node: Node;
    }
  | {
      kind: "node";
      id: string;
      action: "deleted" | "archived" | "visibility_changed";
    };

export type MapDomainChangeResolution =
  | { kind: "local-node-update"; node: Node }
  | { kind: "reload-domain-data" };

export function resolveMapDomainChange(
  change: MapDomainChanged,
): MapDomainChangeResolution {
  if (change.action === "updated" && change.node.id === change.id) {
    return { kind: "local-node-update", node: change.node };
  }
  return { kind: "reload-domain-data" };
}
