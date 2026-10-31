import {defineConfig} from 'vitest/config';
import react from '@vitejs/plugin-react';
export default defineConfig({plugins:[react()],test:{environment:'jsdom',setupFiles:'./src/test/setup.ts'},base:'./',build:{outDir:'../pages/companion',emptyOutDir:true,cssCodeSplit:false,rollupOptions:{output:{inlineDynamicImports:true,entryFileNames:'assets/companion-[hash].js',assetFileNames:'assets/[name]-[hash][extname]'}}}});
