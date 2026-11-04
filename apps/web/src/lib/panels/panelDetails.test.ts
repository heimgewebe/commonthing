import {
  describe,
  it,
  expect,
  vi,
  beforeEach,
  afterEach,
  type Mock,
} from "vitest";
import { get, writable } from "svelte/store";

import {
  buildPanelEndpoint,
  createPanelDetailsLoader,
  type SelectionLike,
} from "./panelDetails";

type DeferredResponse = {
  resolve: (body: unknown) => void;
  reject: (reason: unknown) => void;
  signal: AbortSignal | undefined;
  endpoint: string;
};

function createDeferredFetcher() {
  const calls: DeferredResponse[] = [];
  const fetcher = vi.fn(
    (input: RequestInfo | URL, init?: RequestInit) =>
      new Promise<Response>((resolve, reject) => {
        const endpoint =
          typeof input === "string"
            ? input
            : input instanceof URL
              ? input.toString()
              : input.url;
        const signal = init?.signal ?? undefined;
        const deferred: DeferredResponse = {
          resolve: (body) =>
            resolve({
              ok: true,
              status: 200,
              json: () => Promise.resolve(body),
            } as unknown as Response),
          reject,
          signal,
          endpoint,
        };
        calls.push(deferred);
        if (signal) {
          signal.addEventListener("abort", () => {
            const abortError = new Error("Aborted");
            abortError.name = "AbortError";
            reject(abortError);
          });
        }
      }),
  );
  return { fetcher, calls };
}

async function flushMicrotasks() {
  for (let i = 0; i < 10; i += 1) {
    await Promise.resolve();
  }
}

describe("buildPanelEndpoint", () => {
  it("returns the plural local endpoint exposed by the API router", () => {
    expect(buildPanelEndpoint("node", "abc", "")).toBe("/api/nodes/abc");
    expect(buildPanelEndpoint("account", "x", "")).toBe("/api/accounts/x");
    expect(buildPanelEndpoint("edge", "1", "")).toBe("/api/edges/1");
    expect(buildPanelEndpoint("webgemeindezentrum", "center-1", "")).toBe(
      "/api/webgemeindezentren/center-1",
    );
  });

  it("returns the plural remote endpoint when apiBase is set", () => {
    expect(buildPanelEndpoint("node", "abc", "http://api.test")).toBe(
      "http://api.test/api/nodes/abc",
    );
    expect(buildPanelEndpoint("account", "x", "http://api.test")).toBe(
      "http://api.test/api/accounts/x",
    );
    expect(buildPanelEndpoint("edge", "1", "http://api.test")).toBe(
      "http://api.test/api/edges/1",
    );
    expect(
      buildPanelEndpoint("webgemeindezentrum", "center-1", "http://api.test"),
    ).toBe("http://api.test/api/webgemeindezentren/center-1");
  });
});

describe("createPanelDetailsLoader", () => {
  let onError: Mock<(error: unknown) => void>;

  beforeEach(() => {
    onError = vi.fn<(error: unknown) => void>();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("does not fetch when the selection is initially empty", () => {
    const selection = writable<SelectionLike | null>(null);
    const { fetcher, calls } = createDeferredFetcher();
    const loader = createPanelDetailsLoader<{ id: string }>(selection, {
      buildEndpoint: (id) => `/api/node/${id}`,
      fetcher,
      onError,
    });

    expect(calls).toHaveLength(0);
    expect(get(loader.details)).toBeNull();
    expect(get(loader.isLoading)).toBe(false);

    loader.destroy();
  });

  it("starts loading and stores fetched details for a selection", async () => {
    const selection = writable<SelectionLike | null>(null);
    const { fetcher, calls } = createDeferredFetcher();
    const loader = createPanelDetailsLoader<{ id: string; title: string }>(
      selection,
      {
        buildEndpoint: (id) => `/api/node/${id}`,
        fetcher,
        onError,
      },
    );

    selection.set({ id: "node-1" });
    expect(calls).toHaveLength(1);
    expect(calls[0].endpoint).toBe("/api/node/node-1");
    expect(get(loader.isLoading)).toBe(true);

    calls[0].resolve({ id: "node-1", title: "Knoten 1" });
    await flushMicrotasks();

    expect(get(loader.isLoading)).toBe(false);
    expect(get(loader.details)).toEqual({ id: "node-1", title: "Knoten 1" });
    expect(onError).not.toHaveBeenCalled();

    loader.destroy();
  });

  it("aborts an in-flight request when the selection changes", async () => {
    const selection = writable<SelectionLike | null>(null);
    const { fetcher, calls } = createDeferredFetcher();
    const loader = createPanelDetailsLoader<{ id: string }>(selection, {
      buildEndpoint: (id) => `/api/node/${id}`,
      fetcher,
      onError,
    });

    selection.set({ id: "node-1" });
    expect(calls[0].signal?.aborted).toBe(false);

    selection.set({ id: "node-2" });
    expect(calls[0].signal?.aborted).toBe(true);
    expect(calls).toHaveLength(2);
    expect(calls[1].endpoint).toBe("/api/node/node-2");

    calls[1].resolve({ id: "node-2" });
    await flushMicrotasks();

    expect(get(loader.details)).toEqual({ id: "node-2" });
    expect(onError).not.toHaveBeenCalled();

    loader.destroy();
  });

  it("ignores a stale response that resolves after a newer selection", async () => {
    const selection = writable<SelectionLike | null>(null);
    const { fetcher, calls } = createDeferredFetcher();
    const loader = createPanelDetailsLoader<{ id: string }>(selection, {
      buildEndpoint: (id) => `/api/node/${id}`,
      fetcher,
      onError,
    });

    selection.set({ id: "node-1" });
    selection.set({ id: "node-2" });

    calls[1].resolve({ id: "node-2" });
    await flushMicrotasks();
    calls[0].resolve({ id: "node-1" });
    await flushMicrotasks();

    expect(get(loader.details)).toEqual({ id: "node-2" });

    loader.destroy();
  });

  it("clears state and skips fetching when the selection becomes null", async () => {
    const selection = writable<SelectionLike | null>({ id: "node-1" });
    const { fetcher, calls } = createDeferredFetcher();
    const loader = createPanelDetailsLoader<{ id: string }>(selection, {
      buildEndpoint: (id) => `/api/node/${id}`,
      fetcher,
      onError,
    });

    expect(calls).toHaveLength(1);
    calls[0].resolve({ id: "node-1" });
    await flushMicrotasks();
    expect(get(loader.details)).toEqual({ id: "node-1" });

    selection.set(null);
    expect(get(loader.details)).toBeNull();
    expect(get(loader.isLoading)).toBe(false);
    expect(calls).toHaveLength(1);

    loader.destroy();
  });

  it("invokes onSelectionChange exactly when the selection id changes", async () => {
    const selection = writable<SelectionLike | null>(null);
    const { fetcher, calls } = createDeferredFetcher();
    const onSelectionChange = vi.fn();
    const loader = createPanelDetailsLoader<{ id: string }>(selection, {
      buildEndpoint: (id) => `/api/node/${id}`,
      fetcher,
      onError,
      onSelectionChange,
    });

    expect(onSelectionChange).not.toHaveBeenCalled();

    selection.set({ id: "node-1" });
    expect(onSelectionChange).toHaveBeenCalledTimes(1);
    expect(onSelectionChange).toHaveBeenLastCalledWith("node-1");

    selection.set({ id: "node-1" });
    expect(onSelectionChange).toHaveBeenCalledTimes(1);

    selection.set(null);
    expect(onSelectionChange).toHaveBeenCalledTimes(2);
    expect(onSelectionChange).toHaveBeenLastCalledWith(undefined);

    calls[0].resolve({ id: "node-1" });
    await flushMicrotasks();

    loader.destroy();
  });

  it("aborts the in-flight request on destroy", () => {
    const selection = writable<SelectionLike | null>(null);
    const { fetcher, calls } = createDeferredFetcher();
    const loader = createPanelDetailsLoader<{ id: string }>(selection, {
      buildEndpoint: (id) => `/api/node/${id}`,
      fetcher,
      onError,
    });

    selection.set({ id: "node-1" });
    expect(calls[0].signal?.aborted).toBe(false);
    expect(get(loader.isLoading)).toBe(true);

    loader.destroy();
    expect(calls[0].signal?.aborted).toBe(true);
    expect(get(loader.details)).toBeNull();
    expect(get(loader.isLoading)).toBe(false);
  });

  it("reports an error when fetch resolves with a non-ok response", async () => {
    const selection = writable<SelectionLike | null>(null);
    const fetcher = vi.fn(() =>
      Promise.resolve({
        ok: false,
        status: 500,
        json: () => Promise.resolve({ error: "nope" }),
      } as unknown as Response),
    ) as unknown as typeof fetch;
    const loader = createPanelDetailsLoader<{ id: string }>(selection, {
      buildEndpoint: (id) => `/api/node/${id}`,
      fetcher,
      onError,
      resourceLabel: "node details",
    });

    selection.set({ id: "node-err" });
    await flushMicrotasks();

    expect(get(loader.isLoading)).toBe(false);
    expect(get(loader.details)).toBeNull();
    expect(onError).toHaveBeenCalledTimes(1);
    const reportedError = onError.mock.calls[0][0] as Error;
    expect(reportedError.message).toBe("Failed to load node details");

    loader.destroy();
  });
});
