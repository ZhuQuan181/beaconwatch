/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** Optional Bearer token for a private single-tenant dashboard build. */
  readonly VITE_API_TOKEN?: string;
  /** "1" in the static GitHub Pages build. */
  readonly VITE_MOCK?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
