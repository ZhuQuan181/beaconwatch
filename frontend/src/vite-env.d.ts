/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** Optional Bearer token for a private single-tenant dashboard build. */
  readonly VITE_API_TOKEN?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
