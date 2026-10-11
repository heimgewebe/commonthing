// MapLibre 6 ships as ESM and, by default, loads its worker from
// `maplibre-gl-worker.mjs` next to its own module URL. After bundling that
// file does not exist next to the hashed chunk, so the worker 404s and no
// tile or GeoJSON source ever renders. Vite bundles the worker (with the
// shared module it imports) as its own asset; every map entry point must load
// MapLibre through this module so the worker URL is set before a map exists.
import workerUrl from "maplibre-gl/dist/maplibre-gl-worker.mjs?worker&url";

export type MapLibreModule = typeof import("maplibre-gl");

export async function loadMapLibre(): Promise<MapLibreModule> {
  const maplibre = await import("maplibre-gl");
  if (maplibre.getWorkerUrl() !== workerUrl) {
    maplibre.setWorkerUrl(workerUrl);
  }
  return maplibre;
}
