import { describe, expect, it, vi } from "vitest";
import { MAP_RESOURCE_LOAD_DEADLINE_MS } from "$lib/map/cursorPagination";
import { load } from "./+page";

function cursorPage(items: unknown[] = []) {
  return new Response(
    JSON.stringify({
      items,
      page: { limit: 1000, next_cursor: null, has_more: false },
    }),
    { status: 200, headers: { "content-type": "application/json" } },
  );
}

describe("map PageLoad resource deadline wiring", () => {
  it("forwards the deadline AbortSignal to the real SvelteKit fetch seam", async () => {
    vi.useFakeTimers();
    const errorSpy = vi.spyOn(console, "error").mockImplementation(() => {});
    try {
      let observedSignal: AbortSignal | undefined;
      let abortObserved = false;
      const fetcher = vi.fn(
        (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
          const pathname = new URL(String(input), "http://localhost").pathname;
          if (pathname === "/api/accounts") {
            const signal = init?.signal ?? undefined;
            observedSignal = signal;
            return new Promise<Response>((_resolve, reject) => {
              signal?.addEventListener(
                "abort",
                () => {
                  abortObserved = true;
                  reject(new Error("aborted page-load request"));
                },
                { once: true },
              );
            });
          }
          return Promise.resolve(cursorPage());
        },
      );

      const pending = Promise.resolve(
        load({
          fetch: fetcher,
          depends: vi.fn(),
          url: new URL("http://localhost/map"),
        } as never),
      );

      await vi.advanceTimersByTimeAsync(0);
      expect(observedSignal).toBeDefined();
      await vi.advanceTimersByTimeAsync(MAP_RESOURCE_LOAD_DEADLINE_MS);
      const result = await pending;
      if (!result) throw new Error("expected map page load data");

      expect(observedSignal?.aborted).toBe(true);
      expect(abortObserved).toBe(true);
      expect(result.loadState).toBe("partial");
      expect(result.accounts).toEqual([]);
      expect(result.resourceStatus).toContainEqual({
        resource: "accounts",
        status: "failed",
        error: `Timed out loading accounts after ${MAP_RESOURCE_LOAD_DEADLINE_MS} ms`,
      });
    } finally {
      errorSpy.mockRestore();
      vi.useRealTimers();
    }
  });
});
