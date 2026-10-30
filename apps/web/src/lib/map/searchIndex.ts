import { knottingTagDisplayLabel } from "$lib/knottingTopics";
import { nodeKindLabel } from "$lib/ui/productLanguage";
import type { MapEntityViewModel } from "$lib/map/types";

const normalizedTermsByEntity = new WeakMap<
  MapEntityViewModel,
  readonly string[]
>();

export function normalizeMapSearchTerm(term: string): string {
  return term.toLocaleLowerCase("de-DE");
}

function buildMapSearchTermsNormalized(
  marker: MapEntityViewModel,
): readonly string[] {
  const semanticTerms =
    marker.type === "node"
      ? ["Knoten", marker.kind, nodeKindLabel(marker.kind)]
      : marker.type === "webgemeindezentrum"
        ? [
            "Webgemeindezentrum",
            "Treffpunkt",
            marker.location_state_label,
            marker.location_label,
            marker.ortsweberei.name,
          ]
        : ["Garnrolle", "Garnrollen"];
  const tagTerms = (marker.tags ?? []).flatMap((tag) => {
    const display = knottingTagDisplayLabel(tag);
    return display === tag ? [tag] : [tag, display];
  });

  return [marker.title, marker.summary, ...tagTerms, ...semanticTerms]
    .filter((term): term is string => typeof term === "string")
    .map(normalizeMapSearchTerm);
}

/**
 * Cache normalized search terms by immutable map-entity identity. Scene rebuilds
 * create new entity objects, so changed domain data receives a fresh index. The
 * WeakMap does not retain entities after their scene becomes unreachable.
 */
export function getMapSearchTermsNormalized(
  marker: MapEntityViewModel,
): readonly string[] {
  const cached = normalizedTermsByEntity.get(marker);
  if (cached) return cached;

  const terms = buildMapSearchTermsNormalized(marker);
  normalizedTermsByEntity.set(marker, terms);
  return terms;
}
