import {fileURLToPath} from 'node:url';
import {readFile} from 'node:fs/promises';
const web=new URL('../Parser/ErCore/web/',import.meta.url);
export async function resolve(specifier,context,next){
  if(specifier==='three')return {url:new URL('vendor/three/build/three.module.js',web).href,shortCircuit:true};
  if(specifier.startsWith('three/addons/'))return {url:new URL('vendor/three/examples/jsm/'+specifier.slice(13),web).href,shortCircuit:true};
  return next(specifier,context);
}
export async function load(url,context,next){
  if(url.startsWith(web.href)&&new URL(url).pathname.endsWith('.js'))return {format:'module',source:await readFile(fileURLToPath(url),'utf8'),shortCircuit:true};
  return next(url,context);
}
