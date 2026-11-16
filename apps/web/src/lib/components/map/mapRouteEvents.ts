import type { MapEntityViewModel, Node } from "$lib/map/types";
import type { NodeDeleteReceipt } from "$lib/api/nodeDelete";

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
      receipt?: NodeDeleteReceipt;
    };

export type MapDomainChangeResolution =
  | { kind: "local-node-update"; node: Node }
  | { kind: "local-node-removal"; id: string }
  | { kind: "reload-domain-data" };

export function resolveMapDomainChange(
  change: MapDomainChanged,
): MapDomainChangeResolution {
  if (change.action === "updated" && change.node.id === change.id) {
    return { kind: "local-node-update", node: change.node };
  }
  // The DELETE response is canonical and already validates the cascade IDs.
  // Older/unknown events keep the authoritative reload path.
  if (
    (change.action === "deleted" || change.action === "archived") &&
    change.receipt?.node_id === change.id
  ) {
    return { kind: "local-node-removal", id: change.id };
  }
  return { kind: "reload-domain-data" };
}
