import {binarySTL,volume,zip,clampDepth,connectionCoupon,packedPlateMesh} from './geometry.js';
function parametricScadSource(m,kernel){const p=m.p;return `// PETAL / Parametric Expeditionary Tactical Aperture Lab\n// Editable source. Dimensions in mm. No external libraries.\n/* [Dish] */\ndiameter = ${p.diameter}; // [180:1:1200]\nfd = ${p.fd}; // [0.25:0.01:0.80]\nthickness = ${p.thickness}; // [1.6:0.1:6]\n/* [Joints] */\njoint_style = ${p.jointStyle}; // [0:Legacy straps,1:Recessed plates]\njoint_clearance = ${p.jointClearance}; // [0.1:0.05:0.4]\nadaptive_joints = ${p.adaptiveJoints}; // [0:Off,1:On]\nconnector_spacing = ${p.connectorSpacing}; // [60:5:180]\nstagger_rings = ${p.staggerRings}; // [0:Aligned,1:Half-petal stagger]\n/* [Rear construction] */\nrear_style = ${p.rearStyle}; // [0:Curved shell,1:Two-facet rear]\nfacet_angle = ${p.facetAngle}; // [10:1:15]\n/* [Printer] */\nbed_x = ${p.bedX};\nbed_y = ${p.bedY};\nbed_z = ${p.bedZ};\nmargin = ${p.margin};\n/* [Segmentation] */\n// 0 = automatic. Explicit values avoid repeating the search on each render.\nsectors = ${m.layout.n}; // [0:2:32]\nrows = ${m.layout.rows}; // [0:1:12]\ngap = ${p.gap}; // [0.2:0.1:1]\nresolution = ${p.resolution}; // [2:1:10]\n/* [Print orientation and breakaway ribs] */\nsupports = ${p.supports}; // [0:Off,1:On]\nprint_angle = ${p.printAngle}; // -1 auto; 0 low profile; or 45-70 degrees\nrib_count = ${p.ribCount}; // [2:1:5]\ncontact_gap = ${p.contactGap}; // [0.1:0.05:0.4]\ncontact_width = ${p.contactWidth}; // [0.4:0.1:0.8]\nrib_pitch = ${p.ribPitch}; // [6:1:20]\n/* [Export] */\npart = "assembly"; // [assembly,panel,side-bridge,ring-bridge,hub-rear,hub-clamp]\nring = 1; // One-based radial ring index\nstation = 1; // One-based saddle position in this ring\n\n${kernel}`;}
function baseManifest(m){const p=m.p;return {generator:'PETAL 3.2',name:'Parametric Expeditionary Tactical Aperture Lab',units:'mm',parameters:p,segmentation:{petals:m.layout.n,rings:m.layout.rows,staggered:Boolean(p.staggerRings),ring_offsets_degrees:m.ringPhases.map(a=>a*180/Math.PI),adaptive_connectors:Boolean(p.jointStyle&&p.adaptiveJoints),target_connector_spacing_mm:p.connectorSpacing},focal_length:m.focal,dish_depth:m.depth,interface:{revision:p.jointStyle?7:1,type:'custom',joint:p.jointStyle?'Recessed plates: two bolts within rings, four between rings':'Legacy two-bolt straps',hub_outer_diameter:120,front_clamp_outer_diameter:120,center_opening:30,rear_mount_bolt_circle:p.jointStyle?60:40,rear_mount_holes:4,rear_mount_clocking_degrees:45,round_bore_diameter:4.6,hardware:'M4 bolts, nuts and washers',...(p.jointStyle?{seat_depth:1,seat_clearance_per_side:p.jointClearance,nut_pocket_across_flats:7+2*p.jointClearance,nut_pocket_min_depth:3.4,nut_nominal_across_flats:7,nut_nominal_height:3.2,minimum_plate_nut_bearing_wall:2.8,minimum_hub_nut_bearing_wall:2,minimum_panel_wall_at_plate:2.45,cap_head_recess_diameter:8.4,cap_head_max_diameter:8,cap_head_max_height:2.2,cap_root_shoulder_depth:clampDepth(p),root_lip_depth:2,cap_pilot_inner_diameter:78,cap_pilot_outer_diameter:84,axial_assembly_gap:.2}:{})},assembly_bolts:m.bolts,mount_bolts:4,cap_button_head_screws:p.jointStyle?m.layout.n+4:0,captured_nuts:p.jointStyle?m.bolts+4:0,parts:m.parts.map(p=>({file:`${p.id}${p.supportMeshes.length?'_supported':''}_qty-${p.qty}.stl`,quantity:p.qty,scad_part:p.kind==='panel'?'panel':p.id.startsWith('side-bridge')?'side-bridge':p.id.startsWith('ring-bridge')?'ring-bridge':p.id,scad_ring:Math.max(1,p.row+1),scad_station:(p.spec.station||0)+1,assembly_rotations_degrees:m.instances.filter(i=>i.part.id===p.id).map(i=>i.a*180/Math.PI),vertical_shell_wall_range_mm:p.wallRange,grip_stack_range_mm:p.gripRange,petal_angle_degrees:p.angle,support_ribs:p.supportMeshes.length,support_volume_cm3:+(p.supportMeshes.reduce((s,r)=>s+volume(r),0)/1000).toFixed(3),bed_rotation_degrees:p.bedRotation,dimensions_mm:p.dim.map(x=>+x.toFixed(3)),solid_volume_cm3:+(volume(p.mesh)/1000).toFixed(3)})),notes:['Revision 7 requires matching panels and all seam plates. Revision-5 and revision-6 hubs and caps remain compatible. The 60 mm mount bolt circle is unchanged.','Use M4 bolts through round 4.6 mm bores. Hex pockets capture nut rotation; hold nuts in place until bolts engage.','Cap head seats require heads no larger than 8 mm diameter and 2.2 mm height. Ordinary socket-cap heads will not sit below the front.','Captured petal lips and interrupted grooves locate the roots; the parabolic cap closes the joint. Seat, pilot and nut clearances follow the fit-clearance setting.','Physical printing, strength and fit remain unvalidated. Inspect all supported shells in the slicer.']};}
function standardGuide(m){const p=m.p;return `# PETAL — print and assembly

${p.diameter} mm dish · ${m.layout.n} petals × ${m.layout.rows} rings · all dimensions in mm.

## Hardware and fastening

${p.jointStyle?`Revision 7 uses round Ø4.6 bores and rear hex pockets, ${ (7+2*p.jointClearance).toFixed(2)} mm across flats. Nominal M4 nuts are 7 mm across flats and 3.2 mm tall. Pockets have at least 3.4 mm depth. Seam plates retain at least 2.8 mm above the nut bearing face; hub pockets retain at least 2 mm. The clearance setting adjusts nut fit, plate seats and hub registration. Test actual hardware and printer tolerances before printing a complete set.

Use ${m.bolts} M4 assembly screws and nuts, plus four mount screws and nuts. The ${m.layout.n+4} front-cap screw positions require ISO 7380-1 button heads no larger than Ø8 mm × 2.2 mm tall. Their Ø8.4 counterbores have flat floors at least 2.4 mm below the lowest surrounding dish surface. Ordinary socket-cap heads do not fit these recesses. Use front washers on the petal-to-plate fasteners; do not place washers beneath recessed cap heads or inside nut pockets. Preload the nuts into each plate and cover the rear pocket mouths with removable tape before positioning the plate. Leave clearance for protruding screw ends and remove the tape after both screws have engaged. The hex pockets stop rotation; the tape holds the nuts during handling.`:`Legacy straps use round Ø4.6 bores with ordinary M4 through-bolts, washers and nuts. There are no captive-nut pockets or captured roots in this alternate interface. Mount bolt circle: 40 mm. Assembly screws: ${m.bolts}; mount screws: four additional.`}

Grip lengths run from the screw bearing face to the nut bearing face. Add nut height, any front washer and thread protrusion when choosing screw length. Recessed cap screws use the counterbore floor as their bearing face. Mount-adapter thickness is additional.

${m.parts.filter(p=>p.gripRange).map(p=>`- ${p.name}: ${p.gripRange.map(x=>x.toFixed(1)).join('–')} mm grip`).join('\n')}

Hardware envelope references: [M4 hex nut](https://www.accu.co.uk/hexagon-nuts/766467-NUT203M4), [M4 button-head dimensions](https://www.accu.co.uk/socket-button-screws/8135-SSB-M4-22-A2). These describe dimensions, not a required vendor or screw length.

## Captured center

${p.jointStyle?`The front cap follows the same parabola as the dish. Petal roots have recessed shoulders ${clampDepth(p).toFixed(2)} mm below the front and at least ${Math.max(3,p.thickness).toFixed(1)} mm root web underneath. A 2 mm-deep segmented lip seats in the rear hub groove. Interrupted sections act as indexing stops against circumferential sliding. Radial groove walls locate the petals; the cap traps them axially.

A Ø78–84 mm annular pilot on the cap enters a matching rear-hub channel. There is 0.2 mm axial clearance at the pilot and root groove floors; cap-to-petal shoulders meet at their nominal surfaces. The mount retains four M4 positions on a 60 mm bolt circle at 45° clocking, with a Ø30 center opening. Matching access holes and recessed heads in the cap keep mount fasteners reachable. Add the rear mount adapter after placing the nuts.

The exposed cap is nominally flush with the adjacent parabolic surface, apart from intentional seams and screw openings. Reinforcement is underneath. Use matching revision-7 panels and seam plates. Revision-5 and revision-6 hubs and caps remain compatible; older keyed hubs and root profiles are incompatible.`:'The legacy hub remains a pressure-clamped sandwich assembly. Staggering is still independently selectable.'}

## Layout and plates

Ring offsets: ${m.ringPhases.map((a,j)=>`${j+1}: ${(a*180/Math.PI).toFixed(2)}°`).join(' / ')}. Staggering is independent of connector style. Explicit petal and ring counts fix segmentation when changing construction; automatic printer fitting may select a different count.

${p.jointStyle?'Same-ring plates use two bolts; between-ring plates use four. The plates enter 1 mm footprint seats, giving 0.8 mm nominal engagement before clamping. Rear reinforcement blends out over 4 mm and preserves at least 2.45 mm nominal panel wall beneath these seats. Plate backs follow the local angle and export rotated flat onto the bed. Wider plate ends protect the hex pockets; the nut bearing wall is 2.8 mm. The 0.2 mm axial assembly clearance closes when tightened, so start all screws loosely before tightening the joint.':'Legacy straps use two bolts each and matching hole locations for aligned or staggered rings.'}

Staggered recessed plates join three panels at each inner-ring seam: one bolt per inner petal, two in the outer petal. Each boundary uses one plate per inner seam. Aligned rings and legacy straps retain two-panel overlap connectors. Adaptive spacing adds recessed plates along long same-ring seams and aligned ring overlaps. Changing segmentation, spacing or staggering requires regenerated matching parts.

## Printing

${m.parts.map(p=>`- ${p.id}${p.supportMeshes.length?'_supported':''}_qty-${p.qty}.stl: print ${p.qty}; ${p.dim.map(x=>x.toFixed(1)).join(' × ')} mm; SCAD station ${(p.spec.station||0)+1}`).join('\n')}

The parabolic front remains smooth between the hardware recesses and seams. Faceted backs add material; the center root profile overrides the rear facets locally and blends back by radius 66 mm. Reinforced seats retain at least 2.45 mm nominal panel wall, even when the field shell is set to 1.6 mm. Root web thickness is separate and at least 3 mm.

Supported petals include breakaway ribs with their original relative placement. Keep all shells together. Inspect the root lip, recesses, nut-pocket roofs and cap pilot in the slicer. The cap has curved faces and may need generated supports; built-in ribs apply only to petals. Test one panel, one connector and the hub fit before printing the full set. Do not coat mating seats, nut pockets or indexing grooves before that check.

CAD volume assumes solid material and is not filament usage. Flat print orientation and all geometric clearance checks do not establish strength or print reliability.

## Assembly order

1. Print the three FIT_TEST files first. Check the actual nuts, bores and seat fit, then preload the rear nuts using removable tape. Dry-fit the hub and cap as a separate check.
2. Lower inner petal roots into the matching hub groove; align the interrupted lip sections with the indexing stops.
3. Lower the parabolic cap over the root shoulders and seat its annular pilot. Start the root screws loosely.
4. Add outer rings at the listed offsets. Fit two-bolt side plates and four-bolt ring plates from behind in their reinforced locating seats. Start every screw by hand, leaving the joints loose until the full ring is aligned.
5. Use front washers at the panel screws. Tighten by hand gradually in alternating positions; stop if a washer dishes the panel or the nut pocket visibly deforms. Recheck for looseness after the first assembly cycle. Do not force a mismatched root, seat, pilot or nut pocket with bolt torque.
6. Attach the rear mount using the four accessible cap positions and appropriate screw length for the adapter.

## Verification and limits

Meshes are checked for closed topology, bore clearance, nut capture, bearing faces, root/groove fit, pilot clearance, flush front geometry, printer bounds and support separation. The polymer assembly has not been physically printed or load-tested. No structural, wind or RF rating is provided. Finish, feed, feed support and electronics are separate.

Open the exported petal.scad, choose a part, ring and station, render with F6, then export STL. Regenerate the kit after any geometry or hardware-fit change.
`;}
export function kit(m,kernel){const coupon=connectionCoupon(m);return zip([...m.plates.map((plate,i)=>({name:`PACKED/plate-${i+1}.stl`,data:binarySTL(packedPlateMesh(plate))})),...coupon.map(p=>({name:`FIT_TEST/${p.id}.stl`,data:binarySTL(p.output)})),...(coupon.length?[{name:'FIT_TEST/README.md',data:couponGuide(m)}]:[]),...m.parts.map(p=>({name:`STL/${p.id}${p.supportMeshes.length?'_supported':''}_qty-${p.qty}.stl`,data:binarySTL(p.output)})),{name:(m.p.perforate||m.p.fastenerStyle?'petal-snapshot.scad':'petal.scad'),data:scadSource(m,kernel)},{name:'parameters.json',data:JSON.stringify(manifest(m),null,2)},{name:'ASSEMBLY.md',data:guide(m)}]);}


function standardCouponGuide(m){return `# Side-joint fit test — revision 7

Print coupon-left.stl, coupon-right.stl and coupon-plate.stl once each. These are cropped from the first ring's actual joint, with the full kit's print orientations. They are supplied without breakaway supports; inspect them in the slicer and generate supports as needed.

Use the same material, drying, wall count, layer height and orientation planned for the dish. This is especially useful before committing to a PCTG or ASA/ABS kit. It is a fit and assembly check, not a load rating.

1. Try an M4 bolt in both panel bores and a nominal M4 nut in each rear hex pocket. Nuts should seat against their bearing faces without splitting the pocket walls.
2. Hold the two coupon seam edges together with the configured ${m.p.gap.toFixed(2)} mm gap. Seat the plate behind them; its perimeter should enter both recesses without bolt force.
3. Preload the nuts and temporarily tape the rear pocket mouths. Insert two screws from the parabolic front with washers, starting both by hand before tightening either.
4. Check that both coupons sit at the same front height and that neither rocks on the plate. Check for interference, local panel dimpling or pocket damage as you tighten by hand.
5. Remove and assemble the joint again, then gently flex it in both directions and check for movement or cracks. Any loose fit, whitening or deformation needs correction before printing the dish. Adjust joint clearance and regenerate the complete kit if the fit is too tight.

The full hub, root capture and three-panel ring junction need their own dry fit; this small coupon covers the same-ring side joint only.
`;}


// New construction modes use the exact generated meshes in OpenSCAD. Keeping
// this explicit avoids exporting an older parametric kernel with the wrong
// blind holes, front pockets, or perforations.
export function scadSource(m,kernel){
 if(!m.p.perforate&&!m.p.fastenerStyle)return parametricScadSource(m,kernel);
 const serial=mesh=>JSON.stringify(mesh.v.map(v=>v.map(x=>+x.toFixed(6))));
 let text='// PETAL geometry snapshot. Regenerate in PETAL after changing dimensions.\n// Parameters: '+JSON.stringify(m.p)+'\npart = "assembly"; // [assembly,'+m.parts.map(p=>p.id).join(',')+']\nprinting = true;\n';
 m.parts.forEach((p,i)=>{
  text+=`module assembly_${i}(){polyhedron(points=${serial(p.mesh)},faces=${JSON.stringify(p.mesh.f.map(f=>[...f].reverse()))},convexity=20); }\n`;
  text+=`module print_${i}(){polyhedron(points=${serial(p.output)},faces=${JSON.stringify(p.output.f.map(f=>[...f].reverse()))},convexity=20); }\n`;
  text+=`if(part=="${p.id}"){if(printing)print_${i}();else assembly_${i}();}\n`;
 });
 text+='if(part=="assembly"){\n';
 for(const inst of m.instances)text+=`rotate([0,0,${inst.a*180/Math.PI}])assembly_${m.parts.indexOf(inst.part)}();\n`;
 return text+'}\n';
}


export function manifest(m){
 const data=baseManifest(m),p=m.p;
 data.generator='PETAL 3.3';
 data.interface.fastening=['front screws / rear nuts','rear screws / front hex nuts','rear screws / blind M4 inserts'][p.fastenerStyle];
 data.interface.revision=p.fastenerStyle?8:data.interface.revision;
 data.cap_button_head_screws=p.fastenerStyle?0:data.cap_button_head_screws;
 data.captured_nuts=p.fastenerStyle===2?0:data.captured_nuts;
 data.heat_set_inserts=p.fastenerStyle===2?m.bolts+4:0;
 data.perforations={enabled:Boolean(p.perforate),diameter_mm:6,quantity:m.parts.reduce((s,part)=>s+(part.spec.perforations?.length||0)*part.qty,0)};
 data.openscad_format=p.perforate||p.fastenerStyle?'exact mesh snapshot; regenerate parameters in PETAL':'parametric kernel';
 data.packed_plates=m.plates.map((plate,i)=>({file:`PACKED/plate-${i+1}.stl`,placements:plate.placements.map(({part,yaw,x,y})=>({part:part.id,rotation_degrees:yaw,x_mm:x,y_mm:y}))}));
 if(p.fastenerStyle){
  data.interface.rear_head_recess_diameter=8.4;data.interface.rear_head_max_height=2.2;
  delete data.interface.nut_pocket_min_depth;delete data.interface.minimum_plate_nut_bearing_wall;delete data.interface.minimum_hub_nut_bearing_wall;
  delete data.interface.cap_head_recess_diameter;delete data.interface.cap_head_max_diameter;delete data.interface.cap_head_max_height;
  if(p.fastenerStyle===2){delete data.interface.nut_pocket_across_flats;data.interface.insert={reference:'ruthex RX-M4x8.1',hole_diameter_mm:p.insertDiameter,minimum_cavity_depth_mm:p.insertDepth,minimum_front_skin_mm:2};}
  data.notes=['Revision 8 fastening requires a regenerated full kit, including hub and cap.','Rear button heads must fit diameter 8 mm / height 2.2 mm. Mount adapter thickness affects mount screws.','Print FIT_TEST with the final material and slicer settings before a full kit.','Physical strength, heat-setting and RF performance are unverified.'];
  data.parts.forEach((part,i)=>{part.grip_stack_range_mm=m.parts[i].gripRange;part[p.fastenerStyle===2?'maximum_blind_screw_length_mm':'maximum_flush_screw_length_mm']=m.parts[i].maxScrewLengthRange;});
 }
 return data;
}
function optionNotes(m){return `${m.p.perforate?'## Perforated petals\n\nOptional 6 mm holes are omitted near roots, edges and joint seats. Inspect hole roofs and supports in the slicer. Perforations remove reflector area; RF behavior and any required reflective skin must be checked for the intended application.\n\n':''}${m.p.packPlates?'## Packed beds\n\nPACKED contains every required copy, including built-in supports, arranged with rotated bounding boxes and 6 mm separation. Print either these beds OR the individual STL quantities, not both. Keep the exported positions; brim widths above 3 mm can overlap. Packing rotates about the bed normal and preserves each part’s validated tilt. This is a conservative arrangement, not an optimum nesting solution.\n\n':''}${m.p.perforate||m.p.fastenerStyle?'## OpenSCAD export\n\nNew construction options export an exact mesh snapshot with an assembly/part selector. Change dimensions in PETAL and regenerate; the snapshot is not a second parametric generator.\n\n':''}`;}
export function guide(m){
 if(!m.p.fastenerStyle)return optionNotes(m)+standardGuide(m);
 const p=m.p,insert=p.fastenerStyle===2;
 return `# PETAL — revision 8 fastening

${p.diameter} mm dish · ${m.layout.n} petals × ${m.layout.rows} rings.

${optionNotes(m)}## Hardware

Use ${m.bolts} M4 assembly screws and four mount screws. Screws enter from behind. Rear head recesses accept ISO 7380-1 button heads no larger than 8 mm diameter and 2.2 mm high. Rear plates and hub have round head seats, not nut traps.

${insert?`Use ${m.bolts+4} M4 heat-set inserts. Default reference: ruthex RX-M4x8.1 (8.1 mm long), with a ${p.insertDiameter} mm installation hole and at least ${p.insertDepth} mm cavity depth. The blind cavities preserve at least 2 mm of front skin; additional material is placed behind the dish. Insert hole sizes are manufacturer-specific. [Manufacturer reference](https://www.ruthex.de/en/collections/gewindeeinsatze/products/ruthex-gewindeeinsatz-m4-50-stuck-rx-m4x8-1-messing-gewindebuchsen).`:`Use ${m.bolts+4} nominal M4 hex nuts (7 mm across flats / 3.2 mm high). Front pockets are ${7+2*p.jointClearance} mm across flats with a flat bearing floor below the face. Keep the screw tip below the adjacent face; the pocket is open, although the nut sits recessed.`}

## Screw length

${m.parts.filter(part=>part.gripRange).map(part=>`- ${part.name}: ${part.gripRange.map(x=>x.toFixed(1)).join('–')} mm ${insert?'under-head to insert mouth':'under-head to nut bearing face'}${part.maxScrewLengthRange?`; ${insert?'blind':'flush-face'} maximum ${part.maxScrewLengthRange.map(x=>x.toFixed(1)).join('–')} mm${insert?', leaving 1 mm to the pocket roof':''}`:''}.`).join('\n')}

${insert?'Add the intended thread engagement to the grip. Use the smallest blind maximum for a shared screw length; do not bottom a screw against the front skin.':'Add nut engagement to the grip and check that the tip remains recessed.'} Mount adapter thickness is additional; its screw bearing face must be measured on the actual adapter.

## Assembly

1. Print the three FIT_TEST pieces using the intended PCTG or ASA/ABS settings. Check pocket fit, plate seating and screw access.
2. ${insert?'Install inserts from behind into the petals and front cap before assembling the dish. Use the insert supplier’s heating guidance and a depth stop; verify the front has not bulged.':'Place the nuts into the front pockets of the petals and cap. Temporarily retain them during handling if needed.'}
3. Seat the petal roots in the rear hub and lower the cap onto the root shoulders. Root screws pass from behind through the hub and root into the cap.
4. Seat the rear plates and start every screw by hand. Align the ring before tightening progressively. Stop if a boss, pocket or panel deforms.
5. Dry-fit the full hub and a three-panel ring junction. Repeat assembly and check for looseness before printing the complete dish.

Regenerate panels, plates, hub and cap together when changing fastening modes. No tested load, wind, creep or RF rating is implied.
`;}
export function couponGuide(m){
 if(!m.p.fastenerStyle)return standardCouponGuide(m);
 return `# Side-joint coupon

Print coupon-left.stl, coupon-right.stl and coupon-plate.stl once each, preserving their exported orientations and adding slicer supports where needed. These are cropped from the actual first-ring joint. Use the intended PCTG or ASA/ABS settings.

${m.p.fastenerStyle===2?'Heat-set one M4 insert from behind into each panel coupon. The front should remain closed and undistorted.':'Seat an M4 nut into each front hex pocket.'} Fit the plate behind both coupons and insert two button-head screws from the rear. Start both by hand, check the seam alignment, then tighten progressively. Verify screw length on the coupon before using it on a full panel.

The coupon tests a side seam only; the complete hub and ring junction need separate dry fitting.

`+guide(m);
}
