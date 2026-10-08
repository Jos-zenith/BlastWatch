import { createRouter, createWebHistory } from "vue-router";
import OfficerView from "./views/OfficerView.vue";

export const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: "/", component: OfficerView, meta: { title: "BlastWatch", sub: "tagline" } },
    { path: "/live", component: () => import("./views/LiveView.vue"),
      meta: { title: "BlastWatch Live", sub: "Live operations · every reading, refresh and risk change as it happens" } },
    { path: "/confidence", component: () => import("./views/ConfidenceView.vue"),
      meta: { title: "Forecast Confidence", sub: "Forecast confidence · 122 ensemble forecasts per district, each scored with the same rules" } },
    { path: "/research", component: () => import("./views/ResearchView.vue"),
      meta: { title: "BlastWatch Research", sub: "Research context and validation status" } },
    // Links to the old multi-page site keep working.
    { path: "/index.html", redirect: "/" },
    { path: "/live.html", redirect: "/live" },
    { path: "/ensemble.html", redirect: "/confidence" },
    { path: "/research.html", redirect: "/research" },
    { path: "/:rest(.*)*", redirect: "/" },
  ],
});

router.afterEach((to) => { document.title = to.meta.title || "BlastWatch"; });
