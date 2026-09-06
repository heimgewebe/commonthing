import { browser } from "$app/environment";
import type { PageLoad } from "./$types";
import { selectMapResourceTransport } from "$lib/map/mapResourceTransport";
import { parseMapUrlState } from "$lib/map/urlState";

const MOBILE_MAP_PRELOAD_QUERY = "(max-width: 768px)";

if (browser && window.matchMedia(MOBILE_MAP_PRELOAD_QUERY).matches) {
  void import("maplibre-gl").catch(() => undefined);
}

export const load: PageLoad = async ({ fetch, depends, url }) => {
  depends("weltgewebe:domain-data");
  const apiUrl = import.meta.env.PUBLIC_GEWEBE_API_BASE ?? "";
  const runtime = {
    isDevelopment: import.meta.env.DEV,
    isProduction: import.meta.env.PROD,
    testProxyEnabled: import.meta.env.VITE_PUBLIC_ENABLE_TEST_MAP === "true",
  } as const;
  // Keep the cursor loader split from the initial map chunk; the build enforces this startup budget.
  const { loadMapResources } = await import("$lib/map/cursorPagination");
  const transport = selectMapResourceTransport(apiUrl, runtime);
  const nodeLoadMode = transport === "cursor" ? "viewport" : "global";
  const focus = parseMapUrlState(url.searchParams).focus;
  const focusedNodeId =
    nodeLoadMode === "viewport" && focus?.type === "node" ? focus.id : null;
  return loadMapResources(fetch, apiUrl, transport, {
    nodeLoadMode,
    focusedNodeId,
  });
};
