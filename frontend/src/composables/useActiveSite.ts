import { computed } from "vue";
import { useRoute, useRouter } from "vue-router";

/** Canonical navigation links carrying the active `?site=` query so the
 *  selection survives tab navigation (shared by the App navbar). */
export function buildNavLinks(siteId: string): { path: string; query: Record<string, string | undefined>; label: string }[] {
  const siteQuery = siteId !== "krnovo" ? { site: siteId } : { site: undefined };
  return [
    { path: "/", query: siteQuery, label: "Forecast" },
    { path: "/history", query: siteQuery, label: "Historical runs" },
    { path: "/actuals", query: siteQuery, label: "Actuals & accuracy" },
    { path: "/availability", query: siteQuery, label: "Availability" },
    { path: "/sites", query: {}, label: "Sites" },
  ];
}

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
