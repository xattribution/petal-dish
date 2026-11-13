import assert from 'node:assert/strict';
import {build,sectorGauge,bounds,volume} from '../dist/geometry.js';
import {sectorGaugeGuide,kit} from '../dist/exports.js';
// Petal Z scale: only side-printed parts grow along print Z; flat parts and the assembly are unchanged.
const a=build({}),b=build({zScale:101.5});
for(const kind of ['panel','hub']){const pa=a.parts.find(p=>p.kind===kind),pb=b.parts.find(p=>p.kind===kind),sa=bounds(pa.output).size,sb=bounds(pb.output).size;
 assert.deepEqual(pa.mesh.v.length,pb.mesh.v.length);assert(Math.abs(volume(pa.mesh)-volume(pb.mesh))<1e-6,kind+' assembly mesh unchanged');
 if(kind==='panel'){assert(Math.abs(sb[2]/sa[2]-1.015)<1e-9,'petal print height scales');assert(Math.abs(sb[0]-sa[0])<1e-6&&Math.abs(sb[1]-sa[1])<1e-6,'petal print X/Y unchanged');}
 else assert.deepEqual(sa,sb,'hub print unchanged');}
// The gauge: nominal extents at 100%, file height scaled, and the guide's formula returns the scale that would make the
// measured height match the measured length's XY factor.
const g=sectorGauge(b);assert.equal(g.id,'sector-scale-gauge');assert(Math.abs(g.file.z/g.nominal.z-1.015)<1e-9);assert(Math.abs(g.file.x-g.nominal.x)<1e-9);
// A printer with XY factor kxy and Z factor kz prints the gauge at file.z·kz tall; the guide's value s' must make a petal
// printed at s' come out with height nominal.z·s'·kz = nominal.z·kxy (uniform shrink, so every angle is right).
for(const [kxy,kz] of [[.998,.994],[1,1.003],[.995,.995]]){const measured={x:g.nominal.x*kxy,z:g.file.z*kz},next=100*(measured.x/g.nominal.x)*(g.file.z/measured.z);
 assert(Math.abs(g.nominal.z*next/100*kz-g.nominal.z*kxy)<1e-9,'corrected scale gives uniform shrink');}
{const at=build({zScale:100}),g0=sectorGauge(at),kxy=.998,kz=kxy/1.015,next=100*(g0.nominal.x*kxy/g0.nominal.x)*(g0.file.z/(g0.file.z*kz));assert(Math.abs(next-101.5)<1e-9,'from 100% it recovers the printer correction');}
assert(sectorGaugeGuide(g).includes(g.nominal.x.toFixed(2)));
const names=new TextDecoder('latin1').decode(await kit(a).arrayBuffer());assert(names.includes('FIT_TEST/sector-scale-gauge.stl')&&names.includes('FIT_TEST/SECTOR-GAUGE.md'));
console.log('PASS petal Z scale scales side prints only; sector gauge and its guide');
