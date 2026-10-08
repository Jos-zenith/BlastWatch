import { locale } from "./i18n.js";

export const LEVEL_COLORS = { Low: "#2e9e5b", Moderate: "#d99a1b", High: "#d14343" };
export const ACTION_COLORS = { alert: "#d14343", watch: "#d99a1b", none: "#2e9e5b", unknown: "#8a948f" };
export const NO_DATA = "#8a948f";
export const RANK = { Low: 0, Moderate: 1, High: 2 };

export const dayName = (iso) => new Date(iso + "T00:00:00").toLocaleDateString(locale(), { weekday: "short", day: "numeric" });
export const dayLong = (iso) => new Date(iso + "T00:00:00").toLocaleDateString("en-IN", { weekday: "short", day: "numeric", month: "short" });
export const dateTime = (iso) => new Date(iso).toLocaleString(locale(), { weekday: "short", day: "numeric", month: "short", hour: "numeric", minute: "2-digit" });
export const hourLabel = (iso) => new Date(iso).toLocaleString(locale(), { weekday: "short", hour: "numeric" });
export const clock = (iso) => new Date(iso).toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
export const pct = (p) => `${Math.round(p * 100)}%`;
export const todayIso = () => new Date().toLocaleDateString("en-CA");
export const cssVar = (name, fallback) => getComputedStyle(document.body).getPropertyValue(name).trim() || fallback;
