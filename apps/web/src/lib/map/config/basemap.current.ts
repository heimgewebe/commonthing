// The active basemap mode and sovereign variant are decided at build time by
// scripts/generate-basemap-config.js. A local-sovereign generated module carries
// no remote (CARTO) URL. Nationwide Germany is the normal sovereign contract;
// the Hamburg + Schleswig-Holstein variant is retained only as an explicit rollback.

import {
  BUILD_BASEMAP_CONFIG,
  type LocalBasemapVariant,
} from "../../generated/basemapConfig";
import {
  BASEMAP_MODE_POLICY,
  type BasemapMode,
} from "../../generated/basemapModePolicy";
import { MAP_MAX_ZOOM, MAP_MIN_ZOOM } from "../markerScale";

export type { BasemapMode, LocalBasemapVariant };

type BaseBasemapConfig = {
  center: [number, number];
  zoom: number;
  minZoom?: number;
  maxZoom?: number;
  pitch?: number;
  bearing?: number;
};

export type RemoteStyleBasemapConfig = BaseBasemapConfig & {
  mode: "remote-style";
  styleUrl: string;
  /** Optional dark basemap style; when absent the resolver maps Voyager → Dark Matter. */
  darkStyleUrl?: string;
};

export type LocalSovereignBasemapConfig = BaseBasemapConfig & {
  mode: "local-sovereign";
  // Optional for compatibility with callers created before the nationwide
  // variant existed. The resolver treats absence as the nationwide Germany path.
  variant?: LocalBasemapVariant;
  styleUrl?: never;
  darkStyleUrl?: never;
};

export type BasemapConfig =
  | RemoteStyleBasemapConfig
  | LocalSovereignBasemapConfig;

export interface BasemapInitialView {
  center: [number, number];
  zoom: number;
  minZoom: number;
}

const WORLD_INITIAL_VIEW: BasemapInitialView = {
  center: [0, 0],
  zoom: 1,
  minZoom: 1,
};

const GERMANY_INITIAL_VIEW: BasemapInitialView = {
  center: [10.4515, 51.1657],
  zoom: MAP_MIN_ZOOM,
  minZoom: MAP_MIN_ZOOM,
};

const REGIONAL_INITIAL_VIEW: BasemapInitialView = {
  center: [9.9, 54.2],
  zoom: MAP_MIN_ZOOM,
  minZoom: MAP_MIN_ZOOM,
};

/**
 * Keeps the empty-scene viewport honest about the selected basemap coverage.
 * Only the remote style is treated as worldwide; sovereign variants stay on
 * their actual Germany or Hamburg + Schleswig-Holstein operating region.
 */
export function resolveBasemapInitialView(
  mode: BasemapMode,
  variant?: LocalBasemapVariant,
): BasemapInitialView {
  if (mode === "remote-style") return WORLD_INITIAL_VIEW;
  return variant === "regional" ? REGIONAL_INITIAL_VIEW : GERMANY_INITIAL_VIEW;
}

export function resolveBasemapMode(
  envMode: string | undefined,
  isLocalContext: boolean,
): BasemapMode {
  if (BASEMAP_MODE_POLICY.allowedModes.includes(envMode as BasemapMode)) {
    return envMode as BasemapMode;
  }
  return isLocalContext ? "local-sovereign" : "remote-style";
}

const initialView = resolveBasemapInitialView(
  BUILD_BASEMAP_CONFIG.mode,
  BUILD_BASEMAP_CONFIG.mode === "local-sovereign"
    ? BUILD_BASEMAP_CONFIG.variant
    : undefined,
);

const baseConfig: BaseBasemapConfig = {
  ...initialView,
  maxZoom: MAP_MAX_ZOOM,
};

export const currentBasemap: BasemapConfig =
  BUILD_BASEMAP_CONFIG.mode === "remote-style"
    ? {
        ...baseConfig,
        mode: "remote-style",
        styleUrl: BUILD_BASEMAP_CONFIG.styleUrl,
        ...(BUILD_BASEMAP_CONFIG.darkStyleUrl
          ? { darkStyleUrl: BUILD_BASEMAP_CONFIG.darkStyleUrl }
          : {}),
      }
    : {
        ...baseConfig,
        mode: "local-sovereign",
        variant: BUILD_BASEMAP_CONFIG.variant,
      };
