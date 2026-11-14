import {
  combineKnottingTags,
  knottingTagDisplayLabel,
  prioritizeKnottingTopics,
  splitKnottingTags,
} from "$lib/knottingTopics";
import type {
  MapEntityNode,
  MapEntityWebgemeindezentrum,
} from "$lib/map/types";

export const WEAVE_FALLBACK_COLOR = "#76523d";

const COLORS = [
  "#5f7a55",
  "#4f6682",
  "#765a91",
  "#b37a3e",
  "#9c5547",
  "#397572",
  "#af8d37",
  "#78523f",
  "#65705f",
] as const;
const IGNORED_THEMES = new Set([
  "account",
  "demo",
  "garnrolle",
  "knoten",
  "node",
  "webgemeindezentrum",
]);

export type WeaveEntity = MapEntityNode | MapEntityWebgemeindezentrum;

/** Display-only truncation limit. It never reaches topic identity. */
export const WEAVE_TOPIC_DISPLAY_MAX_LENGTH = 42;

/**
 * Only these exact technical namespaces may be stripped for the final visible
 * label. Generic "word:" prefixes stay — meaningful topics such as
 * `kunst:öffentlicher raum` must not lose their colon-bearing identity text.
 * Identity, deduplication, hash, colour and segment ids always keep the full
 * normalised text, including any colon.
 */
const TECHNICAL_NAMESPACE_ALLOWLIST = new Set(["thema"]);

/**
 * FNV-1a over Unicode code points. Iterating the string already yields full
 * code points (including supplementary-plane characters); hashing must not fall
 * back to UTF-16 code units via `charCodeAt`, or emoji/astral topics would only
 * fold the high surrogate into the colour.
 */
function hash(value: string): number {
  let result = 2166136261;
  for (const character of value) {
    const codePoint = character.codePointAt(0);
    if (codePoint === undefined) continue;
    result = Math.imul(result ^ codePoint, 16777619);
  }
  return result >>> 0;
}

// One shared segmenter: constructing an Intl.Segmenter per call dominated the
// weave projection (hundreds of ms per map load on a throttled mobile CPU).
// `segment()` keeps no state between calls, so reuse is safe.
let graphemeSegmenter: Intl.Segmenter | null | undefined;

function getGraphemeSegmenter(): Intl.Segmenter | null {
  if (graphemeSegmenter === undefined) {
    graphemeSegmenter =
      typeof Intl !== "undefined" && "Segmenter" in Intl
        ? new Intl.Segmenter(undefined, { granularity: "grapheme" })
        : null;
  }
  return graphemeSegmenter;
}

function countGraphemes(value: string): number {
  const segmenter = getGraphemeSegmenter();
  if (segmenter) return Array.from(segmenter.segment(value)).length;
  // Fallback: code points, still safer than UTF-16 code units for astral text.
  return Array.from(value).length;
}

function takeGraphemes(value: string, maxGraphemes: number): string {
  if (maxGraphemes <= 0) return "";
  const segmenter = getGraphemeSegmenter();
  if (segmenter) {
    let taken = "";
    let count = 0;
    for (const { segment } of segmenter.segment(value)) {
      if (count >= maxGraphemes) break;
      taken += segment;
      count += 1;
    }
    return taken;
  }
  return Array.from(value).slice(0, maxGraphemes).join("");
}

/**
 * Canonical text of one topic: compatibility-normalised, whitespace-unified and
 * trimmed. No prefix stripping and no case folding — identity, hash, colour and
 * segment ids all consume this exact form.
 */
export function normalizeWeaveTopicText(value: string): string {
  return value.normalize("NFKC").replace(/\s+/gu, " ").trim();
}

/**
 * Identity of one topic. Exact user contract:
 * `value.normalize('NFKC').replace(/\s+/g, ' ').trim()`.
 * Never shortened, never case-folded, never prefix-stripped. Ignore/compare
 * helpers may still fold case separately.
 */
export function weaveTopicIdentity(label: string): string {
  return normalizeWeaveTopicText(label);
}

/** Case-insensitive membership in the technical ignore set only. */
function isIgnoredThemeIdentity(identity: string): boolean {
  return IGNORED_THEMES.has(identity.toLocaleLowerCase("de-DE"));
}

/**
 * Drop only an allowlisted technical namespace for display. Meaningful colons
 * (`Kunst: Öffentlicher Raum`, `kunst:öffentlicher raum`) stay intact.
 */
function stripAllowlistedTechnicalNamespace(value: string): string {
  const colon = value.indexOf(":");
  if (colon <= 0) return value;
  const namespace = value.slice(0, colon);
  if (!TECHNICAL_NAMESPACE_ALLOWLIST.has(namespace)) return value;
  const remainder = value.slice(colon + 1).trim();
  return remainder || value;
}

/**
 * Shortening is a late presentation decision and carries no identity.
 * Only at this stage may an allowlisted technical namespace be dropped and the
 * remaining grapheme clusters truncated for the visible label.
 */
export function weaveTopicDisplayLabel(label: string): string {
  const normalised = normalizeWeaveTopicText(label);
  const controlledLabel = knottingTagDisplayLabel(normalised);
  const withoutNoise =
    controlledLabel === normalised
      ? stripAllowlistedTechnicalNamespace(normalised)
      : controlledLabel;
  const display = withoutNoise || normalised;
  // A grapheme spans at least one UTF-16 code unit, so a string that is short
  // in code units is short in graphemes too; only longer labels are segmented.
  if (display.length <= WEAVE_TOPIC_DISPLAY_MAX_LENGTH) return display;
  if (countGraphemes(display) <= WEAVE_TOPIC_DISPLAY_MAX_LENGTH) return display;
  const body = takeGraphemes(
    display,
    Math.max(0, WEAVE_TOPIC_DISPLAY_MAX_LENGTH - 1),
  ).trimEnd();
  return `${body}…`;
}

/**
 * Full normalised visual topic texts, deduplicated by identity and never
 * truncated. Once a node has at least one controlled knotting topic, those
 * selected topics are the complete colour authority: free keywords and the
 * node kind remain descriptive/search metadata and must not silently add a
 * second visual meaning. Nodes without controlled topics keep the previous
 * keyword/kind fallback so externally written or older data remains visible
 * without inventing a migration.
 */
export function weaveTopics(entity: WeaveEntity): string[] {
  let raw: Array<string | null | undefined>;
  if (entity.type === "webgemeindezentrum") {
    raw = ["Gemeinschaft", "Mitentscheiden"];
  } else {
    const { topics } = splitKnottingTags(entity.tags ?? []);
    const controlledTopicTags = combineKnottingTags(topics, []);
    raw = controlledTopicTags.length
      ? controlledTopicTags
      : [...prioritizeKnottingTopics(entity.tags ?? []), entity.kind];
  }
  const seen = new Set<string>();
  const result: string[] = [];
  for (const value of raw) {
    if (typeof value !== "string") continue;
    const label = normalizeWeaveTopicText(value);
    const key = weaveTopicIdentity(label);
    if (!label || isIgnoredThemeIdentity(key) || seen.has(key)) continue;
    seen.add(key);
    result.push(label);
  }
  return result.length ? result : ["Gemeingut"];
}

/** Testable FNV-1a of the topic identity over Unicode code points. */
export function weaveTopicHash(label: string): number {
  return hash(weaveTopicIdentity(label));
}

export function weaveTopicColor(label: string): string {
  return COLORS[weaveTopicHash(label) % COLORS.length];
}

export function primaryWeaveColor(entity: WeaveEntity): string {
  return weaveTopicColor(weaveTopics(entity)[0] ?? "Gemeingut");
}
