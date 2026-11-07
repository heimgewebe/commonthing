import { afterEach, describe, expect, it, vi } from "vitest";
import {
  getNodeAudience,
  setNodeAudience,
  NodeAudienceError,
} from "./nodeAudience";

afterEach(() => vi.unstubAllGlobals());

describe("one node, explicit audience scopes", () => {
  it("reads owner-only grants through a no-store authenticated request", async () => {
    const payload = {
      node_id: "existing-node",
      scope: "private",
      circle_ids: [],
    };
    const fetchMock = vi
      .fn()
      .mockResolvedValue(
        new Response(JSON.stringify(payload), { status: 200 }),
      );
    vi.stubGlobal("fetch", fetchMock);
    await expect(getNodeAudience("existing-node")).resolves.toEqual(payload);
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/nodes/existing-node/audience",
      expect.objectContaining({
        credentials: "include",
        cache: "no-store",
        method: "GET",
      }),
    );
  });

  it("requires an explicit scope and selected circle IDs for each PUT", async () => {
    const answer = {
      node_id: "existing-node",
      scope: "circles",
      circle_ids: ["house-one"],
    };
    const fetchMock = vi
      .fn()
      .mockResolvedValue(new Response(JSON.stringify(answer), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    await setNodeAudience("existing-node", "circles", ["house-one"], {
      scope: "private",
      circle_ids: [],
    });
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/nodes/existing-node/audience",
      expect.objectContaining({
        method: "PUT",
        credentials: "include",
        cache: "no-store",
        body: JSON.stringify({
          scope: "circles",
          circle_ids: ["house-one"],
          expected_scope: "private",
          expected_circle_ids: [],
        }),
      }),
    );
  });

  it("does not silently treat a denied node as an editable owner node", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(new Response(null, { status: 404 })),
    );
    await expect(getNodeAudience("private-node")).rejects.toBeInstanceOf(
      NodeAudienceError,
    );
  });

  it("blocks re-publication through the audience editor for hidden or revoked nodes", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(new Response(null, { status: 409 })),
    );
    await expect(
      setNodeAudience("hidden", "public", [], {
        scope: "private",
        circle_ids: [],
      }),
    ).rejects.toMatchObject({
      status: 409,
    });
  });
});
