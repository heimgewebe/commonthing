import { beforeEach, describe, expect, it, vi } from "vitest";

const workerState = vi.hoisted(() => ({ url: "" }));

vi.mock("maplibre-gl", () => ({
  getWorkerUrl: () => workerState.url,
  setWorkerUrl: vi.fn((value: string) => {
    workerState.url = value;
  }),
}));

describe("loadMapLibre", () => {
  beforeEach(() => {
    workerState.url = "";
  });

  it("points MapLibre at the bundled worker before a map can exist", async () => {
    const { loadMapLibre } = await import("./runtime");
    const maplibre = await loadMapLibre();

    expect(maplibre.setWorkerUrl).toHaveBeenCalledTimes(1);
    expect(workerState.url).not.toBe("");
    // The library default resolves next to the hashed chunk and 404s.
    expect(workerState.url).not.toMatch(/(^|\/)maplibre-gl-worker\.mjs$/);
  });

  it("keeps an already configured worker URL", async () => {
    const { loadMapLibre } = await import("./runtime");
    const first = await loadMapLibre();
    vi.mocked(first.setWorkerUrl).mockClear();

    await loadMapLibre();

    expect(first.setWorkerUrl).not.toHaveBeenCalled();
  });
});
