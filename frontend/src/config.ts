export const APP_VERSION = import.meta.env.VITE_APP_VERSION || "dev";

const configuredApiBase = import.meta.env.VITE_API_BASE_URL?.trim();
export const API_BASE_URL = (configuredApiBase || "/api/v1").replace(/\/+$/, "") || "/api/v1";
