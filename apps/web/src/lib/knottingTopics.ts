export const MAX_KNOTTING_TOPICS = 4;

export const KNOTTING_TOPICS = [
  "Wohnen",
  "Ernährung",
  "Mobilität",
  "Energie",
  "Bildung",
  "Gesundheit",
  "Natur",
  "Kunst",
  "Handwerk",
  "Digitales",
  "Nachbarschaft",
  "Mitbestimmung",
] as const;

export type KnottingTopic = (typeof KNOTTING_TOPICS)[number];

const KNOTTING_TOPIC_NAMESPACE = "thema:";

const topicSlug: Record<KnottingTopic, string> = {
  Wohnen: "wohnen",
  Ernährung: "ernährung",
  Mobilität: "mobilität",
  Energie: "energie",
  Bildung: "bildung",
  Gesundheit: "gesundheit",
  Natur: "natur",
  Kunst: "kunst",
  Handwerk: "handwerk",
  Digitales: "digitales",
  Nachbarschaft: "nachbarschaft",
  Mitbestimmung: "mitbestimmung",
};

const topicOrder = new Map<KnottingTopic, number>(
  KNOTTING_TOPICS.map((topic, index) => [topic, index]),
);

function normalizeTag(value: string): string {
  return value.normalize("NFKC").replace(/\s+/gu, " ").trim();
}

export function knottingTopicTag(topic: KnottingTopic): string {
  return `${KNOTTING_TOPIC_NAMESPACE}${topicSlug[topic]}`;
}

const topicByLabelIdentity = new Map<string, KnottingTopic>(
  KNOTTING_TOPICS.map((topic) => [normalizeTag(topic), topic]),
);

const topicByTagIdentity = new Map<string, KnottingTopic>(
  KNOTTING_TOPICS.map((topic) => [
    normalizeTag(knottingTopicTag(topic)),
    topic,
  ]),
);

/** Resolve only the stable machine tag, never a coincidentally equal free label. */
export function knottingTopicForTag(value: string): KnottingTopic | null {
  return topicByTagIdentity.get(normalizeTag(value)) ?? null;
}

/**
 * Canonicalize selector values. This accepts both visible labels and stable
 * machine tags so callers can safely normalize UI state and persisted state.
 */
export function canonicalizeKnottingTopics(
  values: readonly string[],
): KnottingTopic[] {
  const seen = new Set<KnottingTopic>();
  for (const value of values) {
    const identity = normalizeTag(value);
    const topic =
      topicByLabelIdentity.get(identity) ?? topicByTagIdentity.get(identity);
    if (topic) seen.add(topic);
  }
  return [...seen]
    .sort(
      (left, right) =>
        (topicOrder.get(left) ?? 0) - (topicOrder.get(right) ?? 0),
    )
    .slice(0, MAX_KNOTTING_TOPICS);
}

/**
 * Split the existing `tags` array into controlled knotting topics and free
 * keywords. Controlled topics use a stable `thema:<slug>` namespace, so an old
 * or deliberately free keyword such as `Natur` remains a keyword instead of
 * being silently reinterpreted as the controlled topic `thema:natur`.
 *
 * If externally written data contains more than four controlled topic tags,
 * the first four persisted machine tags remain selectable (displayed in
 * canonical catalogue order) and overflow tags are preserved in `keywords`.
 * That makes edit round-trips stable without deleting excess external data.
 */
export function splitKnottingTags(values: readonly string[]): {
  topics: KnottingTopic[];
  keywords: string[];
} {
  const seen = new Set<string>();
  const topics: KnottingTopic[] = [];
  const keywords: string[] = [];

  for (const value of values) {
    const tag = normalizeTag(value);
    if (!tag || seen.has(tag)) continue;
    seen.add(tag);

    const topic = knottingTopicForTag(tag);
    if (topic && topics.length < MAX_KNOTTING_TOPICS) {
      topics.push(topic);
    } else {
      keywords.push(tag);
    }
  }

  topics.sort(
    (left, right) =>
      (topicOrder.get(left) ?? 0) - (topicOrder.get(right) ?? 0),
  );
  return { topics, keywords };
}

/**
 * Persist controlled topics first, followed by free keywords. The server keeps
 * its established `tags: string[]` contract; the namespace supplies the stable
 * semantic identity without a database migration.
 */
export function combineKnottingTags(
  topics: readonly string[],
  keywords: readonly string[],
): string[] {
  const canonicalTopics = canonicalizeKnottingTopics(topics);
  const result: string[] = canonicalTopics.map(knottingTopicTag);
  const seen = new Set<string>(result);
  for (const value of keywords) {
    const tag = normalizeTag(value);
    if (!tag || seen.has(tag)) continue;
    seen.add(tag);
    result.push(tag);
  }
  return result;
}

/**
 * Defensive read-side projection for externally written nodes. All tags remain,
 * but recognised controlled topics are moved to the front in canonical order
 * before the weave chooses its four visible X colours. Legacy free tags keep
 * their historical order and identity.
 */
export function prioritizeKnottingTopics(values: readonly string[]): string[] {
  const { topics, keywords } = splitKnottingTags(values);
  return combineKnottingTags(topics, keywords);
}

/** Human-readable text for search and other presentation-only contexts. */
export function knottingTagDisplayLabel(value: string): string {
  return knottingTopicForTag(value) ?? normalizeTag(value);
}

/**
 * Toggle one topic without letting click order create hidden priority.
 * The returned list always follows the canonical catalogue order because the
 * first four selected topics receive the four visible X arms in the weave.
 */
export function toggleKnottingTopic(
  selected: readonly KnottingTopic[],
  topic: KnottingTopic,
): KnottingTopic[] {
  const next = new Set(selected);
  if (next.has(topic)) {
    next.delete(topic);
  } else if (next.size < MAX_KNOTTING_TOPICS) {
    next.add(topic);
  }
  return KNOTTING_TOPICS.filter((candidate) => next.has(candidate));
}
