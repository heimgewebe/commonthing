<script lang="ts">
  let { word }: { word: "Login" | "Einstellungen" } = $props();
  // Attach to the wrapper: Svelte emits inline replay handlers for img actions.
  function trackImages(root: HTMLElement) {
    const cleanups = Array.from(root.querySelectorAll("img"), (image) => {
      const sync = () =>
        image.classList.toggle(
          "loaded",
          image.complete && image.naturalWidth > 0,
        );
      image.addEventListener("load", sync);
      image.addEventListener("error", sync);
      sync();
      return () => {
        image.removeEventListener("load", sync);
        image.removeEventListener("error", sync);
      };
    });
    return { destroy: () => cleanups.forEach((cleanup) => cleanup()) };
  }
</script>

<span class="yarn" class:login={word === "Login"} use:trackImages>
  <img
    class="light"
    src={`/garnschrift-c/${word.toLowerCase()}-light.png`}
    width={word === "Login" ? 325 : 764}
    height="145"
    alt=""
    aria-hidden="true"
  />
  <img
    class="dark"
    src={`/garnschrift-c/${word.toLowerCase()}-dark.png`}
    width={word === "Login" ? 325 : 764}
    height="145"
    alt=""
    aria-hidden="true"
  />
  <span class="fallback">{word}</span>
</span>

<style>
  .yarn {
    display: grid;
    width: min(22rem, 100%);
  }
  .login {
    width: min(9rem, 100%);
  }
  .fallback,
  img {
    grid-area: 1 / 1;
  }
  img {
    width: 100%;
    height: auto;
    opacity: 0;
  }
  @media (forced-colors: none) {
    :global([data-color-scheme="light"]) .light:global(.loaded),
    :global([data-color-scheme="dark"]) .dark:global(.loaded) {
      opacity: 1;
    }
    :global([data-color-scheme="light"]) .light:global(.loaded) ~ .fallback,
    :global([data-color-scheme="dark"]) .dark:global(.loaded) ~ .fallback {
      opacity: 0;
    }
  }
</style>
