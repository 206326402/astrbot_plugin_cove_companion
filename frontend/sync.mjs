import {cp, mkdir, readdir, unlink} from 'node:fs/promises';
const source = new URL('../pages/companion/', import.meta.url);
const destination = new URL('../views/companion/', import.meta.url);
await mkdir(destination, {recursive: true});
await cp(source, destination, {recursive: true});
const sourceAssets = new URL('assets/', source);
const targetAssets = new URL('assets/', destination);
const published = new Set(await readdir(sourceAssets));
for (const entry of await readdir(targetAssets, {withFileTypes: true})) {
  if (entry.isFile() && /\.(js|css)$/.test(entry.name) && !published.has(entry.name)) {
    await unlink(new URL(entry.name, targetAssets));
  }
}
