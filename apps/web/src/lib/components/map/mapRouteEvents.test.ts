import { describe, expect, it } from "vitest";
import { resolveMapDomainChange } from "./mapRouteEvents";
import type { Node } from "$lib/map/types";

const node: Node = {
  id: "node-1",
  kind: "Knoten",
  title: "Aktueller Knoten",
  created_at: "2026-08-15T07:00:00.000Z",
  updated_at: "2026-08-15T07:30:00.000Z",
  tags: [],
  location: { lat: 53.5, lon: 10 },
};

describe("resolveMapDomainChange", () => {
  it("keeps a canonical successful update on the local map path", () => {
    expect(
      resolveMapDomainChange({
        kind: "node",
        id: node.id,
        action: "updated",
        node,
      }),
    ).toEqual({ kind: "local-node-update", node });
  });

  it("fails closed to a reload when the update payload identity mismatches", () => {
    expect(
      resolveMapDomainChange({
        kind: "node",
        id: "node-2",
        action: "updated",
        node,
      }),
    ).toEqual({ kind: "reload-domain-data" });
  });

  it.each(["deleted", "archived"] as const)(
    "uses the canonical cascade receipt for a local %s",
    (action) => {
      const receipt = {
        node_id: node.id,
        node_state: "removed" as const,
        removed_edge_ids: ["edge-1", "edge-2"],
        conversation:
          action === "archived"
            ? {
                effect: "archived" as const,
                archive_id: "archive-1",
                archive_url: "/api/conversations/archive-1",
              }
            : { effect: "deleted_empty" as const },
      };
      expect(
        resolveMapDomainChange({ kind: "node", id: node.id, action, receipt }),
      ).toEqual({ kind: "local-node-removal", id: node.id });
    },
  );

  it("reloads for absent, mismatched or unsupported delete receipts", () => {
    const receipt = {
      node_id: node.id,
      node_state: "removed" as const,
      removed_edge_ids: ["edge-1"],
      conversation: { effect: "deleted_empty" as const },
    };
    expect(
      resolveMapDomainChange({ kind: "node", id: node.id, action: "deleted" }),
    ).toEqual({ kind: "reload-domain-data" });
    expect(
      resolveMapDomainChange({
        kind: "node",
        id: "another-node",
        action: "deleted",
        receipt,
      }),
    ).toEqual({ kind: "reload-domain-data" });
    expect(
      resolveMapDomainChange({
        kind: "node",
        id: node.id,
        action: "visibility_changed",
      }),
    ).toEqual({ kind: "reload-domain-data" });
  });
});
