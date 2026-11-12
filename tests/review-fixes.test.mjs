import assert from 'node:assert/strict';
import {build,connectionCoupon} from '../dist/geometry.js';
import {connectionCatalog} from '../dist/connections.js';
import {manifest,guide} from '../dist/exports.js';
// Joint IDs: with 6 petals, odd diameters put seam endpoints exactly on a 0.05 mm rounding boundary; both petals
// sharing a seam must still produce the same ID, so overrides and fit strips work.
for(const diameter of [341,383]){const m=build({diameter}),seams=connectionCatalog(m).filter(j=>j.family==='radial');
 assert.equal(seams.length,6,diameter+' mm: six radial seams');assert(seams.every(j=>j.parts.length===2),diameter+' mm: every seam two-sided');
 assert(connectionCoupon(build({diameter,connections:{families:{radial:{seamJoint:2}}}})).length>0);}
console.log('PASS joint IDs pair across odd diameters; fit strips export with overrides');
// Faceted rear + staggered rings + clips/levers: both halves of a ring seam use the same station levels.
for(const seamJoint of [1,3])build({diameter:560,rearStyle:1,seamJoint});
console.log('PASS faceted rear with staggered rings and clips or levers');
// Overrides that remove every clip, or bring in levers: manifest and guide follow the kit, not the global choice.
{const m=build({seamJoint:1,connections:{families:{radial:{seamJoint:0}}}}),x=manifest(m);assert.equal(x.clip,null);assert.equal(x.seam_fastening,'bolts');}
{const m=build({connections:{families:{radial:{seamJoint:3,seamBolt:3}}}}),g=guide(m),x=manifest(m);assert(/TPU/.test(g),'lever steps in the guide');assert(x.seam_levers&&x.seam_levers.installed>0);assert.equal(x.seam_fastening,'seam levers');}
console.log('PASS manifest and guide describe the hardware the overrides actually use');
