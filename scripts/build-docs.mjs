import fs from 'node:fs';
import {build,defaults} from '../dist/geometry.js';
import {guide,reflectorGuide} from '../dist/exports.js';
const m=build(defaults);
fs.mkdirSync('docs',{recursive:true});
fs.writeFileSync('docs/DEFAULT-ASSEMBLY.md',guide(m));
fs.writeFileSync('docs/REFLECTOR.md',reflectorGuide(m));
console.log('Generated default revision-9 assembly and reflector guides.');
