// Language state shared by every view. ?lang=ta in a shared link wins over the remembered choice.
import { ref } from "vue";
import { STRINGS } from "./strings.js";

function initial() {
  const fromUrl = new URLSearchParams(location.search).get("lang");
  if (fromUrl in STRINGS) return fromUrl;
  try { return localStorage.getItem("bw-lang") || "en"; } catch { return "en"; }
}

export const lang = ref(initial());

export function setLang(value) {
  lang.value = value;
  document.documentElement.lang = value;
  try { localStorage.setItem("bw-lang", value); } catch { /* storage unavailable */ }
}

export const T = (key) => STRINGS[lang.value][key] ?? STRINGS.en[key] ?? key;
export const locale = () => (lang.value === "ta" ? "ta-IN" : "en-IN");
export const localName = (x) => (lang.value === "ta" && x?.name_ta ? x.name_ta : x?.name);
