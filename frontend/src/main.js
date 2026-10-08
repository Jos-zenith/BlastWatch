import { createApp } from "vue";
import "leaflet/dist/leaflet.css";
import "./style.css";
import App from "./App.vue";
import { router } from "./router.js";

createApp(App).use(router).mount("#app");
