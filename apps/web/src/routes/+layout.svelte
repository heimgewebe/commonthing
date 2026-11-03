<script lang="ts">
  import "../app.css";
  import "$lib/styles/tokens.css";
  import { onMount } from "svelte";
  import { ensureInertPolyfill } from "$lib/utils/inert-polyfill";
  import { setUAClasses } from "$lib/utils/ua-flags";
  import type { LayoutData } from "./$types";
  import { updateStore } from "$lib/stores/updateStore";
  import UpdateBanner from "$lib/components/UpdateBanner.svelte";

  interface Props {
    data: LayoutData;
    children?: import("svelte").Snippet;
  }

  let { data, children }: Props = $props();

  onMount(() => {
    setUAClasses();
    const q = new URLSearchParams(window.location.search);
    const disable =
      q.get("noinert") === "1" || (window as any).__NO_INERT__ === true;
    if (!disable) ensureInertPolyfill();
    updateStore.init();
  });
</script>

<svelte:head>
  <title>commonThing – Commons gemeinsam verwalten</title>
  <meta
    name="description"
    content="commonThing macht Commons, Knoten, Garnrollen und ihre Beziehungen auf einer gemeinsamen Karte sichtbar und verwaltbar."
  />
  <meta name="application-name" content="commonThing" />
  <meta property="og:site_name" content="commonThing" />
  <link rel="manifest" href="/manifest.webmanifest" />
  <link rel="icon" href="/favicon.svg" type="image/svg+xml" />
  {#if data?.canonical}
    <link rel="canonical" href={data.canonical} />
  {/if}
</svelte:head>

<UpdateBanner />

{@render children?.()}
