import {
  KNOTTING_TOPICS,
  splitKnottingTags,
  type KnottingTopic,
} from "$lib/knottingTopics";
import type { MapEntityViewModel } from "$lib/map/types";

/**
 * Explicit filter axes for the loaded map projection. Empty sets mean "all"
 * on that axis. Further axes can be added here when their real data contract
 * exists; no inferred activity, proximity or recency fields live in this model.
 */
export type MapContentFilters = {
  contentTypes: ReadonlySet<string>;
  topics: ReadonlySet<KnottingTopic>;
};

export type MapFilterOption<T extends string = string> = {
  id: T;
  label: string;
  count: number;
};

export type MapContentFilterEvaluation = {
  entities: MapEntityViewModel[];
  contentTypes: MapFilterOption[];
  topics: MapFilterOption<KnottingTopic>[];
  totalCount: number;
  allTopicsCount: number;
  activeCount: number;
};

export function createEmptyMapContentFilters(): MapContentFilters {
  return { contentTypes: new Set(), topics: new Set() };
}

export function countActiveMapContentFilters(
  filters: MapContentFilters,
): number {
  return filters.contentTypes.size + filters.topics.size;
}

export function hasActiveMapContentFilters(
  filters: MapContentFilters,
): boolean {
  return countActiveMapContentFilters(filters) > 0;
}

/** The existing content-type bucket: node kind, Garnrolle or center. */
export function getMapContentType(entity: MapEntityViewModel): string {
  if (entity.type === "node") return entity.kind || "Knoten";
  if (entity.type === "webgemeindezentrum") return "Webgemeindezentrum";
  return "Garnrolle";
}

/**
 * Canonical controlled topics already stored in the entity's `tags` array.
 * Free keywords that merely resemble a topic are intentionally not promoted.
 */
export function getMapContentTopics(
  entity: MapEntityViewModel,
): KnottingTopic[] {
  return splitKnottingTags(entity.tags ?? []).topics;
}

function matchesContentType(
  contentType: string,
  selected: ReadonlySet<string>,
): boolean {
  return selected.size === 0 || selected.has(contentType);
}

function matchesTopics(
  topics: readonly KnottingTopic[],
  selected: ReadonlySet<KnottingTopic>,
): boolean {
  if (selected.size === 0) return true;
  for (const topic of topics) {
    if (selected.has(topic)) return true;
  }
  return false;
}

export function matchesMapContentFilters(
  entity: MapEntityViewModel,
  filters: MapContentFilters,
): boolean {
  return (
    matchesContentType(getMapContentType(entity), filters.contentTypes) &&
    matchesTopics(getMapContentTopics(entity), filters.topics)
  );
}

/**
 * Evaluate visibility and both facet count sets from the same loaded map data.
 * Multiple values within one axis are ORed; the two axes are ANDed.
 *
 * Counts apply the opposite axis only: type counts respect selected topics,
 * while topic counts respect selected content types. This keeps zero-count
 * choices visible and makes every number a truthful "available here" count.
 */
export function evaluateMapContentFilters(
  entities: MapEntityViewModel[],
  filters: MapContentFilters,
): MapContentFilterEvaluation {
  const contentTypeCounts = new Map<string, number>();
  const topicCounts = new Map<KnottingTopic, number>();
  for (const contentType of filters.contentTypes) {
    contentTypeCounts.set(contentType, 0);
  }
  for (const topic of filters.topics) topicCounts.set(topic, 0);

  const visible: MapEntityViewModel[] = [];
  let allTopicsCount = 0;
  for (const entity of entities) {
    const contentType = getMapContentType(entity);
    const topics = getMapContentTopics(entity);
    if (!contentTypeCounts.has(contentType)) {
      contentTypeCounts.set(contentType, 0);
    }
    for (const topic of topics) {
      if (!topicCounts.has(topic)) topicCounts.set(topic, 0);
    }

    const typeMatches = matchesContentType(contentType, filters.contentTypes);
    const topicsMatch = matchesTopics(topics, filters.topics);

    if (typeMatches && topicsMatch) visible.push(entity);
    if (typeMatches) allTopicsCount += 1;
    if (topicsMatch) {
      contentTypeCounts.set(
        contentType,
        (contentTypeCounts.get(contentType) ?? 0) + 1,
      );
    }
    if (typeMatches) {
      for (const topic of topics) {
        topicCounts.set(topic, (topicCounts.get(topic) ?? 0) + 1);
      }
    }
  }

  const contentTypes = Array.from(contentTypeCounts, ([id, count]) => ({
    id,
    label: id.charAt(0).toUpperCase() + id.slice(1),
    count,
  })).sort((left, right) => left.label.localeCompare(right.label, "de"));
  const topics = KNOTTING_TOPICS.filter((topic) => topicCounts.has(topic)).map(
    (topic) => ({
      id: topic,
      label: topic,
      count: topicCounts.get(topic) ?? 0,
    }),
  );

  return {
    entities: visible,
    contentTypes,
    topics,
    totalCount: entities.length,
    allTopicsCount,
    activeCount: countActiveMapContentFilters(filters),
  };
}
