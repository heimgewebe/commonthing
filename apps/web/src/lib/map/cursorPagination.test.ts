import { describe, expect, it, vi } from "vitest";
import { fetchCursorPages, loadMapResources } from "./cursorPagination";

function page<T>(
  items: T[],
  hasMore: boolean,
  nextCursor: string | null,
  limit = 2,
) {
  return new Response(
    JSON.stringify({
      items,
      page: {
        limit,
        next_cursor: nextCursor,
        has_more: hasMore,
      },
    }),
    { status: 200, headers: { "content-type": "application/json" } },
  );
}

describe("fetchCursorPages", () => {
  it("loads every cursor page and preserves order", async () => {
    const fetcher = vi
      .fn<(input: string) => Promise<Response>>()
      .mockResolvedValueOnce(page([1, 2], true, "cursor-1"))
      .mockResolvedValueOnce(page([3, 4], true, "cursor-2"))
      .mockResolvedValueOnce(page([5], false, null));

    const result = await fetchCursorPages(fetcher, "/api/nodes", {
      pageSize: 2,
      maxPages: 10,
      maxItems: 10,
    });

    expect(result).toEqual({
      items: [1, 2, 3, 4, 5],
      status: "complete",
      pages: 3,
    });
    expect(fetcher.mock.calls.map(([url]) => url)).toEqual([
      "/api/nodes?pagination=cursor&limit=2",
      "/api/nodes?pagination=cursor&limit=2&cursor=cursor-1",
      "/api/nodes?pagination=cursor&limit=2&cursor=cursor-2",
    ]);
  });

  it("loads beyond one maximum-sized API page", async () => {
    const firstPage = Array.from({ length: 1000 }, (_, index) => index);
    const fetcher = vi
      .fn<(input: string) => Promise<Response>>()
      .mockResolvedValueOnce(page(firstPage, true, "cursor-1000", 1000))
      .mockResolvedValueOnce(page([1000], false, null, 1000));

    const result = await fetchCursorPages(fetcher, "/api/nodes");

    expect(result.status).toBe("complete");
    expect(result.items).toHaveLength(1001);
    expect(result.items.at(-1)).toBe(1000);
    expect(fetcher).toHaveBeenCalledTimes(2);
  });

  it("rejects the legacy bare-array response instead of claiming completeness", async () => {
    const fetcher = vi.fn(
      async () => new Response(JSON.stringify([1, 2]), { status: 200 }),
    );

    await expect(fetchCursorPages(fetcher, "/api/nodes")).rejects.toThrow(
      "Invalid cursor response shape",
    );
  });

  it("rejects has_more without a next cursor", async () => {
    const fetcher = vi.fn(async () => page([1], true, null));

    await expect(fetchCursorPages(fetcher, "/api/nodes")).rejects.toThrow(
      "Missing next_cursor",
    );
  });

  it("does not let the page limit hide a broken cursor", async () => {
    const fetcher = vi.fn(async () => page([1], true, null));

    await expect(
      fetchCursorPages(fetcher, "/api/nodes", { maxPages: 1 }),
    ).rejects.toThrow("Missing next_cursor");
  });

  it("rejects a repeated cursor before looping", async () => {
    const fetcher = vi
      .fn<(input: string) => Promise<Response>>()
      .mockResolvedValueOnce(page([1], true, "same"))
      .mockResolvedValueOnce(page([2], true, "same"));

    await expect(fetchCursorPages(fetcher, "/api/nodes")).rejects.toThrow(
      "Cursor did not advance",
    );
    expect(fetcher).toHaveBeenCalledTimes(2);
  });

  it("fails the resource when a later page returns an HTTP error", async () => {
    const fetcher = vi
      .fn<(input: string) => Promise<Response>>()
      .mockResolvedValueOnce(page([1], true, "next"))
      .mockResolvedValueOnce(new Response("boom", { status: 503 }));

    await expect(fetchCursorPages(fetcher, "/api/nodes")).rejects.toThrow(
      "HTTP 503 while loading page 2",
    );
  });

  it("returns truncated when the page limit is reached", async () => {
    const fetcher = vi
      .fn<(input: string) => Promise<Response>>()
      .mockResolvedValueOnce(page([1], true, "cursor-1", 1))
      .mockResolvedValueOnce(page([2], true, "cursor-2", 1));

    await expect(
      fetchCursorPages(fetcher, "/api/nodes", {
        pageSize: 1,
        maxPages: 2,
        maxItems: 10,
      }),
    ).resolves.toEqual({
      items: [1, 2],
      status: "truncated",
      pages: 2,
      reason: "page_limit",
    });
  });

  it("caps the aggregate and returns truncated at the item limit", async () => {
    const fetcher = vi
      .fn<(input: string) => Promise<Response>>()
      .mockResolvedValueOnce(page([1, 2, 3], true, "cursor-1", 3))
      .mockResolvedValueOnce(page([4, 5, 6], true, "cursor-2", 3));

    await expect(
      fetchCursorPages(fetcher, "/api/nodes", {
        pageSize: 3,
        maxPages: 10,
        maxItems: 4,
      }),
    ).resolves.toEqual({
      items: [1, 2, 3, 4],
      status: "truncated",
      pages: 2,
      reason: "item_limit",
    });
  });
});

describe("loadMapResources", () => {
  it("preserves declared resource order when responses resolve out of order", async () => {
    const delays: Record<string, number> = {
      nodes: 15,
      accounts: 5,
      edges: 0,
      webgemeindezentren: 10,
    };
    const fetcher = vi.fn(async (input: string) => {
      const resource = new URL(input, "http://localhost").pathname
        .split("/")
        .at(-1)!;
      await new Promise((resolve) => setTimeout(resolve, delays[resource]));
      return page([], false, null, 1000);
    });

    const result = await loadMapResources(fetcher, "https://api.example.test");

    expect(result.resourceStatus.map((status) => status.resource)).toEqual([
      "nodes",
      "accounts",
      "edges",
      "webgemeindezentren",
    ]);
    expect(result.nodeLoadMode).toBe("global");
    expect(result.loadState).toBe("ok");
    expect(result.loadNotice).toBeNull();
  });

  it("uses cursor transport by default for relative same-origin APIs", async () => {
    const fetcher = vi.fn<(input: string) => Promise<Response>>(async () =>
      page([], false, null, 1000),
    );

    const result = await loadMapResources(fetcher, "");

    expect(fetcher.mock.calls.map(([url]) => url)).toEqual([
      "/api/nodes?pagination=cursor&limit=1000",
      "/api/accounts?pagination=cursor&limit=1000",
      "/api/edges?pagination=cursor&limit=1000",
      "/api/webgemeindezentren?pagination=cursor&limit=1000",
    ]);
    expect(result.resourceStatus).toEqual([
      { resource: "nodes", status: "complete", loaded: 0, pages: 1 },
      { resource: "accounts", status: "complete", loaded: 0, pages: 1 },
      { resource: "edges", status: "complete", loaded: 0, pages: 1 },
      {
        resource: "webgemeindezentren",
        status: "complete",
        loaded: 0,
        pages: 1,
      },
    ]);
    expect(result.nodeLoadMode).toBe("global");
    expect(result.loadState).toBe("ok");
    expect(result.loadNotice).toBeNull();
  });

  it("defers global nodes in viewport mode and fetches only an exact deep-link node", async () => {
    const focusedNode = {
      id: "focus-node",
      kind: "Knoten",
      title: "Focus",
      location: { lon: 10.4, lat: 53.5 },
    };
    const fetcher = vi.fn(async (input: string) => {
      const url = new URL(input, "http://localhost");
      if (url.pathname === "/api/nodes/focus-node") {
        return new Response(JSON.stringify(focusedNode), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      }
      return page([], false, null, 1000);
    });

    const result = await loadMapResources(fetcher, "", "cursor", {
      nodeLoadMode: "viewport",
      focusedNodeId: "focus-node",
    });

    expect(fetcher.mock.calls.map(([url]) => url)).toEqual([
      "/api/nodes/focus-node",
      "/api/accounts?pagination=cursor&limit=1000",
      "/api/edges?pagination=cursor&limit=1000",
      "/api/webgemeindezentren?pagination=cursor&limit=1000",
    ]);
    expect(result.nodes).toEqual([focusedNode]);
    expect(result.nodeLoadMode).toBe("viewport");
    expect(result.resourceStatus[0]).toEqual({
      resource: "nodes",
      status: "viewport",
      loaded: 1,
      pages: 0,
    });
    expect(result.loadState).toBe("ok");
  });

  it("does not issue any node request during ordinary viewport bootstrap", async () => {
    const fetcher = vi.fn<(input: string) => Promise<Response>>(async () =>
      page([], false, null, 1000),
    );

    const result = await loadMapResources(fetcher, "", "cursor", {
      nodeLoadMode: "viewport",
    });

    expect(fetcher.mock.calls.map(([url]) => url)).toEqual([
      "/api/accounts?pagination=cursor&limit=1000",
      "/api/edges?pagination=cursor&limit=1000",
      "/api/webgemeindezentren?pagination=cursor&limit=1000",
    ]);
    expect(result.nodes).toEqual([]);
    expect(result.resourceStatus[0]).toEqual({
      resource: "nodes",
      status: "viewport",
      loaded: 0,
      pages: 0,
    });
  });

  it("times out and aborts one hung resource while preserving the others", async () => {
    vi.useFakeTimers();
    const errorSpy = vi.spyOn(console, "error").mockImplementation(() => {});
    try {
      let hungSignal: AbortSignal | undefined;
      const fetcher = vi.fn(
        (input: string, init?: RequestInit): Promise<Response> => {
          const resource = new URL(input, "http://localhost").pathname
            .split("/")
            .at(-1)!;
          if (resource === "accounts") {
            const signal = init?.signal ?? undefined;
            hungSignal = signal;
            return new Promise<Response>((_resolve, reject) => {
              signal?.addEventListener(
                "abort",
                () => reject(new Error("aborted hung resource")),
                { once: true },
              );
            });
          }
          return Promise.resolve(
            page([{ id: `${resource}-1` }], false, null, 1000),
          );
        },
      );

      const pending = loadMapResources(fetcher, "", "cursor", {
        resourceDeadlineMs: 1_000,
      });
      await vi.advanceTimersByTimeAsync(0);
      await vi.advanceTimersByTimeAsync(1_000);
      const result = await pending;

      expect(hungSignal?.aborted).toBe(true);
      expect(result.nodes).toHaveLength(1);
      expect(result.accounts).toEqual([]);
      expect(result.edges).toHaveLength(1);
      expect(result.webgemeindezentren).toHaveLength(1);
      expect(result.resourceStatus).toEqual([
        { resource: "nodes", status: "complete", loaded: 1, pages: 1 },
        {
          resource: "accounts",
          status: "failed",
          error: "Timed out loading accounts after 1000 ms",
        },
        { resource: "edges", status: "complete", loaded: 1, pages: 1 },
        {
          resource: "webgemeindezentren",
          status: "complete",
          loaded: 1,
          pages: 1,
        },
      ]);
      expect(result.loadState).toBe("partial");
      expect(result.loadNotice).toContain("Garnrollen");
    } finally {
      errorSpy.mockRestore();
      vi.useRealTimers();
    }
  });

  it("applies the same deadline while a response body is hanging", async () => {
    vi.useFakeTimers();
    const errorSpy = vi.spyOn(console, "error").mockImplementation(() => {});
    try {
      let bodySignal: AbortSignal | undefined;
      let bodyAbortObserved = false;
      const fetcher = vi.fn(
        (input: string, init?: RequestInit): Promise<Response> => {
          const resource = new URL(input, "http://localhost").pathname
            .split("/")
            .at(-1)!;
          if (resource === "edges") {
            const signal = init?.signal ?? undefined;
            bodySignal = signal;
            return Promise.resolve({
              ok: true,
              status: 200,
              json: () =>
                new Promise<never>((_resolve, reject) => {
                  signal?.addEventListener(
                    "abort",
                    () => {
                      bodyAbortObserved = true;
                      reject(new Error("aborted hanging body"));
                    },
                    { once: true },
                  );
                }),
            } as unknown as Response);
          }
          return Promise.resolve(page([], false, null, 1000));
        },
      );

      const pending = loadMapResources(fetcher, "", "cursor", {
        resourceDeadlineMs: 1_000,
      });
      await vi.advanceTimersByTimeAsync(0);
      await vi.advanceTimersByTimeAsync(1_000);
      const result = await pending;

      expect(bodySignal?.aborted).toBe(true);
      expect(bodyAbortObserved).toBe(true);
      expect(result.resourceStatus[2]).toEqual({
        resource: "edges",
        status: "failed",
        error: "Timed out loading edges after 1000 ms",
      });
      expect(result.loadState).toBe("partial");
    } finally {
      errorSpy.mockRestore();
      vi.useRealTimers();
    }
  });

  it("bounds the total resource load across cursor continuation pages", async () => {
    vi.useFakeTimers();
    const errorSpy = vi.spyOn(console, "error").mockImplementation(() => {});
    try {
      let nodeCalls = 0;
      let continuationSignal: AbortSignal | undefined;
      const fetcher = vi.fn(
        (input: string, init?: RequestInit): Promise<Response> => {
          const resource = new URL(input, "http://localhost").pathname
            .split("/")
            .at(-1)!;
          if (resource === "nodes") {
            nodeCalls += 1;
            if (nodeCalls === 1) {
              return Promise.resolve(
                page([{ id: "node-first-page" }], true, "next", 1000),
              );
            }
            const signal = init?.signal ?? undefined;
            continuationSignal = signal;
            return new Promise<Response>((_resolve, reject) => {
              signal?.addEventListener(
                "abort",
                () => reject(new Error("aborted continuation")),
                { once: true },
              );
            });
          }
          return Promise.resolve(page([], false, null, 1000));
        },
      );

      const pending = loadMapResources(fetcher, "", "cursor", {
        resourceDeadlineMs: 1_000,
      });
      await vi.advanceTimersByTimeAsync(0);
      expect(nodeCalls).toBe(2);
      await vi.advanceTimersByTimeAsync(1_000);
      const result = await pending;

      expect(continuationSignal?.aborted).toBe(true);
      expect(result.nodes).toEqual([]);
      expect(result.resourceStatus[0]).toEqual({
        resource: "nodes",
        status: "failed",
        error: "Timed out loading nodes after 1000 ms",
      });
      expect(
        result.resourceStatus
          .slice(1)
          .every((status) => status.status === "complete"),
      ).toBe(true);
      expect(result.loadState).toBe("partial");
    } finally {
      errorSpy.mockRestore();
      vi.useRealTimers();
    }
  });

  it("loads the built-in static demo endpoints as complete bare arrays", async () => {
    const staticResources: Record<string, unknown[]> = {
      nodes: [{ id: "node-1" }],
      accounts: [{ id: "account-1" }],
      edges: [{ id: "edge-1" }],
      webgemeindezentren: [{ id: "center-1" }],
    };
    const fetcher = vi.fn(async (input: string) => {
      const resource = new URL(input, "http://localhost").pathname
        .split("/")
        .at(-1)!;
      return new Response(JSON.stringify(staticResources[resource]), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    });

    const result = await loadMapResources(fetcher, "", "static-list");

    expect(fetcher.mock.calls.map(([url]) => url)).toEqual([
      "/api/nodes",
      "/api/accounts",
      "/api/edges",
      "/api/webgemeindezentren",
    ]);
    expect(result.nodes).toEqual([{ id: "node-1" }]);
    expect(result.accounts).toEqual([{ id: "account-1" }]);
    expect(result.edges).toEqual([{ id: "edge-1" }]);
    expect(result.webgemeindezentren).toEqual([{ id: "center-1" }]);
    expect(result.resourceStatus).toEqual([
      { resource: "nodes", status: "complete", loaded: 1, pages: 1 },
      { resource: "accounts", status: "complete", loaded: 1, pages: 1 },
      { resource: "edges", status: "complete", loaded: 1, pages: 1 },
      {
        resource: "webgemeindezentren",
        status: "complete",
        loaded: 1,
        pages: 1,
      },
    ]);
    expect(result.nodeLoadMode).toBe("global");
    expect(result.loadState).toBe("ok");
    expect(result.loadNotice).toBeNull();
  });
});
