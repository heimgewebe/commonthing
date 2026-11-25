// See https://kit.svelte.dev/docs/types
// for information about these interfaces
declare global {
  namespace App {
    interface PageState {
      // Opaque per-document key; discovery queries remain in memory.
      discoveryHistoryId?: string;
    }
  }
}
export {};
