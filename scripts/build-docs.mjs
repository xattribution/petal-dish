import fs from 'node:fs';
import {build,defaults} from '../dist/geometry.js';
import {guide,reflectorGuide} from '../dist/exports.js';
import {feedGuide,rodCSV,feedHardwareCSV} from '../dist/feed.js';
const m=build(defaults);
fs.mkdirSync('docs',{recursive:true});
fs.writeFileSync('docs/DEFAULT-ASSEMBLY.md',guide(m));
fs.writeFileSync('docs/REFLECTOR.md',reflectorGuide(m));
console.log('Generated default revision-12 assembly and reflector guides.');

for(const feedMode of [1,2,3]){const f=build({...defaults,feedMode}),name=['','PRIME','CASSEGRAIN','GREGORIAN'][feedMode];fs.writeFileSync('docs/DEFAULT-'+name+'-SUPPORT.md',feedGuide(f));fs.writeFileSync('docs/DEFAULT-'+name+'-RODS.csv',rodCSV(f));fs.writeFileSync('docs/DEFAULT-'+name+'-HARDWARE.csv',feedHardwareCSV(f));}
