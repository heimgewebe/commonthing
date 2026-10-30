import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import {
  isNodeKind,
  nodeKindLabel,
  NODE_KIND_OPTIONS,
} from "./productLanguage";

const catalog = JSON.parse(
  readFileSync(
    new URL(
      "../../../../../contracts/domain/node-kinds.v1.json",
      import.meta.url,
    ),
    "utf8",
  ),
) as { kinds: { id: string; label: string }[] };

describe("canonical node kinds", () => {
  it("uses the documented domain identities and German labels", () => {
    expect(NODE_KIND_OPTIONS.map(({ id, label }) => ({ id, label }))).toEqual(
      catalog.kinds.map(({ id, label }) => ({ id, label })),
    );
  });

  it.each(["standard", "Unknown", "unclassified", "Werkstatt", ""])(
    "rejects noncanonical kind %s without inventing a default",
    (kind) => {
      expect(isNodeKind(kind)).toBe(false);
      expect(nodeKindLabel(kind)).toBe("Ungültige Knotenart");
    },
  );

  it("accepts exactly the six explicit kinds", () => {
    expect(NODE_KIND_OPTIONS).toHaveLength(6);
    for (const kind of NODE_KIND_OPTIONS) {
      expect(isNodeKind(kind.id)).toBe(true);
      expect(nodeKindLabel(kind.id)).toBe(kind.label);
    }
  });
});
