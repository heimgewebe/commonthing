<script lang="ts">
  import { createEventDispatcher, onMount } from "svelte";
  import "maplibre-gl/dist/maplibre-gl.css";
  import type {
    FitBoundsOptions,
    LngLatBoundsLike,
    LngLatLike,
    MapOptions,
  } from "maplibre-gl";
  import { initMapContext } from "./context";
  import { loadMapLibre } from "./runtime";

  const dispatch = createEventDispatcher();
  const context = initMapContext();

  interface Props {
    style: string;
    center: LngLatLike | undefined;
    zoom: number | undefined;
    minZoom: number | undefined;
    maxZoom: number | undefined;
    bounds: LngLatBoundsLike | undefined;
    fitBoundsOptions: FitBoundsOptions | undefined;
    attributionControl?: boolean;
    interactive: boolean | undefined;
    options?: Partial<MapOptions>;
    children?: import("svelte").Snippet;
    [key: string]: any;
  }

  let {
    style,
    center,
    zoom,
    minZoom,
    maxZoom,
    bounds,
    fitBoundsOptions,
    attributionControl = false,
    interactive,
    options = {},
    children,
    ...rest
  }: Props = $props();

  let container: HTMLDivElement | undefined = $state();
  let map: import("maplibre-gl").Map | null = $state(null);
  let containerProps: Record<string, unknown> = $derived(rest);

  onMount(() => {
    let destroyed = false;

    const initialise = async () => {
      const maplibreModule = await loadMapLibre();

      if (destroyed) {
        return;
      }

      context.maplibre = maplibreModule;

      if (!container) {
        context.maplibre = null;
        return;
      }

      const initialOptions: MapOptions = {
        container,
        style,
        attributionControl,
        ...options,
      } as MapOptions;

      if (center) {
        initialOptions.center = normalizeLngLat(center);
      }

      if (zoom !== undefined) {
        initialOptions.zoom = zoom;
      }

      if (minZoom !== undefined) {
        initialOptions.minZoom = minZoom;
      }

      if (maxZoom !== undefined) {
        initialOptions.maxZoom = maxZoom;
      }

      if (interactive !== undefined) {
        initialOptions.interactive = interactive;
      }

      map = new maplibreModule.Map(initialOptions);
      context.map.set(map);

      map.on("load", () => dispatch("load", { map }));
      map.on("error", (event: unknown) => dispatch("error", event));

      if (bounds) {
        map.fitBounds(bounds, fitBoundsOptions);
      }
    };

    initialise();

    return () => {
      destroyed = true;
      map?.remove();
      map = null;
      context.map.set(null);
      context.maplibre = null;
    };
  });

  function normalizeLngLat(value: LngLatLike): LngLatLike {
    if (Array.isArray(value)) {
      return value;
    }

    if (typeof value === "object" && value !== null) {
      if ("lng" in value && "lat" in value) {
        return [value.lng as number, value.lat as number];
      }

      if ("lon" in value && "lat" in value) {
        return [value.lon as number, value.lat as number];
      }

      if (
        "toArray" in value &&
        typeof (value as { toArray?: () => LngLatLike }).toArray === "function"
      ) {
        return (value as { toArray: () => LngLatLike }).toArray();
      }
    }

    throw new Error(
      `Invalid LngLatLike value passed to normalizeLngLat: ${JSON.stringify(value)}`,
    );
  }
  $effect(() => {
    if (map && center) {
      map.setCenter(normalizeLngLat(center));
    }
  });
  $effect(() => {
    if (map && zoom !== undefined) {
      map.setZoom(zoom);
    }
  });
  $effect(() => {
    if (map && bounds) {
      map.fitBounds(bounds, fitBoundsOptions);
    }
  });
</script>

<div bind:this={container} {...containerProps}>
  {@render children?.()}
</div>
