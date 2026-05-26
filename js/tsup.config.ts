import { defineConfig } from "tsup";

// tsup produces a dual-format build: ESM (`.js`) + CommonJS (`.cjs`) +
// TypeScript declarations (`.d.ts`). The three-piece output is what the
// `exports` map in package.json points at, so consumers can `import` from
// modern bundlers and `require()` from legacy CJS without separate builds.
export default defineConfig({
  entry: ["src/index.ts"],
  format: ["esm", "cjs"],
  dts: true,
  sourcemap: true,
  clean: true,
  target: "es2022",
  splitting: false,
  treeshake: true,
  outDir: "dist",
});
