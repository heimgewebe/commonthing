import { readFileSync } from "node:fs";
import { describe, expect, it, vi } from "vitest";
import {
  MAP_INIT_TIMEOUT_MS,
  resolveMapInitFailure,
  scheduleMapInitTimeout,
} from "$lib/map/mapInitFailure";

describe("resolveMapInitFailure", () => {
  it("fails closed for import, constructor and timeout failures", () => {
    expect(resolveMapInitFailure(false, "import")).toBe("fail");
    expect(resolveMapInitFailure(false, "constructor")).toBe("fail");
    expect(resolveMapInitFailure(false, "timeout")).toBe("fail");
  });

  it("lets the watchdog arbitrate recoverable MapLibre resource errors", () => {
    expect(resolveMapInitFailure(false, "maplibre-error")).toBe("ignore");
    expect(resolveMapInitFailure(true, "maplibre-error")).toBe("ignore");
  });

  it("ignores failures after the first successful load", () => {
    expect(resolveMapInitFailure(true, "timeout")).toBe("ignore");
    expect(resolveMapInitFailure(true, "maplibre-error")).toBe("ignore");
    expect(resolveMapInitFailure(true, "import")).toBe("ignore");
    expect(resolveMapInitFailure(true, "post-load-error")).toBe("ignore");
  });

  it("never fails the map for auth-camera rejection alone", () => {
    expect(resolveMapInitFailure(false, "auth-camera")).toBe("ignore");
    expect(resolveMapInitFailure(true, "auth-camera")).toBe("ignore");
  });
});

describe("scheduleMapInitTimeout", () => {
  it("arms immediately and fires only after the init deadline", () => {
    vi.useFakeTimers();
    try {
      const onTimeout = vi.fn();
      const handle = scheduleMapInitTimeout(onTimeout);

      expect(onTimeout).not.toHaveBeenCalled();
      vi.advanceTimersByTime(MAP_INIT_TIMEOUT_MS - 1);
      expect(onTimeout).not.toHaveBeenCalled();
      vi.advanceTimersByTime(1);
      expect(onTimeout).toHaveBeenCalledTimes(1);
      clearTimeout(handle);
    } finally {
      vi.useRealTimers();
    }
  });

  it("does not fire after clearTimeout cancels the armed handle", () => {
    vi.useFakeTimers();
    try {
      const onTimeout = vi.fn();
      const handle = scheduleMapInitTimeout(onTimeout);
      clearTimeout(handle);
      vi.advanceTimersByTime(MAP_INIT_TIMEOUT_MS);
      expect(onTimeout).not.toHaveBeenCalled();
    } finally {
      vi.useRealTimers();
    }
  });

  it("uses the injected scheduler so callers can prove early arming", () => {
    const scheduled: Array<{ delay: number; fn: () => void }> = [];
    const schedule = ((fn: () => void, delay?: number) => {
      scheduled.push({ fn, delay: delay ?? 0 });
      return 42 as unknown as ReturnType<typeof setTimeout>;
    }) as typeof setTimeout;

    const onTimeout = vi.fn();
    const handle = scheduleMapInitTimeout(onTimeout, schedule, 2500);

    // Scheduling is synchronous: the deadline is live before any later await.
    expect(scheduled).toEqual([{ fn: onTimeout, delay: 2500 }]);
    expect(handle).toBe(42);
    expect(onTimeout).not.toHaveBeenCalled();
    scheduled[0]?.fn();
    expect(onTimeout).toHaveBeenCalledTimes(1);
  });
});

describe("map page early init-timeout wiring", () => {
  const pageSource = readFileSync(
    new URL("../../routes/map/+page.svelte", import.meta.url),
    "utf8",
  );

  it("arms the watchdog synchronously before void initialiseMap() runs", () => {
    const fnIdx = pageSource.indexOf("async function initialiseMap()");
    const armIdx = pageSource.indexOf(
      "loadingTimeout = scheduleMapInitTimeout(",
    );
    const callIdx = pageSource.indexOf("void initialiseMap()");
    const hangCommentIdx = pageSource.indexOf(
      "Arm before the first await/import inside initialiseMap",
    );

    expect(fnIdx).toBeGreaterThan(-1);
    expect(armIdx).toBeGreaterThan(-1);
    expect(callIdx).toBeGreaterThan(-1);
    // Definition first, then arm, then start — so a hung import cannot race
    // past an unarmed deadline.
    expect(fnIdx).toBeLessThan(armIdx);
    expect(armIdx).toBeLessThan(callIdx);
    expect(hangCommentIdx).toBeGreaterThan(-1);
    expect(hangCommentIdx).toBeLessThan(armIdx);

    // The arm callback must still fail closed as a pre-load timeout.
    const armSlice = pageSource.slice(armIdx, callIdx);
    expect(armSlice).toContain("failMapInit(");
    expect(armSlice).toContain('"timeout"');
  });

  it("starts local PMTiles loading in parallel with the MapLibre runtime", () => {
    const initIdx = pageSource.indexOf("async function initialiseMap()");
    const firstGuardIdx = pageSource.indexOf(
      "if (destroyed || mapInitTerminated) return;",
      initIdx,
    );
    expect(initIdx).toBeGreaterThan(-1);
    expect(firstGuardIdx).toBeGreaterThan(initIdx);

    const importBatch = pageSource.slice(initIdx, firstGuardIdx);
    // The runtime loader sets the bundled worker URL before any map exists.
    expect(importBatch).toContain("loadMapLibre()");
    expect(importBatch).not.toContain('import("maplibre-gl")');
    expect(importBatch).toContain('import("$lib/map/overlay/nodes")');
    expect(importBatch).toContain('import("$lib/map/overlay/edgeMotion")');
    expect(importBatch).toContain('currentBasemap.mode === "local-sovereign"');
    expect(importBatch).toContain('? import("pmtiles")');
    expect(importBatch).toContain("Promise.resolve(null)");

    const protocolIdx = pageSource.indexOf(
      'if (currentBasemap.mode === "local-sovereign")',
      firstGuardIdx,
    );
    const mapConstructorIdx = pageSource.indexOf(
      "map = new maplibregl.Map({",
      protocolIdx,
    );
    expect(protocolIdx).toBeGreaterThan(firstGuardIdx);
    expect(mapConstructorIdx).toBeGreaterThan(protocolIdx);
    expect(pageSource.slice(protocolIdx, mapConstructorIdx)).not.toContain(
      'await import("pmtiles")',
    );
  });

  it("does not re-arm a second loading timeout after map construction", () => {
    const arms = pageSource.match(
      /loadingTimeout\s*=\s*scheduleMapInitTimeout\s*\(/g,
    );
    expect(arms).toHaveLength(1);
    expect(pageSource).not.toMatch(/loadingTimeout\s*=\s*setTimeout\s*\(/);
    // No residual late setTimeout deadline inside the map constructor path.
    expect(pageSource).not.toMatch(
      /setTimeout\(\s*\(\)\s*=>\s*\{\s*failMapInit\(\s*"timeout"/,
    );
  });

  it("clears the armed timeout on successful load and on mount cleanup", () => {
    const clearOccurrences = pageSource.match(
      /clearTimeout\(\s*loadingTimeout\s*\)/g,
    );
    // failMapInit, finishInitialLoading, and onMount cleanup must all cancel it.
    expect(clearOccurrences?.length).toBeGreaterThanOrEqual(3);
  });

  it("makes a terminal init failure exclusive with later load success", () => {
    expect(pageSource).toContain("let mapInitTerminated = false;");

    const failIdx = pageSource.indexOf("const failMapInit = (");
    const handlerIdx = pageSource.indexOf("const handleSearchMapMove", failIdx);
    const failSlice = pageSource.slice(failIdx, handlerIdx);
    expect(failSlice).toContain("mapInitTerminated = true;");
    expect(failSlice).toContain("teardownMapRuntime();");

    const finishIdx = pageSource.indexOf(
      "const finishInitialLoading = (generation: number) => {",
    );
    const loadListenerIdx = pageSource.indexOf(
      'map.once("load", async () => {',
      finishIdx,
    );
    expect(finishIdx).toBeGreaterThan(-1);
    expect(loadListenerIdx).toBeGreaterThan(finishIdx);
    const finishSlice = pageSource.slice(finishIdx, loadListenerIdx);
    expect(finishSlice).toMatch(/mapInitTerminated\s*\|\|/);
    expect(finishSlice).toContain("generation !== basemapStyleGeneration");
    expect(finishSlice).toContain("switchBasemapScheme(currentScheme)");
    expect(pageSource).toContain(
      'map.once("idle", () => finishInitialLoading(generation));',
    );
    expect(pageSource).toMatch(/\{\s*diff:\s*false,?\s*\}/);
    expect(pageSource).not.toContain("initialBasemapGeneration");
  });

  it("tears down partial map resources on terminal init failure", () => {
    const teardownIdx = pageSource.indexOf("function teardownMapRuntime()");
    const failIdx = pageSource.indexOf("const failMapInit = (", teardownIdx);
    const teardownSlice = pageSource.slice(teardownIdx, failIdx);

    expect(teardownSlice).toContain("nodesOverlay?.destroy();");
    expect(teardownSlice).toContain("edgeMotion?.destroy();");
    expect(teardownSlice).toContain(
      'if (typeof map.remove === "function") map.remove();',
    );
    expect(teardownSlice).toContain("map = null;");
    expect(teardownSlice).toContain("releasePmtilesProtocol?.();");
  });

  it("covers hung dynamic imports: deadline fires without waiting for resolve", async () => {
    vi.useFakeTimers();
    try {
      const onTimeout = vi.fn();
      // Mount wiring under test: arm first, then a never-resolving import path.
      const handle = scheduleMapInitTimeout(onTimeout);
      const hangingImport = new Promise<never>(() => {
        /* intentionally never resolves */
      });
      void hangingImport;

      expect(onTimeout).not.toHaveBeenCalled();
      await vi.advanceTimersByTimeAsync(MAP_INIT_TIMEOUT_MS);
      expect(onTimeout).toHaveBeenCalledTimes(1);
      clearTimeout(handle);
    } finally {
      vi.useRealTimers();
    }
  });
});
