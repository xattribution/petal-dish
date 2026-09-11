import {binarySTL,volume,zip,clampDepth} from './geometry.js';
export function scadSource(m,kernel){const p=m.p;return `// PETAL / Parametric Expeditionary Tactical Aperture Lab\n// Editable source. Dimensions in mm. No external libraries.\n/* [Dish] */\ndiameter = ${p.diameter}; // [180:1:1200]\nfd = ${p.fd}; // [0.25:0.01:0.80]\nthickness = ${p.thickness}; // [1.6:0.1:6]\n/* [Joints] */\njoint_style = ${p.jointStyle}; // [0:Legacy straps,1:Recessed plates]\njoint_clearance = ${p.jointClearance}; // [0.1:0.05:0.4]\nadaptive_joints = ${p.adaptiveJoints}; // [0:Off,1:On]\nconnector_spacing = ${p.connectorSpacing}; // [60:5:180]\nstagger_rings = ${p.staggerRings}; // [0:Aligned,1:Half-petal stagger]\n/* [Rear construction] */\nrear_style = ${p.rearStyle}; // [0:Curved shell,1:Two-facet rear]\nfacet_angle = ${p.facetAngle}; // [10:1:15]\n/* [Printer] */\nbed_x = ${p.bedX};\nbed_y = ${p.bedY};\nbed_z = ${p.bedZ};\nmargin = ${p.margin};\n/* [Segmentation] */\n// 0 = automatic. Explicit values avoid repeating the search on each render.\nsectors = ${m.layout.n}; // [0:2:32]\nrows = ${m.layout.rows}; // [0:1:12]\ngap = ${p.gap}; // [0.2:0.1:1]\nresolution = ${p.resolution}; // [2:1:10]\n/* [Print orientation and breakaway ribs] */\nsupports = ${p.supports}; // [0:Off,1:On]\nprint_angle = ${p.printAngle}; // -1 auto; 0 low profile; or 45-70 degrees\nrib_count = ${p.ribCount}; // [2:1:5]\ncontact_gap = ${p.contactGap}; // [0.1:0.05:0.4]\ncontact_width = ${p.contactWidth}; // [0.4:0.1:0.8]\nrib_pitch = ${p.ribPitch}; // [6:1:20]\n/* [Export] */\npart = "assembly"; // [assembly,panel,side-bridge,ring-bridge,hub-rear,hub-clamp]\nring = 1; // One-based radial ring index\nstation = 1; // One-based saddle position in this ring\n\n${kernel}`;}
export function manifest(m){const p=m.p;return {generator:'PETAL 3.1',name:'Parametric Expeditionary Tactical Aperture Lab',units:'mm',parameters:p,segmentation:{petals:m.layout.n,rings:m.layout.rows,staggered:Boolean(p.staggerRings),ring_offsets_degrees:m.ringPhases.map(a=>a*180/Math.PI),adaptive_connectors:Boolean(p.jointStyle&&p.adaptiveJoints),target_connector_spacing_mm:p.connectorSpacing},focal_length:m.focal,dish_depth:m.depth,interface:{revision:p.jointStyle?6:1,type:'custom',joint:p.jointStyle?'Recessed plates: two bolts within rings, four between rings':'Legacy two-bolt straps',hub_outer_diameter:120,front_clamp_outer_diameter:120,center_opening:30,rear_mount_bolt_circle:p.jointStyle?60:40,rear_mount_holes:4,rear_mount_clocking_degrees:45,round_bore_diameter:4.6,hardware:'M4 bolts, nuts and washers',...(p.jointStyle?{seat_depth:.35,seat_clearance_per_side:p.jointClearance,nut_pocket_across_flats:7+2*p.jointClearance,nut_pocket_min_depth:3.4,nut_nominal_across_flats:7,nut_nominal_height:3.2,minimum_nut_bearing_wall:2,cap_head_recess_diameter:8.4,cap_head_max_diameter:8,cap_head_max_height:2.2,cap_root_shoulder_depth:clampDepth(p),root_lip_depth:2,cap_pilot_inner_diameter:78,cap_pilot_outer_diameter:84,axial_assembly_gap:.2}:{})},assembly_bolts:m.bolts,mount_bolts:4,cap_button_head_screws:p.jointStyle?m.layout.n+4:0,captured_nuts:p.jointStyle?m.bolts+4:0,parts:m.parts.map(p=>({file:`${p.id}${p.supportMeshes.length?'_supported':''}_qty-${p.qty}.stl`,quantity:p.qty,scad_part:p.kind==='panel'?'panel':p.id.startsWith('side-bridge')?'side-bridge':p.id.startsWith('ring-bridge')?'ring-bridge':p.id,scad_ring:Math.max(1,p.row+1),scad_station:(p.spec.station||0)+1,assembly_rotations_degrees:m.instances.filter(i=>i.part.id===p.id).map(i=>i.a*180/Math.PI),vertical_shell_wall_range_mm:p.wallRange,grip_stack_range_mm:p.gripRange,petal_angle_degrees:p.angle,support_ribs:p.supportMeshes.length,support_volume_cm3:+(p.supportMeshes.reduce((s,r)=>s+volume(r),0)/1000).toFixed(3),bed_rotation_degrees:p.bedRotation,dimensions_mm:p.dim.map(x=>+x.toFixed(3)),solid_volume_cm3:+(volume(p.mesh)/1000).toFixed(3)})),notes:['Revision 6 requires matching panels and ring plates. Revision-5 hubs and caps remain compatible. The 60 mm mount bolt circle is unchanged.','Use M4 bolts through round 4.6 mm bores. Hex pockets capture nut rotation; hold nuts in place until bolts engage.','Cap head seats require heads no larger than 8 mm diameter and 2.2 mm height. Ordinary socket-cap heads will not sit below the front.','Captured petal lips and interrupted grooves locate the roots; the parabolic cap closes the joint. Seat, pilot and nut clearances follow the fit-clearance setting.','Physical printing, strength and fit remain unvalidated. Inspect all supported shells in the slicer.']};}
export function guide(m){const p=m.p;return `# PETAL — print and assembly

${p.diameter} mm dish · ${m.layout.n} petals × ${m.layout.rows} rings · all dimensions in mm.

## Hardware and fastening

${p.jointStyle?`Revision 6 uses round Ø4.6 bores and rear hex pockets, ${ (7+2*p.jointClearance).toFixed(2)} mm across flats. Nominal M4 nuts are 7 mm across flats and 3.2 mm tall. Pockets have at least 3.4 mm depth and 2 mm material above the nut bearing face. The clearance setting adjusts nut fit, plate seats and hub registration. Test actual hardware and printer tolerances before printing a complete set.

Use ${m.bolts} M4 assembly screws and nuts, plus four mount screws and nuts. The ${m.layout.n+4} front-cap screw positions require ISO 7380-1 button heads no larger than Ø8 mm × 2.2 mm tall. Their Ø8.4 counterbores have flat floors at least 2.4 mm below the lowest surrounding dish surface. Ordinary socket-cap heads do not fit these recesses. Use front washers on the petal-to-plate fasteners; do not place washers beneath recessed cap heads or inside nut pockets. Nuts need temporary support until their bolts engage; the hex pockets stop rotation, not loss during handling.`:`Legacy straps use round Ø4.6 bores with ordinary M4 through-bolts, washers and nuts. There are no captive-nut pockets or captured roots in this alternate interface. Mount bolt circle: 40 mm. Assembly screws: ${m.bolts}; mount screws: four additional.`}

Grip lengths run from the screw bearing face to the nut bearing face. Add nut height, any front washer and thread protrusion when choosing screw length. Recessed cap screws use the counterbore floor as their bearing face. Mount-adapter thickness is additional.

${m.parts.filter(p=>p.gripRange).map(p=>`- ${p.name}: ${p.gripRange.map(x=>x.toFixed(1)).join('–')} mm grip`).join('\n')}

Hardware envelope references: [M4 hex nut](https://www.accu.co.uk/hexagon-nuts/766467-NUT203M4), [M4 button-head dimensions](https://www.accu.co.uk/socket-button-screws/8135-SSB-M4-22-A2). These describe dimensions, not a required vendor or screw length.

## Captured center

${p.jointStyle?`The front cap follows the same parabola as the dish. Petal roots have recessed shoulders ${clampDepth(p).toFixed(2)} mm below the front and at least ${Math.max(3,p.thickness).toFixed(1)} mm root web underneath. A 2 mm-deep segmented lip seats in the rear hub groove. Interrupted sections act as indexing stops against circumferential sliding. Radial groove walls locate the petals; the cap traps them axially.

A Ø78–84 mm annular pilot on the cap enters a matching rear-hub channel. There is 0.2 mm axial clearance at the pilot and root groove floors; cap-to-petal shoulders meet at their nominal surfaces. The mount retains four M4 positions on a 60 mm bolt circle at 45° clocking, with a Ø30 center opening. Matching access holes and recessed heads in the cap keep mount fasteners reachable. Add the rear mount adapter after placing the nuts.

The exposed cap is nominally flush with the adjacent parabolic surface, apart from intentional seams and screw openings. Reinforcement is underneath. Use matching revision-6 panels and ring plates. Revision-5 hubs and caps remain compatible; older keyed hubs and root profiles are incompatible.`:'The legacy hub remains a pressure-clamped sandwich assembly. Staggering is still independently selectable.'}

## Layout and plates

Ring offsets: ${m.ringPhases.map((a,j)=>`${j+1}: ${(a*180/Math.PI).toFixed(2)}°`).join(' / ')}. Staggering is independent of connector style. Explicit petal and ring counts fix segmentation when changing construction; automatic printer fitting may select a different count.

${p.jointStyle?'Same-ring plates use two bolts; between-ring plates use four. Shallow 0.35 mm footprint seats remain along panel seams. Plate backs follow the local angle and export rotated flat onto the bed. Added thickness around the nut pockets provides a horizontal nut bearing face and sufficient pocket depth.':'Legacy straps use two bolts each and matching hole locations for aligned or staggered rings.'}

Staggered recessed plates join three panels at each inner-ring seam: one bolt per inner petal, two in the outer petal. Each boundary uses one plate per inner seam. Aligned rings and legacy straps retain two-panel overlap connectors. Adaptive spacing adds recessed plates along long same-ring seams and aligned ring overlaps. Changing segmentation, spacing or staggering requires regenerated matching parts.

## Printing

${m.parts.map(p=>`- ${p.id}${p.supportMeshes.length?'_supported':''}_qty-${p.qty}.stl: print ${p.qty}; ${p.dim.map(x=>x.toFixed(1)).join(' × ')} mm; SCAD station ${(p.spec.station||0)+1}`).join('\n')}

The parabolic front remains smooth between the hardware recesses and seams. Faceted backs add material; the center root profile overrides the rear facets locally and blends back by radius 66 mm. The minimum 1.6 mm shell retains 1.25 mm at the shallow seam seats. Root web thickness is separate and at least 3 mm.

Supported petals include breakaway ribs with their original relative placement. Keep all shells together. Inspect the root lip, recesses, nut-pocket roofs and cap pilot in the slicer. The cap has curved faces and may need generated supports; built-in ribs apply only to petals. Test one panel, one connector and the hub fit before printing the full set. Do not coat mating seats, nut pockets or indexing grooves before that check.

CAD volume assumes solid material and is not filament usage. Flat print orientation and all geometric clearance checks do not establish strength or print reliability.

## Assembly order

1. Place nuts in the rear pockets. Temporarily retain them if needed.
2. Lower inner petal roots into the matching hub groove; align the interrupted lip sections with the indexing stops.
3. Lower the parabolic cap over the root shoulders and seat its annular pilot. Start the root screws loosely.
4. Add outer rings at the listed offsets. Fit two-bolt side plates and four-bolt ring plates from behind in their shallow seats.
5. Tighten gradually in alternating positions. Do not force a mismatched root, seat, pilot or nut pocket with bolt torque.
6. Attach the rear mount using the four accessible cap positions and appropriate screw length for the adapter.

## Verification and limits

Meshes are checked for closed topology, bore clearance, nut capture, bearing faces, root/groove fit, pilot clearance, flush front geometry, printer bounds and support separation. The polymer assembly has not been physically printed or load-tested. No structural, wind or RF rating is provided. Finish, feed, feed support and electronics are separate.

Open the exported petal.scad, choose a part, ring and station, render with F6, then export STL. Regenerate the kit after any geometry or hardware-fit change.
`;}
export function kit(m,kernel){return zip([...m.parts.map(p=>({name:`STL/${p.id}${p.supportMeshes.length?'_supported':''}_qty-${p.qty}.stl`,data:binarySTL(p.output)})),{name:'petal.scad',data:scadSource(m,kernel)},{name:'parameters.json',data:JSON.stringify(manifest(m),null,2)},{name:'ASSEMBLY.md',data:guide(m)}]);}
