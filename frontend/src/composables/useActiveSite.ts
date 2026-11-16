import { computed } from "vue";
import { useRoute, useRouter } from "vue-router";

/** Durable lightweight active-site selection: the `?site=` URL query, so the
 *  choice survives refresh and is shareable. Falls back to the default site. */
export function useActiveSite() {
  const route = useRoute();
  const router = useRouter();

  const siteId = computed<string>(() => {
    const q = route.query.site;
    const value = Array.isArray(q) ? q[0] : q;
    return typeof value === "string" && value.trim() !== "" ? value : "krnovo";
  });

  function setSite(id: string): void {
    void router.push({ query: { ...route.query, site: id === "krnovo" ? undefined : id } });
  }

  return { siteId, setSite };
}
